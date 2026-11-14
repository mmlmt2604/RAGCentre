# Chunker protocol — `ragcentre.chunker/v1`

RAGCentre has five built-in chunk strategies (`fixed`, `recursive`, `structure`, `page` and
`http`). The `http` strategy hands the parsed document to your own service, so you can chunk with
any logic you like (semantic splitting, an LLM, domain rules) and still use the rest of the
pipeline.

RAGCentre's provider type for this protocol is `chunker/http`.

## Endpoints

### `GET /info`

```json
{"protocol": "ragcentre.chunker/v1", "name": "semantic-chunker", "version": "1.2.0"}
```

`protocol` must be exactly `ragcentre.chunker/v1`; `name` and `version` are required, non-empty
strings. As with parsers, RAGCentre pins `name` and `version` when the provider is created, and
refuses to chunk with a service that reports a different version (unless `on_version_drift` is
`warn`). Bump `version` whenever your output could change.

### `POST /chunk`

Request (JSON):

```json
{
  "document": {
    "schema_version": "1.0",
    "metadata": {},
    "pages": [{"page": 1, "width": 612.0, "height": 792.0}],
    "blocks": [
      {"id": "b1", "type": "heading", "level": 1, "text": "Intro", "page": 1,
       "heading_path": ["Intro"], "metadata": {}},
      {"id": "b2", "type": "paragraph", "text": "…", "page": 1, "heading_path": ["Intro"], "metadata": {}}
    ]
  },
  "options": {"target_tokens": 300}
}
```

- `document` is the validated Document IR (see `parser-v1.md`), with every block's `id` and
  `heading_path` filled in. Page images are removed. Unlike the built-in strategies, `page_header`
  and `page_footer` blocks are included; skip them yourself if you do not want them.
- `options` is the chunk node's `options` from the pipeline, passed through unchanged.

Response `200`:

```json
{
  "chunks": [
    {"text": "Intro\n\n…", "context": "Intro", "block_ids": ["b1", "b2"], "metadata": {}}
  ]
}
```

| Field | Rules |
|---|---|
| `text` | Required string. What the user sees in results, and what evaluation matches against. |
| `context` | Optional string or `null`. A prefix used for embedding and keyword indexing but not shown as the chunk's text (e.g. a heading path or a summary). The indexed string is `context + "\n\n" + text`. |
| `block_ids` | List of the IR block ids the chunk draws from (may be empty). Every id must exist in the document. |
| `metadata` | Optional object. |

Return chunks in document order. RAGCentre fills in each chunk's `ordinal`, `page_start` and
`page_end` (from the blocks' pages) and `heading_path` (from the first block). Chunks whose
text is empty after stripping are dropped; a document with no chunks is a success with the
warning `no_chunks`.

## Errors and retries

The same as for parsers: return `{"error": {"code": "…", "message": "…"}}` with a non-2xx status.
Connection errors, timeouts, `408`, `429` and `5xx` are retried; other `4xx` and an invalid
response (missing `chunks`, a non-string `text`, an unknown block id) fail the run permanently.

## Authentication

With an `api_key` secret on the provider, RAGCentre sends `Authorization: Bearer <api_key>`.

## Using it in a pipeline

```yaml
# provider
name: semantic
kind: chunker
type: http
config:
  base_url: http://127.0.0.1:8703
  timeout_seconds: 120      # default 120
```

```yaml
# pipeline node
- id: chunk
  type: chunk
  inputs: [parse]
  strategy: http
  provider: semantic
  options: {target_tokens: 300}
```

The node's fingerprint includes the provider's fingerprint (`service_name`, `service_version`,
`revision`) and the options, so changing either creates new chunks instead of mixing old and new.
