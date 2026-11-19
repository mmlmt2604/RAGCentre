## pyproject extras
None. Verification used the existing lock with `uv sync --frozen --all-extras` on CPython 3.13.15.

## registry BUILTIN
None. Existing adapters were not changed or migrated.

## docker-compose service (host port from the table below)
None.

## ci stores matrix entry
None.

## README store-table row
None. The Python adapter guide is `docs/protocols/vector-store-adapter-guide.md`.

## DECISIONS.md
1. **V1: the eight-method VectorStore interface remains unchanged.** The protocol expresses
   capabilities as a read-only property, accepting both legacy class attributes and per-instance
   attributes. Capabilities adds defaults: `max_dim=None`, `consistency="immediate"`,
   `filter_exact=True`, `max_collections=None`. Registry capabilities remain the type's best case.
2. **Dimension limits have a typed StoreError.** `check_spec` raises `DimensionLimitExceeded`
   when the requested dimension exceeds `max_dim`. Only this subtype is translated by
   `execute_index_vector` into permanent `ExecutionError("store_dimension_limit", ...)`.
   Unrelated store failures retain their old error path. The contract requires a StoreError above
   the declared limit; adapters should use check_spec or the subtype for pipeline classification.
3. **Catalog API for subsequent workers.** `CatalogEntry(dim, metric, fields, options)` serialises
   to/from JSON. Both catalogs expose `get(name)`, `ensure(spec, options=None)`, and `drop(name)`.
   Ensure compares dimension, metric, field types, and effective index options; it adds fields
   without losing existing ones. Options are normalised through JSON before comparison.
   `InStoreCatalog(read, write, remove=None, *, transaction=...)` takes JSON-string callbacks.
   One object serialises its callers. Separate objects/processes must supply a shared transaction
   or lock around the whole read/check/write sequence; callbacks must provide read-after-write
   visibility. A read-back alone is not a distributed atomicity guarantee.
   `LocalCatalog(data_dir, store_type, instance_key)` uses
   `<data_dir>/store_catalogs/<type>.db`, with `(instance_key, collection)` as the primary key and
   `BEGIN IMMEDIATE` around ensures; call `close()` to release connections. Use a stable,
   nonsecret physical-target key, unaffected by credential rotation. Reserve LocalCatalog for
   Pinecone. Losing the file orphans collections, which are rebuildable. Existing adapters retain
   their current metadata. Physical creation, field-index creation, and partial-failure recovery
   remain the adapter's responsibility; do not hold local SQLite transactions across remote calls.
4. **Immutable store configuration uses the existing provider permanent-field mechanism.**
   `VectorStoreType.immutable_fields: tuple[str, ...] = ()` is copied to the wrapped
   `ProviderTypeSpec.fingerprint_fields`. That set already governs 409 `immutable_field` and the
   UI's permanent fields. `fingerprint_for` special-cases vector stores by provider ID, so no
   fingerprint algorithm or ProviderManager change was needed. A store type rejects unknown
   immutable field names. The adapter must also record and compare effective collection options.
5. **Contract hooks preserve the default exact checks.** Override
   `VectorStoreContract.settle(self, store, name)` for eventual stores, and `score_tolerance`
   (default `1e-4`) for measured score precision loss. Settling includes lifecycle writes,
   overwrites, deletions, and concurrent workers. Counts alone do not prove overwrite visibility.
   With instance `filter_exact=False`, filtered checks use brute-force ID recall >= 0.9 (override
   `filter_min_recall` only upwards), while still checking filter membership, score correctness,
   ordering, uniqueness, and result bounds. Unfiltered checks remain exact. Pytest emits a warning
   and records the `filter_mode` user property. `CollectionOptionsContract` takes `options_a`,
   `options_b`, and `open_store(options, tmp_path)`, and closes handles before reopening so local
   databases with exclusive directory locks work. It also exposes the settle hook.
6. **The guide has a runnable, tested example.** `tests/vector_store_example.py` implements all
   eight methods, all three score metrics, filtering before ranking, instance capabilities,
   InStoreCatalog, configuration, factory, and VectorStoreType. Its complete contract suite is in
   `tests/test_v1_adapter_example.py`. It is deliberately ephemeral and documented as such.
   The example lives under tests to stay within this worker's file ownership; no example package
   or shared packaging files were added.
7. **Verification (2026-11-19).** `uv sync --frozen --all-extras` succeeded using the existing
   lock, including Chroma on CPython 3.13.15. Final `uv run pytest -q`: **654 passed, 39 skipped**
   in 18.39s. SQLite, Qdrant local, and Chroma ran unchanged. **32** skipped tests need pgvector
   at 127.0.0.1:54329 (connection refused; Docker is available but no service was started by this
   foundation task); **7** skip the dimension-limit check because no max_dim is declared.
   `uv run ruff check && uv run ruff format --check`: passed, 165 files formatted.
   `uv run mypy`: passed, 97 source files. `git diff --check`: passed.
   Catalog and eventual/approximate/score-tolerance behaviour was exercised with test stores;
   no claim is made about live remote eventual-consistency or approximate-filter guarantees.
   No live database server or cloud account was exercised by this task.
