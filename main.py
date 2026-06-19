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

async def table_exists(conn, table_name):
    result = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = $1)",
        table_name
    )
    return result

MENU = '''
<div style="background: white; padding: 15px 30px; border-bottom: 2px solid #e8ecf1; display: flex; gap: 30px; align-items: center;">
    <a href="/" style="color: #2c3e50; text-decoration: none; font-weight: 600; font-size: 16px; padding: 8px 16px; border-radius: 8px;">📊 Дашборд</a>
    <a href="/users" style="color: #7f8c8d; text-decoration: none; font-weight: 500; font-size: 16px; padding: 8px 16px; border-radius: 8px;">👥 Пользователи</a>
    <a href="/messages" style="color: #7f8c8d; text-decoration: none; font-weight: 500; font-size: 16px; padding: 8px 16px; border-radius: 8px;">💬 Сообщения</a>
    <a href="/withdrawals" style="color: #7f8c8d; text-decoration: none; font-weight: 500; font-size: 16px; padding: 8px 16px; border-radius: 8px;">💰 Система снятий</a>
    <a href="/settings" style="color: #7f8c8d; text-decoration: none; font-weight: 500; font-size: 16px; padding: 8px 16px; border-radius: 8px;">⚙️ Настройки</a>
    <a href="/broadcast" style="color: #7f8c8d; text-decoration: none; font-weight: 500; font-size: 16px; padding: 8px 16px; border-radius: 8px;">📢 Рассылка</a>
</div>
'''

BASE_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Админ панель</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #f0f2f5; color: #2c3e50; }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px 30px; }
        
        /* Меню */
        .menu { background: white; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); padding: 0 20px; margin-bottom: 25px; }
        .menu a { display: inline-block; padding: 16px 20px; color: #7f8c8d; text-decoration: none; font-weight: 500; font-size: 15px; border-bottom: 3px solid transparent; transition: all 0.2s; }
        .menu a:hover { color: #2c3e50; border-bottom-color: #3498db; }
        .menu a.active { color: #2c3e50; border-bottom-color: #3498db; font-weight: 600; }
        
        /* Заголовок */
        .page-title { font-size: 24px; font-weight: 700; margin-bottom: 25px; color: #2c3e50; }
        
        /* Карточки статистики */
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .stat-card { background: white; padding: 25px 20px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); text-align: center; transition: transform 0.2s; }
        .stat-card:hover { transform: translateY(-4px); }
        .stat-number { font-size: 32px; font-weight: 700; color: #2c3e50; }
        .stat-label { font-size: 14px; color: #7f8c8d; margin-top: 5px; }
        .stat-card .icon { font-size: 28px; margin-bottom: 8px; display: block; }
        
        /* Таблицы */
        .table-container { background: white; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); overflow: hidden; }
        table { width: 100%; border-collapse: collapse; }
        th { background: #f8f9fa; color: #2c3e50; font-weight: 600; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; padding: 14px 16px; text-align: left; }
        td { padding: 12px 16px; border-bottom: 1px solid #f0f2f5; font-size: 14px; }
        tr:hover { background: #f8f9fa; }
        
        .badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }
        .badge-text { background: #e8f5e9; color: #2e7d32; }
        .badge-photo { background: #e3f2fd; color: #0d47a1; }
        .badge-video { background: #fbe9e7; color: #bf360c; }
        .badge-voice { background: #fff8e1; color: #f57f17; }
        .badge-sticker { background: #f3e5f5; color: #6a1b9a; }
        .badge-video_note { background: #fff3e0; color: #e65100; }
        
        .empty { text-align: center; padding: 40px; color: #95a5a6; font-size: 16px; }
        .mt-20 { margin-top: 20px; }
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
            <div class="stat-card">
                <span class="icon">👥</span>
                <div class="stat-number">{total_users}</div>
                <div class="stat-label">Пользователи</div>
            </div>
            <div class="stat-card">
                <span class="icon">⭐</span>
                <div class="stat-number">{total_premium}</div>
                <div class="stat-label">Премиум</div>
            </div>
            <div class="stat-card">
                <span class="icon">💬</span>
                <div class="stat-number">{total_messages}</div>
                <div class="stat-label">Сообщений</div>
            </div>
            <div class="stat-card">
                <span class="icon">💳</span>
                <div class="stat-number">{total_payments}</div>
                <div class="stat-label">Платежей ({total_revenue} ₽)</div>
            </div>
        </div>
        '''
        menu = MENU.replace('📊 Дашборд', '<span style="color:#2c3e50;">📊 Дашборд</span>')
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
        html += f'<div class="stat-card"><div class="stat-number">{total}</div><div class="stat-label">Всего пользователей</div></div>'
        html += f'</div>'
        
        if rows:
            html += '<div class="table-container"><table><tr><th>ID</th><th>Username</th><th>Имя</th><th>Зарегистрирован</th></tr>'
            for row in rows:
                html += f"<tr><td>{row['telegram_id']}</td><td>{row.get('username') or '-'}</td><td>{row.get('first_name') or '-'}</td><td>{row.get('registered_at').strftime('%d.%m.%Y %H:%M') if row.get('registered_at') else '-'}</td></tr>"
            html += '</table></div>'
        else:
            html += '<div class="empty">😕 Пользователей пока нет</div>'
            
        menu = MENU.replace('👥 Пользователи', '<span style="color:#2c3e50;">👥 Пользователи</span>')
        return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))
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
            
        menu = MENU.replace('💬 Сообщения', '<span style="color:#2c3e50;">💬 Сообщения</span>')
        return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))
    finally:
        await conn.close()

@app.get("/withdrawals", response_class=HTMLResponse)
async def withdrawals_page():
    html = '<div class="page-title">💰 Система снятий</div><div class="empty">🔧 Раздел в разработке</div>'
    menu = MENU.replace('💰 Система снятий', '<span style="color:#2c3e50;">💰 Система снятий</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    html = '<div class="page-title">⚙️ Настройки</div><div class="empty">🔧 Раздел в разработке</div>'
    menu = MENU.replace('⚙️ Настройки', '<span style="color:#2c3e50;">⚙️ Настройки</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

@app.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page():
    html = '<div class="page-title">📢 Рассылка</div><div class="empty">🔧 Раздел в разработке</div>'
    menu = MENU.replace('📢 Рассылка', '<span style="color:#2c3e50;">📢 Рассылка</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)