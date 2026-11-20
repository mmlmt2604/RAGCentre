# Fragment: t-sqlitevec (V4 items 4 and 5, sqlite_vec)

## DECISIONS.md text

**sqlite_vec: `prefix`, per-instance capabilities, no `dot`.**

- `prefix` (default empty, lowercase `[a-z0-9_]`, at most 64 characters) is prepended to the logical
  collection name to form the physical table name. The physical name is also the key in
  `_collections`, so two prefixes can share one database file without colliding. It is re-validated
  per operation (`^[a-z_][a-z0-9_]{2,127}$`, not `_collections`, not `sqlite_*`; SQLite table names
  are case-insensitive, so uppercase is refused) and raises `StoreError`. With an empty prefix the
  physical name equals the logical name, so existing databases are untouched (no migration). The
  prefix is not in `immutable_fields`, matching qdrant and chroma: it is a connection-level field,
  not an index option; changing it makes existing collections invisible rather than corrupting them.
  `immutable_fields` stays empty (sqlite_vec has no index-shaping options).
- Capabilities are set per instance: `metrics={cosine, l2}`, `max_batch=1000`, `max_dim=None`,
  `consistency="immediate"`, `filter_exact=True`.
- `dot` stays unsupported (Decision V4.5).

Verified by running (sqlite-vec 0.1.9, the pinned and newest stable release; PyPI lists only 0.1.10
alphas beyond it):
- `pragma_function_list` shows only `vec_distance_cosine`, `vec_distance_l2`, `vec_distance_l1`,
  `vec_distance_hamming` and helpers; calling `vec_distance_dot`, `vec_distance_inner_product` and
  `vec_dot` fails with "no such function". `vec_length` returns the element count (3 for [3,4,0]),
  not the norm, so a dot product cannot be derived in SQL from `vec_distance_l2` plus norms either.
  A Python scoring loop would break exactness and the "no post-filtering" rule, so it was not built.
- Dimension limit: `vec_distance_l2` and `vec_distance_cosine` ran correctly on vectors of
  1,000, 4,096, 8,192, 16,384, 65,536 and 1,000,000 dimensions. No sqlite-vec limit found, so
  `max_dim=None` (the practical bound is SQLite's blob length limit, about 250 million float32s).
- Old-layout compatibility: `tests/test_v4_sqlite_vec.py` builds a database with the pre-`prefix`
  DDL by hand (`_collections` plus a table named after the logical name), opens it with the new
  code, queries (with and without a filter), upserts, and calls `ensure_collection`.
- Only read, not run: nothing; this adapter is in-process.

## README.md

No row change. (Optionally in the store table: sqlite_vec notes `cosine`, `l2`; no `dot`; supports `prefix`.)

## Adapter guide (docs/protocols/vector-store-adapter-guide.md)

Replace the last sentence of the "Scores" section with:

> The sqlite-vec adapter (0.1.9, the newest stable release) offers only cosine and l2: the library
> has no dot-product distance function, and its `vec_length` returns the element count rather than
> the norm, so the dot product cannot be derived from the available distances.

No other change; the "Naming and catalogs" section already describes `prefix`.
