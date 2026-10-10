"""
Логика бота: регистрация, список, меню, админ-меню, тихое обновление.

Как работает бот:
1. Человек заходит в бота, пишет имя, фамилию и свой нынешний балл.
2. Баллы меняются кнопками внизу: добавить или отнять +-1,5 и нажать «Подтвердить».
3. Сверху те, у кого баллов меньше. Снизу те, у кого больше.
   Если баллы равны, выше стоит тот, кого нажимали раньше.
4. На каждой кнопке видно, когда человека нажимали в последний раз.
"""

import asyncio
import logging
import os
import random
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
BTN_TASK = "📝 Выбрать задачу"
BTN_NEXT = "🎯 Кто следующий"
BTN_ADD_ME = "➕ Добавить себе"
BTN_SUB_ME = "➖ Отнять у себя"
BTN_ADD_OTHER = "➕ Добавить другому"
BTN_SUB_OTHER = "➖ Отнять у другого"

router = Router()


# ---------- вид списка ----------

def fmt_score(score: float) -> str:
    return f"{score:g}".replace(".", ",")


def fmt_time(ts: float | None) -> str:
    """Всегда число, месяц и время: 07.10 14:05. Не нажимали: «—»."""
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


TASK_ICON = {"hard": "🔥", "easy": "🌱"}
TASK_NAME = {"hard": "сложная", "easy": "легкая"}


def build_keyboard() -> InlineKeyboardMarkup | None:
    people = storage.get_people()
    if not people:
        return None
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{TASK_ICON.get(p['task'], '')} ".lstrip()
                    + f"{p['name']} · {fmt_score(p['score'])} · "
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
    "Список людей с баллами.\n"
    "На кнопке: имя · баллы · когда меняли в последний раз.\n"
    "🔥 сложную задачу хочет, 🌱 легкую.\n"
    "Сверху те, у кого баллов меньше.\n"
    "Баллы меняются кнопками внизу экрана. Список обновляется сам."
)
EMPTY_TEXT = "Список пока пустой. Первым зарегистрируйся: нажми «Регистрация»."


# ---------- нижнее меню ----------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_menu(user_id: int) -> ReplyKeyboardMarkup:
    """Кнопки внизу экрана. Для незарегистрированных они другие."""
    if storage.get_person(user_id):
        rows = [
            [KeyboardButton(text=BTN_ADD_ME), KeyboardButton(text=BTN_SUB_ME)],
        ]
        if is_admin(user_id) or storage.is_editor(user_id):
            rows.append(
                [KeyboardButton(text=BTN_ADD_OTHER), KeyboardButton(text=BTN_SUB_OTHER)]
            )
        rows += [
            [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_ME)],
            [KeyboardButton(text=BTN_TASK), KeyboardButton(text=BTN_NEXT)],
            [KeyboardButton(text=BTN_HELP)],
        ]
    else:
        rows = [
            [KeyboardButton(text=BTN_REGISTER)],
            [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_NEXT)],
            [KeyboardButton(text=BTN_HELP)],
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
        f"{BTN_ADD_ME} и {BTN_SUB_ME}: "
        f"{fmt_score(POINTS_PER_CLICK)} балла за раз, потом нажми «Подтвердить».\n"
        "Чужие баллы меняют только админ и помощники.\n\n"
        "Кнопки внизу:\n"
        f"{BTN_LIST} — показать список\n"
        f"{BTN_ME} — твое место и баллы\n"
        f"{BTN_TASK} — выбрать, какую задачу хочешь: сложную 🔥 или легкую 🌱 "
        "(значок виден всем в списке)\n"
        f"{BTN_NEXT} — кто следующий к доске (у кого меньше всего баллов, "
        "при равных выбирается случайно). Если человек не хочет, нажми «Не хочет выходить»\n"
        f"{BTN_HELP} — эта подсказка\n\n"
        "Команды: /start, /list, /me, /id"
    )
    if is_admin(user_id):
        text += (
            f"\n\nДля админа есть кнопка «{BTN_ADMIN}»: исправить баллы человеку, "
            "выдать помощникам доступ к чужим баллам, убрать человека, обнулить баллы, удалить список.\n"
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
            [InlineKeyboardButton(text="Исправить баллы человеку", callback_data="adm:fix")],
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


def fix_kb() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{p['name']} · {fmt_score(p['score'])}",
                callback_data=f"adm:fixp:{p['id']}",
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
    elif action == "fix":
        if not storage.get_people():
            return await call.answer("Список пустой.", show_alert=True)
        storage.clear_reg(call.from_user.id)
        text, kb = "Кому исправить баллы?", fix_kb()
    elif action == "fixp":
        person = storage.get_person_by_id(int(parts[2]))
        if not person:
            return await call.answer("Уже нет в списке.", show_alert=True)
        storage.set_reg(call.from_user.id, "fix", str(person["id"]))
        text = (
            f"{person['name']}: сейчас {fmt_score(person['score'])} б.\n"
            "Напиши правильный балл сообщением. Например: 5,5"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Отмена", callback_data="adm:fixcancel")]
            ]
        )
    elif action == "fixcancel":
        storage.clear_reg(call.from_user.id)
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
        try:
            await call.bot.send_message(
                row["tg_id"],
                "Тебе выдали доступ: теперь ты можешь менять баллы другим."
                if now
                else "Доступ менять баллы другим забрали.",
                reply_markup=main_menu(row["tg_id"]),
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
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


def can_change(uid: int, person_id: int) -> bool:
    """Себе может каждый. Другим только админ и помощники."""
    if is_admin(uid) or storage.is_editor(uid):
        return True
    me = storage.get_person(uid)
    return bool(me and me["id"] == person_id)


STEP = fmt_score(POINTS_PER_CLICK)


def sign_word(mode: str) -> tuple[str, str]:
    """(знак, глагол) для режима p (добавить) или m (отнять)."""
    return ("+", "Добавить") if mode == "p" else ("−", "Отнять")


def points_confirm_kb(person_id: int, mode: str, back: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"cf:{person_id}:{mode}")]]
    tail = [InlineKeyboardButton(text="Отмена", callback_data="cx")]
    if back:
        tail.insert(0, InlineKeyboardButton(text="Назад", callback_data=f"pick:{mode}"))
    rows.append(tail)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pick_kb(mode: str, exclude_tg_id: int) -> InlineKeyboardMarkup:
    rows = []
    for p in storage.get_people():
        if p["tg_id"] == exclude_tg_id:
            continue
        if mode == "m" and p["score"] < POINTS_PER_CLICK:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{p['name']} · {fmt_score(p['score'])}",
                    callback_data=f"sel:{p['id']}:{mode}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cx")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def ask_for_self(message: Message, mode: str) -> None:
    me = storage.get_person(message.from_user.id)
    if not me:
        return await message.answer("Сначала зарегистрируйся: нажми «Регистрация».")
    if mode == "m" and me["score"] < POINTS_PER_CLICK:
        return await message.answer(f"У тебя {fmt_score(me['score'])} б. Отнимать нечего.")
    sign, verb = sign_word(mode)
    await message.answer(
        f"{verb} себе {sign}{STEP} балла?\nСейчас у тебя {fmt_score(me['score'])} б.",
        reply_markup=points_confirm_kb(me["id"], mode),
    )


async def ask_for_other(message: Message, mode: str) -> None:
    uid = message.from_user.id
    if not (is_admin(uid) or storage.is_editor(uid)):
        return await message.answer("Это могут только админ и помощники.")
    kb = pick_kb(mode, uid)
    if len(kb.inline_keyboard) == 1:
        return await message.answer("Некому менять баллы.")
    sign, verb = sign_word(mode)
    await message.answer(f"{verb} {sign}{STEP}. Выбери человека:", reply_markup=kb)


def pick_next() -> tuple[dict, int, bool] | None:
    """Кто выходит к доске. Возвращает (человек, сколько с равными баллами, все ли отказались)."""
    people = storage.get_people()
    if not people:
        return None
    skipped = storage.get_skipped()
    pool = [p for p in people if p["id"] not in skipped]
    everyone_refused = False
    if not pool:  # отказались все: начинаем круг заново
        storage.clear_skips()
        pool = people
        everyone_refused = True
    lowest = min(p["score"] for p in pool)
    tied = [p for p in pool if p["score"] == lowest]
    # если уже назвали кого-то из них и он все еще подходит, не меняем
    saved = storage.get_next_pick()
    chosen = next((p for p in tied if p["id"] == saved), None)
    if chosen is None:
        chosen = random.choice(tied)
        storage.set_next_pick(chosen["id"])
    return chosen, len(tied), everyone_refused


def next_message() -> tuple[str, InlineKeyboardMarkup | None]:
    picked = pick_next()
    if picked is None:
        return EMPTY_TEXT, None
    chosen, tied_count, refused_all = picked
    text = f"К доске выходит: {chosen['name']}\nБаллов: {fmt_score(chosen['score'])}"
    if chosen.get("task"):
        text += f"\nХочет задачу: {TASK_NAME[chosen['task']]} {TASK_ICON[chosen['task']]}"
    if tied_count > 1:
        text += f"\nУ {tied_count} человек поровну баллов, выбрал случайно."
    if refused_all:
        text += "\nОтказались все, начинаю круг заново."
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🙅 Не хочет выходить, выбрать другого",
                    callback_data=f"nskip:{chosen['id']}",
                )
            ]
        ]
    )
    return text, kb


def task_kb(current: str | None) -> InlineKeyboardMarkup:
    def mark(code: str, label: str) -> str:
        return ("✓ " if current == code else "") + label

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=mark("hard", "🔥 Сложная"), callback_data="task:hard"),
                InlineKeyboardButton(text=mark("easy", "🌱 Легкая"), callback_data="task:easy"),
            ],
            [InlineKeyboardButton(text="Убрать выбор", callback_data="task:none")],
        ]
    )


@router.message(F.text == BTN_TASK)
async def btn_task(message: Message) -> None:
    me = storage.get_person(message.from_user.id)
    if not me:
        return await message.answer("Сначала зарегистрируйся: нажми «Регистрация».")
    now = f"Сейчас: {TASK_NAME[me['task']]}." if me.get("task") else "Сейчас не выбрано."
    await message.answer(
        f"Какую задачу хочешь? {now}", reply_markup=task_kb(me.get("task"))
    )


@router.callback_query(F.data.startswith("task:"))
async def on_task(call: CallbackQuery) -> None:
    code = call.data.split(":", 1)[1]
    task = code if code in TASK_NAME else None
    if not storage.set_task(call.from_user.id, task):
        return await call.answer("Сначала зарегистрируйся.", show_alert=True)
    await call.answer("Записал: " + (TASK_NAME[task] if task else "выбор убран"))
    now = f"Сейчас: {TASK_NAME[task]}." if task else "Сейчас не выбрано."
    await edit_or_pass(call, f"Какую задачу хочешь? {now}", task_kb(task))
    await push_to_all(call.bot)  # у всех в списке появится значок


@router.message(F.text == BTN_NEXT)
async def btn_next(message: Message) -> None:
    text, kb = next_message()
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("nskip:"))
async def on_next_skip(call: CallbackQuery) -> None:
    person_id = int(call.data.split(":", 1)[1])
    if not can_change(call.from_user.id, person_id):
        return await call.answer(
            "Отказаться может сам человек, админ или помощник.", show_alert=True
        )
    if storage.get_next_pick() == person_id:  # кнопка не устарела
        storage.add_skip(person_id)
        storage.clear_next_pick()
        await call.answer("Хорошо, выбираю другого.")
    else:
        await call.answer("Уже выбран другой.")
    text, kb = next_message()
    await edit_or_pass(call, text, kb)


@router.message(F.text == BTN_ADD_ME)
async def btn_add_me(message: Message) -> None:
    await ask_for_self(message, "p")


@router.message(F.text == BTN_SUB_ME)
async def btn_sub_me(message: Message) -> None:
    await ask_for_self(message, "m")


@router.message(F.text == BTN_ADD_OTHER)
async def btn_add_other(message: Message) -> None:
    await ask_for_other(message, "p")


@router.message(F.text == BTN_SUB_OTHER)
async def btn_sub_other(message: Message) -> None:
    await ask_for_other(message, "m")


@router.callback_query(F.data.startswith("hit:"))
async def on_hit(call: CallbackQuery) -> None:
    """Нажатие на строку списка: просто показываем, чьи это баллы."""
    p = storage.get_person_by_id(int(call.data.split(":", 1)[1]))
    if not p:
        return await call.answer("Этого человека уже нет в списке.", show_alert=True)
    await call.answer(f"{p['name']}: {fmt_score(p['score'])} б.")


async def edit_or_pass(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None) -> None:
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.callback_query(F.data == "cx")
async def on_cancel(call: CallbackQuery) -> None:
    await call.answer("Отменено.")
    if is_admin(call.from_user.id):
        storage.clear_reg(call.from_user.id)
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("pick:"))
async def on_pick(call: CallbackQuery) -> None:
    """Назад к списку людей."""
    uid = call.from_user.id
    if not (is_admin(uid) or storage.is_editor(uid)):
        return await call.answer("Это могут только админ и помощники.", show_alert=True)
    mode = call.data.split(":")[1]
    sign, verb = sign_word(mode)
    await call.answer()
    await edit_or_pass(call, f"{verb} {sign}{STEP}. Выбери человека:", pick_kb(mode, uid))


@router.callback_query(F.data.startswith("sel:"))
async def on_select(call: CallbackQuery) -> None:
    """Выбрали человека: показываем кнопку «Подтвердить»."""
    _, pid, mode = call.data.split(":")
    person_id = int(pid)
    if not can_change(call.from_user.id, person_id):
        return await call.answer("Баллы можно менять только себе.", show_alert=True)
    person = storage.get_person_by_id(person_id)
    if not person:
        return await call.answer("Этого человека уже нет в списке.", show_alert=True)
    sign, verb = sign_word(mode)
    await call.answer()
    await edit_or_pass(
        call,
        f"{verb} {sign}{STEP} балла: {person['name']}?\n"
        f"Сейчас {fmt_score(person['score'])} б.",
        points_confirm_kb(person_id, mode, back=True),
    )


@router.callback_query(F.data.startswith("cf:"))
async def on_confirm(call: CallbackQuery) -> None:
    _, pid, mode = call.data.split(":")
    person_id = int(pid)

    async def close_prompt() -> None:
        try:
            await call.message.delete()
        except TelegramBadRequest:
            pass

    if not can_change(call.from_user.id, person_id):
        await call.answer("Баллы можно менять только себе.", show_alert=True)
        return await close_prompt()

    delta = POINTS_PER_CLICK if mode == "p" else -POINTS_PER_CLICK
    result = storage.change_points(person_id, delta)
    if result == "low":
        await call.answer("Баллов меньше, чем нужно отнять.", show_alert=True)
        return await close_prompt()
    if result == "gone":
        await call.answer("Этого человека уже нет в списке.", show_alert=True)
        return await close_prompt()
    sign, _ = sign_word(mode)
    await call.answer(f"{sign}{STEP}")
    await close_prompt()
    await push_to_all(call.bot)  # список меняется у всех, в том числе у тебя


@router.callback_query(F.data.startswith("fixcf:"))
async def on_fix_confirm(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return await call.answer("Это только для админа.", show_alert=True)
    _, pid, value = call.data.split(":")
    storage.clear_reg(call.from_user.id)
    ok = storage.set_score(int(pid), float(value))
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    if not ok:
        return await call.answer("Этого человека уже нет в списке.", show_alert=True)
    await call.answer(f"Баллы исправлены: {fmt_score(float(value))}")
    await push_to_all(call.bot)


# ---------- ответы на шаги регистрации (самый последний обработчик) ----------

@router.message(F.text, ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    if message.chat.type != "private":
        return
    uid = message.from_user.id
    state = storage.get_reg(uid)
    if not state:
        return  # человек не регистрируется, лишний текст игнорируем

    if state["step"] == "fix":
        if not is_admin(uid):
            storage.clear_reg(uid)
            return
        person = storage.get_person_by_id(int(state["name"]))
        if not person:
            storage.clear_reg(uid)
            return await message.answer("Этого человека уже нет в списке.")
        try:
            value = float(message.text.strip().replace(",", "."))
            if not (0 <= value <= 100000):
                raise ValueError
        except ValueError:
            return await message.answer("Напиши число. Например: 5,5")
        return await message.answer(
            f"Поставить {person['name']} балл {fmt_score(value)}?\n"
            f"Сейчас {fmt_score(person['score'])} б.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Подтвердить",
                            callback_data=f"fixcf:{person['id']}:{value:g}",
                        )
                    ],
                    [InlineKeyboardButton(text="Отмена", callback_data="cx")],
                ]
            ),
        )

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
