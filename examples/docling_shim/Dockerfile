# syntax=docker/dockerfile:1

# The Docling parser shim. Build with `examples/` as the context (it needs ../shimkit):
#   docker build -f examples/docling_shim/Dockerfile examples
# or, from the repository root, `docker compose --profile docling up --build`.
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# PyTorch build: `cpu` keeps the image small and runs anywhere. For an NVIDIA GPU pass, for example,
# `--build-arg TORCH_BACKEND=cu128` and give the container the GPU.
ARG TORCH_BACKEND=cpu

# `local` (default) runs Docling in this container. `remote` installs no Docling or PyTorch and
# needs DOCLING_SERVE_URL pointing at a docling-serve:
#   docker build --build-arg ENGINE=remote -f examples/docling_shim/Dockerfile examples
ARG ENGINE=local

WORKDIR /src
COPY shimkit ./shimkit
COPY docling_shim/pyproject.toml ./docling_shim/
COPY docling_shim/docling_shim ./docling_shim/docling_shim
COPY docling_shim/check_ocr.py ./docling_shim/
RUN uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache --no-sources --torch-backend "$TORCH_BACKEND" ./shimkit \
      "$( [ "$ENGINE" = remote ] && echo ./docling_shim || echo './docling_shim[local]' )" \
 && if [ "$ENGINE" = remote ]; then /opt/venv/bin/python -c "import docling_shim.app"; \
    else /opt/venv/bin/python -c "import docling.document_converter, docling_shim.app"; fi

# Docling downloads its layout and table models into ~/.cache on first use; mount a volume there.
# RapidOCR's default models come in its wheel. It downloads those for other languages (`ocr_lang`)
# next to them, so that directory, and only that one, is writable by the service's user.
RUN useradd --create-home --uid 1000 shim \
 && mkdir /home/shim/.cache && chown shim /home/shim/.cache \
 && if [ "$ENGINE" != remote ]; then \
      chown shim "$(/opt/venv/bin/python -c 'import importlib.util as u; print(u.find_spec("rapidocr").submodule_search_locations[0])')/models"; \
    fi
ENV PATH="/opt/venv/bin:$PATH"
USER shim
WORKDIR /home/shim
# A healthy /info says nothing about OCR, so fail the build here: the default OCR engine must read
# text as this user, with no network and so no downloads.
RUN --network=none if [ "$ENGINE" != remote ]; then python /src/docling_shim/check_ocr.py; fi
VOLUME /home/shim/.cache
EXPOSE 8701
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8701/info', timeout=3)"
CMD ["uvicorn", "docling_shim.app:app", "--host", "0.0.0.0", "--port", "8701"]
