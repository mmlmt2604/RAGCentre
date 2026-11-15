# Parser protocol — `ragcentre.parser/v1`

A parser service turns an uploaded file into a **Document IR**: an ordered list of text blocks
with their structure, pages and positions. Write one to plug any parser or OCR system into
RAGCentre. `examples/docling_shim/` is a complete example, and `examples/mineru_shim/`,
`examples/paddleocr_shim/` and `examples/surya_shim/` are three more (MinerU, PaddleOCR and Surya),
built on the shared helpers in `examples/shimkit/`.

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
  "options_schema": {"type": "object", "properties": {"ocr": {"type": "boolean"}}, "additionalProperties": false}
}
```

- `protocol` must be exactly `ragcentre.parser/v1`.
- `name` and `version` are required, non-empty strings. RAGCentre reads them **when the provider
  is created** and stores them in the provider's fingerprint. Change `version` whenever a change
  could alter your output (a new model, a new library version, a new mapping): parses from
  different versions must not be mixed in one index.
- `mime_types` (optional) is informational. Which files reach the service is decided by the
  pipeline's parse routes.
- `capabilities` (optional) is informational in version 1.
- `options_schema` (optional) is a JSON Schema for the `options` object of `POST /parse`. It is
  informational in this release.

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

A service should reject options it does not recognise, or whose type is wrong, with
`400 invalid_options` and a message that names the option, rather than ignore them: options are part
of the pipeline's fingerprint, so a mistyped one would otherwise re-parse every document and change
nothing.

The response is `200` with a Document IR. The call is synchronous; RAGCentre's default timeout
for parser providers is 600 seconds (`timeout_seconds`).

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
  timeout_seconds: 600              # default 600
  max_concurrency: 4                # parallel requests, default 4
  revision: ""                      # change to force a new fingerprint
  on_version_drift: fail            # or warn
```

The fingerprint is `service_name`, `service_version` (both captured from `/info`) and `revision`.
`base_url`, `timeout_seconds` and `max_concurrency` can be changed later without new work.
