import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DB = os.getenv("DATABASE_URL", "")
HOOK = os.getenv("N8N_GENERATE_WEBHOOK_URL", "http://host.docker.internal:5678/webhook/generate-social-post")
app = FastAPI(title="SocialFlow API")
logger = logging.getLogger("socialflow.generate")


def db():
    if not DB:
        raise RuntimeError('DATABASE_URL is not configured')
    return psycopg.connect(DB, row_factory=dict_row, connect_timeout=8)


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


class Update(BaseModel):
    topic: Optional[str] = None
    platform: Optional[str] = None
    post_content: Optional[str] = None
    hashtags: Optional[str] = None
    image_prompt: Optional[str] = None
    image_url: Optional[str] = None
    status: Optional[str] = None
    scheduled_at: Optional[datetime] = None


PLATFORMS = {"facebook": "Facebook", "linkedin": "LinkedIn", "instagram": "Instagram"}


@app.get('/api/health')
def health():
    try:
        with db() as c:
            c.execute('SELECT 1')
        return {'ok': True, 'database': 'connected'}
    except Exception:
        return {'ok': False, 'database': 'unavailable'}


@app.get('/api/dashboard')
def dashboard():
    try:
        with db() as c:
            s = c.execute("SELECT COUNT(*) total,COUNT(*) FILTER(WHERE status IN ('pending','approved')) pending,COUNT(*) FILTER(WHERE status='posted') posted,COUNT(*) FILTER(WHERE status='failed') failed FROM social_posts").fetchone()
            platforms = c.execute("SELECT platform,COUNT(*) total,COUNT(*) FILTER(WHERE status='posted') posted FROM social_posts GROUP BY platform ORDER BY platform").fetchall()
            recent = c.execute('SELECT * FROM social_posts ORDER BY created_at DESC LIMIT 8').fetchall()
        return {'stats': s, 'platforms': platforms, 'recent': recent}
    except Exception:
        raise HTTPException(503, 'Could not load dashboard from the configured database.')


@app.get('/api/posts')
def posts(status: Optional[str] = None, platform: Optional[str] = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    try:
        cond, args = [], []
        if status:
            cond.append('status=%s'); args.append(status)
        if platform:
            cond.append('LOWER(platform)=LOWER(%s)'); args.append(platform)
        w = ' WHERE ' + ' AND '.join(cond) if cond else ''
        with db() as c:
            items = c.execute(f'SELECT * FROM social_posts{w} ORDER BY created_at DESC LIMIT %s OFFSET %s', (*args, limit, offset)).fetchall()
            total = c.execute(f'SELECT COUNT(*) total FROM social_posts{w}', args).fetchone()['total']
        return {'items': items, 'total': total}
    except Exception:
        raise HTTPException(503, 'Could not load posts from the configured database.')


def normalize_generated(body):
    # Support common n8n response envelopes while rejecting an unrecognized shape.
    if isinstance(body, list) and len(body) == 1 and isinstance(body[0], dict):
        body = body[0]
    if isinstance(body, dict) and isinstance(body.get('data'), (dict, list)):
        body = body['data']
    if isinstance(body, dict) and isinstance(body.get('output'), (dict, list)):
        body = body['output']
    # n8n AI Agent nodes commonly return a JSON string in `output` or `text`.
    if isinstance(body, dict) and isinstance(body.get('text'), str):
        body = body['text']
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            raise HTTPException(502, 'Generation webhook returned unstructured text.')
    items = body if isinstance(body, list) else body.get('posts') if isinstance(body, dict) and isinstance(body.get('posts'), list) else [body]
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        platform = item.get('platform', item.get('Platform', item.get('social_platform')))
        content = item.get('post_content', item.get('content', item.get('text', item.get('Post Content'))))
        if platform and isinstance(content, str) and content.strip():
            platform_name = PLATFORMS.get(str(platform).strip().lower())
            if platform_name:
                result.append({'platform': platform_name, 'post_content': content.strip(), 'hashtags': item.get('hashtags', item.get('Hashtags')) or '', 'image_prompt': item.get('image_prompt', item.get('Image Prompt')) or ''})
    return result


@app.post('/api/generate')
async def generate(x: Generate):
    ps = list(dict.fromkeys(PLATFORMS[p.strip().lower()] for p in x.platforms if p.strip().lower() in PLATFORMS))
    if not ps or len(ps) != len({p.strip().lower() for p in x.platforms if p.strip()}):
        raise HTTPException(400, 'Use Facebook, LinkedIn, and/or Instagram.')
    payload = {'topic': x.topic.strip(), 'platforms': ps}
    if x.image_url:
        payload['image_url'] = x.image_url
    try:
        async with httpx.AsyncClient(timeout=180) as h:
            r = await h.post(HOOK, json=payload)
        if r.is_error:
            # Log a bounded response preview to aid diagnosis; do not return it because
            # webhook responses can contain internal workflow details or secrets.
            logger.error("n8n generation webhook returned HTTP %s: %s", r.status_code, r.text[:1000])
            raise HTTPException(502, f'Generation webhook returned HTTP {r.status_code}. Check the configured n8n webhook URL and workflow execution logs.')
        if not r.content or not r.text.strip():
            logger.error("n8n generation webhook returned an empty body (HTTP %s, content-type %s)", r.status_code, r.headers.get('content-type', ''))
            raise HTTPException(502, 'Generation webhook returned an empty response body. In n8n, configure the Webhook node to respond after the generation chain and return the generated JSON in its response body.')
        try:
            raw = r.json()
        except ValueError as e:
            preview = r.text[:500]
            logger.error("n8n generation webhook returned invalid JSON (HTTP %s, content-type %s, bytes %s): %s", r.status_code, r.headers.get('content-type', ''), len(r.content), preview)
            raise HTTPException(502, f'Generation webhook returned an invalid JSON body (HTTP {r.status_code}, content-type {r.headers.get("content-type", "unknown")}, {len(r.content)} bytes). Check the Webhook response body configuration.') from e
        try:
            generated = normalize_generated(raw)
        except HTTPException as e:
            logger.error("n8n generation response shape was invalid: %s", str(raw)[:1000])
            raise e
        by_platform = {p['platform'].lower(): p for p in generated}
        missing = [p for p in ps if p.lower() not in by_platform]
        if missing:
            logger.error("n8n response missing requested platform(s) %s; top-level type=%s keys=%s", missing, type(raw).__name__, list(raw[0].keys()) if isinstance(raw, list) and raw and isinstance(raw[0], dict) else list(raw.keys()) if isinstance(raw, dict) else [])
            raise HTTPException(502, 'Generation response was missing valid content for: ' + ', '.join(missing))
        return {'ok': True, 'posts': [by_platform[p.lower()] for p in ps]}
    except HTTPException:
        raise
    except httpx.TimeoutException as e:
        logger.exception("n8n generation webhook timed out")
        raise HTTPException(504, 'Generation webhook timed out. Check that the n8n workflow completed and responded within 180 seconds.') from e
    except httpx.HTTPError as e:
        logger.exception("n8n generation webhook request failed")
        raise HTTPException(502, f'Could not reach the generation webhook ({type(e).__name__}). Verify N8N_GENERATE_WEBHOOK_URL and network access from the app container.') from e


@app.post('/api/approve')
def approve(x: Approval):
    platform = PLATFORMS.get(x.platform.strip().lower())
    if not platform:
        raise HTTPException(422, 'Platform must be Facebook, LinkedIn, or Instagram.')
    key = str(x.idempotency_key)
    try:
        with db() as c:
            # Advisory lock makes concurrent retries for the same key safe without schema changes.
            lock_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big', signed=True)
            c.execute('SELECT pg_advisory_xact_lock(%s)', (lock_id,))
            cols = {r['column_name'] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='social_posts'").fetchall()}
            required = {'id', 'topic', 'platform', 'post_content', 'status'}
            if not required.issubset(cols):
                raise HTTPException(503, 'social_posts is missing required columns: ' + ', '.join(sorted(required - cols)))
            if x.image_url and 'image_url' not in cols:
                raise HTTPException(503, "Cannot save this image URL: the active social_posts table has no image_url column. Add the column through your database migration process, then retry approval.")
            # Store idempotency in an existing metadata field when available. For a table
            # without one, use a transaction-scoped key lock plus identical approved content.
            if 'approval_key' in cols:
                prior = c.execute('SELECT id FROM social_posts WHERE approval_key=%s LIMIT 1', (key,)).fetchone()
                if prior:
                    return {'ok': True, 'id': prior['id'], 'message': 'Already approved', 'duplicate': True}
            else:
                prior = None
                if 'topic' in cols and 'platform' in cols and 'post_content' in cols:
                    prior = c.execute('SELECT id FROM social_posts WHERE topic=%s AND LOWER(platform)=LOWER(%s) AND post_content=%s AND status=%s LIMIT 1', (x.topic.strip(), platform, x.post_content.strip(), 'approved')).fetchone()
                if prior:
                    return {'ok': True, 'id': prior['id'], 'message': 'Already approved', 'duplicate': True}
            vals = {'topic': x.topic.strip(), 'platform': platform, 'post_content': x.post_content.strip(), 'status': 'approved'}
            if 'batch_id' in cols:
                vals['batch_id'] = str(uuid4())
            if 'hashtags' in cols:
                vals['hashtags'] = x.hashtags
            if 'image_prompt' in cols:
                vals['image_prompt'] = x.image_prompt
            if 'image_url' in cols:
                vals['image_url'] = x.image_url
            if 'approval_key' in cols:
                vals['approval_key'] = key
            if 'updated_at' in cols:
                vals['updated_at'] = datetime.now().astimezone()
            names = list(vals)
            returning = 'id, image_url' if 'image_url' in vals else 'id'
            row = c.execute(f"INSERT INTO social_posts ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))}) RETURNING {returning}", [vals[n] for n in names]).fetchone()
            if x.image_url and row.get('image_url') != x.image_url:
                raise HTTPException(503, 'The database did not persist the supplied image URL; approval was rolled back.')
        return {'ok': True, 'id': row['id'], 'image_url': row.get('image_url'), 'status': 'approved', 'message': 'Post approved and saved for scheduling', 'duplicate': False}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, 'Could not save the approved post. Check database connectivity and social_posts schema.') from e


@app.patch('/api/posts/{pid}')
def update(pid: int, x: Update):
    v = x.model_dump(exclude_unset=True)
    if not v:
        raise HTTPException(400, 'No changes supplied')
    if 'status' in v and v['status'] not in {'pending', 'approved', 'queued', 'posted', 'failed', 'draft'}:
        raise HTTPException(400, 'Invalid status')
    v['updated_at'] = datetime.now().astimezone()
    sets = ', '.join(f'{k}=%s' for k in v)
    try:
        with db() as c:
            r = c.execute(f'UPDATE social_posts SET {sets} WHERE id=%s RETURNING *', (*v.values(), pid)).fetchone()
        if not r:
            raise HTTPException(404, 'Post not found')
        return r
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, 'Could not update post.') from e


@app.post('/api/posts/{pid}/retry')
def retry(pid: int):
    try:
        with db() as c:
            r = c.execute("UPDATE social_posts SET status='pending',retry_count=0,error_message=NULL,posted_at=NULL,platform_post_id=NULL,updated_at=NOW() WHERE id=%s RETURNING *", (pid,)).fetchone()
        if not r:
            raise HTTPException(404, 'Post not found')
        return r
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, 'Could not retry post.') from e


@app.delete('/api/posts/{pid}')
def delete(pid: int):
    try:
        with db() as c:
            r = c.execute('DELETE FROM social_posts WHERE id=%s RETURNING id', (pid,)).fetchone()
        if not r:
            raise HTTPException(404, 'Post not found')
        return {'ok': True, 'id': r['id']}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, 'Could not delete post.') from e


app.mount('/static', StaticFiles(directory='frontend'), name='static')


@app.get('/')
def home():
    return FileResponse('frontend/index.html')
