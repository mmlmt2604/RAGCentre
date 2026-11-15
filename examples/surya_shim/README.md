# Surya parser shim

A small standalone service that lets RAGCentre parse PDFs and images with the
[Surya](https://github.com/datalab-to/surya) OCR 2.5 model, which does layout analysis, text
recognition and table recognition in one vision-language model. It speaks the parser protocol
(`docs/protocols/parser-v1.md`) and returns RAGCentre's Document IR.

It is an example, not part of the `ragcentre` package: it has its own dependencies and downloads
the model on first use, so the shim lives in its own environment. It uses the shared helpers in
`examples/shimkit/`.

## Run it

```sh
cd examples/surya_shim
uv sync
uv run uvicorn surya_shim.app:app --host 127.0.0.1 --port 8704
```

The `surya-ocr` package is light (no PyTorch). The model runs in a separate **inference server**
that Surya starts on the first parse:

- **CPU and Apple Silicon**: `llama-server` from [llama.cpp](https://github.com/ggml-org/llama.cpp)
  must be on `PATH` (`brew install llama.cpp`). A one-page document took about ten seconds on an Apple Silicon Mac, including starting the
  server. The first parse downloads the GGUF model and its projector (about 1.3 GB).
- **NVIDIA GPUs**: Surya uses `vllm` in Docker.
- **A server you already run**: set `SURYA_INFERENCE_BACKEND` and `SURYA_INFERENCE_URL`.

Surya picks the backend itself (`SURYA_INFERENCE_BACKEND` overrides it). The shim stops the server
it started when the service stops.

### With Docker

From the repository root, this starts RAGCentre and this service together:

```sh
docker compose --profile surya up --build     # the service on http://127.0.0.1:8704
```

RAGCentre runs in the same Compose network and reaches the service by name, so register the
provider with `base_url: http://surya:8704` (change that line in
`examples/providers/surya-parser.yaml`, or add the provider in the web UI). Models are downloaded on
the first parse into the `surya-models` volume (`~/.cache` in the container), so they survive a
rebuild. The image contains `llama-server` and runs the model on the CPU, which is slow: a one-page PDF
took about four and a half minutes in Docker on an Apple Silicon Mac, including starting the server. With a GPU, run the inference server yourself and set
`SURYA_INFERENCE_BACKEND` and `SURYA_INFERENCE_URL` on the service.

To build the image alone, use `examples/` as the build context:

```sh
docker build -t surya-shim -f examples/surya_shim/Dockerfile examples
```

Then register it with RAGCentre and route files to it (from the repository root):

```sh
uv run ragcentre provider add -f examples/providers/surya-parser.yaml
```

```yaml
# in a pipeline's parse node
routes:
  - {mime: "text/*", provider: builtin-text}
  - {mime: "application/pdf", provider: surya, options: {}}
  - {mime: "image/*", provider: surya, options: {}}
```

If the inference server cannot start or cannot be reached, the shim answers `503`, which RAGCentre
retries.

## Options

The parse route's `options` are validated by the service and reported by `/info` as `options_schema`;
RAGCentre checks a pipeline's route against it when the pipeline is created. An unknown key or a
value of the wrong type is `400 invalid_options` naming the option.

| Option | Default | Meaning |
|---|---|---|
| `include_page_images` | `false` | Return each rendered page as a base64 PNG in `pages[].image`. Its pixels are the unit of the boxes |
| `ocr_figures` | `false` | Also read text inside pictures, figures and diagrams (otherwise they are left empty) |
| `dpi` | `192` | Render resolution for PDF pages, an integer from 72 to 400 |
| `page_range` | whole file | `[first, last]`, 1-based and inclusive. IR page numbers are the file's, not positions in the range. A range outside a PDF is `400 invalid_options`; for an image only a range that includes page 1 is accepted |

There is no `engine` passthrough and no `full_page` option: the shim never gives Surya layout results,
so Surya always reads whole pages (its more accurate mode), and `full_page=False` would raise without
them. The backend, model and inference-server settings are Surya's own `SURYA_*` environment variables
(`SURYA_INFERENCE_BACKEND`, `SURYA_INFERENCE_URL`, `SURYA_MODEL_CHECKPOINT`, `SURYA_GGUF_*`); the ones
that change the output are in the version below.

The shim does not serialise parses: it is a client of the inference server. The provider file sets
`max_concurrency: 2`. Only the first frame of a multi-page TIFF is read.

## Authentication

Set `SURYA_SHIM_API_KEY` to require `Authorization: Bearer <key>` on `/parse`, and give RAGCentre
the same value as the provider's `api_key` secret:

```sh
uv run ragcentre provider set-secret surya api_key --env SURYA_SHIM_API_KEY
```

`GET /info` stays open: RAGCentre reads it without credentials when it creates the provider.

## Versioning

`/info` reports `0.3.0+surya.<surya-ocr version>.<model>.<backend>`, with `.gguf-<file>` added for a
llama.cpp model file other than the default, for example `0.3.0+surya.0.24.0.surya-ocr-2.5.llamacpp`:

- `<model>` is the last segment of `SURYA_MODEL_CHECKPOINT`.
- `<backend>` is `SURYA_INFERENCE_BACKEND` if set, otherwise what Surya's own auto-detection picks
  (`vllm` on an NVIDIA GPU, otherwise `llamacpp`). That detection is a private function
  (`surya.inference._autodetect_backend`); if a Surya release removes it, the backend is reported
  as `auto`.
- `.gguf-<file>` appears only with the `llamacpp` backend and a file other than `surya-2.5.gguf`: the
  base name of `SURYA_GGUF_LOCAL_MODEL_PATH` (Surya uses it only together with
  `SURYA_GGUF_LOCAL_MMPROJ_PATH`), else `SURYA_GGUF_MODEL_FILE`. Characters outside `[A-Za-z0-9_-]`
  become `-`.

RAGCentre records that version when the provider is created and refuses to parse with a service
reporting a different one (unless the provider's `on_version_drift` is `warn`). Upgrading Surya or
changing the model therefore means creating a new provider and a new pipeline.

## How Surya output maps to the IR

See the table at the top of `surya_shim/convert.py`. Surya returns each block as HTML; the shim
reads it with the standard library. In short: section headers become headings (level from the
`<hn>` tag the model wrote), lists and tables of contents become one `list_item` per `<li>`, tables
become Markdown (spans expanded) with the HTML alongside, equations become `formula` blocks (the
LaTeX inside `<math>`), pictures, figures and diagrams become `figure` blocks, and captions,
footnotes, page headers and page footers keep their names. Blocks come in reading order.

- **Positions** are the pixels of the rendered page (a US-letter PDF at 192 DPI is 1632 x 2112),
  and `pages[].width/height` use the same unit.
- **No document title.** Surya has no title label, so a document has a level-1 heading only when
  the model wrote an `<h1>`. Nothing is promoted.
- **Captions** are separate `caption` blocks next to the figure or table they belong to.
- **Confidence.** Surya's per-block `confidence` is carried as `blocks[].confidence` (schema 1.1),
  clamped to 0 to 1; every `list_item` made from one list block carries the list's value.

## Licence

The Surya code is Apache 2.0. The **model weights use a modified OpenRAIL-M licence**: free for
research, personal use and organisations under USD 5 million in funding or revenue; broader
commercial use needs a licence from Datalab (see the Surya README and
<https://www.datalab.to/pricing>). Check the current terms before you deploy it.

## Tests

The conversion works on plain dicts and the engine tests use a stand-in for `surya`, so the tests
need only `fastapi`, `httpx` and `pytest`, not Surya, a model or Pillow:

```sh
uv run --isolated --no-project --with ../shimkit --with fastapi --with python-multipart --with httpx --with pytest pytest
```

`tests/test_live.py` parses a small generated PDF through the real engine. It is skipped unless
`SHIM_LIVE=1` and Surya is installed (and an inference server can start):

```sh
SHIM_LIVE=1 uv run pytest tests/test_live.py -s
```

The same file checks that every parameter of the Surya calls the shim makes
(`RecognitionPredictor.__call__`, `load_pdf`) is accounted for in `surya_shim/engine.py`, so a Surya
upgrade that adds one fails it until someone decides where it belongs.

`tests/golden/sample_ir.json` is the IR for a sample document; RAGCentre's own suite validates it
(`tests/test_parser_shim_goldens.py`). Regenerate it with `UPDATE_GOLDEN=1 uv run pytest`.
