# Интеграция Monitor в существующий compose

Эта инструкция — для **корпоративного** деплоя где у вас УЖЕ есть `docker-compose` с PostgreSQL, OpenWebUI и nginx, и нужно **добавить туда Monitor**.

Если вы разворачиваете всё с нуля — используйте `docker-compose.yml` + `docker-compose.prod.yml` из репо как есть (см. `MIGRATION_GUIDE.md`).

---

## Что должно быть на сервере перед началом

| | Должно быть | Команда проверки |
|---|---|---|
| 1 | Постгрес запущен и здоров | `docker compose ps postgres` |
| 2 | OpenWebUI запущен | `docker compose ps open-webui` |
| 3 | `OPENROUTER_API_KEY` в `.env` | `grep OPENROUTER_API_KEY .env` |
| 4 | Код Monitor в `./open-webui-monitor/` | `ls open-webui-monitor/monitor_function.py` |
| 5 | Общая docker-сеть | `docker network ls \| grep project-ai` |

Если в `.env` нет ключа OpenRouter — добавить строку:

```bash
echo "OPENROUTER_API_KEY=sk-or-v1-ваш-ключ" >> .env
```

---

## Шаг 1 — Создать отдельную БД в существующем PostgreSQL

Monitor хранит **свои** таблицы (`requests`, `prompts`, `capture_queue`) в отдельной БД, чтобы **не смешиваться** с таблицами OpenWebUI.

```bash
docker compose exec postgres psql -U ${POSTGRES_USER:-postgres} -c "CREATE DATABASE owmonitor;"
```

> Если выдаст `already exists` — пропустить, БД уже создана.

Проверка:

```bash
docker compose exec postgres psql -U ${POSTGRES_USER:-postgres} -l | grep owmonitor
```

---

## Шаг 2 — Добавить блок `owmonitor` в `compose.yaml`

Скопируйте этот блок **целиком** в свой `compose.yaml` (рядом с другими сервисами):

```yaml
  owmonitor:
    build:
      context: ./open-webui-monitor
    container_name: project-ai-owmonitor
    restart: unless-stopped
    env_file:
      - ./.env
    environment:
      # ВАЖНО: используется существующий postgres из этого же compose,
      # но БД отдельная (owmonitor) — чтобы не смешать таблицы с OpenWebUI
      DATABASE_URL: "postgresql+psycopg2://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:${POSTGRES_PORT:-5432}/owmonitor"
      PORT: 8088
      # OpenRouter key — тот же что и для OpenWebUI; используется sync-воркером
      # для запросов /api/v1/generation?id= к OpenRouter API
      OPENROUTER_API_KEY: "${OPENROUTER_API_KEY}"
      OPENROUTER_BASE: "https://openrouter.ai/api/v1"
      TZ: ${TZ:-Europe/Moscow}
    volumes:
      # model_pricing.json не обязателен — оставлен для совместимости,
      # цены в v2 берутся напрямую от OpenRouter
      - ./open-webui-monitor/model_pricing.json:/app/model_pricing.json:ro
    # Порт публикуется на хост для УДОБСТВА разработки.
    # В production — заменить на `expose: - "8088"` и проксировать через nginx
    ports:
      - "8081:8088"
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test:
        - "CMD"
        - "python"
        - "-c"
        - "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8088/api/health',timeout=3).status==200 else 1)"
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 20s
    # КРИТИЧНО: та же сеть что и у open-webui, иначе DNS-имя owmonitor
    # не разрешится из open-webui контейнера и прокси не заработает
    networks:
      - project-ai
```

> **Замените имя сети `project-ai`** на то что у вас в compose (`networks:` блок внизу файла). Если ваш OpenWebUI в другой сети — Monitor должен быть в той же.

---

## Шаг 3 — Изменить `OPENAI_API_BASE_URLS` у open-webui

Найдите в своём `compose.yaml` блок `open-webui:` → секция `environment` → переменную `OPENAI_API_BASE_URLS`. Сейчас там обычно:

```yaml
OPENAI_API_BASE_URLS: "https://api.openai.com/v1;http://pipelines:9099;https://openrouter.ai/api/v1"
OPENAI_API_KEYS: "${OPENAI_API_KEY};${PIPELINES_API_KEY};${OPENROUTER_API_KEY}"
```

**Замените** на:

```yaml
OPENAI_API_BASE_URLS: "https://api.openai.com/v1;http://owmonitor:8088/v1"
OPENAI_API_KEYS: "${OPENAI_API_KEY};${OPENROUTER_API_KEY}"
```

Что изменилось:
- `http://pipelines:9099` — **убран** (старый Pipelines больше не используется)
- `https://openrouter.ai/api/v1` — **заменён** на `http://owmonitor:8088/v1` (наш прокси)
- Прямой `api.openai.com` (если есть) **оставлен** — он не идёт через прокси и не отслеживается, но без него прямой OpenAI не работает

> **Можно ли убрать `https://openrouter.ai/api/v1` совсем?** Да, и нужно. Если оставить — пользователи в Connections могут случайно выбрать прямую модель из этого соединения, и она пройдёт мимо Monitor.

---

## Шаг 4 — Применить изменения

```bash
cd /srv/project-AI
docker compose up -d --force-recreate owmonitor open-webui
```

`--force-recreate` нужен чтобы новые env-переменные подхватились (без него `docker compose restart` не перечитает).

---

## Шаг 5 — Проверить что всё ок

### 5.1 Контейнер up и healthy

```bash
docker compose ps owmonitor
```

Ожидание: `Status: Up X seconds (healthy)`.

### 5.2 Env подхватился

```bash
docker compose exec owmonitor env | grep -E "DATABASE_URL|OPENROUTER_API_KEY|OPENROUTER_BASE"
```

Должно вернуть:
```
DATABASE_URL=postgresql+psycopg2://...
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_BASE=https://openrouter.ai/api/v1
```

> ❌ Если `DATABASE_URL=sqlite:///./data/monitor.db` — старый env закешировался, повторить `--force-recreate`.

### 5.3 Логи без ошибок

```bash
docker compose logs owmonitor --tail 30
```

Должно быть:
```
[monitor.main] INFO: starting openrouter_sync worker
[monitor.sync] INFO: openrouter_sync worker starting (concurrency=4)
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8088
```

**Не должно быть:**
- ❌ `sqlite3.OperationalError` (значит env не подхватился)
- ❌ `OPENROUTER_API_KEY not set` (значит ключа нет в .env)
- ❌ `could not connect to server: Connection refused` (значит postgres недоступен)

### 5.4 БД создана и таблицы есть

```bash
docker compose exec postgres psql -U ${POSTGRES_USER} -d owmonitor -c "\dt"
```

Должны быть таблицы: `requests`, `prompts`, `capture_queue`.

### 5.5 DNS-разрешение между контейнерами

```bash
docker compose exec open-webui sh -c "getent hosts owmonitor"
```

Должен вернуть IP типа `172.21.0.5  owmonitor`. Если пусто — open-webui и owmonitor в разных сетях, см. Шаг 2 (общая сеть).

### 5.6 Прокси отвечает изнутри open-webui

```bash
docker compose exec open-webui sh -c "wget -qO- http://owmonitor:8088/api/health"
```

Должно вернуть `{"ok":true}`. Если timeout или connection refused — проверьте сеть.

---

## Шаг 6 — Загрузить Function v2.1 в OpenWebUI

Файл уже в репо: `./open-webui-monitor/monitor_function.py`.

Через API (один раз):

```bash
# Получить токен админа
TOKEN=$(curl -s https://${WEBUI_URL#https://}/api/v1/auths/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"<admin@your-domain>","password":"<password>"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

# Подготовить тело запроса
CONTENT=$(python3 -c "import json; print(json.dumps(open('open-webui-monitor/monitor_function.py').read()))")

# Удалить старую если была
curl -s -X DELETE https://${WEBUI_URL#https://}/api/v1/functions/id/openwebui_monitor/delete \
  -H "Authorization: Bearer $TOKEN" 2>/dev/null || true

# Создать новую
curl -s https://${WEBUI_URL#https://}/api/v1/functions/create \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"id\":\"openwebui_monitor\",\"name\":\"OpenWebUI Monitor\",\"meta\":{\"description\":\"v2.1\",\"manifest\":{}},\"content\":$CONTENT}"

# Включить
curl -s -X POST https://${WEBUI_URL#https://}/api/v1/functions/id/openwebui_monitor/toggle \
  -H "Authorization: Bearer $TOKEN"

# Сделать глобальной
curl -s -X POST https://${WEBUI_URL#https://}/api/v1/functions/id/openwebui_monitor/toggle/global \
  -H "Authorization: Bearer $TOKEN"

# Проверка
curl -s https://${WEBUI_URL#https://}/api/v1/functions/id/openwebui_monitor \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('is_active:',d['is_active'],'| is_global:',d['is_global'])"
# Должно вернуть: is_active: True | is_global: True
```

Альтернатива через UI: Admin Panel → Functions → + → вставить код → Save → Enabled → ⋯ → Global.

---

## Шаг 7 — Финальный тест

1. Открыть OpenWebUI как пользователь
2. Новый чат на модели **из соединения `http://owmonitor:8088/v1`** (например `openai/gpt-4o-mini`)
3. Написать «привет»
4. Дождаться ответа
5. **Подождать ещё 60 секунд** (OpenRouter индексирует generation 30-60 сек)
6. Открыть `http://server:8081` (Monitor dashboard)
7. В **Журнале запросов** должна появиться строка с email пользователя, моделью и ценой `$0.0001x`

---

## Шаг 8 (опционально) — Удалить старый `pipelines`

Если вы не использовали `pipelines` ни для чего другого — можно убрать сервис целиком:

```bash
# В compose.yaml удалить весь блок pipelines:
docker compose rm -f -s pipelines
```

И удалить переменные `PIPELINES_*` из `.env`.

---

## Шаг 9 (production) — Закрыть прямой порт `:8081` и поставить за nginx с HTTPS + auth

В `compose.yaml` у `owmonitor:` заменить:

```yaml
    ports:
      - "8081:8088"
```

на:

```yaml
    expose:
      - "8088"
```

И добавить server-блок в `nginx/conf.d/monitor.conf`:

```nginx
server {
    listen 443 ssl http2;
    server_name monitor.your-domain;

    ssl_certificate     /etc/letsencrypt/live/monitor.your-domain/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/monitor.your-domain/privkey.pem;

    # Basic auth — простейшая защита
    auth_basic           "Monitor";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://project-ai-owmonitor:8088;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Создать htpasswd:

```bash
# на хосте, в директории где смонтирован nginx config
htpasswd -c /srv/project-AI/nginx/.htpasswd admin
```

Получить TLS-сертификат для `monitor.your-domain` через certbot (уже есть в стеке) и применить:

```bash
docker compose up -d nginx
```

После этого `http://server:8081` будет недоступен (порт закрыт), а `https://monitor.your-domain` — открыт с логином.

---

## Частые ошибки

### «Запросы не появляются в дашборде»

| Проверка | Команда | Что ожидать |
|---|---|---|
| Прокси ловит? | `docker compose logs owmonitor \| grep captured` | `captured gen-...` после каждого чата |
| Sync синкается? | `docker compose logs owmonitor \| grep synced` | `synced gen-...: cost=$0.0001x` через 30-60 сек после capture |
| Function активна? | через API (см. шаг 6 проверка) | `is_active: True \| is_global: True` |
| OpenWebUI шлёт в Monitor? | `docker compose logs owmonitor \| grep POST` | строки `POST /v1/chat/completions ... 200` |

### «Чат сломался после миграции»

Откатить URL соединения через UI:

Admin Panel → Settings → Connections → найти `http://owmonitor:8088/v1` → удалить → добавить обратно `https://openrouter.ai/api/v1` с тем же ключом → Save.

Чат заработает напрямую, мониторинг временно отключится. Можно разбираться без давления.

### «sqlite3.OperationalError» в логах

env-переменная `DATABASE_URL` не подхватилась — старый контейнер. Решение:

```bash
docker compose stop owmonitor
docker compose rm -f owmonitor
docker compose up -d owmonitor
```

(`restart` и `up -d` без `--force-recreate` могут оставить старые env.)
