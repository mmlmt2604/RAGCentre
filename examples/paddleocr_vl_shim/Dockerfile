# syntax=docker/dockerfile:1

# The PaddleOCR-VL parser shim. Build with `examples/` as the context (it needs ../shimkit):
#   docker build -f examples/paddleocr_vl_shim/Dockerfile examples
# or, from the repository root, `docker compose --profile paddleocr-vl up --build`.
# Python 3.13: PaddlePaddle publishes no wheels for newer versions yet.
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# The PaddlePaddle build. `paddlepaddle` is the CPU build; for an NVIDIA GPU see the README.
ARG PADDLE_PACKAGE=paddlepaddle

# OpenCV's and PaddlePaddle's shared libraries.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY shimkit ./shimkit
COPY paddleocr_vl_shim/pyproject.toml ./paddleocr_vl_shim/
COPY paddleocr_vl_shim/paddleocr_vl_shim ./paddleocr_vl_shim/paddleocr_vl_shim
RUN uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache --no-sources ./shimkit ./paddleocr_vl_shim "$PADDLE_PACKAGE" \
 && /opt/venv/bin/python -c "import paddle, paddleocr_vl_shim.app"

# PaddleX downloads its models into ~/.paddlex on first use (the layout model, and for the native backend
# the 1.8 GB recognition model); mount a volume there.
RUN useradd --create-home --uid 1000 shim \
 && mkdir /home/shim/.paddlex && chown shim /home/shim/.paddlex
ENV PATH="/opt/venv/bin:$PATH"
USER shim
WORKDIR /home/shim
VOLUME /home/shim/.paddlex
EXPOSE 8705
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8705/info', timeout=3)"
CMD ["uvicorn", "paddleocr_vl_shim.app:app", "--host", "0.0.0.0", "--port", "8705"]
