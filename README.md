# OpenWebUI Monitor v2

**Точный учёт расхода токенов и стоимости** для корпоративного OpenWebUI поверх OpenRouter — включая tool-calling, Image Studio и любые multi-step цепочки.

## Содержание
1. Что это такое
2. Архитектура (как работает)
3. Что изменилось в v2 (миграция с v1)
4. Быстрый запуск
5. Установка Function в OpenWebUI
6. Дашборд
7. PostgreSQL
8. FAQ / диагностика

---

## 1. Что это такое

Стек из **трёх** Docker-контейнеров:

| Сервис | Роль |
|---|---|
| **open-webui** | Веб-интерфейс чатов. Все его запросы к LLM ходят **через Monitor** (см. § 2). |
| **monitor** | Прозрачный HTTP-прокси перед OpenRouter + дашборд аналитики + Postgres-API. |
| **postgres** | Хранилище учётных записей и текстов промтов. |

Опционально подключаются `jupyter` (code interpreter) и любые Pipelines.

---

## 2. Архитектура

```
[Пользователи] → [OpenWebUI] ──HTTP──► [Monitor :8088/v1] ──HTTP──► [OpenRouter]
                                            │                            │
                                            │ X-Generation-Id            │
                                            ▼                            │
                                      [async queue]                      │
                                            │  (через 1-60 сек)          │
                                            └─GET /generation?id=────────┘
                                                       │
                                                       ▼
                                                  [PostgreSQL]
                                                  requests (точные деньги)
                                                  prompts  (читаемые тексты)
                                                       ▲
                                                       │ POST /api/ingest_text
                                                       │
                                              [OpenWebUI Function]
                                              (filter, global — захватывает
                                               текст промта/ответа + chat_id)
```

**Ключевая идея:** Monitor — это **HTTP-прокси на пути к OpenRouter**. Он видит **каждый** billable вызов LLM, включая промежуточные tool-call rounds (Tavily-поиск, кальки и т.п.). Получает реальную стоимость от OpenRouter `/api/v1/generation?id=<gen-id>` и линкует с текстом промта через Function по `chat_id`.

Function-only архитектура v1 видела только финальный ответ → недосчёт в 4-5 раз при tool-calling. Подробности в `PROXY_MIGRATION_PLAN.md`.

---

## 3. Что изменилось в v2 (миграция с v1)

| Аспект | v1 | v2 |
|---|---|---|
| Откуда берётся cost/tokens | Function парсит `body.usage` (только финальный round) | Прокси ловит каждый `X-Generation-Id`, sync-воркер запрашивает OpenRouter API |
| Точность учёта | Недосчёт в 4-5 раз при tool-calling | 100% точно (источник правды — OpenRouter) |
| Image Studio | Не отслеживался вообще | Полностью отслеживается через прокси |
| БД | SQLite | PostgreSQL 16 |
| Файл цен `model_pricing.json` | Нужен (локальные цены) | **Не используется** — цена приходит от OpenRouter |
| `OPENAI_API_BASE_URL` в OpenWebUI | `https://openrouter.ai/api/v1` | `http://monitor:8088/v1` |
| Function | v1.0 — отправляет tokens+cost | v2.0 — отправляет только текст промта/ответа |

**Миграция:** удалить старую БД (`./data/monitor.db`), пересобрать стек, обновить Function через UI/API. Подробно в `PROXY_MIGRATION_PLAN.md`, § 11.

---

## 4. Быстрый запуск

**Шаг 1 — Скопировать проект:**
```bash
git clone <repo> openwebui-monitor && cd openwebui-monitor
cp .env.example .env
```

**Шаг 2 — Заполнить `.env`:**
```
OPENAI_API_KEY=sk-or-v1-ваш-ключ-openrouter
TAVILY_API_KEY=tvly-ваш-ключ-tavily          # опционально
WEBUI_SECRET_KEY=любая-случайная-строка
POSTGRES_PASSWORD=сильный-пароль              # для production
```

**Шаг 3 — Запустить:**
```bash
docker compose up -d --build
```

Поднимаются 3 (или 4-5) контейнера: `postgres`, `monitor`, `open-webui` (+ опционально `jupyter`).

**Шаг 4 — Проверить:**
- OpenWebUI: http://localhost:3000 (создать admin при первом входе)
- Monitor: http://localhost:8088
- В Monitor → Connections должен быть **только** `http://monitor:8088/v1` (env подставит автоматически)

**Шаг 5 — установить Function** (см. § 5).

---

## 5. Установка Function в OpenWebUI

Файл — `monitor_function.py` в корне репозитория. Сейчас он **только** захватывает текст промта/ответа и `chat_id` — никаких токенов/cost. Точные деньги приходят через прокси.

### Через UI (5 кликов)
1. **Admin Panel → Functions → +**
2. Name: `OpenWebUI Monitor`, ID: `openwebui_monitor`
3. Вставить всё содержимое `monitor_function.py` → **Save**
4. Включить тумблер **Enabled**
5. ⋯ → **Global** (зелёная иконка глобуса)

### Через API
```bash
# 0. Токен
TOKEN=$(curl -s http://localhost:3000/api/v1/auths/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@x.x","password":"…"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

# 1. Загрузить
CONTENT=$(python3 -c "import json; print(json.dumps(open('monitor_function.py').read()))")
curl -s http://localhost:3000/api/v1/functions/create \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"id\":\"openwebui_monitor\",\"name\":\"OpenWebUI Monitor\",\"meta\":{\"description\":\"v2\",\"manifest\":{}},\"content\":$CONTENT}"

# 2. Включить + Global
curl -s -X POST http://localhost:3000/api/v1/functions/id/openwebui_monitor/toggle \
  -H "Authorization: Bearer $TOKEN"
curl -s -X POST http://localhost:3000/api/v1/functions/id/openwebui_monitor/toggle/global \
  -H "Authorization: Bearer $TOKEN"
```

### Valves
По умолчанию `MONITOR_URL=http://monitor:8088`. Менять не нужно, если оба контейнера в одной docker-сети.

### Проверка
1. Открыть чат в OpenWebUI → отправить «привет».
2. Через 40-60 сек (OpenRouter индексирует генерацию не сразу) проверить http://localhost:8088 → Request Log.
3. Должна появиться запись с user, model, **точным** cost и preview промта.

---

## 6. Дашборд

| Страница | Что показывает |
|---|---|
| **Dashboard** | Сводка: requests, tokens, cost, активные юзеры. Графики по дням за 30 дней. |
| **Request Log** | Логические запросы (один user turn = одна строка). При tool-calling рядом с моделью бейдж `4×` — клик откроет детали со всеми rounds и их провайдерами. |
| **By User** | Расход по email (берётся из Prompt). |
| **By Model** | Расход по `model` + `provider` (Azure / OpenAI / NovitaAI / AtlasCloud и т.д.). |

Формат цены: USD с центами. `$0.0349` для обычных, `$0.000005` для микро-вызовов.

---

## 7. PostgreSQL

Используется `postgres:16-alpine` из официального образа. Volume `postgres-data` сохраняет данные между рестартами.

Подключиться вручную:
```bash
docker compose exec postgres psql -U owmonitor -d owmonitor
\dt                       # список таблиц
SELECT count(*) FROM requests;
SELECT count(*) FROM prompts;
```

Таблицы:
- **requests** — один билинговый вызов OpenRouter (`generation_id` уникальный, `request_id` группирует tool-rounds)
- **prompts** — текст одного user-turn от Function (`chat_id` индексирован)
- **capture_queue** — резерв для durability (in-memory queue теряется при рестарте)

Бэкап:
```bash
docker compose exec postgres pg_dump -U owmonitor owmonitor > backup-$(date +%F).sql
```

Внешний PostgreSQL — поменять `DATABASE_URL` в `docker-compose.yml`:
```yaml
- DATABASE_URL=postgresql+psycopg2://user:pwd@external-host:5432/owmonitor
```
И убрать сервис `postgres` из compose.

---

## 8. FAQ / Диагностика

**В дашборде ничего не появляется после чата.**
1. Прокси ловит запросы? `docker compose logs monitor | grep captured` — должна быть строка `captured gen-…`
2. Sync синкается? `docker compose logs monitor | grep synced`
3. OpenRouter может индексировать generation **40-60 секунд**. Подождать.
4. Function активна? `Admin Panel → Functions` — две зелёные галки (Enabled, Global).

**Чат сломался после установки прокси.**
Это on-path архитектура — Monitor должен быть жив. Если упал:
```bash
docker compose ps     # все Up?
docker compose logs monitor --tail 50
```
**Fallback** — в OpenWebUI вернуть Connections на прямой OpenRouter: `Admin → Settings → Connections → URL: https://openrouter.ai/api/v1`. Чат заработает (без мониторинга).

**В Request Log запись есть, но `user=(unknown)` и нет prompt-preview.**
Function не связалась с этим request. Причины:
- Function не активна / не Global
- Запрос пришёл не из OpenWebUI (например, прямой `curl` на прокси)
- Image Studio — Function не вызывается, prompt-текста нет; только billing-данные

**Tool-calling показывает 1 round вместо нескольких.**
Запрос **без** tool-call'ов всегда даёт 1 round. Чтобы увидеть 4×, нужен запрос с веб-поиском Tavily на модели с `Function Calling: Native` (см. `openwebui_tavily.docx`).

**Как обновить Function после правок `monitor_function.py`?**
Через UI: Admin Panel → Functions → ✏ → заменить код → Save. Перезапуск контейнера **не нужен**.

**Как остановить мониторинг временно?**
Admin Panel → Functions → выключить тумблер Enabled у `OpenWebUI Monitor`. Прокси при этом продолжит работать (это другой канал) — биллинг будет идти, но без текстов промтов.

**Как полностью отключить прокси (откатить на v1-like поведение)?**
В `docker-compose.yml` поменять `OPENAI_API_BASE_URL=http://monitor:8088/v1` на `https://openrouter.ai/api/v1`, удалить Function. Контейнер `monitor` можно оставить — он не будет получать новых данных.

**Как заполнить старую БД историческими данными OpenRouter?**
Сейчас — никак (API `/api/v1/activity` отдаёт только агрегаты, не индивидуальные generation IDs). Это ограничение OpenRouter. Считаем «новая эпоха с миграции v2».
