# Docling parser shim

A small standalone service that lets RAGCentre parse PDFs, Office documents, HTML and images with
[Docling](https://github.com/docling-project/docling). It speaks the parser protocol
(`docs/protocols/parser-v1.md`) and returns RAGCentre's Document IR.

It is an example, not part of the `ragcentre` package: it has its own dependencies (Docling pulls
in PyTorch and downloads its layout and OCR models on first use), so it lives in its own
environment.

## Run it

```sh
cd examples/docling_shim
uv sync
uv run uvicorn docling_shim.app:app --host 127.0.0.1 --port 8701
```

The first conversion is slow while Docling fetches its models.

### With Docker

From the repository root, this starts RAGCentre and this service together:

```sh
docker compose --profile docling up --build     # the service on http://127.0.0.1:8701
```

RAGCentre runs in the same Compose network and reaches the service by name, so register the
provider with `base_url: http://docling:8701` (change that line in
`examples/providers/docling-parser.yaml`, or add the provider in the web UI). Models are downloaded on
the first parse into the `docling-models` volume (`~/.cache` in the container), so they survive a
rebuild. The image installs the CPU build of PyTorch; pass `--build-arg TORCH_BACKEND=cu128` (or the
backend for your CUDA version) to build for an NVIDIA GPU.

To build the image alone, use `examples/` as the build context:

```sh
docker build -t docling-shim -f examples/docling_shim/Dockerfile examples
```

Then register it with RAGCentre and route non-text files to it (from the repository root):

```sh
uv run ragcentre provider add -f examples/providers/docling-parser.yaml
```

```yaml
# in a pipeline's parse node
routes:
  - {mime: "text/*", provider: builtin-text}
  - {mime: "*/*", provider: docling, options: {ocr: true}}
```

## Options

The parse route's `options` are validated by the service and reported by `/info` as
`options_schema`; RAGCentre checks a pipeline's route against it when the pipeline is created. An
unknown key or a value of the wrong type (`"false"` is not a boolean) is `400 invalid_options`
naming the option. Options apply to PDFs and to image files; other formats ignore the pipeline.

| Option | Default | Docling setting |
|---|---|---|
| `pipeline` | `standard` | `standard` (layout, OCR and tables) or `vlm` (a vision-language model reads each page; slow, downloads a model). The `vlm` pipeline takes none of `ocr`, `tables`, `table_mode`, `ocr_engine`, `languages`, `force_full_page_ocr`, `formulas`, `code` |
| `vlm_model` | `granite_docling` | The preset of the `vlm` pipeline: `granite_docling` or `smoldocling` (Docling has 28 presets; these two return DocTags with boxes). Only with `pipeline: vlm` |
| `ocr` | `true` | `do_ocr` |
| `include_page_images` | `false` | `generate_page_images`: return each page as a base64 PNG in `pages[].image` |
| `page_range` | whole file | `convert(page_range=(first, last))`: `[first, last]`, 1-based, inclusive. IR page numbers are the file's |
| `languages` | the engine's | `ocr_options.lang`, in the engine's own codes (`en` for EasyOCR, `eng` for Tesseract) |
| `tables` | `true` | `do_table_structure` |
| `table_mode` | `accurate` | `table_structure_options.mode`: `fast` or `accurate` |
| `ocr_engine` | `auto` | Which OCR options class: `auto` (`OcrAutoOptions`), `easyocr`, `rapidocr`, `tesseract` (`TesseractOcrOptions`, the `tesserocr` bindings) or `tesseract_cli` (`TesseractCliOcrOptions`, the `tesseract` program). A missing package is `503 engine_unavailable` naming it |
| `force_full_page_ocr` | `false` | `ocr_options.mode = full_page`: OCR the whole page even where there is a text layer |
| `formulas` | `false` | `do_formula_enrichment` |
| `code` | `false` | `do_code_enrichment` |
| `picture_classification` | `false` | `do_picture_classification`: the class goes to `metadata.classification` |
| `picture_description` | `false` | `do_picture_description`: the description goes to `metadata.description` and is appended to the figure's `text`. Docling's local model, or the server in `DOCLING_SHIM_PICTURE_DESCRIPTION_URL` |
| `charts` | `false` | `do_chart_extraction` |
| `images_scale` | `1.0` | `images_scale`, above 0 and at most 4 |
| `engine` | `{}` | Any other `PdfPipelineOptions` (or, with `pipeline: vlm`, `VlmPipelineOptions`) field, merged over the options above, for example `{"table_structure_options": {"do_cell_matching": false}}` |

### The `engine` passthrough

`engine` exists so that a Docling setting is usable the day Docling ships it. It is merged over the
pipeline options the named options built (nested models are merged, not replaced) and validated by
Docling's own model, so a bad field or value is `400 invalid_options` with Docling's message.

- A setting that a named option covers is refused when you give both (`ocr` and `engine.do_ocr`).
- Refused at any depth: `artifacts_path`, `enable_remote_services`, `allow_external_plugins`,
  `accelerator_options`, `document_timeout`, `url`, `headers`, `api_key`, `params`, `kwargs`, `input`,
  `path`, `tesseract_cmd`, `batch_size`, `concurrency`, `timeout`, `num_threads`, `device`, `use_gpu`,
  and any key ending in `_path`, `_dir`, `_directory` or `_batch_size` (and Docling's queue fields).
  They are service settings or would let a pipeline author read files, run programs or call URLs.
- Options are stored in the pipeline and hashed into its fingerprint; keep URLs, keys and hardware
  out of them.

### Service settings

Environment variables, read when the service starts (`docker-compose.yml` passes them through):

| Variable | Effect | In the version |
|---|---|---|
| `DOCLING_SHIM_DEVICE`, `DOCLING_SHIM_NUM_THREADS` | Docling's `accelerator_options` | no |
| `DOCLING_SHIM_PICTURE_DESCRIPTION_URL` | An OpenAI-compatible chat server for `picture_description` (sets `enable_remote_services`) | no |
| `DOCLING_SHIM_PICTURE_DESCRIPTION_MODEL` | The model name sent to that server | yes, `.desc-<model>` |
| `DOCLING_SHIM_PICTURE_DESCRIPTION_API_KEY` | Its bearer token | no |

The converter cache holds the four most recently used option sets: each holds models in memory.

## Authentication

Set `DOCLING_SHIM_API_KEY` to require `Authorization: Bearer <key>` on `POST /parse` (`GET /info`
stays open: RAGCentre reads it without credentials when it creates the provider), and give RAGCentre the same
value as the provider's `api_key` secret:

```sh
uv run ragcentre provider set-secret docling api_key --env DOCLING_SHIM_API_KEY
```

## Versioning

`/info` reports `0.3.0+docling.<docling version>.ocr-<engine>`, with `.desc-<model>` added when
`DOCLING_SHIM_PICTURE_DESCRIPTION_MODEL` is set, for example
`0.3.0+docling.2.150.1.ocr-rapidocr-torch`. `<engine>` is the OCR engine Docling's automatic default
picks, worked out without loading any model: the first installed of `ocrmac` (macOS), `nemotron`
(Linux x86-64, Python 3.12, CUDA), `rapidocr-onnxruntime`, `easyocr`, `rapidocr-torch`, or `none`.
Installing or removing one of those packages therefore changes the version. RAGCentre records that
version when the provider is created and refuses to parse with a service reporting a different one (unless the
provider's `on_version_drift` is `warn`). Upgrading Docling therefore means creating a new
provider and a new pipeline, so parses from two Docling versions are never mixed in one index.

## How Docling output maps to the IR

See the table at the top of `docling_shim/convert.py`. In short: the title and section headers
become headings, tables become Markdown (with HTML alongside), pictures become `figure` blocks
whose text is their caption and, when Docling described them, the description (also kept in
`metadata.description`, with `metadata.classification` for the picture's class), and every block
carries its page number and a top-left-origin bounding box. A block that runs over a page break also
has `spans`, one entry per provenance entry. The IR is schema 1.1.

## Tests

The tests need only `docling-core` (Docling's document model), not the models:

```sh
uv run pytest
```

`tests/test_live.py` runs against the real Docling when `SHIM_LIVE=1`: it checks that every field of
`PdfPipelineOptions` and `VlmPipelineOptions` is accounted for in `docling_shim/options.py` (a named
option, settable through `engine`, or deliberately not exposed), so a Docling upgrade that adds a field
fails it until someone decides where the field belongs.
