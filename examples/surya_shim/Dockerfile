# syntax=docker/dockerfile:1

# The Surya parser shim, with llama.cpp's `llama-server` inside so Surya can start its inference
# server on the CPU. Build with `examples/` as the context (it needs ../shimkit):
#   docker build -f examples/surya_shim/Dockerfile examples
# or, from the repository root, `docker compose --profile surya up --build`.
# The base is llama.cpp's own server image (Ubuntu), because its binaries need that image's C
# library; Python comes from uv.
FROM ghcr.io/ggml-org/llama.cpp:server
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# llama-server finds its shared libraries next to itself only when started from /app.
ENV PATH="/app:$PATH" \
    LD_LIBRARY_PATH=/app \
    UV_PYTHON_INSTALL_DIR=/opt/python

WORKDIR /src
RUN llama-server --version
COPY shimkit ./shimkit
COPY surya_shim/pyproject.toml ./surya_shim/
COPY surya_shim/surya_shim ./surya_shim/surya_shim
RUN uv venv --python 3.13 /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache --no-sources ./shimkit ./surya_shim \
 && /opt/venv/bin/python -c "import surya, surya_shim.app"

# Surya downloads the GGUF model and its projector (about 1.3 GB) into ~/.cache on first use;
# mount a volume there.
RUN (userdel --remove ubuntu 2>/dev/null || true) \
 && useradd --create-home --uid 1000 shim \
 && mkdir /home/shim/.cache && chown shim /home/shim/.cache
ENV PATH="/opt/venv/bin:$PATH" \
    SURYA_INFERENCE_BACKEND=llamacpp
USER shim
WORKDIR /home/shim
VOLUME /home/shim/.cache
EXPOSE 8704
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8704/info', timeout=3)"
ENTRYPOINT []
CMD ["uvicorn", "surya_shim.app:app", "--host", "0.0.0.0", "--port", "8704"]
