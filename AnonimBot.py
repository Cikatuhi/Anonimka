import asyncio
import logging
import os
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
import database

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or "без_юзернейма"
    first_name = user.first_name or "Пользователь"

    # 1. Регистрируем пользователя в PostgreSQL
    is_new = await database.register_user(user_id, username, first_name)
    
    # 2. Создаём персональную ссылку
    bot_username = context.bot.username
    deep_link = f"https://t.me/{bot_username}?start={user_id}"
    
    # 3. Приветственное сообщение
    welcome_text = (
        f"👋 Привет, {first_name}!\n\n"
        f"Твой уникальный ID: `{user_id}`\n"
        f"👉 Твоя персональная ссылка:\n{deep_link}\n\n"
        f"Добавь эту ссылку ☝️ в описание профиля Telegram/TikTok/Instagram и в истории, <b>что-бы получать послания</b>💌"
    )
    
    # 4. Кнопка для копирования
    keyboard = [
        [InlineKeyboardButton("📋 Копировать ссылку", callback_data="copy_link")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=reply_markup,
        parse_mode=None
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я умею: /start — получить свою ссылку")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "copy_link":
        user_id = query.from_user.id
        bot_username = context.bot.username
        deep_link = f"https://t.me/{bot_username}?start={user_id}"
        await query.edit_message_text(
            text=f"Скопируй эту ссылку:\n`{deep_link}`",
            parse_mode="Markdown"
        )

def main():
    # Инициализация базы данных
    asyncio.run(database.init_db())
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    print("🤖 Бот запущен и подключен к PostgreSQL...")
    app.run_polling()

if __name__ == "__main__":
    main()