# LLM protocol (OpenAI-compatible chat completions)

RAGCentre does not define its own protocol for language models. It speaks the OpenAI chat
completions API, which llama-server, vLLM, Ollama (`/v1`), LM Studio, LiteLLM, OpenRouter and the
hosted APIs implement.

RAGCentre's provider type is `llm/openai_compatible`. Enrich nodes (chunk enrichment) use it to
write a short text for every chunk.

## Request

`POST {base_url}/chat/completions`, JSON:

```json
{
  "model": "qwen2.5-7b-instruct",
  "messages": [
    {"role": "system", "content": "You help index documents for search. Here is the whole document, titled guide.md:\n<document>\n…\n</document>"},
    {"role": "user", "content": "Here is the chunk we want to situate within the whole document\n<chunk>\n…\n</chunk>\n…"}
  ],
  "max_tokens": 200,
  "temperature": 0.0
}
```

The system message is present only when the enrich node sends the document (`max_doc_chars` above
0). It is the same for every chunk of a document, so a server with prompt-prefix caching can reuse
it. RAGCentre sends a document's requests in parallel, so the first few requests may all pay for the
document before the cache is warm; lower `max_concurrency` on the provider if that matters.

An `api_key` secret, when set, is sent as `Authorization: Bearer <key>`.

## Response

```json
{
  "choices": [{"message": {"role": "assistant", "content": "This chunk is from the refunds section…"}}],
  "usage": {"prompt_tokens": 1180, "completion_tokens": 41}
}
```

RAGCentre checks that `choices[0].message.content` is a string (a missing or `null` content counts
as an empty answer). `usage` is optional; without it, tokens are estimated at four characters per
token. A response that does not match returns `provider_error`; an HTTP 429 or 5xx is retried by the
queue, and a `Retry-After` header on a 429 or 503 sets the minimum delay.

## Provider configuration

| Field | Fingerprint | Meaning |
|---|---|---|
| `base_url` | no | Server URL, with the `/v1` prefix if the server needs it |
| `model` | yes | Model name sent in each request |
| `revision` | yes | Free string; change it to force a new fingerprint (for example after the model behind a name changes) |
| `timeout_seconds`, `max_concurrency` | no | Request timeout and parallel requests |
| `max_tokens_per_run` | no | Fail one document's enrichment when its estimated tokens (prompt and answer, uncached requests only) exceed this |
| `daily_token_budget` | no | Stop enriching for the rest of the UTC day once this many tokens are used. Runs in flight at the same time check the budget independently, so it can be exceeded slightly |

A run stopped by either limit fails with `llm_budget_exceeded` before any request is sent. Retry
it after raising the limit (the fields are not part of the fingerprint, so editing them does not
invalidate anything) or when the day changes.

`POST /v1/providers/{id}/test` sends a one-word prompt and reports the model name.

## Caching

Every answer is stored in `cache.db` under the provider's fingerprint and the SHA-256 of the whole
request (system message, prompt, `max_tokens`, `temperature`). A retry, a second pipeline or a second
corpus that sends the same requests reads the stored answers and makes no calls. Changing the
model, the revision, the mode, the prompt, `max_doc_chars`, `max_tokens` or `temperature` changes the
requests, so the answers are asked for again.

## Enrich node settings

| Option | Default | Meaning |
|---|---|---|
| `mode` | `situate` | `situate` (where the chunk sits in its document), `summary`, `keywords` or `questions` |
| `prompt` | none | Replaces the mode's instruction. Must contain `{chunk}`; `{context}`, `{heading_path}` and `{title}` are also filled in |
| `max_doc_chars` | 24000 | How much of the document goes into the system message; 0 sends the chunk alone |
| `max_tokens` | 200 | Cap on each answer |
| `temperature` | 0.0 | Sampling temperature |

All of these enter the enrich node's fingerprint.
