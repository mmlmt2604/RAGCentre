# syntax=docker/dockerfile:1

# The EasyOCR parser shim. Build with `examples/` as the context (it needs ../shimkit):
#   docker build -f examples/easyocr_shim/Dockerfile examples
# or, from the repository root, `docker compose --profile easyocr up --build`.
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
COPY easyocr_shim/pyproject.toml ./easyocr_shim/
COPY easyocr_shim/easyocr_shim ./easyocr_shim/easyocr_shim
RUN uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache --index-url "$TORCH_INDEX" torch torchvision \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache --no-sources ./shimkit ./easyocr_shim \
 && /opt/venv/bin/python -c "import easyocr, easyocr_shim.app"

# EasyOCR downloads its detection model and one recognition model per script (about 100 MB
# each) into ~/.EasyOCR on first use; mount a volume there.
RUN useradd --create-home --uid 1000 shim \
 && mkdir -p /home/shim/.EasyOCR && chown shim /home/shim/.EasyOCR
ENV PATH="/opt/venv/bin:$PATH"
USER shim
WORKDIR /home/shim
VOLUME /home/shim/.EasyOCR
EXPOSE 8705
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8705/info', timeout=3)"
CMD ["uvicorn", "easyocr_shim.app:app", "--host", "0.0.0.0", "--port", "8705"]
