import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
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

async def get_db():
    return await asyncpg.connect(DATABASE_URL)

MENU = '''
<div style="background: #2c3e50; padding: 15px; margin-bottom: 20px; border-radius: 8px;">
    <a href="/" style="color: white; text-decoration: none; margin: 0 15px; font-weight: bold;">📊 Дашборд</a>
    <a href="/users" style="color: white; text-decoration: none; margin: 0 15px;">👥 Пользователи</a>
    <a href="/messages" style="color: white; text-decoration: none; margin: 0 15px;">💬 Сообщения</a>
    <a href="/withdrawals" style="color: white; text-decoration: none; margin: 0 15px;">💰 Система снятий</a>
    <a href="/settings" style="color: white; text-decoration: none; margin: 0 15px;">⚙️ Настройки</a>
    <a href="/broadcast" style="color: white; text-decoration: none; margin: 0 15px;">📢 Рассылка</a>
</div>
'''

BASE_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Админ панель</title>
    <style>
        body { font-family: Arial, sans-serif; padding: 20px; margin: 0; background: #f4f6f9; }
        .container { max-width: 1200px; margin: 0 auto; }
        .card { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .number { font-size: 48px; font-weight: bold; color: #007bff; }
        .menu a:hover { color: #f39c12; }
        table { border-collapse: collapse; width: 100%; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
        th { background: #007bff; color: white; }
        tr:nth-child(even) { background: #f9f9f9; }
        .stats { display: flex; gap: 20px; flex-wrap: wrap; }
        .stat-card { background: white; padding: 20px; border-radius: 10px; flex: 1; min-width: 150px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); text-align: center; }
        .stat-number { font-size: 32px; font-weight: bold; color: #007bff; }
        .badge {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
            color: white;
        }
        .badge-text { background: #28a745; }
        .badge-photo { background: #17a2b8; }
        .badge-video { background: #dc3545; }
        .badge-voice { background: #ffc107; color: #333; }
        .badge-sticker { background: #6f42c1; }
        .badge-video_note { background: #fd7e14; }
    </style>
</head>
<body>
    <div class="container">
        MENU_PLACEHOLDER
        CONTENT_PLACEHOLDER
    </div>
</body>
</html>
'''

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    conn = await get_db()
    try:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_messages = await conn.fetchval("SELECT COUNT(*) FROM messages") if await table_exists(conn, "messages") else 0
        total_withdrawals = 0
        total_premium = 0

        content = f'''
        <h1>📊 Дашборд</h1>
        <div class="stats">
            <div class="stat-card"><h3>Всего пользователей</h3><div class="stat-number">{total_users}</div></div>
            <div class="stat-card"><h3>💬 Сообщений</h3><div class="stat-number">{total_messages}</div></div>
            <div class="stat-card"><h3>💰 Снятий</h3><div class="stat-number">{total_withdrawals}</div></div>
            <div class="stat-card"><h3>⭐ Премиум</h3><div class="stat-number">{total_premium}</div></div>
        </div>
        '''
        html = BASE_HTML.replace('MENU_PLACEHOLDER', MENU).replace('CONTENT_PLACEHOLDER', content)
        return HTMLResponse(content=html)
    finally:
        await conn.close()

@app.get("/users", response_class=HTMLResponse)
async def users_page():
    conn = await get_db()
    try:
        rows = await conn.fetch("SELECT * FROM users ORDER BY registered_at DESC LIMIT 100")
        html = '<h1>👥 Пользователи</h1><table><tr><th>ID</th><th>Username</th><th>Имя</th><th>Зарегистрирован</th></tr>'
        for row in rows:
            html += f"<tr><td>{row['telegram_id']}</td><td>{row.get('username') or '-'}</td><td>{row.get('first_name') or '-'}</td><td>{row.get('registered_at') or '-'}</td></tr>"
        html += '</table>'
        return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', MENU).replace('CONTENT_PLACEHOLDER', html))
    finally:
        await conn.close()

@app.get("/messages", response_class=HTMLResponse)
async def messages_page():
    conn = await get_db()
    try:
        rows = await conn.fetch("""
            SELECT 
                m.id,
                m.content,
                m.content_type,
                m.created_at,
                s.first_name as sender_name,
                s.username as sender_username,
                r.first_name as recipient_name,
                r.username as recipient_username
            FROM messages m
            LEFT JOIN users s ON m.sender_id = s.telegram_id
            LEFT JOIN users r ON m.recipient_id = r.telegram_id
            ORDER BY m.created_at DESC
            LIMIT 100
        """)

        html = '''
        <h1>💬 Сообщения</h1>
        <table>
            <tr>
                <th>ID</th>
                <th>От кого</th>
                <th>Кому</th>
                <th>Тип</th>
                <th>Сообщение</th>
                <th>Дата</th>
            </tr>
        '''
        for row in rows:
            sender = row.get('sender_username') or row.get('sender_name') or 'Неизвестно'
            recipient = row.get('recipient_username') or row.get('recipient_name') or 'Неизвестно'
            
            # Цветная метка для типа сообщения
            badge_class = "badge-text"
            if row['content_type'] == 'photo':
                badge_class = "badge-photo"
            elif row['content_type'] == 'video':
                badge_class = "badge-video"
            elif row['content_type'] == 'voice':
                badge_class = "badge-voice"
            elif row['content_type'] == 'sticker':
                badge_class = "badge-sticker"
            elif row['content_type'] == 'video_note':
                badge_class = "badge-video_note"
            
            html += f'''
            <tr>
                <td>{row['id']}</td>
                <td>@{sender}</td>
                <td>@{recipient}</td>
                <td><span class="badge {badge_class}">{row['content_type']}</span></td>
                <td>{row['content'][:100]}{'...' if len(row['content'] or '') > 100 else ''}</td>
                <td>{row['created_at'].strftime('%d.%m.%Y %H:%M') if row['created_at'] else '-'}</td>
            </tr>
            '''
        html += '</table>'
        return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', MENU).replace('CONTENT_PLACEHOLDER', html))
    finally:
        await conn.close()

@app.get("/withdrawals", response_class=HTMLResponse)
async def withdrawals_page():
    content = '<h1>💰 Система снятий</h1><p>Здесь будет список заявок на вывод.</p>'
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', MENU).replace('CONTENT_PLACEHOLDER', content))

@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    content = '<h1>⚙️ Настройки</h1><p>Здесь будут настройки бота.</p>'
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', MENU).replace('CONTENT_PLACEHOLDER', content))

@app.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page():
    content = '<h1>📢 Рассылка</h1><p>Здесь будет форма для рассылки.</p>'
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', MENU).replace('CONTENT_PLACEHOLDER', content))

async def table_exists(conn, table_name):
    result = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = $1)",
        table_name
    )
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)