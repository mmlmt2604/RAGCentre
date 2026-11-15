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

The parse route's `options` are validated by the service and reported by `/info` as `options_schema`;
RAGCentre checks a pipeline's route against it when the pipeline is created. An unknown key or a
value of the wrong type is `400 invalid_options` naming the option.

| Option | Default | Meaning |
|---|---|---|
| `tables` | `true` | Recognise table structure (without it a table's text is plain text, with no HTML). `use_table_recognition` |
| `formulas` | `true` | Recognise formulas as LaTeX. `use_formula_recognition` |
| `charts` | `false` | Recognise charts (loads a vision-language model). `use_chart_recognition` |
| `seals` | `false` | Recognise seals and stamps. `use_seal_recognition` |
| `orientation` | `false` | Detect and correct page orientation. `use_doc_orientation_classify` |
| `unwarp` | `false` | Flatten curved or photographed pages. `use_doc_unwarping` |
| `heading_levels` | `false` | Rank section titles with PaddleX's own heading-level assignment. Off, every section title is level 2 and the document title (if PaddleOCR finds one) is level 1 |
| `languages` | the service's | One PaddleOCR language code (`en`, `ch`, `fr`, `japan`, ...), as `lang`. Replaces `PADDLEOCR_SHIM_LANG` for this route |
| `ocr_version` | PaddleOCR's | `PP-OCRv3`, `PP-OCRv4` or `PP-OCRv5` (PP-StructureV3 does not take v6) |
| `textline_orientation` | PP-StructureV3's | `use_textline_orientation`: classify each text line's orientation |
| `region_detection` | PP-StructureV3's | `use_region_detection` |
| `page_range` | whole file | `[first, last]`, 1-based and inclusive. IR page numbers are the file's, not positions in the range, and `pages` lists only the pages parsed. A range that reaches past the end of a PDF is `400 invalid_options`; for an image only a range that includes page 1 is accepted |
| `engine` | `{}` | Other `PPStructureV3` arguments, see below |

PP-StructureV3 has no page selection, so for a PDF the shim cuts the chosen pages into a temporary
PDF with `pypdfium2` (a dependency of PaddleX, so nothing extra to install) and passes that to
`predict`. `page_range` is not part of the pipeline cache key: the same pipeline serves every range.

### The `engine` passthrough

`engine` is an object of other `PPStructureV3` arguments, so a new PaddleOCR parameter is usable the
day it ships. Each key goes to the constructor, to `predict`, or to both, by which signature accepts
it (`engine.py` lists them: the layout and text thresholds such as `layout_threshold` and
`text_det_thresh`, the `*_model_name` arguments, the table switches such as
`use_ocr_results_with_table_cells`, `format_block_content`, `markdown_ignore_labels`). A key must be one
of those: an unknown one is `400 invalid_options`.

- An argument that has a named option (`use_table_recognition`, `lang`, ...) is refused in `engine`:
  use the option.
- Refused at any depth, as operational: `*_model_dir` (so a model is picked by name, never by path),
  `*_batch_size`, `url`, `headers`, `api_key`, `params`, `kwargs`, `input`, and the other keys listed
  in `shimkit.DENIED_ENGINE_KEYS`.
- Options are stored in the pipeline and hashed into its fingerprint; keep paths, URLs and keys out.

PaddleX loads models for a feature when a pipeline is *built*, so the shim builds and keeps a pipeline
per distinct set of constructor arguments (the four most recently used, since each holds models in
memory); a request can only use features its pipeline was built with.

The shim serialises parses (one at a time) and the provider file sets `max_concurrency: 1`.

### Language

Set `PADDLEOCR_SHIM_LANG` (for example `en`, `ch`, `fr`) before starting the service to choose
PaddleOCR's text models. It changes the output, so it is part of the version below. The option
`languages` replaces it for one route; the version names only this environment default.

## Authentication

Set `PADDLEOCR_SHIM_API_KEY` to require `Authorization: Bearer <key>` on `/parse`, and give
RAGCentre the same value as the provider's `api_key` secret:

```sh
uv run ragcentre provider set-secret paddleocr api_key --env PADDLEOCR_SHIM_API_KEY
```

`GET /info` stays open: RAGCentre reads it without credentials when it creates the provider.

## Versioning

`/info` reports `0.4.0+paddleocr.<paddleocr version>`, with `.lang-<lang>` added when
`PADDLEOCR_SHIM_LANG` is set. RAGCentre records that version when the provider is created and
refuses to parse with a service reporting a different one (unless the provider's
`on_version_drift` is `warn`). Upgrading PaddleOCR or changing the language therefore means creating
a new provider and a new pipeline. The same goes for 0.4.0, which added `page_range` to the options
schema: a provider created from 0.3.0 keeps the old schema, so re-create it to use the option.

## How PaddleOCR output maps to the IR

PaddleOCR's result carries no per-block score, so the IR (schema 1.1) has no `confidence` for its blocks.

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

## Tests

The conversion works on plain dicts and the engine tests use a stand-in for `paddleocr`, so the
tests need only `fastapi`, `httpx` and `pytest`, not PaddleOCR or its models:

```sh
uv run --isolated --no-project --with ../shimkit --with fastapi --with python-multipart --with httpx --with pytest pytest
```

`tests/test_live.py` parses a small generated PDF through the real engine, and a three-page PDF with
`page_range: [2, 3]`. It is skipped unless
`SHIM_LIVE=1` and PaddleOCR is installed:

```sh
SHIM_LIVE=1 uv run pytest tests/test_live.py -s
```

The same file checks that every argument of `PPStructureV3.__init__` and `predict` is accounted for in
`paddleocr_shim/engine.py` (a named option, allowed through `engine`, or refused on purpose), so a
PaddleOCR upgrade that adds one fails it until someone decides where it belongs.

`tests/golden/sample_ir.json` is the IR for a sample document; RAGCentre's own suite validates it
(`tests/test_parser_shim_goldens.py`). Regenerate it with `UPDATE_GOLDEN=1 uv run pytest`.
