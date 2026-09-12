# PROJECT_MAP

Карта актуальной архитектуры проекта Anonimka. Документ нужен как быстрый
индекс перед изменениями: сначала найти нужный поток здесь, затем проверить
реализацию в указанном файле и связанные SQL-запросы.

Карту нужно обновлять вместе с изменением маршрутов, таблиц, сервисов и
переменных окружения. Секретные значения (пароли, токены, ключи и содержимое
`.env`) в этот файл не записываются.

## Быстрый поиск

| Задача | Основной файл | Точки входа |
| --- | --- | --- |
| Telegram-бот и сценарии пользователя | `AnonimBot.py` | `main()`, `start()`, `handle_message()`, callback handlers |
| Состояние подписки и списания | `subscription.py` | `SubscriptionManager`, `process_expired_subscription()`, `handle_payment_webhook()` |
| HTTP-клиент CloudPayments | `cloudpayments.py` | `CloudPaymentsClient.charge_card()`, `list_payments*()` |
| Webhook CloudPayments и API подписки | `webhook_handler.py` | `handle_webhook()`, `/subscription/*` |
| Планировщик списаний | `main.py` | `lifespan()`, `process_expired_subscriptions_job()` |
| Платёжная страница и документы | `payment_site.py` | `/pay/{user_id}`, `/doc1`, `/doc2`, `/doc3` |
| Админка, HTML и её API | `admin_panel.py` | `dashboard()`, страницы `/users`, `/messages`, `/subscriptions`, `/broadcast` и другие |
| Рассылки | `admin_panel.py`, `worker.py` | `/broadcast/send`, `claim_next_job()`, `process_job()` |
| PostgreSQL, миграции и запросы | `database.py` | `init_db()`, `*_schema()`, функции доступа к данным |
| Переменные окружения и тарифы | `config.py` | `Config`, `env_flag()` |
| Часовые пояса | `time_utils.py` | `now_msk()`, `to_db_timestamp()`, `as_utc()` |

## Точки входа

- `AnonimBot.py` запускает Telegram-приложение `python-telegram-bot` в режиме
  polling. Регистрация обработчиков находится в `main()` внизу файла.
- `main.py` создаёт FastAPI-приложение платёжного backend, подключает router из
  `webhook_handler.py` и запускает APScheduler.
- `payment_site.py` создаёт отдельное FastAPI-приложение для HTML платёжной
  страницы и документов.
- `admin_panel.py` создаёт отдельное FastAPI-приложение. HTML, CSS и JavaScript
  админки сейчас встроены в Python-строки; отдельного frontend-каталога нет.
- `worker.py` запускает бесконечный async-цикл обработки очереди рассылок.
- `database.py` не является отдельным процессом: его импортируют все сервисы.

## Runtime и systemd

В production unit-файлы находятся в `/etc/systemd/system` (исходники в
`services/`). Все пять процессов используют одну рабочую копию
`/opt/anonimka`, пользователя `codexops`, локальный venv и файл
`/etc/anonimka/anonimka.env`.

| Unit | Python-файл | Назначение | Порт/режим |
| --- | --- | --- | --- |
| `webhook-handler.service` | `AnonimBot.py` | Telegram-бот | HTTP-порта нет, polling |
| `payment-system.service` | `main.py` | Webhook CloudPayments, API подписок, scheduler | `0.0.0.0:8002` через `PORT=8002` |
| `payment-site.service` | `payment_site.py` | Платёжная страница и документы | `0.0.0.0:8001` |
| `admin-panel.service` | `admin_panel.py` | Приватная админка | `127.0.0.1:8000` через `ADMIN_BIND_HOST` |
| `worker.service` | `worker.py` | Очередь Telegram-рассылок | HTTP-порта нет |

Все units настроены на автоматический перезапуск. Для точных текущих значений
на сервере смотреть `systemctl cat <unit>`: окружение и unit-файлы могут быть
новее или отличаться от копии в Git.

### Внешний трафик

Nginx и TLS не входят в этот репозиторий. Обычно Nginx принимает HTTPS,
проксирует платёжный сайт на `8001`, webhook/backend на `8002`, а админку на
loopback `8000` (или через SSH-туннель). При проблеме доступа проверять
`/etc/nginx/sites-enabled/*`, `nginx -t` и соответствующие `proxy_pass`.

## HTTP API

### Payment backend (`main.py` + `webhook_handler.py`, обычно порт 8002)

| Метод и путь | Назначение |
| --- | --- |
| `GET /` | Сводка состояния backend, тарифы и политика retry |
| `GET /health` | Состояние scheduler и количество `retry_low` |
| `POST /webhook` | Приём нативного или legacy payload CloudPayments; журналирование и обработка результата |
| `POST /subscription/init` | Совместимый endpoint первоначальной инициализации подписки |
| `POST /subscription/process-expired` | Ручной запуск проверки истёкших подписок |
| `GET /subscription/{user_id}` | Текущий статус подписки пользователя |
| `POST /subscription/cancel/{user_id}` | Отключение автопродления с сохранением оплаченного `end_date` |
| `POST /subscription/check-expired` | Ещё один совместимый endpoint проверки истёкших подписок |

`main.py` подключает scheduler только если `PAYMENT_SCHEDULER_ENABLED` не
отключён. Проверка запускается каждые 5 минут; сам scheduler инициирует charge,
но доступ продлевает только подтверждённый webhook.

### Payment site (`payment_site.py`, обычно порт 8001)

| Метод и путь | Назначение |
| --- | --- |
| `GET /` | Главная/документация платёжного сайта |
| `GET /doc1` | Политика конфиденциальности |
| `GET /doc2` | Условия использования/оферта |
| `GET /doc3` | Тарификация и правила рекуррентных платежей |
| `GET /pay/{user_id}` | Страница оплаты 5 ₽; записывает визит и показывает согласия или виджет |
| `GET /favicon.svg`, `/favicon.ico`, `/pay/favicon.*` | Favicon без отдельного файла |

### Admin panel (`admin_panel.py`, loopback 8000)

Аутентификация: `GET /admin/login`, `POST /admin/login`, `GET /admin/logout`.
Остальные маршруты проходят через `auth_middleware`.

| Группа | Маршруты |
| --- | --- |
| Состояние и дашборд | `GET /health`, `GET /monitoring/data`, `GET /dashboard/data`, `GET /dashboard/events`, `GET /` |
| Таблицы | `GET /users`, `/messages`, `/subscriptions`, `/blocked`, `/unsubscribed`, `/webhooks`, `/monitoring`, `/settings`, `/subgram`, `/payment-visits`, `/payments` |
| Настройки | `POST /api/settings/update` |
| Рассылка | `GET /broadcast`, `POST /broadcast/send`, `GET /broadcast/status/{job_id}`, `POST /broadcast/pause/{job_id}`, `POST /broadcast/resume/{job_id}`, `POST /broadcast/archive/{job_id}`, `GET /broadcast/list` |
| Действия с подписками | `POST /unsubscribed/restore/{user_id}`, `POST /subscription/block/{user_id}` |

## PostgreSQL

Подключение и пул находятся в `database.py`. Параметры берутся из окружения
`DB_*`; при наличии `DB_SSL_CA` создаётся SSL-контекст. Каждый процесс имеет
собственный asyncpg pool. Миграции идемпотентны и выполняются при старте
нескольких сервисов под advisory lock.

### Основные таблицы

| Таблица | Ключевые данные и роль |
| --- | --- |
| `users` | `telegram_id` (PK), username, имя, `registered_at`, `has_seen_premium_offer`; основной каталог пользователей |
| `blocked_users` | `telegram_id` (PK), профиль и `blocked_at`; маркер блокировки Telegram, строка в `users` сохраняется |
| `messages` | отправитель, получатель, текст/тип, `created_at`; полная история сообщений |
| `subscriptions` | пользователь, план, статус, `start_date`, `end_date`, card token, provider id; текущий доступ и состояние списаний |
| `unsubscribed_users` | архив отмен/технических блокировок, причина, профиль и JSON-снимок подписок для восстановления |
| `payment_tokens` | одноразовые ссылки/токены оплаты, срок действия и признак использования; `user_id` связан с `users` каскадно |
| `payment_link_visits` | агрегат визитов на платёжную ссылку по пользователю: количество, первый и последний визит |
| `webhook_events` | неизменяемый журнал входящих CloudPayments payload в JSONB, статус, transaction/account id |
| `payment_events` | ledger рекуррентных операций: уникальный `transaction_id`, пользователь, сумма в копейках, статус, время |
| `broadcast_jobs` | сообщение, статус задачи, счётчики, прогресс, timestamps, heartbeat и архивирование |
| `broadcast_job_recipients` | зафиксированная аудитория рассылки и статус каждого получателя; FK на `users` удалён миграцией, чтобы история переживала блокировку |
| `service_heartbeats` | heartbeat сервисов; сейчас worker пишет строку каждые ~30 секунд |
| `settings` | простые настройки key/value, используемые админкой и платёжной страницей; текущий ключ `show_payment_checkboxes` |

### Связи и правила удаления

- `subscriptions.user_id -> users.telegram_id` защищён `ON DELETE RESTRICT`.
  Удаление пользователя не должно молча удалить историю подписки.
- `payment_events.user_id -> users.telegram_id` также мигрируется к
  `RESTRICT/NO ACTION`, чтобы сохранить бухгалтерскую историю.
- `payment_tokens.user_id` остаётся `ON DELETE CASCADE`: токены временные и
  могут исчезать вместе с пользователем.
- `broadcast_job_recipients` намеренно не имеет FK на пользователя: запись
  рассылки должна переживать перенос пользователя в `blocked_users` или архив.
- `ensure_message_retention_schema()` удаляет старый триггер
  `trigger_clean_messages`; текущий код не ограничивает таблицу `messages` по
  количеству строк.

### Dashboard notifications

`ensure_admin_dashboard_notifications()` создаёт statement-level triggers на
`users`, `blocked_users`, `unsubscribed_users`, `messages`,
`payment_link_visits`, `webhook_events`, `payment_events` и `subscriptions`.
Триггер вызывает `pg_notify('admin_dashboard_changed', ...)` после commit.

`admin_panel.py` держит одно выделенное LISTEN-соединение, публикует сигнал
подключённым браузерам через `GET /dashboard/events` (SSE), а браузер делает
один запрос `GET /dashboard/data`. SQL-снимок дашборда дополнительно разделяется
между одновременными запросами на короткий TTL и сбрасывается при `NOTIFY`; это
инвалидация, а не передача самих данных. При разрыве LISTEN выполняется
переподключение.

Дашборд считает:

- доступных пользователей: `users` без строк в `blocked_users` и
  `unsubscribed_users`;
- активные подписки: действующий `active` или ручной `cancelled` до
  сохранённого `end_date`;
- сообщения, переходы на оплату и почасовые графики за сегодняшний день;
- общую сумму успешных списаний из `webhook_events` и `payment_events` без
  дублей по transaction id;
- жив ли worker по `service_heartbeats`;
- баланс SubGram через внешний API с process-local cache на 60 секунд (это не
  PostgreSQL-событие).

Помимо SSE остаётся редкий fallback-refresh дашборда и health раз в 60 секунд,
чтобы восстановиться после прокси или сетевого сбоя.

## Платежи и подписки

### Первичная оплата 5 ₽

1. Бот в `AnonimBot.py` формирует `${SITE_URL}/pay/{user_id}` после нажатия
   «Узнать кто это», если оплаченного доступа нет.
2. `payment_site.py` проверяет существование пользователя, записывает визит в
   `payment_link_visits` и показывает два согласия. При `checkout=1` или при
   отключённых согласиях монтирует CloudPayments PaymentBlocks.
3. Виджет отправляет 5 RUB, `tokenize=true`, `accountId=user_id`.
4. CloudPayments вызывает `POST /webhook` на payment backend.
5. `webhook_handler.handle_webhook()` нормализует JSON/form payload, пишет его
   в `webhook_events`, а при `Status=Completed` создаёт или активирует
   подписку, сохраняет card token и убирает архив отмены.

### Тарифы и рекуррентные списания

Тарифы находятся в `config.py` (суммы хранятся в копейках):

- `HIGH`: 699 ₽ / 7 дней;
- `MEDIUM`: 299 ₽ / 3 дня;
- `LOW`: 99 ₽ / 1 день;
- первоначальный платёж: 5 ₽.

`SubscriptionManager` реализует цепочку `HIGH -> MEDIUM -> LOW` при немедленном
отказе API. `CloudPaymentsClient.charge_card()` только инициирует charge через
`/payments/tokens/charge`; результат считается подтверждённым после webhook.
Обработчик рекуррентного webhook:

- блокирует повторную обработку transaction через уникальный
  `payment_events.transaction_id` и advisory lock;
- при успехе продлевает доступ на дни оплаченного плана и записывает ledger;
- при отказе выбирает следующий план или переводит подписку в `retry_low` с
  `next_retry_at`;
- при кодах недоступной карты архивирует подписку и прекращает попытки;
- при ручной отмене сохраняет оплаченный `end_date`, но не запускает новые
  списания (даже если уже начатый charge завершился позже).

`main.py` запускает эту проверку каждые пять минут, а не отдельным cron.

## Рассылки

1. Администратор отправляет форму на `POST /broadcast/send`.
2. В транзакции создаётся `broadcast_jobs` и фиксируется аудитория в
   `broadcast_job_recipients`. Исключаются пользователи из `blocked_users` и
   `unsubscribed_users`; состав аудитории после создания задачи не плавает.
3. `worker.py::claim_next_job()` атомарно получает одну pending-задачу через
   `FOR UPDATE SKIP LOCKED` и ставит `running`/heartbeat.
4. `claim_recipient_batch()` арендует до 50 pending-получателей. Для уже
   заблокированных пользователей worker ставит `blocked` без вызова Telegram.
5. Telegram API вызывается с HTML-текстом и кнопками «Ответить»/«Узнать кто
   это». Временные сетевые ошибки и HTTP 429/5xx повторяются до трёх раз с
   backoff; интервал между отправками около 40 мс.
6. `record_recipient_results()` одним SQL обновляет результаты и счётчики
   задачи. Статусы: `pending`, `processing`, `sent`, `failed`, `blocked`,
   `cancelled`.
7. Pause/resume/archive находятся в `admin_panel.py`. Heartbeat и
   `recover_stale_work()` возвращают незавершённые аренды после падения worker
   (порог stale-задачи — 15 минут).

## Telegram-сценарии

Обработчики регистрируются в `AnonimBot.main()`:

- `/start`: снимает блок-маркер при возвращении пользователя, регистрирует
  пользователя и выдаёт deep link;
- `/info`: показывает поддержку и ссылки на документы;
- `/unsub`: подтверждает отмену автопродления; оплаченный период не отзывается;
- обычное сообщение по deep link: отправляет анонимный текст/фото/видео/voice/
  video note/sticker получателю и сохраняет запись в `messages`;
- `reply_*` и `reply_anon`: поток анонимного ответа с временной сессией;
- `who_is_it_*`: проверяет премиум-доступ; при отсутствии доступа отправляет
  ссылку на оплату 5 ₽, при наличии раскрывает отправителя;
- `subgram-op`: повторная проверка обязательных заданий SubGram перед ответом;
- неизвестные callback-и получают безопасное сообщение об устаревшей кнопке.

Блокировка Telegram фиксируется в `blocked_users`, но строка в `users` и
платёжные записи не удаляются. Новая регистрация через `/start` может снять
блок-маркер.

## Переменные окружения

Ниже только имена. Значения находятся в `/etc/anonimka/anonimka.env` на сервере
и не должны попадать в Git:

`BOT_TOKEN`, `SITE_URL`, `ADMIN_ID`, `WELCOME_STICKER_FILE_ID`,
`CLOUDPAYMENTS_PUBLIC_ID`, `CLOUDPAYMENTS_API_SECRET`, `WEBHOOK_URL`,
`PAYMENT_SCHEDULER_ENABLED`, `HOST`, `PORT`, `ADMIN_BIND_HOST`, `ADMIN_PORT`,
`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_SSL_CA`,
`SUBGRAM_API_TOKEN`, `SUBGRAM_API_KEY`, `TELEGRAM_PROXY_URL`.

В production `SITE_URL` нужно задавать явно: fallback в Telegram-боте и
fallback платёжного сайта исторически различаются. Аналогично проверять
`PORT`/`ADMIN_PORT` по systemd unit, а не по значениям по умолчанию в Python.

## Зависимости и границы проекта

Зависимости перечислены в `requirements.txt`: FastAPI/Uvicorn, asyncpg,
python-telegram-bot, httpx, aiohttp, APScheduler, psutil, python-dotenv и
python-multipart. Docker-файлов и каталога тестов в репозитории нет.

Внешние системы, которые нельзя эмулировать только изменением Python-кода:

- Telegram Bot API и доступность сети/прокси;
- CloudPayments API, webhook-настройки и токены карт;
- SubGram API и баланс;
- PostgreSQL Timeweb/другой хостинг;
- Nginx, DNS, TLS-сертификат и systemd на сервере.

## Безопасная диагностика

Команды ниже не выводят секреты:

```bash
cd /opt/anonimka
git status --short
git log -1 --oneline
git remote -v
systemctl status webhook-handler.service payment-system.service payment-site.service admin-panel.service worker.service --no-pager
journalctl -u payment-system.service -n 100 --no-pager
journalctl -u admin-panel.service -n 100 --no-pager
curl -fsS http://127.0.0.1:8002/health
curl -fsS http://127.0.0.1:8000/health
ss -ltnp | grep -E ':(8000|8001|8002)'
sudo nginx -t
```

Для PostgreSQL использовать `psql` только с паролем из защищённого окружения;
не выполнять `env`, `printenv`, `cat .env` и не включать card token/API secret
в логи или скриншоты. Проверять схему и индексы лучше узкими `SELECT` из
`pg_catalog`, а не выгрузкой всей базы.

## Известные места, которые нужно проверить перед переносом

- `subscription.py` содержит исторический `sys.path.append('/root/Anonimno-Artem-/')`.
  При переносе на новый путь убедиться, что импортируется именно текущий
  `database.py`, а не старый checkout.
- Рабочая копия, указанная systemd, — `/opt/anonimka`; команды для
  `/root/Anonimka` относятся к другой/старой схеме развёртывания.
- Встроенный HTML админки и платёжного сайта означает, что изменение UI обычно
  затрагивает большой Python-файл; после правок обязательны `py_compile` и
  проверка маршрута через локальный HTTP.
- SSE требует, чтобы Nginx не буферизовал `/dashboard/events` (в приложении
  выставлен `X-Accel-Buffering: no`). При задержке обновлений проверять proxy,
  PostgreSQL LISTEN и журнал `admin-panel.service`.
- Баланс SubGram не обновляется по PostgreSQL NOTIFY: значение внешнее и
  ограничено кэшем 60 секунд, даже если остальные карточки обновились сразу.

## Минимальная проверка после изменения

1. `git diff --check`.
2. `PYTHONPYCACHEPREFIX=/tmp/anonimka-pycache python3 -m py_compile *.py`.
3. Проверить изменённый endpoint локальным `curl` и соответствующий unit в
   `systemctl status`.
4. Для миграции БД убедиться, что приложение стартует на копии/резервной базе,
   advisory-lock не оставил незавершённую транзакцию, а ограничения FK и
   индексы соответствуют разделу PostgreSQL выше.
