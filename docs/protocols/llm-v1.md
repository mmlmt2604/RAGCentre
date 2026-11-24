# Language model provider

The `llm` kind exposes synchronous, non-streaming chat completions. Consumers pass their own
prompts as a sequence of system, user, or assistant messages. Installed packages can register
other implementations through `ragcentre.provider_types`. The built-in type is
`openai_compatible`.

## Request

The provider sends `POST {base_url}/chat/completions`. Include the API version prefix in
`base_url` when the server uses one, for example `http://localhost:11434/v1`. The optional
`api_key` provider secret becomes a bearer token.

```json
{
  "model": "llama3.2",
  "messages": [
    {"role": "system", "content": "Give a short answer."},
    {"role": "user", "content": "What is a corpus?"}
  ],
  "temperature": 0,
  "max_tokens": 128,
  "stream": false
}
```

Text content can also be sent as `{"type":"text","text":"…"}` within an ordered content array.
With `supports_images: true`, an image part becomes
`{"type":"image_url","image_url":{"url":"data:image/png;base64,…"}}`. Image input accepts
PNG, JPEG and WebP. The upstream model must support the input format.

The provider sends `temperature` (default 0, range 0–2) unless it is set to null, which leaves
the server default; OpenAI reasoning models accept only their default. It always sends a token
limit (default 1024, range 1–131072) in the field named by `token_limit_field`: `max_tokens`
(the default, which most servers accept) or `max_completion_tokens` (required by OpenAI
reasoning models). Servers that do not know `max_completion_tokens` may ignore it
(Ollama does), so use it only where it is needed. A call can request a smaller positive token limit. It sends `top_p` (range 0–1)
and `seed` when configured. A call requesting JSON adds
`"response_format": {"type":"json_object"}`. The consumer should also ask for JSON in the
prompt and validate the returned text against its needs.

`extra_body` supplies additional server-specific fields such as `chat_template_kwargs`. It must
be a JSON object. The reserved keys are `model`, `messages`, `stream`, `temperature`, `top_p`,
`seed`, `max_tokens`, `max_completion_tokens`, and `response_format`. Store credentials using the provider's secret settings.

## Response and connection test

RAGCentre reads the first entry of a non-empty `choices` array. Its `message.content` must
contain non-blank text, with one exception: when `finish_reason` is `length` and there is no
answer text (a reasoning model spent the whole limit thinking), the completion is returned with
empty text. Reasoning returned in `reasoning_content` or `reasoning` is never passed on.
`finish_reason` maps to `stop`, `length`, or `other`. The consumer decides whether to accept a
response that reached its token limit. The reported `model` and integer `usage.prompt_tokens`
and `usage.completion_tokens` are optional.

Connection and timeout failures, HTTP 408, 429 and 5xx are retryable. Other 4xx responses and
invalid success bodies are permanent failures. A `Retry-After` header on 429 or 503 supplies a
minimum retry delay. Each `complete` call makes one request.

The connection test sends a short text prompt with a limit of 64 tokens or the configured
ceiling, whichever is lower. Any non-blank answer passes. A response that reached the limit
before answering also passes, with a message that the model spends its budget on reasoning. The result shows the model name
returned by the endpoint or the configured model name.

## Configuration

Model, sampling settings, token ceiling, image support, extra request fields and optional
`revision` are permanent after creation and enter the provider fingerprint. The base URL,
timeout (default 120 seconds), concurrency limit and `token_limit_field` can be edited. Updating the endpoint may
require setting its secret again.

An [Ollama provider YAML](../../examples/providers/openai-compatible-llm.yaml) is included.
Automated tests exercise the protocol with a mock HTTP transport. A live run on 2026-11-24
against Ollama with `qwen2.5:0.5b` passed the connection test, plain and JSON-mode completions,
and a truncated completion. Reasoning models and OpenAI itself have not been run. See the [OpenAI Chat Completions reference](https://platform.openai.com/docs/api-reference/chat/create)
and [Ollama OpenAI compatibility reference](https://docs.ollama.com/api/openai-compatibility)
for server-specific options.
