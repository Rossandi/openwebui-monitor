"""
title: OpenWebUI Monitor
author: openwebui-monitor
author_url: https://github.com/openwebui-monitor
funding_url: https://github.com/openwebui-monitor
version: 2.0.0
required_open_webui_version: 0.4.0
description: Captures human-readable prompt + assistant response + chat_id for analytics. Token/cost accounting is handled by the Monitor proxy, NOT by this Function. All exceptions are silently swallowed — this filter must NEVER break a chat.
"""

# === ARCHITECTURE NOTE (v2) ===
# v1 sent tokens and cost from `body.usage` in outlet. That broke down with
# tool-calling: OpenWebUI makes N HTTP calls to LLM under the hood but only
# the final usage reaches outlet → undercount by 4-5x.
#
# v2 stops trying to count tokens here. Instead, Monitor sits as an HTTP proxy
# in front of OpenRouter and captures the authoritative X-Generation-Id of
# EVERY billable call (including tool rounds). This Function's sole job now is
# to ship the human-readable prompt/response text + chat_id so the dashboard
# can show meaningful audit info.
#
# The two streams are linked in the DB by chat_id (we inject it into the
# `user` field of every OpenRouter request via OpenWebUI's natural flow).

import hashlib
from datetime import datetime, timezone
from typing import Optional

import httpx
from pydantic import BaseModel, Field


def _fingerprint(text: str) -> str:
    """MUST match monitor.proxy.fingerprint_user_msg algorithm exactly."""
    if not text:
        return "no-user-msg"
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]


def _last_user_text(messages) -> str:
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


class Filter:
    class Valves(BaseModel):
        MONITOR_URL: str = Field(
            default="http://monitor:8088",
            description="Base URL of the Monitor service (no trailing slash). "
                        "Inside docker-compose: http://monitor:8088",
        )
        priority: int = Field(
            default=0,
            description="Filter priority. Lower numbers run earlier.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._pending: dict[str, dict] = {}
        self.type = "filter"
        self.name = "OpenWebUI Monitor"

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _chat_id(body) -> str:
        try:
            return (body or {}).get("chat_id") or (body or {}).get("id") or ""
        except Exception:
            return ""

    @staticmethod
    def _user_fields(user) -> dict:
        if not isinstance(user, dict):
            return {"id": "", "name": "", "email": ""}
        return {
            "id": user.get("id", "") or "",
            "name": user.get("name", "") or "",
            "email": user.get("email", "") or "",
        }

    @staticmethod
    def _extract_response(body: dict) -> str:
        """Pull the last assistant message text out of the body."""
        try:
            msgs = (body or {}).get("messages") or []
            for m in reversed(msgs):
                if isinstance(m, dict) and m.get("role") == "assistant":
                    c = m.get("content")
                    if isinstance(c, str):
                        return c
                    if isinstance(c, list):
                        return "\n".join(
                            p.get("text", "")
                            for p in c
                            if isinstance(p, dict)
                        )
        except Exception:
            pass
        return ""

    # ---- hooks ------------------------------------------------------------

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        """
        Stash the incoming prompt + chat metadata. We re-emit it from outlet
        when we have the assistant response.

        Also: inject `user="<chat_id>:<owui_user_id>"` into the body so it
        flows to OpenRouter and ends up in `external_user`, which the Monitor
        proxy uses to link this Prompt row to Request rows.
        """
        try:
            chat_id = self._chat_id(body)
            u = self._user_fields(__user__)

            # Inject user marker for OpenRouter mapping. Don't overwrite if caller
            # already set it (some integrations rely on their own value).
            try:
                if isinstance(body, dict) and not body.get("user") and chat_id and u["id"]:
                    body["user"] = f"{chat_id}:{u['id']}"
            except Exception:
                pass

            self._pending[chat_id or "default"] = {
                "chat_id": chat_id,
                "user_id": u["id"],
                "user_name": u["name"],
                "user_email": u["email"],
                "model_hint": (body or {}).get("model", "") or "",
                "messages": (body or {}).get("messages", []) or [],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except Exception:
            pass
        return body

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        """
        Ship prompt+response to Monitor /api/ingest_text. No tokens, no cost
        — those come from OpenRouter sync.
        """
        try:
            chat_id = self._chat_id(body)
            ctx = self._pending.pop(chat_id or "default", None)
            if ctx is None:
                # outlet without inlet (rare race / orphan) — best-effort rebuild
                u = self._user_fields(__user__)
                ctx = {
                    "chat_id": chat_id,
                    "user_id": u["id"],
                    "user_name": u["name"],
                    "user_email": u["email"],
                    "model_hint": (body or {}).get("model", "") or "",
                    "messages": (body or {}).get("messages", []) or [],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

            response_text = self._extract_response(body)
            # Compute fingerprint of the LAST user message — must match the
            # one the proxy injects on the upstream request. This is how
            # we link this Prompt row to all the Request rows belonging to
            # the same user turn (across tool-chain rounds).
            user_msg_hash = _fingerprint(_last_user_text(ctx.get("messages") or []))

            payload = {
                "chat_id": ctx.get("chat_id", ""),
                "user_id": ctx.get("user_id", ""),
                "user_name": ctx.get("user_name", ""),
                "user_email": ctx.get("user_email", ""),
                "model_hint": ctx.get("model_hint", "")
                              or (body or {}).get("model", "")
                              or "",
                "messages": ctx.get("messages", []),
                "response": response_text,
                "timestamp": ctx.get("timestamp"),
                "user_msg_hash": user_msg_hash,
            }

            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    await client.post(
                        self.valves.MONITOR_URL.rstrip("/") + "/api/ingest_text",
                        json=payload,
                    )
            except Exception:
                # Silent: never break the chat.
                pass
        except Exception:
            pass
        return body
