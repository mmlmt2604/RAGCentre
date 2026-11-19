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

The parse route's `options` are passed to the service:

| Option | Default | Meaning |
|---|---|---|
| `ocr` | `true` | Run OCR on scanned pages and images in PDFs |
| `include_page_images` | `false` | Render each page and return it as a base64 PNG in `pages[].image` |
| `heading_hierarchy` | `true` | Rank section headers by PDF bookmarks, numbering (`1`, `1.1`) and font style. Docling's own default leaves every header at one level, which flattens RAGCentre's heading paths and the `structure` chunker. PDF only |

Options must be booleans: `"false"` (a string) is rejected with `400 invalid_options`.

## Authentication

Set `DOCLING_SHIM_API_KEY` to require `Authorization: Bearer <key>` on every request except
`GET /info` (RAGCentre reads `/info` without credentials when it creates the provider), and give RAGCentre the same
value as the provider's `api_key` secret:

```sh
uv run ragcentre provider set-secret docling api_key --env DOCLING_SHIM_API_KEY
```

## Versioning

`/info` reports `0.2.0+docling.<docling version>`. RAGCentre records that version when the
provider is created and refuses to parse with a service reporting a different one (unless the
provider's `on_version_drift` is `warn`). Upgrading Docling therefore means creating a new
provider and a new pipeline, so parses from two Docling versions are never mixed in one index.

## How Docling output maps to the IR

See the table at the top of `docling_shim/convert.py`. In short: the title and section headers
become headings, tables become Markdown (with HTML alongside), pictures become `figure` blocks
whose text is their caption, and every block carries its page number and a top-left-origin
bounding box.

## Tests

The tests need only `docling-core` (Docling's document model), not the models:

```sh
uv run pytest
```
