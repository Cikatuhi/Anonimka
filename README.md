# Anonimno-Artem-

## Настройка SOCKS5 для Telegram

Если Telegram недоступен напрямую, задайте прокси через переменную окружения
`TELEGRAM_PROXY_URL`. Пароль не храните в исходном коде или GitHub:

```text
TELEGRAM_PROXY_URL=socks5://USER:PASSWORD@HOST:PORT
```

Для SOCKS5 установите зависимость python-telegram-bot с поддержкой SOCKS:

```bash
pip install "python-telegram-bot[socks]"
```
