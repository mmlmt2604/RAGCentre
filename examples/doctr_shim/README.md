# docTR parser shim

A small standalone service that lets RAGCentre read scanned PDFs and images with Mindee's
[docTR](https://github.com/mindee/doctr) (PyTorch). It speaks the parser protocol
(`docs/protocols/parser-v1.md`) and returns RAGCentre's Document IR.

It is an example, not part of the `ragcentre` package: docTR brings PyTorch and downloads models on
first use, so the shim lives in its own environment. It uses the shared helpers in
`examples/shimkit/`.

**What to expect.** docTR detects and reads words and lines, accurately and with good boxes, but it
does no layout analysis of tables, figures or headings. The shim groups the lines into paragraphs by
position and, with the `headings` option, guesses headings from the text height (see
[Layout](#layout)). It reads Latin-script text best (its default recognition model's vocabulary is
mostly European languages). For structured documents, Docling, MinerU, PaddleOCR (PP-StructureV3) or
Surya keep more of the structure.

## Run it

Python 3.11 or newer (docTR 1.x).

```sh
cd examples/doctr_shim
uv venv && uv pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision   # CPU-only (optional)
uv sync
uv run uvicorn doctr_shim.app:app --host 127.0.0.1 --port 8706
```

The first line installs PyTorch's CPU build. Skip it on a machine with an NVIDIA GPU, or on macOS.
`DOCTR_SHIM_DEVICE` chooses where the models run: `auto` (default: CUDA when PyTorch has it, else the
CPU), `cpu`, `cuda`, `cuda:1` or `mps`. `auto` never picks `mps`, so on Apple silicon it runs on the CPU.

The first parse with a model pair downloads it (about 100 to 200 MB) into `~/.cache/doctr`.

### With Docker

From the repository root, this starts RAGCentre and this service together:

```sh
docker compose --profile doctr up --build     # the service on http://127.0.0.1:8706
```

RAGCentre reaches it by name in the Compose network: register the provider with
`base_url: http://doctr:8706`. Models go into the `doctr-models` volume. The image installs PyTorch's
CPU build; `--build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu128` picks a CUDA one. Compose
passes `DOCTR_SHIM_API_KEY` and `DOCTR_SHIM_DEVICE` on from your environment or `.env`.

Then register it with RAGCentre and route files to it (from the repository root):

```sh
uv run ragcentre provider add -f examples/providers/doctr-parser.yaml
```

```yaml
# in a pipeline's parse node
routes:
  - {mime: "text/*", provider: builtin-text}
  - {mime: "application/pdf", provider: doctr, options: {headings: true}}
  - {mime: "image/*", provider: doctr, options: {straight_pages: false}}
```

## Options

The parse route's `options` are passed to the service. A typo in an option name is a `400`, not ignored.

| Option | Default | Meaning |
|---|---|---|
| `detection` | `fast_base` | Text-detection model: `fast_base`, `fast_small`, `fast_tiny`, `db_resnet50`, `db_mobilenet_v3_large`, `linknet_resnet18`, `linknet_resnet34`, `linknet_resnet50` |
| `recognition` | `crnn_vgg16_bn` | Text-recognition model: `crnn_vgg16_bn`, `crnn_mobilenet_v3_small`, `crnn_mobilenet_v3_large`, `parseq`, `master`, `sar_resnet31`, `vitstr_small`, `vitstr_base` |
| `straight_pages` | `true` | Assume text lines are horizontal (faster). Turn it off for rotated, skewed or photographed pages: boxes are then the upright rectangles around the rotated lines |
| `dpi` | `144` | Render resolution for PDF pages (72 to 400; docTR's own PDF loader uses 144) |
| `page_range` | every page | `[first, last]`, 1-based and inclusive. For PDFs and multi-page TIFFs. Page numbers stay the original's |
| `paragraphs` | `true` | Group lines into paragraphs. Off, every line is its own block |
| `headings` | `false` | Guess headings from the text height. The shared layout keeps a heading out of the paragraph next to it, so a taller title stays its own paragraph and is found (on the test report, 3 headings with `paragraphs` on and off) |
| `min_confidence` | `0` | Drop lines docTR read with a mean word confidence below this, in percent |
| `include_page_images` | `false` | Return each page image as a base64 PNG in `pages[].image` |

The shim builds one predictor per combination of `detection`, `recognition` and `straight_pages` and
keeps it; using many combinations holds many models in memory.

## Layout

The shim asks docTR for lines (`resolve_lines`) but not blocks (`resolve_blocks` off) and lays them out
with `shimkit.layout`, the same way as the EasyOCR shim:

1. A line joins the paragraph above it when it starts close below it, overlaps it horizontally and has
   about the same height. Paragraphs are ordered top to bottom, then left to right. That is right for a
   single column; on a page with columns side by side, the paragraphs of the two columns interleave.
2. With `headings`, a short paragraph (one or two lines, at most 15 words, no closing full stop or
   comma) whose text is at least 1.25 times as tall as the document's median line is a heading. The
   tallest size is level 1, the next level 2, and so on (at most 4). It is a guess, so it is off by
   default.

Every block is a `paragraph` or a `heading`, has a `page` and a `bbox` (the page image's pixels, top-left
origin, the same unit as `pages[].width/height`), and has `metadata.confidence`, the mean of its words'.

## Authentication

Set `DOCTR_SHIM_API_KEY` to require `Authorization: Bearer <key>` on `/parse`, and give RAGCentre the
same value as the provider's `api_key` secret:

```sh
uv run ragcentre provider set-secret doctr api_key --env RAGCENTRE_PROVIDER_DOCTR_SHIM_API_KEY
```

`GET /info` stays open: RAGCentre reads it without credentials when it creates the provider.

## Versioning

`/info` reports `0.1.0+doctr.<python-doctr version>`. RAGCentre records that version when the provider
is created and refuses to parse with a service reporting a different one (unless the provider's
`on_version_drift` is `warn`). The models are route options, so changing them changes the pipeline,
not the provider.

## Long documents

The service reports `capabilities.async`: RAGCentre submits each document as a background job and polls
it, so a long scan does not hit the provider's `timeout_seconds`. Parses run one at a time. Pages are
rendered and released one at a time, so a long document does not keep every page image in memory.
With `include_page_images`, the encoded images are retained in the response.

## Tests

`tests/test_shim.py` uses a stand-in engine and needs only `fastapi`, `httpx` and `pytest`.
`tests/test_engine.py` renders real pages and uses a stand-in for docTR, so it also needs Pillow,
pypdfium2 and numpy (it is skipped without them):

```sh
uv run --isolated --no-project --with ../shimkit --with fastapi --with python-multipart --with httpx --with pytest \
  --with pillow --with pypdfium2 --with numpy pytest
```

`tests/test_live.py` reads a small generated PDF through the real docTR. It is skipped unless
`SHIM_LIVE=1` and docTR is installed:

```sh
SHIM_LIVE=1 uv run pytest tests/test_live.py -s
```

`tests/golden/sample_ir.json` is the IR for a sample document (with `headings` on); RAGCentre's own
suite validates it (`tests/test_parser_shim_goldens.py`). Regenerate it with `UPDATE_GOLDEN=1 uv run pytest`.

## Licence

docTR is Apache 2.0, and so are its pretrained models.
