# Repository Guidelines

## Project Structure & Module Organization

`apps/market-web/` contains the public Vue 3 market UI managed by Vite Plus (`vp`). Source lives in `src/`, with `components/`, `views/`, `router/`, `stores/`, `utils/`, `types/`, and `assets/theme.css`; static assets belong in `public/`. `apps/api/` contains the FastAPI service, with `app/main.py`, shared schemas, auth, configuration, OpenAPI filtering, and storage helpers. API tests live in `apps/api/tests/`. Project documentation is under `docs/`. Keep server-owned plugin data and runtime state out of this repository.

## Build, Test, and Development Commands

- `npm install` installs workspace and frontend dependencies.
- `uv sync --project apps/api` installs API dependencies for Python 3.11+.
- `npm run dev:web` starts the market UI.
- `npm run dev:api` starts FastAPI on `127.0.0.1:8787`.
- `npm run build:web` builds the frontend bundle.
- `npm test` or `npm run test:api` runs the API pytest suite.
- `npm --prefix apps/market-web test` runs frontend Vitest tests.

Frontend tooling is routed through Vite Plus. Use the npm scripts above, or the package scripts in `apps/market-web/package.json` (`vp dev`, `vp build`, `vp preview`, `vp test --run`); do not call `vite` or `vitest` directly. `apps/market-web/package.json` pins the npm version through `devEngines`, so use the required npm version when local npm rejects a frontend command.

## Coding Style & Naming Conventions

Use small, single-purpose modules and prefer existing local patterns. Python code uses four-space indentation, type hints for public helpers, Ruff with a 100-character line length, and Python 3.11 syntax. Vue code should use TypeScript, Composition API patterns, Pinia for shared state, and Vue Router for navigation. Keep plugin IDs in the `astrbot_plugin_<name>` pattern. API routes stay under `/v1/*`, with explicit admin and core-admin paths.

## Testing Guidelines

Backend tests use pytest and FastAPI `TestClient`; name files `apps/api/tests/test_*.py`. Cover role checks, GitHub login and session handling, plugin submission, ownership checks, moderation actions, OpenAPI filtering, and failure paths. Frontend tests use Vitest with `*.test.ts` naming, as in `src/utils/github.test.ts`. Run API tests after backend changes and `npm run build:web` after visible UI or routing changes.

## Commit & Pull Request Guidelines

Recent commits use concise imperative subjects, for example `Improve plugin card display and logo fallback` or `Add role-aware API docs endpoints`. Keep commits focused. PRs should describe the affected API or UI surface, link issues when available, list validation commands, and include screenshots for visible UI changes.

## Security & Configuration Tips

Do not commit GitHub OAuth secrets, API keys, Redis data, PostgreSQL dumps, session cookies, `.env` files, `node_modules/`, `.venv/`, or build output. PostgreSQL is the durable market store; Redis handles sessions, OAuth state, cache, and rate limits. First-run setup creates the internal core admin account; GitHub users must not become core admins automatically. Plugin owners may edit only their own listings, while normal admins are limited to moderation actions.
