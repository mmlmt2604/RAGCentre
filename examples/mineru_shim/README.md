# MinerU parser shim

A small standalone service that lets RAGCentre parse PDFs, images, Office documents, HTML, CSV and
EPUB with [MinerU](https://github.com/opendatalab/MinerU) 4.x. It speaks the parser protocol
(`docs/protocols/parser-v1.md`) and returns RAGCentre's Document IR.

It is an example, not part of the `ragcentre` package: MinerU has its own (large) dependencies and
downloads models on first use, so the shim lives in its own environment. It uses the shared helpers
in `examples/shimkit/`.

## Run it

Python 3.11 to 3.14 (MinerU's range).

```sh
cd examples/mineru_shim
uv sync
uv run uvicorn mineru_shim.app:app --host 127.0.0.1 --port 8702
```

### With Docker

From the repository root, this starts RAGCentre and this service together:

```sh
docker compose --profile mineru up --build     # the service on http://127.0.0.1:8702
```

RAGCentre runs in the same Compose network and reaches the service by name, so register the
provider with `base_url: http://mineru:8702` (change that line in
`examples/providers/mineru-parser.yaml`, or add the provider in the web UI). Models are downloaded on
the first parse into the `mineru-models` volume (`~/.cache` in the container), so they survive a
rebuild. The image installs the CPU build of PyTorch; pass `--build-arg TORCH_BACKEND=cu128` (or the
backend for your CUDA version) to build for an NVIDIA GPU.

To build the image alone, use `examples/` as the build context:

```sh
docker build -t mineru-shim -f examples/mineru_shim/Dockerfile examples
```

Then register it with RAGCentre and route files to it (from the repository root):

```sh
uv run ragcentre provider add -f examples/providers/mineru-parser.yaml
```

```yaml
# in a pipeline's parse node
routes:
  - {mime: "text/*", provider: builtin-text}
  - {mime: "*/*", provider: mineru, options: {tier: basic}}
```

The first parse at each tier is slow while MinerU fetches its models from Hugging Face. The
`flash` tier needs the least; `basic` runs MinerU's local layout and OCR models; `standard` and
`advanced` use a vision-language model, which MinerU starts for itself (here it used `llama-server`
from llama.cpp, with a Qwen-VL model) or reaches through its own configuration.

## Options

The parse route's `options` are validated by the service and reported by `/info` as `options_schema`;
RAGCentre checks a pipeline's route against it when the pipeline is created. An unknown key or a
value of the wrong type is `400 invalid_options` naming the option.

| Option | Default | Meaning |
|---|---|---|
| `tier` | `standard` | `flash`, `basic`, `standard` or `advanced`. Only PDFs and images support every tier; Office, HTML, CSV and EPUB files are always parsed with `flash`, whatever you ask for |
| `ocr` | `"auto"` | `true` forces OCR, `false` prefers the text layer (above `flash`, MinerU still OCRs regions that have no usable text), `"auto"` lets MinerU decide |
| `image_analysis` | `false` | Describe pictures with a vision model. MinerU's own default is on; it is off here so that a default run does not need a VLM for every picture |
| `page_range` | whole file | `[first, last]`, 1-based and inclusive, PDFs only (passed to MinerU as `"first-last"`). IR page numbers are the file's, and `pages` lists only the pages parsed. MinerU leaves out pages past the end of the file |

`mineru.parse` has no other output parameters, so there is no `engine` passthrough.

### Service settings

Where the `standard` and `advanced` tiers find their vision-language model. Environment variables,
read when the service starts (`docker-compose.yml` passes them through); they become a `VlmConfig`
passed to `mineru.parse`, and MinerU's own configuration applies when none is set:

| Variable | `VlmConfig` field | In the version |
|---|---|---|
| `MINERU_SHIM_VLM_ENGINE` | `engine`: `auto`, `llama-cpp`, `vllm`, `lmdeploy` or `mlx` | yes, `.vlm-<engine>-<model>` |
| `MINERU_SHIM_VLM_MODEL` | `model` | yes |
| `MINERU_SHIM_VLM_SERVER_URL` | `server_url` (a remote server wins over a local engine) | no |
| `MINERU_SHIM_VLM_API_KEY` | `api_key` | no |

An invalid value is `503 engine_unavailable` naming `MINERU_SHIM_VLM_*`. `http_timeout` and
`max_concurrency` are left to MinerU's configuration.

The shim serialises parses (one at a time) and the provider file sets `max_concurrency: 1`.

## Authentication

Set `MINERU_SHIM_API_KEY` to require `Authorization: Bearer <key>` on `/parse`, and give RAGCentre
the same value as the provider's `api_key` secret:

```sh
uv run ragcentre provider set-secret mineru api_key --env MINERU_SHIM_API_KEY
```

`GET /info` stays open: RAGCentre reads it without credentials when it creates the provider.

## Versioning

`/info` reports `0.3.0+mineru.<mineru version>`, with `.vlm-<engine>-<model>` added when
`MINERU_SHIM_VLM_ENGINE` or `MINERU_SHIM_VLM_MODEL` is set (`auto` stands for an unset engine), for
example `0.3.0+mineru.4.2.2.vlm-llama-cpp-Qwen-Qwen3-VL-4B`; characters outside `[A-Za-z0-9_-]` become `-`. RAGCentre records that version when the provider
is created and refuses to parse with a service reporting a different one (unless the provider's
`on_version_drift` is `warn`). Upgrading MinerU means creating a new provider and a new pipeline, so
parses from two MinerU versions are never mixed in one index.

## How MinerU output maps to the IR

MinerU's result carries no per-block score, so the IR (schema 1.1) has no `confidence` for its blocks.

See the table at the top of `mineru_shim/convert.py`. In short: titles become headings (MinerU's
`text_level`), lists and tables-of-contents become `list_item` blocks, tables become Markdown with
the HTML alongside (their captions and footnotes are separate `caption` and `footnote` blocks),
images and charts become `figure` blocks whose text is the caption (plus MinerU's description when
`image_analysis` is on), equations become `formula` blocks (LaTeX), and headers, footers and page
numbers become `page_header` and `page_footer`.

- **Positions.** MinerU reports boxes on a 0 to 1000 scale and no page sizes. The shim reads the
  real sizes (points for a PDF, via pypdfium2; pixels for an image, via Pillow) and scales the
  boxes, so `bbox` and `pages[].width/height` share a unit. For Office, HTML, CSV and EPUB files
  there are no page sizes, so blocks have a `page` but no `bbox`.
- **Heading levels** are MinerU's. The lighter tiers may mark a document title as level 2 rather
  than 1.

## Licence

MinerU is Apache 2.0 **with additional terms** (`LICENSE.md` in its wheel): you must clearly say
that MinerU is used if you provide an online service built on it, and a separate commercial licence
is required above 100 million monthly active users or USD 20 million monthly revenue. Check the
current terms before you deploy it.

## Tests

The conversion works on plain dicts, so the tests need only `fastapi`, `httpx` and `pytest`, not
MinerU or its models:

```sh
uv run --isolated --no-project --with ../shimkit --with fastapi --with python-multipart --with httpx --with pytest pytest
```

`tests/test_live.py` parses a small generated PDF through the real engine. It is skipped unless
`SHIM_LIVE=1` and MinerU is installed:

```sh
SHIM_LIVE=1 uv run pytest tests/test_live.py -s
```

The same file checks that every parameter of `mineru.parse` and every field of `VlmConfig` is accounted
for in `mineru_shim/engine.py` (an option, a service setting, or deliberately not exposed), so a MinerU
upgrade that adds one fails it until someone decides where it belongs.

`tests/golden/sample_ir.json` is the IR for a sample document; RAGCentre's own suite validates it
(`tests/test_parser_shim_goldens.py`). Regenerate it with `UPDATE_GOLDEN=1 uv run pytest`.
