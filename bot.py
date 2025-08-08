import logging
import sqlite3
from datetime import datetime
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, CallbackContext
)
import os

# === Логи ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === База данных ===
DB_PATH = "birthdays.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS birthdays (
    user_id INTEGER,
    chat_id INTEGER,
    username TEXT,
    birthday TEXT,
    remind_days_before INTEGER,
    custom_message TEXT,
    PRIMARY KEY (user_id, chat_id)
)
""")
conn.commit()

# === Команды ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот для напоминаний о днях рождения 🎂\n\n"
        "📌 Команды:\n"
        "/set_birthday ДД.ММ [дни_до] [текст]\n"
        "/my_birthday — показать твой ДР\n"
        "/list_birthdays — показать все даты в чате\n"
        "/delete_birthday — удалить твой ДР"
    )

async def set_birthday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) < 1:
            await update.message.reply_text("Используй: /set_birthday ДД.ММ [дни_до] [текст]")
            return

        date_str = context.args[0]
        datetime.strptime(date_str, "%d.%m")  # проверка формата

        remind_days = int(context.args[1]) if len(context.args) >= 2 and context.args[1].isdigit() else 0
        custom_msg = " ".join(context.args[2:]) if len(context.args) > 2 else None

        cursor.execute("""
            REPLACE INTO birthdays (user_id, chat_id, username, birthday, remind_days_before, custom_message)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            update.effective_user.id,
            update.effective_chat.id,
            update.effective_user.first_name,
            date_str,
            remind_days,
            custom_msg
        ))
        conn.commit()

        await update.message.reply_text(f"✅ Дата сохранена: {date_str}, напоминание за {remind_days} дней")
    except ValueError:
        await update.message.reply_text("❌ Неверный формат даты. Используй ДД.ММ")

async def my_birthday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("""
        SELECT birthday, remind_days_before, custom_message
        FROM birthdays WHERE user_id=? AND chat_id=?
    """, (update.effective_user.id, update.effective_chat.id))
    data = cursor.fetchone()
    if data:
        await update.message.reply_text(f"🎂 Дата: {data[0]}\n⏳ За {data[1]} дней\n💬 {data[2]}")
    else:
        await update.message.reply_text("ℹ️ Ты ещё не сохранял дату.")

async def list_birthdays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("""
        SELECT username, birthday, remind_days_before
        FROM birthdays WHERE chat_id=?
    """, (update.effective_chat.id,))
    rows = cursor.fetchall()
    if not rows:
        await update.message.reply_text("📭 В этом чате пока нет сохранённых дней рождения.")
        return

    text = "📅 Список дней рождения:\n\n"
    for username, birthday, remind_days in rows:
        text += f"• {username} — {birthday} (за {remind_days} дней)\n"
    await update.message.reply_text(text)

async def delete_birthday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("DELETE FROM birthdays WHERE user_id=? AND chat_id=?",
                   (update.effective_user.id, update.effective_chat.id))
    conn.commit()
    await update.message.reply_text("🗑 Данные удалены.")

# === Проверка дней рождения ===
async def check_birthdays(context: CallbackContext):
    today_str = datetime.now().strftime("%d.%m")
    now = datetime.now()

    cursor.execute("SELECT user_id, chat_id, username, birthday, remind_days_before, custom_message FROM birthdays")
    for row in cursor.fetchall():
        user_id, chat_id, username, birthday, remind_days, custom_msg = row
        bday_date = datetime.strptime(birthday, "%d.%m").replace(year=now.year)

        # Напоминание за N дней
        if remind_days > 0 and (bday_date - now).days == remind_days:
            text = custom_msg or f"🎉 У {username} скоро день рождения!"
            await context.bot.send_message(chat_id=chat_id, text=text.replace("{username}", username))

        # В сам день
        if bday_date.strftime("%d.%m") == today_str:
            text = custom_msg or f"🎂 У {username} сегодня день рождения!"
            await context.bot.send_message(chat_id=chat_id, text=text.replace("{username}", username))

# === Запуск ===
async def main():
    TOKEN = os.getenv("BOT_TOKEN")
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set_birthday", set_birthday))
    app.add_handler(CommandHandler("my_birthday", my_birthday))
    app.add_handler(CommandHandler("list_birthdays", list_birthdays))
    app.add_handler(CommandHandler("delete_birthday", delete_birthday))

    app.job_queue.run_repeating(check_birthdays, interval=86400, first=5)

    await app.run_polling()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
