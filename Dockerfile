# syntax=docker/dockerfile:1

# Stage 1: build the web UI into src/ragcentre/ui_dist (see ui/vite.config.ts).
FROM node:22-slim AS ui
WORKDIR /app/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN mkdir -p /app/src/ragcentre && npm run build

# Stage 2: build the wheel (with the UI inside) and install it into a virtualenv.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY --from=ui /app/src/ragcentre/ui_dist ./src/ragcentre/ui_dist
RUN uv build --wheel --out-dir /dist \
 && uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install /dist/*.whl

# Stage 3: runtime.
FROM python:3.13-slim
RUN useradd --create-home --uid 1000 ragcentre \
 && mkdir /data && chown ragcentre /data
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    RAGCENTRE_DATA_DIR=/data \
    RAGCENTRE_HOST=0.0.0.0 \
    RAGCENTRE_PORT=8700
USER ragcentre
VOLUME /data
EXPOSE 8700
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/v1/health' % os.environ['RAGCENTRE_PORT'], timeout=2)"
CMD ["ragcentre", "serve"]
