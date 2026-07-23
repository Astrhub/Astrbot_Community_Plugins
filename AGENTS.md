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

After route, navigation, head metadata, sitemap, prerender, or cache-header changes, verify a unique title, description, canonical and absolute Open Graph image; exactly one H1; crawlable `<a href>` navigation; query-backed search and pagination; listed plugin URLs in sitemap and prerender output; `noindex,nofollow` on private routes; a real 404 for unknown paths; and the expected Cache-Control class for HTML, assets, APIs, plugin feeds, and crawler files.

## AGENTS.md Maintenance

Keep this file limited to long-lived repository rules. Put one-time plans, migrations, audits, and temporary operational notes in a dedicated document under `docs/`, delete them when obsolete, and reference them here in at most one sentence when a durable pointer is useful.

## SEO Requirements

SEO requirements are merge-blocking for public UI changes.

- Indexable content must have its own URL route; never make a modal or drawer the only entry. Admin, setup, settings, personal, and notification routes must emit `noindex,nofollow`.
- Internal navigation must use `<router-link>` or `<a href>`, never click-only navigation. Search and pagination state must be represented in URL query parameters.
- Every public page must use `@unhead/vue` `useHead` through the local SEO helper to set a unique Chinese `{页面名} - {品牌名}` title, description, self-referencing canonical URL, and Open Graph metadata. Plugin details require `SoftwareApplication` JSON-LD; the homepage requires `WebSite` plus `SearchAction`.
- `og:image` must be an absolute URL to a 1200x630 PNG. Relative URLs and SVG Open Graph images are forbidden.
- Every page must contain exactly one H1: the homepage positioning statement or the plugin display name. Images require descriptive alt text.
- The sitemap must be generated at build time from `/v1/plugins`. Production builds must inject `VITE_BASE_URL=https://plugins.eloina.cn`; otherwise sitemap and prerender output silently degrade.
- Unknown paths must return an actual HTTP 404. A blanket SPA fallback with status 200 is forbidden.
- Public content pages must pass the prerender snapshot flow. Add every new public route to both sitemap and prerender route lists.
- Head metadata has one source of truth: `index.html` provides defaults, route-level `useHead` owns runtime metadata, site configuration must not overwrite it, backend brand defaults must match the static title, and favicon MIME type must match its file.

## Cache Requirements

The origin must explicitly classify every response with Cache-Control; CDN behavior is fallback only and must not override origin policy. Fingerprinted `/assets/*` files use `public, max-age=31536000, immutable`; HTML uses `public, max-age=0, must-revalidate`; APIs default to `private, no-store` with explicitly reviewed public exceptions; plugin feeds use `public, max-age=300`; and sitemap/robots/llms files use `public, max-age=3600`. Every new endpoint or static directory must be assigned a class and covered by a response-header test.

## Commit & Pull Request Guidelines

Recent commits use concise imperative subjects, for example `Improve plugin card display and logo fallback` or `Add role-aware API docs endpoints`. Keep commits focused. PRs should describe the affected API or UI surface, link issues when available, list validation commands, and include screenshots for visible UI changes.

## Security & Configuration Tips

Do not commit GitHub OAuth secrets, API keys, Redis data, PostgreSQL dumps, session cookies, `.env` files, `node_modules/`, `.venv/`, or build output. PostgreSQL is the durable market store; Redis handles sessions, OAuth state, cache, and rate limits. First-run setup creates the internal core admin account; GitHub users must not become core admins automatically. Plugin owners may edit only their own listings, while normal admins are limited to moderation actions.
