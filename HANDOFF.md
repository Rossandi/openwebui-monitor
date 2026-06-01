# HANDOFF — что в этом репо и с чего начать

Production-ready код для OpenWebUI Monitor v2.

## Что внутри

| Файл | Зачем |
|---|---|
| `MIGRATION_GUIDE.md` | **Главный документ.** Пошаговая инструкция миграции v1 → v2 в проде. С чего начать. |
| `README.md` | Общее описание архитектуры |
| `PROXY_MIGRATION_PLAN.md` | Технический design-документ (для понимания «почему так») |
| `.env.example` | Шаблон переменных окружения — скопировать в `.env` и заполнить секретами |
| `docker-compose.yml` | Базовый стек: postgres, monitor, open-webui |
| `docker-compose.prod.yml` | Production overlay: Caddy + HTTPS + resource limits + log rotation |
| `Caddyfile` | Reverse proxy + Let's Encrypt + security headers (вписать ваши домены) |
| `Dockerfile` | Образ Monitor |
| `requirements.txt` | Python deps |
| `monitor/` | Исходники Monitor (FastAPI + proxy + sync worker + dashboard) |
| `monitor_function.py` | **OpenWebUI Function v2.1** — загрузить через UI или API. Захватывает текст промтов. |
| `openrouter_image_studio_patched.py` | **OpenWebUI Pipe v0.4 для Image Studio.** Загрузить только если используется генерация картинок. Без патча — Pipe пойдёт мимо Monitor и расход на картинки не будет учитываться. |
| `backup.sh` | PostgreSQL backup + ротация 30 дней. Cron-ready. |
| `model_pricing.json` | Legacy. **Не используется** в v2 (цены приходят от OpenRouter). Удалить можно. |

## Quick start (одной командой)

```bash
# 0. Распаковать (если из tar.gz) или склонировать
git clone https://github.com/Rossandi/openwebui-monitor.git
cd openwebui-monitor

# 1. Заполнить .env
cp .env.example .env && nano .env
#    OPENAI_API_KEY=sk-or-v1-...        (OpenRouter key с балансом)
#    WEBUI_SECRET_KEY=$(openssl rand -hex 32)
#    POSTGRES_PASSWORD=$(openssl rand -base64 24)
#    TAVILY_API_KEY=tvly-...            (если есть веб-поиск)

# 2. В Caddyfile — вписать ваши домены (chat.* и monitor.*) + email админа

# 3. Запустить
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# 4. По шагам из MIGRATION_GUIDE.md разделов 3.4–3.6 — загрузить Function и
#    (опционально) Image Studio Pipe через API + переключить connection
```

## Полный план (с откатами и проверками)

См. **MIGRATION_GUIDE.md**.

## Git

Репозиторий: https://github.com/Rossandi/openwebui-monitor (приватный)

Если нужен доступ — попросить владельца добавить как collaborator.

## Контакт

Открыть issue в репозитории или связаться с автором проекта.
