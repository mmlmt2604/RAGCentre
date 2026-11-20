# Memory shim

A reference [`ragcentre.vector_store/v1`](../../../docs/protocols/vector-store-v1.md) service over
an in-memory brute-force index. It is the working example for
[`vector-store-shim-guide.md`](../../../docs/protocols/vector-store-shim-guide.md): replace the
dicts in `memory_shim/service.py` with your database's calls to wrap a real store.

Run it:

```sh
cd examples/vector_store_shims/memory
uv sync --extra local
uv run uvicorn memory_shim:app --port 8702
```

Register it in RAGCentre:

```yaml
name: memory
kind: vector_store
type: http
config:
  url: http://127.0.0.1:8702
```

Set `MEMORY_SHIM_API_KEY` (or pass `api_key=` to `create_app`) to require a bearer token on every
request except `GET /info`, then give RAGCentre the same value as the provider's `api_key` secret.

Data lives in memory and disappears on restart. The service is for the contract suite and as a
starting point, not for production ingestion.
