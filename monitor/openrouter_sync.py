"""
Background worker that pulls generation IDs from the proxy queue and
fetches their full billing data from OpenRouter.

OpenRouter endpoint: GET https://openrouter.ai/api/v1/generation?id=<gen-id>

Behavior:
  * Drains `proxy.generation_queue` continuously.
  * On enqueue, immediately try fetching (OpenRouter typically takes 1-3s
    after the response to make generation data queryable — we retry on 404).
  * Exponential backoff: 2s, 4s, 8s, 16s, 32s.
  * After 5 failed attempts, mark `Request.sync_status = 'failed'` and stop.
  * Idempotent: UPSERT on `generation_id` (unique).

Spawned by main.py via FastAPI lifespan event.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import Request
from .proxy import generation_queue

logger = logging.getLogger("monitor.sync")

OPENROUTER_BASE = os.environ.get("OPENROUTER_BASE", "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# Retry schedule (seconds). OpenRouter typically indexes a generation within
# 30-60 seconds of the request, but it can occasionally take longer. The
# schedule below sleeps cumulatively 3+7+15+30+60+90 = 205 seconds before
# giving up — comfortably past the empirical p99.
RETRY_DELAYS = [3, 7, 15, 30, 60, 90]
MAX_ATTEMPTS = len(RETRY_DELAYS)

# How many fetches in flight at once
WORKER_CONCURRENCY = 4


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp into a UTC-naive datetime.

    Critical: returning a tz-aware datetime would let psycopg2 silently
    convert it into the container's local timezone before writing into a
    TIMESTAMP WITHOUT TIME ZONE column — which then double-shifts in the
    UI (UTC→MSK on save, then MSK→MSK+3 on render). Normalize to UTC and
    strip tzinfo here so the DB always stores plain UTC.
    """
    if not s:
        return None
    try:
        # OpenRouter format: "2026-05-27T16:21:25.123456+00:00"
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


async def fetch_generation(client: httpx.AsyncClient, generation_id: str) -> Optional[dict]:
    """
    Fetch one generation. Returns dict or None if not (yet) available.
    Raises only for our internal bugs — never for transient errors.
    """
    url = f"{OPENROUTER_BASE}/generation"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    try:
        r = await client.get(url, params={"id": generation_id}, headers=headers, timeout=20.0)
    except httpx.RequestError as e:
        logger.info("network error fetching %s: %s", generation_id, e)
        return None
    if r.status_code == 200:
        body = r.json()
        # OpenRouter wraps payload in {"data": {...}}
        return body.get("data") if isinstance(body, dict) else None
    if r.status_code == 404:
        # Generation not indexed yet — retry later
        return None
    logger.warning("unexpected status %d fetching %s: %s", r.status_code, generation_id, r.text[:200])
    return None


def upsert_request(db: Session, data: dict, generation_id: str) -> None:
    """UPSERT one Request row from OpenRouter response payload."""
    values = {
        "generation_id": generation_id,
        "request_id": data.get("request_id"),
        "created_at": _parse_iso(data.get("created_at")) or datetime.utcnow(),
        "model": data.get("model"),
        "model_permaslug": data.get("model_permaslug"),
        "provider_name": data.get("provider_name"),
        "api_type": data.get("api_type") or "chat",
        "tokens_prompt": data.get("tokens_prompt") or 0,
        "tokens_completion": data.get("tokens_completion") or 0,
        "native_tokens_prompt": data.get("native_tokens_prompt") or 0,
        "native_tokens_completion": data.get("native_tokens_completion") or 0,
        "native_tokens_reasoning": data.get("native_tokens_reasoning") or 0,
        "native_tokens_cached": data.get("native_tokens_cached") or 0,
        "native_tokens_completion_images": data.get("native_tokens_completion_images") or 0,
        "num_search_results": data.get("num_search_results") or 0,
        "num_media_completion": data.get("num_media_completion") or 0,
        "total_cost": float(data.get("total_cost") or 0.0),
        "cache_discount": float(data.get("cache_discount") or 0.0),
        "upstream_inference_cost": float(data.get("upstream_inference_cost") or 0.0),
        "generation_time": data.get("generation_time") or 0,
        "latency": data.get("latency") or 0,
        "moderation_latency": data.get("moderation_latency") or 0,
        "streamed": bool(data.get("streamed")),
        "cancelled": bool(data.get("cancelled")),
        "is_byok": bool(data.get("is_byok")),
        "finish_reason": data.get("finish_reason"),
        "external_user": data.get("external_user"),
        "sync_status": "synced",
        "sync_attempts": 1,
        "sync_error": None,
    }

    # PostgreSQL ON CONFLICT — works since we standardized on Postgres in Stage 2
    stmt = pg_insert(Request).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["generation_id"],
        set_={k: v for k, v in values.items() if k != "generation_id"},
    )
    db.execute(stmt)
    db.commit()


def mark_failed(db: Session, generation_id: str, error: str, attempts: int) -> None:
    """Record a failed generation so we have audit trail."""
    stmt = pg_insert(Request).values(
        generation_id=generation_id,
        created_at=datetime.utcnow(),
        sync_status="failed",
        sync_attempts=attempts,
        sync_error=error[:1000],
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["generation_id"],
        set_={"sync_status": "failed", "sync_attempts": attempts, "sync_error": error[:1000]},
    )
    db.execute(stmt)
    db.commit()


async def sync_one(client: httpx.AsyncClient, item: dict) -> None:
    """Sync a single generation ID with retries."""
    generation_id = item["generation_id"]
    logger.info("syncing %s", generation_id)

    last_error = ""
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        await asyncio.sleep(delay)
        try:
            data = await fetch_generation(client, generation_id)
        except Exception as e:
            last_error = f"fetch exception: {e}"
            logger.exception("fetch raised for %s", generation_id)
            continue

        if data is None:
            last_error = f"not available yet (attempt {attempt}/{MAX_ATTEMPTS})"
            continue

        # Got it — persist and exit
        try:
            db = SessionLocal()
            try:
                upsert_request(db, data, generation_id)
                logger.info(
                    "synced %s: model=%s provider=%s tokens=%d+%d cost=$%.6f",
                    generation_id,
                    data.get("model"),
                    data.get("provider_name"),
                    data.get("tokens_prompt") or 0,
                    data.get("tokens_completion") or 0,
                    data.get("total_cost") or 0.0,
                )
            finally:
                db.close()
            return
        except SQLAlchemyError as e:
            last_error = f"db error: {e}"
            logger.exception("db error saving %s", generation_id)
            # Don't give up on DB errors — retry
            continue

    # All attempts exhausted
    logger.warning("failed to sync %s after %d attempts: %s", generation_id, MAX_ATTEMPTS, last_error)
    try:
        db = SessionLocal()
        try:
            mark_failed(db, generation_id, last_error, MAX_ATTEMPTS)
        finally:
            db.close()
    except Exception:
        logger.exception("could not mark %s failed", generation_id)


async def worker_loop():
    """Main loop. Pulls from queue and spawns sync tasks."""
    logger.info("openrouter_sync worker starting (concurrency=%d)", WORKER_CONCURRENCY)
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY not set — sync worker will fail on every fetch")

    sem = asyncio.Semaphore(WORKER_CONCURRENCY)

    async with httpx.AsyncClient() as client:
        while True:
            try:
                item = await generation_queue.get()
            except asyncio.CancelledError:
                logger.info("worker cancelled")
                raise
            except Exception:
                logger.exception("queue get failed")
                await asyncio.sleep(1)
                continue

            async def _do(item=item):
                async with sem:
                    try:
                        await sync_one(client, item)
                    finally:
                        generation_queue.task_done()

            asyncio.create_task(_do())


# === Public API ===

_task: Optional[asyncio.Task] = None


def start_worker():
    """Spawn the background task. Idempotent."""
    global _task
    if _task is not None and not _task.done():
        return
    loop = asyncio.get_event_loop()
    _task = loop.create_task(worker_loop(), name="openrouter_sync")


async def stop_worker():
    """Cancel the background task."""
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
