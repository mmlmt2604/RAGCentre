# Embedder protocol (OpenAI-compatible)

RAGCentre does not define its own embedding protocol. It speaks the OpenAI embeddings API, which
most embedding servers already implement: OpenAI, Azure OpenAI (through a compatible gateway),
vLLM, Ollama (`/v1`), Hugging Face Text Embeddings Inference (`/v1`), Infinity, LiteLLM, LM Studio
and others.

RAGCentre's provider type is `embedder/openai_compatible`. For tests and demos there is also
`embedder/builtin_hash`, which runs in-process and needs no server.

## Request

`POST {base_url}/embeddings`, JSON:

```json
{"model": "text-embedding-3-small", "input": ["first text", "second text"], "encoding_format": "float"}
```

`"dimensions": N` is added only when the provider's `dimensions` is set (for models that support
shortened embeddings). Texts are sent in batches of `batch_size` (default 64).

Before sending, RAGCentre prepends `document_prefix` to chunk texts or `query_prefix` to queries
(for models such as E5 or BGE that expect `"passage: "` / `"query: "`), then truncates to
`max_input_chars` if set. Truncated inputs are counted in the run's statistics.

## Response

```json
{
  "data": [
    {"index": 0, "embedding": [0.0123, -0.0456, …]},
    {"index": 1, "embedding": [0.0789, 0.0012, …]}
  ]
}
```

RAGCentre checks that:

- `data` has exactly one item per input;
- each item's `index` is a distinct position in the batch (items may arrive in any order; a
  missing `index` means "in order");
- every `embedding` is a non-empty list of finite numbers, and all have the same length;
- if `dimensions` was requested, every vector has that length.

Any failed check is a permanent error. Other response fields (`model`, `usage`, `object`) are
ignored. Base64-encoded embeddings are not supported; RAGCentre always asks for `float`.

With `normalize: true`, RAGCentre L2-normalises the vectors itself (useful for models that do not,
when the vector index uses `dot`).

## Errors and retries

Connection errors, timeouts, `408`, `429` and `5xx` are retried with backoff, honouring
`Retry-After` on `429`/`503`. Other `4xx` (bad model name, input too long) fail the run
permanently. Error messages are read from `{"error": {"message": …}}`, `{"error": "…"}` or
`{"detail": …}`.

## Authentication

With an `api_key` secret, RAGCentre sends `Authorization: Bearer <api_key>`. Keep the key out of
files by referencing an environment variable:

```sh
uv run ragcentre provider set-secret openai-small api_key --env OPENAI_API_KEY
```

## Provider configuration

```yaml
name: openai-small
kind: embedder
type: openai_compatible
config:
  base_url: https://api.openai.com/v1   # required; RAGCentre appends /embeddings
  model: text-embedding-3-small          # required
  dimensions: null                       # optional
  normalize: false
  query_prefix: ""
  document_prefix: ""
  batch_size: 64
  max_input_chars: null
  timeout_seconds: 60
  max_concurrency: 4
  revision: ""
```

The fingerprint is `model`, `dimensions`, `normalize`, `query_prefix`, `document_prefix` and
`revision`: changing any of them means new embeddings, so it requires a new provider. `base_url`,
`batch_size`, `max_input_chars`, `timeout_seconds` and `max_concurrency` can be changed later.
If the server silently swaps the model behind the same name, change `revision` (by creating a new
provider) so the new vectors are not mixed with the old ones.

## Notes for specific servers

- **Ollama**: use `base_url: http://127.0.0.1:11434/v1`.
- **Text Embeddings Inference**: use `base_url: http://host:port/v1`; its native `/embed`
  endpoint is a different protocol.
- **vLLM**: start the server with an embedding model (`--task embed`); `base_url` ends in `/v1`.
