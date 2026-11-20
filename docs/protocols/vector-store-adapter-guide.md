# Writing a Python vector-store adapter

A vector store is a named collection of `(id, vector, metadata)` records, with filtered top-k
search. Implement the eight methods below, publish a `VectorStoreType` entry point, and run the
contract suite. The core keeps chunk text, document rows, and the record-id ledger in SQLite;
adapters store only vectors and flat, typed metadata.

The complete, runnable example is [tests/vector_store_example.py](../../tests/vector_store_example.py).
It implements exact search over Python dictionaries, uses `InStoreCatalog`, and runs through the
same tests as the built-in adapters in
[tests/test_v1_adapter_example.py](../../tests/test_v1_adapter_example.py). It needs no optional
client package. Its data disappears on close; replace the dictionaries with durable database
operations before using this pattern for actual ingestion.

## Interface and required behaviour

Import the models and helpers from `ragcentre.stores.base`. The method signatures remain:

```python
class VectorStore(Protocol):
    @property
    def capabilities(self) -> Capabilities: ...

    def healthcheck(self) -> None: ...
    def ensure_collection(self, spec: CollectionSpec) -> None: ...
    def collection_info(self, name: str) -> CollectionInfo | None: ...
    def drop_collection(self, name: str) -> None: ...
    def upsert(self, name: str, records: Sequence[Record]) -> None: ...
    def delete(self, name: str, ids: Sequence[str]) -> None: ...
    def query(self, name: str, vector: Sequence[float], k: int,
              filter: Filter | None = None) -> list[Match]: ...
    def close(self) -> None: ...
```

The protocol's read-only `capabilities` property is a typing declaration: your implementation
can use a class attribute, an instance attribute, or a property. No inheritance from the protocol
is required. Existing adapters with `ClassVar[Capabilities]` continue to work.

- `ensure_collection` calls `check_spec(spec, self.capabilities)` before creating anything. It is
  idempotent and safe when multiple ingestion workers call it concurrently. A different dimension,
  metric, field type, or recorded index option raises `ragcentre.errors.CollectionMismatch`.
  New fields are additive: retain old fields and create any required indexes for the new ones.
- `collection_info` returns `CollectionInfo(dim, metric, count)` or `None` for a missing
  collection. Report the actual stored shape; retrieval checks it against the built index.
- `upsert` replaces a record completely, including its metadata. IDs are UUID strings.
  Validate dimensions and reject batches above `max_batch`. The core's `upsert_batched` helper
  respects that bound. An empty batch is harmless.
- `delete` ignores missing IDs. `drop_collection` ignores missing collections and removes both
  records and catalog metadata. Dropping and recreating a name may change its shape.
- `query` returns only IDs and scores, at most `k`, sorted by descending score. Validate the
  query dimension. A nonpositive `k` returns an empty list. Apply filters before or during search.
- `healthcheck` checks that the configured database is reachable and usable. `close` releases
  client resources. Methods may be called from multiple threads: use a thread-safe client or locks.

Raise `StoreError` for store failures and `CollectionNotFound` when a required collection is
missing. Do not catch and rewrap `CollectionMismatch` or `DimensionLimitExceeded` as a generic
client error. `check_spec` raises `DimensionLimitExceeded` (a `StoreError`) above `max_dim`;
the indexing executor turns it into the permanent `store_dimension_limit` activity error. An
adapter that discovers a limit from the server should raise that same exception with a clear
message. Query operations must not retry invisibly.

## Scores

Higher is always better. Return the mathematical score, not a database-specific relevance score.

| Metric | Required score | Common database conversion |
|---|---|---|
| `cosine` | cosine similarity | cosine distance `d = 1 − cos` becomes `1 − d` |
| `dot` | unnormalised dot product | negative inner product `d` becomes `−d`; `1 − dot` becomes `1 − d` |
| `l2` | negative Euclidean distance | distance `d` becomes `−d`; **squared** distance becomes `−sqrt(d)` |

For zero vectors, the reference cosine similarity is zero. Do not advertise `dot` if the database
requires unit-length vectors and cannot return the raw dot product for arbitrary inputs. Check
vendor score definitions against a running client: Euclidean and squared Euclidean are easy to
confuse. Scores are comparable within one collection; the core uses ranks for default fusion.
The sqlite-vec adapter (0.1.9, the newest stable release) offers only cosine and l2: the library
has no dot-product distance function, and its `vec_length` returns the element count rather than
the norm, so the dot product cannot be derived from the available distances.

## Filters and metadata

`Record.metadata` contains `document_id: str` plus every field in the corpus metadata schema.
Types are `str`, `int`, `float`, and `bool`, with defaults already filled in. Chunk text and
arbitrary document attributes are not sent to the store. Adding a schema field schedules all
records to be rewritten, so adapters need not backfill old vectors themselves.

`query` receives a parsed `Filter` tree, not raw user JSON. Translate `Cond`, `And`, and `Or`
from `ragcentre.models.filters` to the database's filter language. The JSON language accepts:

```json
{"$and": [{"lang": {"$in": ["en", "fr"]}}, {"year": {"$gte": 2020}}]}
```

Support `$eq`, `$ne`, `$gt`, `$gte`, `$lt`, `$lte`, `$in`, `$nin`, `$and`, and `$or`.
Ranges apply only to numeric fields. Integers are valid values for float fields. The parser
normalises shorthand equality and implicit conjunctions. `matches(filter, metadata)` is the
reference evaluator; `to_json(filter)` serialises the tree when the target accepts that language.

Push filters into nearest-neighbour search. Fetching an unfiltered top-k and discarding
nonmatches can miss all relevant records. The selective-filter contract test catches that mistake.
For SQL, bind values as parameters, validate identifiers and quote them with the database's
identifier tools. Existing SQL compilers are in `ragcentre.stores.filter_sql`.

If the database cannot guarantee filtered top-k even with its best supported filtering mode,
set instance `filter_exact=False` and document the limitation. This permits recall testing; it
never permits returning a record that fails the filter or moving filtering into the core.

## Capabilities and limits

The original fields remain; new fields have compatible defaults:

```python
self.capabilities = Capabilities(
    metrics=frozenset({"cosine", "l2", "dot"}),
    max_batch=256,
    multi_vector=False,     # reserved
    native_hybrid=False,    # reserved
    max_dim=None,          # no known dimension limit
    consistency="immediate",  # or "eventual"
    filter_exact=True,
    max_collections=None,  # no known collection-count limit
)
```

Set instance capabilities from configuration or server discovery. A cloud deployment may have
smaller batches than the local deployment of the same adapter. `VectorStoreType.capabilities`
is the type's best case for discovery; runtime code uses the instance. `max_dim` is enforced by
`check_spec`; `max_collections` is informational and any collection quota enforcement belongs to
the adapter. Neither reserved capability changes the eight-method interface.

Eventual consistency means a successful write may not yet be visible to reads. The contract's
`settle` hook handles this in tests; it does not make production ingestion synchronous or add a
ninth store method. Document what users can expect immediately after indexing or deletion.

## Naming and catalogs

The core passes logical names such as `rc_01234567_0123456789abcdef`. Validate with
`check_collection_name`: `^[a-z][a-z0-9_]{2,62}$`. An adapter may map that name to a vendor name
or a namespace, and may prepend a configured prefix. Validate the resulting physical name too.
Keep the mapping deterministic and avoid collisions. Every operation must use the same mapping.
The core should not learn the vendor's naming rules.

A collection must retain its dimension, metric, field types, and index-shaping options. Existing
adapters keep their current metadata storage; these helpers are for adapters that need one:

```python
from ragcentre.stores.catalog import InStoreCatalog, LocalCatalog

catalog = InStoreCatalog(read_metadata, write_metadata, remove_metadata)
entry = catalog.ensure(spec, options={"quantization": "none", "hnsw_m": 16})
entry = catalog.get(spec.name)  # CatalogEntry or None
catalog.drop(spec.name)        # idempotent
```

`read_metadata(name) -> str | None` and `write_metadata(name, text) -> None` store a JSON entry
in the database's `_meta`, description, side collection, or key. `remove_metadata(name) -> None`
is optional when dropping the physical collection already removes its metadata. `CatalogEntry`
has `dim`, `metric`, `fields`, `options`, and `to_json` / `from_json`. `ensure` compares shape
and options, merges additional fields, and never removes or retypes an existing field. Pass
resolved effective options (including defaults), and never credentials. Options are normalised
through JSON, so tuples and lists compare consistently after storage.

`InStoreCatalog` serialises calls through one object. For multiple objects or processes, supply
`transaction=...`, a context-manager factory that locks the whole read/check/write sequence.
For example, it can acquire a database advisory lock and start a transaction used by both
callbacks. A shared `threading.RLock` factory covers separate objects in one process. Plain
read/write callbacks and a read-back check alone cannot prevent lost updates between processes.
Callbacks must provide read-after-write visibility for catalog metadata, even if vector search
itself is eventually consistent.

For Pinecone, which has no collection metadata slot, use:

```python
catalog = LocalCatalog(data_dir, "pinecone", instance_key="nonsecret-host/index-name")
# same get / ensure / drop API
catalog.close()
```

This uses `<data_dir>/store_catalogs/pinecone.db`, keyed by `(instance key, collection)`. Choose
a stable nonsecret key identifying the physical target, including tenant/database/index where
needed. Two providers using the same target must use the same key; credential rotation must not
change it. `BEGIN IMMEDIATE` transactions serialize concurrent ensures across SQLite handles
and processes. No SQLite transaction should span a remote database call.

Keep this file with the application's data and backups. Losing it orphans existing collections;
rebuild them, as after losing the core database. The catalog does not discover or adopt unknown
remote collections. Prefer in-store metadata for databases that can keep it. The catalog only
records metadata: the adapter still must coordinate creation of the physical collection and
its metadata, validate existing remote dimensions, create field indexes, and recover from a
partial creation failure before reporting success.

## Immutable index options

List every index-shaping configuration field in `VectorStoreType.immutable_fields`, for example
`("quantization", "hnsw_m")`. They must be fields of the configuration model. Provider updates
that change one return HTTP 409 `immutable_field`; sending its existing value is allowed.
Connection URL, timeout, and credentials remain mutable unless the adapter explicitly lists them.

The provider wrapper uses `ProviderTypeSpec.fingerprint_fields` as its permanent-field list.
For stores, `fingerprint_for` still uses only the provider's ID. A new store provider therefore
creates a new index, and embedding-cache reuse avoids re-embedding unchanged content.

Provider immutability alone is insufficient: a different provider or direct adapter caller can
open an existing collection. Record effective options in its catalog and compare them in every
`ensure_collection`. A changed option must raise `CollectionMismatch` before modifying the index.

## Running the contract suite

Install pytest in your adapter's development environment, then subclass the shipped suite:

```python
import pytest
from ragcentre.testing.vector_store_contract import VectorStoreContract
from my_adapter import MyStore

class TestMyStore(VectorStoreContract):
    @pytest.fixture
    def store(self, tmp_path):
        store = MyStore(path=tmp_path / "store")
        yield store
        store.close()
```

Use a fresh store for every test. Server fixtures must isolate their test collections and drop
them at teardown. The suite uses 200 seeded records with dimension 8 (some lifecycle tests use
another dimension), validates every advertised metric and every filter operator, and tests
concurrent reads and writes. To run the worked example from this checkout:

```sh
uv run pytest -q tests/test_v1_adapter_example.py
```

Override `settle(self, store, name)` in the test class for an eventual store. It runs after writes
and before assertions that require visibility; the default is a no-op. It is also called for
dropped/missing collections and from concurrent worker threads. A refresh API is a good choice:

```python
class TestRefreshingStore(TestMyStore):
    def settle(self, store, name):
        store.refresh_if_present(name)  # adapter-specific test support, not part of VectorStore
```

For asynchronous services, use the supplied `wait_until(predicate, timeout=30, interval=0.05)`
with a meaningful visibility condition. Counts alone cannot prove an overwrite is visible:
the count stays the same, so also verify an operation token, version, or changed content.
Give waits a deadline so a broken adapter fails the test instead of hanging.

`score_tolerance = 1e-4` is the default absolute tolerance. Override it only for a measured
precision loss and explain why alongside the override. Scores, ordering, result count, and the
brute-force top-k score sequence are exact by default (within that tolerance).

For `filter_exact=False`, filtered queries use `filter_min_recall = 0.9` or a stricter value,
measured against brute-force top-k IDs. Every result must still pass the filter and have a correct
score and order. Unfiltered queries retain the default exact check. The pytest warning summary
states the relaxed mode, and the `filter_mode` user property records it in JUnit output. A store
with `max_dim=None` skips the dimension-limit test; a declared limit must reject `max_dim + 1`
with `StoreError` before creating a collection.

Also run `CollectionOptionsContract` for adapters with immutable options:

```python
from ragcentre.testing.vector_store_contract import CollectionOptionsContract

class TestMyOptions(CollectionOptionsContract):
    options_a = {"quantization": "none"}
    options_b = {"quantization": "scalar"}

    def open_store(self, options, tmp_path):
        return MyStore(path=tmp_path / "shared-store", **options)
```

Both configurations must address the same physical storage. The mixin ensures with A, then
requires B to raise `CollectionMismatch`, and checks reopening with A. Use isolated disposable
storage and clean it up in a fixture for remote tests.

## Package registration and optional clients

A factory receives `(config: BaseModel, secrets: dict[str, str], data_dir: Path)` and returns a
store. Use a Pydantic configuration model with `extra="forbid"`, named secret keys, and a
`VectorStoreType` object. The example file includes both a factory and `STORE_TYPE`.

In your adapter package's `pyproject.toml`:

```toml
[project.entry-points."ragcentre.vector_stores"]
example_memory = "my_adapter:STORE_TYPE"
```

Use a distinct type name; built-in types take precedence on collisions. Entry points load the
object, not a function returning it. Listing providers must work without importing heavy clients.
Import optional vendor libraries in the factory/constructor via `require_extra(...)`; a missing
extra then gives a clear `StoreUnavailable`. RAGCentre never installs packages itself. Publish
your tested client requirements with your package, and verify signatures and score transforms
against the installed client and a real server or emulator before claiming compatibility.

## Stores shipped

The built-in stores, with their extras, metrics, guarantees and a config example, are in the
README's [Vector stores](../../README.md#vector-stores) table. It is generated from the registry
(`ragcentre store list --markdown`), so your `VectorStoreType` capabilities show up there the same
way; a built-in store also needs an entry in `ragcentre/stores/docs.py` and an example provider
file in `examples/providers/`. `ragcentre store check` verifies a live store against what it claims.
