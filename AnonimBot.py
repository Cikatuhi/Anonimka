import asyncio
import logging
import os
import random
from datetime import datetime
from dotenv import load_dotenv
from time_utils import now_msk
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
import database

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
SITE_URL = os.getenv("SITE_URL", "https://secretky.ru").rstrip("/")

logging.basicConfig(level=logging.INFO)

active_sessions = {}
reply_sessions = {}
MAX_SESSIONS = 1000

ADMIN_ID = int(os.getenv("ADMIN_ID", "7683869901"))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or "без_юзернейма"
    first_name = user.first_name or "Пользователь"

    if await database.is_user_blocked(user_id):
        await database.remove_blocked_user(user_id)

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
                'recipient_name': recipient_name,
                'created_at': asyncio.get_event_loop().time()
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
    # Telegram can deliver channel/service updates without an effective user.
    # They must not crash the message handler (and should not affect sessions).
    if user is None or update.message is None:
        logging.info(
            "Ignoring message update without user/message: update_id=%s",
            update.update_id,
        )
        return
    user_id = user.id

    if user_id in reply_sessions:
        original_sender_id = reply_sessions.pop(user_id)
        try:
            reply_keyboard = [
                [InlineKeyboardButton("✍️ Ответить", callback_data=f"reply_{user_id}")],
                [InlineKeyboardButton(
                    "Узнать кто это",
                    callback_data=f"who_is_it_{original_sender_id}",
                    api_kwargs={
                        "style": "primary",
                        "icon_custom_emoji_id": "5424885441100782420",
                    },
                )]
            ]
            reply_markup = InlineKeyboardMarkup(reply_keyboard)
    
            content = "медиа-файл"
            content_type = "text"
    
            if update.message.text:
                content = update.message.text
                content_type = "text"
                await context.bot.send_message(
                    chat_id=original_sender_id,
                    text=f"✉️ Вам пришёл анонимный ответ:\n\n{content}",
                    reply_markup=reply_markup
                )
            elif update.message.photo:
                content_type = "photo"
                content = update.message.caption or "фото"
                photo = update.message.photo[-1].file_id
                await context.bot.send_photo(
                    chat_id=original_sender_id,
                    photo=photo,
                    caption=f"✉️ Вам пришёл анонимный ответ:\n\n{content}",
                    reply_markup=reply_markup
                )
            elif update.message.video:
                content_type = "video"
                content = update.message.caption or "видео"
                video = update.message.video.file_id
                await context.bot.send_video(
                    chat_id=original_sender_id,
                    video=video,
                    caption=f"✉️ Вам пришёл анонимный ответ:\n\n{content}",
                    reply_markup=reply_markup
                )
            elif update.message.voice:
                content_type = "voice"
                content = "голосовое сообщение"
                voice = update.message.voice.file_id
                await context.bot.send_voice(
                    chat_id=original_sender_id,
                    voice=voice,
                    caption="✉️ Вам пришёл анонимный ответ (голосовое)",
                    reply_markup=reply_markup
                )
            elif update.message.video_note:
                content_type = "video_note"
                content = "кружок"
                video_note = update.message.video_note.file_id
                await context.bot.send_video_note(
                    chat_id=original_sender_id,
                    video_note=video_note,
                    reply_markup=reply_markup
                )
            elif update.message.sticker:
                content_type = "sticker"
                content = "стикер"
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
    
            await database.save_message(user_id, original_sender_id, content, content_type)
            await update.message.reply_text("✅ Ваш ответ отправлен анонимно!")

            await delete_reply_prompt(context, user_id)
            context.user_data.clear()

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
            error_text = str(e).lower()
            if any(keyword in error_text for keyword in [
                "bot was blocked", 
                "forbidden", 
                "user is deactivated", 
                "bot was kicked", 
                "chat not found",
                "can't send messages to user"
            ]):
                if original_sender_id:
                    user_data = await database.get_user(original_sender_id)
                    if user_data:
                        await database.add_blocked_user(
                            original_sender_id,
                            user_data.get('username', ''),
                            user_data.get('first_name', '')
                        )
                        await update.message.reply_text(
                            f"⚠️ Пользователь @{user_data.get('username', '')} заблокировал бота. Он удалён из базы."
                        )
                    else:
                        if await database.is_user_blocked(original_sender_id):
                            await update.message.reply_text("⚠️ Этот пользователь уже в чёрном списке.")
            else:
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
            [InlineKeyboardButton(
                "Узнать кто это",
                callback_data=f"who_is_it_{user_id}",
                api_kwargs={
                    "style": "primary",
                    "icon_custom_emoji_id": "5424885441100782420",
                },
            )]
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
                text="✉️ Вам пришёл анонимный стикер:",
                reply_markup=reply_markup
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
        error_text = str(e).lower()
        if any(keyword in error_text for keyword in [
            "bot was blocked", 
            "forbidden", 
            "user is deactivated", 
            "bot was kicked", 
            "chat not found",
            "can't send messages to user"
        ]):
            user_data = await database.get_user(recipient_id)
            if user_data:
                await database.add_blocked_user(
                    recipient_id,
                    user_data.get('username', ''),
                    user_data.get('first_name', '')
                )
                await update.message.reply_text(
                    f"⚠️ Пользователь @{user_data.get('username', '')} заблокировал бота. Он удалён из базы."
                )
            else:
                if await database.is_user_blocked(recipient_id):
                    await update.message.reply_text("⚠️ Этот пользователь уже в чёрном списке.")
        else:
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

        try:
            await query.message.delete()
        except Exception as e:
            print(f"Не удалось удалить сообщение с инструкцией ответа: {e}")

        context.user_data.clear()
        return

    if user_id in active_sessions:
        del active_sessions[user_id]
        try:
            await query.message.delete()
        except Exception as e:
            print(f"Не удалось удалить сообщение с отменённым действием: {e}")
        return

    try:
        await query.message.delete()
    except Exception as e:
        print(f"Не удалось удалить сообщение с отменённым действием: {e}")


async def delete_reply_prompt(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Удаляет отдельное сообщение с инструкцией для ответа."""
    message_id = context.user_data.get('question_message_id')
    if not message_id:
        return

    try:
        await context.bot.delete_message(
            chat_id=context.user_data.get('original_chat_id', user_id),
            message_id=message_id,
        )
    except Exception as e:
        print(f"Ошибка удаления сообщения с инструкцией ответа: {e}")


async def start_reply_action(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    data: str,
    original_chat_id: int,
    original_message_id: int,
    original_text: str | None,
    original_reply_markup,
) -> bool:
    """Открывает форму анонимного ответа после успешной проверки SubGram."""
    if user_id in reply_sessions:
        await delete_reply_prompt(context, user_id)

        del reply_sessions[user_id]
        context.user_data.clear()

    if data == "reply_anon":
        original_sender_id = ADMIN_ID
    elif data.startswith("reply_"):
        parts = data.split("_")
        if len(parts) >= 2:
            try:
                original_sender_id = int(parts[1])
            except ValueError:
                return False
        else:
            return False
    else:
        return False

    context.user_data['original_chat_id'] = original_chat_id

    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    prompt_text = (
        "<b>✍️ Напишите ваш ответ</b>\n\n"
        "Введите текст, фото, видео или другой контент, который хотите отправить анонимно."
    )

    msg = await context.bot.send_message(
        chat_id=original_chat_id,
        text=prompt_text,
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

    reply_sessions[user_id] = original_sender_id
    context.user_data['question_message_id'] = msg.message_id
    return True


async def reply_anon_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or query.from_user is None or query.message is None:
        logging.warning(
            "Ignoring malformed reply callback: update_id=%s",
            update.update_id,
        )
        return
    data = query.data
    user_id = query.from_user.id
    chat_id = update.effective_chat.id
    original_message = query.message

    logging.info(
        "Reply callback received: update_id=%s data=%r user_id=%s chat_id=%s",
        update.update_id,
        data,
        user_id,
        chat_id,
    )

    # ===== 1. ПРОВЕРКА SUBGRAM =====
    await query.answer("⏳ Проверяем подписки...")
    subgram_result = await database.check_subgram(user_id, chat_id)

    if subgram_result.get("status") == "warning":
        # SubGram сам отправляет блок обязательной подписки. Сохраняем
        # исходное действие, чтобы продолжить его после «Я выполнил».
        context.user_data['subgram_pending_reply'] = {
            'data': data,
            'chat_id': original_message.chat_id,
            'message_id': original_message.message_id,
            'text': original_message.text,
            'reply_markup': original_message.reply_markup,
        }
        return

    if subgram_result.get("status") == "error":
        logging.warning("SubGram не ответил корректно: %s. Доступ разрешён.", subgram_result.get("message"))

    # ===== 2. СТАНДАРТНАЯ ЛОГИКА ОТВЕТА =====
    started = await start_reply_action(
        context=context,
        user_id=user_id,
        data=data,
        original_chat_id=original_message.chat_id,
        original_message_id=original_message.message_id,
        original_text=original_message.text,
        original_reply_markup=original_message.reply_markup,
    )

    if not started:
        await query.edit_message_text(
            text="❌ Не удалось обработать запрос.",
            parse_mode="HTML"
        )


async def subgram_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повторно проверяет подписки после кнопки SubGram «Я выполнил»."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    subgram_result = await database.check_subgram(user_id, chat_id)

    if subgram_result.get("status") == "warning":
        await query.answer("❌ Выполнены ещё не все задания. Попробуйте снова после подписки.", show_alert=True)
        return

    if subgram_result.get("status") == "error":
        logging.warning("SubGram не ответил корректно: %s. Доступ разрешён.", subgram_result.get("message"))

    pending_reply = context.user_data.pop('subgram_pending_reply', None)
    if not pending_reply:
        await query.answer("✅ Подписка подтверждена.")
        return

    started = await start_reply_action(
        context=context,
        user_id=user_id,
        data=pending_reply['data'],
        original_chat_id=pending_reply['chat_id'],
        original_message_id=pending_reply['message_id'],
        original_text=pending_reply.get('text'),
        original_reply_markup=pending_reply.get('reply_markup'),
    )

    if not started:
        await query.answer("❌ Не удалось продолжить действие. Нажмите кнопку «Ответить» ещё раз.", show_alert=True)
        return

    try:
        await query.message.delete()
    except Exception as e:
        print(f"Не удалось удалить сообщение SubGram: {e}")


async def unknown_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log callback data that has no registered handler."""
    query = update.callback_query
    if query is None:
        return
    logging.warning(
        "Unhandled callback: update_id=%s data=%r user_id=%s chat_id=%s",
        update.update_id,
        query.data,
        query.from_user.id if query.from_user else None,
        query.message.chat_id if query.message else None,
    )
    await query.answer("Кнопка устарела. Откройте новое сообщение и попробуйте ещё раз.", show_alert=True)


async def application_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Unhandled Telegram update error: %s", context.error, exc_info=context.error)

async def who_is_it_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    user_id = user.id
    username = user.username or "без_юзернейма"
    first_name = user.first_name or "Пользователь"
    
    await database.register_user(user_id, username, first_name)
    
    callback_data = query.data
    sender_id = None
    if callback_data.startswith("who_is_it_"):
        parts = callback_data.split("_")
        if len(parts) >= 3:
            try:
                sender_id = int(parts[2])
            except ValueError:
                sender_id = None
    
    print(f"🔍 sender_id: {sender_id}")
    
    conn = await database.get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT status, end_date FROM subscriptions WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
            user_id
        )
        print(f"🔍 SQL row: {row}")
    finally:
        await database.release_conn(conn)
    
    is_active = False
    if row:
        status = row['status']
        end_date = row['end_date']
        if status == 'active' and (end_date is None or end_date > now_msk()):
            is_active = True
            print(f"✅ Подписка АКТИВНА для {user_id}!")
        else:
            print(f"⚠️ Подписка НЕ активна: status={status}, end_date={end_date}")
    else:
        print(f"❌ Подписка НЕ найдена для {user_id}")
    
    if is_active:
        if sender_id and sender_id != 0:
            await show_sender_as_new_message(query, sender_id)
            await query.answer("🔓 Отправитель раскрыт!")
        else:
            all_users = await database.get_all_users(limit=1000)
            available_users = [u for u in all_users if u != user_id]
            
            if available_users:
                random_user_id = random.choice(available_users)
                await show_sender_as_new_message(query, random_user_id)
                await query.answer("🔓 Показан случайный пользователь (рассылка)")
            else:
                await query.message.reply_text(
                    "😅 В базе пока нет других пользователей.",
                    parse_mode="HTML"
                )
                await query.answer("❌ Нет пользователей")
        return
    
    has_seen = await database.has_user_seen_premium_offer(user_id)
    
    if not has_seen:
        await database.mark_premium_offer_as_seen(user_id)
        text = (
            "💌 <b>Узнайте кто отправил это послание - приобретите пробный Премиум за 5₽!</b> 👇\n\n"
        )
        keyboard = [
            [InlineKeyboardButton("💳 Получить Премиум", callback_data="buy_premium")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        await query.answer("💳 Нажмите на кнопку для оплаты")
        return
    
    payment_link = f"{SITE_URL}/pay/{user_id}"
    text = "<b>💌 Узнайте, кто отправил вам анонимное сообщение – всего за 5₽!</b>"
    keyboard = [
        [InlineKeyboardButton("👀 Узнать отправителя", url=payment_link)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text(
        text=text,
        reply_markup=reply_markup,
        parse_mode="HTML"
    )
    await query.answer("🔗 Ссылка на оплату отправлена!")


async def show_sender_as_new_message(query, sender_id):
    sender_data = await database.get_user(sender_id)
    
    if sender_data:
        username = sender_data.get('username', 'без_юзернейма')
        first_name = sender_data.get('first_name', 'Пользователь')
        
        text = (
            f"🤫 <b>Пользователь рассекречен. Им оказался – {first_name}</b>\n\n"
            f"<b>Логин пользователя:</b> @{username}\n"
            f"<b>Его идентификатор в Telegram:</b> <code>{sender_id}</code>\n\n"
        )
        
        keyboard = None
        if username and username != 'без_юзернейма':
            keyboard = [
                [InlineKeyboardButton("👤 Перейти в профиль", url=f"https://t.me/{username}")]
            ]
        else:
            keyboard = [
                [InlineKeyboardButton("👤 Перейти в профиль", url=f"tg://user?id={sender_id}")]
            ]
        
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        
        await query.message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
    else:
        await query.message.reply_text(
            text="😅 Произошла ошибка. Попробуйте позже.",
            parse_mode="HTML"
        )


async def buy_premium_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    user_id = user.id
    username = user.username or "без_юзернейма"
    first_name = user.first_name or "Пользователь"
    
    await database.register_user(user_id, username, first_name)
        
    payment_link = f"{SITE_URL}/pay/{user_id}"
    
    text = (
        f"<b>💌 Узнайте, кто отправил вам анонимное сообщение – всего за 5₽!</b>\n\n"
        f"Нажмите на кнопку ниже, чтобы оплатить."
    )
    
    keyboard = [
        [InlineKeyboardButton("💳 Оплатить 5₽", url=payment_link)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=text,
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def show_sender(query, user_id):
    callback_data = query.data
    sender_id = None
    
    if callback_data.startswith("who_is_it_"):
        try:
            sender_id = int(callback_data.split("_")[2])
        except (IndexError, ValueError):
            sender_id = None
    
    if sender_id:
        sender_data = await database.get_user(sender_id)
        if sender_data:
            username = sender_data.get('username', 'без_юзернейма')
            first_name = sender_data.get('first_name', 'Пользователь')
            
            text = (
                f"🤫 <b>Пользователь рассекречен. Им оказался – {first_name}</b>\n\n"
                f"Знакомы вы с этим пользователем?\n\n"
                f"<b>Логин пользователя:</b> @{username}\n"
                f"<b>Его идентификатор в Telegram:</b> <code>{sender_id}</code>\n\n"
            )
            
            keyboard = None
            if username and username != 'без_юзернейма':
                keyboard = [
                    [InlineKeyboardButton("👤 Перейти в профиль", url=f"https://t.me/{username}")]
                ]
            else:
                keyboard = [
                    [InlineKeyboardButton("👤 Перейти в профиль", url=f"tg://user?id={sender_id}")]
                ]
            
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
            
            await query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
            return
    
    all_users = await database.get_all_users(limit=1000)
    available_users = [u for u in all_users if u != user_id]
    
    if not available_users:
        await query.edit_message_text(
            text="😅 К сожалению, в базе пока нет других пользователей.",
            parse_mode="HTML"
        )
        return
    
    random_user_id = random.choice(available_users)
    random_user = await database.get_user(random_user_id)
    
    if not random_user:
        await query.edit_message_text(
            text="😅 Произошла ошибка. Попробуйте позже.",
            parse_mode="HTML"
        )
        return
    
    username = random_user.get('username', 'без_юзернейма')
    first_name = random_user.get('first_name', 'Пользователь')
    
    text = (
        f"🤫 <b>Этот пользователь отправил вам анонимное сообщение – {first_name}</b>\n\n"
        f"Знакомы вы с этим пользователем?\n\n"
        f"<b>Логин пользователя:</b> @{username}\n"
        f"<b>Его идентификатор в Telegram:</b> <code>{random_user_id}</code>\n\n"
        f"Перейти в профиль пользователя, можно по нажатию на кнопку ниже."
    )
    
    keyboard = [
        [InlineKeyboardButton("👤 Перейти в профиль", url=f"tg://user?id={random_user_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
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
        f"├─ <a href='{SITE_URL}/doc1'>Политика конфиденциальности</a>\n"
        f"├─ <a href='{SITE_URL}/doc2'>Условия использования</a>\n"
        f"└─ <a href='{SITE_URL}/doc3'>Тарификация</a>\n\n"
        f"📛 <b>Контактная информация</b>\n" 
        f"└─ ИП Докучаева Оксана Анатольевна\n"
        f" ├─ ИНН: 631936284733\n"
        f" └─ ОРГНИП: 325632700083871\n",
        parse_mode="HTML"
    )


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or ''
    first_name = user.first_name or 'Пользователь'
    
    try:
        subscription = await database.get_subscription_by_user_id(user_id)
    except Exception:
        logging.exception("/unsub: failed to read subscription for user %s", user_id)
        await update.message.reply_text(
            "⚠️ Не удалось проверить подписку. Попробуйте ещё раз через минуту."
        )
        return
    
    if not subscription:
        await update.message.reply_text(
            "❌ У вас нет активной подписки для отмены.\n\n"
            "Чтобы оформить подписку, нажмите на кнопку «👀 Узнать отправителя» в любом анонимном сообщении."
        )
        return
    
    try:
        already_unsubscribed = await database.is_user_unsubscribed(user_id)
    except Exception:
        logging.exception("/unsub: failed to check unsubscribe state for user %s", user_id)
        await update.message.reply_text(
            "⚠️ Не удалось проверить статус подписки. Попробуйте ещё раз через минуту."
        )
        return

    if already_unsubscribed:
        await update.message.reply_text(
            "ℹ️ Вы уже отписались от подписки.\n\n"
            "Если хотите оформить подписку снова — нажмите «👀 Узнать отправителя» в любом анонимном сообщении."
        )
        return
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, отписаться", callback_data="confirm_unsubscribe"),
            InlineKeyboardButton("❌ Нет, передумал", callback_data="cancel_unsubscribe")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"⚠️ <b>Вы уверены, что хотите отписаться от подписки?</b>\n\n"
        f"После отписки вы больше не сможете узнавать авторов анонимных сообщений.\n\n"
        f"Вы всегда можете оформить подписку заново, нажав на кнопку «👀 Узнать отправителя» в любом анонимном сообщении.",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def confirm_unsubscribe_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    user_id = user.id
    username = user.username or ''
    first_name = user.first_name or 'Пользователь'
    
    subscription = await database.get_subscription_by_user_id(user_id)
    if not subscription:
        await query.edit_message_text(
            "❌ У вас нет активной подписки для отмены.",
            parse_mode="HTML"
        )
        return
    
    success = await database.unsubscribe_user(user_id, username, first_name, 'manual')
    
    if success:
        await query.edit_message_text(
            "✅ <b>Вы успешно отписались от подписки!</b>\n\n"
            "Премиум-доступ отключён.\n"
            "Вы больше не будете получать уведомления о списаниях.\n\n"
            "Чтобы оформить подписку снова — нажмите «👀 Узнать отправителя» в любом анонимном сообщении.",
            parse_mode="HTML"
        )
    else:
        await query.edit_message_text(
            "❌ Произошла ошибка при отписке. Попробуйте позже или обратитесь в поддержку.",
            parse_mode="HTML"
        )


async def cancel_unsubscribe_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "✅ Отписка отменена. Ваша подписка активна.",
        parse_mode="HTML"
    )


def main():
    import asyncio
    import database
    
    async def initialize_database():
        try:
            await database.init_db()
        finally:
            await database.close_pool()

    asyncio.run(initialize_database())

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("unsub", unsubscribe_command))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(cancel_handler, pattern="cancel"))
    app.add_handler(CallbackQueryHandler(send_again_handler, pattern="send_again_"))
    app.add_handler(CallbackQueryHandler(subgram_callback_handler, pattern=r"^subgram-op$"))
    app.add_handler(CallbackQueryHandler(reply_anon_handler, pattern="reply_"))
    app.add_handler(CallbackQueryHandler(who_is_it_handler, pattern="who_is_it"))
    app.add_handler(CallbackQueryHandler(buy_premium_handler, pattern="buy_premium"))
    app.add_handler(CallbackQueryHandler(confirm_unsubscribe_handler, pattern="confirm_unsubscribe"))
    app.add_handler(CallbackQueryHandler(cancel_unsubscribe_handler, pattern="cancel_unsubscribe"))
    app.add_handler(CallbackQueryHandler(unknown_callback_handler))
    app.add_error_handler(application_error_handler)

    print("🤖 Бот запущен и готов к приёму анонимных сообщений...")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()


if __name__ == "__main__":
    main()
