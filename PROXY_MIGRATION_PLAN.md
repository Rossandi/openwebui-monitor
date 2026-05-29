# План: миграция Monitor на HTTP-прокси OpenRouter

**Версия:** 1.0 · **Цель:** получить 100%-точные данные о расходе токенов и стоимости, включая tool-calling rounds и Image Studio.

---

## 1. Проблема и цель

### 1.1 Текущая проблема
OpenWebUI Function (filter) видит только **финальный** вызов LLM. Когда модель использует tool-calling (Tavily-поиск → ответ), OpenRouter выставляет счёт за **каждый** round, но Monitor записывает только последний.

**Пример (реальные данные за 27.05.2026):**
| Источник | input tokens | output tokens | cost |
|---|---|---|---|
| OpenRouter (4 round'а) | 10 965 | 2 129 | **$0.0349** |
| Monitor (только last round) | 4 583 | 1 900 | **$0.0084** |
| **Расхождение** | **−2.4×** | **−1.1×** | **−4.2×** |

Image Studio вообще не виден — он идёт мимо Function.

### 1.2 Цель
- Записывать **каждый** billable вызов к OpenRouter (chat/image/embedding/audio)
- Стоимость и токены — из официальной API OpenRouter (`GET /api/v1/generation?id=`)
- Привязывать к OpenWebUI пользователю
- Сохранять промт/ответ для аудита
- **Никогда не ломать чат**, даже при сбое Monitor

---

## 2. Архитектура

### 2.1 Было
```
[OpenWebUI] ──HTTP──► [OpenRouter]
     │
     │ Function (filter)
     └──► [Monitor /api/ingest]   ← теряет 80% данных
```

### 2.2 Будет
```
                ┌─── всё транзитом ───┐
[OpenWebUI] ──HTTP──► [Monitor :8088/v1] ──HTTP──► [OpenRouter]
   ENV:                     │                          │
   OPENAI_API_BASE_URL  │                          │
   = http://monitor:    │                          │
   8088/v1              │ catch X-Generation-Id    │
                        │ from response headers    │
                        ▼                          │
                  [async queue]                    │
                        │                          │
                        │ через 1-3 сек            │
                        └──GET /generation?id=─────┘
                                │
                                ▼
                          [SQLite]
                          точные tokens, cost, provider

[OpenWebUI Function]  ─────► [Monitor /api/ingest_text]
(оставляем для текстов)        (промт, ответ, chat_id, user)
```

### 2.3 Поток одного пользовательского запроса (с tool-calling)
1. Пользователь пишет «расскажи про ИИ» в OpenWebUI.
2. OpenWebUI делает 4 HTTP-вызова к `http://monitor:8088/v1/chat/completions` (round 1, 2, 3, 4 в tool-chain).
3. **Каждый** запрос Monitor проксирует на `https://openrouter.ai/api/v1/chat/completions`, отдаёт ответ обратно в OpenWebUI без задержки. Параллельно копирует `X-Generation-Id` из response headers в очередь.
4. После завершения цепочки Function `outlet` отправляет на `/api/ingest_text` итоговый промт + ответ + `chat_id` + `user_email`.
5. Async-воркер раз в 2 сек берёт ID из очереди, дёргает `/api/v1/generation?id=` и пишет в БД точные tokens/cost.
6. Worker связывает 4 generation-записи и 1 text-запись через `chat_id` (берём из metadata).

---

## 3. Изменения в файлах

| Файл | Что меняем |
|---|---|
| `monitor/main.py` | Новый endpoint `@app.api_route('/v1/{path:path}', methods=['GET','POST','PUT','DELETE'])` — прозрачный прокси. Новый endpoint `POST /api/ingest_text` — приёмник текстов от Function. |
| `monitor/proxy.py` *(новый)* | Логика проксирования: streaming/non-streaming, capture headers, push to queue. Fallback: при любой нашей ошибке — пробрасывать запрос как есть. |
| `monitor/openrouter_sync.py` *(новый)* | Async-воркер. На старте FastAPI поднимает background task. Каждые 2 сек: pop ID → GET /generation → UPSERT в БД. Retry с экспоненциальным backoff (max 5 попыток). |
| `monitor/models.py` | Расширить `Request`: `generation_id` (UNIQUE, indexed), `provider_name`, `api_type` (chat/image/embedding/...), `external_user`, `request_id` (для группировки rounds одного логического запроса), `is_tool_round` (bool). Старые поля сохраняем. |
| `monitor/database.py` | Миграция: добавить новые колонки. Старая БД стирается (см. § 11). |
| `monitor_function.py` | Function больше **не** пишет в `/api/ingest`. Шлёт только текст в `/api/ingest_text`: `chat_id`, `user_id`, `user_email`, `messages`, `response`, `model_hint`, `timestamp`. |
| `monitor/main.py` (логи и дашборд) | Изменить вью: список запросов теперь из `requests` + `JOIN texts ON chat_id`. Группировка по `request_id` для UI «свернуть tool-rounds в один логический запрос». |
| `docker-compose.yml` | OpenWebUI env: `OPENAI_API_BASE_URL=http://monitor:8088/v1` (вместо openrouter.ai). Также `IMAGES_OPENAI_API_BASE_URL` → тоже на monitor. Monitor нужен env-ключ OpenRouter для sync: `OPENROUTER_API_KEY=${OPENAI_API_KEY}`. Добавить сервис `postgres:16-alpine` с volume `postgres-data` и healthcheck. Monitor `DATABASE_URL=postgresql://owmonitor:owmonitor@postgres:5432/owmonitor`. |
| `requirements.txt` | Добавить `psycopg2-binary` (драйвер PostgreSQL). |

---

## 4. Контракты новых endpoints в Monitor

### 4.1 `ANY /v1/{path:path}` — прокси
Прозрачно форвардит на `https://openrouter.ai/api/v1/{path}`. Поддерживает:
- streaming (SSE) — `text/event-stream`
- multipart (для image upload)
- все методы
- сохранение всех headers кроме `host`

Response headers пробрасываются клиенту **как есть** (включая `X-Generation-Id`). До отправки клиенту читаем header, кладём в очередь `(generation_id, captured_at, request_body_hash)`.

**Поведение при ошибке:**
- Timeout к OpenRouter (>60 сек): 504 клиенту.
- Сетевая ошибка: 502 клиенту.
- Наша внутренняя ошибка (исключение в proxy.py): пробрасываем raw response **без** capture.

### 4.2 `POST /api/ingest_text` — текст от Function
Body:
```json
{
  "chat_id": "uuid",
  "user_id": "owui-user-uuid",
  "user_email": "user@corp.ru",
  "user_name": "Ross",
  "model_hint": "openai/gpt-5.1",
  "messages": [{"role": "user", "content": "..."}],
  "response": "...",
  "timestamp": "2026-05-27T16:21:25Z"
}
```
Записывает в таблицу `prompts`. **Не** проставляет tokens/cost.

### 4.3 `POST /api/ingest` — оставляем для совместимости
Старый endpoint оставляем (возвращает 200 ok), но игнорируем входящие данные (deprecated stub). Это для случая, если Function не успели обновить — старый Function ещё может слать запросы, мы их не теряем, просто не пишем.

---

## 5. Схема БД (после миграции)

### 5.1 Таблица `requests` (источник: proxy + OpenRouter API)
```
id            INTEGER PRIMARY KEY
generation_id TEXT UNIQUE NOT NULL    ← gen-1779890105-vbDtYygOjXF8Guhyjilk
created_at    DATETIME NOT NULL       ← из OpenRouter API
model         TEXT                    ← openai/gpt-5.1
provider_name TEXT                    ← OpenAI / NovitaAI / AtlasCloud
api_type      TEXT                    ← chat / image / embedding / tts / stt
tokens_prompt INTEGER
tokens_completion INTEGER
native_tokens_reasoning INTEGER       ← для thinking-моделей
total_cost    REAL                    ← USD, точно от OpenRouter
generation_time INTEGER               ← ms
external_user TEXT                    ← OpenWebUI user_id (через body.user)
request_id    TEXT                    ← OpenRouter group id (свяжет 4 round'а)
streamed      BOOLEAN
finish_reason TEXT
sync_status   TEXT                    ← pending / synced / failed
sync_attempts INTEGER
```

### 5.2 Таблица `prompts` (источник: Function)
```
id          INTEGER PRIMARY KEY
chat_id     TEXT INDEXED            ← uuid чата в OpenWebUI
user_id     TEXT                    ← uuid пользователя
user_email  TEXT
user_name   TEXT
model_hint  TEXT                    ← модель, выбранная пользователем
messages_json TEXT                  ← полные сообщения чата
response    TEXT
created_at  DATETIME
```

### 5.3 Связывание (linking)
Через `external_user` + `chat_id` + временное окно ±30 сек:
- В прокси при форварде **инъектируем** в body `"user": "<chat_id>:<owui_user_id>"`. OpenRouter сохранит это в `external_user`.
- При sync парсим `external_user` обратно на `chat_id` и `user_id`.
- В UI: для каждой записи `requests` находим соответствующую `prompts` по `chat_id` и берём промт/ответ.

Если `prompts`-записи нет (Image Studio без Function, или Function не сработала) — показываем только данные из `requests`. Промт остаётся пустым.

---

## 6. План реализации по этапам

### Этап 1 — Прокси + capture (можно протестировать сразу)
1. Создать `monitor/proxy.py` с прозрачным форвардом
2. Добавить endpoint `/v1/{path:path}` в `main.py`
3. Очередь в памяти (`asyncio.Queue`), без БД, просто `print()` пойманных ID
4. Переключить `OPENAI_API_BASE_URL` в compose
5. Тест: чат работает, в логах monitor появляются `X-Generation-Id`

### Этап 2 — Async sync
1. Создать `monitor/openrouter_sync.py` с background-task
2. Добавить таблицу `requests`, миграция
3. Запись в БД при успешном GET /generation
4. Тест: после 1 промпта с tool-calling в БД 4 записи

### Этап 3 — Function переписать
1. Обновить `monitor_function.py` — шлёт только в `/api/ingest_text`
2. Создать `monitor/models.py::Prompt`, endpoint `/api/ingest_text`
3. Update Function в OpenWebUI через API
4. Тест: каждый чат-запрос → 1 запись `prompts`

### Этап 4 — UI/Dashboard
1. Изменить вью `/api/logs` — JOIN `requests` + `prompts`
2. Группировка по `request_id` (свернуть tool rounds)
3. Чекбокс «показать все rounds» / «свернуть»
4. Колонки: дата, пользователь, модель, провайдер, промт-preview, in/out tokens, cost, latency

### Этап 5 — Edge cases
1. Картинки (Image Studio) — нет промта/ответа в Function, есть только в proxy
2. Streaming — headers приходят до байтов, capture работает
3. Ошибки proxy — пробрасывать, не блокировать чат
4. Старый Function запас — endpoint `/api/ingest` принимает но игнорирует

---

## 7. Безопасность и fallback

**Критично:** Monitor теперь on-path. Если он упадёт — чат сломается. Митигации:

1. **Healthcheck в compose** — `restart: unless-stopped` уже стоит. Добавить `healthcheck` с retry.
2. **Внутренний try/except в proxy** — при любой нашей ошибке (capture, queue, etc.) **проксировать без capture**. Никогда не блокировать.
3. **Timeout к OpenRouter** — 60 сек hard timeout, после — 504.
4. **Memory queue limit** — `asyncio.Queue(maxsize=1000)`. При переполнении — drop с warning в логи.
5. **OpenRouter sync failure** — после 5 неуспешных попыток мечтаем запись `failed`. Не блокируем новые.
6. **Fallback на прямой OpenRouter в OpenWebUI** — Admin Panel → Settings → Connections можно временно вернуть `https://openrouter.ai/api/v1` если Monitor упал.

---

## 8. Тестирование

### Юнит
- Mock OpenRouter `/generation?id=...` → проверка парсинга
- Streaming SSE → headers capture

### Интеграция (вручную)
1. **Простой чат** (без tool): `openai/gpt-5.1` + «привет». Ожидание: 1 запись в `requests`, 1 в `prompts`.
2. **С tool-calling**: `openai/gpt-5.1` + «что сегодня в новостях». Ожидание: 4 записи в `requests` (одна `request_id`), 1 в `prompts`.
3. **Image Studio**: «нарисуй кота». Ожидание: 1 запись `requests` с `api_type=image`, **нет** записи в `prompts`.
4. **Monitor падает**: `docker stop monitor` во время чата. Ожидание: пользователь получает 502 (не висит вечно). Что некомфортно, но это компромисс on-path архитектуры.
5. **Cost-checking**: после 5 разных промтов сумма `total_cost` в Monitor должна совпасть с OpenRouter activity ±$0.01.

### Регрессии
- Старые промты (text) не теряются — Function продолжает работать
- Дашборд открывается без ошибок
- `model_pricing.json` больше не нужен — стоимость теперь приходит от OpenRouter (но файл оставляем для совместимости)

---

## 9. Изменения в инструкциях

После реализации обновить:
- `README.md` — раздел «Как это работает» + новый раздел «Прокси-режим»
- `INSTALL_FOR_ADMIN.md` (для нового файла .docx) — `OPENAI_API_BASE_URL=http://monitor:8088/v1` обязательно, **не** оставлять `openrouter.ai/api/v1`
- `monitor_function.py` — обновить в репо
- Добавить в env `.env.example`: `OPENROUTER_API_KEY=${OPENAI_API_KEY}` (тот же ключ, для sync)

---

## 10. Решения по открытым вопросам *(зафиксировано 27.05.2026)*

1. **DB engine — PostgreSQL** *(сразу, не SQLite).* Добавляем контейнер `postgres:16-alpine` в `docker-compose.yml` с volume для данных и healthcheck. `DATABASE_URL=postgresql://owmonitor:<pwd>@postgres:5432/owmonitor`. В `requirements.txt` добавить `psycopg2-binary`.
2. **UI rounds-grouping — свернуть по умолчанию.** В дашборде один логический запрос пользователя = одна строка с суммарными tokens/cost. По клику разворачивается — видны все rounds с провайдером и стоимостью каждого. Группировка по `request_id` от OpenRouter.
3. **TTL для pending-очереди** — после 30 сек попыток помечаем `failed`, не ретраим. Записи сохраняются для аудита.
4. **Валюта — USD с центами** *(без конвертации в RUB).* Везде в UI: `$0.0349` (4 знака после точки для small spend). В API: `total_cost_usd` (REAL). Никакого курса ЦБ.

---

## 11. Стирание старых данных

Перед началом Этапа 2:
```bash
docker compose down
rm -f ./data/monitor.db          # старая SQLite (если осталась)
docker volume rm openwebuimonitorv2docker_postgres-data  # на всякий случай если переразворачиваем
docker compose up -d
```
Volume `open-webui-data` **не** трогаем — там пользователи, настройки, Function.

---

## 12. Оценка работ

| Этап | Время | Риск |
|---|---|---|
| 1 — Proxy + capture | 45 мин | Низкий — streaming через httpx стандартно |
| 2 — Async sync + DB | 60 мин | Средний — миграция БД |
| 3 — Function rewrite | 30 мин | Низкий |
| 4 — UI dashboard | 60 мин | Средний — нужно перерисовать вью |
| 5 — Edge + тестирование | 60 мин | Высокий — Image Studio проверять отдельно |
| **Итого** | **~4 часа** | |
