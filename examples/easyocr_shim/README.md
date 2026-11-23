# EasyOCR parser shim

A small standalone service that lets RAGCentre read scanned PDFs and images with
[EasyOCR](https://github.com/JaidedAI/EasyOCR) (80+ languages, PyTorch). It speaks the parser
protocol (`docs/protocols/parser-v1.md`) and returns RAGCentre's Document IR.

It is an example, not part of the `ragcentre` package: EasyOCR brings PyTorch and downloads models on
first use, so the shim lives in its own environment. It uses the shared helpers in
`examples/shimkit/`.

**What to expect.** EasyOCR finds and reads lines of text. It does no layout analysis: there are no
tables, figures, lists or headings in what it returns. The shim groups the lines into paragraphs by
their position and, with the `headings` option, guesses headings from the text height (see
[Layout](#layout)). Use it for scans and photos where plain text is what you need, or for scripts the
layout-aware shims read less well. For structured documents, Docling, MinerU, PaddleOCR (PP-StructureV3)
or Surya keep more of the structure.

## Run it

```sh
cd examples/easyocr_shim
uv venv && uv pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision   # CPU-only (optional)
uv sync
uv run uvicorn easyocr_shim.app:app --host 127.0.0.1 --port 8705
```

The first line installs PyTorch's CPU build. Skip it on a machine with an NVIDIA GPU, or on macOS
(where the default build is the CPU/MPS one anyway). EasyOCR uses CUDA or Apple's MPS when PyTorch has
them; set `EASYOCR_SHIM_GPU=0` to force the CPU.

The first parse downloads the detection model and the recognition model for the languages
(about 100 MB each) into `~/.EasyOCR`, so it is slow.

### With Docker

From the repository root, this starts RAGCentre and this service together:

```sh
docker compose --profile easyocr up --build     # the service on http://127.0.0.1:8705
```

RAGCentre reaches it by name in the Compose network: register the provider with
`base_url: http://easyocr:8705`. Models go into the `easyocr-models` volume. The image installs
PyTorch's CPU build; `--build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu128` picks a CUDA one.

Then register it with RAGCentre and route files to it (from the repository root):

```sh
uv run ragcentre provider add -f examples/providers/easyocr-parser.yaml
```

```yaml
# in a pipeline's parse node
routes:
  - {mime: "text/*", provider: builtin-text}
  - {mime: "application/pdf", provider: easyocr, options: {headings: true}}
  - {mime: "image/*", provider: easyocr, options: {languages: ["en", "de"]}}
```

## Options

The parse route's `options` are passed to the service. A typo in an option name is a `400`, not ignored.

| Option | Default | Meaning |
|---|---|---|
| `languages` | `EASYOCR_SHIM_LANGS`, else `["en"]` | EasyOCR language codes. Each list loads its own recognition model (kept in memory). Some scripts can only be combined with English (`["ch_sim", "en"]`); an impossible combination or an unknown code is a `400` |
| `dpi` | `200` | Render resolution for PDF pages (72 to 400) |
| `page_range` | every page | `[first, last]`, 1-based and inclusive. For PDFs and multi-page TIFFs. Page numbers stay the original's |
| `paragraphs` | `true` | Group lines into paragraphs. Off, every line is its own block |
| `headings` | `false` | Guess headings from the text height |
| `min_confidence` | `0` | Drop text EasyOCR read with a confidence below this, in percent |
| `decoder` | `greedy` | `greedy`, `beamsearch` or `wordbeamsearch` (slower, sometimes more accurate) |
| `rotated_text` | `false` | Also try each text box turned by 90, 180 and 270 degrees (slower) |
| `canvas_size` | `2560` | Longest side, in pixels, that text detection works at; a larger page is scaled down. Raise it with `dpi` for small print |
| `include_page_images` | `false` | Return each page image as a base64 PNG in `pages[].image` |

## Layout

EasyOCR returns pieces of text with a box and a confidence. `shimkit.layout` turns them into blocks:

1. Pieces on the same text line that are close together are joined (EasyOCR splits a line at a wide
   gap). Pieces far apart, like the columns of a table, stay separate lines.
2. A line joins the paragraph above it when it starts close below it, overlaps it horizontally and has
   about the same height. Paragraphs are ordered top to bottom, then left to right. That is right for a
   single column; on a page with columns side by side, the paragraphs of the two columns interleave.
3. With `headings`, a short paragraph (one or two lines, at most 15 words, no closing full stop or
   comma) whose text is at least 1.25 times as tall as the document's median line is a heading. The
   tallest size is level 1, the next level 2, and so on (at most 4). It is a guess: a large caption or
   a pull quote can become a heading. That is why it is off by default.

Every block is a `paragraph` or a `heading`, has a `page` and a `bbox` (the page image's pixels, top-left
origin, the same unit as `pages[].width/height`), and has `metadata.confidence`, the mean of its lines'.

## Authentication

Set `EASYOCR_SHIM_API_KEY` to require `Authorization: Bearer <key>` on `/parse`, and give RAGCentre the
same value as the provider's `api_key` secret:

```sh
uv run ragcentre provider set-secret easyocr api_key --env RAGCENTRE_PROVIDER_EASYOCR_SHIM_API_KEY
```

`GET /info` stays open: RAGCentre reads it without credentials when it creates the provider.

## Versioning

`/info` reports `0.1.0+easyocr.<easyocr version>`, with `.lang-<a>+<b>` added when
`EASYOCR_SHIM_LANGS` is set. RAGCentre records that version when the provider is created and refuses to
parse with a service reporting a different one (unless the provider's `on_version_drift` is `warn`).

## Long documents

The service reports `capabilities.async`: RAGCentre submits each document as a background job and polls
it, so a long scan does not hit the provider's `timeout_seconds`. Parses run one at a time. Pages are
rendered and released one at a time, so a long document does not keep every page image in memory.
With `include_page_images`, the encoded images are retained in the response.

## Tests

`tests/test_shim.py` uses a stand-in engine and needs only `fastapi`, `httpx` and `pytest`.
`tests/test_engine.py` renders real pages and uses a stand-in for EasyOCR, so it also needs Pillow,
pypdfium2 and numpy (it is skipped without them):

```sh
uv run --isolated --no-project --with ../shimkit --with fastapi --with python-multipart --with httpx --with pytest \
  --with pillow --with pypdfium2 --with numpy pytest
```

`tests/test_live.py` reads a small generated PDF through the real EasyOCR. It is skipped unless
`SHIM_LIVE=1` and EasyOCR is installed:

```sh
SHIM_LIVE=1 uv run pytest tests/test_live.py -s
```

`tests/golden/sample_ir.json` is the IR for a sample document (with `headings` on); RAGCentre's own
suite validates it (`tests/test_parser_shim_goldens.py`). Regenerate it with `UPDATE_GOLDEN=1 uv run pytest`.

## Licence

EasyOCR is Apache 2.0.
