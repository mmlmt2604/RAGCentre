# Milvus, Weaviate and Pinecone integrations

RAGCentre now supports these three databases through its existing vector-store provider interface.
Chunk text, document metadata and the record ledger remain in RAGCentre's core database. The
adapters send supplied dense vectors and flat filter metadata to the selected service; they do
not ask a database to generate embeddings.

## Selection and feasibility

The [DB-Engines vector DBMS ranking](https://db-engines.com/en/ranking/vector+dbms), checked on
20 November 2026, lists Pinecone, Qdrant, Milvus and Weaviate as the highest-ranked dedicated
vector databases. Qdrant is already bundled. Pinecone, Milvus and Weaviate are therefore the
three highest-ranked remaining **dedicated** vector databases in that source. This scope matters:
Elasticsearch, OpenSearch and Couchbase rank above them when general-purpose databases with
vector search are included. Popularity here is DB-Engines' composite indicator, not measured
market share or a performance ranking. Milvus and Weaviate also have active open-source server
projects ([Milvus](https://github.com/milvus-io/milvus),
[Weaviate](https://github.com/weaviate/weaviate)).

All three fit the existing eight-method adapter contract without a pipeline or retrieval API
change. They support server-side metadata filtering and all three required distance metrics.
Their operational costs differ:

| Store | Integration effort and deployment | Important behavior |
|---|---|---|
| Milvus | Low to moderate: Python client, server/Zilliz Cloud, or local Milvus Lite | JSON metadata filters; strong reads; FLAT exact search or approximate graph/automatic indexing |
| Weaviate | Moderate: Python client, HTTP **and gRPC** connectivity, native typed properties | FIELD tokenization preserves whole-string equality; immutable shape in collection description; field additions in native schema |
| Pinecone | Moderate: API key, serverless index creation, durable local catalog | One index per RAGCentre collection; Cloud reads are eventual; project quotas and billing affect feasibility for many pipelines |

Pinecone needs the most operational planning: each pipeline/index rebuild can create a new
physical index, so check project index quotas before choosing it for a large installation. This
adapter intentionally owns its indexes so collections can use independent dimensions and metrics.
It does not attach collections to arbitrary existing indexes or delete indexes lacking a matching
local catalog entry. Its SDK is constrained to `>=7.3,<8`: SDK 10 uses a new schema-based
control-plane API that the tested Pinecone Local emulator rejects. Cloud support uses SDK 7's
classic dense-index API; live Cloud credentials and provisioning were not tested here.

## Install and configure

In a source checkout, install the extra for the provider you want:

```sh
uv sync --extra milvus          # Milvus server or Zilliz Cloud
uv sync --extra milvus-lite     # local embedded Milvus Lite
uv sync --extra weaviate
uv sync --extra pinecone
```

Installed distributions expose the equivalent `ragcentre[milvus]`, `ragcentre[milvus-lite]`,
`ragcentre[weaviate]` and `ragcentre[pinecone]` extras. Clients are imported lazily: listing
provider types works without these packages, and opening an unavailable store reports the
missing extra. The schema-driven provider UI discovers the adapters automatically.

Ready-to-import examples:

- [Milvus server](../examples/providers/milvus-vectors.yaml), with a token read from `MILVUS_TOKEN`.
- [Milvus Lite](../examples/providers/milvus-lite-vectors.yaml), with a `.db` path under the app data directory.
- [Weaviate](../examples/providers/weaviate-vectors.yaml), using the test service's HTTP and gRPC ports.
- [Pinecone Cloud](../examples/providers/pinecone-vectors.yaml), with a key read from `PINECONE_API_KEY`.
- [Pinecone Local](../examples/providers/pinecone-local-vectors.yaml), for development with a disposable service.

Import an example with `ragcentre provider add -f PATH`, then choose that vector-store provider
in a pipeline. Set the environment variables before the application opens a provider. An
anonymous local Milvus or Weaviate server needs no secret; remove the `secrets` block from the
example in that case. Pinecone Local accepts any nonempty API key. For Weaviate Cloud, configure
an HTTPS URL, `grpc_port: 443` and `grpc_secure: true`; a separate `grpc_host` can be supplied.

Milvus `index_type`, Weaviate `index_type`, and Pinecone `cloud`/`region` are immutable provider
options. Create a new provider to change them. Collection creation also checks saved options,
dimensions, metrics and field types when a different client reopens an existing collection.

Milvus and Pinecone retain catalogs in `<data_dir>/store_catalogs/milvus.db` and `pinecone.db`.
Back up these files with the app database; losing the Pinecone catalog requires restoring it or
rebuilding the remote indexes. Use distinct Pinecone `index_prefix` values for separate projects
or applications sharing a data directory: API keys do not form part of the catalog key.
Weaviate keeps the shape in the collection description and derives field types from native
properties, so concurrent additive schema updates do not overwrite a shared field list.

## Search and limits

All three adapters implement upserts with complete metadata replacement, idempotent deletion,
batch bounds, dimension validation and nested AND/OR filters. Every filter is applied by the
backend during search. `$in`/`$nin` expand to equality conditions where a backend lacks a native
form (Milvus JSON paths and Pinecone boolean lists).

Scores follow RAGCentre's convention: cosine similarity, raw dot product, or negative Euclidean
distance. Milvus and Pinecone squared L2 distances become `-sqrt(distance)`; Weaviate cosine
distance becomes `1-distance`, squared L2 becomes `-sqrt(distance)`, and negative dot distance
becomes `-distance`.

Milvus FLAT and Weaviate flat advertise exact filtered search. Their approximate index choices,
and Pinecone, advertise `filter_exact=False`; the contract suite checks filtered recall at least
0.9 while still checking each returned record's filter and score. Pinecone Cloud also advertises
eventual consistency: indexing completion does not guarantee immediate search visibility.
Pinecone Local tests do not establish Cloud consistency or production recall.

Milvus limits query results to 16,384 and vectors to 32,768 dimensions. Pinecone limits queries
to 10,000 results and dense vectors to 20,000 dimensions; its service also imposes request-size
and metadata-size limits. Weaviate uses the configured server's limits. Provider batch limits
are upper bounds; the service may reject unusually large metadata or payloads.

## Validation

Start the local services and run all new adapter tests:

```sh
docker compose -f tests/docker-compose.yml up -d --wait milvus weaviate pinecone
uv sync --all-extras
uv run pytest -q tests/test_v15_milvus.py tests/test_v15_weaviate.py tests/test_v15_pinecone.py
```

The shared contract covers all advertised metrics, every filter operator, nested filters,
selective filters, lifecycle operations, dimension mismatches and concurrent writes/queries.
Milvus and Weaviate also run it with both flat and HNSW indexes. Separate tests cover saved
index options, reconnecting, and replacing metadata. Tests isolate physical collections/indexes
and clean up their own data. Missing services skip integration tests locally; CI requires them.
Milvus Lite runs the contract without Docker. The integration services are development fixtures,
not a production deployment template. Cloud authentication and provider-specific Cloud quotas
need validation in the intended account.

## Vendor references

- [Milvus insert/upsert/delete](https://milvus.io/docs/insert-update-delete.md),
  [scalar filtering](https://milvus.io/docs/boolean.md),
  [Milvus Lite](https://milvus.io/docs/milvus_lite.md).
- [Weaviate distance metrics](https://docs.weaviate.io/weaviate/config-refs/distances),
  [property tokenization](https://docs.weaviate.io/weaviate/config-refs/schema),
  [object updates](https://docs.weaviate.io/weaviate/manage-objects/update).
- [Pinecone indexing overview](https://docs.pinecone.io/guides/index-data/indexing-overview),
  [metadata filtering](https://docs.pinecone.io/guides/search/filter-by-metadata),
  [local development](https://docs.pinecone.io/guides/operations/local-development),
  [service limits](https://docs.pinecone.io/reference/api/database-limits).
