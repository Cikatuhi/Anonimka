import os
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import asyncpg
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Admin Panel для Telegram Бота")

templates = Jinja2Templates(directory="templates")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "anonim_bot")
DB_USER = os.getenv("DB_USER", "bot_user")
DB_PASSWORD = os.getenv("DB_PASSWORD")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

async def get_db():
    return await asyncpg.connect(DATABASE_URL)

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    conn = await get_db()
    try:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_messages = await conn.fetchval("SELECT COUNT(*) FROM messages") if await table_exists(conn, "messages") else 0
        total_payments = await conn.fetchval("SELECT COUNT(*) FROM payments") if await table_exists(conn, "payments") else 0
        total_premium = await conn.fetchval("SELECT COUNT(*) FROM users WHERE premium = true") if await column_exists(conn, "users", "premium") else 0

        # Статистика за 7 дней
        stats = []
        for i in range(6, -1, -1):
            date = datetime.now().date() - timedelta(days=i)
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE DATE(registered_at) = $1",
                date
            )
            stats.append({
                "date": date.strftime("%d.%m"),
                "new_users": count,
                "active_users": count,
                "messages": 0
            })

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "total_users": total_users or 0,
            "total_messages": total_messages or 0,
            "total_payments": total_payments or 0,
            "total_premium": total_premium or 0,
            "total_revenue": "0.00",
            "stats": stats
        })
    finally:
        await conn.close()

@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    conn = await get_db()
    try:
        users = await conn.fetch("SELECT * FROM users ORDER BY registered_at DESC LIMIT 100")
        return templates.TemplateResponse("users.html", {
            "request": request,
            "users": users
        })
    finally:
        await conn.close()

async def table_exists(conn, table_name):
    result = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = $1)",
        table_name
    )
    return result

async def column_exists(conn, table_name, column_name):
    result = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = $1 AND column_name = $2)",
        table_name, column_name
    )
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)