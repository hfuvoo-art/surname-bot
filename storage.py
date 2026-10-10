"""
Хранение данных в Postgres (бесплатная база Neon).

На Vercel файлы не сохраняются, поэтому баллы, регистрация и список
сообщений лежат во внешней базе. Адрес базы берется из переменной
DATABASE_URL (ее добавляет Neon, когда подключаешь его к проекту).
"""

import os
import time

import psycopg
from psycopg.rows import dict_row

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS people (
        id         BIGSERIAL PRIMARY KEY,
        tg_id      BIGINT NOT NULL UNIQUE,
        name       TEXT NOT NULL,
        score      DOUBLE PRECISION NOT NULL DEFAULT 0,
        sort_key   DOUBLE PRECISION NOT NULL,
        clicked_at DOUBLE PRECISION
    )
    """,
    # Сообщения со списком, которые нужно тихо обновлять у всех
    """
    CREATE TABLE IF NOT EXISTS list_messages (
        chat_id    BIGINT NOT NULL,
        message_id BIGINT NOT NULL,
        PRIMARY KEY (chat_id, message_id)
    )
    """,
    # Шаг регистрации: на Vercel программа не помнит прошлый запрос,
    # поэтому "я жду от тебя имя" или "я жду балл" хранится здесь
    """
    CREATE TABLE IF NOT EXISTS reg_state (
        tg_id BIGINT PRIMARY KEY,
        step  TEXT NOT NULL,
        name  TEXT
    )
    """,
    # Кого бот назвал «следующим к доске»: запоминаем, пока он остается среди самых малобалльных
    """
    CREATE TABLE IF NOT EXISTS next_pick (
        slot      INT PRIMARY KEY,
        person_id BIGINT NOT NULL
    )
    """,
    # Помощники: люди, которым админ разрешил менять баллы другим
    """
    CREATE TABLE IF NOT EXISTS editors (
        tg_id BIGINT PRIMARY KEY
    )
    """,
]

_conn: psycopg.Connection | None = None
_schema_ready = False


def _database_url() -> str:
    url = (
        os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URL")
        or os.getenv("DATABASE_URL_UNPOOLED")
    )
    if not url:
        raise RuntimeError(
            "Не задана переменная DATABASE_URL. "
            "Подключи базу Neon к проекту на Vercel."
        )
    return url


def _get_conn() -> psycopg.Connection:
    """Одно соединение на весь запуск. Если оно оборвалось, откроем новое."""
    global _conn, _schema_ready
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(
            _database_url(),
            autocommit=True,
            row_factory=dict_row,
            # Neon отдает адрес через пулер, ему не нравятся prepared statements
            prepare_threshold=None,
            connect_timeout=10,
        )
        if not _schema_ready:
            for stmt in SCHEMA:
                try:
                    _conn.execute(stmt)
                except (
                    psycopg.errors.DuplicateTable,
                    psycopg.errors.UniqueViolation,
                ):
                    pass  # другой запуск успел создать таблицу первым
            _schema_ready = True
    return _conn


def _exec(sql: str, params: tuple = (), fetch: str | None = None):
    """fetch: None (вернет число строк), 'one' или 'all'."""
    global _conn
    last_error: Exception | None = None
    for _ in range(2):  # Neon засыпает через 5 минут: переподключаемся
        try:
            cur = _get_conn().execute(sql, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return cur.rowcount
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            last_error = e
            try:
                if _conn is not None:
                    _conn.close()
            except Exception:
                pass
            _conn = None
    raise last_error  # type: ignore[misc]


# ---------- люди ----------

def get_person(tg_id: int):
    return _exec("SELECT * FROM people WHERE tg_id = %s", (tg_id,), "one")


def get_people() -> list[dict]:
    return _exec(
        "SELECT id, tg_id, name, score, clicked_at FROM people "
        "ORDER BY score ASC, sort_key ASC, id ASC",
        fetch="all",
    )


def register(tg_id: int, name: str, score: float, max_people: int) -> str:
    """Возвращает: ok, exists или full."""
    row = _exec(
        """
        INSERT INTO people (tg_id, name, score, sort_key)
        SELECT %s::bigint, %s::text, %s::double precision, %s::double precision
        WHERE (SELECT COUNT(*) FROM people) < %s::bigint
        ON CONFLICT (tg_id) DO NOTHING
        RETURNING id
        """,
        (tg_id, name, score, time.time(), max_people),
        "one",
    )
    if row:
        return "ok"
    return "exists" if get_person(tg_id) else "full"


def give_points(person_id: int, points: float) -> bool:
    now = time.time()
    return (
        _exec(
            "UPDATE people SET score = score + %s, sort_key = %s, "
            "clicked_at = %s WHERE id = %s",
            (points, now, now, person_id),
        )
        > 0
    )


def change_points(person_id: int, delta: float) -> str:
    """Меняет баллы на delta. Возвращает ok, low (ушли бы ниже нуля) или gone."""
    now = time.time()
    n = _exec(
        "UPDATE people SET score = score + %s, sort_key = %s, clicked_at = %s "
        "WHERE id = %s AND score + %s >= 0",
        (delta, now, now, person_id, delta),
    )
    if n > 0:
        return "ok"
    return "low" if get_person_by_id(person_id) else "gone"


def set_score(person_id: int, score: float) -> bool:
    now = time.time()
    return (
        _exec(
            "UPDATE people SET score = %s, sort_key = %s, clicked_at = %s WHERE id = %s",
            (score, now, now, person_id),
        )
        > 0
    )


def get_person_by_id(person_id: int):
    return _exec("SELECT * FROM people WHERE id = %s", (person_id,), "one")


def remove_person(name: str) -> bool:
    # Сравниваем в Python, чтобы регистр букв не мешал
    for row in _exec("SELECT id, name FROM people", fetch="all"):
        if row["name"].casefold() == name.casefold():
            return remove_person_by_id(row["id"])
    return False


def remove_person_by_id(person_id: int) -> bool:
    return _exec("DELETE FROM people WHERE id = %s", (person_id,)) > 0


def reset_scores() -> None:
    clear_next_pick()
    _exec(
        "UPDATE people SET score = 0, clicked_at = NULL, "
        "sort_key = %s::double precision + id / 1000.0",
        (time.time(),),
    )


def is_editor(tg_id: int) -> bool:
    return _exec("SELECT 1 AS x FROM editors WHERE tg_id = %s", (tg_id,), "one") is not None


def toggle_editor(tg_id: int) -> bool:
    """Выдать доступ или забрать. Возвращает True, если теперь доступ есть."""
    if is_editor(tg_id):
        _exec("DELETE FROM editors WHERE tg_id = %s", (tg_id,))
        return False
    _exec("INSERT INTO editors (tg_id) VALUES (%s) ON CONFLICT DO NOTHING", (tg_id,))
    return True


def get_editor_ids() -> set[int]:
    rows = _exec("SELECT tg_id FROM editors", fetch="all")
    return {r["tg_id"] for r in rows}


def get_next_pick() -> int | None:
    row = _exec("SELECT person_id FROM next_pick WHERE slot = 1", fetch="one")
    return row["person_id"] if row else None


def set_next_pick(person_id: int) -> None:
    _exec(
        "INSERT INTO next_pick (slot, person_id) VALUES (1, %s) "
        "ON CONFLICT (slot) DO UPDATE SET person_id = EXCLUDED.person_id",
        (person_id,),
    )


def clear_next_pick() -> None:
    _exec("DELETE FROM next_pick")


def clear_people() -> None:
    clear_next_pick()
    _exec("DELETE FROM people")


# ---------- сообщения со списком ----------

def track_list(chat_id: int, message_id: int) -> None:
    """Запоминаем сообщение со списком. На один чат храним последние 3."""
    _exec(
        "INSERT INTO list_messages (chat_id, message_id) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING",
        (chat_id, message_id),
    )
    _exec(
        "DELETE FROM list_messages WHERE chat_id = %s AND message_id NOT IN "
        "(SELECT message_id FROM list_messages WHERE chat_id = %s "
        "ORDER BY message_id DESC LIMIT 3)",
        (chat_id, chat_id),
    )


def forget_list(chat_id: int, message_id: int) -> None:
    _exec(
        "DELETE FROM list_messages WHERE chat_id = %s AND message_id = %s",
        (chat_id, message_id),
    )


def get_tracked() -> list[dict]:
    return _exec("SELECT chat_id, message_id FROM list_messages", fetch="all")


# ---------- шаги регистрации ----------

def set_reg(tg_id: int, step: str, name: str | None = None) -> None:
    _exec(
        "INSERT INTO reg_state (tg_id, step, name) VALUES (%s, %s, %s) "
        "ON CONFLICT (tg_id) DO UPDATE SET step = EXCLUDED.step, "
        "name = EXCLUDED.name",
        (tg_id, step, name),
    )


def get_reg(tg_id: int):
    return _exec("SELECT step, name FROM reg_state WHERE tg_id = %s", (tg_id,), "one")


def clear_reg(tg_id: int) -> None:
    _exec("DELETE FROM reg_state WHERE tg_id = %s", (tg_id,))
