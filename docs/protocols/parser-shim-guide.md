# Write your own parser shim

A **shim** is a small web service that puts your OCR or document-parsing engine behind
[`ragcentre.parser/v1`](parser-v1.md). RAGCentre posts a file to it and gets back a **Document IR**:
an ordered list of blocks (headings, paragraphs, tables...) with pages and positions. This guide
shows the shortest way to write one, what RAGCentre does with what you return, and how to check it.

`examples/` has six complete shims (Docling, MinerU, PaddleOCR, Surya, EasyOCR, docTR) to read next to
this guide. All of them are built on `examples/shimkit`, the same helper this guide uses. If your
engine returns only text lines, as EasyOCR and docTR do, `shimkit.lines_to_ir` groups them into
paragraphs and can guess headings, and `shimkit.render_pages` turns a PDF into page images.

## 1. A shim in forty lines

Install the helper from a RAGCentre checkout (`pip install ./examples/shimkit`; it is not published
to PyPI), your engine, and `uvicorn`. This is `examples/shimkit/example/tesseract_shim.py`, a real
file with its own tests:

```python
import io

import pytesseract
from PIL import Image

from shimkit import ShimError, build_ir, create_app, file_extension, parse_options, string

OPTIONS = [string("lang", "eng", "Tesseract language codes, for example eng or eng+deu")]
IMAGES = {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"}


def parse(content: bytes, filename: str, options: dict) -> dict:
    opts = parse_options(OPTIONS, options)
    if file_extension(filename) not in IMAGES:
        raise ShimError(415, "unsupported_media_type", f"{filename}: this service reads images only")
    image = Image.open(io.BytesIO(content))
    words = pytesseract.image_to_data(image, lang=opts["lang"], output_type=pytesseract.Output.DICT)
    paragraphs: dict[tuple, list[int]] = {}  # (block, paragraph) -> indexes of its words, in reading order
    for i, text in enumerate(words["text"]):
        if text.strip():
            paragraphs.setdefault((words["block_num"][i], words["par_num"][i]), []).append(i)
    blocks = []
    for indexes in paragraphs.values():
        boxes = [(words["left"][i], words["top"][i], words["width"][i], words["height"][i]) for i in indexes]
        blocks.append(
            {
                "type": "paragraph",
                "text": " ".join(words["text"][i] for i in indexes),
                "page": 1,
                "bbox": [min(x for x, _, _, _ in boxes), min(y for _, y, _, _ in boxes),
                         max(x + w for x, _, w, _ in boxes), max(y + h for _, y, _, h in boxes)],
            }
        )  # fmt: skip
    pages = [{"page": 1, "width": image.width, "height": image.height}]
    return build_ir(blocks, pages, source_name=filename)


app = create_app(
    name="tesseract-shim",
    version=lambda: f"0.1.0+tesseract.{pytesseract.get_tesseract_version()}",  # RAGCentre pins this
    mime_types=["image/png", "image/jpeg", "image/tiff", "image/bmp", "image/webp"],
    capabilities={"page_images": False},
    api_key_env="TESSERACT_SHIM_API_KEY",
    parse=parse,
    options=OPTIONS,
    serialise=True,  # one document at a time
    jobs=True,  # parse in the background, so a slow scan is no problem
)
```

Run it and check it:

```sh
uvicorn tesseract_shim:app --port 8710
ragcentre parser check http://127.0.0.1:8710 --file scan.png
```

What `create_app` gives you:

| You write | shimkit provides |
|---|---|
| `parse(content, filename, options) -> IR dict` | `GET /info`, `POST /parse`, multipart handling, error responses |
| `OPTIONS = [...]` | validation with defaults, `400 invalid_options` for typos and wrong types, and `/info.options_schema` (RAGCentre validates pipelines against it and draws a form for it) |
| `api_key_env="..."` | bearer-token auth on everything except `GET /info` |
| `jobs=True` | async jobs (`POST /parse?mode=async`, `/jobs/{id}`) so a slow document never hits a request timeout |
| `serialise=True` | one `parse` call at a time, for engines that are not thread-safe |
| `raise ShimError(status, code, message)` | a protocol error body; any other exception becomes `422 conversion_failed` |

You do not have to use shimkit. A service in any language that answers the two endpoints in
[`parser-v1.md`](parser-v1.md) is a shim.

## 2. What RAGCentre does with the IR

Return as much as your engine knows; these are the features that use it today.

| RAGCentre feature | What your IR needs |
|---|---|
| Chunk context and `heading_path`, the `structure` chunker (never merges across sections) | `heading` blocks with a **correct `level`**. All headings at one level flattens the document |
| The `page` chunker, `page_start` / `page_end` on chunks, evaluation targets that name pages | `page` on every block (1-based). `metadata.page_end` when a block runs over a page break |
| Table-aware splitting that repeats the header row | `table` blocks with a **Markdown pipe table** in `text`. `html` is optional |
| Code kept whole | `code` blocks |
| Header and footer removal | `page_header` / `page_footer` blocks, instead of plain paragraphs |
| The Parsed tab in the UI | `type`, `level`, `page` |
| Not used yet | `bbox`, `html`, `pages[].image`, other `metadata` |

If your engine cannot produce one of these, say nothing: `ragcentre parser check` will tell the user
which feature they lose, and everything else still works. A parser that returns only `paragraph`
blocks with page numbers is a perfectly good shim.

### Mapping advice

- **Reading order.** `blocks` must be in reading order. If your engine returns words or lines with
  coordinates, group them into paragraphs first (the example uses Tesseract's block and paragraph
  numbers) and sort columns left to right.
- **Headings.** If the engine has no heading label, a size or style heuristic is better than none,
  but give a level only if you are fairly sure: a wrong nesting is worse than a flat one.
- **Tables.** Convert cells to Markdown (`shimkit.table_to_markdown` takes HTML). If you only have
  text, return a `paragraph` rather than a `table` without pipes.
- **Boxes.** `bbox` is `[x0, y0, x1, y1]`, origin top-left, in the units of `pages[].width/height`.
  Many engines use a bottom-left origin (PDF); convert.
- **Unknown labels.** Return the engine's label as the block `type`; RAGCentre keeps it as
  `metadata.original_type` and treats the block as a paragraph.
- **Empty blocks.** `build_ir` drops blocks with blank text (figures excepted) and numbers the rest.

## 3. Options

Declare every route option the engine can use, with a default and a one-line description:

```python
OPTIONS = [
    boolean("ocr", True, "Run OCR on scanned pages"),
    choice("table_mode", ["accurate", "fast"], "accurate", "Table-structure model"),
    integer("dpi", 192, minimum=72, maximum=400, description="Render resolution"),
    strings("lang", "OCR languages"),
    page_range("page_range"),          # [first, last], 1-based
    boolean("tables", None, "..."),    # None: not set, the engine's own default applies
]
```

Users set them in the pipeline's parse route (`options: {dpi: 300}`). RAGCentre checks a saved
pipeline against your schema, so a typo such as `{"dpi_": 300}` fails when the pipeline is saved,
not when the 400th document is parsed. Options are part of the parse cache key: changing one
re-parses; the same options reuse the cache.

Keep secrets, model choices that must not vary per request, and server addresses in **environment
variables**, not options.

## 4. The version

`/info.version` is pinned by RAGCentre when the provider is created. If the service later reports a
different version, parsing is refused (`provider_version_drift`) until the user makes a new provider.
That is what keeps two versions of an engine from being mixed in one index. So:

- Put **everything that changes output** in the version: your shim's own version, the engine's
  version, and the model or language pack. `0.1.0+tesseract.5.3.0` is a good shape.
- Bump your own version whenever you change the mapping.
- If the engine sits behind a server that reports no version, end the version in `.remote` and tell
  users to set the provider's `revision` whenever they change the server.

## 5. Slow engines

OCR on a CPU can take minutes per page. Pass `jobs=True` and RAGCentre submits each document as a
job and polls it, with no limit other than the provider's `job_timeout_seconds` (six hours by
default). The default sync call is limited to the provider's `timeout_seconds` (ten minutes). Jobs live in
memory: a restart loses them, and RAGCentre simply submits again.

## 6. Errors

Use the status code to say whether a retry can help:

| Situation | Respond |
|---|---|
| The file is not a type you read | `415` with `{"error": {"code": "unsupported_media_type", "message": "..."}}` |
| The file is broken, encrypted or empty | `422` |
| The options are wrong | `400 invalid_options` (shimkit does this for you) |
| The engine is starting up or overloaded | `503`, optionally with `Retry-After` |
| You crashed | any `5xx`: RAGCentre retries with backoff |

A `4xx` is permanent: that document's parse fails and is not retried. A `5xx` is retried. Never
answer `500` for a file you can never parse.

## 7. A server you already run

If the engine already has a server (docling-serve, a MinerU API server, PaddleX serving, a Marker
server), do not load the engine in the shim: make your `parse` post the file to that server and map
the response. The four example shims do this when an environment variable such as
`DOCLING_SERVE_URL` is set. Copy that pattern: one `convert.py` that maps the engine's data to the
IR, and a small `engine.py` for each way of obtaining the data (in-process, remote). Test the
mapping with a captured response from a real server and no network.

## 8. Register and check it

```sh
ragcentre provider add -f examples/providers/docling-parser.yaml     # a template to copy
ragcentre provider set-secret my-shim api_key --env MY_SHIM_API_KEY   # if you set an API key
ragcentre provider test my-shim --deep                                # parses a sample document
ragcentre parser check http://127.0.0.1:8710                          # works without RAGCentre running
```

`parser check` posts a built-in two-page PDF (a title, headings, a paragraph, bullets, a ruled
table, a figure caption, a repeated header and footer), or your own file with `--file`, and tells you:

- **FAIL**: RAGCentre cannot use the service as it is. Wrong protocol string, `/info` behind
  authentication (RAGCentre reads it without credentials when it creates the provider), an invalid
  IR, or a failed job.
- **warn**: it works, but a feature is lost, and the warning names it ("all 4 headings are level 2:
  loses nested heading paths and the structure chunker"). It also warns when no block has a page,
  when tables have no pipes, when a header repeats on every page without being labelled, when a box
  lies outside its page, when the sample comes back out of reading order, and when a file the
  service cannot read gets a `5xx`.

Before you publish a shim, run it against the sample and against two or three of your own documents
(a scan, a table-heavy page, a multi-column page), and read the Parsed tab in the RAGCentre UI.

## Checklist

- [ ] `/info` answers without credentials and reports the engine, model and your version
- [ ] Blocks are in reading order, with `page` and (if you can) heading `level`
- [ ] Tables are Markdown pipe tables
- [ ] Options are declared, and typos are a `400`
- [ ] Unreadable files get a `4xx` with an error body
- [ ] Slow engines use `jobs=True`
- [ ] `ragcentre parser check` shows no `FAIL` and you understand each `warn`
