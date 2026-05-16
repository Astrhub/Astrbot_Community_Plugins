# Astrbot Community Plugins

Astrbot Community Plugins is a server-backed community market for AstrBot plugins. The GitHub repository only hosts the market source code and API contract; plugin records, comments, likes, moderation state, and accounts live on the market server.

## Layout

- `apps/market-web/` - Vue 3 + Vite site for browsing, searching, and submitting plugins.
- `apps/api/` - FastAPI backend with GitHub OAuth, role checks, and moderation endpoints.
- `docs/` - architecture, security, and OpenAPI docs.

## Development

```bash
uv sync --project apps/api
npm install --prefix apps/market-web
npm run dev:api
npm run dev:web
```

Frontend environment variables are defined in `apps/market-web/.env.example`:

- `VITE_BASE_URL` - public FastAPI base URL. The website uses it for API calls, the copy button appends `/plugins.json`, and sitemap generation uses the same host.

If `VITE_BASE_URL` is empty, the site uses the current browser origin and skips sitemap generation.

Backend uses Python 3.11+ and `uv`:

```bash
cd apps/api
uv sync
uv run uvicorn app.main:app --reload
uv run pytest
```

On first launch, if `DATABASE_URL` or `REDIS_URL` is missing, the web UI opens `/setup` and writes the values into `apps/api/data/runtime.env`.

## Identity and Roles

- The first GitHub user becomes the core admin.
- Core admin can grant or remove admins and publish announcements.
- Normal admins can list/unlist plugins, delete comments, mute users, and handle plugin moderation.
- Plugin owners can edit their own plugin metadata after GitHub ownership is verified.

## Integration Notes

The future AstrBot WebUI plugin will call the public API with an API key. The market site supports GitHub OAuth only for user login and moderation actions. Plugin submission happens on the website form, not through GitHub Issues.

AstrBot itself can consume this market as a custom plugin source. Add this URL in AstrBot WebUI:

```text
https://your-market-domain/plugins.json
```

The feed matches AstrBot's current custom registry format: a JSON object keyed by plugin name, with `name`, `display_name`, `desc`, `author`, `repo`, `tags`, `version`, `logo`, `stars`, `updated_at`, `download_url`, `astrbot_version`, `category`, and `support_platforms`. The API also exposes `/plugins-md5.json` for AstrBot's source cache validation.

PostgreSQL will hold persistent market data. Redis will hold sessions, OAuth state, caching, and rate-limiting state.
