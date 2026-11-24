# Changelog

## 0.0.4

- Add chunk enrichment. An `enrich` pipeline node asks an `llm` provider for a short text per chunk
  (`situate`, `summary`, `keywords` or `questions`, or your own prompt) and stores it beside the chunk,
  so chunk ids do not change and enriched and plain sources still fuse. Embedding, keyword indexing,
  reranking and query results use the enriched text; results gain `enrichment` and the response
  `enriched_sources`. Enrich nodes may stack; at most one may feed the retrieval sources.
- Add the `llm` provider kind with an `openai_compatible` type (`POST /chat/completions`). Answers are
  cached by provider fingerprint and request, and `max_tokens_per_run` and `daily_token_budget` stop a
  run before it spends more than you allow. See `docs/protocols/llm-v1.md`.
- The pipeline builder has an optional Enrich column, and chunks, query results and pipeline diffs show
  the enrichment. Database migration 0004 adds the `chunk_enrichments` table.

## 0.0.3

- Harden API request handling, outbound provider access, provider secret scoping,
  corpus isolation, and document cleanup.
- Add a standalone RAGCentre MCP server with stdio and Streamable HTTP transports.
- Detect running OCR shims and show whether their engine works, proven by a scanned test page, on
  Providers, in the pipeline parser picker and in the status bar, with one-click setup.
- shimkit reports an optional `engine` object on `GET /info`. The Docling shim's remote mode keeps its
  version and reports `unreachable` while docling-serve is down.
- Fix OCR in the Docling shim image: install onnxruntime so RapidOCR uses the models in its wheel, and
  check at build time that it reads text offline. The shim is now 0.3.1 because its output changes, so
  a provider pinned to 0.3.0 must be created again.

## 0.0.2

- Add HTTP vector-store services and adapters for LanceDB, Redis, Milvus, Weaviate,
  Pinecone, Elasticsearch, OpenSearch and MongoDB Atlas Vector Search.
- Expand Qdrant, pgvector, Chroma and sqlite-vec deployment and index options, with
  per-instance capabilities, collection catalogs and compatibility checks.
- Add `ragcentre store list`, live `ragcentre store check`, deep provider checks,
  provider examples and capability details in the web UI.
- Add asynchronous parser jobs, remote parser engine modes, typed parser options
  and richer document mapping for the existing parser services.
- Add EasyOCR and docTR parser services with streaming page rendering, shared text
  layout helpers, Docker Compose profiles and protocol examples.
- Improve catalog concurrency, upgrade handling and adapter regression coverage;
  fix CI checks for optional clients and the shared Docling shim dependency.

## 0.0.1

- Initial retrieval service, web UI, evaluation harness and Docker deployment.
