# syntax=docker/dockerfile:1

# The Docling parser shim. Build with `examples/` as the context:
#   docker build -f examples/docling_shim/Dockerfile examples
# or, from the repository root, `docker compose --profile docling up --build`.
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# PyTorch build: `cpu` keeps the image small and runs anywhere. For an NVIDIA GPU pass, for example,
# `--build-arg TORCH_BACKEND=cu128` and give the container the GPU.
ARG TORCH_BACKEND=cpu

WORKDIR /src
COPY docling_shim/pyproject.toml ./
COPY docling_shim/docling_shim ./docling_shim
RUN uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache --torch-backend "$TORCH_BACKEND" . \
 && /opt/venv/bin/python -c "import docling.document_converter, docling_shim.app"

# Docling downloads its layout and OCR models into ~/.cache on first use; mount a volume there.
RUN useradd --create-home --uid 1000 shim \
 && mkdir /home/shim/.cache && chown shim /home/shim/.cache
ENV PATH="/opt/venv/bin:$PATH"
USER shim
WORKDIR /home/shim
VOLUME /home/shim/.cache
EXPOSE 8701
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8701/info', timeout=3)"
CMD ["uvicorn", "docling_shim.app:app", "--host", "0.0.0.0", "--port", "8701"]
