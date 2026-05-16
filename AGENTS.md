# Repository Guidelines

## Project Structure & Module Organization

`apps/market-web/` is the public Vue 3 + Vite market. `apps/api/` is the FastAPI backend with route handlers, schemas, auth helpers, storage adapters, and pytest tests. `docs/` contains architecture, security, and OpenAPI notes. Keep server-owned plugin data out of the GitHub repo.

## Build, Test, and Development Commands

- `npm install` installs workspace dependencies.
- `uv sync --project apps/api` installs Python API dependencies.
- `npm run dev:api` starts FastAPI on `127.0.0.1:8787`.
- `npm run dev:web` starts the market UI.
- `npm run build:web` builds the frontend bundle.
- `npm test` or `uv run --project apps/api pytest` runs the API tests.

## Coding Style & Naming Conventions

Use Python 3.11+, four-space indentation, type hints on public API helpers, and small single-purpose functions. Keep plugin IDs in the `astrbot_plugin_<name>` pattern. Route names stay under `/v1/*`, with explicit paths for admin and core-admin actions.

## Testing Guidelines

Tests live in `apps/api/tests/test_*.py` and use pytest plus FastAPI `TestClient`. Cover role checks, GitHub login/session handling, plugin submission, ownership checks, moderation actions, and failure paths. Run `npm test` after API changes and `npm run build:web` after UI changes.

## Commit & Pull Request Guidelines

Use concise imperative commit messages like `Add plugin moderation routes`. PRs should describe the affected area, mention validation commands, and include screenshots for visible UI changes.

## Security & Configuration Tips

Do not commit GitHub OAuth secrets, API keys, Redis data, PostgreSQL dumps, or session cookies. PostgreSQL is the durable market store; Redis is for sessions, OAuth state, cache, and rate limits. The first-run setup creates an internal core admin account; GitHub users never become core admin automatically. Plugin owners may edit only their own listings, while normal admins are limited to moderation actions such as list/unlist, comment removal, and user mute.
