"""
Говорит Telegram, куда присылать сообщения бота.
Запускается один раз с твоего компьютера, после того как бот выложен на Vercel.

Как запустить (на Mac):
    export BOT_TOKEN="токен_от_BotFather"
    export WEBHOOK_SECRET="тот_же_секрет_что_на_Vercel"
    python3 set_webhook.py https://имя-проекта.vercel.app

Чтобы отключить вебхук:
    python3 set_webhook.py --delete

Нужен только Python, ничего устанавливать не надо.
"""

import json
import os
import sys
import urllib.error
import urllib.request

COMMANDS = [
    {"command": "start", "description": "Регистрация и список"},
    {"command": "list", "description": "Показать список"},
    {"command": "me", "description": "Мое место и баллы"},
    {"command": "help", "description": "Помощь"},
    {"command": "id", "description": "Мой Telegram ID"},
]


def telegram(token: str, method: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        return json.load(e)


def check_site(url: str) -> None:
    """Проверяем, что сайт на Vercel открывается."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            body = resp.read().decode(errors="replace")
        if '"ok"' in body:
            print("Сайт на Vercel отвечает. Хорошо.")
        else:
            print("Сайт открылся, но ответ не похож на бота. Проверь адрес.")
    except urllib.error.HTTPError as e:
        print(f"Сайт ответил ошибкой {e.code}.")
        if e.code in (401, 403):
            print(
                "Похоже, включена защита Vercel. Открой проект на Vercel:\n"
                "Settings -> Deployment Protection -> выключи Vercel Authentication."
            )
    except Exception as e:
        print(f"Не удалось открыть сайт: {e}")


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        sys.exit('Сначала задай токен: export BOT_TOKEN="123:ABC..."')

    if "--delete" in sys.argv:
        print(telegram(token, "deleteWebhook", {"drop_pending_updates": True}))
        return

    secret = os.getenv("WEBHOOK_SECRET")
    if not secret:
        sys.exit('Сначала задай секрет: export WEBHOOK_SECRET="..."')
    if len(sys.argv) < 2:
        sys.exit("Напиши адрес проекта: python3 set_webhook.py https://имя.vercel.app")

    base = sys.argv[1].strip().rstrip("/")
    if not base.startswith("https://"):
        sys.exit("Адрес должен начинаться с https://")

    check_site(base)

    result = telegram(
        token,
        "setWebhook",
        {
            "url": f"{base}/webhook",
            "secret_token": secret,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": True,
        },
    )
    print("setWebhook:", result.get("description", result))
    if not result.get("ok"):
        sys.exit(1)

    result = telegram(token, "setMyCommands", {"commands": COMMANDS})
    print("setMyCommands:", "ok" if result.get("ok") else result)

    info = telegram(token, "getWebhookInfo").get("result", {})
    print("Адрес вебхука:", info.get("url"))
    if info.get("last_error_message"):
        print("Последняя ошибка Telegram:", info["last_error_message"])
    else:
        print("Ошибок нет. Напиши боту /start.")


if __name__ == "__main__":
    main()
