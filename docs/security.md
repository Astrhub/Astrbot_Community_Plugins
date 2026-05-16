# Security

## Identity

GitHub OAuth is the only supported login path. The first authenticated user becomes the core admin. Later users gain permissions only through explicit admin promotion or verified repository ownership.

## Permissions

- Core admin can grant or remove admins and publish announcements.
- Normal admins can list/unlist plugins, remove comments, mute users, and moderate submissions.
- Plugin owners can edit only their own plugin metadata.

## API Keys

API keys are for machine clients such as the future AstrBot WebUI plugin. Keys should be scoped, revocable, and logged. Use `Authorization: Bearer <key>`.

## Data Safety

Treat plugin metadata, README content, and comments as untrusted input. Validate GitHub repository URLs, sanitize rendered markdown, and store moderation actions server-side.

## Storage

PostgreSQL is the durable store for market data. Redis is the short-lived store for session tokens, OAuth state, cache, and rate limits. Neither should contain GitHub secrets or raw OAuth tokens beyond the minimum required for the login flow.

## First-Run Setup

If the infrastructure URLs are missing on first boot, the UI may collect them once and write them to `apps/api/data/runtime.env`. After that, only the core admin should be able to change them.
