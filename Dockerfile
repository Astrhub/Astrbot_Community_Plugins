FROM node:24-bookworm-slim AS web-build
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src/apps/market-web
RUN apt-get update && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/*
COPY apps/market-web/package*.json ./
ARG NPM_REGISTRY=""
ARG VITE_BASE_URL=""
ENV VITE_BASE_URL=$VITE_BASE_URL \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi \
    && npm install --global npm@11.18.0 \
    && npm ci
COPY apps/market-web/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.9.7 AS uv
FROM docker:29.6.1-cli AS docker-cli

FROM python:3.11-slim AS api-base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/apps/api/.venv \
    PATH="/app/apps/api/.venv/bin:${PATH}"
WORKDIR /app
COPY --from=uv /uv /uvx /usr/local/bin/
COPY apps/api/pyproject.toml apps/api/uv.lock ./apps/api/
ARG PYPI_INDEX_URL=""
RUN if [ -n "$PYPI_INDEX_URL" ]; then \
        uv sync --project apps/api --locked --no-dev --no-install-project \
            --default-index "$PYPI_INDEX_URL"; \
    else \
        uv sync --project apps/api --locked --no-dev --no-install-project; \
    fi
COPY apps/api ./apps/api
RUN groupadd --gid 10001 astrbot-market \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin astrbot-market \
    && mkdir -p /var/lib/astrbot-market/artifacts /var/lib/astrbot-runtime-results \
    && chown -R 10001:10001 /var/lib/astrbot-market /var/lib/astrbot-runtime-results
WORKDIR /app/apps/api

FROM api-base AS api
COPY --from=web-build /src/apps/market-web/dist /app/apps/market-web/dist
EXPOSE 8787
USER 10001:10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]

FROM api-base AS runtime-runner
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
USER 10001:10001
CMD ["python", "-m", "app.runtime_runner"]
