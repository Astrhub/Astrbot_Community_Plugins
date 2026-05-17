FROM node:24-bookworm-slim AS web-build
WORKDIR /src/apps/market-web
COPY apps/market-web/package*.json ./
RUN npm ci
COPY apps/market-web/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.9.7 AS uv

FROM python:3.11-slim AS api
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/apps/api/.venv \
    PATH="/app/apps/api/.venv/bin:${PATH}"
WORKDIR /app
COPY --from=uv /uv /uvx /usr/local/bin/
COPY apps/api/pyproject.toml apps/api/uv.lock ./apps/api/
RUN uv sync --project apps/api --locked --no-dev --no-install-project
COPY apps/api ./apps/api
COPY --from=web-build /src/apps/market-web/dist ./apps/market-web/dist
EXPOSE 8787
WORKDIR /app/apps/api
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
