# Обновление бота под MAX API (июнь 2026)

Краткая сводка изменений для миграции с `platform-api.max.ru` на `platform-api2.max.ru` и корректной работы в Docker на VPS (reg.ru / ISPmanager).

**Дедлайн MAX:** до **19 июля 2026** перейти на `platform-api2.max.ru` и использовать доверенные сертификаты (в т.ч. НУЦ Минцифры).

---

## 1. Изменения в коде

### `bot.py`

| Было | Стало |
|------|--------|
| `https://platform-api.max.ru` | `https://platform-api2.max.ru` |

Константа `API_BASE` — все исходящие запросы (`/me`, `/messages`, `/subscriptions`, `/chats` и т.д.) идут на новый домен.

Токен по-прежнему передаётся в заголовке `Authorization` (не в query).

---

## 2. Изменения в `Dockerfile`

1. **Сертификаты НУЦ Минцифры** — при сборке образа скачиваются и добавляются в системное хранилище CA:
   - `https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt`
   - `https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt`

2. **`SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`** — httpx по умолчанию использует bundle **certifi** (без Минцифры). Переменная заставляет Python/httpx брать **системный** bundle, куда попали сертификаты выше.

Без этой переменной возможна ошибка:
```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate
```

---

## 3. Изменения в `docker-compose.yml`

| Параметр | Назначение |
|----------|------------|
| `dns: 8.8.8.8, 77.88.8.8` | DNS для контейнера (устраняет `Temporary failure in name resolution`) |
| `environment.SSL_CERT_FILE` | Дублирует настройку из Dockerfile для runtime |
| `ports: "8000:8000"` | Проброс webhook-сервера на хост |

Если порт **8000** занят другим контейнером — либо остановите старый бот, либо смените маппинг (например `"8010:8000"`) и обновите nginx (см. ниже).

---

## 4. Nginx (VPS reg.ru / ISPmanager)

Конфиг **не** в репозитории. На сервере `194-67-122-251.regru.cloud`:

**Файл:**
```
/etc/nginx/vhosts-resources/194-67-122-251.regru.cloud/webhook_proxy.conf
```

**Актуальный фрагмент для основного бота:**
```nginx
location ^~ /webhook {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location ^~ /health {
    proxy_pass http://127.0.0.1:8000;
    ...
}
```

Если бот слушает на **8010** снаружи — замените `8000` на `8010` в `proxy_pass`.

**В `.env` бота:**
```
WEBHOOK_URL=https://194-67-122-251.regru.cloud/webhook
```

После правки nginx:
```bash
nginx -t && systemctl reload nginx
```

Папки `sites-enabled/` на этом VPS могут быть **пустыми** — это нормально для ISPmanager, править нужно `vhosts-resources/`.

---

## 5. Переменные `.env` (без изменений логики)

| Переменная | Описание |
|------------|----------|
| `MAX_BOT_TOKEN` | Токен бота из кабинета MAX |
| `WEBHOOK_URL` | Публичный HTTPS URL, например `https://194-67-122-251.regru.cloud/webhook` |
| `WEBHOOK_SECRET` | Секрет для заголовка `X-Max-Bot-Api-Secret` (5–256 символов `[a-zA-Z0-9_-]`) |
| `WEBHOOK_LISTEN` | По умолчанию `0.0.0.0:8000` (внутри контейнера) |
| `ADMIN_USER_IDS` | ID мастер-админов через запятую |

---

## 6. Команды на сервере (деплой)

Выполнять **по SSH на VPS**, не на локальном Windows (локальный Docker Desktop к серверу не подключён).

```bash
ssh root@194.67.122.251
cd ~/max_users_resend

# Обновить код
git pull

# Если порт 8000 занят старым ботом — остановить его
docker ps | grep 8000
docker stop max_resend_chat_to_chat-max-forward-bot-1   # имя может отличаться

# Пересборка и запуск
docker compose down
docker compose build --no-cache    # при первой установке CA или после правок Dockerfile
docker compose up -d

# Логи
docker compose logs -f --tail=30
```

**Успешный старт:**
```
Logged in as bot ID ...
Webhook: URL=https://194-67-122-251.regru.cloud/webhook ...
```

---

## 7. Проверки после деплоя

```bash
# DNS на хосте
getent hosts platform-api2.max.ru

# SSL + API с хоста
curl -sI https://platform-api2.max.ru | head -5

# Бот локально
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/webhook

# Через nginx (как видит MAX)
curl -s https://194-67-122-251.regru.cloud/webhook

# SSL внутри контейнера (ожидаем 401 или 404, не SSL error)
docker compose run --rm --no-deps max-forward-bot python -c \
  "import httpx; r=httpx.get('https://platform-api2.max.ru/me', headers={'Authorization':'test'}, timeout=10); print(r.status_code)"
```

---

## 8. Типичные ошибки

| Ошибка | Причина | Решение |
|--------|---------|---------|
| `port is already allocated` | Другой контейнер на 8000 | `docker stop <старый-контейнер>` или сменить порт в compose + nginx |
| `Temporary failure in name resolution` | DNS в контейнере | `dns:` в compose, `docker compose up --force-recreate` |
| `CERTIFICATE_VERIFY_FAILED` | httpx + certifi без Минцифры | `SSL_CERT_FILE` в Dockerfile/compose, пересборка `--no-cache` |
| `Container is restarting` | Crash loop из-за ошибки выше | `docker compose stop`, исправить причину, `up -d` |
| `dockerDesktopLinuxEngine` на Windows | `docker compose` запущен локально | Команды только на VPS через SSH |

---

## 9. Другие боты на том же сервере

На VPS могут параллельно работать другие проекты:

| Контейнер | Порт | Webhook в nginx |
|-----------|------|-----------------|
| `max_users_resend` (этот) | 8000 | `/webhook`, `/health` |
| `max_resend_from_channel_to_chat` | 8091 | `/webhook_8091`, `/health_8091` |

Не запускайте два контейнера на одном порту хоста.

---

## 10. Чеклист миграции

- [ ] `API_BASE` = `https://platform-api2.max.ru` в `bot.py`
- [ ] Образ пересобран с CA Минцифры и `SSL_CERT_FILE`
- [ ] `docker compose up` без ошибок DNS/SSL
- [ ] В логах: login + webhook subscribed
- [ ] `curl https://…/webhook` → `{"ok": true, ...}`
- [ ] Старый бот на 8000 остановлен (если заменяется новым)
- [ ] nginx `proxy_pass` указывает на актуальный порт бота
