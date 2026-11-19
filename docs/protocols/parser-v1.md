# Parser protocol — `ragcentre.parser/v1`

A parser service turns an uploaded file into a **Document IR**: an ordered list of text blocks
with their structure, pages and positions. Write one to plug any parser or OCR system into
RAGCentre. `examples/docling_shim/` is a complete example, and `examples/mineru_shim/`,
`examples/paddleocr_shim/` and `examples/surya_shim/` are three more (MinerU, PaddleOCR and Surya),
built on the shared helpers in `examples/shimkit/`. [`parser-shim-guide.md`](parser-shim-guide.md)
walks through writing one, and `ragcentre parser check <url>` tells you whether yours is usable and
which RAGCentre features its output feeds.

RAGCentre's provider type for this protocol is `parser/http`.

## Endpoints

### `GET /info`

```json
{
  "protocol": "ragcentre.parser/v1",
  "name": "docling-shim",
  "version": "0.1.0+docling.2.150.0",
  "mime_types": ["application/pdf", "image/*"],
  "capabilities": {"page_images": true},
  "options_schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "ocr": {"type": "boolean", "default": true, "description": "Run OCR on scanned pages"},
      "dpi": {"type": "integer", "minimum": 72, "maximum": 400}
    }
  }
}
```

- `protocol` must be exactly `ragcentre.parser/v1`.
- `name` and `version` are required, non-empty strings. RAGCentre reads them **when the provider
  is created** and stores them in the provider's fingerprint. Change `version` whenever a change
  could alter your output (a new model, a new library version, a new mapping): parses from
  different versions must not be mixed in one index.
- `mime_types` (optional) is informational. Which files reach the service is decided by the
  pipeline's parse routes.
- `capabilities` (optional). `async: true` turns on the job endpoints below. `page_images` is informational.
- `options_schema` (optional): a JSON Schema for the `options` object that `/parse` accepts. RAGCentre
  keeps a copy with the provider (refreshed by `provider test`), checks a pipeline's parse-route options
  against it when the pipeline is saved, and builds the options form in the UI from it. It reads only
  `properties` with `type` (`boolean`, `integer`, `number`, `string`, `array`), `enum`, `minimum`,
  `maximum`, `items`, `minItems`, `maxItems`, `default` and `description`, plus
  `additionalProperties: false` to make unknown options an error. Anything else is ignored, and a
  service without a schema accepts any options. A service that publishes a schema should reject
  options it does not list with `400 invalid_options`.

RAGCentre calls `/info` without credentials when the provider is created, and again (with
credentials, cached for five minutes) before parsing. If the reported version differs from the
pinned one, parsing fails with `provider_version_drift`, unless the provider's `on_version_drift`
is `warn`.

### `POST /parse`

A `multipart/form-data` request:

| Part | Content |
|---|---|
| `file` | The document, with its filename and MIME type |
| `options` | A JSON object, as a string: the parse route's `options` (may be `{}`) |

The response is `200` with a Document IR. By default the call is synchronous; RAGCentre's default
timeout for parser providers is 600 seconds (`timeout_seconds`). A service that needs longer
offers async jobs (below).

### Async jobs (protocol 1.1, optional)

OCR of a large scan can take minutes per page, longer than any sensible request timeout. A service
that can run a parse in the background says so in `/info`:

```json
{"capabilities": {"page_images": true, "async": true}}
```

RAGCentre then submits every document as a job and polls it. A sync-only service needs none of
this and keeps working unchanged, and RAGCentre never sends `mode=async` to it.

| Request | Response |
|---|---|
| `POST /parse?mode=async` (same body as above; `Prefer: respond-async` is equivalent) | `202 {"job_id": "…", "poll_after_seconds": 2}` |
| `GET /jobs/{job_id}` | `200 {"status": "running"}`, `{"status": "succeeded"}` or `{"status": "failed", "error": {"http_status": 422, "code": "…", "message": "…"}}`. `404` when the job is unknown or expired. |
| `GET /jobs/{job_id}/result` | The Document IR (`200`) when it succeeded. For a failed job, the same status and `{"error": …}` body the synchronous call would have returned. `409` while it is running. |
| `DELETE /jobs/{job_id}` | `204`. Optional, best effort; RAGCentre calls it after reading a result. |

- `job_id` is an opaque string. The `/jobs/…` requests carry the same credentials as `/parse`.
- RAGCentre waits `poll_after_seconds` (clamped to 0.5–30 s) before the first status request, then
  backs off by 1.5× up to 30 s. It gives up after the provider's `job_timeout_seconds` (default
  21600, six hours) and retries the document later, resuming the same job. `timeout_seconds`
  then limits each request, not the whole parse.
- `failed.error.http_status` is the status a sync call would have used: `4xx` is permanent for the
  document, `5xx` and `429` are retried with a fresh job.
- RAGCentre stores the `job_id` while the job runs, so after a restart it resumes polling. If the
  service answers `404` for a stored job (it restarted or expired the result), RAGCentre submits the
  document again once.
- A service should keep finished jobs for at least an hour, and may run jobs concurrently or one at a
  time. Options are validated when the job runs, not at submit time: bad options end the job in
  `failed` with `http_status: 400`.
- A service that ignores `?mode=async` and answers `200` with the IR is also accepted.

## Document IR (schema 1.x)

```json
{
  "schema_version": "1.0",
  "metadata": {"title": "Pump Manual", "language": "en", "page_count": 12},
  "pages": [
    {"page": 1, "width": 612.0, "height": 792.0, "image": {"mime": "image/png", "data_b64": "…"}}
  ],
  "blocks": [
    {"id": "b1", "type": "heading", "level": 1, "text": "Introduction",
     "page": 1, "bbox": [72.0, 80.0, 540.0, 104.0], "metadata": {}},
    {"id": "b2", "type": "paragraph", "text": "…", "page": 1},
    {"id": "b3", "type": "table", "text": "| a | b |\n|---|---|\n| 1 | 2 |",
     "html": "<table>…</table>", "page": 2}
  ]
}
```

| Field | Rules |
|---|---|
| `schema_version` | Required. Any `1.x` is accepted; another major version is a permanent error. |
| `metadata` | Optional free-form object. |
| `pages` | Optional. `page` is 1-based; `width`/`height` are in the units of `bbox`. `image` only when asked for (`include_page_images` in the options). |
| `blocks` | Required, **in reading order**. |
| `blocks[].type` | Required. One of `heading`, `paragraph`, `list_item`, `table`, `figure`, `code`, `formula`, `caption`, `footnote`, `page_header`, `page_footer`. An unknown type is accepted as `paragraph` and the original kept in `metadata.original_type`. |
| `blocks[].text` | Required string; may be empty for a `figure`. For a `table`, the table as Markdown. |
| `blocks[].level` | Required for `heading` (1–6); ignored otherwise. |
| `blocks[].id` | Optional. Blocks without one get `b{index}` (1-based). Duplicate ids are rejected. |
| `blocks[].page` | Optional, 1-based. Needed for page-aware chunking and for evaluation targets that name pages. |
| `blocks[].bbox` | Optional `[x0, y0, x1, y1]`, origin **top-left**, in the page's units. |
| `blocks[].html` | Optional, for tables. |
| `blocks[].metadata` | Optional free-form object. |

### Block metadata RAGCentre reads

`blocks[].metadata` is free-form, and RAGCentre keeps all of it. These keys have a meaning:

| Key | Type | Used for |
|---|---|---|
| `page_end` | integer | The last page a block touches, when it runs over a page break (a table split across pages). `page` is the first. A chunk's `page_end` is the largest of its blocks' `page` and `page_end`. Ignored unless it is greater than `page` |
| `original_type` | string | Set by RAGCentre when it turns an unknown block `type` into `paragraph`; a parser can also set it to say what a `paragraph` was |
| `language` | string | The programming language of a `code` block, or the language of a text block |
| `classification` | string | What a `figure` shows (`bar_chart`, `logo`...) |
| `confidence` | number 0 to 1 | The recogniser's confidence in the block |

Only `page_end` changes what RAGCentre does today; the others are shown in the UI and passed to
custom chunking services.

RAGCentre computes each block's `heading_path` (the enclosing headings) after validation: a
heading of level N closes every open heading of level N or deeper. The built-in chunkers drop
`page_header` and `page_footer` blocks; a custom chunker service receives them.

## Errors

Return a non-2xx status with:

```json
{"error": {"code": "unsupported_media_type", "message": "cannot read .xyz files"}}
```

RAGCentre classifies failures:

| Response | Treatment |
|---|---|
| Connection error, timeout, `408`, `429`, `5xx` | Retried with backoff. `Retry-After` on `429`/`503` sets the minimum delay. |
| Other `4xx` | Permanent: the document's parse run fails and is not retried. |
| `200` with an invalid IR (bad JSON, wrong `schema_version`, missing `type`/`text`, heading without a valid `level`, duplicate ids) | Permanent. |

So: use `4xx` for "this file cannot be parsed" and `5xx`/`503` for "try again later".

## Authentication

When the provider has an `api_key` secret, RAGCentre sends `Authorization: Bearer <api_key>` on
every request (except the `/info` call at creation). Each provider receives only its own secret.

## Provider configuration

```yaml
name: docling
kind: parser
type: http
config:
  base_url: http://127.0.0.1:8701   # required
  timeout_seconds: 600              # default 600; per request
  job_timeout_seconds: 21600        # async services only: the whole parse, default 6 hours
  max_concurrency: 4                # parallel requests, default 4
  revision: ""                      # change to force a new fingerprint
  on_version_drift: fail            # or warn
```

The fingerprint is `service_name`, `service_version` (both captured from `/info`) and `revision`.
`base_url`, `timeout_seconds` and `max_concurrency` can be changed later without new work.
