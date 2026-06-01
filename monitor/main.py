"""
Monitor HTTP service.

Layout:
  1. Proxy endpoint /v1/{path:path} — transparent forward to OpenRouter + capture.
  2. Ingest endpoints — /api/ingest_text (new, from Function) and /api/ingest (legacy no-op stub).
  3. Stats endpoints (Stage 2 minimal versions — full UI in Stage 4):
       /api/stats/summary, /api/stats/by-user, /api/stats/by-model, /api/stats/timeline
  4. Logs — /api/logs, /api/logs/{id}, joined with Prompt for human-readable view.
  5. Static dashboard mounted at /.
  6. FastAPI lifespan starts the openrouter_sync background worker.
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, Request as FastAPIRequest, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, distinct, cast, Date
from sqlalchemy.orm import Session

from .database import Base, engine, get_db, SessionLocal
from .models import Request as ReqModel, Prompt
from .proxy import proxy_request, shutdown_client
from . import openrouter_sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("monitor.main")

Base.metadata.create_all(bind=engine)


def _add_column_if_missing():
    """Lightweight ad-hoc migration for the `user_msg_hash` column on prompts.
    create_all only adds tables, not columns. Real projects use Alembic; this
    one stays small."""
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE prompts ADD COLUMN IF NOT EXISTS user_msg_hash VARCHAR(32)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_prompts_user_msg_hash ON prompts (user_msg_hash)"
            ))
    except Exception as e:
        # Not fatal — if PG syntax differs, just skip and hope create_all caught it.
        logging.getLogger("monitor.main").warning("migration warning: %s", e)


_add_column_if_missing()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("starting openrouter_sync worker")
    openrouter_sync.start_worker()
    yield
    # Shutdown
    logger.info("stopping openrouter_sync worker")
    await openrouter_sync.stop_worker()
    await shutdown_client()


app = FastAPI(title="OpenWebUI Monitor", lifespan=lifespan)


# ============================================================
# 1. Transparent proxy → OpenRouter (Stage 1).
# Declared first so it is matched before the static mount.
# ============================================================
@app.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def openrouter_proxy(path: str, request: FastAPIRequest):
    return await proxy_request(path, request)


# ============================================================
# 2. Ingest endpoints.
# ============================================================

@app.post("/api/ingest_text")
async def api_ingest_text(req: FastAPIRequest, db: Session = Depends(get_db)):
    """Receives prompt+response text from OpenWebUI Function (Stage 3)."""
    try:
        raw = await req.body()
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"bad payload: {e}"}, status_code=200)

    try:
        ts = payload.get("timestamp")
        try:
            created_at = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        except Exception:
            created_at = datetime.now(timezone.utc)
        # Strip timezone for naive DB comparisons (we store UTC consistently)
        if created_at.tzinfo is not None:
            created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)

        row = Prompt(
            chat_id=str(payload.get("chat_id") or "")[:128],
            user_id=str(payload.get("user_id") or "")[:128],
            user_name=str(payload.get("user_name") or "")[:256],
            user_email=str(payload.get("user_email") or "")[:256],
            model_hint=str(payload.get("model_hint") or "")[:256],
            messages_json=json.dumps(payload.get("messages") or [], ensure_ascii=False),
            response=payload.get("response") or "",
            created_at=created_at,
            user_msg_hash=str(payload.get("user_msg_hash") or "")[:32],
        )
        db.add(row)
        db.commit()
        return {"ok": True, "id": row.id}
    except Exception as e:
        logger.exception("ingest_text failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.post("/api/ingest")
async def api_ingest_legacy(req: FastAPIRequest):
    """Legacy endpoint — old Function may still send here. We swallow & ignore."""
    try:
        await req.body()
    except Exception:
        pass
    return {"ok": True, "deprecated": True}


# ============================================================
# 3. Stats endpoints.
# Note: Stage 2 versions show data from `requests` table only
# (one row per billable OpenRouter call — tool-rounds visible separately).
# Stage 4 will collapse rounds by request_id in the UI.
# ============================================================

def period_cutoff(period: Optional[str]) -> Optional[datetime]:
    now = datetime.utcnow()
    if not period or period == "all":
        return None
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return now - timedelta(days=7)
    if period == "month":
        return now - timedelta(days=30)
    return None


def apply_period(q, period):
    cutoff = period_cutoff(period)
    if cutoff is not None:
        q = q.filter(ReqModel.created_at >= cutoff)
    return q


@app.get("/api/stats/summary")
def stats_summary(period: Optional[str] = "all", db: Session = Depends(get_db)):
    q = apply_period(db.query(ReqModel).filter(ReqModel.sync_status == "synced"), period)
    total_requests = q.count()
    agg = q.with_entities(
        func.coalesce(func.sum(ReqModel.tokens_prompt), 0),
        func.coalesce(func.sum(ReqModel.tokens_completion), 0),
        func.coalesce(func.sum(ReqModel.total_cost), 0.0),
        func.count(distinct(ReqModel.external_user)),
    ).one()
    in_tok, out_tok, cost, users = agg
    return {
        "total_requests": total_requests,
        "input_tokens": int(in_tok or 0),
        "output_tokens": int(out_tok or 0),
        "total_tokens": int((in_tok or 0) + (out_tok or 0)),
        "cost_usd": float(cost or 0.0),
        "active_users": int(users or 0),
    }


@app.get("/api/stats/by-user")
def stats_by_user(period: Optional[str] = "all", db: Session = Depends(get_db)):
    """Group by Prompt.user_email via user_msg_hash echoed in external_user."""
    q = apply_period(db.query(ReqModel).filter(ReqModel.sync_status == "synced"), period)
    rows = q.with_entities(
        ReqModel.external_user,
        ReqModel.tokens_prompt,
        ReqModel.tokens_completion,
        ReqModel.total_cost,
    ).all()

    # hash → (email, name) — keep first prompt per hash
    hash_map: dict[str, tuple[str, str]] = {}
    for p in db.query(Prompt.user_msg_hash, Prompt.user_email, Prompt.user_name).all():
        if p.user_msg_hash and p.user_msg_hash not in hash_map:
            hash_map[p.user_msg_hash] = (p.user_email or "", p.user_name or "")

    agg: dict[str, dict] = {}
    for ext, in_t, out_t, cost in rows:
        email, name = hash_map.get((ext or "").strip(), ("", ""))
        if not email:
            email = "(unknown)"
        bucket = agg.setdefault(email, {
            "user_email": email, "user_name": name,
            "requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
        })
        bucket["requests"] += 1
        bucket["input_tokens"] += int(in_t or 0)
        bucket["output_tokens"] += int(out_t or 0)
        bucket["cost_usd"] += float(cost or 0.0)

    out = sorted(agg.values(), key=lambda r: r["cost_usd"], reverse=True)
    for r in out:
        r["cost_usd"] = round(r["cost_usd"], 6)
    return out


@app.get("/api/stats/by-model")
def stats_by_model(period: Optional[str] = "all", db: Session = Depends(get_db)):
    q = apply_period(db.query(ReqModel).filter(ReqModel.sync_status == "synced"), period)
    q = q.with_entities(
        ReqModel.model,
        ReqModel.provider_name,
        func.count(ReqModel.id),
        func.coalesce(func.sum(ReqModel.tokens_prompt), 0),
        func.coalesce(func.sum(ReqModel.tokens_completion), 0),
        func.coalesce(func.sum(ReqModel.total_cost), 0.0),
        func.coalesce(func.avg(ReqModel.generation_time), 0.0),
    ).group_by(ReqModel.model, ReqModel.provider_name).order_by(func.count(ReqModel.id).desc())
    out = []
    for model, provider, cnt, in_t, out_t, cost, lat in q.all():
        out.append({
            "model": model or "",
            "provider_name": provider or "",
            "requests": int(cnt),
            "input_tokens": int(in_t or 0),
            "output_tokens": int(out_t or 0),
            "cost_usd": float(cost or 0.0),
            "avg_generation_time_ms": float(lat or 0.0),
        })
    return out


@app.get("/api/stats/timeline")
def stats_timeline(days: int = 30, db: Session = Depends(get_db)):
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(
            cast(ReqModel.created_at, Date).label("d"),
            func.count(ReqModel.id),
            func.coalesce(func.sum(ReqModel.tokens_prompt), 0),
            func.coalesce(func.sum(ReqModel.tokens_completion), 0),
            func.coalesce(func.sum(ReqModel.total_cost), 0.0),
        )
        .filter(ReqModel.created_at >= cutoff, ReqModel.sync_status == "synced")
        .group_by("d")
        .order_by("d")
        .all()
    )
    by_day = {str(r[0]): (int(r[1]), int(r[2] or 0), int(r[3] or 0), float(r[4] or 0.0)) for r in rows}
    out = []
    for i in range(days):
        d = (cutoff + timedelta(days=i)).strftime("%Y-%m-%d")
        c, in_t, out_t, cost = by_day.get(d, (0, 0, 0, 0.0))
        out.append({
            "date": d, "requests": c, "input_tokens": in_t, "output_tokens": out_t, "cost_usd": cost,
        })
    return out


# ============================================================
# 4. Logs (Stage 4 — collapsed by request_id).
# ============================================================

def _attach_prompt_by_hash(db: Session, msg_hash: Optional[str]) -> Optional[Prompt]:
    """Find Prompt row whose user_msg_hash matches. This is the canonical
    link key — Request.external_user (echoed by OpenRouter from proxy's
    injected `user`) == Prompt.user_msg_hash (computed from same text)."""
    if not msg_hash:
        return None
    return (
        db.query(Prompt)
        .filter(Prompt.user_msg_hash == msg_hash)
        .order_by(Prompt.created_at.desc())
        .first()
    )


def _serialize_round(r: ReqModel) -> dict:
    # Prefer native_tokens_* — these are what OpenRouter Activity shows
    # and what providers actually bill. tokens_prompt/completion are
    # OpenAI-normalized and don't match upstream reports.
    return {
        "id": r.id,
        "generation_id": r.generation_id,
        "timestamp": r.created_at.isoformat() if r.created_at else None,
        "model": r.model,
        "provider_name": r.provider_name,
        "api_type": r.api_type,
        "input_tokens": r.native_tokens_prompt or r.tokens_prompt or 0,
        "output_tokens": r.native_tokens_completion or r.tokens_completion or 0,
        "reasoning_tokens": r.native_tokens_reasoning or 0,
        "cost_usd": r.total_cost or 0.0,
        "generation_time_ms": r.generation_time or 0,
        "latency_ms": r.latency or 0,
        "streamed": bool(r.streamed),
        "finish_reason": r.finish_reason,
    }


@app.get("/api/logs")
def api_logs(
    page: int = 1,
    limit: int = 50,
    user_email: Optional[str] = None,
    model: Optional[str] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Returns LOGICAL user turns. One row in the UI = one user message, summing
    tokens/cost across the (often several) independent OpenAI API calls that
    OpenWebUI fires under the hood per turn (search-classifier, query-gen,
    final response, etc).

    Linking algorithm:
      Each Prompt row (from Function outlet) represents a finished user turn.
      We attach to it all Request rows whose created_at falls within a
      [-60s, +10s] window around the Prompt time AND have not yet been
      claimed by an earlier-attached Prompt.

      A perfect external_user match (user_msg_hash) takes precedence — it
      pins the right rows even when the time windows of two turns overlap.

    Requests with no matching Prompt show up at the end as "unlinked"
    (Image Studio, direct curl, requests before Function was deployed, …).
    """
    page = max(1, page)
    limit = max(1, min(200, limit))

    # 1) Fetch all candidate Requests (recent first, cap defensively)
    q = db.query(ReqModel).filter(ReqModel.sync_status == "synced")
    if model:
        q = q.filter(ReqModel.model == model)
    all_requests = q.order_by(ReqModel.created_at.desc()).limit(5000).all()

    # 2) Fetch all Prompts (recent first)
    pq = db.query(Prompt).order_by(Prompt.created_at.desc()).limit(2000)
    if user_email:
        pq = pq.filter(Prompt.user_email == user_email)
    all_prompts = pq.all()

    # 3) Greedy assignment, newest Prompt first.
    #    OpenWebUI fires several API calls per user turn: search-classifier,
    #    query-gen, the actual answer, then later a title-generation call.
    #    The window [-60s, +90s] around the Prompt outlet timestamp captures
    #    all of these without bleeding into the next turn (turns are normally
    #    minutes apart).
    WINDOW_BEFORE = timedelta(seconds=60)
    WINDOW_AFTER = timedelta(seconds=90)

    claimed: set[int] = set()
    items: list[dict] = []

    for prompt in all_prompts:
        if not prompt.created_at:
            continue

        # Find unclaimed Requests in the time window (or matching the hash exactly).
        win_lo = prompt.created_at - WINDOW_BEFORE
        win_hi = prompt.created_at + WINDOW_AFTER
        matched: list[ReqModel] = []
        for r in all_requests:
            if r.id in claimed:
                continue
            if not r.created_at:
                continue
            in_window = (win_lo <= r.created_at <= win_hi)
            hash_match = (
                prompt.user_msg_hash
                and r.external_user
                and r.external_user.strip() == prompt.user_msg_hash.strip()
            )
            if in_window or hash_match:
                matched.append(r)
        if not matched:
            continue
        for r in matched:
            claimed.add(r.id)

        rounds = sorted(matched, key=lambda x: x.created_at)
        first = rounds[0]
        msg_hash = prompt.user_msg_hash or ""
        chat_id = prompt.chat_id or ""

        total_in = sum((r.native_tokens_prompt or r.tokens_prompt or 0) for r in rounds)
        total_out = sum((r.native_tokens_completion or r.tokens_completion or 0) for r in rounds)
        total_reasoning = sum(r.native_tokens_reasoning or 0 for r in rounds)
        total_cost = sum(r.total_cost or 0.0 for r in rounds)
        total_gen_time = sum(r.generation_time or 0 for r in rounds)

        # Filter: search in prompt/response text
        if search:
            haystack = ((prompt.messages_json or "") + " " + (prompt.response or "")).lower()
            if search.lower() not in haystack:
                # release these requests for potential later matching
                for r in matched:
                    claimed.discard(r.id)
                continue

        # last user message preview
        prompt_preview = ""
        try:
            msgs = json.loads(prompt.messages_json or "[]")
            for m in reversed(msgs):
                if isinstance(m, dict) and m.get("role") == "user":
                    c = m.get("content")
                    if isinstance(c, str):
                        prompt_preview = c
                        break
                    if isinstance(c, list):
                        prompt_preview = "\n".join(
                            p.get("text", "") for p in c if isinstance(p, dict)
                        )
                        break
        except Exception:
            prompt_preview = ""

        items.append({
            "group_key": f"prompt:{prompt.id}",
            "user_msg_hash": msg_hash,
            "timestamp": prompt.created_at.isoformat(),
            "model": first.model,
            "providers": sorted({r.provider_name for r in rounds if r.provider_name}),
            "user_email": prompt.user_email or "",
            "user_name": prompt.user_name or "",
            "chat_id": chat_id,
            "rounds_count": len(rounds),
            "input_tokens": total_in,
            "output_tokens": total_out,
            "reasoning_tokens": total_reasoning,
            "cost_usd": round(total_cost, 6),
            "generation_time_ms": total_gen_time,
            "prompt_preview": (prompt_preview[:160] + ("…" if len(prompt_preview) > 160 else "")) if prompt_preview else "",
            "has_prompt": True,
            "rounds": [_serialize_round(r) for r in rounds],
        })

    # 4) Append unmatched Requests as standalone rows (Image Studio, curl, etc).
    if not user_email:  # filtering by user means we only want linked rows
        for r in all_requests:
            if r.id in claimed:
                continue
            if search:  # nothing to match against — skip when user is searching prompt text
                continue
            items.append({
                "group_key": r.generation_id,
                "user_msg_hash": "",
                "timestamp": r.created_at.isoformat() if r.created_at else None,
                "model": r.model,
                "providers": [r.provider_name] if r.provider_name else [],
                "user_email": "",
                "user_name": "",
                "chat_id": "",
                "rounds_count": 1,
                "input_tokens": r.native_tokens_prompt or r.tokens_prompt or 0,
                "output_tokens": r.native_tokens_completion or r.tokens_completion or 0,
                "reasoning_tokens": r.native_tokens_reasoning or 0,
                "cost_usd": round(r.total_cost or 0.0, 6),
                "generation_time_ms": r.generation_time or 0,
                "prompt_preview": "",
                "has_prompt": False,
                "rounds": [_serialize_round(r)],
            })

    # Sort everything by timestamp desc
    items.sort(key=lambda x: x["timestamp"] or "", reverse=True)

    total = len(items)
    offset = (page - 1) * limit
    items = items[offset:offset + limit]
    return {"total": total, "page": page, "limit": limit, "items": items}


@app.get("/api/logs/{group_key}")
def api_log_detail(group_key: str, db: Session = Depends(get_db)):
    """
    Detail of one logical user turn (or single unlinked request).
    `group_key` is either:
      - "prompt:<id>" — a linked turn; we recompute its time window and
        return the same rounds shown in /api/logs
      - <generation_id> — an unlinked solo request
    """
    if group_key.startswith("prompt:"):
        try:
            pid = int(group_key.split(":", 1)[1])
        except ValueError:
            raise HTTPException(404)
        prompt = db.query(Prompt).filter(Prompt.id == pid).first()
        if not prompt or not prompt.created_at:
            raise HTTPException(404)

        win_lo = prompt.created_at - timedelta(seconds=60)
        win_hi = prompt.created_at + timedelta(seconds=90)
        rounds = (
            db.query(ReqModel)
            .filter(ReqModel.sync_status == "synced")
            .filter(
                ((ReqModel.created_at >= win_lo) & (ReqModel.created_at <= win_hi))
                | (ReqModel.external_user == (prompt.user_msg_hash or "__"))
            )
            .order_by(ReqModel.created_at.asc())
            .all()
        )
        if not rounds:
            raise HTTPException(404)
    else:
        rounds = (
            db.query(ReqModel)
            .filter(ReqModel.generation_id == group_key)
            .filter(ReqModel.sync_status == "synced")
            .all()
        )
        prompt = None
        if not rounds:
            raise HTTPException(404)

    total_in = sum((r.native_tokens_prompt or r.tokens_prompt or 0) for r in rounds)
    total_out = sum((r.native_tokens_completion or r.tokens_completion or 0) for r in rounds)
    total_reasoning = sum(r.native_tokens_reasoning or 0 for r in rounds)
    total_cost = sum(r.total_cost or 0.0 for r in rounds)

    return {
        "group_key": group_key,
        "user_msg_hash": (prompt.user_msg_hash if prompt else "") or "",
        "timestamp": rounds[0].created_at.isoformat() if rounds[0].created_at else None,
        "model": rounds[0].model,
        "providers": sorted({r.provider_name for r in rounds if r.provider_name}),
        "rounds_count": len(rounds),
        "input_tokens": total_in,
        "output_tokens": total_out,
        "reasoning_tokens": total_reasoning,
        "cost_usd": round(total_cost, 6),
        "rounds": [_serialize_round(r) for r in rounds],
        "prompt": ({
            "chat_id": prompt.chat_id,
            "user_email": prompt.user_email,
            "user_name": prompt.user_name,
            "model_hint": prompt.model_hint,
            "messages": json.loads(prompt.messages_json or "[]"),
            "response": prompt.response,
            "created_at": prompt.created_at.isoformat() if prompt.created_at else None,
        } if prompt else None),
    }


@app.get("/api/users")
def api_users(db: Session = Depends(get_db)):
    """List distinct OWUI users (from Prompt table)."""
    rows = db.query(Prompt.user_email, Prompt.user_name).distinct().all()
    return [{"email": e or "", "name": n or ""} for e, n in rows if e]


@app.get("/api/models")
def api_models(db: Session = Depends(get_db)):
    rows = db.query(ReqModel.model).filter(ReqModel.sync_status == "synced").distinct().all()
    return [r[0] for r in rows if r[0]]


@app.get("/api/health")
def api_health(db: Session = Depends(get_db)):
    """Quick health probe — checks DB + worker."""
    try:
        db.execute(func.count(ReqModel.id).select())
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ============================================================
# 5. Static dashboard. Must be LAST — catch-all.
# ============================================================
_static_dir = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
