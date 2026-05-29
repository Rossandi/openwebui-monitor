"""
SQLAlchemy models.

Schema redesign (Stage 2 — proxy-based architecture):

- `Request` is the canonical billable event, sourced from OpenRouter's
  `GET /api/v1/generation?id=...` for absolute accuracy. One row = one
  HTTP call OpenWebUI made to OpenRouter (so a tool-calling chain becomes
  4 rows linked by `request_id`).

- `Prompt` is the human-readable text (user message, assistant reply)
  captured by the OpenWebUI Function. One row per user turn, linked to
  `Request` rows by `chat_id` + time window.

- Legacy columns from v1 schema were dropped — old DB is wiped per the
  migration plan (§ 11). PostgreSQL only from this version onwards.
"""
from sqlalchemy import (
    Column, Integer, BigInteger, String, DateTime, Float, Boolean, Text, Index,
)
from sqlalchemy.sql import func

from .database import Base


class Request(Base):
    """
    One billable event from OpenRouter. Filled by the async sync worker
    using data from GET /api/v1/generation?id=<generation_id>.
    """
    __tablename__ = "requests"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # === Identity ===
    generation_id = Column(String(128), unique=True, nullable=False, index=True)
    request_id = Column(String(128), index=True, nullable=True)
    # Group key for tool-calling rounds. Multiple Request rows share request_id.

    # === When ===
    created_at = Column(DateTime, index=True, nullable=False)
    captured_at = Column(DateTime, server_default=func.now())

    # === Model & provider ===
    model = Column(String(256), index=True)
    model_permaslug = Column(String(256))
    provider_name = Column(String(128), index=True)
    api_type = Column(String(32), index=True)  # chat/image/embedding/tts/stt/video/rerank

    # === Usage ===
    tokens_prompt = Column(Integer, default=0)
    tokens_completion = Column(Integer, default=0)
    native_tokens_prompt = Column(Integer, default=0)
    native_tokens_completion = Column(Integer, default=0)
    native_tokens_reasoning = Column(Integer, default=0)
    native_tokens_cached = Column(Integer, default=0)
    native_tokens_completion_images = Column(Integer, default=0)
    num_search_results = Column(Integer, default=0)
    num_media_completion = Column(Integer, default=0)

    # === Cost (USD) ===
    total_cost = Column(Float, default=0.0)
    cache_discount = Column(Float, default=0.0)
    upstream_inference_cost = Column(Float, default=0.0)

    # === Timing ===
    generation_time = Column(Integer, default=0)   # ms
    latency = Column(Integer, default=0)           # ms (time-to-first-byte)
    moderation_latency = Column(Integer, default=0)

    # === Flags ===
    streamed = Column(Boolean, default=False)
    cancelled = Column(Boolean, default=False)
    is_byok = Column(Boolean, default=False)
    finish_reason = Column(String(64))

    # === Mapping to OpenWebUI user ===
    external_user = Column(String(256), index=True)
    # Format we inject in proxy: "<chat_id>:<owui_user_id>" — parsed at link-time.

    # === Sync state ===
    sync_status = Column(String(16), default="synced", index=True)  # synced/pending/failed
    sync_attempts = Column(Integer, default=0)
    sync_error = Column(Text)


Index("ix_requests_created_user", Request.created_at, Request.external_user)


class Prompt(Base):
    """
    Human-readable conversation captured by OpenWebUI Function.
    Linked to Request rows by chat_id + external_user + time window.
    """
    __tablename__ = "prompts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    chat_id = Column(String(128), index=True, nullable=False)
    user_id = Column(String(128), index=True)
    user_name = Column(String(256))
    user_email = Column(String(256), index=True)

    model_hint = Column(String(256))  # what user selected in OpenWebUI
    messages_json = Column(Text)      # full conversation as JSON string
    response = Column(Text)           # final assistant response (text only)
    created_at = Column(DateTime, index=True, nullable=False)

    # SHA1[:16] of the last user message text. Matches Request.external_user
    # (which is OpenRouter's echo of the `user` field injected by the proxy).
    # This is the canonical link key between Prompt and its Request rows.
    user_msg_hash = Column(String(32), index=True)


Index("ix_prompts_chat_created", Prompt.chat_id, Prompt.created_at)
Index("ix_prompts_msg_hash", Prompt.user_msg_hash)


class CaptureQueue(Base):
    """
    Durable queue of captured generation IDs awaiting sync from OpenRouter.

    The in-memory `proxy.generation_queue` (asyncio.Queue) is fast but lost
    on restart. This table is a fallback: when the in-mem queue drains
    successfully, items go to Request directly. If sync fails or restart
    happens, queued IDs persist here and the worker retries them.
    """
    __tablename__ = "capture_queue"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    generation_id = Column(String(128), unique=True, nullable=False, index=True)
    path = Column(String(256))
    method = Column(String(16))
    captured_at = Column(DateTime, server_default=func.now())
    attempts = Column(Integer, default=0)
    last_error = Column(Text)
    next_retry_at = Column(DateTime, index=True)
