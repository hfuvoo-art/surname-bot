"""
Логика бота: регистрация, список, меню, админ-меню, тихое обновление.

Как работает бот:
1. Человек заходит в бота, пишет имя, фамилию и свой нынешний балл.
2. Нажал на человека: ему +1,5 балла, и он уходит вниз списка.
3. Сверху те, у кого баллов меньше. Снизу те, у кого больше.
   Если баллы равны, выше стоит тот, кого нажимали раньше.
4. На каждой кнопке видно, когда человека нажимали в последний раз.
"""

import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import storage

POINTS_PER_CLICK = 1.5
MAX_PEOPLE = 99  # у Telegram лимит 100 кнопок, одна нужна для "Назад"/"Обновить"
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
ADMIN_IDS = {
    int(x)
    for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",")
    if x.isdigit()
}

# Тексты кнопок нижнего меню
BTN_LIST = "Список"
BTN_ME = "Мой балл"
BTN_HELP = "Помощь"
BTN_REGISTER = "Регистрация"
BTN_ADMIN = "Админ-меню"

router = Router()


# ---------- вид списка ----------

def fmt_score(score: float) -> str:
    return f"{score:g}".replace(".", ",")


def fmt_time(ts: float | None) -> str:
    """Всегда число, месяц и время: 07.10 14:05. Не нажимали: «—»."""
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


def build_keyboard() -> InlineKeyboardMarkup | None:
    people = storage.get_people()
    if not people:
        return None
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{p['name']} · {fmt_score(p['score'])} · "
                    f"{fmt_time(p['clicked_at'])}"
                ),
                callback_data=f"hit:{p['id']}",
            )
        ]
        for p in people
    ]
    rows.append([InlineKeyboardButton(text="Обновить", callback_data="refresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


LIST_TEXT = (
    f"Нажми на себя: +{fmt_score(POINTS_PER_CLICK)} балла, "
    "и ты уйдешь вниз. Чужие кнопки нажимают только админ и его помощники.\n"
    "На кнопке: имя · баллы · когда нажимали в последний раз.\n"
    "Сверху те, у кого баллов меньше.\n"
    "Список обновляется сам."
)
EMPTY_TEXT = "Список пока пустой. Первым зарегистрируйся: нажми «Регистрация»."


# ---------- нижнее меню ----------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_menu(user_id: int) -> ReplyKeyboardMarkup:
    """Кнопки внизу экрана. Для незарегистрированных они другие."""
    if storage.get_person(user_id):
        rows = [
            [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_ME)],
            [KeyboardButton(text=BTN_HELP)],
        ]
    else:
        rows = [
            [KeyboardButton(text=BTN_REGISTER)],
            [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_HELP)],
        ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True, is_persistent=True
    )


async def send_list(message: Message) -> None:
    kb = build_keyboard()
    if kb is None:
        await message.answer(
            EMPTY_TEXT, reply_markup=main_menu(message.from_user.id)
        )
    else:
        sent = await message.answer(LIST_TEXT, reply_markup=kb)
        storage.track_list(sent.chat.id, sent.message_id)


async def refresh_message(call: CallbackQuery) -> None:
    """Обновить список в том сообщении, где нажали кнопку."""
    kb = build_keyboard()
    try:
        await call.message.edit_text(
            LIST_TEXT if kb else EMPTY_TEXT, reply_markup=kb
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ---------- тихое обновление списка у всех ----------
# Бот правит старые сообщения со списком. Telegram не присылает
# уведомление, когда сообщение меняется, поэтому у людей просто
# меняется порядок кнопок.
# На Vercel функция засыпает сразу после ответа, поэтому обновляем всех
# прямо внутри запроса, а не в фоне.

async def push_to_all(bot: Bot, skip: tuple[int, int] | None = None) -> None:
    rows = [
        r
        for r in storage.get_tracked()
        if (r["chat_id"], r["message_id"]) != skip
    ]
    if not rows:
        return
    kb = build_keyboard()
    text = LIST_TEXT if kb else EMPTY_TEXT
    sem = asyncio.Semaphore(4)  # не превышаем лимиты Telegram

    async def edit_one(chat_id: int, message_id: int) -> None:
        async with sem:
            for attempt in range(2):  # вторая попытка, если просят подождать
                try:
                    await bot.edit_message_text(
                        text,
                        chat_id=chat_id,
                        message_id=message_id,
                        reply_markup=kb,
                    )
                except TelegramRetryAfter as e:
                    if attempt == 0 and e.retry_after <= 3:
                        await asyncio.sleep(e.retry_after)
                        continue
                except TelegramBadRequest as e:
                    if "message is not modified" not in str(e):
                        storage.forget_list(chat_id, message_id)  # удалили
                except TelegramForbiddenError:
                    storage.forget_list(chat_id, message_id)  # заблокировали
                except Exception:
                    logging.exception("Не удалось обновить список у %s", chat_id)
                break
            await asyncio.sleep(0.06)

    await asyncio.gather(
        *(edit_one(r["chat_id"], r["message_id"]) for r in rows)
    )


async def deny(message: Message) -> None:
    await message.answer("Это может только админ бота. Свой номер: /id")


def help_text(user_id: int) -> str:
    text = (
        "Список людей с баллами.\n"
        f"Нажал на человека: +{fmt_score(POINTS_PER_CLICK)} балла, "
        "и он уходит вниз.\n\n"
        "Кнопки внизу:\n"
        f"{BTN_LIST} — показать список\n"
        f"{BTN_ME} — твое место и баллы\n"
        f"{BTN_HELP} — эта подсказка\n\n"
        "Команды: /start, /list, /me, /id"
    )
    if is_admin(user_id):
        text += (
            f"\n\nДля админа есть кнопка «{BTN_ADMIN}»: выдать помощникам доступ "
            "к чужим баллам, убрать человека, обнулить баллы, удалить список.\n"
            "Или командами: /remove Иван Иванов, /reset, /clear"
        )
    return text


# ---------- старт и регистрация ----------

async def begin_registration(message: Message) -> None:
    storage.set_reg(message.from_user.id, "name")
    await message.answer(
        "Напиши свои имя и фамилию.\nНапример: Иван Иванов",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    storage.clear_reg(message.from_user.id)
    if message.chat.type != "private":
        return await message.answer(
            "Зарегистрируйся в личке с ботом: открой его и нажми /start."
        )
    person = storage.get_person(message.from_user.id)
    if person:
        await message.answer(
            f"Ты в списке как {person['name']}.",
            reply_markup=main_menu(message.from_user.id),
        )
        return await send_list(message)
    await message.answer("Привет!")
    await begin_registration(message)


@router.message(F.text == BTN_REGISTER)
async def btn_register(message: Message) -> None:
    storage.clear_reg(message.from_user.id)
    if storage.get_person(message.from_user.id):
        await message.answer(
            "Ты уже в списке.", reply_markup=main_menu(message.from_user.id)
        )
        return await send_list(message)
    await begin_registration(message)


# ---------- кнопки нижнего меню и обычные команды ----------

@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message) -> None:
    await message.answer(
        help_text(message.from_user.id),
        reply_markup=main_menu(message.from_user.id),
    )


@router.message(Command("list"))
@router.message(F.text == BTN_LIST)
async def cmd_list(message: Message) -> None:
    await send_list(message)


@router.message(Command("me"))
@router.message(F.text == BTN_ME)
async def cmd_me(message: Message) -> None:
    people = storage.get_people()
    for place, p in enumerate(people, start=1):
        if p["tg_id"] == message.from_user.id:
            return await message.answer(
                f"{p['name']}\n"
                f"Баллы: {fmt_score(p['score'])}\n"
                f"Место в списке: {place} из {len(people)}\n"
                f"Последнее нажатие: {fmt_time(p['clicked_at'])}",
                reply_markup=main_menu(message.from_user.id),
            )
    await message.answer(
        "Тебя нет в списке. Нажми «Регистрация».",
        reply_markup=main_menu(message.from_user.id),
    )


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Твой Telegram ID: {message.from_user.id}")


# ---------- админ-меню ----------

def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Помощники (кто может менять баллы)", callback_data="adm:helpers")],
            [InlineKeyboardButton(text="Убрать человека", callback_data="adm:remove")],
            [InlineKeyboardButton(text="Обнулить баллы", callback_data="adm:reset")],
            [InlineKeyboardButton(text="Удалить весь список", callback_data="adm:clear")],
            [InlineKeyboardButton(text="Закрыть", callback_data="adm:close")],
        ]
    )


def confirm_kb(yes_data: str, yes_text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=yes_text, callback_data=yes_data)],
            [InlineKeyboardButton(text="Отмена", callback_data="adm:menu")],
        ]
    )


def remove_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"Убрать: {p['name']}", callback_data=f"adm:del:{p['id']}"
            )
        ]
        for p in storage.get_people()
    ]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="adm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def helpers_kb() -> InlineKeyboardMarkup:
    ids = storage.get_editor_ids()
    rows = [
        [
            InlineKeyboardButton(
                text=("✅ " if p["tg_id"] in ids else "▫️ ") + p["name"],
                callback_data=f"adm:help:{p['id']}",
            )
        ]
        for p in storage.get_people()
    ]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="adm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


HELPERS_TEXT = (
    "Помощники могут нажимать на любого человека и давать ему баллы.\n"
    "✅ доступ есть, ▫️ доступа нет. Нажми на имя, чтобы выдать или забрать."
)


@router.message(F.text == BTN_ADMIN)
async def btn_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return await deny(message)
    await message.answer("Меню админа:", reply_markup=admin_menu_kb())


@router.callback_query(F.data.startswith("adm:"))
async def on_admin(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return await call.answer("Это только для админа.", show_alert=True)

    parts = call.data.split(":")
    action = parts[1]

    if action == "close":
        await call.answer()
        return await call.message.delete()

    changed = False
    answered = False
    if action == "menu":
        text, kb = "Меню админа:", admin_menu_kb()
    elif action == "helpers":
        if not storage.get_people():
            return await call.answer("Список пустой.", show_alert=True)
        text, kb = HELPERS_TEXT, helpers_kb()
    elif action == "help":
        row = next((p for p in storage.get_people() if p["id"] == int(parts[2])), None)
        if not row:
            return await call.answer("Уже нет в списке.", show_alert=True)
        now = storage.toggle_editor(row["tg_id"])
        await call.answer("Доступ выдан." if now else "Доступ забран.")
        answered = True
        text, kb = HELPERS_TEXT, helpers_kb()
    elif action == "remove":
        if not storage.get_people():
            return await call.answer("Список пустой.", show_alert=True)
        text, kb = "Кого убрать из списка?", remove_kb()
    elif action == "del":
        removed = storage.remove_person_by_id(int(parts[2]))
        await call.answer("Убрал." if removed else "Уже нет в списке.")
        answered, changed = True, True
        if storage.get_people():
            text, kb = "Кого убрать из списка?", remove_kb()
        else:
            text, kb = "Список пустой.", admin_menu_kb()
    elif action == "reset":
        text, kb = (
            "Точно обнулить баллы у всех?",
            confirm_kb("adm:reset_yes", "Да, обнулить"),
        )
    elif action == "reset_yes":
        storage.reset_scores()
        await call.answer("Баллы обнулены.")
        answered, changed = True, True
        text, kb = "Все баллы обнулены.", admin_menu_kb()
    elif action == "clear":
        text, kb = (
            "Точно удалить весь список? Люди зарегистрируются заново.",
            confirm_kb("adm:clear_yes", "Да, удалить"),
        )
    elif action == "clear_yes":
        storage.clear_people()
        await call.answer("Список удален.")
        answered, changed = True, True
        text, kb = "Список удален.", admin_menu_kb()
    else:
        return await call.answer()

    if not answered:
        await call.answer()
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    if changed:
        await push_to_all(call.bot)


# ---------- команды админа (то же самое, но текстом) ----------

@router.message(Command("remove"))
async def cmd_remove(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        return await deny(message)
    name = " ".join((command.args or "").split())
    if not name:
        return await message.answer("Напиши имя:\n/remove Иван Иванов")
    if storage.remove_person(name):
        await message.answer(f"Убрал: {name}.")
        await push_to_all(message.bot)
    else:
        await message.answer("Такого человека нет в списке.")


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return await deny(message)
    storage.reset_scores()
    await message.answer("Все баллы обнулены.")
    await push_to_all(message.bot)


@router.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return await deny(message)
    storage.clear_people()
    await message.answer("Список удален. Люди регистрируются заново.")
    await push_to_all(message.bot)


# ---------- кнопки списка ----------

@router.callback_query(F.data == "refresh")
async def on_refresh(call: CallbackQuery) -> None:
    await call.answer("Список обновлен")
    storage.track_list(call.message.chat.id, call.message.message_id)
    await refresh_message(call)


@router.callback_query(F.data.startswith("hit:"))
async def on_hit(call: CallbackQuery) -> None:
    person_id = int(call.data.split(":", 1)[1])
    me = storage.get_person(call.from_user.id)
    uid = call.from_user.id
    if not (is_admin(uid) or storage.is_editor(uid) or (me and me["id"] == person_id)):
        await call.answer("Баллы можно давать только себе.", show_alert=True)
        return
    if storage.give_points(person_id, POINTS_PER_CLICK):
        await call.answer(f"+{fmt_score(POINTS_PER_CLICK)}")
    else:
        await call.answer("Этого человека уже нет в списке.", show_alert=True)
    here = (call.message.chat.id, call.message.message_id)
    storage.track_list(*here)
    await refresh_message(call)  # у нажавшего список меняется сразу
    await push_to_all(call.bot, skip=here)  # у остальных тихо, следом


# ---------- ответы на шаги регистрации (самый последний обработчик) ----------

@router.message(F.text, ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    if message.chat.type != "private":
        return
    uid = message.from_user.id
    state = storage.get_reg(uid)
    if not state:
        return  # человек не регистрируется, лишний текст игнорируем

    if state["step"] == "name":
        name = " ".join(message.text.split())
        if len(name.split()) < 2 or len(name) > 60:
            return await message.answer(
                "Нужны имя и фамилия, два слова. Например: Иван Иванов"
            )
        storage.set_reg(uid, "score", name)
        return await message.answer(
            "Теперь напиши свой нынешний балл.\nНапример: 0 или 4,5"
        )

    # шаг "score"
    try:
        score = float(message.text.strip().replace(",", "."))
        if not (0 <= score <= 100000):
            raise ValueError
    except ValueError:
        return await message.answer("Напиши число. Например: 0 или 4,5")

    name = state["name"]
    result = storage.register(uid, name, score, MAX_PEOPLE)
    storage.clear_reg(uid)

    if result == "full":
        return await message.answer(
            "Список полный. Напиши админу.", reply_markup=main_menu(uid)
        )
    if result == "exists":
        text = "Ты уже в списке."
    else:
        text = f"Готово! Ты в списке: {name}, баллов: {fmt_score(score)}."
    await message.answer(text, reply_markup=main_menu(uid))
    await send_list(message)
    if result == "ok":
        await push_to_all(message.bot)  # у остальных появится новый человек
