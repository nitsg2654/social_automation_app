# SocialFlow Dashboard

Light-theme dashboard for the existing n8n social publishing setup. The application stores its `social_posts` data in MySQL 8.4. n8n's own internal database remains unchanged.

## Configuration

Copy `.env.example` to `.env` once and set strong, unique MySQL root and application passwords plus the PostgreSQL source URL used for migration. Compose reads `.env` automatically. `.env` is ignored by Git. Required settings:

- `MYSQL_ROOT_PASSWORD`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`: MySQL container and application account.
- `MYSQL_PUBLISHED_PORT`: host-only port for the one-time migration tool (default 3307).
- `N8N_GENERATE_WEBHOOK_URL`: existing n8n content-generation webhook.
- `PG_SOURCE_URL`, `PG_SOURCE_SCHEMA`: read-only source for the migration script.
- `MYSQL_MIGRATION_HOST`: normally `127.0.0.1` when running migration from Windows.

## Start MySQL and the dashboard

From PowerShell in this folder:

```powershell
Copy-Item .env.example .env   # only if .env does not already exist
# Edit .env and replace all example credentials and the PostgreSQL source DSN.
docker compose up -d --build
```

MySQL 8.4 stores its files in the persistent `mysql-data` volume. Its port is published on `127.0.0.1` only for migration. The backend waits for the MySQL health check. The initialization SQL creates `social_posts` and the application's idempotency side table on a fresh MySQL volume. Existing PostgreSQL tables and volumes are not modified or removed.

## PostgreSQL to MySQL data migration

The inspected PostgreSQL `social_posts` table has `id`, `batch_id`, `topic`, `platform`, `post_content`, `hashtags`, `image_prompt`, `status`, `scheduled_at`, `posted_at`, `platform_post_id`, `retry_count`, `error_message`, `created_at`, `updated_at`, and `image_url`. The MySQL schema preserves these fields; `id` is `BIGINT AUTO_INCREMENT`, `batch_id` is `CHAR(36)`, and timestamps are `DATETIME(6)` with timezone-aware source values normalized to UTC.

1. Back up PostgreSQL first. For example, use `pg_dump -h 127.0.0.1 -p 5432 -U <source-user> -d <source-database> -t social_posts -Fc -f social_posts.dump`. This is read-only and does not stop the existing application.
2. Confirm `.env` has a correct `PG_SOURCE_URL` and the MySQL target is running: `docker compose up -d mysql`.
3. Install the one-off migration dependencies and run the importer from this folder:

   ```powershell
   python -m venv .venv-migrate
   .\.venv-migrate\Scripts\Activate.ps1
   python -m pip install -r database/migration-requirements.txt
   python database/migrate_postgres_to_mysql.py
   ```

   The importer reads PostgreSQL only, requires an empty MySQL `social_posts` target to prevent accidental overwrite, checks that every source column exists at the destination, copies IDs and rows in batches, and commits the MySQL transaction only after the copy succeeds. It never drops or alters the PostgreSQL database. Verify row counts and sample rows in both databases before switching n8n publishing nodes.
4. Start/rebuild the API: `docker compose up -d --build social-dashboard`.

The source remains intact for rollback. Do not remove the PostgreSQL container or volume until you have verified the migrated data and completed a separate backup/retirement decision.

## n8n integration

The running n8n instance was inspected and its application workflows were updated in place. The active `content generation` workflow no longer has the PostgreSQL insert node and now returns generated content directly for review. Both existing publisher queue workflows now use MySQL nodes and the `SocialFlow MySQL` credential; they remain inactive so the migration cannot cause unexpected external posts. The running n8n container is not replaced, and its internal PostgreSQL configuration remains unchanged. Its application-data credential points to MySQL at `mysql:3306` using the app database credentials. For another n8n deployment, attach its existing container to the app network with `docker network connect social_media_net <n8n-container>` and create an equivalent n8n MySQL credential.

Configure the scheduler's MySQL node to select eligible rows with `status='pending'`. After a successful platform response, have the workflow call `POST http://social-dashboard:8000/api/posts/{id}/publish-result` with JSON such as `{"status":"posted","platform_post_id":"<platform-id>"}`. On a publish failure send `{"status":"failed","error_message":"<error>"}`. This callback updates the existing row. Keep n8n's own PostgreSQL credential and internal database untouched. Generation continues to use the existing webhook, returns platform content to the review UI, and does not insert rows.

## Application behavior and API

Generated content remains temporary in frontend state. Approval inserts it into MySQL as `pending`; decline creates no database row. Each card can regenerate only its own platform. Approval uses a unique idempotency key in `social_approval_keys`, preventing repeat inserts without adding or renaming columns in `social_posts`.

API: `GET /api/health`, `GET /api/dashboard`, `GET /api/posts`, `POST /api/generate`, `POST /api/approve`, `POST /api/decline`, `PATCH /api/posts/{id}`, `POST /api/posts/{id}/retry`, `POST /api/posts/{id}/publish-result`, and `DELETE /api/posts/{id}`.

No login is included; use only on a trusted network until authentication and access controls are added.

## Published post previews and timezones

The dashboard's Published Posts tab reads only rows with `status='posted'`. It supports platform filters, search, paging, image fallback, and links to `platform_post_url` only when a valid HTTP(S) URL was recorded. It refreshes when opened and every 20 seconds while visible. Failed publisher callbacks set `status='failed'`, clear posted metadata, and remain visible in All posts with the publisher error.

`social_posts.platform_post_url` was added with the additive migration in `database/migrations/002_add_platform_post_url.sql`; it does not replace or delete existing rows. On an existing DB, apply that SQL once (the change has already been applied to the current project DB). Fresh MySQL volumes get the column from `database/init/001_social_posts.sql`.

`APP_TIMEZONE` controls the app's scheduling and display timezone (default `Asia/Kolkata`, set to your Windows/system IANA timezone in `.env`). Datetimes are stored as UTC in MySQL `DATETIME`; published API timestamps include the configured local offset, and the browser formats them in that same zone. n8n publisher SQL writes UTC instants with `UTC_TIMESTAMP(6)` and converts local `scheduled_at` values before comparing with UTC. Keep the same `APP_TIMEZONE` in the app and scheduler. Use `POST /api/posts/{id}/publish-result` with `status`, platform-returned `platform_post_id`, optional platform-returned `platform_post_url`, and an optional ISO-8601 `posted_at`. Do not construct a post URL from an ID; omit the URL if the platform response did not provide a valid one.

The current n8n publisher flows are inactive. Their MySQL queue flow's Facebook, Instagram, and LinkedIn nodes return post IDs, but no explicit original-post URL mapping was present in the workflow configuration. Therefore previews will show the link button only after the workflow receives/maps a genuine URL in the API response. Update each successful branch's MySQL update to set `platform_post_url` from that response field only; keep it NULL when absent. Continue setting `status='posted'` only on the success output, and send failures through the `failed` update/callback.

Published posts API: `GET /api/posts/published?platform=Facebook&search=topic&page=1&page_size=9` (`platform`, `search`, and paging parameters are optional).
