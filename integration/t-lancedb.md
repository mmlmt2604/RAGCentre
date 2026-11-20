# Integration fragment: t-lancedb (LanceDB vector store)

Branch `v0.1.13-t-lancedb`. Owns `src/ragcentre/stores/lancedb.py`, `src/ragcentre/stores/filter_datafusion.py`,
`tests/test_vs_lancedb.py`. Nothing else is edited.

## pyproject extras
```toml
lancedb = [
    "lancedb>=0.41.0",
]
```
(0.41.0 is the only version tested; it pulls in `pyarrow`, `numpy`, `pydantic`, etc. The adapter imports
`lancedb`, `lancedb.index` and `pyarrow` lazily through `require_extra`, with no static imports, so mypy
needs no override.) `uv.lock`: run `uv lock`.

## registry BUILTIN
```python
    "lancedb": "ragcentre.stores.lancedb:STORE_TYPE",
```

## docker-compose service (host port from the table below)
None. The default test run uses a local directory under `tmp_path`.

## ci stores matrix entry
No service. Add `tests/test_vs_lancedb.py` to the `stores` job's pytest line (it runs on `--all-extras`,
Python 3.13). The test module skips cleanly when the extra is missing, so the default job needs no change.
Check lancedb's Python 3.15 wheel status before adding the extra to the default (3.15) job; it was tested
on 3.13 only.

## README store-table row
| `lancedb` | `ragcentre[lancedb]` | Embedded or object-store (`s3://`, `gs://`, `az://`) tables, or LanceDB Cloud (`db://`). Exact search below `ann_index_min_rows`, ANN index above it. |

## DECISIONS.md
**V5 lancedb (t-lancedb).**
1. **Catalog without a catalog table.** LanceDB 0.41 can update per-field metadata but has no API to
   change *schema-level* metadata after creation. Metric and the immutable index options are written once
   into the Arrow schema metadata (key `ragcentre`) at `create_table`. Dimension (fixed-size-list size) and
   filter fields (typed columns: string/int64/float64/bool) are read from the columns, so adding a field is
   `add_columns` and the schema metadata is untouched (verified: it survives `add_columns`). No
   `InStoreCatalog` or `LocalCatalog` is used; `catalog.reconcile` supplies the mismatch and merge rules.
   Old rows have NULL in an added column; the filter compiler treats NULL as "absent" like `matches()`.
2. **Reserved columns.** Table columns are `_rc_id` and `_rc_vector` (not `id`/`vector`), so user schema
   fields named `id`, `vector` or `Year` work. Fields starting `_rc_` are rejected with `StoreError`.
3. **Scores** from `_distance` (verified by running 0.41.0): cosine `1 - d`; dot `1 - d` (`_distance` is
   `1 - dot`); l2 `-sqrt(d)` because LanceDB returns the **squared** distance. A zero vector has no cosine
   distance, and LanceDB omits such rows from cosine results (verified): they cannot be retrieved. Accepted.
4. **ANN threshold.** `ann_index_min_rows` (default 50000, minimum 256, empty = never) and `index_type`
   (`IVF_FLAT` default, `IVF_SQ`, `IVF_PQ`, `IVF_HNSW_SQ`; all four verified at dims 3, 7, 8, 100) are
   index-shaping: both are `immutable_fields` and are recorded in the schema metadata and compared on every
   `ensure_collection` (`CollectionMismatch`). The index (named `_rc_vector_idx`) is built in the upsert
   that first reaches the threshold; `optimize()` runs when unindexed rows exceed the indexed ones and every
   64 upserts. Maintenance failures are logged and never fail an upsert (search stays correct: unindexed rows
   are scanned flat). `nprobes` is a mutable query-time option.
5. **Filtering.** `where(sql, prefilter=True)`, no post-filtering. In 0.41.0 an IVF index with prefilter
   returned k rows whenever k existed (run, 1% selectivity, 5000 rows), contrary to the docs' warning. As
   insurance, a filtered query that returns fewer than k rows while an index exists is re-run with
   `bypass_vector_index()` (an exact scan of the filtered rows). So `filter_exact=True` for local and
   object-store URIs. `db://` is declared `filter_exact=False`, `consistency="eventual"` (not run).
6. **DataFusion compiler** (`filter_datafusion.py`): backtick-quoted identifiers checked with
   `FIELD_RE.fullmatch` plus an allow-list of the collection's columns; string literals with `'` doubled
   (a backslash is not an escape; NUL, newline, comment markers and quotes verified harmless through the
   engine; lone surrogates rejected); ints must fit int64; floats finite; `$ne`/`$nin` compile to
   `(col IS NULL OR ...)`. Subqueries are rejected by Lance itself (verified).
7. **Finding (not fixed here, `filter_sql.py` is not ours):** `filter_sql.check_field` uses
   `FIELD_RE.match`, and `FIELD_RE` ends in `$`, so a field name with a trailing newline (`"a\n"`) passes.
   `models/metadata.py` uses `fullmatch` so schemas cannot contain one, but `check_field` should use
   `fullmatch` too.
8. **Run vs only read.** *Run* against lancedb 0.41.0, local directory: everything above on scores, filters,
   `add_columns`, schema-metadata persistence, `update_field_metadata` (per-field only), index types,
   scalar BTree index on an empty table, `merge_insert`, `delete`, missing-table errors, `exist_ok`,
   concurrency (contract: 4 threads; 4 separate handles ensuring one collection), dims up to 32768 (so
   `max_dim=None`), `ragcentre store check`. *Only read:* `db://` (LanceDB Cloud: whether it accepts schema
   metadata, `bypass_vector_index`, prefilter semantics, consistency), `s3://` and the `region` mapping
   (`storage_options={"aws_region": ...}` for object stores, `region=` for `db://`). Cloud/object-store
   behaviour is therefore unverified.
9. Upserts keep only metadata keys that are schema columns (others are not filterable); duplicate ids in a
   batch keep the last; `max_batch` is 1000.
