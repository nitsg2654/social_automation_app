# SocialFlow Dashboard

Light-theme dashboard for the existing n8n + PostgreSQL social publishing setup.

## Run with Docker Desktop on Windows

1. Ensure PostgreSQL is reachable on port 5432 and n8n is reachable on port 5678 from the Docker host.
2. Configure `.env` once. Copy `.env.example` to `.env` and set `DATABASE_URL` and `N8N_GENERATE_WEBHOOK_URL`. Docker Compose loads `.env` automatically on every run, so no per-session PowerShell variables are needed. Keep `.env` private; it is excluded from version control. If n8n runs as another Compose service on the same Docker network, use its service name in the URL, such as `http://n8n:5678/webhook/generate-social-post`.
3. Run `docker compose up -d --build` from the project folder and open http://localhost:8000.
4. Activate the n8n production `/webhook/` workflow and ensure it returns JSON containing one object per requested platform with `platform`, `post_content` (or `content` / `text`), `hashtags`, and `image_prompt`. The generation workflow must not insert into `social_posts`; publishing remains handled by the existing publisher workflow.

Generated, unapproved content exists only in browser memory and is lost on refresh. Approve each platform card to insert it as `approved`; configure the existing n8n scheduler workflow to select `approved` posts and then publish them; declines are removed from review without database writes. Regenerate retries only that card's platform. A failed approval remains available to retry. The app detects existing `social_posts` columns at approval time and inserts only recognized metadata fields; the existing table must provide `id`, `topic`, `platform`, `post_content`, and `status`. It does not create, alter, or drop tables. Duplicate approval is protected with a transaction advisory lock and existing `approval_key` if the schema has one; on schemas without that optional column, exact identical approved content is treated as an already-saved approval.

Features include the approval review flow, dashboard stats, post search/edit/status/schedule, retry to pending, delete, scheduled posts, and activity logs. API: GET `/api/health`, GET `/api/dashboard`, GET `/api/posts`, POST `/api/generate`, POST `/api/approve`, PATCH `/api/posts/{id}`, POST `/api/posts/{id}/retry`, DELETE `/api/posts/{id}`.

No login is included; this app is intended for local/trusted use, not public internet deployment. Add authentication, HTTPS, and access controls before public exposure.
