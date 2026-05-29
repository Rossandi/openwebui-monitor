# Установка OpenWebUI Monitor — инструкция для сисадмина

Документ описывает, как подключить мониторинг запросов пользователей к **уже существующему** корпоративному OpenWebUI.

---

## Что в итоге получится

```
[Пользователи] → [Корпоративный OpenWebUI] ──→ [LLM API (OpenRouter / OpenAI / прочее)]
                          │
                          │  Function "OpenWebUI Monitor" (filter, global)
                          │  перехватывает inlet (промт) и outlet (ответ + токены)
                          ▼
                    POST /api/ingest
                          ▼
                    [Monitor — новый контейнер]
                          │
                          ▼
                    SQLite (или PostgreSQL)
                          │
                          ▼
                    Дашборд http://<хост>:8088
```

**Важные свойства:**

1. **Никаких отдельных Pipelines-серверов не нужно.** Раньше для перехвата использовался отдельный Docker-контейнер `pipelines`. Этот вариант **больше не используется** — он ломался при подключении OpenRouter. Сейчас всё работает через нативный механизм OpenWebUI «Functions».
2. **Function ставится ОДИН раз через Admin Panel** или через API. Дальше она применяется ко всем моделям и всем соединениям автоматически.
3. **Function не может сломать чат.** Все исключения внутри неё проглатываются — если Monitor упал, пользователи это не заметят.

**Минимальные требования к OpenWebUI:** версия `0.4.0` или новее. Проверить:
```bash
docker exec <имя-контейнера-openwebui> sh -c "cat /app/package.json | grep version"
```

---

## Шаг 1 — Развернуть контейнер Monitor

Monitor — это отдельный сервис. Где его развернуть — зависит от того, как у вас уже задеплоен OpenWebUI.

### Вариант 1A — OpenWebUI запущен через docker-compose

Это самый чистый вариант. В `docker-compose.yml` добавьте новый сервис рядом с `open-webui`:

```yaml
  monitor:
    build:
      context: ./openwebui-monitor       # путь к репозиторию проекта Monitor
    container_name: monitor
    ports:
      - "8088:8088"
    environment:
      - DATABASE_URL=sqlite:///./data/monitor.db
      - PORT=8088
    volumes:
      - ./openwebui-monitor/data:/app/data
      - ./openwebui-monitor/model_pricing.json:/app/model_pricing.json:ro
    networks:
      - <та же сеть, что у open-webui>
    restart: unless-stopped
```

Где взять файлы `Dockerfile`, `monitor/`, `model_pricing.json`, `requirements.txt`, `monitor_function.py` — забрать из репозитория проекта (тот, который вам передали вместе с этой инструкцией).

После этого:
```bash
docker compose up -d --build monitor
```

Проверить:
```bash
docker compose logs monitor --tail 20
# должно быть: "Application startup complete" и "Uvicorn running on http://0.0.0.0:8088"
curl http://localhost:8088/api/stats/summary
# должно вернуть JSON с нулями
```

### Вариант 1B — OpenWebUI запущен отдельным `docker run`

Создайте Docker-сеть (если ещё нет) и подключите к ней оба контейнера:

```bash
# 1. Создать сеть
docker network create owmonitor-net

# 2. Подключить существующий OpenWebUI к этой сети
docker network connect owmonitor-net open-webui

# 3. Собрать образ Monitor из репозитория
cd /path/to/openwebui-monitor
docker build -t owmonitor:latest .

# 4. Запустить Monitor в той же сети
docker run -d --name monitor \
  --network owmonitor-net \
  -p 8088:8088 \
  -e DATABASE_URL=sqlite:///./data/monitor.db \
  -e PORT=8088 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/model_pricing.json:/app/model_pricing.json:ro \
  --restart unless-stopped \
  owmonitor:latest
```

### Вариант 1C — OpenWebUI запущен в Kubernetes / на отдельной VM

Поднимаете Monitor на любой машине, до которой OpenWebUI может достучаться по HTTP. Достаточно открыть **только порт 8088** для входящих от хоста с OpenWebUI. Ниже в Шаге 4 будете указывать `MONITOR_URL` — это полный URL, например `http://monitor.internal:8088` или `http://10.0.5.42:8088`.

### Что выбрать для MONITOR_URL

| Где Monitor | Какой URL ставить в Function |
|---|---|
| Тот же docker-compose, что и OpenWebUI | `http://monitor:8088` |
| Отдельный `docker run` в общей docker-сети | `http://monitor:8088` |
| Отдельная VM / отдельный сервер | `http://<host-or-ip>:8088` |
| Kubernetes service | `http://monitor.<namespace>.svc.cluster.local:8088` |

**Нельзя** использовать `http://localhost:8088` внутри Function — Function выполняется внутри контейнера OpenWebUI, и `localhost` там указывает на сам OpenWebUI, а не на Monitor.

---

## Шаг 2 — Удалить старый Pipelines-сервер (если он у вас был)

Если ранее уже использовался pipeline_filter через отдельный сервер `ghcr.io/open-webui/pipelines`, его нужно убрать — иначе будет двойной перехват.

### 2.1 Удалить контейнер
В `docker-compose.yml` удалить весь блок:
```yaml
  pipelines:
    image: ghcr.io/open-webui/pipelines:main
    ...
```
И из блока `open-webui` удалить:
- строку `- PIPELINES_URLS=http://pipelines:9099` из `environment`
- `pipelines` из `depends_on`

Применить:
```bash
docker compose up -d --remove-orphans
```

### 2.2 Удалить соединение Pipelines из настроек OpenWebUI

Через UI: **Admin Panel → Settings → Connections** — найти запись `http://pipelines:9099` (auth key `0p3n-w3bu!`), нажать корзину, **Save**.

Через API (если UI недоступен):
```bash
TOKEN=$(curl -s http://localhost:3000/api/v1/auths/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"admin-password"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

# Получить текущую конфигурацию
curl -s http://localhost:3000/openai/config \
  -H "Authorization: Bearer $TOKEN"

# Перезаписать список соединений — оставить только нужные
# (пример: оставляем только OpenRouter в индексе 0)
curl -s http://localhost:3000/openai/config/update -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "ENABLE_OPENAI_API": true,
    "OPENAI_API_BASE_URLS": ["https://openrouter.ai/api/v1"],
    "OPENAI_API_KEYS": ["sk-or-v1-xxxxxxxxxxx"],
    "OPENAI_API_CONFIGS": {
      "0": {"enable":true,"tags":[],"prefix_id":"","model_ids":[],"connection_type":"external","auth_type":"bearer"}
    }
  }'
```

### 2.3 Удалить старый Pipeline Filter из OpenWebUI

Если в **Admin Panel → Settings → Pipelines** в списке pipeline-фильтров есть `pipeline_filter` (со старого Pipelines-сервера) — он автоматически отвалится после удаления соединения. Дополнительных действий не нужно.

Если же кто-то загружал его как Function (а не через Pipelines-сервер) — Admin Panel → Functions → найти его → удалить.

---

## Шаг 3 — Загрузить Function в OpenWebUI

Файл `monitor_function.py` должен лежать в вашем репозитории Monitor.

### Вариант 3A — через UI (5 кликов, удобно для первого раза)

1. Залогиниться в OpenWebUI как **администратор**.
2. Кликнуть по своей аватарке в нижнем левом углу → **Admin Panel**.
3. В Admin Panel — вкладка **Functions** (в верхнем меню).
4. Кнопка **+** (Add new function) в правом верхнем углу.
5. Заполнить поля:
   - **Function Name:** `OpenWebUI Monitor`
   - **Function ID:** `openwebui_monitor` (без пробелов и кириллицы — это идентификатор для API)
   - **Function Description:** `Captures inlet/outlet of all chats and forwards to Monitor`
6. В большое поле кода **полностью** скопировать содержимое файла `monitor_function.py` (открыть файл в любом редакторе, выделить всё, скопировать, вставить).
7. Нажать **Save**.
8. После сохранения функция появится в списке. Включить **тумблер Enabled** напротив неё.
9. Кликнуть на ⋯ (три точки) рядом с функцией → выбрать **Global**. Появится зелёная глобус-иконка — это означает, что Function применяется ко **всем** моделям.
10. (Опционально) Кликнуть ⋯ → **Valves** — проверить, что `MONITOR_URL` правильный (см. таблицу из Шага 1). Если нет — поправить и Save.

### Вариант 3B — через API (для автоматизации / ansible / CI)

```bash
# 0. Получить токен админа
TOKEN=$(curl -s http://localhost:3000/api/v1/auths/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"admin-password"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
echo "Token: ${TOKEN:0:20}..."

# 1. Подготовить JSON-payload (читаем код функции и экранируем)
CONTENT=$(python3 -c "import json; print(json.dumps(open('monitor_function.py').read()))")

# 2. Создать Function
curl -s http://localhost:3000/api/v1/functions/create \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"id\":\"openwebui_monitor\",\"name\":\"OpenWebUI Monitor\",\"meta\":{\"description\":\"Monitor filter\",\"manifest\":{}},\"content\":$CONTENT}"

# 3. Включить
curl -s -X POST http://localhost:3000/api/v1/functions/id/openwebui_monitor/toggle \
  -H "Authorization: Bearer $TOKEN"

# 4. Сделать глобальной
curl -s -X POST http://localhost:3000/api/v1/functions/id/openwebui_monitor/toggle/global \
  -H "Authorization: Bearer $TOKEN"

# 5. Установить Valves (правильный MONITOR_URL!)
curl -s -X POST http://localhost:3000/api/v1/functions/id/openwebui_monitor/valves/update \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"MONITOR_URL":"http://monitor:8088","priority":0}'

# 6. Проверить состояние
curl -s http://localhost:3000/api/v1/functions/id/openwebui_monitor \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('is_active:', d.get('is_active'), '| is_global:', d.get('is_global'))"
# Должно быть: is_active: True | is_global: True
```

---

## Шаг 4 — Тестирование

1. Открыть OpenWebUI как обычный пользователь (или admin).
2. Создать новый чат на любой модели.
3. Написать что-нибудь, например `привет`.
4. Дождаться ответа модели.
5. Открыть Monitor: `http://<monitor-host>:8088`.
6. На странице **Request Log** должна появиться свежая запись:
   - timestamp = время запроса
   - user_email = email того, кто писал
   - model = ID модели
   - input_tokens, output_tokens, cost_usd — заполнены

Если запись **не появилась** — см. раздел «Диагностика» ниже.

---

## Шаг 5 — Настройка цен на токены

Цены лежат в `model_pricing.json` рядом с `monitor_function.py`. Формат — `$ за 1000 токенов`:

```json
{
  "openai/gpt-4o":              {"input": 0.0025,  "output": 0.010},
  "openai/gpt-4o-mini":         {"input": 0.00015, "output": 0.0006},
  "anthropic/claude-3.5-sonnet":{"input": 0.003,   "output": 0.015},
  "google/gemini-2.5-pro":      {"input": 0.00125, "output": 0.005},
  "default":                    {"input": 0.001,   "output": 0.002}
}
```

Совпадение **подстрочное**: `openai/gpt-4o-mini-2024-07-18` совпадёт с ключом `openai/gpt-4o-mini`. Если ничего не совпало — берётся `default`.

После правки:
```bash
docker compose restart monitor
```

Актуальные цены OpenRouter — здесь: https://openrouter.ai/models

---

## Шаг 6 — Удаление, обновление, бэкап

### Обновить Function после правок в `monitor_function.py`

Через UI: Admin Panel → Functions → ✏️ напротив `OpenWebUI Monitor` → заменить содержимое поля кода → Save. Перезапуск OpenWebUI **не нужен** — Function перезагружается налету.

Через API:
```bash
CONTENT=$(python3 -c "import json; print(json.dumps(open('monitor_function.py').read()))")
curl -s -X POST http://localhost:3000/api/v1/functions/id/openwebui_monitor/update \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"id\":\"openwebui_monitor\",\"name\":\"OpenWebUI Monitor\",\"meta\":{\"description\":\"Monitor filter\",\"manifest\":{}},\"content\":$CONTENT}"
```

### Полностью отключить мониторинг (без удаления)

Admin Panel → Functions → выключить тумблер **Enabled** напротив `OpenWebUI Monitor`. Function останется в системе, но перехват прекратится. Все ранее собранные данные в Monitor сохранятся.

### Удалить Function

Admin Panel → Functions → ⋯ → **Delete**. Или через API:
```bash
curl -s -X DELETE http://localhost:3000/api/v1/functions/id/openwebui_monitor/delete \
  -H "Authorization: Bearer $TOKEN"
```

### Бэкап БД Monitor

База — SQLite-файл по умолчанию: `./data/monitor.db` на хосте (вне контейнера).

Резервная копия — обычное копирование файла:
```bash
cp ./data/monitor.db ./data/monitor.db.bak.$(date +%F)
```

Для production рекомендуется PostgreSQL — см. README.md, раздел 7.

### Удалить весь стек

```bash
docker compose down                  # без потери volumes
docker compose down -v               # вместе с volumes (потеря всех данных OpenWebUI и Monitor!)
```

---

## Диагностика

### Запрос не появляется в Monitor

Идти по цепочке:

**1. Function реально загружена?**
```bash
docker compose logs open-webui | grep "Loaded module: function_openwebui_monitor"
```
Если нет такой строки — Function не сохранилась или содержит синтаксическую ошибку. Зайти в Admin Panel → Functions → ✏️ → проверить.

**2. Function активна и глобальна?**
```bash
curl -s http://localhost:3000/api/v1/functions/id/openwebui_monitor \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('is_active:', d['is_active'], '| is_global:', d['is_global'])"
```
Если хотя бы одно `False` — выполнить toggle (см. Шаг 3B пункты 3, 4).

**3. Доступен ли Monitor с контейнера OpenWebUI?**
```bash
docker compose exec open-webui sh -c "wget -qO- http://monitor:8088/api/stats/summary"
# Должен вернуть JSON
```
Если ошибка `bad address` — контейнеры в разных сетях. Если `connection refused` — Monitor не запущен.

**4. Какой MONITOR_URL установлен у Function?**
```bash
curl -s http://localhost:3000/api/v1/functions/id/openwebui_monitor \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('valves'))"
```

**5. Прямая проверка ingest-эндпоинта**
```bash
curl -s -X POST http://localhost:8088/api/ingest \
  -H "Content-Type: application/json" \
  -d '{"user_id":"test","user_email":"t@t.t","model":"test/m","timestamp":"2026-04-28T00:00:00Z","messages":[{"role":"user","content":"hi"}],"response":"hello","input_tokens":1,"output_tokens":1,"latency_ms":100}'
# Ожидается: {"ok":true}
```
Затем `curl http://localhost:8088/api/logs?limit=1` — должна быть запись.

### Чат сломался / ругается на Function

Этого не должно быть — все методы Function обёрнуты в `try/except`. Если всё же:
```bash
docker compose logs open-webui --tail 100 | grep -iE "function|error|exception"
```
Как временное решение — выключить Function (Admin Panel → Functions → тумблер Enabled). Чат сразу заработает, мониторинг будет приостановлен.

### Дашборд показывает «estimated» токены

Это значит, что LLM API в ответе не вернул поле `usage`. Monitor оценивает токены через `tiktoken` (cl100k_base — кодировка GPT-4). Для большинства моделей через OpenRouter `usage` приходит корректно. Если для конкретной модели всегда `estimated` — проверить, что у соединения OpenRouter включён параметр `usage: {include: true}` (через UI: Admin → Connections → ⋯ → Usage tracking).

---

## Контакт

Если в инструкции нашли неточность или столкнулись с непокрытым случаем — напишите в чат проекта (или указать сюда канал поддержки).
