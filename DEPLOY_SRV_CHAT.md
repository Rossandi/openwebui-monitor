# Деплой OpenWebUI Monitor v2 на SRV-CHAT

Пошаговый чек-лист для конкретного сервера `chat.iitservices.ru` с уже существующим стеком `/srv/project-AI/`.

**Текущее состояние:** Monitor уже стоит, но в неправильной конфигурации (SQLite + без OpenRouter ключа в env). По этой инструкции перенастроим его на работающий вариант.

**Время:** ~30 минут от первого SSH до работающего мониторинга.

---

## 🔑 Перед началом

Подготовь:
- SSH-доступ к серверу
- **OpenRouter API ключ** с балансом — должен уже быть в `.env` под именем `OPENROUTER_API_KEY`
- **Email и пароль администратора** твоего OpenWebUI (для загрузки Function через API)

---

## Шаг 0 — Подключиться и сделать резервную копию compose

```bash
ssh user@chat.iitservices.ru
cd /srv/project-AI

cp compose.yaml compose.yaml.backup-$(date +%F)
ls -la compose.yaml*
```

---

## Шаг 1 — Обновить код Monitor

В `/srv/project-AI/open-webui-monitor/` лежит старая версия. Скачай актуальную:

```bash
cd /srv/project-AI

# Backup старого кода (на случай отката)
mv open-webui-monitor open-webui-monitor.old-$(date +%F)

# Клонировать актуальную версию из публичного репо
git clone https://github.com/Rossandi/openwebui-monitor.git open-webui-monitor

# Проверить что код последний
cd open-webui-monitor
git log --oneline | head -3
# Ожидаем:
#   392b27f  Fix /api/logs date filter ...
#   e1eaf95  Custom date range ...
#   287225b  Add INTEGRATE_INTO_EXISTING_STACK.md ...

cd ..
```

---

## Шаг 2 — Создать отдельную БД в существующем Postgres

```bash
# Узнай имя пользователя из .env
grep POSTGRES_USER /srv/project-AI/.env

# Создай БД (подставь значение $POSTGRES_USER)
docker compose exec postgres psql -U <POSTGRES_USER> -c "CREATE DATABASE owmonitor;"
# Если выдаст "already exists" — пропусти, БД уже создана
```

---

## Шаг 3 — Проверить `OPENROUTER_API_KEY` в `.env`

```bash
grep OPENROUTER_API_KEY /srv/project-AI/.env
```

Должно вернуть `OPENROUTER_API_KEY=sk-or-v1-...`. Если **пусто** — добавь:

```bash
echo "OPENROUTER_API_KEY=sk-or-v1-ваш_ключ" >> /srv/project-AI/.env
```

---

## Шаг 4 — Заменить блок `owmonitor:` в compose.yaml

```bash
nano /srv/project-AI/compose.yaml
```

Найди текущий блок `owmonitor:` (~внизу, до `volumes:`). **Удали его целиком** и вставь это:

```yaml
  owmonitor:
    build:
      context: ./open-webui-monitor
    container_name: project-ai-owmonitor
    restart: unless-stopped
    env_file:
      - ./.env
    environment:
      DATABASE_URL: "postgresql+psycopg2://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:${POSTGRES_PORT:-5432}/owmonitor"
      PORT: 8088
      OPENROUTER_API_KEY: "${OPENROUTER_API_KEY}"
      OPENROUTER_BASE: "https://openrouter.ai/api/v1"
      TZ: ${TZ:-Europe/Moscow}
    volumes:
      - ./open-webui-monitor/model_pricing.json:/app/model_pricing.json:ro
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
    networks:
      - project-ai
```

**Что критично vs текущий блок:**
- `DATABASE_URL` теперь PostgreSQL (был SQLite)
- `OPENROUTER_API_KEY` добавлен в environment
- Mount `./data:/app/data` убран (SQLite больше не используется)
- Добавлен healthcheck

---

## Шаг 5 — Поправить `OPENAI_API_BASE_URLS` у open-webui

В том же файле в блоке `open-webui:` → `environment:` найди:

```yaml
OPENAI_API_BASE_URLS: "https://api.openai.com/v1;http://pipelines:9099;https://openrouter.ai/api/v1"
OPENAI_API_KEYS: "${OPENAI_API_KEY};${PIPELINES_API_KEY};${OPENROUTER_API_KEY}"
```

**Замени** на:

```yaml
OPENAI_API_BASE_URLS: "https://api.openai.com/v1;http://owmonitor:8088/v1"
OPENAI_API_KEYS: "${OPENAI_API_KEY};${OPENROUTER_API_KEY}"
```

Изменения:
- `http://pipelines:9099` **убран** (старый Pipelines не нужен в v2)
- `https://openrouter.ai/api/v1` **заменён** на `http://owmonitor:8088/v1` (через прокси Monitor)
- Соответственно `${PIPELINES_API_KEY}` убран

Сохрани: `Ctrl+O`, `Enter`, `Ctrl+X`.

---

## Шаг 6 — Применить изменения

```bash
cd /srv/project-AI

# Пересобрать образ Monitor с новым кодом
docker compose build --no-cache owmonitor

# Пересоздать контейнеры с новым env (НЕ restart!)
docker compose up -d --force-recreate owmonitor open-webui

# Подождать
sleep 15
docker compose ps
```

Ожидание: `owmonitor` со статусом `Up X seconds (healthy)`.

---

## Шаг 7 — Шесть проверок что всё ок

### 7.1 Env подхватился (главное!)

```bash
docker compose exec owmonitor env | grep -E "DATABASE_URL|OPENROUTER_API_KEY"
```

Должно быть **две** строки:
```
DATABASE_URL=postgresql+psycopg2://...    ← НЕ sqlite!
OPENROUTER_API_KEY=sk-or-v1-...           ← НЕ пусто!
```

> Если `DATABASE_URL=sqlite:...` — старый env закешировался. `docker compose stop owmonitor && docker compose rm -f owmonitor && docker compose up -d owmonitor`.

### 7.2 Логи без ошибок

```bash
docker compose logs owmonitor --tail 30
```

Должно быть:
```
[monitor.main] INFO: starting openrouter_sync worker
[monitor.sync] INFO: openrouter_sync worker starting (concurrency=4)
INFO:     Uvicorn running on http://0.0.0.0:8088
```

**Не должно быть:**
- ❌ `sqlite3.OperationalError`
- ❌ `OPENROUTER_API_KEY not set`

### 7.3 БД создана и таблицы есть

```bash
docker compose exec postgres psql -U <POSTGRES_USER> -d owmonitor -c "\dt"
```

Должны быть таблицы: `requests`, `prompts`, `capture_queue`.

### 7.4 DNS между контейнерами

```bash
docker compose exec open-webui sh -c "getent hosts owmonitor"
```

Должен вернуть IP типа `172.21.0.5  owmonitor`.

### 7.5 open-webui видит monitor

```bash
docker compose exec open-webui sh -c "wget -qO- http://owmonitor:8088/api/health"
```

Должно вернуть `{"ok":true}`.

### 7.6 Дашборд доступен

```bash
curl http://localhost:8081/api/stats/summary
```

Или открой в браузере: **http://chat.iitservices.ru:8081**

---

## Шаг 8 — Загрузить Function v2.1 в OpenWebUI

Через API:

```bash
cd /srv/project-AI/open-webui-monitor

# Подставь свои email и пароль
ADMIN_EMAIL="admin@iitservices.ru"
ADMIN_PASSWORD="ваш_пароль"

TOKEN=$(curl -s https://chat.iitservices.ru/api/v1/auths/signin \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

echo "Token: ${TOKEN:0:20}..."

# Снести старую если была
curl -s -X DELETE https://chat.iitservices.ru/api/v1/functions/id/openwebui_monitor/delete \
  -H "Authorization: Bearer $TOKEN" 2>/dev/null || true

# Создать v2.1
CONTENT=$(python3 -c "import json; print(json.dumps(open('monitor_function.py').read()))")
curl -s https://chat.iitservices.ru/api/v1/functions/create \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"id\":\"openwebui_monitor\",\"name\":\"OpenWebUI Monitor\",\"meta\":{\"description\":\"v2.1\",\"manifest\":{}},\"content\":$CONTENT}"

# Включить + Global
curl -s -X POST https://chat.iitservices.ru/api/v1/functions/id/openwebui_monitor/toggle \
  -H "Authorization: Bearer $TOKEN"
curl -s -X POST https://chat.iitservices.ru/api/v1/functions/id/openwebui_monitor/toggle/global \
  -H "Authorization: Bearer $TOKEN"

# Проверка
curl -s https://chat.iitservices.ru/api/v1/functions/id/openwebui_monitor \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('is_active:',d['is_active'],'| is_global:',d['is_global'])"
```

Ожидаем: `is_active: True | is_global: True`.

**Альтернатива UI:** Admin Panel → Functions → + → вставить `monitor_function.py` → Save → Enabled → ⋯ → Global.

---

## Шаг 9 — Удалить старый Pipeline Filter (если был)

Admin Panel → Functions. Если есть `pipeline_filter` или другая filter-функция от v1 — удалить.

---

## Шаг 10 — Финальный тест

1. Открыть `https://chat.iitservices.ru` как обычный пользователь
2. Новый чат
3. Выбрать модель **из соединения через прокси** (например `openai/gpt-4o-mini`)
4. Написать «привет»
5. Дождаться ответа
6. **Подождать ещё 60 секунд** (OpenRouter индексирует не мгновенно)
7. Открыть `http://chat.iitservices.ru:8081`
8. В **Журнале запросов** должна появиться строка с твоим email и точной ценой типа `$0.00018`

✅ Появилась — мониторинг работает.

---

## 🆘 Plan B — откат за 2 минуты

```bash
cd /srv/project-AI

# Восстановить старый compose
cp compose.yaml.backup-* compose.yaml
docker compose up -d --force-recreate open-webui

# Или через UI: Admin Panel → Settings → Connections
# Вернуть URL https://openrouter.ai/api/v1 (прямой)
```

Чат заработает напрямую, мониторинг временно отключится. Можно разбираться спокойно.

---

## 🔐 После того как всё работает — security

Сейчас дашборд открыт миру по `http://chat.iitservices.ru:8081` **без пароля**. Это серьёзный риск — там видны все промты пользователей.

**Этапы:**

1. Получить TLS-сертификат на `monitor.iitservices.ru` через ваш certbot
2. Добавить server-блок в nginx (`/srv/project-AI/nginx/conf.d/monitor.conf`):

```nginx
server {
    listen 443 ssl http2;
    server_name monitor.iitservices.ru;

    ssl_certificate     /etc/letsencrypt/live/monitor.iitservices.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/monitor.iitservices.ru/privkey.pem;

    auth_basic           "Monitor";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://project-ai-owmonitor:8088;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

3. Создать htpasswd:
```bash
htpasswd -c /srv/project-AI/nginx/.htpasswd admin
```

4. В `compose.yaml` у `owmonitor:` заменить `ports: - "8081:8088"` на `expose: - "8088"`

5. `docker compose up -d nginx owmonitor`

Делать **только после** того как мониторинг точно работает.

---

## ❓ Частые проблемы

| Симптом | Команда диагностики / Решение |
|---|---|
| Запросы не в дашборде | `docker compose logs owmonitor \| grep -E "captured\|synced"` — должны быть оба |
| `OPENROUTER_API_KEY not set` | Проверь `.env` + `docker compose up -d --force-recreate owmonitor` |
| `sqlite3.OperationalError` | `docker compose stop owmonitor && rm -f owmonitor && up -d owmonitor` |
| `getent hosts owmonitor` пусто | Проверь что в compose у `owmonitor:` есть `networks: - project-ai` |
| Чат отдаёт 502 | UI → Connections → проверь URL соединения = `http://owmonitor:8088/v1` |

Если застрял — собери `docker compose logs owmonitor --tail 50` + `docker compose ps` и обращайся.

---

**Удачи!** Не делай больше одной правки за раз и проверяй после каждой — будет проще найти если что-то пойдёт не так.
