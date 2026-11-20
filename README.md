# RAGCentre

A small, self-hosted retrieval service for building, comparing and evaluating RAG pipelines.

Register your own parser, embedder, vector store and reranker; wire them into pipelines over a
corpus of documents; query them; and compare pipelines with a built-in evaluation harness that
reports which differences are statistically significant. Work is keyed by what it computes, not
by pipeline, so two pipelines that parse the same way share every parse, and changing only the
chunker re-chunks without re-parsing.

Everything runs locally on SQLite: one process, a data directory, and no other services unless
you choose to plug them in.

## Try it

[`examples/quickstart.md`](examples/quickstart.md) builds two pipelines over a small document set,
queries them and evaluates one chunker against another. It needs no API keys, model downloads or
external services and takes a couple of minutes.

```sh
uv sync
uv run ragcentre serve          # API on http://127.0.0.1:8700, data in ./data
```

Or with Docker (builds the web UI; data lives in the `ragcentre-data` volume):

```sh
docker compose up --build       # http://localhost:8700
```

Set `RAGCENTRE_API_TOKEN` before exposing the port beyond localhost.

The example parser services are Compose profiles, so one command also gives you a parser for PDFs,
Office documents and images:

```sh
docker compose --profile docling up --build     # RAGCentre, and Docling on http://docling:8701
```

The other profiles are `mineru`, `paddleocr`, `surya`, `easyocr` and `doctr`. Register the parser with the `base_url`
shown (see [`examples/providers/`](examples/providers/)); each service's README has the details.

## Concepts

- **Provider**: a configured parser, chunker, embedder, reranker or vector store. Built in:
  `builtin_text` (Markdown and plain text), `builtin_hash` (a lexical embedder for demos and
  tests) and `sqlite_vec`. Over HTTP: any parser or chunker speaking RAGCentre's protocols, any
  OpenAI-compatible embeddings server, and Cohere/Jina-style rerankers. Vector stores: see
  [Vector stores](#vector-stores).
- **Corpus**: a set of documents with a typed metadata schema for filtering.
- **Pipeline**: parse → chunk → embed → index (vector and/or full-text), plus retrieval settings
  (fusion, reranking, neighbour expansion). See [`examples/pipelines/`](examples/pipelines/).
- **Evaluation**: a JSON Lines dataset of questions whose answers are named by document, page and
  text, so the same ground truth works for any chunking. Runs compute hit rate, recall,
  precision, nDCG and MRR at several k, with paired-bootstrap confidence intervals between
  pipelines.

## Command line

`ragcentre serve` and `ragcentre worker` run the server and workers. Every other command is a
client of the HTTP API (`RAGCENTRE_URL`, default `http://127.0.0.1:8700`; `RAGCENTRE_API_TOKEN`
when the server requires a token). Names or ids are accepted wherever a provider, corpus,
pipeline or dataset is expected, and `ragcentre --json …` prints the API's JSON.

```
ragcentre serve [--host] [--port] [--workers N]
ragcentre worker [--workers N]
ragcentre provider add -f provider.yaml | list | test NAME | set-secret NAME KEY (--env VAR | --value-stdin)
ragcentre corpus create NAME [--schema schema.yaml] | list | activate CORPUS PIPELINE [--force]
ragcentre docs add CORPUS PATH... [--metadata k=v] [--external-id ID] | list CORPUS | rm ID
ragcentre pipeline create CORPUS -f pipeline.yaml [--dry-run] | list CORPUS | status ID [--watch] | diff A B | archive ID
ragcentre query CORPUS "question" [--pipeline ID] [--top-k N] [--filter JSON]
ragcentre eval import CORPUS NAME file.jsonl | run DATASET PIPELINE... [--wait] | show RUN
```

Run `ragcentre COMMAND --help` for details.

## Settings

| Variable | Default | |
|---|---|---|
| `RAGCENTRE_DATA_DIR` | `./data` | Database, blobs, embedding cache and the default vector store |
| `RAGCENTRE_HOST` / `RAGCENTRE_PORT` | `127.0.0.1` / `8700` | Where `serve` listens |
| `RAGCENTRE_WORKERS` | `4` | Ingestion worker threads |
| `RAGCENTRE_API_TOKEN` | unset | When set, every request needs `Authorization: Bearer <token>` |
| `RAGCENTRE_SECRET_KEY` | unset | Fernet key for storing provider secrets; without it only environment-variable references are allowed |
| `RAGCENTRE_MAX_UPLOAD_MB` | `200` | Upload size limit |
| `RAGCENTRE_MAX_ATTEMPTS` | `5` | Attempts before a retryable failure becomes permanent |
| `RAGCENTRE_LOG_LEVEL` | `INFO` | JSON logs on standard error |

## Writing your own services

- [`docs/protocols/parser-v1.md`](docs/protocols/parser-v1.md): parse files into the Document IR.
  [`examples/docling_shim/`](examples/docling_shim/) is a complete parser service built on Docling;
  [`mineru_shim/`](examples/mineru_shim/), [`paddleocr_shim/`](examples/paddleocr_shim/) and
  [`surya_shim/`](examples/surya_shim/) do the same for MinerU, PaddleOCR and Surya, on a small
  shared package, [`shimkit/`](examples/shimkit/). Each can run its engine in-process or call a server
  you already run (docling-serve, a MinerU API server, PaddleX serving, a Surya inference server), takes
  typed options, and parses long documents as background jobs. For plain OCR of scans and photos,
  [`easyocr_shim/`](examples/easyocr_shim/) and [`doctr_shim/`](examples/doctr_shim/) wrap EasyOCR and
  docTR, which read text lines only; the shims group the lines into paragraphs and can guess headings.
  [`docs/protocols/parser-shim-guide.md`](docs/protocols/parser-shim-guide.md) shows how to plug in
  another engine, and `ragcentre parser check <url>` tells you whether a service is usable and which
  RAGCentre features its output feeds.
- [`docs/protocols/chunker-v1.md`](docs/protocols/chunker-v1.md): custom chunking.
- [`docs/protocols/embedder-v1.md`](docs/protocols/embedder-v1.md): OpenAI-compatible embeddings.
- [`docs/protocols/reranker-v1.md`](docs/protocols/reranker-v1.md): Cohere/Jina-style reranking.

### Vector stores

`sqlite_vec` works out of the box. Every other store needs its optional extra
(`pip install "ragcentre[qdrant]"`, and so on); RAGCentre does not install them, and using a store
whose extra is missing fails with an error naming it. Secrets (API keys, connection strings) are
never written in a provider file: reference an environment variable (`secrets: {api_key: {env: NAME}}`)
or set a stored value with `ragcentre provider set-secret`. Each store has an example provider file
under [`examples/providers/`](examples/providers/), registered with `ragcentre provider add -f FILE`.

<!-- vector-stores:start -->
| Type | Install | Metrics | Exact filters | Consistency | Max dim | Max batch | Config example | Details |
|---|---|---|---|---|---|---|---|---|
| `sqlite_vec` | built in | cosine, l2 | yes | immediate | none known | 1,000 | `{"prefix": "staging_"}` | [example](examples/providers/sqlite-vec-prefixed.yaml) |
| `qdrant` | `ragcentre[qdrant]` | cosine, dot, l2 | yes | immediate | none known | 256 | `{"url": "https://HOST:6333", "quantization": "scalar"}` | [example](examples/providers/qdrant-cloud.yaml) |
| `pgvector` | `ragcentre[pgvector]` | cosine, dot, l2 | yes | immediate | 16,000 | 500 | `{"vector_type": "halfvec", "hnsw_m": 16}` | [example](examples/providers/pgvector-halfvec.yaml) |
| `chroma` | `ragcentre[chroma]` | cosine, dot, l2 | yes | immediate | none known | 1,000 | `{"mode": "http", "host": "localhost", "port": 8000}` | [example](examples/providers/chroma-http.yaml) |
| `lancedb` | `ragcentre[lancedb]` | cosine, dot, l2 | yes | immediate | none known | 1,000 | `{"uri": "lancedb", "index_type": "IVF_FLAT"}` | [example](examples/providers/lancedb-local.yaml) |
| `redis` | `ragcentre[redis]` | cosine, dot, l2 | yes | immediate | none known | 256 | `{"url": "redis://localhost:6379/0", "algorithm": "hnsw"}` | [example](examples/providers/redis-hnsw.yaml) |
| `weaviate` | `ragcentre[weaviate]` | cosine, dot, l2 | yes | immediate | 65,535 | 256 | `{"url": "http://localhost:8080"}` | [example](examples/providers/weaviate.yaml) |
| `elasticsearch` | `ragcentre[elasticsearch]` | cosine, dot, l2 | yes | immediate | 4,096 | 500 | `{"url": "https://localhost:9200", "index_options": "hnsw"}` | [example](examples/providers/elasticsearch.yaml) |
| `opensearch` | `ragcentre[opensearch]` | cosine, dot, l2 | yes | immediate | 16,000 | 500 | `{"url": "https://localhost:9200", "engine": "faiss"}` | [example](examples/providers/opensearch.yaml) |
| `milvus` | `ragcentre[milvus]` | cosine, dot, l2 | yes | immediate | 32,768 | 256 | `{"uri": "milvus.db", "index_type": "FLAT"}` | [example](examples/providers/milvus-lite.yaml) |
| `pinecone` | `ragcentre[pinecone]` | cosine, dot, l2 | **no** | **eventual** | 20,000 | 1,000 | `{"index_name": "ragcentre"}` | [example](examples/providers/pinecone.yaml) |
| `mongodb` | `ragcentre[mongodb]` | cosine, l2 | **no** | **eventual** | 8,192 | 500 | `{"database": "ragcentre", "exact": false}` | [example](examples/providers/mongodb-atlas.yaml) |
| `http` | built in | cosine, dot, l2 | yes | immediate | none known | 1,000 | `{"url": "http://127.0.0.1:8702"}` | [example](examples/providers/http-vector-store.yaml), [protocol](docs/protocols/vector-store-v1.md) |

The guarantees are each type's best case. An instance can report less: `http` reads them from the service's `/info`, Pinecone's depend on the index, Chroma Cloud takes 300 records per write, LanceDB Cloud (`db://`) is eventually consistent without exact filters, and MongoDB with `exact: true` filters exactly. `ragcentre store check` shows what a live store gives. Filters are exact when a filtered search returns `k` matches whenever `k` exist; “eventual” consistency means a write may not be searchable at once.
<!-- vector-stores:end -->

This table is generated from the store registry (`ragcentre store list --markdown`), and a test
fails when it drifts. `ragcentre store list` shows the same facts in the terminal, and
`ragcentre store check` writes to a temporary collection on a live store and reports which
guarantees it really gives. The web UI shows a type's guarantees and warnings when you create a
provider. HTTP stores capture their service name, version and actual capabilities from `/info`;
the provider detail page uses those reported capabilities, and a connection test refreshes them.

pgvector retries a short filtered HNSW result with an exact scan over matching rows, preserving
filtered result counts even when scan limits are reached. Quantized Elasticsearch indexes require
8.18 or later for full-precision score rescoring. MongoDB filter metadata and integer operands must
stay within ±2⁵³, where its search index preserves numeric comparisons. Milvus coordinates schema
changes across worker processes on one host; separate hosts must use separate prefixes or serialize
schema changes externally. Back up Pinecone's local collection catalog with the RAGCentre data directory.
The [Milvus, Weaviate and Pinecone guide](docs/vector-database-integrations.md) covers deployment,
collection ownership, configuration examples and validation.

To add a store, follow the [adapter guide](docs/protocols/vector-store-adapter-guide.md): publish a
`VectorStoreType` through the `ragcentre.vector_stores` entry point and pass
`ragcentre.testing.vector_store_contract`. A service in any language can also speak the
[`ragcentre.vector_store/v1`](docs/protocols/vector-store-v1.md) HTTP protocol (see the
[shim guide](docs/protocols/vector-store-shim-guide.md) and the reference shim in
[`examples/vector_store_shims/memory/`](examples/vector_store_shims/memory/)) and register as an
`http` store.

## Web UI

`ragcentre serve` also serves the web UI at `/` when it has been built into
`src/ragcentre/ui_dist/` (released wheels include it; a source checkout needs one build). Without
a build, `/` explains how to make one. The UI asks for `RAGCENTRE_API_TOKEN` itself when the
server requires one.

```sh
cd ui
npm ci
npm run dev          # http://localhost:5173 against the built-in mock API (no backend needed)
npm run dev:live     # the same, proxying /v1 to a server on 127.0.0.1:8700
npm run build        # production build into ../src/ragcentre/ui_dist
npm run e2e          # Playwright against a real `ragcentre serve` (after `npm run build`)
```

`ui/src/api/types.ts` is the hand-written API contract. `npm run types:api` regenerates
`ui/src/api/generated/openapi.ts` from the backend, and `npm run typecheck` fails if the two
disagree (`ui/src/api/contract-check.ts`); CI runs both.

### Releasing

Build the UI first, then the wheel, so the wheel contains it:

```sh
(cd ui && npm ci && npm run build)
uv build
```

## Development

```sh
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check
uv run mypy
```

The pgvector tests are marked `integration` and need PostgreSQL with pgvector:

```sh
docker compose -f tests/docker-compose.yml up -d --wait
uv run pytest -m integration
docker compose -f tests/docker-compose.yml down
```

The parser shims describe how each engine maps to the Document IR in their own READMEs:
[`mineru_shim`](examples/mineru_shim/), [`paddleocr_shim`](examples/paddleocr_shim/),
[`surya_shim`](examples/surya_shim/), [`easyocr_shim`](examples/easyocr_shim/) and
[`doctr_shim`](examples/doctr_shim/). Working plans and design notes are kept locally, not in the
repository.
