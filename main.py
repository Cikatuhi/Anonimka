import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
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
        # Передаём данные как простые переменные
        return templates.TemplateResponse(
            "dashboard.html", 
            {
                "request": request,
                "total_users": total_users or 0
            }
        )
    finally:
        await conn.close()

@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    conn = await get_db()
    try:
        users = await conn.fetch("SELECT * FROM users ORDER BY registered_at DESC LIMIT 100")
        # Преобразуем записи в словари для безопасной передачи в шаблон
        users_list = [dict(user) for user in users]
        return templates.TemplateResponse(
            "users.html",
            {
                "request": request,
                "users": users_list
            }
        )
    finally:
        await conn.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)