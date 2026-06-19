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

# Хранилище результатов рассылки (в памяти)
broadcast_results = {}

# --- HTML ШАБЛОНЫ ---

MENU = '''
<div style="background: #0a0a1a; padding: 15px 30px; border: 1px solid #00ffcc33; border-radius: 12px 12px 0 0; display: flex; gap: 30px; align-items: center; box-shadow: 0 0 30px #00ffcc11;">
    <a href="/" style="color: #88f0e0; text-decoration: none; font-weight: 600; font-size: 16px; padding: 8px 20px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease;" 
       onmouseover="this.style.border='1px solid #00ffcc'; this.style.boxShadow='0 0 20px #00ffcc55'; this.style.background='#00ffcc22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">📊 Дашборд</a>
    <a href="/users" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 16px; padding: 8px 20px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease;" 
       onmouseover="this.style.border='1px solid #00ffcc'; this.style.boxShadow='0 0 20px #00ffcc55'; this.style.background='#00ffcc22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">👥 Пользователи</a>
    <a href="/messages" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 16px; padding: 8px 20px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease;" 
       onmouseover="this.style.border='1px solid #00ffcc'; this.style.boxShadow='0 0 20px #00ffcc55'; this.style.background='#00ffcc22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">💬 Сообщения</a>
    <a href="/withdrawals" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 16px; padding: 8px 20px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease;" 
       onmouseover="this.style.border='1px solid #00ffcc'; this.style.boxShadow='0 0 20px #00ffcc55'; this.style.background='#00ffcc22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">💰 Система снятий</a>
    <a href="/settings" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 16px; padding: 8px 20px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease;" 
       onmouseover="this.style.border='1px solid #00ffcc'; this.style.boxShadow='0 0 20px #00ffcc55'; this.style.background='#00ffcc22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">⚙️ Настройки</a>
    <a href="/broadcast" style="color: #00ffcc; text-decoration: none; font-weight: 600; font-size: 16px; padding: 8px 20px; border-radius: 8px; border: 1px solid #00ffcc44; transition: all 0.3s ease; text-shadow: 0 0 20px #00ffcc44;" 
       onmouseover="this.style.border='1px solid #00ffcc'; this.style.boxShadow='0 0 30px #00ffcc55'; this.style.background='#00ffcc22';" 
       onmouseout="this.style.border='1px solid #00ffcc44'; this.style.boxShadow='none'; this.style.background='transparent';">📢 Рассылка</a>
</div>
'''

BASE_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Админ панель</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #0a0a1a; color: #cdd6f4; }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px 30px; }
        .menu { background: #0a0a1a; border-radius: 12px; margin-bottom: 25px; border: 1px solid #00ffcc33; box-shadow: 0 0 30px #00ffcc11; }
        .page-title { font-size: 24px; font-weight: 700; margin-bottom: 25px; color: #cdd6f4; text-shadow: 0 0 40px #00ffcc11; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .stat-card { background: #111128; padding: 25px 20px; border-radius: 12px; text-align: center; transition: all 0.3s ease; border: 1px solid #00ffcc22; }
        .stat-card:hover { transform: translateY(-4px); border: 1px solid #00ffcc; box-shadow: 0 0 30px #00ffcc33; background: #00ffcc11; }
        .stat-number { font-size: 32px; font-weight: 700; color: #00ffcc; text-shadow: 0 0 40px #00ffcc44; }
        .stat-label { font-size: 14px; color: #8899aa; margin-top: 5px; }
        .stat-card .icon { font-size: 28px; margin-bottom: 8px; display: block; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #1a1a3a; padding: 10px; text-align: left; }
        th { color: #00ffcc; background: #0a0a1a; }
        .badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; border: 1px solid transparent; }
        .badge-text { background: #00ffcc22; color: #00ffcc; border-color: #00ffcc44; }
        .badge-photo { background: #4488ff22; color: #4488ff; border-color: #4488ff44; }
        .badge-video { background: #ff448822; color: #ff4488; border-color: #ff448844; }
        .badge-voice { background: #ffaa4422; color: #ffaa44; border-color: #ffaa4444; }
        .badge-sticker { background: #aa44ff22; color: #aa44ff; border-color: #aa44ff44; }
        .badge-video_note { background: #ff664422; color: #ff6644; border-color: #ff664444; }
        .form-container { background: #111128; padding: 30px; border-radius: 12px; border: 1px solid #00ffcc22; }
        .form-container:hover { border: 1px solid #00ffcc44; box-shadow: 0 0 30px #00ffcc11; }
        label { display: block; margin-bottom: 8px; color: #8899aa; font-weight: 500; }
        textarea, input[type="text"] {
            width: 100%; padding: 12px 16px; border-radius: 8px; border: 1px solid #00ffcc33; background: #0a0a1a; color: #cdd6f4; font-size: 15px; font-family: 'Segoe UI', Arial, sans-serif; transition: all 0.3s ease;
        }
        textarea:focus, input[type="text"]:focus { outline: none; border-color: #00ffcc; box-shadow: 0 0 30px #00ffcc22; }
        button { padding: 12px 32px; border: 1px solid #00ffcc; border-radius: 8px; background: transparent; color: #00ffcc; font-weight: 600; font-size: 16px; cursor: pointer; transition: all 0.3s ease; }
        button:hover { background: #00ffcc22; box-shadow: 0 0 30px #00ffcc55; transform: translateY(-2px); }
        .mt-20 { margin-top: 20px; }
        .mb-20 { margin-bottom: 20px; }
        .empty { text-align: center; padding: 40px; color: #8899aa; font-size: 16px; }
        .result-box { background: #111128; padding: 30px; border-radius: 12px; border: 1px solid #00ffcc44; }
        .result-box h2 { margin-bottom: 20px; }
        .result-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; margin-bottom: 20px; }
        .result-stats div { text-align: center; }
        .result-stats .number { font-size: 28px; font-weight: 700; color: #00ffcc; }
        .result-stats .label { color: #8899aa; font-size: 14px; }
        .result-message { background: #0a0a1a; padding: 15px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #00ffcc22; }
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

# --- ФУНКЦИЯ ДЛЯ ОТПРАВКИ РАССЫЛКИ ---

async def send_broadcast_message(bot_token: str, user_ids: list, text: str):
    import httpx
    
    results = {
        'total': len(user_ids),
        'sent': 0,
        'failed': 0,
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
                    results['failed'] += 1
                    results['errors'].append(f"User {user_id}: {response.status_code}")
            except Exception as e:
                results['failed'] += 1
                results['errors'].append(f"User {user_id}: {str(e)[:50]}")
            
            if i % 30 == 0:
                await asyncio.sleep(0.1)
    
    return results

# --- МАРШРУТЫ ---

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
            html += '<div class="table-container"><table><tr><th>ID</th><th>Username</th><th>Имя</th><th>Зарегистрирован</th></tr>'
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
            html += '<div class="table-container"><table><tr><th>ID</th><th>От кого</th><th>Кому</th><th>Тип</th><th>Сообщение</th><th>Дата</th></tr>'
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
                    <td>{row['content'][:80]}{'...' if len(row['content'] or '') > 80 else ''}</td>
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

@app.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request):
    conn = await get_db()
    try:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        
        # Проверяем, есть ли результат в сессии (передаём через параметр result)
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
                </div>
                <div class="result-message">
                    <p style="color: #8899aa; font-size: 14px; margin-bottom: 5px;">📝 Текст сообщения:</p>
                    <p style="color: #cdd6f4;">{results['text']}</p>
                </div>
                <a href="/broadcast" style="display: inline-block; padding: 12px 24px; border: 1px solid #00ffcc; border-radius: 8px; color: #00ffcc; text-decoration: none; transition: all 0.3s ease;"
                   onmouseover="this.style.background='#00ffcc22'; this.style.boxShadow='0 0 30px #00ffcc55';"
                   onmouseout="this.style.background='transparent'; this.style.boxShadow='none';">⬅️ Новая рассылка</a>
            </div>
            '''
        
        content = f'''
        <div class="page-title">📢 Рассылка</div>
        <div class="stats" style="grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));">
            <div class="stat-card"><span class="icon">👥</span><div class="stat-number">{total_users}</div><div class="stat-label">Получателей</div></div>
        </div>
        <div class="form-container">
            <form action="/broadcast/send" method="post" id="broadcastForm">
                <label for="message">📝 Текст сообщения (HTML поддерживается):</label>
                <textarea id="message" name="message" rows="6" placeholder="Введите текст рассылки...&#10;Можно использовать HTML: &lt;b&gt;жирный&lt;/b&gt;, &lt;i&gt;курсив&lt;/i&gt;"></textarea>
                <div class="mt-20" style="display: flex; gap: 15px; align-items: center; flex-wrap: wrap;">
                    <button type="submit" id="sendBtn">🚀 Отправить рассылку</button>
                    <span style="color: #8899aa; font-size: 14px;">Будет отправлено <strong style="color: #00ffcc;">{total_users}</strong> пользователям</span>
                </div>
            </form>
        </div>
        {result_html}
        <script>
            // Предотвращаем повторную отправку формы
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
        
        # Сохраняем результат во временном хранилище
        import uuid
        result_id = str(uuid.uuid4())[:8]
        broadcast_results[result_id] = results
        
        return RedirectResponse(url=f"/broadcast?result={result_id}", status_code=303)
    finally:
        await conn.close()

@app.get("/withdrawals", response_class=HTMLResponse)
async def withdrawals_page():
    html = '<div class="page-title">💰 Система снятий</div><div class="empty">🔧 Раздел в разработке</div>'
    menu = MENU.replace('💰 Система снятий', '<span style="color:#00ffcc; text-shadow: 0 0 30px #00ffcc44;">💰 Система снятий</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    html = '<div class="page-title">⚙️ Настройки</div><div class="empty">🔧 Раздел в разработке</div>'
    menu = MENU.replace('⚙️ Настройки', '<span style="color:#00ffcc; text-shadow: 0 0 30px #00ffcc44;">⚙️ Настройки</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)