import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

import httpx
import mysql.connector
from mysql.connector import Error as MySQLError
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logger = logging.getLogger("socialflow")
HOOK = os.getenv("N8N_GENERATE_WEBHOOK_URL", "http://n8n:5678/webhook/generate-social-post")
PLATFORMS = {"facebook": "Facebook", "linkedin": "LinkedIn", "instagram": "Instagram"}
app = FastAPI(title="SocialFlow API")


@contextmanager
def db():
    config = {
        "host": os.getenv("DB_HOST", "mysql"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "social_app"),
        "password": os.getenv("DB_PASSWORD", ""),
        "database": os.getenv("DB_NAME", "n8n_social"),
        "connection_timeout": 8,
        "charset": "utf8mb4",
        "collation": "utf8mb4_0900_ai_ci",
    }
    if not config["password"]:
        raise RuntimeError("DB_PASSWORD is not configured")
    conn = mysql.connector.connect(**config)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_one(conn, sql, params=()):
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(sql, params)
        return cur.fetchone()
    finally:
        cur.close()


def fetch_all(conn, sql, params=()):
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        cur.close()


def execute(conn, sql, params=()):
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        return {"rowcount": cur.rowcount, "lastrowid": cur.lastrowid}
    finally:
        cur.close()


def columns(conn):
    rows = fetch_all(conn, "SELECT COLUMN_NAME AS column_name FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name=%s", ("social_posts",))
    return {r["column_name"] for r in rows}


def mysql_datetime(value):
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


@app.on_event("startup")
def init_db():
    # A side table gives approvals a unique idempotency key without changing the
    # existing social_posts schema or its publisher-facing columns.
    try:
        with db() as conn:
            execute(conn, """CREATE TABLE IF NOT EXISTS social_approval_keys (
                idempotency_key CHAR(36) NOT NULL PRIMARY KEY,
                post_id BIGINT NOT NULL,
                created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                UNIQUE KEY uq_social_approval_post (post_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""")
            existing = fetch_one(conn, "SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s", ("social_posts",))
            if not existing or not existing["n"]:
                logger.warning("social_posts does not exist yet; apply database/init/001_social_posts.sql or import PostgreSQL data")
    except Exception:
        logger.exception("MySQL startup check failed")


class Generate(BaseModel):
    topic: str = Field(min_length=2, max_length=500)
    platforms: list[str] = Field(min_length=1, max_length=3)
    image_url: Optional[str] = None


class Approval(BaseModel):
    idempotency_key: UUID
    topic: str = Field(min_length=2, max_length=500)
    platform: str
    post_content: str = Field(min_length=1, max_length=20000)
    hashtags: Optional[str] = Field(default=None, max_length=5000)
    image_prompt: Optional[str] = Field(default=None, max_length=5000)
    image_url: Optional[str] = Field(default=None, max_length=2000)


class Decline(BaseModel):
    idempotency_key: UUID
    platform: str


class Update(BaseModel):
    topic: Optional[str] = None
    platform: Optional[str] = None
    post_content: Optional[str] = None
    hashtags: Optional[str] = None
    image_prompt: Optional[str] = None
    image_url: Optional[str] = None
    status: Optional[str] = None
    scheduled_at: Optional[datetime] = None


class PublishResult(BaseModel):
    status: str = Field(pattern="^(posted|failed)$")
    platform_post_id: Optional[str] = None
    error_message: Optional[str] = None
    posted_at: Optional[datetime] = None


@app.get("/api/health")
def health():
    try:
        with db() as conn:
            fetch_one(conn, "SELECT 1 AS ok")
        return {"ok": True, "database": "connected", "engine": "mysql"}
    except Exception:
        logger.exception("MySQL health check failed")
        return {"ok": False, "database": "unavailable", "engine": "mysql"}


@app.get("/api/dashboard")
def dashboard():
    try:
        with db() as conn:
            stats = fetch_one(conn, """SELECT COUNT(*) AS total,
                COALESCE(SUM(status='pending'),0) AS pending,
                COALESCE(SUM(status='posted'),0) AS posted,
                COALESCE(SUM(status='failed'),0) AS failed FROM social_posts""")
            platforms = fetch_all(conn, "SELECT platform, COUNT(*) AS total, COALESCE(SUM(status='posted'),0) AS posted FROM social_posts GROUP BY platform ORDER BY platform")
            recent = fetch_all(conn, "SELECT * FROM social_posts ORDER BY created_at DESC LIMIT 8")
        return {"stats": stats, "platforms": platforms, "recent": recent}
    except MySQLError as e:
        logger.exception("Dashboard query failed")
        raise HTTPException(503, "Could not load dashboard from MySQL. Verify the database schema and connection.") from e


@app.get("/api/posts")
def posts(status: Optional[str] = None, platform: Optional[str] = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    conditions, args = [], []
    if status:
        conditions.append("status=%s")
        args.append(status)
    if platform:
        conditions.append("LOWER(platform)=LOWER(%s)")
        args.append(platform)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    try:
        with db() as conn:
            items = fetch_all(conn, f"SELECT * FROM social_posts{where} ORDER BY created_at DESC LIMIT %s OFFSET %s", (*args, limit, offset))
            total = fetch_one(conn, f"SELECT COUNT(*) AS total FROM social_posts{where}", args)["total"]
        return {"items": items, "total": total}
    except MySQLError as e:
        logger.exception("Post listing query failed")
        raise HTTPException(503, "Could not load posts from MySQL. Verify the database schema and connection.") from e


def normalize_generated(body):
    if isinstance(body, list) and len(body) == 1 and isinstance(body[0], dict):
        body = body[0]
    if isinstance(body, dict) and isinstance(body.get("data"), (dict, list)):
        body = body["data"]
    if isinstance(body, dict) and isinstance(body.get("output"), (dict, list)):
        body = body["output"]
    if isinstance(body, dict) and isinstance(body.get("text"), str):
        body = body["text"]
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError as e:
            raise HTTPException(502, "Generation webhook returned unstructured text.") from e
    items = body if isinstance(body, list) else body.get("posts") if isinstance(body, dict) and isinstance(body.get("posts"), list) else [body]
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_platform = item.get("platform", item.get("Platform", item.get("social_platform")))
        content = item.get("post_content", item.get("content", item.get("text", item.get("Post Content"))))
        platform = PLATFORMS.get(str(raw_platform).strip().lower()) if raw_platform else None
        if platform and isinstance(content, str) and content.strip():
            result.append({"platform": platform, "post_content": content.strip(), "hashtags": item.get("hashtags", item.get("Hashtags")) or "", "image_prompt": item.get("image_prompt", item.get("Image Prompt")) or ""})
    return result


@app.post("/api/generate")
async def generate(x: Generate):
    requested = list(dict.fromkeys(str(p).strip().lower() for p in x.platforms if str(p).strip()))
    if not requested or any(p not in PLATFORMS for p in requested):
        raise HTTPException(400, "Use Facebook, LinkedIn, and/or Instagram.")
    platforms = [PLATFORMS[p] for p in requested]
    payload = {"topic": x.topic.strip(), "platforms": platforms}
    if x.image_url:
        payload["image_url"] = x.image_url
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(HOOK, json=payload)
        if response.is_error:
            logger.error("n8n generation returned HTTP %s: %s", response.status_code, response.text[:1000])
            raise HTTPException(502, f"Generation webhook returned HTTP {response.status_code}. Check n8n execution logs.")
        if not response.content or not response.text.strip():
            raise HTTPException(502, "Generation webhook returned an empty response. Configure the n8n Webhook node to return the generated JSON.")
        try:
            raw = response.json()
        except ValueError as e:
            logger.error("n8n returned invalid JSON content-type=%s body=%s", response.headers.get("content-type", ""), response.text[:500])
            raise HTTPException(502, "Generation webhook returned invalid JSON. Check the n8n Webhook response body.") from e
        generated = normalize_generated(raw)
        by_platform = {post["platform"].lower(): post for post in generated}
        missing = [PLATFORMS[p] for p in requested if p not in by_platform]
        if missing:
            raise HTTPException(502, "Generation response was missing valid content for: " + ", ".join(missing))
        return {"ok": True, "posts": [by_platform[p] for p in requested]}
    except HTTPException:
        raise
    except httpx.TimeoutException as e:
        raise HTTPException(504, "Generation webhook timed out after 180 seconds.") from e
    except httpx.HTTPError as e:
        logger.exception("n8n generation webhook request failed")
        raise HTTPException(502, f"Could not reach the generation webhook ({type(e).__name__}). Verify N8N_GENERATE_WEBHOOK_URL.") from e


@app.post("/api/approve")
def approve(x: Approval):
    platform = PLATFORMS.get(x.platform.strip().lower())
    if not platform:
        raise HTTPException(422, "Platform must be Facebook, LinkedIn, or Instagram.")
    key = str(x.idempotency_key)
    try:
        with db() as conn:
            prior = fetch_one(conn, "SELECT post_id FROM social_approval_keys WHERE idempotency_key=%s", (key,))
            if prior:
                saved = fetch_one(conn, "SELECT id, image_url FROM social_posts WHERE id=%s", (prior["post_id"],))
                return {"ok": True, "id": prior["post_id"], "image_url": saved.get("image_url") if saved else None, "status": "pending", "message": "Already approved", "duplicate": True}
            cols = columns(conn)
            required = {"id", "batch_id", "topic", "platform", "post_content", "status"}
            if not required.issubset(cols):
                raise HTTPException(503, "social_posts is missing required columns: " + ", ".join(sorted(required - cols)))
            if x.image_url and "image_url" not in cols:
                raise HTTPException(503, "Cannot save image URL: social_posts has no image_url column.")
            values = {"batch_id": str(uuid4()), "topic": x.topic.strip(), "platform": platform, "post_content": x.post_content.strip(), "status": "pending"}
            for optional in ("hashtags", "image_prompt", "image_url"):
                if optional in cols:
                    values[optional] = getattr(x, optional)
            if "updated_at" in cols:
                values["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
            names = list(values)
            placeholders = ", ".join(["%s"] * len(names))
            execute(conn, f"INSERT INTO social_posts ({', '.join(names)}) VALUES ({placeholders})", [values[n] for n in names])
            inserted = fetch_one(conn, "SELECT LAST_INSERT_ID() AS id")
            post_id = inserted["id"]
            execute(conn, "INSERT INTO social_approval_keys (idempotency_key, post_id) VALUES (%s, %s)", (key, post_id))
            saved = fetch_one(conn, "SELECT id, image_url, status FROM social_posts WHERE id=%s", (post_id,))
            if x.image_url and saved.get("image_url") != x.image_url:
                raise HTTPException(503, "MySQL did not persist the image URL; approval was rolled back.")
        return {"ok": True, "id": post_id, "image_url": saved.get("image_url"), "status": "pending", "message": "Post approved and saved as pending", "duplicate": False}
    except HTTPException:
        raise
    except mysql.connector.IntegrityError as e:
        # A concurrent request may have won the unique idempotency key race.
        try:
            with db() as conn:
                prior = fetch_one(conn, "SELECT post_id FROM social_approval_keys WHERE idempotency_key=%s", (key,))
                if prior:
                    return {"ok": True, "id": prior["post_id"], "status": "pending", "message": "Already approved", "duplicate": True}
        except MySQLError:
            logger.exception("Could not resolve concurrent approval retry")
        raise HTTPException(503, "Could not save approval. Check for database constraints and retry.") from e
    except MySQLError as e:
        logger.exception("MySQL approval insert failed")
        raise HTTPException(503, "Could not save the approved post. Check MySQL connectivity and schema.") from e


@app.post("/api/decline")
def decline(x: Decline):
    if x.platform.strip().lower() not in PLATFORMS:
        raise HTTPException(422, "Platform must be Facebook, LinkedIn, or Instagram.")
    # Declines are acknowledged without creating or changing any database rows.
    return {"ok": True, "declined": True, "message": "Content declined; no database row was created."}


@app.patch("/api/posts/{pid}")
def update(pid: int, x: Update):
    values = x.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(400, "No changes supplied")
    if "status" in values and values["status"] not in {"pending", "approved", "queued", "posted", "failed", "draft"}:
        raise HTTPException(400, "Invalid status")
    try:
        with db() as conn:
            cols = columns(conn)
            unknown = set(values) - cols
            if unknown:
                raise HTTPException(400, "Unsupported social_posts columns: " + ", ".join(sorted(unknown)))
            if "updated_at" in cols:
                values["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
            if "scheduled_at" in values:
                values["scheduled_at"] = mysql_datetime(values["scheduled_at"])
            sets = ", ".join(f"{name}=%s" for name in values)
            result = execute(conn, f"UPDATE social_posts SET {sets} WHERE id=%s", (*values.values(), pid))
            if not result["rowcount"]:
                exists = fetch_one(conn, "SELECT id FROM social_posts WHERE id=%s", (pid,))
                if not exists:
                    raise HTTPException(404, "Post not found")
            return fetch_one(conn, "SELECT * FROM social_posts WHERE id=%s", (pid,))
    except HTTPException:
        raise
    except MySQLError as e:
        logger.exception("Post update failed")
        raise HTTPException(503, "Could not update post in MySQL.") from e


@app.post("/api/posts/{pid}/retry")
def retry(pid: int):
    try:
        with db() as conn:
            cols = columns(conn)
            reset = {"status": "pending", "retry_count": 0, "error_message": None, "posted_at": None, "platform_post_id": None}
            reset = {name: value for name, value in reset.items() if name in cols}
            if "updated_at" in cols:
                reset["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
            sets = ", ".join(f"{name}=%s" for name in reset)
            result = execute(conn, f"UPDATE social_posts SET {sets} WHERE id=%s", (*reset.values(), pid))
            if not result["rowcount"]:
                exists = fetch_one(conn, "SELECT id FROM social_posts WHERE id=%s", (pid,))
                if not exists:
                    raise HTTPException(404, "Post not found")
            return fetch_one(conn, "SELECT * FROM social_posts WHERE id=%s", (pid,))
    except HTTPException:
        raise
    except MySQLError as e:
        logger.exception("Post retry failed")
        raise HTTPException(503, "Could not retry post in MySQL.") from e


@app.post("/api/posts/{pid}/publish-result")
def publish_result(pid: int, x: PublishResult):
    """Callback for n8n after a platform publish attempt; update the existing row."""
    try:
        with db() as conn:
            cols = columns(conn)
            values = {"status": x.status}
            if "platform_post_id" in cols:
                values["platform_post_id"] = x.platform_post_id
            if "error_message" in cols:
                values["error_message"] = x.error_message
            if "posted_at" in cols and x.status == "posted":
                values["posted_at"] = mysql_datetime(x.posted_at) or datetime.now(timezone.utc).replace(tzinfo=None)
            if "updated_at" in cols:
                values["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
            sets = ", ".join(f"{name}=%s" for name in values)
            result = execute(conn, f"UPDATE social_posts SET {sets} WHERE id=%s", (*values.values(), pid))
            if not result["rowcount"]:
                exists = fetch_one(conn, "SELECT id FROM social_posts WHERE id=%s", (pid,))
                if not exists:
                    raise HTTPException(404, "Post not found")
            return {"ok": True, "post": fetch_one(conn, "SELECT * FROM social_posts WHERE id=%s", (pid,))}
    except HTTPException:
        raise
    except MySQLError as e:
        logger.exception("Publish result callback failed")
        raise HTTPException(503, "Could not update publishing status in MySQL.") from e


@app.delete("/api/posts/{pid}")
def delete(pid: int):
    try:
        with db() as conn:
            execute(conn, "DELETE FROM social_approval_keys WHERE post_id=%s", (pid,))
            result = execute(conn, "DELETE FROM social_posts WHERE id=%s", (pid,))
            if not result["rowcount"]:
                raise HTTPException(404, "Post not found")
        return {"ok": True, "id": pid}
    except HTTPException:
        raise
    except MySQLError as e:
        logger.exception("Post deletion failed")
        raise HTTPException(503, "Could not delete post from MySQL.") from e


app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/")
def home():
    return FileResponse("frontend/index.html")
