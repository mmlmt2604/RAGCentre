# syntax=docker/dockerfile:1

# The Surya parser shim, with llama.cpp's `llama-server` inside so Surya can start its inference
# server on the CPU. Build with `examples/` as the context (it needs ../shimkit):
#   docker build -f examples/surya_shim/Dockerfile examples
# or, from the repository root, `docker compose --profile surya up --build`.
FROM ghcr.io/ggml-org/llama.cpp:server AS llama

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# llama-server and its shared libraries, and what they link against.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libcurl4 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=llama /app /opt/llama.cpp
ENV PATH="/opt/llama.cpp:$PATH" \
    LD_LIBRARY_PATH="/opt/llama.cpp"
RUN llama-server --version

WORKDIR /src
COPY shimkit ./shimkit
COPY surya_shim/pyproject.toml ./surya_shim/
COPY surya_shim/surya_shim ./surya_shim/surya_shim
RUN uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache --no-sources ./shimkit ./surya_shim \
 && /opt/venv/bin/python -c "import surya, surya_shim.app"

# Surya downloads the GGUF model and its projector (about 1.3 GB) into ~/.cache on first use;
# mount a volume there.
RUN useradd --create-home --uid 1000 shim \
 && mkdir /home/shim/.cache && chown shim /home/shim/.cache
ENV PATH="/opt/venv/bin:$PATH" \
    SURYA_INFERENCE_BACKEND=llamacpp
USER shim
WORKDIR /home/shim
VOLUME /home/shim/.cache
EXPOSE 8704
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8704/info', timeout=3)"
CMD ["uvicorn", "surya_shim.app:app", "--host", "0.0.0.0", "--port", "8704"]
