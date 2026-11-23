# PaddleOCR parser shim

A small standalone service that lets RAGCentre parse PDFs and images with
[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) 3.x, using its PP-StructureV3 document
pipeline (layout, OCR, tables, formulas). It speaks the parser protocol
(`docs/protocols/parser-v1.md`) and returns RAGCentre's Document IR.

It is an example, not part of the `ragcentre` package: PaddleOCR and PaddlePaddle have their own
dependencies and download models on first use, so the shim lives in its own environment. It uses
the shared helpers in `examples/shimkit/`.

## Run it

Python 3.11 to 3.13 (PaddlePaddle publishes no wheels for newer versions yet).

```sh
cd examples/paddleocr_shim
uv sync
uv pip install paddlepaddle      # CPU build; see below for GPUs
uv run uvicorn paddleocr_shim.app:app --host 127.0.0.1 --port 8703
```

**PaddlePaddle is not installed by `uv sync`**: pick the build for your machine. `paddlepaddle` is
the CPU build (it ran on macOS arm64). For an NVIDIA GPU install `paddlepaddle-gpu` from the index
that matches your CUDA version (see the
[PaddlePaddle install guide](https://www.paddlepaddle.org.cn/en/install/quick)).

The first parse downloads twelve models (about 1.7 GB) into `~/.paddlex`, so it is slow. Set
`PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True` to skip PaddleX's connectivity check at start-up.

### With Docker

From the repository root, this starts RAGCentre and this service together:

```sh
docker compose --profile paddleocr up --build     # the service on http://127.0.0.1:8703
```

RAGCentre runs in the same Compose network and reaches the service by name, so register the
provider with `base_url: http://paddleocr:8703` (change that line in
`examples/providers/paddleocr-parser.yaml`, or add the provider in the web UI). Models are downloaded on
the first parse into the `paddleocr-models` volume (`~/.paddlex` in the container), so they survive a
rebuild. The image installs the CPU build of PaddlePaddle (`--build-arg PADDLE_PACKAGE=…` chooses another
package). Set `PADDLEOCR_SHIM_LANG` in the environment of `docker compose` to choose the language.

To build the image alone, use `examples/` as the build context:

```sh
docker build -t paddleocr-shim -f examples/paddleocr_shim/Dockerfile examples
```

Then register it with RAGCentre and route files to it (from the repository root):

```sh
uv run ragcentre provider add -f examples/providers/paddleocr-parser.yaml
```

```yaml
# in a pipeline's parse node
routes:
  - {mime: "text/*", provider: builtin-text}
  - {mime: "application/pdf", provider: paddleocr, options: {}}
  - {mime: "image/*", provider: paddleocr, options: {}}
```

## Options

The parse route's `options` are passed to the service. A typo in an option name is a `400`, not ignored.

| Option | Default | Meaning |
|---|---|---|
| `tables` | `true` | Recognise table structure (without it a table's text is plain text, with no HTML) |
| `formulas` | `true` | Recognise formulas as LaTeX |
| `charts` | `false` | Recognise charts (loads a vision-language model) |
| `seals` | `false` | Recognise seals and stamps |
| `orientation` | `false` | Detect and correct page orientation |
| `unwarp` | `false` | Flatten curved or photographed pages |
| `textline_orientation` | engine's | Correct upside-down or rotated text lines |
| `region_detection` | engine's | Detect layout regions before working out reading order |
| `heading_levels` | `false` | Rank section titles with PaddleX's own heading-level assignment. Off, every section title is level 2 and the document title (if PaddleOCR finds one) is level 1 |
| `pipeline` | `structure_v3` | `structure_v3` (PP-StructureV3) or `vl` (PaddleOCR-VL, see below) |
| `lang` | `PADDLEOCR_SHIM_LANG` | Language of the text models for this route, e.g. `en`, `ch`, `japan`, `korean` (PP-StructureV3 only) |
| `text_det_limit_side_len` | engine's | Longest-side limit in pixels for text detection input (PP-StructureV3 only); raise it for dense pages |
| `list_heuristic` | `false` | PaddleOCR has no list label, so a bullet comes out as a paragraph. With this on, a paragraph whose every line starts with a bullet (`•`, `-`, `*`...) or a number (`1.`, `2)`, `(3)`, `a)`) becomes one `list_item` per line. It is a guess; leave it off if your documents have numbered headings or paragraphs |

PaddleX loads models for a feature when a pipeline is *built*, so the shim builds and keeps one
pipeline per combination of the feature switches, `lang` and `text_det_limit_side_len`; a request can only use features its pipeline
was built with. Using many different combinations holds many pipelines in memory.

The shim serialises parses (one at a time) and the provider file sets `max_concurrency: 1`.

### Language

Set `PADDLEOCR_SHIM_LANG` (for example `en`, `ch`, `fr`) before starting the service to choose
PaddleOCR's text models. It changes the output, so it is part of the version below.

## Authentication

Set `PADDLEOCR_SHIM_API_KEY` to require `Authorization: Bearer <key>` on `/parse`, and give
RAGCentre the same value as the provider's `api_key` secret:

```sh
uv run ragcentre provider set-secret paddleocr api_key --env RAGCENTRE_PROVIDER_PADDLEOCR_SHIM_API_KEY
```

`GET /info` stays open: RAGCentre reads it without credentials when it creates the provider.

## Versioning

`/info` reports `0.1.0+paddleocr.<paddleocr version>`, with `.lang-<lang>` added when
`PADDLEOCR_SHIM_LANG` is set. RAGCentre records that version when the provider is created and
refuses to parse with a service reporting a different one (unless the provider's
`on_version_drift` is `warn`). Upgrading PaddleOCR or changing the language therefore means creating
a new provider and a new pipeline.

## How PaddleOCR output maps to the IR

See the table at the top of `paddleocr_shim/convert.py`. In short: `doc_title` becomes a level-1
heading and the other title labels become level-2 headings, text-like labels become paragraphs,
tables become Markdown with the HTML alongside, images, charts and seals become `figure` blocks,
`table_title`/`figure_title`/`chart_title` become `caption`, formulas keep their LaTeX, algorithms
become `code`, and headers, footers and page numbers become `page_header` and `page_footer`.
Header and footer images and formula numbers are dropped.

- **Positions** are PaddleOCR's pixels: a PDF page is rendered at 2x, so a US-letter page is
  1224 x 1584. `pages[].width/height` use the same unit.
- **Lists.** PaddleOCR has no list label, so a bulleted line comes out as a paragraph.
- **Line joins.** PaddleOCR joins the lines of a text block without a separator, so two adjacent
  lines can read "surface.Close the valve".
- **Heading levels.** PP-StructureV3 does not assign them (PaddleOCR-VL does). The opt-in
  `heading_levels` option applies PaddleX's assignment, which clusters titles by pixel height and
  numbering; on short documents the levels can look arbitrary.

## Licence

PaddleOCR is Apache 2.0. Check the licence of each model it downloads before commercial use.

## PaddleOCR-VL

`pipeline: "vl"` runs PaddleOCR-VL, PaddleOCR's vision-language document parser. It reads tables and
formulas itself (so `tables` and `formulas` do not apply), keeps reading order, and gives section
titles their levels (`heading_levels` is always on). The pages come back merged across page breaks
(`restructure_pages`). Unlike PP-StructureV3, it needs a GPU or a recognition server to be fast. The
model is configured by environment, not per request, because it decides every document's output:

| Variable | Meaning |
|---|---|
| `PADDLEOCR_VL_VERSION` | `v1`, `v1.5` or `v1.6` (PaddleOCR's default) |
| `PADDLEOCR_VL_BACKEND` | `native` (run here), or `vllm-server`, `sglang-server`, `fastdeploy-server`, `mlx-vlm-server`, `llama-cpp-server` |
| `PADDLEOCR_VL_SERVER_URL` | URL of the server, for the `*-server` backends |
| `PADDLEOCR_VL_API_MODEL`, `PADDLEOCR_VL_API_KEY` | model name and key that server expects |

The version and backend are part of `/info.version`, so changing them needs a new provider. This is
a thin wrapper over PaddleOCR's own API; it is tested against a stand-in, not a live model.

## Using a PaddleX server you already run

Set `PADDLEX_SERVING_URL` and the shim calls `POST /layout-parsing` on a running PaddleX serving
instance (`paddlex --serve --pipeline PP-StructureV3 --port 8080`) instead of loading PaddleOCR. It
then needs neither PaddleOCR nor PaddlePaddle:

```sh
uv sync                                  # without the `local` extra
PADDLEX_SERVING_URL=http://localhost:8080 uv run uvicorn paddleocr_shim.app:app --port 8703
docker build --build-arg ENGINE=remote -f examples/paddleocr_shim/Dockerfile examples   # slim image
```

(`uv sync --extra local` installs PaddleOCR for the in-process mode.) Things to know:

- The server's pipeline was built with its own features. A request can switch a feature off (the
  options above are sent as `useTableRecognition` and so on) but cannot switch on one the server
  loaded without.
- The server's JSON has no heading levels, so `heading_levels` has no effect and every section title
  is level 2.
- PaddleX serving reports no version, so `/info` says `<shim>+paddlex-serving.remote`. Set the
  provider's `revision` whenever you change the server's pipeline or models, so RAGCentre does not
  mix the outputs. `PADDLEX_SERVING_API_KEY` is sent as a bearer token for a reverse proxy in front
  of it; `PADDLEX_SERVING_TIMEOUT` (seconds, default 3600) limits one document.

## Long documents

The service reports `capabilities.async`: RAGCentre submits each document as a background job and
polls it, so a long scan does not hit the provider's `timeout_seconds` (see
`docs/protocols/parser-v1.md`).

## Tests

The conversion works on plain dicts and the engine tests use a stand-in for `paddleocr`, so the
tests need only `fastapi`, `httpx` and `pytest`, not PaddleOCR or its models:

```sh
uv run --isolated --no-project --with ../shimkit --with fastapi --with python-multipart --with httpx --with pytest pytest
```

`tests/test_live.py` parses a small generated PDF through the real engine. It is skipped unless
`SHIM_LIVE=1` and PaddleOCR is installed:

```sh
SHIM_LIVE=1 uv run pytest tests/test_live.py -s
```

`tests/golden/sample_ir.json` is the IR for a sample document; RAGCentre's own suite validates it
(`tests/test_parser_shim_goldens.py`). Regenerate it with `UPDATE_GOLDEN=1 uv run pytest`.
