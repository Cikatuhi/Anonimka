import os
import asyncio
import uuid
import json
import psutil
import threading
from datetime import datetime
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import asyncpg
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Admin Panel для Telegram Бота")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "anonim_bot")
DB_USER = os.getenv("DB_USER", "bot_user")
DB_PASSWORD = os.getenv("DB_PASSWORD")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 7683869901

# Глобальный словарь для активных задач
active_jobs = {}

async def get_db():
    return await asyncpg.connect(DATABASE_URL)

async def table_exists(conn, table_name):
    result = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = $1)",
        table_name
    )
    return result

# ===================== ФУНКЦИИ ДЛЯ ПОДПИСОК =====================
async def count_active_subscriptions():
    conn = await get_db()
    try:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'active' AND (end_date IS NULL OR end_date > NOW())"
        )
        return count
    finally:
        await conn.close()

async def get_all_subscriptions(limit: int = 100, offset: int = 0):
    conn = await get_db()
    try:
        rows = await conn.fetch("""
            SELECT s.*, u.username, u.first_name 
            FROM subscriptions s
            LEFT JOIN users u ON s.user_id = u.telegram_id
            ORDER BY s.created_at DESC
            LIMIT $1 OFFSET $2
        """, limit, offset)
        return [dict(row) for row in rows]
    finally:
        await conn.close()
# ================================================================

# ===================== ФУНКЦИИ ДЛЯ РАССЫЛКИ =====================

def get_cpu_usage():
    """Получает текущую загрузку CPU в процентах"""
    return psutil.cpu_percent(interval=0.5)

def get_memory_usage():
    """Получает текущую загрузку памяти в процентах"""
    return psutil.virtual_memory().percent

async def create_broadcast_job(message: str, user_ids: list):
    """Создаёт задание на рассылку в БД"""
    conn = await get_db()
    try:
        job_id = await conn.fetchval("""
            INSERT INTO broadcast_jobs (message, total_users, user_ids_json, status, created_at)
            VALUES ($1, $2, $3::jsonb, 'pending', NOW())
            RETURNING id
        """, message, len(user_ids), json.dumps(user_ids))
        return job_id
    finally:
        await conn.close()

async def update_job_status(job_id: int, status: str, **kwargs):
    """Обновляет статус задания"""
    conn = await get_db()
    try:
        fields = []
        values = []
        for key, val in kwargs.items():
            fields.append(f"{key} = ${len(values)+1}")
            values.append(val)
        values.append(job_id)
        if fields:
            query = f"UPDATE broadcast_jobs SET {', '.join(fields)} WHERE id = ${len(values)}"
            await conn.execute(query, *values)
        else:
            await conn.execute("UPDATE broadcast_jobs SET status = $1 WHERE id = $2", status, job_id)
    finally:
        await conn.close()

async def get_job_status(job_id: int):
    """Получает статус задания"""
    conn = await get_db()
    try:
        row = await conn.fetchrow("SELECT * FROM broadcast_jobs WHERE id = $1", job_id)
        return dict(row) if row else None
    finally:
        await conn.close()

async def send_broadcast_with_monitoring(job_id: int, bot_token: str):
    """Отправляет рассылку с мониторингом нагрузки"""
    import httpx
    
    job = await get_job_status(job_id)
    if not job:
        return
    
    user_ids = job['user_ids_json']
    message = job['message']
    total = len(user_ids)
    
    results = {
        'sent': 0,
        'failed': 0,
        'blocked': 0,
        'errors': []
    }
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "✍️ Ответить", "callback_data": "reply_anon"}],
            [{"text": "👀 Узнать кто это", "callback_data": "who_is_it", "style": "primary"}]
        ]
    }
    
    message_text = f"✉️ Вам пришло анонимное сообщение:\n\n{message}"
    
    # Обновляем статус на running
    await update_job_status(job_id, 'running', started_at=datetime.now())
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i, user_id in enumerate(user_ids):
            # Проверяем нагрузку CPU каждые 10 сообщений
            if i % 10 == 0 and i > 0:
                cpu = get_cpu_usage()
                memory = get_memory_usage()
                await update_job_status(job_id, 'running', cpu_usage=cpu, memory_usage=memory)
                
                # Если CPU > 80% — ставим на паузу
                if cpu > 80:
                    await update_job_status(job_id, 'paused', paused_at=datetime.now())
                    # Ждём пока нагрузка упадёт до 60%
                    while get_cpu_usage() > 60:
                        await asyncio.sleep(5)
                    # Возобновляем
                    await update_job_status(job_id, 'running')
            
            try:
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                payload = {
                    "chat_id": user_id,
                    "text": message_text,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard
                }
                response = await client.post(url, json=payload)
                
                if response.status_code == 200:
                    results['sent'] += 1
                else:
                    error_data = response.json()
                    error_text = str(error_data).lower()
                    
                    if any(keyword in error_text for keyword in [
                        "bot was blocked", "forbidden", "user is deactivated",
                        "bot was kicked", "chat not found", "can't send messages to user"
                    ]):
                        results['blocked'] += 1
                        try:
                            conn = await get_db()
                            user_data = await conn.fetchrow(
                                "SELECT username, first_name FROM users WHERE telegram_id = $1",
                                user_id
                            )
                            if user_data:
                                await conn.execute(
                                    "INSERT INTO blocked_users (telegram_id, username, first_name) VALUES ($1, $2, $3) ON CONFLICT (telegram_id) DO NOTHING",
                                    user_id, user_data['username'], user_data['first_name']
                                )
                                await conn.execute(
                                    "DELETE FROM users WHERE telegram_id = $1",
                                    user_id
                                )
                            await conn.close()
                        except Exception as db_error:
                            print(f"Ошибка БД при добавлении в ЧС: {db_error}")
                    else:
                        results['failed'] += 1
                        results['errors'].append(f"User {user_id}: {response.status_code}")
                        
            except Exception as e:
                results['failed'] += 1
                results['errors'].append(f"User {user_id}: {str(e)[:50]}")
            
            # Обновляем прогресс каждые 10 сообщений
            if i % 10 == 0 or i == total - 1:
                progress = ((i + 1) / total) * 100
                await update_job_status(
                    job_id, 'running',
                    sent_users=results['sent'],
                    failed_users=results['failed'],
                    blocked_users=results['blocked'],
                    progress=progress
                )
            
            if i % 30 == 0:
                await asyncio.sleep(0.1)
    
    # Завершаем задание
    await update_job_status(
        job_id, 'completed',
        sent_users=results['sent'],
        failed_users=results['failed'],
        blocked_users=results['blocked'],
        progress=100,
        finished_at=datetime.now()
    )
    
    # Удаляем из активных задач
    if job_id in active_jobs:
        del active_jobs[job_id]

# ================================================================

# ===================== HTML ШАБЛОНЫ =====================

MENU = '''
<div style="background: #0a0a1a; padding: 10px 15px; border: 1px solid #00ffcc33; border-radius: 12px 12px 0 0; display: flex; flex-wrap: wrap; gap: 5px; align-items: center; box-shadow: 0 0 30px #00ffcc11;">
    <a href="/" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #00ffcc'; this.style.boxShadow='0 0 20px #00ffcc55'; this.style.background='#00ffcc22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">📊 Дашборд</a>
    <a href="/users" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #00ffcc'; this.style.boxShadow='0 0 20px #00ffcc55'; this.style.background='#00ffcc22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">👥 Пользователи</a>
    <a href="/messages" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #00ffcc'; this.style.boxShadow='0 0 20px #00ffcc55'; this.style.background='#00ffcc22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">💬 Сообщения</a>
    <a href="/subscriptions" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #00ffcc'; this.style.boxShadow='0 0 20px #00ffcc55'; this.style.background='#00ffcc22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">📋 Подписки</a>
    <a href="/withdrawals" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #00ffcc'; this.style.boxShadow='0 0 20px #00ffcc55'; this.style.background='#00ffcc22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">💰 Снятия</a>
    <a href="/blocked" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #00ffcc'; this.style.boxShadow='0 0 20px #00ffcc55'; this.style.background='#00ffcc22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">🚫 ЧС</a>
    <a href="/settings" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #00ffcc'; this.style.boxShadow='0 0 20px #00ffcc55'; this.style.background='#00ffcc22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">⚙️ Настройки</a>
    <a href="/broadcast" style="color: #00ffcc; text-decoration: none; font-weight: 600; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid #00ffcc44; transition: all 0.3s ease; text-shadow: 0 0 20px #00ffcc44; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #00ffcc'; this.style.boxShadow='0 0 30px #00ffcc55'; this.style.background='#00ffcc22';" 
       onmouseout="this.style.border='1px solid #00ffcc44'; this.style.boxShadow='none'; this.style.background='transparent';">📢 Рассылка</a>
</div>
'''

BASE_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=yes">
    <title>Админ панель</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #0a0a1a; color: #cdd6f4; }
        .container { max-width: 1400px; margin: 0 auto; padding: 10px 15px; }
        .menu { background: #0a0a1a; border-radius: 12px; margin-bottom: 20px; border: 1px solid #00ffcc33; box-shadow: 0 0 30px #00ffcc11; overflow-x: auto; }
        .menu a { display: inline-block; padding: 10px 12px; color: #a6adc8; text-decoration: none; font-weight: 500; font-size: 13px; border-bottom: 3px solid transparent; transition: all 0.2s; white-space: nowrap; }
        .menu a:hover { color: #cdd6f4; border-bottom-color: #89b4fa; }
        .menu a.active { color: #cdd6f4; border-bottom-color: #89b4fa; font-weight: 600; }
        .page-title { font-size: 20px; font-weight: 700; margin-bottom: 15px; color: #cdd6f4; text-shadow: 0 0 40px #00ffcc11; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }
        .stat-card { background: #111128; padding: 15px 12px; border-radius: 12px; text-align: center; transition: all 0.3s ease; border: 1px solid #00ffcc22; }
        .stat-card:hover { transform: translateY(-4px); border: 1px solid #00ffcc; box-shadow: 0 0 30px #00ffcc33; background: #00ffcc11; }
        .stat-number { font-size: 28px; font-weight: 700; color: #00ffcc; text-shadow: 0 0 40px #00ffcc44; }
        .stat-label { font-size: 12px; color: #8899aa; margin-top: 4px; }
        .stat-card .icon { font-size: 24px; margin-bottom: 5px; display: block; }
        .table-container { background: #111128; border-radius: 12px; overflow-x: auto; border: 1px solid #00ffcc22; transition: all 0.3s ease; }
        .table-container:hover { border: 1px solid #00ffcc44; box-shadow: 0 0 30px #00ffcc11; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th { background: #0a0a1a; color: #00ffcc; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.3px; padding: 10px 8px; text-align: left; border-bottom: 1px solid #00ffcc22; }
        td { padding: 8px 8px; border-bottom: 1px solid #1a1a3a; font-size: 13px; color: #cdd6f4; word-break: break-word; }
        tr:hover td { background: #00ffcc11; }
        .badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; border: 1px solid transparent; white-space: nowrap; }
        .badge-text { background: #00ffcc22; color: #00ffcc; border-color: #00ffcc44; }
        .badge-photo { background: #4488ff22; color: #4488ff; border-color: #4488ff44; }
        .badge-video { background: #ff448822; color: #ff4488; border-color: #ff448844; }
        .badge-voice { background: #ffaa4422; color: #ffaa44; border-color: #ffaa4444; }
        .badge-sticker { background: #aa44ff22; color: #aa44ff; border-color: #aa44ff44; }
        .badge-video_note { background: #ff664422; color: #ff6644; border-color: #ff664444; }
        .form-container { background: #111128; padding: 20px; border-radius: 12px; border: 1px solid #00ffcc22; }
        .form-container:hover { border: 1px solid #00ffcc44; box-shadow: 0 0 30px #00ffcc11; }
        label { display: block; margin-bottom: 6px; color: #8899aa; font-weight: 500; font-size: 14px; }
        textarea, input[type="text"] {
            width: 100%; padding: 10px 14px; border-radius: 8px; border: 1px solid #00ffcc33; background: #0a0a1a; color: #cdd6f4; font-size: 14px; font-family: 'Segoe UI', Arial, sans-serif; transition: all 0.3s ease;
        }
        textarea:focus, input[type="text"]:focus { outline: none; border-color: #00ffcc; box-shadow: 0 0 30px #00ffcc22; }
        button { padding: 10px 20px; border: 1px solid #00ffcc; border-radius: 8px; background: transparent; color: #00ffcc; font-weight: 600; font-size: 14px; cursor: pointer; transition: all 0.3s ease; width: 100%; }
        button:hover { background: #00ffcc22; box-shadow: 0 0 30px #00ffcc55; transform: translateY(-2px); }
        .mt-20 { margin-top: 15px; }
        .mb-20 { margin-bottom: 15px; }
        .empty { text-align: center; padding: 30px 15px; color: #8899aa; font-size: 14px; }
        .result-box { background: #111128; padding: 20px; border-radius: 12px; border: 1px solid #00ffcc44; }
        .result-box h2 { font-size: 18px; margin-bottom: 15px; }
        .result-stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 15px; }
        .result-stats div { text-align: center; }
        .result-stats .number { font-size: 24px; font-weight: 700; color: #00ffcc; }
        .result-stats .label { color: #8899aa; font-size: 12px; }
        .result-message { background: #0a0a1a; padding: 12px; border-radius: 8px; margin-bottom: 15px; border: 1px solid #00ffcc22; word-break: break-word; font-size: 14px; }
        .glow-button { display: inline-block; padding: 10px 20px; border: 1px solid #00ffcc; border-radius: 8px; color: #00ffcc; text-decoration: none; transition: all 0.3s ease; text-align: center; width: 100%; }
        .glow-button:hover { background: #00ffcc22; box-shadow: 0 0 30px #00ffcc55; }
        .progress-bar { width: 100%; height: 20px; background: #1a1a3a; border-radius: 10px; overflow: hidden; margin: 10px 0; border: 1px solid #00ffcc33; }
        .progress-fill { height: 100%; background: linear-gradient(90deg, #00ffcc, #00ff88); transition: width 0.5s ease; border-radius: 10px; width: 0%; }
        .status-running { color: #00ffcc; }
        .status-paused { color: #ffaa44; }
        .status-completed { color: #44ff88; }
        .status-pending { color: #8899aa; }
        .status-failed { color: #ff4488; }
        .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 20px; }
        .detail-item { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #1a1a3a; }
        .detail-item .label { color: #8899aa; }
        .detail-item .value { color: #cdd6f4; font-weight: 500; }
        @media (max-width: 600px) {
            .container { padding: 8px 10px; }
            .menu a { font-size: 11px; padding: 6px 8px; }
            .stats { grid-template-columns: repeat(2, 1fr); gap: 8px; }
            .stat-card { padding: 10px 8px; }
            .stat-number { font-size: 20px; }
            .stat-label { font-size: 10px; }
            .stat-card .icon { font-size: 18px; }
            .page-title { font-size: 17px; }
            table { font-size: 11px; }
            th, td { padding: 5px 5px; }
            .badge { font-size: 9px; padding: 2px 6px; }
            button { font-size: 13px; padding: 12px; }
            .result-stats { grid-template-columns: repeat(2, 1fr); }
            .form-container { padding: 15px; }
            textarea { font-size: 13px; }
            .detail-grid { grid-template-columns: 1fr; }
        }
        @media (max-width: 400px) {
            .stats { grid-template-columns: 1fr 1fr; gap: 6px; }
            .menu a { font-size: 10px; padding: 4px 6px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="menu">MENU_PLACEHOLDER</div>
        CONTENT_PLACEHOLDER
    </div>
</body>
</html>
'''

def get_badge_class(content_type):
    types = {
        'text': 'text',
        'photo': 'photo',
        'video': 'video',
        'voice': 'voice',
        'sticker': 'sticker',
        'video_note': 'video_note'
    }
    return types.get(content_type, 'text')

# ===================== МАРШРУТЫ =====================

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    conn = await get_db()
    try:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_messages = await conn.fetchval("SELECT COUNT(*) FROM messages") if await table_exists(conn, "messages") else 0
        total_subscriptions = await count_active_subscriptions()
        total_premium = 0
        total_payments = 0
        total_revenue = "0"

        content = f'''
        <div class="page-title">📊 Дашборд</div>
        <div class="stats">
            <div class="stat-card"><span class="icon">👥</span><div class="stat-number">{total_users}</div><div class="stat-label">Пользователи</div></div>
            <div class="stat-card"><span class="icon">📋</span><div class="stat-number">{total_subscriptions}</div><div class="stat-label">Подписки</div></div>
            <div class="stat-card"><span class="icon">⭐</span><div class="stat-number">{total_premium}</div><div class="stat-label">Премиум</div></div>
            <div class="stat-card"><span class="icon">💬</span><div class="stat-number">{total_messages}</div><div class="stat-label">Сообщений</div></div>
            <div class="stat-card"><span class="icon">💳</span><div class="stat-number">{total_payments}</div><div class="stat-label">Платежей ({total_revenue} ₽)</div></div>
        </div>
        '''
        menu = MENU.replace('📊 Дашборд', '<span style="color:#00ffcc; text-shadow: 0 0 30px #00ffcc44;">📊 Дашборд</span>')
        return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', content))
    finally:
        await conn.close()

@app.get("/users", response_class=HTMLResponse)
async def users_page():
    conn = await get_db()
    try:
        rows = await conn.fetch("SELECT * FROM users ORDER BY registered_at DESC LIMIT 100")
        total = len(rows)
        
        html = f'<div class="page-title">👥 Пользователи</div>'
        html += f'<div class="stats" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));">'
        html += f'<div class="stat-card"><div class="stat-number">{total}</div><div class="stat-label">Всего пользователей</div></div></div>'
        
        if rows:
            html += '<div class="table-container"><table><tr><th>ID</th><th>Username</th><th>Имя</th><th>Регистрация</th></tr>'
            for row in rows:
                html += f"<tr><td>{row['telegram_id']}</td><td>{row.get('username') or '-'}</td><td>{row.get('first_name') or '-'}</td><td>{row.get('registered_at').strftime('%d.%m.%Y %H:%M') if row.get('registered_at') else '-'}</td></tr>"
            html += '</table></div>'
        else:
            html += '<div class="empty">😕 Пользователей пока нет</div>'
            
        menu = MENU.replace('👥 Пользователи', '<span style="color:#00ffcc; text-shadow: 0 0 30px #00ffcc44;">👥 Пользователи</span>')
        return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))
    finally:
        await conn.close()

@app.get("/messages", response_class=HTMLResponse)
async def messages_page():
    conn = await get_db()
    try:
        rows = await conn.fetch("""
            SELECT m.id, m.content, m.content_type, m.created_at,
                   s.first_name as sender_name, s.username as sender_username,
                   r.first_name as recipient_name, r.username as recipient_username
            FROM messages m
            LEFT JOIN users s ON m.sender_id = s.telegram_id
            LEFT JOIN users r ON m.recipient_id = r.telegram_id
            ORDER BY m.created_at DESC LIMIT 100
        """)

        html = '<div class="page-title">💬 Сообщения</div>'
        if rows:
            html += '<div class="table-container"><table><tr><th>ID</th><th>От</th><th>Кому</th><th>Тип</th><th>Сообщение</th><th>Дата</th></tr>'
            for row in rows:
                sender = row.get('sender_username') or row.get('sender_name') or 'Неизвестно'
                recipient = row.get('recipient_username') or row.get('recipient_name') or 'Неизвестно'
                badge_class = get_badge_class(row['content_type'])
                html += f'''
                <tr>
                    <td>{row['id']}</td>
                    <td>@{sender}</td>
                    <td>@{recipient}</td>
                    <td><span class="badge badge-{badge_class}">{row['content_type']}</span></td>
                    <td>{row['content'][:50]}{'...' if len(row['content'] or '') > 50 else ''}</td>
                    <td>{row['created_at'].strftime('%d.%m.%Y %H:%M') if row['created_at'] else '-'}</td>
                </tr>
                '''
            html += '</table></div>'
        else:
            html += '<div class="empty">😕 Сообщений пока нет</div>'
            
        menu = MENU.replace('💬 Сообщения', '<span style="color:#00ffcc; text-shadow: 0 0 30px #00ffcc44;">💬 Сообщения</span>')
        return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))
    finally:
        await conn.close()

@app.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions_page():
    conn = await get_db()
    try:
        rows = await get_all_subscriptions(limit=200)
        total_active = await count_active_subscriptions()
        
        html = f'<div class="page-title">📋 Подписки</div>'
        html += f'<div class="stats" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));">'
        html += f'<div class="stat-card"><div class="stat-number">{total_active}</div><div class="stat-label">Активных подписок</div></div></div>'
        
        if rows:
            html += '<div class="table-container"><table><tr><th>ID</th><th>Пользователь</th><th>План</th><th>Начало</th><th>Окончание</th><th>Статус</th></tr>'
            for row in rows:
                status_color = "#00ffcc" if row['status'] == 'active' else "#ff4488"
                end_date_str = row['end_date'].strftime('%d.%m.%Y') if row['end_date'] else '∞'
                html += f'''
                <tr>
                    <td>{row['id']}</td>
                    <td>@{row.get('username') or row.get('first_name') or row['user_id']}</td>
                    <td>{row['plan']}</td>
                    <td>{row['start_date'].strftime('%d.%m.%Y') if row['start_date'] else '-'}</td>
                    <td>{end_date_str}</td>
                    <td><span style="color:{status_color};">{row['status']}</span></td>
                </tr>
                '''
            html += '</table></div>'
        else:
            html += '<div class="empty">😕 Подписок пока нет</div>'
            
        menu = MENU.replace('📋 Подписки', '<span style="color:#00ffcc; text-shadow: 0 0 30px #00ffcc44;">📋 Подписки</span>')
        return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))
    finally:
        await conn.close()

@app.get("/blocked", response_class=HTMLResponse)
async def blocked_page():
    conn = await get_db()
    try:
        rows = await conn.fetch("SELECT * FROM blocked_users ORDER BY blocked_at DESC")
        total = len(rows)
        
        html = f'<div class="page-title">🚫 Чёрный список</div>'
        html += f'<div class="stats" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));">'
        html += f'<div class="stat-card"><div class="stat-number">{total}</div><div class="stat-label">Заблокировали бота</div></div></div>'
        
        if rows:
            html += '<div class="table-container"><table><tr><th>ID</th><th>Username</th><th>Имя</th><th>Заблокирован</th></tr>'
            for row in rows:
                html += f"<tr><td>{row['telegram_id']}</td><td>{row.get('username') or '-'}</td><td>{row.get('first_name') or '-'}</td><td>{row['blocked_at'].strftime('%d.%m.%Y %H:%M') if row['blocked_at'] else '-'}</td></tr>"
            html += '</table></div>'
        else:
            html += '<div class="empty">😕 Пользователей в ЧС нет</div>'
            
        menu = MENU.replace('🚫 ЧС', '<span style="color:#00ffcc; text-shadow: 0 0 30px #00ffcc44;">🚫 ЧС</span>')
        return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))
    finally:
        await conn.close()

@app.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request):
    conn = await get_db()
    try:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        job_id = request.query_params.get("job")
        job_data = None
        
        if job_id:
            try:
                job_id = int(job_id)
                job_data = await get_job_status(job_id)
            except:
                pass
        
        result_html = ""
        
        if job_data:
            total = job_data['total_users'] or 0
            sent = job_data['sent_users'] or 0
            failed = job_data['failed_users'] or 0
            blocked = job_data['blocked_users'] or 0
            progress = job_data['progress'] or 0
            remaining = total - sent - failed - blocked
            
            status_text = job_data['status']
            status_class = f"status-{status_text}"
            status_labels = {
                'pending': '⏳ Ожидание',
                'running': '🔄 Выполняется',
                'paused': '⏸️ Приостановлено',
                'completed': '✅ Завершено',
                'failed': '❌ Ошибка'
            }
            status_label = status_labels.get(status_text, status_text)
            
            result_html = f'''
            <div class="result-box" style="border-color: #00ffcc44; margin-top: 20px;">
                <h2 style="color: #00ffcc;">📊 Статистика рассылки - {status_label}</h2>
                <div class="result-stats">
                    <div><div class="number">{total}</div><div class="label">Всего пользователей</div></div>
                    <div><div class="number" style="color: #00ffcc;">{sent}</div><div class="label">Успешно отправлено<br><span style="font-size:11px; color:#8899aa;">{f"{sent/total*100:.1f}%" if total > 0 else "0%"}</span></div></div>
                    <div><div class="number" style="color: #ff4488;">{failed}</div><div class="label">Ошибок<br><span style="font-size:11px; color:#8899aa;">{f"{failed/total*100:.1f}%" if total > 0 else "0%"}</span></div></div>
                    <div><div class="number" style="color: #ffaa44;">{remaining}</div><div class="label">Осталось обработать<br><span style="font-size:11px; color:#8899aa;">{f"{remaining/total*100:.1f}%" if total > 0 else "0%"}</span></div></div>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width:{progress}%;"></div>
                </div>
                <p style="text-align:center; color:#8899aa; font-size:13px; margin-bottom:15px;">{progress:.2f}% - Обработано: {sent + failed + blocked} из {total}</p>
                
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-top: 15px;">
                    <div style="background: #0a0a1a; padding: 15px; border-radius: 8px; border: 1px solid #00ffcc22;">
                        <h4 style="color: #00ffcc; margin-bottom: 10px; font-size: 14px;">📋 Детали рассылки</h4>
                        <div class="detail-grid">
                            <div class="detail-item"><span class="label">Статус</span><span class="value {status_class}">{status_label}</span></div>
                            <div class="detail-item"><span class="label">Тип сообщения</span><span class="value">Текстовое</span></div>
                            <div class="detail-item"><span class="label">Прогресс</span><span class="value">{progress:.1f}% ({sent + failed + blocked}/{total})</span></div>
                            <div class="detail-item"><span class="label">ID очереди</span><span class="value">{job_data['id']}</span></div>
                        </div>
                    </div>
                    <div style="background: #0a0a1a; padding: 15px; border-radius: 8px; border: 1px solid #00ffcc22;">
                        <h4 style="color: #00ffcc; margin-bottom: 10px; font-size: 14px;">⏱️ Время выполнения</h4>
                        <div class="detail-grid">
                            <div class="detail-item"><span class="label">Запущена</span><span class="value">{job_data['started_at'].strftime('%d.%m.%Y, %H:%M:%S') if job_data['started_at'] else '-'}</span></div>
                            <div class="detail-item"><span class="label">Завершена</span><span class="value">{job_data['finished_at'].strftime('%d.%m.%Y, %H:%M:%S') if job_data['finished_at'] else '-'}</span></div>
                        </div>
                    </div>
                </div>
                <div style="background: #0a0a1a; padding: 15px; border-radius: 8px; margin-top: 15px; border: 1px solid #00ffcc22;">
                    <h4 style="color: #00ffcc; margin-bottom: 10px; font-size: 14px;">💻 Системная информация</h4>
                    <div class="detail-grid">
                        <div class="detail-item"><span class="label">Память</span><span class="value">{job_data.get('memory_usage', 0):.1f} MB</span></div>
                        <div class="detail-item"><span class="label">Пик памяти</span><span class="value">{job_data.get('memory_usage', 0):.1f} MB</span></div>
                        <div class="detail-item"><span class="label">CPU</span><span class="value">{job_data.get('cpu_usage', 0):.1f}%</span></div>
                        <div class="detail-item"><span class="label">Активных пользователей</span><span class="value">{total}</span></div>
                    </div>
                </div>
                <div style="margin-top: 15px; display: flex; gap: 10px; flex-wrap: wrap;">
                    <a href="/broadcast" class="glow-button" style="flex:1;">⬅️ Новая рассылка</a>
                    <a href="/broadcast/list" class="glow-button" style="flex:1; border-color: #4488ff; color: #4488ff;">📋 Все рассылки</a>
                </div>
            </div>
            '''
        
        content = f'''
        <div class="page-title">📢 Рассылка</div>
        <div class="stats" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));">
            <div class="stat-card"><span class="icon">👥</span><div class="stat-number">{total_users}</div><div class="stat-label">Всего пользователей</div></div>
        </div>
        <div class="form-container">
            <form action="/broadcast/send" method="post" id="broadcastForm">
                <label for="message">📝 Текст сообщения (HTML поддерживается):</label>
                <textarea id="message" name="message" rows="5" placeholder="Введите текст рассылки...&#10;Можно использовать HTML: &lt;b&gt;жирный&lt;/b&gt;, &lt;i&gt;курсив&lt;/i&gt;"></textarea>
                <div class="mt-20" style="display: flex; flex-direction: column; gap: 10px;">
                    <button type="submit" id="sendBtn">🚀 Запустить рассылку</button>
                    <span style="color: #8899aa; font-size: 13px; text-align: center;">Будет отправлено <strong style="color: #00ffcc;">{total_users}</strong> пользователям</span>
                    <span style="color: #ffaa44; font-size: 12px; text-align: center;">⚡ При загрузке CPU > 80% рассылка автоматически приостанавливается</span>
                </div>
            </form>
        </div>
        {result_html}
        <script>
            document.getElementById('broadcastForm').addEventListener('submit', function(e) {{
                var btn = document.getElementById('sendBtn');
                btn.disabled = true;
                btn.textContent = '⏳ Запуск...';
                setTimeout(function() {{
                    btn.disabled = false;
                    btn.textContent = '🚀 Запустить рассылку';
                }}, 3000);
            }});
        </script>
        '''
        
        menu = MENU.replace('📢 Рассылка', '<span style="color:#00ffcc; text-shadow: 0 0 30px #00ffcc44;">📢 Рассылка</span>')
        return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', content))
    finally:
        await conn.close()

@app.post("/broadcast/send")
async def send_broadcast(request: Request, message: str = Form(...)):
    if not message or len(message.strip()) == 0:
        return RedirectResponse(url="/broadcast?error=empty", status_code=303)
    
    conn = await get_db()
    try:
        # Фиксируем список пользователей на момент запуска
        rows = await conn.fetch("SELECT telegram_id FROM users")
        user_ids = [row['telegram_id'] for row in rows]
        
        if not user_ids:
            return RedirectResponse(url="/broadcast?error=no_users", status_code=303)
        
        # Создаём задание в БД
        job_id = await create_broadcast_job(message, user_ids)
        
        # Запускаем рассылку в фоновом потоке
        def run_broadcast():
            asyncio.run(send_broadcast_with_monitoring(job_id, BOT_TOKEN))
        
        thread = threading.Thread(target=run_broadcast)
        thread.daemon = True
        thread.start()
        
        return RedirectResponse(url=f"/broadcast?job={job_id}", status_code=303)
    finally:
        await conn.close()

@app.get("/broadcast/list", response_class=HTMLResponse)
async def broadcast_list():
    conn = await get_db()
    try:
        rows = await conn.fetch("""
            SELECT * FROM broadcast_jobs 
            ORDER BY created_at DESC 
            LIMIT 50
        """)
        
        html = '<div class="page-title">📋 История рассылок</div>'
        
        if rows:
            html += '<div class="table-container"><table><tr><th>ID</th><th>Статус</th><th>Всего</th><th>Отправлено</th><th>Ошибок</th><th>Прогресс</th><th>Создана</th><th>Действие</th></tr>'
            for row in rows:
                status_color = {
                    'pending': '#8899aa',
                    'running': '#00ffcc',
                    'paused': '#ffaa44',
                    'completed': '#44ff88',
                    'failed': '#ff4488'
                }.get(row['status'], '#8899aa')
                
                status_labels = {
                    'pending': '⏳ Ожидание',
                    'running': '🔄 Выполняется',
                    'paused': '⏸️ Пауза',
                    'completed': '✅ Завершено',
                    'failed': '❌ Ошибка'
                }.get(row['status'], row['status'])
                
                html += f'''
                <tr>
                    <td>{row['id']}</td>
                    <td><span style="color:{status_color};">{status_labels}</span></td>
                    <td>{row['total_users']}</td>
                    <td>{row['sent_users']}</td>
                    <td>{row['failed_users']}</td>
                    <td>{row['progress']:.1f}%</td>
                    <td>{row['created_at'].strftime('%d.%m.%Y %H:%M') if row['created_at'] else '-'}</td>
                    <td><a href="/broadcast?job={row['id']}" style="color:#00ffcc;">Подробнее</a></td>
                </tr>
                '''
            html += '</table></div>'
        else:
            html += '<div class="empty">😕 Рассылок пока нет</div>'
        
        menu = MENU.replace('📢 Рассылка', '<span style="color:#00ffcc; text-shadow: 0 0 30px #00ffcc44;">📢 Рассылка</span>')
        return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))
    finally:
        await conn.close()

@app.get("/withdrawals", response_class=HTMLResponse)
async def withdrawals_page():
    html = '<div class="page-title">💰 Система снятий</div><div class="empty">🔧 Раздел в разработке</div>'
    menu = MENU.replace('💰 Снятия', '<span style="color:#00ffcc; text-shadow: 0 0 30px #00ffcc44;">💰 Снятия</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    html = '<div class="page-title">⚙️ Настройки</div><div class="empty">🔧 Раздел в разработке</div>'
    menu = MENU.replace('⚙️ Настройки', '<span style="color:#00ffcc; text-shadow: 0 0 30px #00ffcc44;">⚙️ Настройки</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)