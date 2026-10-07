"""
Точка входа для Vercel.

Telegram сам присылает сюда каждое сообщение и нажатие кнопки (вебхук).
Бот отвечает и засыпает до следующего сообщения.
"""

import hmac
import logging
import os

from aiogram import Bot, Dispatcher
from fastapi import FastAPI, Header, HTTPException, Request

from handlers import router

logging.basicConfig(level=logging.INFO)

dp = Dispatcher()
dp.include_router(router)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def make_bot() -> Bot:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise HTTPException(500, "BOT_TOKEN не задан")
    return Bot(token)


@app.get("/")
async def index():
    """Страница для проверки: открой адрес проекта в браузере."""
    return {"ok": True, "service": "surname-bot"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    # Секрет защищает от чужих запросов: без него любой мог бы
    # притвориться админом и прислать боту поддельное сообщение.
    secret = os.getenv("WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(500, "WEBHOOK_SECRET не задан")
    if not hmac.compare_digest(
        (x_telegram_bot_api_secret_token or "").encode(), secret.encode()
    ):
        raise HTTPException(403, "Неверный секрет")

    data = await request.json()
    bot = make_bot()
    try:
        await dp.feed_webhook_update(bot, data)
    except Exception:
        # Всегда отвечаем 200, иначе Telegram будет слать то же сообщение снова
        logging.exception("Ошибка при обработке обновления")
    finally:
        await bot.session.close()
    return {"ok": True}
