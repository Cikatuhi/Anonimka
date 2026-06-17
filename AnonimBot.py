import asyncio
import logging
from dotenv import load_dotenv
import os

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
TOKEN = "8751506578:AAGQ3A5hV-d5la4s3x4UV0UFl6xCW-x1x0I"

logging.basicConfig(level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет - это проверка start")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("А тут можно я расскажу что умею start and help. Good luck")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
