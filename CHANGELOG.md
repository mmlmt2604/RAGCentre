# Changelog

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
