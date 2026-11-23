# Milvus, Weaviate and Pinecone integrations

These databases use RAGCentre's vector-store provider interface. RAGCentre keeps chunk text,
document metadata and its record ledger in the core database, and sends supplied dense vectors
and filter metadata to the selected service. The adapters do not generate embeddings.

## Install and configure

```sh
uv sync --extra milvus       # includes Milvus Lite, standalone and Zilliz Cloud support
uv sync --extra weaviate
uv sync --extra pinecone
```

Installed distributions expose the equivalent `ragcentre[milvus]`, `ragcentre[weaviate]` and
`ragcentre[pinecone]` extras. Clients are imported lazily: listing provider types works without
these packages, and opening an unavailable store reports the missing extra.

Ready-to-import examples:

- [Milvus server](../examples/providers/milvus-vectors.yaml), with an optional `RAGCENTRE_PROVIDER_MILVUS_TOKEN` environment reference.
- [Milvus Lite](../examples/providers/milvus-lite-vectors.yaml), with a `.db` path under the data directory.
- [Weaviate](../examples/providers/weaviate-vectors.yaml), using the test service's HTTP and gRPC ports.
- [Pinecone Cloud](../examples/providers/pinecone-vectors.yaml), using an existing dense index and `PINECONE_API_KEY`.
- [Pinecone Local](../examples/providers/pinecone-local-vectors.yaml), using an existing emulator index.

Import an example with `ragcentre provider add -f PATH`, then select that vector-store provider
in a pipeline. Set the referenced environment variables before opening a provider. Anonymous
local Milvus and Weaviate servers need no secret; remove the example's `secrets` block in that
case. Pinecone Local accepts any nonempty API key.

## Deployment and collection ownership

| Store | Deployment | Collection mapping and metadata |
|---|---|---|
| Milvus | Local Milvus Lite file, standalone server or Zilliz Cloud | One physical collection per logical collection, with a `prefix`; a separate Milvus catalog collection stores field types and index options |
| Weaviate | HTTP and gRPC endpoints, including Weaviate Cloud | One physical collection per logical collection, with a `prefix`; collection descriptions hold catalog entries |
| Pinecone | Existing dense index in Cloud or Pinecone Local | One namespace per logical collection, with a `prefix`; a local catalog records field types and collection shape |

For Milvus, configure `uri`, `prefix` and `index_type` (`FLAT` or `HNSW`). Index type and build
options are immutable. Milvus coordinates schema and catalog changes across cooperating worker
processes on one host. Separate hosts must use separate prefixes or serialize schema changes
externally. Queries use strong consistency.

Weaviate needs both HTTP and gRPC connectivity. Configure either `url` or `host`/`port`/`secure`,
plus `grpc_port`. For Weaviate Cloud use the HTTPS endpoint and `grpc_port: 443`. Optional HNSW
settings control index construction and search. Text properties use FIELD tokenization and
encode string boundaries to preserve equality, including leading and trailing whitespace.

Create the Pinecone index before registering its provider, with the dimension and metric of
your embedder, then configure `index_name`. RAGCentre creates and clears namespaces inside that
index; it never creates or deletes the index itself. Different dimensions or metrics require
different indexes/providers. `controller_host` selects a Pinecone Local endpoint, and
`index_host` can override the data endpoint. The supported client is `pinecone>=10.3,<11`;
the adapter handles the emulator's older index-description format.

Back up `<data_dir>/store_catalogs/pinecone.db` with the core database. Catalog identity uses the
resolved physical index host and namespace prefix, so separate projects remain isolated and API
key rotation preserves the catalog. Keep namespace prefixes distinct for independent installations.

## Search and limits

All three adapters implement metadata replacement on upsert, idempotent deletion, dimension
validation and nested AND/OR filters. Filtering happens in the database. Boolean `$in`/`$nin`
operands are expanded when a backend does not accept boolean lists.

Scores follow RAGCentre's convention: cosine similarity, raw dot product or negative Euclidean
distance. Milvus and Pinecone squared L2 distances become `-sqrt(distance)`; Weaviate cosine
distance becomes `1-distance`, squared L2 becomes `-sqrt(distance)`, and negative dot distance
becomes `-distance`.

Milvus FLAT and the tested Weaviate configurations support exact filtered results. Milvus HNSW
advertises approximate filtered search. Pinecone advertises approximate filtered search and
eventual consistency: a completed write may not be searchable immediately. Emulator results do
not establish Cloud recall or consistency. Use `ragcentre store check` to test a live provider's
guarantees, and consult the generated [store table](../README.md#vector-stores) for limits.

Milvus supports up to 32,768 dimensions and 16,384 query results. Weaviate supports up to 65,535
dimensions and its configured query limit (10,000 by default). Pinecone supports up to 20,000
dimensions and 10,000 query results without returned metadata. Pinecone batches are bounded by
both record count and encoded size, and metadata integers must stay within ±2⁵³.

## Validation

```sh
docker compose -f tests/docker-compose.yml up -d --wait milvus weaviate pinecone
uv sync --python 3.13 --all-extras
RAGCENTRE_TEST_PINECONE_URL=http://localhost:55080 uv run pytest \
  tests/test_vs_milvus.py tests/test_vs_milvus_filter.py \
  tests/test_vs_weaviate.py tests/test_vs_pinecone.py
```

The suites cover advertised metrics, filter operators, nested and selective filters, lifecycle
operations, catalog isolation, reconnecting, metadata replacement and concurrent operations.
Milvus tests run on both Lite and standalone. Missing local services skip integration tests;
Cloud tests require explicit credentials. Live Cloud authentication, quotas and production
behavior need validation in the intended account.

The Compose services are development fixtures. Tests isolate their own collection names and
clean up the data they create.

## Vendor references

- [Milvus insert/upsert/delete](https://milvus.io/docs/insert-update-delete.md),
  [filtering](https://milvus.io/docs/boolean.md) and [Milvus Lite](https://milvus.io/docs/milvus_lite.md).
- [Weaviate distance metrics](https://docs.weaviate.io/weaviate/config-refs/distances),
  [schema and tokenization](https://docs.weaviate.io/weaviate/config-refs/schema).
- [Pinecone indexing](https://docs.pinecone.io/guides/index-data/indexing-overview),
  [metadata filtering](https://docs.pinecone.io/guides/search/filter-by-metadata) and
  [service limits](https://docs.pinecone.io/reference/api/database-limits).
