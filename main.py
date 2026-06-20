import os
import asyncio
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

async def get_db():
    return await asyncpg.connect(DATABASE_URL)

async def table_exists(conn, table_name):
    result = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = $1)",
        table_name
    )
    return result

broadcast_results = {}

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

async def send_broadcast_message(bot_token: str, user_ids: list, text: str):
    import httpx
    
    results = {
        'total': len(user_ids),
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
    
    message_text = f"✉️ Вам пришло анонимное сообщение:\n\n{text}"
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i, user_id in enumerate(user_ids):
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
                        "bot was blocked", 
                        "forbidden", 
                        "user is deactivated", 
                        "bot was kicked", 
                        "chat not found",
                        "can't send messages to user"
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
                                results['errors'].append(f"User {user_id} moved to blacklist")
                            await conn.close()
                        except Exception as db_error:
                            print(f"Ошибка БД при добавлении в ЧС: {db_error}")
                    else:
                        results['failed'] += 1
                        results['errors'].append(f"User {user_id}: {response.status_code}")
                        
            except Exception as e:
                results['failed'] += 1
                results['errors'].append(f"User {user_id}: {str(e)[:50]}")
            
            if i % 30 == 0:
                await asyncio.sleep(0.1)
    
    return results

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    conn = await get_db()
    try:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_messages = await conn.fetchval("SELECT COUNT(*) FROM messages") if await table_exists(conn, "messages") else 0
        total_premium = 0
        total_payments = 0
        total_revenue = "0"

        content = f'''
        <div class="page-title">📊 Дашборд</div>
        <div class="stats">
            <div class="stat-card"><span class="icon">👥</span><div class="stat-number">{total_users}</div><div class="stat-label">Пользователи</div></div>
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
        
        result_id = request.query_params.get("result")
        result_html = ""
        
        if result_id and result_id in broadcast_results:
            results = broadcast_results.pop(result_id)
            status_color = "#00ffcc" if results['failed'] == 0 else "#ff8844"
            result_html = f'''
            <div class="result-box" style="border-color: {status_color}44; margin-top: 20px;">
                <h2 style="color: {status_color};">{'✅ Рассылка завершена!' if results['failed'] == 0 else '⚠️ Рассылка завершена с ошибками'}</h2>
                <div class="result-stats">
                    <div><div class="number">{results['total']}</div><div class="label">Всего</div></div>
                    <div><div class="number" style="color: #00ffcc;">{results['sent']}</div><div class="label">✅ Доставлено</div></div>
                    <div><div class="number" style="color: #ff4488;">{results['failed']}</div><div class="label">❌ Ошибок</div></div>
                    <div><div class="number" style="color: #ffaa44;">{results['blocked']}</div><div class="label">🚫 В ЧС</div></div>
                </div>
                <div class="result-message">
                    <p style="color: #8899aa; font-size: 13px; margin-bottom: 5px;">📝 Текст:</p>
                    <p style="color: #cdd6f4;">{results['text']}</p>
                </div>
                <a href="/broadcast" class="glow-button">⬅️ Новая рассылка</a>
            </div>
            '''
        
        content = f'''
        <div class="page-title">📢 Рассылка</div>
        <div class="stats" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));">
            <div class="stat-card"><span class="icon">👥</span><div class="stat-number">{total_users}</div><div class="stat-label">Получателей</div></div>
        </div>
        <div class="form-container">
            <form action="/broadcast/send" method="post" id="broadcastForm">
                <label for="message">📝 Текст сообщения (HTML поддерживается):</label>
                <textarea id="message" name="message" rows="5" placeholder="Введите текст рассылки...&#10;Можно использовать HTML: &lt;b&gt;жирный&lt;/b&gt;, &lt;i&gt;курсив&lt;/i&gt;"></textarea>
                <div class="mt-20" style="display: flex; flex-direction: column; gap: 10px;">
                    <button type="submit" id="sendBtn">🚀 Отправить рассылку</button>
                    <span style="color: #8899aa; font-size: 13px; text-align: center;">Будет отправлено <strong style="color: #00ffcc;">{total_users}</strong> пользователям</span>
                </div>
            </form>
        </div>
        {result_html}
        <script>
            document.getElementById('broadcastForm').addEventListener('submit', function(e) {{
                var btn = document.getElementById('sendBtn');
                btn.disabled = true;
                btn.textContent = '⏳ Отправка...';
                setTimeout(function() {{
                    btn.disabled = false;
                    btn.textContent = '🚀 Отправить рассылку';
                }}, 5000);
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
        rows = await conn.fetch("SELECT telegram_id FROM users")
        user_ids = [row['telegram_id'] for row in rows]
        
        results = await send_broadcast_message(BOT_TOKEN, user_ids, message)
        results['text'] = message
        
        import uuid
        result_id = str(uuid.uuid4())[:8]
        broadcast_results[result_id] = results
        
        return RedirectResponse(url=f"/broadcast?result={result_id}", status_code=303)
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