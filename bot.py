import asyncio
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date, time, timedelta, timezone

import pytz
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv

# ---------- Setup ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DATABASE_PATH", "bot.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(BOT_TOKEN, parse_mode=ParseMode.MARKDOWN)
dp = Dispatcher()
router = Router()
scheduler = AsyncIOScheduler(timezone="UTC")

# ---------- DB helpers ----------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.commit()
        conn.close()

def init_db():
    with db() as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS birthdays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            username TEXT,
            date TEXT NOT NULL, -- YYYY-MM-DD
            remind_days_before INTEGER, -- NULL if not needed
            remind_on_day INTEGER NOT NULL DEFAULT 1, -- 0/1
            custom_message TEXT,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            default_message TEXT NOT NULL DEFAULT 'У {name} сегодня день рождения! 🎉'
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            birthday_id INTEGER NOT NULL,
            run_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            UNIQUE(birthday_id, kind, run_at)
        );
        """)
        conn.commit()

def get_chat_settings(chat_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM chat_settings WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        if not row:
            c.execute("INSERT INTO chat_settings(chat_id) VALUES(?)", (chat_id,))
            conn.commit()
            return {"chat_id": chat_id, "timezone": "UTC", "default_message": "У {name} сегодня день рождения! 🎉"}
        return dict(row)

def set_chat_timezone(chat_id: int, tz: str):
    with db() as conn:
        c = conn.cursor()
        c.execute("""
        INSERT INTO chat_settings(chat_id, timezone) VALUES(?,?)
        ON CONFLICT(chat_id) DO UPDATE SET timezone=excluded.timezone
        """, (chat_id, tz))

def set_chat_default_message(chat_id: int, text: str):
    with db() as conn:
        c = conn.cursor()
        c.execute("""
        INSERT INTO chat_settings(chat_id, default_message) VALUES(?,?)
        ON CONFLICT(chat_id) DO UPDATE SET default_message=excluded.default_message
        """, (chat_id, text))

def add_birthday(chat_id: int, name: str, username: str | None, d: date,
                 remind_days_before: int | None, remind_on_day: bool,
                 custom_message: str | None, tz: str, created_by: int) -> int:
    with db() as conn:
        c = conn.cursor()
        c.execute("""
        INSERT INTO birthdays(chat_id, name, username, date, remind_days_before, remind_on_day, custom_message, timezone, created_by, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (
            chat_id, name, username, d.strftime("%Y-%m-%d"),
            remind_days_before, 1 if remind_on_day else 0,
            custom_message, tz, created_by, datetime.utcnow().isoformat()
        ))
        return c.lastrowid

def list_birthdays(chat_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute("""
        SELECT * FROM birthdays WHERE chat_id=? ORDER BY date(name), name
        """, (chat_id,))
        return [dict(r) for r in c.fetchall()]

def get_birthday(birthday_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM birthdays WHERE id=?", (birthday_id,))
        row = c.fetchone()
        return dict(row) if row else None

def delete_birthday(birthday_id: int, chat_id: int) -> bool:
    with db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM birthdays WHERE id=? AND chat_id=?", (birthday_id, chat_id))
        return c.rowcount > 0

def add_job(job_id: str, birthday_id: int, run_at: datetime, kind: str):
    with db() as conn:
        c = conn.cursor()
        c.execute("""
        INSERT OR IGNORE INTO jobs(id, birthday_id, run_at, kind) VALUES(?,?,?,?)
        """, (job_id, birthday_id, run_at.replace(tzinfo=timezone.utc).isoformat(), kind))

def list_jobs_for_birthday(birthday_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM jobs WHERE birthday_id=?", (birthday_id,))
        return [dict(r) for r in c.fetchall()]

def remove_jobs_for_birthday(birthday_id: int):
    with db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM jobs WHERE birthday_id=?", (birthday_id,))

def list_all_jobs():
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM jobs")
        return [dict(r) for r in c.fetchall()]

# ---------- Time helpers ----------
def parse_date(s: str) -> date:
    # supports DD.MM or DD.MM.YYYY
    s = s.strip()
    if len(s.split(".")) == 2:
        d = datetime.strptime(s, "%d.%m")
        # store with 1900 as year, we will normalize to next occurrence ignoring year
        return date(1900, d.month, d.day)
    else:
        d = datetime.strptime(s, "%d.%m.%Y")
        return d.date()

def next_occurrence(bday: date, tz: pytz.BaseTzInfo) -> datetime:
    # bday.year may be 1900 => recurring annual event
    now = datetime.now(tz)
    year = now.year
    try_date = date(year, bday.month, bday.day)
    if try_date < now.date():
        try_date = date(year + 1, bday.month, bday.day)
    # Trigger at 09:00 local time by default for "on day"
    dt_local = datetime.combine(try_date, time(9, 0))
    return tz.localize(dt_local)

def specific_time_today_or_next(dt_local: datetime, tz: pytz.BaseTzInfo) -> datetime:
    now = datetime.now(tz)
    if dt_local <= now:
        return dt_local + timedelta(days=1)
    return dt_local

def to_utc(dt_local: datetime) -> datetime:
    if dt_local.tzinfo is None:
        raise ValueError("Expected tz-aware datetime")
    return dt_local.astimezone(pytz.utc)

# ---------- Scheduler logic ----------
async def send_birthday_message(birthday_id: int, kind: str):
    b = get_birthday(birthday_id)
    if not b:
        return
    chat_id = b["chat_id"]
    settings = get_chat_settings(chat_id)
    name = b["name"]
    username = b["username"]
    # Choose message
    template = b["custom_message"] or settings["default_message"]
    text = template.replace("{name}", name)

    # Append @username if provided
    if username:
        text += f" (@" + username.strip("@") + ")"

    try:
        await bot.send_message(chat_id, text)
    except Exception as e:
        logger.error(f"Failed to send message to {chat_id}: {e}")

def schedule_for_birthday(birthday_row: dict):
    # Clear existing in-memory jobs for this birthday (we keep DB jobs to avoid dupes)
    # Here we rely on unique job_id, so duplicates won't be scheduled twice.
    chat_id = birthday_row["chat_id"]
    tzname = birthday_row["timezone"] or get_chat_settings(chat_id)["timezone"]
    tz = pytz.timezone(tzname)

    # When is the next birthday day occurrence?
    bdate = datetime.strptime(birthday_row["date"], "%Y-%m-%d").date()
    on_day_local = next_occurrence(bdate, tz)  # 09:00 local
    # on-day schedule
    if birthday_row["remind_on_day"]:
        run_at_utc = to_utc(on_day_local)
        job_id = f"bday:{birthday_row['id']}:day:{run_at_utc.isoformat()}"
        add_job(job_id, birthday_row["id"], run_at_utc, "day")
        if not scheduler.get_job(job_id):
            scheduler.add_job(send_birthday_message, DateTrigger(run_date=run_at_utc),
                              kwargs={"birthday_id": birthday_row["id"], "kind": "day"},
                              id=job_id, replace_existing=False)

    # days-before schedule
    if birthday_row["remind_days_before"] is not None:
        days = int(birthday_row["remind_days_before"])
        before_local = on_day_local - timedelta(days=days)
        # Напоминать также в 09:00 локального времени
        run_at_utc = to_utc(before_local)
        job_id = f"bday:{birthday_row['id']}:before:{run_at_utc.isoformat()}"
        add_job(job_id, birthday_row["id"], run_at_utc, "before")
        if not scheduler.get_job(job_id):
            scheduler.add_job(send_birthday_message, DateTrigger(run_date=run_at_utc),
                              kwargs={"birthday_id": birthday_row["id"], "kind": "before"},
                              id=job_id, replace_existing=False)

def reschedule_all_from_db():
    # Rehydrate scheduler on startup
    jobs = list_all_jobs()
    for j in jobs:
        run_at = datetime.fromisoformat(j["run_at"])
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        # Skip past jobs
        if run_at < datetime.now(timezone.utc):
            continue
        b = get_birthday(j["birthday_id"])
        if not b:
            continue
        if not scheduler.get_job(j["id"]):
            scheduler.add_job(send_birthday_message, DateTrigger(run_date=run_at),
                              kwargs={"birthday_id": j["birthday_id"], "kind": j["kind"]},
                              id=j["id"], replace_existing=False)

# ---------- FSM States ----------
class AddStates(StatesGroup):
    name = State()
    username = State()
    date = State()
    remind_choice = State()
    days_before = State()
    custom_message = State()

class DeleteStates(StatesGroup):
    choose_id = State()

class TimezoneStates(StatesGroup):
    tz = State()

class DefaultMsgStates(StatesGroup):
    text = State()

# ---------- Keyboards ----------
def yes_no_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Да"), KeyboardButton(text="Нет")]],
                               resize_keyboard=True, one_time_keyboard=True)

def remind_options_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="В день")],
            [KeyboardButton(text="За N дней")],
            [KeyboardButton(text="И то, и то")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

# ---------- Handlers ----------
@router.message(Command("start"))
async def cmd_start(message: Message):
    get_chat_settings(message.chat.id)
    text = (
        "Привет! Я напомню о днях рождения.\n\n"
        "Команды:\n"
        "/add — добавить день рождения\n"
        "/list — список сохранённых ДР\n"
        "/delete — удалить запись ДР\n"
        "/set_timezone — установить часовой пояс (например, Europe/Moscow)\n"
        "/set_default_message — задать дефолтный текст уведомлений для этого чата\n\n"
        "Поддерживаю плейсхолдер {name} в тексте уведомлений."
    )
    await message.answer(text)

@router.message(Command("set_timezone"))
async def cmd_set_timezone(message: Message, state: FSMContext):
    await state.set_state(TimezoneStates.tz)
    await message.answer("Укажи часовой пояс, например: Europe/Moscow\nСписок доступных: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones")

@router.message(TimezoneStates.tz)
async def set_timezone_value(message: Message, state: FSMContext):
    tz = message.text.strip()
    if tz not in pytz.all_timezones:
        await message.answer("Некорректный часовой пояс. Пример: Europe/Moscow")
        return
    set_chat_timezone(message.chat.id, tz)
    await state.clear()
    await message.answer(f"Часовой пояс для этого чата установлен: {tz}")

@router.message(Command("set_default_message"))
async def cmd_set_def_msg(message: Message, state: FSMContext):
    await state.set_state(DefaultMsgStates.text)
    await message.answer("Введи дефолтный текст уведомления для этого чата.\nИспользуй {name} для подстановки имени.")

@router.message(DefaultMsgStates.text)
async def set_def_msg_value(message: Message, state: FSMContext):
    text = message.text.strip()
    if "{name}" not in text:
        await message.answer("В тексте должен быть плейсхолдер {name}. Попробуй ещё раз.")
        return
    set_chat_default_message(message.chat.id, text)
    await state.clear()
    await message.answer("Дефолтный текст сохранён.")

@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    await state.set_state(AddStates.name)
    await message.answer("Введи имя человека для поздравления (как будет отображаться).")

@router.message(AddStates.name)
async def add_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AddStates.username)
    await message.answer("Укажи @username (если есть) или напиши 'нет'.")

@router.message(AddStates.username)
async def add_username(message: Message, state: FSMContext):
    u = message.text.strip()
    if u.lower() == "нет":
        u = None
    else:
        u = u if u.startswith("@") else f"@{u}"
    await state.update_data(username=u)
    await state.set_state(AddStates.date)
    await message.answer("Укажи дату ДР в формате DD.MM или DD.MM.YYYY")

@router.message(AddStates.date)
async def add_date(message: Message, state: FSMContext):
    try:
        d = parse_date(message.text)
    except Exception:
        await message.answer("Неверный формат. Используй DD.MM или DD.MM.YYYY")
        return
    await state.update_data(date=d.isoformat())
    await state.set_state(AddStates.remind_choice)
    await message.answer("Когда напоминать?", reply_markup=remind_options_kb())

@router.message(AddStates.remind_choice, F.text.in_({"В день", "За N дней", "И то, и то"}))
async def add_remind_choice(message: Message, state: FSMContext):
    choice = message.text.strip()
    await state.update_data(remind_choice=choice)
    if choice == "За N дней" or choice == "И то, и то":
        await state.set_state(AddStates.days_before)
        await message.answer("За сколько дней напоминать? Введи число (например, 3).")
    else:
        await state.set_state(AddStates.custom_message)
        await message.answer("Введи кастомный текст уведомления или напиши 'по умолчанию'.")

@router.message(AddStates.days_before)
async def add_days_before(message: Message, state: FSMContext):
    try:
        n = int(message.text.strip())
        if n < 0 or n > 365:
            raise ValueError
    except Exception:
        await message.answer("Введи целое число от 0 до 365.")
        return
    await state.update_data(days_before=n)
    await state.set_state(AddStates.custom_message)
    await message.answer("Введи кастомный текст уведомления или напиши 'по умолчанию'.")

@router.message(AddStates.custom_message)
async def add_custom_msg(message: Message, state: FSMContext):
    txt = message.text.strip()
    custom = None if txt.lower() == "по умолчанию" else txt
    data = await state.get_data()

    name = data["name"]
    username = data["username"]
    d = datetime.fromisoformat(data["date"]).date()
    choice = data["remind_choice"]
    days_before = data.get("days_before", None)
    remind_on_day = choice in ("В день", "И то, и то")
    remind_days = days_before if choice in ("За N дней", "И то, и то") else None

    settings = get_chat_settings(message.chat.id)
    tzname = settings["timezone"]
    b_id = add_birthday(
        chat_id=message.chat.id,
        name=name,
        username=(username[1:] if username else None),
        d=d,
        remind_days_before=remind_days,
        remind_on_day=remind_on_day,
        custom_message=custom,
        tz=tzname,
        created_by=message.from_user.id
    )

    # Schedule jobs
    row = get_birthday(b_id)
    schedule_for_birthday(row)

    await state.clear()
    parts = [f"Добавлено: {name}, дата: {d.strftime('%d.%m')}",
             f"Напоминание: {'в день' if remind_on_day else ''} {'и ' if remind_on_day and remind_days is not None else ''}{f'за {remind_days} дн.' if remind_days is not None else ''}"]
    if custom:
        parts.append("Кастомный текст сохранён.")
    await message.answer("\n".join(parts))

@router.message(Command("list"))
async def cmd_list(message: Message):
    rows = list_birthdays(message.chat.id)
    if not rows:
        await message.answer("Записей не найдено. Используй /add чтобы добавить.")
        return
    out = ["Список ДР:"]
    for r in rows:
        d = datetime.strptime(r["date"], "%Y-%m-%d").date()
        base = f"- ID {r['id']}: {r['name']} ({d.strftime('%d.%m')})"
        extras = []
        if r["remind_on_day"]:
            extras.append("в день")
        if r["remind_days_before"] is not None:
            extras.append(f"за {r['remind_days_before']} дн.")
        if r["custom_message"]:
            extras.append("кастомный текст")
        if r["username"]:
            extras.append(f"@{r['username']}")
        if extras:
            base += " — " + ", ".join(extras)
        out.append(base)
    await message.answer("\n".join(out))

@router.message(Command("delete"))
async def cmd_delete(message: Message, state: FSMContext):
    rows = list_birthdays(message.chat.id)
    if not rows:
        await message.answer("Удалять нечего — список пуст.")
        return
    await state.set_state(DeleteStates.choose_id)
    await message.answer("Введи ID записи, которую нужно удалить. Посмотри /list.")

@router.message(DeleteStates.choose_id)
async def delete_choose(message: Message, state: FSMContext):
    try:
        b_id = int(message.text.strip())
    except Exception:
        await message.answer("Нужен числовой ID. Посмотри /list.")
        return
    ok = delete_birthday(b_id, message.chat.id)
    if not ok:
        await message.answer("Запись не найдена для этого чата.")
        return
    remove_jobs_for_birthday(b_id)
    # Перезапуск расписания проще не делать — исторические jobs очищаются, новые будут созданы при повторном добавлении
    await state.clear()
    await message.answer("Удалено.")

# ---------- Main ----------
async def on_startup():
    init_db()
    scheduler.start()
    reschedule_all_from_db()
    logger.info("Bot started.")

async def main():
    await on_startup()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
