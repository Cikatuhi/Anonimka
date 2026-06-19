import asyncio
import logging
import os
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
import database

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO)

# Хранилище активных сессий {user_id: {'recipient_id': int, 'recipient_name': str}}
active_sessions = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or "без_юзернейма"
    first_name = user.first_name or "Пользователь"

    # Регистрируем пользователя в БД
    await database.register_user(user_id, username, first_name)

    # Проверяем, есть ли параметр start (когда перешли по ссылке)
    args = context.args
    if args:
        recipient_id = int(args[0])
        if recipient_id != user_id:
            # Получаем имя получателя из базы данных
            recipient_data = await database.get_user(recipient_id)
            if recipient_data:
                recipient_name = recipient_data['first_name']
            else:
                recipient_name = "этого человека"

            # Сохраняем сессию
            active_sessions[user_id] = {
                'recipient_id': recipient_id,
                'recipient_name': recipient_name
            }

            keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"<b>💌 Отправьте анонимное сообщение этому человеку!</b>\n\n"
                f"<b>✍️ Напишите сюда всё, что хотите ему передать</b>, и {recipient_name} увидит ваше сообщение сразу же, но не будет знать, от кого оно.\n\n"
                f"Отправить можно: 📝 текст, 🎞 фото или видео, 🔊 кружки и голосовые, а также стикеры ✨",
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
            return  # ⬅️ Важный return, чтобы не отправлять реферальную ссылку сразу

    # Если это обычный /start (без ссылки) или пришёл после отправки
    bot_username = context.bot.username
    deep_link = f"https://t.me/{bot_username}?start={user_id}"

    welcome_text = (
        f"<b>💌 Начните получать анонимные сообщения прямо сейчас!</b>\n\n"
        f"👉 Твоя ссылка:\n{deep_link}\n\n"
        f"💬 Поделитесь этой ссылкой в истории или в описании профиля, чтобы начать получать анонимные сообщения"
    )

    share_text = f"Отправь мне анонимное сообщение, по этой ссылке 👉"
    share_keyboard = [
        [InlineKeyboardButton("📤 Поделиться ссылкой", url=f"https://t.me/share/url?url={deep_link}&text={share_text}")]
    ]
    reply_markup = InlineKeyboardMarkup(share_keyboard)

    await update.message.reply_text(
        welcome_text,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=True
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    # Проверяем, есть ли активная сессия
    session = active_sessions.pop(user_id, None)
    if not session:
        await update.message.reply_text("Отправьте /start, чтобы получить свою ссылку.")
        return

    recipient_id = session['recipient_id']
    recipient_name = session['recipient_name']

    try:
        # Отправляем сообщение получателю (анонимно)
        if update.message.text:
            content = update.message.text
            await context.bot.send_message(chat_id=recipient_id, text=f"✉️ Вам пришло анонимное сообщение:\n\n{content}")
        elif update.message.photo:
            photo = update.message.photo[-1].file_id
            caption = update.message.caption or ""
            await context.bot.send_photo(chat_id=recipient_id, photo=photo, caption=f"✉️ Вам пришло анонимное фото:\n\n{caption}")
        elif update.message.video:
            video = update.message.video.file_id
            caption = update.message.caption or ""
            await context.bot.send_video(chat_id=recipient_id, video=video, caption=f"✉️ Вам пришло анонимное видео:\n\n{caption}")
        elif update.message.voice:
            voice = update.message.voice.file_id
            await context.bot.send_voice(chat_id=recipient_id, voice=voice, caption="✉️ Вам пришло анонимное голосовое сообщение")
        elif update.message.video_note:
            video_note = update.message.video_note.file_id
            await context.bot.send_video_note(chat_id=recipient_id, video_note=video_note)
        elif update.message.sticker:
            sticker = update.message.sticker.file_id
            # Сначала уведомление
            await context.bot.send_message(
                chat_id=recipient_id,
                text="✉️ Вам пришёл анонимный стикер:"
            )
            # Потом сам стикер
            await context.bot.send_sticker(
                chat_id=recipient_id,
                sticker=sticker
            )
        else:
            await update.message.reply_text("⚠️ Этот тип сообщения пока не поддерживается.")
            return

        # Подтверждение отправителю
        await update.message.reply_text("<b>✅ Сообщение доставлено!</b>", parse_mode="HTML")

        # Отправляем отправителю его реферальную ссылку
        bot_username = context.bot.username
        deep_link = f"https://t.me/{bot_username}?start={user_id}"

        welcome_text = (
            f"<b>💌 Начните получать анонимные сообщения прямо сейчас!</b>\n\n"
            f"👉 Твоя ссылка:\n{deep_link}\n\n"
            f"💬 Поделитесь этой ссылкой в истории или в описании профиля, чтобы начать получать анонимные сообщения"
        )

        share_text = f"Отправь мне анонимное сообщение, по этой ссылке 👉"
        share_keyboard = [
            [InlineKeyboardButton("📤 Поделиться ссылкой", url=f"https://t.me/share/url?url={deep_link}&text={share_text}")]
        ]
        reply_markup = InlineKeyboardMarkup(share_keyboard)

        await update.message.reply_text(
            welcome_text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    except Exception as e:
        await update.message.reply_text("❌ Не удалось отправить сообщение. Возможно, получатель заблокировал бота.")
        print(f"Ошибка отправки: {e}")

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if user_id in active_sessions:
        del active_sessions[user_id]

    await query.edit_message_text("❌ Отправка отменена.")

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🌟 ℹ️ <b>Информация</b> 🌟\n\n"
        f"🍬 <b>Техническая поддержка</b>\n"
        f"Если у вас возник вопрос, предложение или просто есть желание что-то обсудить, напишите нам – @youzabota\n"
        f"⏰ <i>Время работы: с 10:00 до 19:00 (МСК)</i>\n\n"
        f"📚 <b>Документация</b>\n"
        f"├─ Ссылка на условия пользования\n"
        f"├─ Ссылка на политику конф.\n"
        f"└─ Ссылка на тариф.\n\n"
        f"📛 Контактная информация\n" 
        f"└─ ИП Докучаева Оксана Анатольевна\n"
        f" ├─ ИНН: 631936284733\n"
        f" └─ ОРГНИП: 325632700083871\n",
        parse_mode="HTML"
        
    )

def main():
    asyncio.run(database.init_db())

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(cancel_handler, pattern="cancel"))

    print("🤖 Бот запущен и готов к приёму анонимных сообщений...")
    app.run_polling()

if __name__ == "__main__":
    main()