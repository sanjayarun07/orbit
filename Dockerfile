# syntax=docker/dockerfile:1.7

# ---- Stage 1: bundle the wallet/Relay browser clients (web/*.ts -> app/static/*.js)
FROM node:22-alpine AS web
WORKDIR /src
COPY package.json package-lock.json tsconfig.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY web ./web
RUN mkdir -p app/static && npm run build:web

# ---- Stage 2: resolve Python dependencies into an isolated prefix
FROM python:3.11-slim AS deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml ./
# The project is run in place (uvicorn app.main:app), not installed as a
# package, so only its pinned dependencies are materialised.
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > requirements.txt \
 && pip install --prefix=/install -r requirements.txt

# ---- Stage 3: runtime
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8000 \
    UVICORN_WORKERS=1 \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    LOG_LEVEL=INFO \
    ROLE_MEMORY_PATH=/app/data/role_memory.json \
    PROVIDER_OVERRIDES_PATH=/app/data/provider-overrides.json \
    DSPY_CACHEDIR=/app/data/dspy-cache \
    ALLOW_MEMORY_FALLBACK=false
WORKDIR /app
RUN groupadd --system --gid 10001 orbit \
 && useradd --system --uid 10001 --gid orbit --home-dir /app --shell /usr/sbin/nologin orbit \
 && mkdir -p /app/data && chown -R orbit:orbit /app
COPY --from=deps /install /usr/local
COPY --chown=orbit:orbit app ./app
COPY --chown=orbit:orbit mcp.json pyproject.toml ./
COPY --chown=orbit:orbit --from=web /src/app/static/*.js ./app/static/
COPY --chown=orbit:orbit docker/entrypoint.sh /usr/local/bin/orbit-entrypoint
RUN chmod +x /usr/local/bin/orbit-entrypoint
USER orbit
VOLUME ["/app/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/health\", timeout=4).status == 200 else 1)"
ENTRYPOINT ["orbit-entrypoint"]
