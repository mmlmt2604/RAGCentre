# syntax=docker/dockerfile:1

# The MinerU parser shim. Build with `examples/` as the context (it needs ../shimkit):
#   docker build -f examples/mineru_shim/Dockerfile examples
# or, from the repository root, `docker compose --profile mineru up --build`.
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# PyTorch build: `cpu` keeps the image small and runs anywhere. For an NVIDIA GPU pass, for example,
# `--build-arg TORCH_BACKEND=cu128` and give the container the GPU.
ARG TORCH_BACKEND=cpu

# OpenCV's and PyTorch's shared libraries.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY shimkit ./shimkit
COPY mineru_shim/pyproject.toml ./mineru_shim/
COPY mineru_shim/mineru_shim ./mineru_shim/mineru_shim
RUN uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache --no-sources --torch-backend "$TORCH_BACKEND" ./shimkit ./mineru_shim \
 && /opt/venv/bin/python -c "import mineru, mineru_shim.app"

# MinerU downloads its models into ~/.cache on first use; mount a volume there.
RUN useradd --create-home --uid 1000 shim \
 && mkdir /home/shim/.cache && chown shim /home/shim/.cache
ENV PATH="/opt/venv/bin:$PATH"
USER shim
WORKDIR /home/shim
VOLUME /home/shim/.cache
EXPOSE 8702
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8702/info', timeout=3)"
CMD ["uvicorn", "mineru_shim.app:app", "--host", "0.0.0.0", "--port", "8702"]
