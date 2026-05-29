# Production migration: OpenWebUI Monitor v1 → v2

**Аудитория:** сисадмин, у которого уже работает корпоративный OpenWebUI + Monitor v1 (Pipelines-based, SQLite).
**Цель:** перейти на v2 (proxy + PostgreSQL + точные данные от OpenRouter) **без простоя пользователей**.
**Время:** ~40 минут активной работы + 1 час наблюдения.

---

## 0. Pre-flight — обязательно перед началом

```bash
# 0.1. Определить рабочую директорию prod-стека (далее $PROD_DIR)
cd /opt/openwebui-monitor   # или где у вас лежит docker-compose.yml v1

# 0.2. Резерв volume открытого OpenWebUI (пользователи, чаты, настройки)
docker run --rm \
  -v openwebuimonitorv2docker_open-webui-data:/data \
  -v "$(pwd)":/backup \
  alpine tar -czf /backup/owui-volume-pre-migration-$(date +%F).tar.gz -C /data .

# 0.3. Резерв старой SQLite БД монитора v1 (если была)
[ -f ./data/monitor.db ] && cp ./data/monitor.db ./data/monitor.db.v1-backup

# 0.4. Снять текущую версию OpenWebUI — должна быть >= 0.4.0
docker exec open-webui sh -c "cat /app/package.json | grep version"
# Если < 0.4.0 — сначала: docker compose pull && docker compose up -d
```

**Контрольная точка:** должны быть три файла:
- `owui-volume-pre-migration-YYYY-MM-DD.tar.gz` (~50 МБ–1 ГБ)
- `data/monitor.db.v1-backup` (если v1 БД была)
- Версия OpenWebUI ≥ 0.4.0

---

## 1. Развернуть v2 артефакты

Распаковать архив v2 в **новую** директорию (НЕ переписывать поверх старого):

```bash
mkdir -p /opt/owmonitor-v2 && cd /opt/owmonitor-v2
tar -xzf /tmp/openwebui-monitor-v2.tar.gz
ls -la
# должно быть: docker-compose.yml docker-compose.prod.yml Caddyfile
#              monitor/ monitor_function.py requirements.txt Dockerfile
#              .env.example MIGRATION_GUIDE.md backup.sh README.md
```

Создать `.env`:

```bash
cp .env.example .env
nano .env
```

Заполнить **обязательно**:

| Переменная | Что вписать |
|---|---|
| `OPENAI_API_KEY` | Ваш OpenRouter ключ (`sk-or-v1-...`) — тот же, что в v1 |
| `WEBUI_SECRET_KEY` | Случайная строка ≥ 32 байт (`openssl rand -hex 32`) |
| `POSTGRES_PASSWORD` | Сильный пароль (`openssl rand -base64 24`) |
| `TAVILY_API_KEY` | Если есть веб-поиск |
| `JUPYTER_TOKEN` | Только если используется Code Interpreter |

В `Caddyfile` — заменить `chat.your-company.ru` и `monitor.your-company.ru` на ваши DNS-имена, `admin@your-company.ru` на реальный email.

---

## 2. Сборка образов БЕЗ запуска

Чтобы downtime был минимальным, сначала собираем всё, потом одним движением переключаем:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
# image monitor:latest готов, ничего не запущено
```

---

## 3. Переключение (downtime ~2 минуты)

### 3.1 — Поднять Postgres + Monitor + Caddy

```bash
cd /opt/owmonitor-v2
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d postgres monitor caddy
sleep 10
docker compose ps     # postgres healthy, monitor up, caddy up
curl -k https://monitor.your-company.ru/api/stats/summary  # JSON с нулями
```

### 3.2 — Перенос volume `open-webui-data`

Если v2 в той же compose-сети — volume будет с тем же именем (`openwebuimonitorv2docker_open-webui-data`). Если в **другом** репо — переименовать или перенести:

```bash
# Вариант А: один и тот же docker-compose project name → volume переиспользуется автоматически
# Вариант Б: разные project → импорт:
OLD_VOL=$(docker volume ls -q | grep open-webui-data | head -1)
NEW_VOL_NAME=owmonitor-v2_open-webui-data
docker volume create "${NEW_VOL_NAME}"
docker run --rm \
  -v "${OLD_VOL}":/src \
  -v "${NEW_VOL_NAME}":/dst \
  alpine sh -c "cp -a /src/. /dst/"
# далее в v2 docker-compose.yml volumes указать external: true с именем выше.
```

### 3.3 — Остановить v1 OpenWebUI и поднять v2

```bash
# Стоп v1 (только open-webui, чтобы освободить порт/имя)
cd /opt/openwebui-monitor    # путь к v1
docker compose stop open-webui pipelines

# Старт v2 open-webui
cd /opt/owmonitor-v2
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d open-webui
docker compose logs -f open-webui --tail 50   # дождаться "Application startup complete"
```

С этого момента чат снова работает, но идёт через Monitor-прокси.

### 3.4 — Сменить URL соединения в OpenWebUI

OpenWebUI хранит base URL в БД (volume), а не только в env. Принудительно обновить:

```bash
# Получить токен админа
TOKEN=$(curl -s https://chat.your-company.ru/api/v1/auths/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@your-company.ru","password":"YOUR_ADMIN_PASSWORD"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

# Заменить connection
curl -s https://chat.your-company.ru/openai/config/update -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "ENABLE_OPENAI_API": true,
    "OPENAI_API_BASE_URLS": ["http://monitor:8088/v1"],
    "OPENAI_API_KEYS": ["'"${OPENAI_API_KEY}"'"],
    "OPENAI_API_CONFIGS": {
      "0": {"enable": true, "tags": [], "prefix_id": "", "model_ids": [],
            "connection_type": "external", "auth_type": "bearer"}
    }
  }'
```

### 3.5 — Удалить старый pipeline_filter (если был)

Через UI: **Admin Panel → Functions** или **Settings → Pipelines** — найти `pipeline_filter`, удалить.

Через API:
```bash
curl -s -X DELETE https://chat.your-company.ru/api/v1/functions/id/pipeline_filter/delete \
  -H "Authorization: Bearer $TOKEN" 2>/dev/null || true
```

### 3.6 — Установить Function v2

```bash
CONTENT=$(python3 -c "import json; print(json.dumps(open('monitor_function.py').read()))")

# Снести старую (если ID совпадает)
curl -s -X DELETE https://chat.your-company.ru/api/v1/functions/id/openwebui_monitor/delete \
  -H "Authorization: Bearer $TOKEN" 2>/dev/null || true

# Создать v2.1
curl -s https://chat.your-company.ru/api/v1/functions/create \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"id\":\"openwebui_monitor\",\"name\":\"OpenWebUI Monitor\",\"meta\":{\"description\":\"v2.1\",\"manifest\":{}},\"content\":$CONTENT}"

# Включить + Global
curl -s -X POST https://chat.your-company.ru/api/v1/functions/id/openwebui_monitor/toggle \
  -H "Authorization: Bearer $TOKEN"
curl -s -X POST https://chat.your-company.ru/api/v1/functions/id/openwebui_monitor/toggle/global \
  -H "Authorization: Bearer $TOKEN"

# Verify
curl -s https://chat.your-company.ru/api/v1/functions/id/openwebui_monitor \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('active:',d['is_active'],'global:',d['is_global'])"
# active: True global: True
```

---

## 4. Проверка работы

```bash
# 4.1 Сделать любой чат в OpenWebUI как реальный пользователь
#     Открыть https://chat.your-company.ru, отправить «привет»

# 4.2 Через ~60 сек посмотреть капчеры в логах
docker compose logs monitor --tail 50 | grep -E "captured|synced"

# 4.3 Проверить через API монитора
curl -s https://monitor.your-company.ru/api/stats/summary?period=today
# {"total_requests":1, ...}

curl -s 'https://monitor.your-company.ru/api/logs?limit=5' | head -c 500
# должна быть запись с user_email, model, cost
```

**Открыть https://monitor.your-company.ru в браузере** — dashboard, Request Log с бейджем `4×` для запросов с веб-поиском.

---

## 5. Снять старый стек

После 1-2 часов наблюдения, если жалоб нет:

```bash
cd /opt/openwebui-monitor
docker compose down                              # стоп v1, volumes сохранены
# Через неделю — если v2 без проблем:
docker compose down -v                           # удалить volumes v1 (БД монитора v1)
```

Каталог `/opt/openwebui-monitor` можно архивировать и убрать. **Volume `open-webui-data` ОДИН для обеих версий** — НЕ удалять.

---

## 6. Бэкапы

```bash
chmod +x ./backup.sh
./backup.sh   # ручной запуск
ls -la backups/
# owmonitor-2026-05-28.sql.gz

# Cron — каждый день в 03:30
crontab -e
# Добавить строку:
30 3 * * * cd /opt/owmonitor-v2 && ./backup.sh >> /var/log/owmonitor-backup.log 2>&1
```

---

## 7. Plan B — откат за 2 минуты

Если после переключения что-то пошло не так:

```bash
# 7.1 Снять v2 open-webui (Monitor можно оставить — он не вреден сам по себе)
cd /opt/owmonitor-v2
docker compose stop open-webui

# 7.2 Через UI или API вернуть URL соединения на прямой OpenRouter
TOKEN=$(curl -s https://chat.your-company.ru/api/v1/auths/signin -H "Content-Type: application/json" \
  -d '{"email":"admin@...","password":"..."}' | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
curl -s https://chat.your-company.ru/openai/config/update -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"OPENAI_API_BASE_URLS":["https://openrouter.ai/api/v1"], ...}'

# 7.3 Поднять старый v1 open-webui
cd /opt/openwebui-monitor
docker compose up -d open-webui
```

Чат работает напрямую с OpenRouter, мониторинг временно отключён. Можно разбираться без давления.

**Если совсем плохо** — restore volume из бэкапа:

```bash
docker compose down
docker volume rm openwebuimonitorv2docker_open-webui-data
docker volume create openwebuimonitorv2docker_open-webui-data
docker run --rm \
  -v openwebuimonitorv2docker_open-webui-data:/data \
  -v "$(pwd)":/backup \
  alpine tar -xzf /backup/owui-volume-pre-migration-YYYY-MM-DD.tar.gz -C /data
docker compose up -d
```

---

## 8. Восстановление PostgreSQL из бэкапа

Если БД монитора повреждена:

```bash
docker compose stop monitor
zcat backups/owmonitor-2026-05-28.sql.gz | \
  docker compose exec -T postgres psql -U owmonitor -d owmonitor
docker compose start monitor
```

---

## 9. Чек-лист готовности к продакшену

- [ ] Бэкап volume `open-webui-data` сделан
- [ ] `.env` заполнен с реальными секретами (не `owmonitor`, не `change-me`)
- [ ] DNS A-записи указывают на этот сервер для двух доменов
- [ ] Firewall открыт: 80, 443. Закрыт: 3000, 8088, 5432
- [ ] Caddyfile с реальными именами хостов
- [ ] Stack поднят `up -d` через **оба** compose-файла
- [ ] HTTPS-сертификаты выпущены (`docker compose logs caddy | grep -i certificate`)
- [ ] Function v2.1 загружена, is_active+is_global = True
- [ ] Тестовый чат прошёл, в дашборде появилась запись
- [ ] cron на `backup.sh` настроен
- [ ] Старый v1 стек остановлен (но не удалён ещё ~неделю)
