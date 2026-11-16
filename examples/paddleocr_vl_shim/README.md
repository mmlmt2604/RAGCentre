# PaddleOCR-VL parser shim

A small standalone service that lets RAGCentre parse PDFs and images with
[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) 3.x's **PaddleOCR-VL** document pipeline
(`paddleocr.PaddleOCRVL`): a layout model finds the regions of each page and a 0.9B vision-language
model reads each one. It speaks the parser protocol (`docs/protocols/parser-v1.md`) and returns
RAGCentre's Document IR.

It is a separate service from [`paddleocr_shim`](../paddleocr_shim/) (PP-StructureV3) because the two
pipelines return different results: other labels, no reading-order index, heading levels assigned in a
second step, and a recognition model that can run on a server. Use that one for a classic detector and
recogniser pipeline, this one for the VL model.

It is an example, not part of the `ragcentre` package: PaddleOCR and PaddlePaddle have their own
dependencies and download models on first use, so the shim lives in its own environment. It uses the
shared helpers in `examples/shimkit/`.

## Run it

Python 3.11 to 3.13 (PaddlePaddle publishes no wheels for newer versions yet).

```sh
cd examples/paddleocr_vl_shim
uv sync
uv pip install paddlepaddle      # CPU build; see below for GPUs
uv run uvicorn paddleocr_vl_shim.app:app --host 127.0.0.1 --port 8705
```

**PaddlePaddle is not installed by `uv sync`**: pick the build for your machine. `paddlepaddle` is the
CPU build (it ran on macOS arm64). For an NVIDIA GPU install `paddlepaddle-gpu` from the index that
matches your CUDA version (see the [PaddlePaddle install guide](https://www.paddlepaddle.org.cn/en/install/quick)).

The first parse downloads the layout model (`PP-DocLayoutV3`) and, with the native backend, the
recognition model (`PaddleOCR-VL-1.6`, 1.8 GB) into `~/.paddlex`. Set
`PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True` to skip PaddleX's connectivity check at start-up.

### Where the recognition model runs

The recognition model runs in one of two ways, chosen by the service settings below:

- **`native`** (the default): inside this process, through PaddlePaddle. It ran on a CPU-only macOS
  arm64 machine (about 30 s for a one-page scan) and is the simplest setup. It is slow on a CPU for long
  documents; use a GPU build or a server.
- **A server** that speaks the OpenAI chat API: `llama-cpp-server`, `mlx-vlm-server`, `vllm-server`,
  `sglang-server` or `fastdeploy-server`. With llama.cpp, for example (the GGUF files are the
  [official ones](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6-GGUF), 1.8 GB):

  ```sh
  llama-server -m PaddleOCR-VL-1.6-GGUF.gguf --mmproj PaddleOCR-VL-1.6-GGUF-mmproj.gguf \
      --host 127.0.0.1 --port 8080 --temp 0

  PADDLEOCR_VL_SHIM_REC_BACKEND=llama-cpp-server \
  PADDLEOCR_VL_SHIM_REC_SERVER_URL=http://127.0.0.1:8080/v1 \
  PADDLEOCR_VL_SHIM_REC_MODEL=PaddleOCR-VL-1.6-0.9B \
  uv run uvicorn paddleocr_vl_shim.app:app --port 8705
  ```

  The layout model still runs in the shim; only the recognition calls go to the server.

### Service settings

Environment variables, read at start-up. They are deployment choices, not options, so a pipeline author
cannot set them (and `engine` refuses their arguments). A wrong value is `503 engine_unavailable` at the
first parse, naming the variable.

| Variable | Effect | In version |
|---|---|---|
| `PADDLEOCR_VL_SHIM_PIPELINE_VERSION` | `pipeline_version`: `v1`, `v1.5` or `v1.6` (default `v1.6`; each uses its own layout and recognition model) | yes, `.vl-<version>` |
| `PADDLEOCR_VL_SHIM_REC_BACKEND` | `vl_rec_backend`: `native` (default) or a server backend | yes, `.backend-<name>` unless `native` |
| `PADDLEOCR_VL_SHIM_REC_SERVER_URL` | `vl_rec_server_url`: required for a server backend, refused with `native` | no |
| `PADDLEOCR_VL_SHIM_REC_MODEL` | `vl_rec_model_name`, for example `PaddleOCR-VL-1.6-0.9B` | yes, `.model-<name>` |
| `PADDLEOCR_VL_SHIM_REC_API_MODEL` | `vl_rec_api_model_name`: the name the server knows the model by | yes, `.apimodel-<name>` |
| `PADDLEOCR_VL_SHIM_REC_API_KEY` | `vl_rec_api_key` for the server | no |
| `PADDLEOCR_VL_SHIM_REC_MAX_CONCURRENCY` | `vl_rec_max_concurrency`: requests in flight to the server | no |
| `PADDLEOCR_VL_SHIM_API_KEY` | the shim's own bearer token, see Authentication | no |

The URL and the key never appear in `/info`, the version or an error message.

If the server cannot be reached the shim answers `503 engine_unavailable` naming
`PADDLEOCR_VL_SHIM_REC_SERVER_URL` (it checks the connection after a failed parse, because PaddleX
re-raises the worker's error as a plain `RuntimeError`).

Device, thread-count and other PaddleOCR runtime arguments (`device`, `cpu_threads`, ...) are not exposed.

### With Docker

From the repository root, this starts RAGCentre and this service together:

```sh
docker compose --profile paddleocr-vl up --build     # the service on http://127.0.0.1:8705
```

Register the provider with `base_url: http://paddleocr-vl:8705` (change that line in
`examples/providers/paddleocr-vl-parser.yaml`, or add the provider in the web UI). Models are downloaded on
the first parse into the `paddleocr-vl-models` volume (`~/.paddlex` in the container). The image installs
the CPU build of PaddlePaddle (`--build-arg PADDLE_PACKAGE=…` chooses another package). Pass the
`PADDLEOCR_VL_SHIM_*` variables above in the environment of `docker compose`; to use a llama.cpp server on
the host, set the backend to `llama-cpp-server` and the URL to `http://host.docker.internal:8080/v1`. The
Docker image has not been built or run.

Then register it with RAGCentre and route files to it (from the repository root):

```sh
uv run ragcentre provider add -f examples/providers/paddleocr-vl-parser.yaml
```

```yaml
# in a pipeline's parse node
routes:
  - {mime: "text/*", provider: builtin-text}
  - {mime: "application/pdf", provider: paddleocr-vl, options: {}}
  - {mime: "image/*", provider: paddleocr-vl, options: {}}
```

## Options

The parse route's `options` are validated by the service and reported by `/info` as `options_schema`;
RAGCentre checks a pipeline's route against it when the pipeline is created. An unknown key or a value of
the wrong type is `400 invalid_options` naming the option.

| Option | Default | Meaning |
|---|---|---|
| `layout_detection` | `true` | Find the page's regions first. Off, the whole page is one block of the kind `engine.prompt_label` names (`ocr` by default). `use_layout_detection` |
| `orientation` | `false` | Detect and correct page orientation. `use_doc_orientation_classify` |
| `unwarp` | `false` | Flatten curved or photographed pages. `use_doc_unwarping` |
| `charts` | `false` | Recognise charts (otherwise a chart is a figure with no text). `use_chart_recognition` |
| `seals` | `false` | Recognise seals (otherwise a seal is a figure with no text). `use_seal_recognition` |
| `image_ocr` | `false` | Read the text inside pictures. `use_ocr_for_image_block` |
| `heading_levels` | `true` | Rank section titles with PaddleX's assignment, over the whole document (`restructure_pages(relevel_titles=…)`). Off, every section title is level 2 |
| `merge_tables` | `true` | Join a table that continues across pages (`restructure_pages(merge_tables=…)`) |
| `engine` | `{}` | Other `PaddleOCRVL` arguments, see below |

There is no `page_range` (PaddleOCR-VL has no page selection, so a PDF is parsed whole) and no
`languages` (the model is multilingual).

`heading_levels` is on by default here, unlike in the PP-StructureV3 shim, because PaddleOCR-VL's own
second step does it. That second step (`PaddleOCRVL.restructure_pages`) edits the blocks in place; the shim
skips it when both options are off.

### The `engine` passthrough

`engine` is an object of other `PaddleOCRVL` arguments, so a new PaddleOCR parameter is usable the day it
ships. Each key goes to the constructor, to `predict`, or to both, by which signature accepts it
(`engine.py` lists them). A key must be one of those: an unknown one is `400 invalid_options`.

- **Layout** (both): `layout_threshold`, `layout_nms`, `layout_unclip_ratio`, `layout_merge_bboxes_mode`,
  `merge_layout_blocks`; `layout_shape_mode` (`predict` only); `layout_detection_model_name`,
  `doc_orientation_classify_model_name`, `doc_unwarping_model_name` (constructor only).
- **Sampling** (`predict`): `temperature`, `top_p`, `repetition_penalty`, `max_new_tokens`, `min_pixels`,
  `max_pixels`. Among server backends, only `vllm-server` honours these pixel limits; the other server
  backends warn and ignore them. The native backend also accepts them.
- **Prompt** (`predict`): `prompt_label` (`ocr`, `formula`, `table`, `chart`, `spotting`, `seal`; used when
  `layout_detection` is off) and `vlm_extra_args`.
- An argument that has a named option (`use_chart_recognition`, ...) is refused: use the option.
- The service-setting arguments (`pipeline_version`, `vl_rec_*`) are refused and name their variable.
- Refused at any depth, as operational: `*_model_dir` (so a model is picked by name, never by path), `url`,
  `headers`, `api_key`, `params`, `kwargs`, `input`, and the other keys in `shimkit.DENIED_ENGINE_KEYS`.
- Not reachable, because they do not change what the shim reads: `use_queues` (a threading setting),
  `format_block_content` and `markdown_ignore_labels` (they only change the result's Markdown and JSON
  export).
- Options are stored in the pipeline and hashed into its fingerprint; keep paths, URLs and keys out.

PaddleX loads models for a feature when a pipeline is *built*, so the shim builds and keeps a pipeline per
distinct set of constructor arguments (the four most recently used, since each holds models in memory; it
closes the ones it drops).

The shim serialises parses (one at a time) and the provider file sets `max_concurrency: 1`.

## Authentication

Set `PADDLEOCR_VL_SHIM_API_KEY` to require `Authorization: Bearer <key>` on `/parse`, and give RAGCentre the
same value as the provider's `api_key` secret:

```sh
uv run ragcentre provider set-secret paddleocr-vl api_key --env PADDLEOCR_VL_SHIM_API_KEY
```

`GET /info` stays open: RAGCentre reads it without credentials when it creates the provider. (This key is
for the shim; the key for a recognition server is `PADDLEOCR_VL_SHIM_REC_API_KEY`.)

## Versioning

`/info` reports `0.1.0+paddleocr.<paddleocr version>.vl-<pipeline version>`, with `.backend-<backend>` added
for a server backend, `.model-<name>` when `PADDLEOCR_VL_SHIM_REC_MODEL` is set and `.apimodel-<name>` when
`PADDLEOCR_VL_SHIM_REC_API_MODEL` is set (characters other than letters, digits, `_` and `-` become `-`). For
example `0.1.0+paddleocr.3.7.0.vl-v1.6.backend-llama-cpp-server.model-PaddleOCR-VL-1-6-0-9B`. RAGCentre
records the version when the provider is created and refuses to parse with a service reporting a different
one (unless the provider's `on_version_drift` is `warn`). Upgrading PaddleOCR, or changing the pipeline
version, backend or model, therefore means creating a new provider and a new pipeline. The server's URL, key
and concurrency are not part of it. A GGUF quantisation or a server-side sampling default that the shim
cannot see is not either: change the provider's `revision` when those change. `REC_MODEL` must name a
model PaddleX recognises; it is not a free-form revision label.

## How PaddleOCR-VL output maps to the IR

The IR is schema 1.1. PaddleOCR's result carries no per-block score, so blocks have no `confidence`.

See the table at the top of `paddleocr_vl_shim/convert.py`. The labels are the 25 in the layout models'
`label_list` (`PP-DocLayoutV2` and `V3` have the same ones) plus `ocr` and `spotting`, which only appear when
`layout_detection` is off. In short: `doc_title` becomes a level-1 heading and `paragraph_title` a level 2 to
6 heading; `text`, `vertical_text`, `abstract`, `content`, `reference` and `reference_content` become
paragraphs; tables become Markdown with the HTML alongside; `image`, `chart` and `seal` become `figure`
blocks; `figure_title` (PaddleOCR-VL's one caption label) becomes `caption`; `display_formula` and
`inline_formula` keep their LaTeX as `formula`; `algorithm` becomes `code`; `footnote` and `vision_footnote`
become `footnote`; `header`, `footer` and `number` (the page number) become `page_header` and `page_footer`;
`aside_text` is kept as its own type. Header and footer images and formula numbers are dropped.

- **Reading order** is the order of the engine's blocks. PaddleOCR-VL has no `order_index`; the layout model
  returns its regions already ordered.
- **Positions** are PaddleOCR's pixels: a PDF page is rendered at 2x, so a US-letter page is 1224 x 1584.
  `pages[].width/height` use the same unit. A single image is page 1.
- **Heading levels.** `predict` leaves `title_level` unset; the second step sets it on `paragraph_title`
  blocks, starting at 1, so the IR level is `title_level + 1`. A document whose title the layout model labels
  `paragraph_title` rather than `doc_title` (the live text-layer PDF did) therefore has no level-1 heading and
  no `metadata.title`.
- **Lists.** PaddleOCR has no list label, so a bulleted line comes out as a paragraph.
- **Tables** are HTML when the engine can convert the model's table tokens; otherwise the block's text is the
  engine's own fallback string. Merged tables retain every page location in `spans`, with `page` and
  `bbox` pointing at the first segment.
- **Text layers.** A PDF is rendered and read as an image; its text layer is not used.

## Licence

PaddleOCR is Apache 2.0. Check the licence of each model it downloads before commercial use.

## Tests

The conversion works on plain dicts and the engine tests use a stand-in for `paddleocr`, so the tests need
only `fastapi`, `httpx` and `pytest`, not PaddleOCR or its models:

```sh
uv run --isolated --no-project --with ../shimkit --with fastapi --with python-multipart --with httpx --with pytest pytest
```

`tests/test_live.py` parses a generated scan and a generated PDF through the real engine, using the backend
the `PADDLEOCR_VL_SHIM_*` variables name. It is skipped unless `SHIM_LIVE=1` and PaddleOCR is installed:

```sh
SHIM_LIVE=1 uv run pytest tests/test_live.py -s
```

The same file checks that every argument of `PaddleOCRVL.__init__` and `predict` is accounted for in
`paddleocr_vl_shim/engine.py` (a named option, a service setting, allowed through `engine`, or refused on
purpose), that the backend and pipeline-version lists still match PaddleOCR's, and that the label list
matches the layout models' `inference.yml`, so an upgrade that changes one fails it until someone decides.

`tests/golden/sample_ir.json` is the IR for a sample document that carries every label. It has also been
validated manually with RAGCentre's `normalise_ir` and structure chunker; adding it to the root golden-test
matrix is left to the integrator because that test file is outside this workstream's paths.
Regenerate it with `UPDATE_GOLDEN=1 uv run pytest`.
