# Architecture

The market is split into two deployable parts.

1. `apps/market-web/` renders the public market, theme system, plugin submission form, and user-facing browsing pages.
2. `apps/api/` exposes a FastAPI backend for GitHub OAuth, plugin CRUD, moderation, comments, likes, announcements, and API-key endpoints.

The source of truth for plugin records is the market server and its database, not GitHub. The repository only stores the application code and API contract. GitHub OAuth is used to identify whether a signed-in user owns a plugin repository or belongs to a trusted admin organization.

Authorization model:

- first authenticated user becomes core admin
- core admin can grant/remove admins and publish announcements
- normal admins can list/unlist plugins, delete comments, mute users, and handle moderation
- plugin owners can edit their own metadata after ownership checks succeed

Persistence and infrastructure:

- PostgreSQL stores users, plugins, submissions, comments, announcements, and audit data.
- Redis stores sessions, OAuth state, cache entries, and rate-limiting counters.
- First launch can be completed from the web UI `/setup` page, which writes `apps/api/data/runtime.env` and asks for an API restart.

The separate AstrBot WebUI plugin will consume this API through API keys and should not duplicate market state locally.
