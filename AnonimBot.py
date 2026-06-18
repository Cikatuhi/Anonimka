import asyncio
import logging
import os
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
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
        f"💬 Начни получать анонимные сообщения прямо сейчас!\n\n"
        f"👉 Твоя ссылка:\n{deep_link}\n\n"
        f"Добавь эту ссылку ☝️ в описание профиля Telegram/TikTok/Instagram и в истории, чтобы получать послания💌"
    )
    
# 4. Кнопка "Поделиться ссылкой"
    share_text = f"Отправь мне анонимное сообщение, по этой ссылке 👉"
    share_keyboard = [
    [InlineKeyboardButton("📤 Поделиться ссылкой", url=f"https://t.me/share/url?url={deep_link}&text={share_text}")]
]
    reply_markup = InlineKeyboardMarkup(share_keyboard)
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=reply_markup,
        parse_mode=None,
        disable_web_page_preview=True
    )
        

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я умею: /start — получить свою ссылку")

def main():
    # Инициализация базы данных
    asyncio.run(database.init_db())
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    
    print("🤖 Бот запущен и подключен к PostgreSQL...")
    app.run_polling()

if __name__ == "__main__":
    main()