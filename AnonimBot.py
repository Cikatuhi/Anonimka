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

active_sessions = {}
reply_sessions = {}

ADMIN_ID = 7683869901

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or "без_юзернейма"
    first_name = user.first_name or "Пользователь"

    await database.register_user(user_id, username, first_name)

    await update.message.reply_text("💌")

    args = context.args
    if args:
        recipient_id = int(args[0])
        if recipient_id != user_id:
            recipient_data = await database.get_user(recipient_id)
            if recipient_data:
                recipient_name = recipient_data['first_name']
            else:
                recipient_name = "этого человека"

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
            return

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

    if user_id in reply_sessions:
        original_sender_id = reply_sessions.pop(user_id)
        try:
            reply_keyboard = [
                [InlineKeyboardButton("✍️ Ответить", callback_data=f"reply_{user_id}")],
                [InlineKeyboardButton("👀 Узнать кто это", callback_data="who_is_it", style="primary")]
            ]
            reply_markup = InlineKeyboardMarkup(reply_keyboard)

            if update.message.text:
                content = update.message.text
                await context.bot.send_message(
                    chat_id=original_sender_id,
                    text=f"✉️ Вам пришёл анонимный ответ:\n\n{content}",
                    reply_markup=reply_markup
                )
            elif update.message.photo:
                photo = update.message.photo[-1].file_id
                caption = update.message.caption or ""
                await context.bot.send_photo(
                    chat_id=original_sender_id,
                    photo=photo,
                    caption=f"✉️ Вам пришёл анонимный ответ:\n\n{caption}",
                    reply_markup=reply_markup
                )
            elif update.message.video:
                video = update.message.video.file_id
                caption = update.message.caption or ""
                await context.bot.send_video(
                    chat_id=original_sender_id,
                    video=video,
                    caption=f"✉️ Вам пришёл анонимный ответ:\n\n{caption}",
                    reply_markup=reply_markup
                )
            elif update.message.voice:
                voice = update.message.voice.file_id
                await context.bot.send_voice(
                    chat_id=original_sender_id,
                    voice=voice,
                    caption="✉️ Вам пришёл анонимный ответ (голосовое)",
                    reply_markup=reply_markup
                )
            elif update.message.video_note:
                video_note = update.message.video_note.file_id
                await context.bot.send_video_note(
                    chat_id=original_sender_id,
                    video_note=video_note,
                    reply_markup=reply_markup
                )
            elif update.message.sticker:
                sticker = update.message.sticker.file_id
                await context.bot.send_message(
                    chat_id=original_sender_id,
                    text="✉️ Вам пришёл анонимный ответ (стикер):",
                    reply_markup=reply_markup
                )
                await context.bot.send_sticker(
                    chat_id=original_sender_id,
                    sticker=sticker
                )
            else:
                await update.message.reply_text("⚠️ Этот тип сообщения пока не поддерживается для ответа.")
                return

            await update.message.reply_text("✅ Ваш ответ отправлен анонимно!")

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
            share_reply_markup = InlineKeyboardMarkup(share_keyboard)

            await update.message.reply_text(
                welcome_text,
                reply_markup=share_reply_markup,
                parse_mode="HTML",
                disable_web_page_preview=True
            )

        except Exception as e:
            await update.message.reply_text("❌ Не удалось отправить ответ. Возможно, получатель заблокировал бота.")
            print(f"Ошибка отправки ответа: {e}")
        return

    session = active_sessions.pop(user_id, None)
    if not session:
        await update.message.reply_text("Отправьте /start, чтобы получить свою ссылку.")
        return

    recipient_id = session['recipient_id']
    recipient_name = session['recipient_name']

    try:
        reply_keyboard = [
            [InlineKeyboardButton("✍️ Ответить", callback_data=f"reply_{user_id}")],
            [InlineKeyboardButton("👀 Узнать кто это", callback_data="who_is_it", style="primary")]
        ]
        reply_markup = InlineKeyboardMarkup(reply_keyboard)

        content = "медиа-файл"
        content_type = "text"

        if update.message.text:
            content = update.message.text
            content_type = "text"
            await context.bot.send_message(
                chat_id=recipient_id,
                text=f"✉️ Вам пришло анонимное сообщение:\n\n{content}",
                reply_markup=reply_markup
            )
        elif update.message.photo:
            content_type = "photo"
            content = update.message.caption or "фото"
            photo = update.message.photo[-1].file_id
            await context.bot.send_photo(
                chat_id=recipient_id,
                photo=photo,
                caption=f"✉️ Вам пришло анонимное фото:\n\n{content}",
                reply_markup=reply_markup
            )
        elif update.message.video:
            content_type = "video"
            content = update.message.caption or "видео"
            video = update.message.video.file_id
            await context.bot.send_video(
                chat_id=recipient_id,
                video=video,
                caption=f"✉️ Вам пришло анонимное видео:\n\n{content}",
                reply_markup=reply_markup
            )
        elif update.message.voice:
            content_type = "voice"
            content = "голосовое сообщение"
            voice = update.message.voice.file_id
            await context.bot.send_voice(
                chat_id=recipient_id,
                voice=voice,
                caption="✉️ Вам пришло анонимное голосовое сообщение",
                reply_markup=reply_markup
            )
        elif update.message.video_note:
            content_type = "video_note"
            content = "кружок"
            video_note = update.message.video_note.file_id
            await context.bot.send_video_note(
                chat_id=recipient_id,
                video_note=video_note,
                reply_markup=reply_markup
            )
        elif update.message.sticker:
            content_type = "sticker"
            content = "стикер"
            sticker = update.message.sticker.file_id
            await context.bot.send_message(
                chat_id=recipient_id,
                text="✉️ Вам пришёл анонимный стикер:"
            )
            await context.bot.send_sticker(
                chat_id=recipient_id,
                sticker=sticker,
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text("⚠️ Этот тип сообщения пока не поддерживается.")
            return

        await database.save_message(user_id, recipient_id, content, content_type)

        keyboard = [
            [InlineKeyboardButton("📨 Отправить ещё одно сообщение", callback_data=f"send_again_{recipient_id}_{recipient_name}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "<b>✅ Ваше сообщение доставлено, ожидайте ответа от него</b>",
            reply_markup=reply_markup,
            parse_mode="HTML"
        )

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
        share_reply_markup = InlineKeyboardMarkup(share_keyboard)

        await update.message.reply_text(
            welcome_text,
            reply_markup=share_reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    except Exception as e:
        await update.message.reply_text("❌ Не удалось отправить сообщение. Возможно, получатель заблокировал бота.")
        print(f"Ошибка отправки: {e}")

async def send_again_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split("_")
    recipient_id = int(data[2])
    recipient_name = data[3]

    user_id = query.from_user.id

    active_sessions[user_id] = {
        'recipient_id': recipient_id,
        'recipient_name': recipient_name
    }

    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"<b>💌 Отправьте ещё одно анонимное сообщение этому человеку!</b>\n\n"
        f"<b>✍️ Напишите сюда всё, что хотите ему передать</b>, и {recipient_name} увидит ваше сообщение сразу же, но не будет знать, от кого оно.\n\n"
        f"Отправить можно: 📝 текст, 🎞 фото или видео, 🔊 кружки и голосовые, а также стикеры ✨",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if user_id in reply_sessions:
        del reply_sessions[user_id]

        if 'original_text' in context.user_data:
            try:
                original_text = context.user_data.get('original_text', 'Сообщение')
                original_reply_markup = context.user_data.get('original_reply_markup')
                original_chat_id = context.user_data.get('original_chat_id', user_id)

                await context.bot.send_message(
                    chat_id=original_chat_id,
                    text=original_text,
                    reply_markup=original_reply_markup,
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"Ошибка восстановления: {e}")
        
        try:
            await query.delete_message()
        except Exception as e:
            print(f"Не удалось удалить сообщение: {e}")
        
        context.user_data.clear()
        return

    if user_id in active_sessions:
        del active_sessions[user_id]
        await query.edit_message_text("❌ Отправка отменена.")
        return

    await query.edit_message_text("❌ Отправка отменена.")

async def reply_anon_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id
    original_message = query.message

    if user_id in reply_sessions:
        if 'question_message_id' in context.user_data:
            try:
                await context.bot.delete_message(
                    chat_id=user_id,
                    message_id=context.user_data['question_message_id']
                )
            except Exception as e:
                print(f"Не удалось удалить сообщение с вопросом: {e}")

        if 'original_text' in context.user_data:
            try:
                original_text = context.user_data.get('original_text', 'Сообщение')
                original_reply_markup = context.user_data.get('original_reply_markup')
                original_chat_id = context.user_data.get('original_chat_id', user_id)

                await context.bot.send_message(
                    chat_id=original_chat_id,
                    text=original_text,
                    reply_markup=original_reply_markup,
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"Ошибка восстановления: {e}")

        del reply_sessions[user_id]
        context.user_data.clear()

    context.user_data['original_text'] = original_message.text
    context.user_data['original_chat_id'] = original_message.chat_id
    context.user_data['original_reply_markup'] = original_message.reply_markup

    if data == "reply_anon":
        reply_sessions[user_id] = ADMIN_ID
        keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        msg = await query.edit_message_text(
            text=f"<b>✍️ Напишите ваш ответ</b>\n\n"
                 f"Введите текст, фото, видео или другой контент, который хотите отправить анонимно.",
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        context.user_data['question_message_id'] = msg.message_id
        return

    if data.startswith("reply_"):
        parts = data.split("_")
        if len(parts) >= 2:
            try:
                original_sender_id = int(parts[1])
                reply_sessions[user_id] = original_sender_id

                keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                msg = await query.edit_message_text(
                    text=f"<b>✍️ Напишите ваш ответ</b>\n\n"
                         f"Введите текст, фото, видео или другой контент, который хотите отправить анонимно.",
                    reply_markup=reply_markup,
                    parse_mode="HTML"
                )
                context.user_data['question_message_id'] = msg.message_id
                return
            except ValueError:
                pass

    await query.edit_message_text(
        text="❌ Не удалось обработать запрос.",
        parse_mode="HTML"
    )

async def who_is_it_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = (
        f"<b>💌 Узнайте, кто отправил вам анонимное сообщение – всего за 5₽!</b>\n\n"
    )

    keyboard = [
        [InlineKeyboardButton("👀 Узнать отправителя", url="https://ваш-сайт.ру")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=query.from_user.id,
        text=text,
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

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
    app.add_handler(CallbackQueryHandler(send_again_handler, pattern="send_again_"))
    app.add_handler(CallbackQueryHandler(reply_anon_handler, pattern="reply_"))
    app.add_handler(CallbackQueryHandler(who_is_it_handler, pattern="who_is_it"))

    print("🤖 Бот запущен и готов к приёму анонимных сообщений...")
    app.run_polling()

if __name__ == "__main__":
    main()