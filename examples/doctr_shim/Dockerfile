# syntax=docker/dockerfile:1

# The docTR parser shim. Build with `examples/` as the context (it needs ../shimkit):
#   docker build -f examples/doctr_shim/Dockerfile examples
# or, from the repository root, `docker compose --profile doctr up --build`.
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# The PyTorch build. The CPU wheels by default (much smaller than the CUDA ones); for an NVIDIA GPU
# pass e.g. --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu128.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

# OpenCV's shared libraries.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY shimkit ./shimkit
COPY doctr_shim/pyproject.toml ./doctr_shim/
COPY doctr_shim/doctr_shim ./doctr_shim/doctr_shim
RUN uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache --index-url "$TORCH_INDEX" torch torchvision \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache --no-sources ./shimkit ./doctr_shim \
 && /opt/venv/bin/python -c "import doctr, doctr_shim.app"

# docTR downloads its detection and recognition models (about 100 to 200 MB) into ~/.cache/doctr
# on first use; mount a volume on ~/.cache.
RUN useradd --create-home --uid 1000 shim \
 && mkdir -p /home/shim/.cache && chown shim /home/shim/.cache
ENV PATH="/opt/venv/bin:$PATH"
USER shim
WORKDIR /home/shim
VOLUME /home/shim/.cache
EXPOSE 8706
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8706/info', timeout=3)"
CMD ["uvicorn", "doctr_shim.app:app", "--host", "0.0.0.0", "--port", "8706"]
