# Reranker protocol (Cohere/Jina-style `/rerank`)

A reranker reorders the retrieved chunks by relevance to the query. RAGCentre speaks the
`/rerank` request shape shared by Cohere, Jina, Voyage, Mixedbread and the self-hosted servers
that copy it (Infinity, LiteLLM, vLLM's `/v1/rerank`, llama.cpp server).

RAGCentre's provider type is `reranker/http`.

## Request

`POST {base_url}/rerank`, JSON:

```json
{
  "model": "BAAI/bge-reranker-v2-m3",
  "query": "how long is the frame warranty?",
  "documents": ["first chunk text", "second chunk text", "…"],
  "top_n": 30
}
```

`documents` are the best `rerank.top_n` retrieved chunks (fewer if fewer were retrieved), each as
its indexed text (context and text), in retrieval order. `top_n` is the number of documents sent,
so the server scores all of them. RAGCentre does not send `return_documents`; it
does not need the texts back.

## Response

```json
{"results": [{"index": 1, "relevance_score": 0.91}, {"index": 0, "relevance_score": 0.12}]}
```

- `index` is the position in `documents` (0-based). Each index may appear once.
- `relevance_score` must be a finite number; higher is more relevant. A `score` field is accepted
  in its place.
- Results may be in any order and may cover fewer documents than were sent (servers apply
  `top_n`). RAGCentre sorts by score, best first, breaking ties by index.

An index out of range, a repeated index, or a missing or non-numeric score is a permanent error.

## Servers that differ

| Server | Difference | What to do |
|---|---|---|
| Hugging Face Text Embeddings Inference | `POST /rerank` takes `{"query", "texts"}` and returns a bare list `[{"index", "score"}]` | Put a compatible gateway in front of it (Infinity or LiteLLM serve the same models with this protocol) |
| Cohere v2 API | Same shape at `https://api.cohere.com/v2/rerank` | `base_url: https://api.cohere.com/v2` |
| Jina | Same shape at `https://api.jina.ai/v1/rerank` | `base_url: https://api.jina.ai/v1` |
| vLLM | Same shape at `/v1/rerank` (and `/rerank`) | `base_url: http://host:port/v1` |

## Errors and retries

As for every HTTP provider: connection errors, timeouts, `408`, `429` and `5xx` are retried
(with `Retry-After` honoured on `429`/`503`); other `4xx` and invalid responses are permanent. A
reranker failure during a query returns `502 provider_error` to the caller.

## Authentication

With an `api_key` secret, RAGCentre sends `Authorization: Bearer <api_key>`.

## Provider configuration and use

```yaml
name: reranker
kind: reranker
type: http
config:
  base_url: http://127.0.0.1:8702   # required; RAGCentre appends /rerank
  model: BAAI/bge-reranker-v2-m3    # required
  timeout_seconds: 60
  max_concurrency: 4
  revision: ""
```

```yaml
# in a pipeline's retrieval section
retrieval:
  sources: [{id: dense, index: vec, top_k: 30}]
  rerank: {provider: reranker, top_n: 30}
  top_k: 8
```

The fingerprint is `model` and `revision`. Reranking happens at query time only, so changing the
reranker never rebuilds an index; it does change the pipeline's fingerprint, which keeps
evaluation results for different rerankers apart.
