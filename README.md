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

## Concepts

- **Provider**: a configured parser, chunker, embedder, reranker or vector store. Built in:
  `builtin_text` (Markdown and plain text), `builtin_hash` (a lexical embedder for demos and
  tests) and `sqlite_vec`. Over HTTP: any parser or chunker speaking RAGCentre's protocols, any
  OpenAI-compatible embeddings server, and Cohere/Jina-style rerankers. Optional vector stores:
  Qdrant, PostgreSQL with pgvector, and Chroma.
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
  shared package, [`shimkit/`](examples/shimkit/).
- [`docs/protocols/chunker-v1.md`](docs/protocols/chunker-v1.md): custom chunking.
- [`docs/protocols/embedder-v1.md`](docs/protocols/embedder-v1.md): OpenAI-compatible embeddings.
- [`docs/protocols/reranker-v1.md`](docs/protocols/reranker-v1.md): Cohere/Jina-style reranking.

Third-party vector stores can be added as Python packages through the `ragcentre.vector_stores`
entry point; `ragcentre.testing.vector_store_contract` is the test suite an adapter must pass.

### Optional vector stores

`sqlite_vec` works out of the box. The `qdrant`, `pgvector` and `chroma` stores need their
optional extras (`ragcentre[qdrant]`, `ragcentre[pgvector]`, `ragcentre[chroma]`); RAGCentre does
not install them, and using a store whose extra is missing fails with an error naming it.

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
[`mineru_shim`](examples/mineru_shim/), [`paddleocr_shim`](examples/paddleocr_shim/) and
[`surya_shim`](examples/surya_shim/). Working plans and design notes are kept locally, not in the
repository.
