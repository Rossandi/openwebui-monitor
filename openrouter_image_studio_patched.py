"""
title: OpenRouter Image Studio
version: 0.5
description: Универсальный Pipe для генерации и редактирования изображений через OpenRouter. Multi-image input, контроль размера/aspect, форсирование image output. v0.4: OPENROUTER_BASE_URL для проксирования через Monitor. v0.5: attribution в Monitor /api/ingest_text — каждая картинка имеет user_email и preview промта в дашборде.
author: ROSS
"""

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, Field


def _fingerprint(text: str) -> str:
    """SHA1[:16] of the last user message. MUST match monitor.proxy.fingerprint_user_msg
    and monitor_function.py — that's how Monitor links Prompt rows to Request rows."""
    if not text:
        return "no-user-msg"
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]


class Pipe:
    class Valves(BaseModel):
        # Ключ OpenRouter.
        OPENROUTER_API_KEY: str = Field(
            default="",
            description="OpenRouter API key",
        )

        # Базовый URL OpenAI-совместимого endpoint.
        # По умолчанию — Monitor-прокси (он проксирует в OpenRouter и учитывает
        # каждый запрос в дашборде). Если хочешь идти напрямую к OpenRouter
        # без учёта — поставь https://openrouter.ai/api/v1
        OPENROUTER_BASE_URL: str = Field(
            default="http://monitor:8088/v1",
            description="OpenAI-compatible endpoint base URL. Use http://monitor:8088/v1 to route through Monitor proxy for accounting.",
        )

        # Модель OpenRouter.
        MODEL: str = Field(
            default="google/gemini-3.1-flash-image-preview",
            description="OpenRouter image model",
        )

        # Внутренний адрес Open WebUI из контейнера Open WebUI.
        OPENWEBUI_BASE_URL: str = Field(
            default="http://localhost:8080",
            description="Open WebUI internal URL",
        )

        # URL Monitor для записи промта/ответа (attribution в дашборде).
        # Если пусто или Monitor недоступен — биллинг всё равно учтётся через
        # прокси, просто без email и текста промта в Журнале.
        MONITOR_URL: str = Field(
            default="http://monitor:8088",
            description="Monitor URL for prompt/response attribution. Empty = disable.",
        )

        # Таймауты на генерацию и сохранение файлов.
        TIMEOUT: int = Field(
            default=180,
            description="Request timeout in seconds",
        )

        # Если True, Pipe будет пытаться использовать последнюю картинку из истории.
        USE_LAST_IMAGE_FROM_HISTORY: bool = Field(
            default=True,
            description="Use last image from chat history when no image is attached",
        )

        # Максимальный размер диагностического JSON в ответе при ошибке.
        DEBUG_RESPONSE_CHARS: int = Field(
            default=4000,
            description="Max debug response length",
        )

        # Параметры image_config (OpenRouter image generation API)
        IMAGE_SIZE: str = Field(
            default="2K",
            description="image_config.image_size: '', 0.5K, 1K, 2K, 4K",
        )

        ASPECT_RATIO: str = Field(
            default="",
            description="image_config.aspect_ratio, например 16:9, 1:1, 9:16. Пусто = не передавать",
        )

        MAX_REFERENCE_IMAGES: int = Field(
            default=8,
            description="Max reference images per request (Gemini image)",
        )

        PROMPT_PREFIX: str = Field(
            default="Сгенерируй изображение по следующему описанию. Верни именно изображение, не текстовый ответ.\n\n",
            description="Префикс к промпту, форсирующий image output. Пусто = выключить",
        )

    def __init__(self):
        self.type = "pipe"
        self.name = "OpenRouter Image Studio"
        self.valves = self.Valves()

    # -------------------------------------------------------------------------
    # Вспомогательные функции
    # -------------------------------------------------------------------------

    def _clean_api_key(self) -> str:
        return (self.valves.OPENROUTER_API_KEY or "").strip()

    def _openrouter_chat_completions_url(self) -> str:
        """
        Полный URL endpoint для chat/completions.
        Учитывает что OPENROUTER_BASE_URL может уже содержать /v1 в конце.
        """
        base = (self.valves.OPENROUTER_BASE_URL or "").rstrip("/")
        if not base:
            base = "https://openrouter.ai/api/v1"
        return f"{base}/chat/completions"

    def _get_auth_token_from_request(self, __request__=None) -> str:
        if __request__ is None:
            return ""
        try:
            auth = __request__.headers.get("authorization", "")
            if not auth:
                return ""
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
            return auth.strip()
        except Exception:
            return ""

    def _normalize_openwebui_url(self, path_or_url: str) -> str:
        if not path_or_url:
            return ""
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        base = self.valves.OPENWEBUI_BASE_URL.rstrip("/") + "/"
        return urljoin(base, path_or_url.lstrip("/"))

    def _guess_mime_from_url(self, url: str) -> str:
        lower = url.lower().split("?")[0]
        if lower.endswith(".jpg") or lower.endswith(".jpeg"):
            return "image/jpeg"
        if lower.endswith(".webp"):
            return "image/webp"
        if lower.endswith(".gif"):
            return "image/gif"
        return "image/png"

    def _data_url_to_bytes(self, data_url: str) -> tuple[str, bytes]:
        match = re.match(
            r"data:(image/[a-zA-Z0-9.+-]+);base64,(.+)",
            data_url,
            re.DOTALL,
        )
        if not match:
            raise ValueError("Некорректный data URL изображения")
        mime_type = match.group(1)
        b64_data = match.group(2).replace("\n", "").replace("\r", "")
        image_bytes = base64.b64decode(b64_data)
        return mime_type, image_bytes

    def _bytes_to_data_url(self, image_bytes: bytes, mime_type: str) -> str:
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:{mime_type};base64,{encoded}"

    def _extract_prompt_from_messages(self, messages: list) -> str:
        if not messages:
            return ""
        last = messages[-1]
        content = last.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts).strip()
        return str(content).strip()

    def _extract_image_urls_from_openrouter_response(self, data: dict) -> list:
        result = []
        for choice in data.get("choices", []) or []:
            message = choice.get("message", {}) or {}
            for img in message.get("images", []) or []:
                if not isinstance(img, dict):
                    continue
                image_url = img.get("image_url", {})
                if isinstance(image_url, dict):
                    url = image_url.get("url")
                    if isinstance(url, str) and url.startswith("data:image/"):
                        result.append(url)
            content = message.get("content", "")
            if isinstance(content, str):
                found = re.findall(
                    r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\n\r]+",
                    content,
                )
                result.extend(found)
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    image_url = item.get("image_url", {})
                    if isinstance(image_url, dict):
                        url = image_url.get("url")
                        if isinstance(url, str) and url.startswith("data:image/"):
                            result.append(url)
        unique = []
        seen = set()
        for url in result:
            if url not in seen:
                unique.append(url)
                seen.add(url)
        return unique

    def _extract_markdown_image_urls_from_text(self, text: str) -> list:
        if not isinstance(text, str):
            return []
        return re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)

    def _extract_last_image_from_history(self, messages: list) -> Optional[str]:
        if not self.valves.USE_LAST_IMAGE_FROM_HISTORY:
            return None
        for message in reversed(messages):
            content = message.get("content", "")
            if isinstance(content, str):
                urls = self._extract_markdown_image_urls_from_text(content)
                if urls:
                    return urls[-1]
            if isinstance(content, list):
                for item in reversed(content):
                    if not isinstance(item, dict):
                        continue
                    image_url = item.get("image_url", {})
                    if isinstance(image_url, dict):
                        url = image_url.get("url")
                        if isinstance(url, str):
                            return url
        return None

    def _extract_attached_image_urls(self, messages: list) -> list:
        if not messages:
            return []
        last = messages[-1]
        result = []
        content = last.get("content", "")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                image_url = item.get("image_url", {})
                if isinstance(image_url, dict):
                    url = image_url.get("url")
                    if isinstance(url, str):
                        result.append(url)
        for key in ("files", "attachments"):
            files = last.get(key, []) or []
            if not isinstance(files, list):
                continue
            for f in files:
                if not isinstance(f, dict):
                    continue
                candidate_urls = [
                    f.get("url"),
                    f.get("path"),
                    f.get("content_url"),
                    f.get("download_url"),
                ]
                nested_file = f.get("file")
                if isinstance(nested_file, dict):
                    candidate_urls.extend([
                        nested_file.get("url"),
                        nested_file.get("path"),
                        nested_file.get("content_url"),
                        nested_file.get("download_url"),
                    ])
                    file_id = nested_file.get("id")
                    if file_id:
                        candidate_urls.append(f"/api/v1/files/{file_id}/content")
                file_id = f.get("id")
                if file_id:
                    candidate_urls.append(f"/api/v1/files/{file_id}/content")
                for url in candidate_urls:
                    if isinstance(url, str) and url:
                        result.append(url)
        unique = []
        seen = set()
        for url in result:
            if url not in seen:
                unique.append(url)
                seen.add(url)
        return unique

    async def _download_image_as_data_url(self, image_url: str, token: str = "") -> str:
        if image_url.startswith("data:image/"):
            return image_url
        absolute_url = self._normalize_openwebui_url(image_url)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(
            timeout=self.valves.TIMEOUT, follow_redirects=True
        ) as client:
            resp = await client.get(absolute_url, headers=headers)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            content_type = self._guess_mime_from_url(absolute_url)
        return self._bytes_to_data_url(resp.content, content_type)

    async def _save_generated_image_to_openwebui(
        self, data_url: str, token: str = ""
    ) -> str:
        mime_type, image_bytes = self._data_url_to_bytes(data_url)
        ext = mime_type.split("/")[-1]
        if ext == "jpeg":
            ext = "jpg"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as client:
            resp = await client.post(
                f"{self.valves.OPENWEBUI_BASE_URL.rstrip('/')}/api/v1/files/",
                headers=headers,
                files={
                    "file": (
                        f"openrouter-image.{ext}",
                        image_bytes,
                        mime_type,
                    )
                },
            )
        resp.raise_for_status()
        file_id = resp.json().get("id")
        if not file_id:
            raise ValueError("Open WebUI не вернул id сохранённого файла")
        return f"/api/v1/files/{file_id}/content"

    def _make_openrouter_messages(
        self, prompt: str, source_image_data_urls: list
    ) -> list:
        if source_image_data_urls:
            content = [{"type": "text", "text": prompt}]
            for data_url in source_image_data_urls:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": data_url},
                })
            return [{"role": "user", "content": content}]
        return [{"role": "user", "content": prompt}]

    async def _report_to_monitor(
        self,
        messages: list,
        response_text: str,
        prompt_text: str,
        chat_id: str,
        user: Optional[dict],
    ) -> None:
        """
        Send prompt/response text to Monitor for dashboard attribution.

        Mirrors monitor_function.py outlet payload. Silent on failure — never
        breaks image generation. The proxy already captured the billing data;
        this is purely the human-readable layer.
        """
        if not self.valves.MONITOR_URL:
            return
        u = user if isinstance(user, dict) else {}
        payload = {
            "chat_id": chat_id or "",
            "user_id": str(u.get("id") or ""),
            "user_name": str(u.get("name") or ""),
            "user_email": str(u.get("email") or ""),
            "model_hint": self.valves.MODEL,
            "messages": messages,
            "response": response_text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_msg_hash": _fingerprint(prompt_text),
        }
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(
                    self.valves.MONITOR_URL.rstrip("/") + "/api/ingest_text",
                    json=payload,
                )
        except Exception:
            # Silent — Monitor unreachable should never block image generation.
            pass

    def _short_debug_json(self, data: Any) -> str:
        try:
            text = json.dumps(data, ensure_ascii=False, indent=2)
        except Exception:
            text = str(data)
        limit = int(self.valves.DEBUG_RESPONSE_CHARS or 4000)
        if len(text) > limit:
            text = text[:limit] + "\n... <обрезано>"
        return text

    # -------------------------------------------------------------------------
    # Основной метод Pipe
    # -------------------------------------------------------------------------

    async def pipe(self, body: dict, __user__: dict = None, __request__=None):
        api_key = self._clean_api_key()
        if not api_key:
            return "❌ Не указан OPENROUTER_API_KEY в настройках функции."

        messages = body.get("messages", []) or []
        if not messages:
            return "❌ Нет сообщений для обработки."

        prompt = self._extract_prompt_from_messages(messages)
        if not prompt:
            return "❌ Пустой prompt. Напишите, что нужно нарисовать или изменить."

        prompt_prefix = self.valves.PROMPT_PREFIX or ""
        if prompt_prefix and not prompt.startswith(prompt_prefix.strip()[:20]):
            effective_prompt = prompt_prefix + prompt
        else:
            effective_prompt = prompt

        token = self._get_auth_token_from_request(__request__)

        attached_image_urls = self._extract_attached_image_urls(messages)
        source_image_urls: list = []
        source_mode = "generate"

        if attached_image_urls:
            source_image_urls = list(attached_image_urls)
            source_mode = "edit_attached"

        if not source_image_urls:
            history_image_url = self._extract_last_image_from_history(messages)
            if history_image_url:
                source_image_urls = [history_image_url]
                source_mode = "edit_history"

        max_refs = max(1, int(self.valves.MAX_REFERENCE_IMAGES or 8))
        if len(source_image_urls) > max_refs:
            source_image_urls = source_image_urls[:max_refs]

        source_image_data_urls: list = []
        for ref_url in source_image_urls:
            try:
                data_url = await self._download_image_as_data_url(
                    ref_url, token=token
                )
                source_image_data_urls.append(data_url)
            except Exception as e:
                return (
                    "❌ Не удалось прочитать исходное изображение для редактирования.\n\n"
                    f"Источник: `{ref_url}`\n\n"
                    f"Ошибка: `{e}`"
                )

        openrouter_messages = self._make_openrouter_messages(
            prompt=effective_prompt,
            source_image_data_urls=source_image_data_urls,
        )

        payload: dict = {
            "model": self.valves.MODEL,
            "messages": openrouter_messages,
            "modalities": ["image", "text"],
        }

        image_config: dict = {}
        aspect_ratio = (self.valves.ASPECT_RATIO or "").strip()
        if aspect_ratio:
            image_config["aspect_ratio"] = aspect_ratio
        image_size = (self.valves.IMAGE_SIZE or "").strip()
        if image_size:
            image_config["image_size"] = image_size
        if image_config:
            payload["image_config"] = image_config

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://openwebui.local",
            "X-Title": "Open WebUI Image Studio",
        }

        # === v0.4 CHANGE ===
        # Используем настраиваемый base URL вместо hardcoded openrouter.ai.
        # По умолчанию указывает на Monitor-прокси для учёта в дашборде.
        target_url = self._openrouter_chat_completions_url()

        try:
            async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as client:
                resp = await client.post(
                    target_url,
                    headers=headers,
                    json=payload,
                )
        except Exception as e:
            return f"❌ Не удалось обратиться к OpenRouter: `{e}`"

        if resp.status_code != 200:
            return (
                f"❌ Ошибка OpenRouter `{resp.status_code}`.\n\n"
                f"Endpoint: `{target_url}`\n\n"
                f"Режим: `{source_mode}`\n\n"
                f"Ответ:\n```text\n{resp.text[:4000]}\n```"
            )

        try:
            data = resp.json()
        except Exception:
            return (
                "❌ OpenRouter вернул не JSON.\n\n"
                f"Ответ:\n```text\n{resp.text[:4000]}\n```"
            )

        generated_data_urls = self._extract_image_urls_from_openrouter_response(data)

        if not generated_data_urls:
            text_reply = ""
            image_tokens = None
            try:
                choices = data.get("choices") or []
                if choices:
                    msg = choices[0].get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, str):
                        text_reply = content
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text_reply += str(item.get("text", ""))
                usage = data.get("usage") or {}
                details = usage.get("completion_tokens_details") or {}
                image_tokens = details.get("image_tokens")
            except Exception:
                pass

            if image_tokens == 0 and text_reply:
                preview = text_reply[:1200]
                if len(text_reply) > 1200:
                    preview += "\n... <обрезано>"
                return (
                    "❌ Модель ответила текстом вместо изображения "
                    f"(image_tokens = 0).\n\n"
                    f"Режим: `{source_mode}` · референсов: "
                    f"{len(source_image_data_urls)}\n\n"
                    "Попробуйте:\n"
                    "- начать промпт с явного «Сгенерируй изображение:»;\n"
                    "- включить или усилить `PROMPT_PREFIX` в настройках Pipe;\n"
                    "- переформулировать в повелительном наклонении.\n\n"
                    f"Текстовый ответ модели:\n```text\n{preview}\n```"
                )

            return (
                "❌ OpenRouter не вернул изображение.\n\n"
                f"Режим: `{source_mode}`\n\n"
                "Диагностика ответа:\n"
                f"```json\n{self._short_debug_json(data)}\n```"
            )

        markdown_images = []
        for index, data_url in enumerate(generated_data_urls, start=1):
            try:
                file_url = await self._save_generated_image_to_openwebui(
                    data_url, token=token
                )
                markdown_images.append(f"![generated image {index}]({file_url})")
            except Exception as e:
                markdown_images.append(
                    f"❌ Ошибка сохранения изображения {index}: `{e}`"
                )

        result = "\n\n".join(markdown_images)

        refs_count = len(source_image_data_urls)
        config_summary_parts = []
        if image_config.get("aspect_ratio"):
            config_summary_parts.append(f"ar={image_config['aspect_ratio']}")
        if image_config.get("image_size"):
            config_summary_parts.append(f"size={image_config['image_size']}")
        config_summary = (
            " · " + " · ".join(config_summary_parts) if config_summary_parts else ""
        )

        result += (
            f"\n\n_Режим: `{source_mode}` · Модель: `{self.valves.MODEL}`"
            f" · референсов: {refs_count}{config_summary}_"
        )

        # Attribution → Monitor (v0.5).
        # IMPORTANT: hash effective_prompt (with PROMPT_PREFIX), not the raw
        # user text. The proxy fingerprints the messages it forwards upstream,
        # and those include the prefix. Without this match the Prompt row
        # never links to the Request row and the dashboard shows (unknown).
        # Silent on failure — never block generation.
        try:
            chat_id = (body or {}).get("chat_id") or (body or {}).get("id") or ""
            await self._report_to_monitor(
                messages=messages,
                response_text=result,
                prompt_text=effective_prompt,
                chat_id=str(chat_id),
                user=__user__,
            )
        except Exception:
            pass

        return result
