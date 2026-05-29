"""
Transparent HTTP proxy: OpenWebUI → Monitor → OpenRouter.

Goals:
  1. Forward every request to OpenRouter unmodified (transparent).
  2. Capture X-Generation-Id from response headers into a queue for later sync.
  3. Never break chat — on any internal error, return upstream response as-is
     (or 502 if network unreachable).

The queue is consumed by `openrouter_sync.py` background worker (Stage 2).
"""
from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import os
import time
from typing import Optional

import httpx
from fastapi import Request as FastAPIRequest
from fastapi.responses import JSONResponse, StreamingResponse


def fingerprint_user_msg(text: str) -> str:
    """Stable 16-char hex hash of a user message.

    Same function is used in monitor_function.py — DO NOT change the
    algorithm without bumping both. Empty/missing → 'no-user-msg'.
    """
    if not text:
        return "no-user-msg"
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]


def _last_user_message_text(messages) -> str:
    """Walk messages from end, return the last user message as plain text."""
    if not isinstance(messages, list):
        return ""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(
                p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"
            )
    return ""


def _inject_fingerprint(body: bytes) -> bytes:
    """Parse JSON body, inject `user=<fingerprint>` if absent, re-serialize.
    On any error returns original bytes unchanged."""
    try:
        parsed = _json.loads(body.decode("utf-8"))
    except Exception:
        return body
    if not isinstance(parsed, dict):
        return body
    if parsed.get("user"):
        # Caller already set a user — don't override (test scripts, etc).
        return body
    msg_text = _last_user_message_text(parsed.get("messages"))
    fp = fingerprint_user_msg(msg_text)
    parsed["user"] = fp
    return _json.dumps(parsed, ensure_ascii=False).encode("utf-8")

logger = logging.getLogger("monitor.proxy")

OPENROUTER_BASE = os.environ.get("OPENROUTER_BASE", "https://openrouter.ai/api/v1").rstrip("/")
PROXY_TIMEOUT = float(os.environ.get("PROXY_TIMEOUT", "120"))  # seconds, end-to-end

# In-memory queue of generation IDs awaiting sync from OpenRouter API.
# Consumed by openrouter_sync.py worker (Stage 2).
generation_queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=2000)

# Reusable httpx client (created lazily so import is cheap)
_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(PROXY_TIMEOUT, connect=10.0),
            follow_redirects=False,
        )
    return _client


async def shutdown_client():
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# Headers that must not be forwarded as-is between client/upstream
_HOP_BY_HOP = {
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-length",  # we restream — let starlette set this
    "content-encoding",  # httpx auto-decodes; restream raw bytes
}


def _filter_headers(headers, extra_drop: set[str] | None = None) -> dict:
    drop = _HOP_BY_HOP | (extra_drop or set())
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def _capture_generation_id(upstream_resp: httpx.Response, path: str, method: str) -> Optional[str]:
    """Read X-Generation-Id (case-insensitive) and enqueue. Best-effort."""
    gen_id = (
        upstream_resp.headers.get("X-Generation-Id")
        or upstream_resp.headers.get("x-generation-id")
    )
    if not gen_id:
        return None
    try:
        generation_queue.put_nowait({
            "generation_id": gen_id,
            "captured_at": time.time(),
            "path": path,
            "method": method,
            "upstream_status": upstream_resp.status_code,
        })
        logger.info("captured %s for %s %s", gen_id, method, path)
    except asyncio.QueueFull:
        logger.warning("generation queue full (size=%d), dropping %s", generation_queue.maxsize, gen_id)
    return gen_id


async def proxy_request(path: str, request: FastAPIRequest) -> StreamingResponse | JSONResponse:
    """
    Main proxy handler. Forwards request to OpenRouter, streams response back,
    captures X-Generation-Id from response headers.

    NEVER raises — on any error returns 502 or upstream-as-is so chat keeps working.
    """
    method = request.method
    target_url = f"{OPENROUTER_BASE}/{path}"
    params = dict(request.query_params)

    try:
        body = await request.body()
    except Exception as e:
        logger.exception("failed to read request body")
        return JSONResponse({"error": {"message": f"proxy: read body failed: {e}"}}, status_code=400)

    # Inject a stable per-turn fingerprint as `user` field so OpenRouter echoes
    # it back as `external_user`. This is how we link tool-chain rounds to
    # each other AND to the Prompt row captured by the Function:
    #
    #   fingerprint = sha1(last_user_message_text)[:16]
    #
    # OpenWebUI's chat completion forwarder strips most fields from the body
    # before sending upstream, but `user` is OpenAI-canonical and survives.
    # All rounds of a tool-chain share the same last user message (only tool
    # calls/results get appended between rounds), so they all get the same
    # fingerprint. The Function computes the same hash from `__user__` /
    # `body.messages` so the Prompt row carries the same key.
    if "chat/completions" in path and body and request.method.upper() == "POST":
        # We inject `user=<fingerprint>` mostly as an identification echo —
        # it doesn't group rounds (OpenWebUI fires several independent calls
        # per user turn, each with different messages), but it still gives
        # us a perfect-match link for SINGLE-call turns (e.g. simple chats,
        # image generation). Group-by-chat_id+time-window picks up the rest
        # at query time in /api/logs.
        try:
            body = _inject_fingerprint(body)
        except Exception:
            logger.exception("fingerprint injection failed (non-fatal)")

    # Forward all headers except hop-by-hop ones.
    fwd_headers = _filter_headers(request.headers)

    client = get_client()

    try:
        upstream_req = client.build_request(method, target_url, headers=fwd_headers, content=body, params=params)
        upstream = await client.send(upstream_req, stream=True)
    except httpx.TimeoutException:
        logger.warning("upstream timeout: %s %s", method, target_url)
        return JSONResponse({"error": {"message": "upstream timeout (OpenRouter)"}}, status_code=504)
    except httpx.RequestError as e:
        logger.warning("upstream network error: %s", e)
        return JSONResponse({"error": {"message": f"upstream network error: {e}"}}, status_code=502)
    except Exception as e:
        logger.exception("unexpected proxy error before send")
        return JSONResponse({"error": {"message": f"proxy internal error: {e}"}}, status_code=502)

    # Capture generation ID. Wrapped in try — capture failure must NOT break chat.
    try:
        _capture_generation_id(upstream, path, method)
    except Exception:
        logger.exception("capture failed (non-fatal)")

    # Restream response.
    resp_headers = _filter_headers(upstream.headers)

    async def body_iter():
        try:
            # aiter_bytes() returns decoded body (httpx auto-decodes gzip/br).
            # We strip Content-Encoding from resp_headers above, so client gets
            # consistent plain bytes. aiter_raw() would give compressed bytes
            # and break clients that expect the header to match.
            async for chunk in upstream.aiter_bytes():
                yield chunk
        except Exception:
            logger.exception("stream interrupted")
        finally:
            try:
                await upstream.aclose()
            except Exception:
                pass

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )
