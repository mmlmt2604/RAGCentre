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
docker compose run --build --rm -it ragcentre ragcentre account init   # create your account once
docker compose up --build                                              # http://localhost:8700
```

The `account init` step is required before the server starts on a non-loopback bind (the container
listens on `0.0.0.0`). Or set `RAGCENTRE_API_TOKEN` instead and skip the account. Or create the
account in the browser instead of the CLI:

```sh
RAGCENTRE_ALLOW_REMOTE_SETUP=1 docker compose up --build   # then http://localhost:8700 → "Create your account"
```

On first run, open the web UI and create your account (username + password). The browser stays signed
in with a cookie; **API keys** (minted under **Account**) are what the CLI, MCP and scripts use. For a
headless or Docker host, create the account from the server's shell with `ragcentre account init`.

For a non-loopback bind, `ragcentre serve` requires an account or `RAGCENTRE_API_TOKEN` unless you
explicitly pass `--allow-no-token`. First-run setup is only offered in the browser on a loopback bind;
set `RAGCENTRE_ALLOW_REMOTE_SETUP=1` to allow it on a remote host.

`RAGCENTRE_API_TOKEN` still works as a full-scope operator credential for the CLI and MCP. When a
token is configured, `/v1/docs` and `/v1/openapi.json` require an Authorization header. To read the
schema, run `curl -H "Authorization: Bearer $RAGCENTRE_API_TOKEN" http://127.0.0.1:8700/v1/openapi.json`.

The example parser services are Compose profiles, so one command also gives you a parser for PDFs,
Office documents and images:

```sh
docker compose --profile docling up --build     # RAGCentre, and Docling on http://docling:8701
```

The other profiles are `mineru`, `paddleocr`, `surya`, `easyocr` and `doctr`. Register the parser with the `base_url`
shown (see [`examples/providers/`](examples/providers/)); each service's README has the details.

## OCR detection

RAGCentre finds parser shims that are running and shows whether their OCR works, on **Providers**, in
the pipeline's parser picker and in the status bar. On a new server, guided setup shows the command
to start a shim and selects it when it answers.

- **Found, not tested:** a shim answers `GET /info`. Nothing has been sent to it. **Add and test OCR**
  registers it.
- **OCR ready:** the shim and its engine read a scanned test page (a page without a text layer) and
  returned its text. A running container or a healthy `/info` is never enough for this.
- Anything else names what failed (shim, engine, or text) and what to do.

It looks on the example shims' ports (8701-8706 and 8710): on loopback, or, when RAGCentre runs in
Docker, at the Compose service names and at `host.docker.internal`. It never scans the network. For a
shim on another computer, add a parser with its URL, or list it:

| Variable | Default | Meaning |
|---|---|---|
| `RAGCENTRE_DISCOVERY_URLS` | `auto` | `auto`, `off`, or comma-separated URLs. `auto,http://gpu-box:8701` keeps both |
| `RAGCENTRE_OCR_RECHECK_SECONDS` | `600` | How often a working engine is re-tested while the UI is open. `0` never |

Each test parses one page with the shim's default options, so it costs one page on a paid engine.
Added parsers are tested when first seen, when they come back or restart, when they are edited, and
on the recheck interval. Nothing runs while the UI is closed.

## Concepts

- **Provider**: a configured parser, chunker, embedder, reranker, language model or vector store. Built in:
  `builtin_text` (Markdown and plain text), `builtin_hash` (a lexical embedder for demos and
  tests) and `sqlite_vec`. Over HTTP: any parser or chunker speaking RAGCentre's protocols, any
  OpenAI-compatible embeddings or chat-completions server, and Cohere/Jina-style rerankers.
  Language model providers support configuration and connection tests. Chunk enrichment and
  answer generation are planned separately. [Language model protocol](docs/protocols/llm-v1.md)
  and [vector stores](#vector-stores).
- **Corpus**: a set of documents with a typed metadata schema for filtering.
- **Pipeline**: parse → chunk → embed → index (vector and/or full-text), plus retrieval settings
  (fusion, reranking, neighbour expansion). See [`examples/pipelines/`](examples/pipelines/).
- **Query transform**: an optional step in a pipeline's retrieval settings. A language model turns
  the query into the queries that are actually searched. See [Query transforms](#query-transforms).
- **Evaluation**: a JSON Lines dataset of questions whose answers are named by document, page and
  text, so the same ground truth works for any chunking. Runs compute hit rate, recall,
  precision, nDCG and MRR at several k, with paired-bootstrap confidence intervals between
  pipelines.

## Query transforms

A pipeline can ask a language model provider to reshape the query before searching. Add a
`retrieval.transform` block ([`multi-query.yaml`](examples/pipelines/multi-query.yaml),
[`hyde.yaml`](examples/pipelines/hyde.yaml)):

```yaml
retrieval:
  transform: {mode: multi_query, provider: ollama-chat, n: 3}
```

| `mode` | The model writes | What is searched |
|---|---|---|
| `rewrite` | one clearer query | the rewrite, and the original |
| `multi_query` | up to `n` alternative phrasings | the original and each phrasing |
| `hyde` | up to `n` short passages that would answer the query | vector sources search the passages; keyword sources search the original |
| `decompose` | up to `n` sub-questions | each sub-question, with an equal share of the results |

A transform is off unless you add it. It changes only the query path: indexes are shared with the
pipeline that has no transform, so adding one never re-indexes anything. Settings: `n` (1 to 8,
default 3), `include_original` (default true), `original_weight` and `k` (how the queries of one
source are merged by reciprocal rank fusion) and `instructions` (extra text for the prompt, such as
your documents' vocabulary).

- **Cost.** One language model call before the search of every query that has not been seen before.
  Answers are cached in the data directory, so a repeated query makes no call and evaluation runs
  are repeatable. After the model behind a provider changes, run `ragcentre cache clear --transforms`.
- **Failures are errors.** If the model fails or answers with something unusable, the query fails
  with a 502 naming the provider. It does not quietly search the raw query.
- **Results.** The response lists the generated queries under `transform`. Each result still has one
  rank per source. With `debug: true`, a result also lists which queries found it.
- **Skipping it.** `"transform": false` in a query request (`--no-transform` in the CLI, a toggle on
  the query page, an argument of the MCP `search` tool) searches the query as written. Agents that
  already rewrite their own queries can use it.
- **Measure first.** Create the pipeline next to one without a transform and run
  `ragcentre eval run DATASET PLAIN TRANSFORMED`. Transforms help most on short or differently
  worded queries, and can hurt on exact lookups. Reasoning models may spend their output limit
  thinking and return nothing, which fails the query; the editor warns when a provider allows fewer
  than 512 output tokens.

## Command line

`ragcentre serve` and `ragcentre worker` run the server and workers. Every other command is a
client of the HTTP API (`RAGCENTRE_URL`, default `http://127.0.0.1:8700`; `RAGCENTRE_API_TOKEN`
when the server requires a token). Names or ids are accepted wherever a provider, corpus,
pipeline or dataset is expected, and `ragcentre --json …` prints the API's JSON.

```
ragcentre serve [--host] [--port] [--workers N]
ragcentre worker [--workers N]
ragcentre provider add -f provider.yaml | list | test NAME | edit NAME -f settings.yaml | delete NAME
ragcentre provider set-secret NAME KEY (--env VAR | --value-stdin)
ragcentre corpus create NAME [--schema schema.yaml] | list | activate CORPUS PIPELINE [--force]
ragcentre docs add CORPUS PATH... [--metadata k=v] [--external-id ID] | list CORPUS | rm ID
ragcentre pipeline create CORPUS -f pipeline.yaml [--dry-run] | list CORPUS | status ID [--watch] | diff A B | archive ID
ragcentre query CORPUS "question" [--pipeline ID] [--top-k N] [--filter JSON] [--no-transform]
ragcentre cache clear --transforms
ragcentre eval import CORPUS NAME file.jsonl | run DATASET PIPELINE... [--wait] | show RUN
```

Run `ragcentre COMMAND --help` for details.

## Settings

| Variable | Default | |
|---|---|---|
| `RAGCENTRE_DATA_DIR` | `./data` | Database, blobs, embedding cache and the default vector store |
| `RAGCENTRE_HOST` / `RAGCENTRE_PORT` | `127.0.0.1` / `8700` | Where `serve` listens |
| `RAGCENTRE_WORKERS` | `4` | Ingestion worker threads |
| `RAGCENTRE_API_TOKEN` | unset | When set, API requests need `Authorization: Bearer <token>` except `GET /v1/health` |
| `RAGCENTRE_ALLOW_REMOTE_SETUP` | unset | Allow first-run account setup in the browser on a non-loopback bind |
| `RAGCENTRE_SECRET_KEY` | unset | Fernet key for storing provider secrets; without it only environment-variable references are allowed |
| `RAGCENTRE_SECRET_ENV_PREFIXES` | `RAGCENTRE_PROVIDER_` | Comma-separated prefixes allowed for provider secret references |
| `RAGCENTRE_EGRESS_ALLOW` | unset | Optional comma-separated provider hostnames, IP addresses or CIDRs |
| `RAGCENTRE_MAX_UPLOAD_MB` | `200` | Request body size limit, including multipart overhead |
| `RAGCENTRE_MAX_ATTEMPTS` | `5` | Attempts before a retryable failure becomes permanent |
| `RAGCENTRE_LOG_LEVEL` | `INFO` | JSON logs on standard error |

## Security upgrade

Back up the data directory before opening it with schema version 3. To roll back to an older
binary, restore that backup.

- Set existing provider secrets again after upgrading. The server binds each secret to the
  provider's destination and clears it when the destination changes. Use environment variables
  such as `RAGCENTRE_PROVIDER_OPENAI_API_KEY`, or configure dedicated prefixes for your deployment.
  The API token and encryption key cannot be referenced as provider secrets.
- The built-in HTTP transport blocks link-local and known cloud metadata addresses at connect time
  and disables redirects and environment proxies. SDK-backed stores are subject to configuration
  checks for explicit endpoints. Enforce the same restrictions with network egress controls for
  SDK-resolved endpoints and OCR services that make their own upstream connections.
- Use relative local store paths under `<data dir>/stores/`. Move existing stores configured
  with a path into that directory and update the provider configuration. Existing stores in the
  default locations are recognized automatically.
- Run `ragcentre rebuild-broken-indexes` against the data directory if an index is flagged with a
  legacy name collision. Start the ingestion workers to repopulate it. Old vector collections are
  left for retirement by the store administrator after verifying the rebuild.
- Run ingestion workers to process deletions and old content purges. SQLite 3.42 or later is
  required for FTS secure deletion. Configure retention separately for backups and external
  services.

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
a build, `/` explains how to make one. The UI signs you in with your account, and **Account → API keys**
mints the `read`/`full` keys that MCP, the CLI and scripts use.

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
