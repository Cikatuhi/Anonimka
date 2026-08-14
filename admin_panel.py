import os
import uuid
import json
import psutil
import re
import aiohttp
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from cloudpayments import CloudPaymentsClient
from time_utils import now_msk
import database

load_dotenv()

ADMINS = {
    "Alkatras": "147258369",
    "cikatuh": "1324576890",
    "glavskamer": "AK113689avip$top",
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Инициализация БД...", flush=True)
    await database.init_pool()
    print("✅ БД готова", flush=True)
    try:
        yield
    finally:
        await database.close_pool()
        print("🛑 Соединения с БД закрыты", flush=True)
app = FastAPI(title="Admin Panel для Telegram Бота", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LOGIN_PAGE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Вход в админ-панель</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: 'Segoe UI', Arial, sans-serif; 
            background: #0a0a1a; 
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            padding: 20px;
        }
        .login-box {
            background: #111128;
            border-radius: 16px;
            padding: 40px;
            max-width: 400px;
            width: 100%;
            border: 1px solid #7c3aed33;
            box-shadow: 0 0 40px #7c3aed11;
        }
        .login-box h1 {
            color: #7c3aed;
            font-size: 28px;
            text-align: center;
            margin-bottom: 8px;
        }
        .login-box .subtitle {
            color: #8899aa;
            text-align: center;
            margin-bottom: 30px;
            font-size: 14px;
        }
        .login-box label {
            display: block;
            color: #8899aa;
            margin-bottom: 6px;
            font-weight: 500;
            font-size: 14px;
        }
        .login-box input[type="text"],
        .login-box input[type="password"] {
            width: 100%;
            padding: 12px 16px;
            border-radius: 10px;
            border: 1px solid #7c3aed33;
            background: #0a0a1a;
            color: #cdd6f4;
            font-size: 16px;
            transition: all 0.3s ease;
            margin-bottom: 16px;
        }
        .login-box input:focus {
            outline: none;
            border-color: #7c3aed;
            box-shadow: 0 0 20px #7c3aed22;
        }
        .login-box button {
            width: 100%;
            padding: 14px;
            background: #7c3aed;
            color: #0a0a1a;
            border: none;
            border-radius: 10px;
            font-size: 18px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        .login-box button:hover {
            background: #00ddbb;
            box-shadow: 0 0 30px #7c3aed44;
            transform: translateY(-2px);
        }
        .login-box .error {
            color: #ff4488;
            text-align: center;
            margin-top: 12px;
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="login-box">
        <h1>🔐 Вход</h1>
        <p class="subtitle">Введите ник и пароль</p>
        <form action="/admin/login" method="post">
            <label>Ник</label>
            <input type="text" name="username" placeholder="Введите ник" required>
            <label>Пароль</label>
            <input type="password" name="password" placeholder="Введите пароль" required>
            <button type="submit">🚀 Войти</button>
        </form>
        <div id="error" class="error"></div>
    </div>
</body>
</html>
'''

MENU = '''
<div style="background: #0a0a1a; padding: 10px 15px; border: 1px solid #7c3aed33; border-radius: 12px 12px 0 0; display: flex; flex-wrap: wrap; gap: 5px; align-items: center; box-shadow: 0 0 30px #7c3aed11;">
    <a href="/" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #7c3aed'; this.style.boxShadow='0 0 20px #7c3aed55'; this.style.background='#7c3aed22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">📊 Дашборд</a>
    <a href="/users" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #7c3aed'; this.style.boxShadow='0 0 20px #7c3aed55'; this.style.background='#7c3aed22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">👥 Пользователи</a>
    <a href="/messages" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #7c3aed'; this.style.boxShadow='0 0 20px #7c3aed55'; this.style.background='#7c3aed22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">💬 Сообщения</a>
    <a href="/subscriptions" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #7c3aed'; this.style.boxShadow='0 0 20px #7c3aed55'; this.style.background='#7c3aed22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">📋 Подписки</a>
    <a href="/withdrawals" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #7c3aed'; this.style.boxShadow='0 0 20px #7c3aed55'; this.style.background='#7c3aed22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">💰 Снятия</a>
    <a href="/blocked" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #7c3aed'; this.style.boxShadow='0 0 20px #7c3aed55'; this.style.background='#7c3aed22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">🚫 ЧС</a>
    <a href="/unsubscribed" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #7c3aed'; this.style.boxShadow='0 0 20px #7c3aed55'; this.style.background='#7c3aed22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">🚫 Отписались</a>
    <a href="/monitoring" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #7c3aed'; this.style.boxShadow='0 0 20px #7c3aed55'; this.style.background='#7c3aed22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">📊 Мониторинг</a>
    <a href="/settings" style="color: #88f0e0; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid transparent; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #7c3aed'; this.style.boxShadow='0 0 20px #7c3aed55'; this.style.background='#7c3aed22';" 
       onmouseout="this.style.border='1px solid transparent'; this.style.boxShadow='none'; this.style.background='transparent';">⚙️ Настройки</a>
    <a href="/broadcast" style="color: #7c3aed; text-decoration: none; font-weight: 600; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid #7c3aed44; transition: all 0.3s ease; text-shadow: 0 0 20px #7c3aed44; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #7c3aed'; this.style.boxShadow='0 0 30px #7c3aed55'; this.style.background='#7c3aed22';" 
       onmouseout="this.style.border='1px solid #7c3aed44'; this.style.boxShadow='none'; this.style.background='transparent';">📢 Рассылка</a>
    <a href="/webhooks" style="color: #ffaa44; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid #ffaa4444; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #ffaa44'; this.style.boxShadow='0 0 20px #ffaa4455'; this.style.background='#ffaa4422';" 
       onmouseout="this.style.border='1px solid #ffaa4444'; this.style.boxShadow='none'; this.style.background='transparent';">📨 Вебхуки</a>
    <a href="/subgram" style="color: #ffaa44; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid #ffaa4444; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #ffaa44'; this.style.boxShadow='0 0 20px #ffaa4455'; this.style.background='#ffaa4422';" 
       onmouseout="this.style.border='1px solid #ffaa4444'; this.style.boxShadow='none'; this.style.background='transparent';">📊 SubGram</a>  
    <a href="/payments" style="color: #7c3aed; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid #7c3aed44; transition: all 0.3s ease; white-space: nowrap;" 
       onmouseover="this.style.border='1px solid #7c3aed'; this.style.boxShadow='0 0 20px #7c3aed55'; this.style.background='#7c3aed22';" 
       onmouseout="this.style.border='1px solid #7c3aed44'; this.style.boxShadow='none'; this.style.background='transparent';">💳 Платежи</a>    
    <a href="/admin/logout" style="color: #ff4488; text-decoration: none; font-weight: 500; font-size: 14px; padding: 6px 12px; border-radius: 8px; border: 1px solid #ff448844; transition: all 0.3s ease; white-space: nowrap; margin-left: auto;" 
       onmouseover="this.style.border='1px solid #ff4488'; this.style.boxShadow='0 0 20px #ff448855'; this.style.background='#ff448822';" 
       onmouseout="this.style.border='1px solid #ff448844'; this.style.boxShadow='none'; this.style.background='transparent';">🚪 Выйти</a>
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
        .menu { background: #0a0a1a; border-radius: 12px; margin-bottom: 20px; border: 1px solid #7c3aed33; box-shadow: 0 0 30px #7c3aed11; overflow-x: auto; }
        .menu a { display: inline-block; padding: 10px 12px; color: #a6adc8; text-decoration: none; font-weight: 500; font-size: 13px; border-bottom: 3px solid transparent; transition: all 0.2s; white-space: nowrap; }
        .menu a:hover { color: #cdd6f4; border-bottom-color: #89b4fa; }
        .menu a.active { color: #cdd6f4; border-bottom-color: #89b4fa; font-weight: 600; }
        .page-title { font-size: 20px; font-weight: 700; margin-bottom: 15px; color: #cdd6f4; text-shadow: 0 0 40px #7c3aed11; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }
        .stat-card { background: #111128; padding: 15px 12px; border-radius: 12px; text-align: center; transition: all 0.3s ease; border: 1px solid #7c3aed22; }
        .stat-card:hover { transform: translateY(-4px); border: 1px solid #7c3aed; box-shadow: 0 0 30px #7c3aed33; background: #7c3aed11; }
        .stat-number { font-size: 28px; font-weight: 700; color: #7c3aed; text-shadow: 0 0 40px #7c3aed44; }
        .stat-label { font-size: 12px; color: #8899aa; margin-top: 4px; }
        .stat-card .icon { font-size: 24px; margin-bottom: 5px; display: block; }
        .table-container { background: #111128; border-radius: 12px; overflow-x: auto; border: 1px solid #7c3aed22; transition: all 0.3s ease; }
        .table-container:hover { border: 1px solid #7c3aed44; box-shadow: 0 0 30px #7c3aed11; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th { background: #0a0a1a; color: #7c3aed; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.3px; padding: 10px 8px; text-align: left; border-bottom: 1px solid #7c3aed22; }
        td { padding: 8px 8px; border-bottom: 1px solid #1a1a3a; font-size: 13px; color: #cdd6f4; word-break: break-word; }
        tr:hover td { background: #7c3aed11; }
        .badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; border: 1px solid transparent; white-space: nowrap; }
        .badge-text { background: #7c3aed22; color: #7c3aed; border-color: #7c3aed44; }
        .badge-photo { background: #4488ff22; color: #4488ff; border-color: #4488ff44; }
        .badge-video { background: #ff448822; color: #ff4488; border-color: #ff448844; }
        .badge-voice { background: #ffaa4422; color: #ffaa44; border-color: #ffaa4444; }
        .badge-sticker { background: #aa44ff22; color: #aa44ff; border-color: #aa44ff44; }
        .badge-video_note { background: #ff664422; color: #ff6644; border-color: #ff664444; }
        .form-container { background: #111128; padding: 20px; border-radius: 12px; border: 1px solid #7c3aed22; }
        .form-container:hover { border: 1px solid #7c3aed44; box-shadow: 0 0 30px #7c3aed11; }
        label { display: block; margin-bottom: 6px; color: #8899aa; font-weight: 500; font-size: 14px; }
        textarea, input[type="text"] {
            width: 100%; padding: 10px 14px; border-radius: 8px; border: 1px solid #7c3aed33; background: #0a0a1a; color: #cdd6f4; font-size: 14px; font-family: 'Segoe UI', Arial, sans-serif; transition: all 0.3s ease;
        }
        textarea:focus, input[type="text"]:focus { outline: none; border-color: #7c3aed; box-shadow: 0 0 30px #7c3aed22; }
        button { padding: 10px 20px; border: 1px solid #7c3aed; border-radius: 8px; background: transparent; color: #7c3aed; font-weight: 600; font-size: 14px; cursor: pointer; transition: all 0.3s ease; width: 100%; }
        button:hover { background: #7c3aed22; box-shadow: 0 0 30px #7c3aed55; transform: translateY(-2px); }
        .mt-20 { margin-top: 15px; }
        .mb-20 { margin-bottom: 15px; }
        .empty { text-align: center; padding: 30px 15px; color: #8899aa; font-size: 14px; }
        .result-box { background: #111128; padding: 20px; border-radius: 12px; border: 1px solid #7c3aed44; }
        .result-box h2 { font-size: 18px; margin-bottom: 15px; }
        .result-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 15px; }
        .result-stats div { text-align: center; }
        .result-stats .number { font-size: 28px; font-weight: 700; color: #7c3aed; }
        .result-stats .label { color: #8899aa; font-size: 12px; }
        .result-message { background: #0a0a1a; padding: 12px; border-radius: 8px; margin-bottom: 15px; border: 1px solid #7c3aed22; word-break: break-word; font-size: 14px; }
        .glow-button { display: inline-block; padding: 10px 20px; border: 1px solid #7c3aed; border-radius: 8px; color: #7c3aed; text-decoration: none; transition: all 0.3s ease; text-align: center; width: 100%; }
        .glow-button:hover { background: #7c3aed22; box-shadow: 0 0 30px #7c3aed55; }
        .progress-bar { width: 100%; height: 20px; background: #1a1a3a; border-radius: 10px; overflow: hidden; margin: 10px 0; border: 1px solid #7c3aed33; }
        .progress-fill { height: 100%; background: linear-gradient(90deg, #7c3aed, #00ff88); transition: width 0.5s ease; border-radius: 10px; width: 0%; }
        .status-running { color: #7c3aed; }
        .status-paused { color: #ffaa44; }
        .status-completed { color: #44ff88; }
        .status-pending { color: #8899aa; }
        .status-failed { color: #ff4488; }
        .status-cancelled { color: #ff6644; }
        .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 20px; }
        .detail-item { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid #1a1a3a; }
        .detail-item .label { color: #8899aa; }
        .detail-item .value { color: #cdd6f4; font-weight: 500; }
        .updating { opacity: 0.5; transition: opacity 0.3s; }
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
        <div id="content">CONTENT_PLACEHOLDER</div>
    </div>
</body>
</html>
'''


def verify_password(username: str, password: str) -> bool:
    clean_username = username.replace('@', '').strip().lower()
    for admin_username, admin_password in ADMINS.items():
        if admin_username.lower() == clean_username:
            return admin_password == password
    return False

def create_session(username: str) -> str:
    token = str(uuid.uuid4())
    sessions[token] = {
        "username": username,
        "created_at": now_msk(),
        "expires_at": now_msk() + timedelta(hours=24)
    }
    return token

def get_session_user(token: str) -> dict:
    if token not in sessions:
        return None
    session = sessions[token]
    if session["expires_at"] < now_msk():
        del sessions[token]
        return None
    return session

sessions = {}

async def auth_middleware(request: Request, call_next):
    if request.url.path == "/admin/login":
        return await call_next(request)
    
    session_token = request.cookies.get("admin_session")
    if not session_token:
        return RedirectResponse(url="/admin/login", status_code=303)
    
    session_data = get_session_user(session_token)
    if not session_data:
        response = RedirectResponse(url="/admin/login", status_code=303)
        response.delete_cookie("admin_session")
        return response
    
    request.state.admin_username = session_data["username"]
    return await call_next(request)

app.middleware("http")(auth_middleware)


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(content=LOGIN_PAGE)

@app.post("/admin/login")
async def login(request: Request):
    form = await request.form()
    username = form.get("username")
    password = form.get("password")
    
    if not username or not password:
        return HTMLResponse(content=LOGIN_PAGE.replace(
            '<div id="error" class="error"></div>',
            '<div id="error" class="error">❌ Заполните все поля</div>'
        ))
    
    if not verify_password(username, password):
        return HTMLResponse(content=LOGIN_PAGE.replace(
            '<div id="error" class="error"></div>',
            '<div id="error" class="error">❌ Неверный ник или пароль</div>'
        ))
    
    token = create_session(username)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key="admin_session",
        value=token,
        httponly=True,
        max_age=86400,
        secure=True,
        samesite="lax"
    )
    return response

@app.get("/admin/logout")
async def logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie("admin_session")
    return response

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    total_users = 0
    total_messages = 0
    total_subscriptions = 0
    
    try:
        conn = await database.get_conn()
        try:
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            total_messages = await conn.fetchval("SELECT COUNT(*) FROM messages")
            total_subscriptions = await database.count_active_subscriptions()
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка БД в дашборде: {e}")
        import traceback
        traceback.print_exc()

    content = f'''
    <div class="page-title">📊 Дашборд</div>
    <div class="stats">
        <div class="stat-card"><span class="icon">👥</span><div class="stat-number">{total_users}</div><div class="stat-label">Пользователи</div></div>
        <div class="stat-card"><span class="icon">📋</span><div class="stat-number">{total_subscriptions}</div><div class="stat-label">Подписки</div></div>
        <div class="stat-card"><span class="icon">💬</span><div class="stat-number">{total_messages}</div><div class="stat-label">Всего сообщений</div></div>
    </div>
    '''
    
    menu = MENU.replace('📊 Дашборд', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">📊 Дашборд</span>')
    html = BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', content)
    return HTMLResponse(content=html)


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    rows = []
    total = 0
    
    try:
        conn = await database.get_conn()
        try:
            rows = await conn.fetch("SELECT * FROM users ORDER BY registered_at DESC LIMIT 100")
            total = len(rows)
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка загрузки пользователей: {e}")
    
    html = f'<div class="page-title">👥 Пользователи</div>'
    html += f'<div class="stats"><div class="stat-card"><div class="stat-number">{total}</div><div class="stat-label">Всего пользователей</div></div></div>'
    
    if rows:
        html += '<div class="table-container"><table><tr><th>ID</th><th>Username</th><th>Имя</th><th>Регистрация</th></tr>'
        for row in rows:
            html += f"<tr><td>{row['telegram_id']}</td><td>{row.get('username') or '-'}</td><td>{row.get('first_name') or '-'}</td><td>{row.get('registered_at').strftime('%d.%m.%Y %H:%M') if row.get('registered_at') else '-'}</td></tr>"
        html += '</table></div>'
    else:
        html += '<div class="empty">😕 Пользователей пока нет</div>'
    
    menu = MENU.replace('👥 Пользователи', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">👥 Пользователи</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))


@app.get("/messages", response_class=HTMLResponse)
async def messages_page(request: Request):
    rows = []
    
    try:
        conn = await database.get_conn()
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
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка загрузки сообщений: {e}")
    
    html = '<div class="page-title">💬 Сообщения</div>'
    if rows:
        html += '<div class="table-container"><table><tr><th>ID</th><th>От</th><th>Кому</th><th>Тип</th><th>Сообщение</th><th>Дата</th></tr>'
        for row in rows:
            sender = row.get('sender_username') or row.get('sender_name') or 'Неизвестно'
            recipient = row.get('recipient_username') or row.get('recipient_name') or 'Неизвестно'
            html += f'''
            <tr>
                <td>{row['id']}</td>
                <td>@{sender}</td>
                <td>@{recipient}</td>
                <td><span class="badge badge-text">{row['content_type']}</span></td>
                <td>{row['content'][:50]}{'...' if len(row['content'] or '') > 50 else ''}</td>
                <td>{row['created_at'].strftime('%d.%m.%Y %H:%M') if row['created_at'] else '-'}</td>
            </tr>
            '''
        html += '</table></div>'
    else:
        html += '<div class="empty">😕 Сообщений пока нет</div>'
    
    menu = MENU.replace('💬 Сообщения', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">💬 Сообщения</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))


@app.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions_page(request: Request):
    total_active = 0
    rows = []
    
    try:
        total_active = await database.count_active_subscriptions()
        rows = await database.get_all_subscriptions(limit=200)
    except Exception as e:
        print(f"❌ Ошибка загрузки подписок: {e}")
    
    html = f'<div class="page-title">📋 Подписки</div>'
    html += f'<div class="stats"><div class="stat-card"><div class="stat-number">{total_active}</div><div class="stat-label">Активных подписок</div></div></div>'
    
    if rows:
        html += '''
        <div class="table-container">
            <table>
                <tr>
                    <th>ID</th>
                    <th>Пользователь</th>
                    <th>План</th>
                    <th>Начало</th>
                    <th>Окончание</th>
                    <th>Статус</th>
                    <th>Действие</th>
                </tr>
        '''
        for row in rows:
            status_color = "#7c3aed" if row['status'] == 'active' else "#ff4488"
            end_date_str = row['end_date'].strftime('%d.%m.%Y') if row['end_date'] else '∞'
            
            block_btn = ''
            if row['status'] in ['active', 'pending']:
                block_btn = f'''
                <button onclick="blockSubscription({row['user_id']})" 
                        style="padding:4px 12px; border:1px solid #ff4488; border-radius:6px; background:transparent; color:#ff4488; cursor:pointer; font-size:11px;">
                    🚫 Заблокировать
                </button>
                '''
            
            html += f'''
            <tr>
                <td>{row['id']}</td>
                <td>@{row.get('username') or row.get('first_name') or row['user_id']}</td>
                <td>{row['plan']}</td>
                <td>{row['start_date'].strftime('%d.%m.%Y') if row['start_date'] else '-'}</td>
                <td>{end_date_str}</td>
                <td><span style="color:{status_color};">{row['status']}</span></td>
                <td>{block_btn}</td>
            </tr>
            '''
        html += '</table></div>'
        
        html += '''
        <script>
            async function blockSubscription(userId) {
                if (!confirm('⚠️ Вы уверены, что хотите ЗАБЛОКИРОВАТЬ подписку пользователя #' + userId + '?')) return;
                
                const btn = event.target;
                btn.textContent = '⏳...';
                btn.disabled = true;
                
                try {
                    const response = await fetch('/subscription/block/' + userId, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    const data = await response.json();
                    if (data.success) {
                        alert('✅ ' + data.message);
                        window.location.reload();
                    } else {
                        alert('❌ Ошибка: ' + data.error);
                    }
                } catch (e) {
                    alert('❌ Ошибка: ' + e.message);
                } finally {
                    btn.textContent = '🚫 Заблокировать';
                    btn.disabled = false;
                }
            }
        </script>
        '''
    else:
        html += '<div class="empty">😕 Подписок пока нет</div>'
    
    menu = MENU.replace('📋 Подписки', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">📋 Подписки</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))


@app.get("/blocked", response_class=HTMLResponse)
async def blocked_page(request: Request):
    rows = []
    total = 0
    
    try:
        conn = await database.get_conn()
        try:
            rows = await conn.fetch("SELECT * FROM blocked_users ORDER BY blocked_at DESC")
            total = len(rows)
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка загрузки ЧС: {e}")
    
    html = f'<div class="page-title">🚫 Чёрный список</div>'
    html += f'<div class="stats"><div class="stat-card"><div class="stat-number">{total}</div><div class="stat-label">Заблокировали бота</div></div></div>'
    
    if rows:
        html += '<div class="table-container"><table><tr><th>ID</th><th>Username</th><th>Имя</th><th>Заблокирован</th></tr>'
        for row in rows:
            html += f"<tr><td>{row['telegram_id']}</td><td>{row.get('username') or '-'}</td><td>{row.get('first_name') or '-'}</td><td>{row['blocked_at'].strftime('%d.%m.%Y %H:%M') if row['blocked_at'] else '-'}</td></tr>"
        html += '</table></div>'
    else:
        html += '<div class="empty">😕 Пользователей в ЧС нет</div>'
    
    menu = MENU.replace('🚫 ЧС', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">🚫 ЧС</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))


@app.get("/unsubscribed", response_class=HTMLResponse)
async def unsubscribed_page(request: Request):
    total = 0
    users = []
    
    try:
        conn = await database.get_conn()
        try:
            total = await conn.fetchval("SELECT COUNT(*) FROM unsubscribed_users")
            rows = await conn.fetch("""
                SELECT * FROM unsubscribed_users 
                ORDER BY unsubscribed_at DESC 
                LIMIT 200
            """)
            users = [dict(row) for row in rows]
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка загрузки отписавшихся: {e}")
    
    html = f'''
    <div class="page-title">🚫 Отписавшиеся пользователи</div>
    
    <div class="stats">
        <div class="stat-card">
            <div class="stat-number">{total}</div>
            <div class="stat-label">Всего отписавшихся</div>
        </div>
    </div>
    '''
    
    if users:
        html += '''
        <div class="table-container">
            <table>
                <tr>
                    <th>ID</th>
                    <th>Username</th>
                    <th>Имя</th>
                    <th>Дата отписки</th>
                    <th>Причина</th>
                    <th>Токен</th>
                    <th>Действие</th>
                </tr>
        '''
        for row in users:
            reason_labels = {
                'manual': '👤 Самостоятельно',
                'admin_block': '🚫 Админ',
                'expired': '⏰ Истекла'
            }
            reason = reason_labels.get(row.get('reason', 'manual'), row.get('reason', 'manual'))
            
            token = row.get('card_token', '')
            token_display = token[:20] + '...' if token and len(token) > 20 else token or '—'
            
            # Кнопка восстановления
            restore_btn = f'''
            <button onclick="restoreUser({row['telegram_id']})" 
                    style="padding:4px 12px; border:1px solid #7c3aed; border-radius:6px; background:transparent; color:#7c3aed; cursor:pointer; font-size:11px;">
                🔄 Восстановить
            </button>
            '''
            
            html += f'''
            <tr>
                <td>{row['telegram_id']}</td>
                <td>{row.get('username') or '-'}</td>
                <td>{row.get('first_name') or '-'}</td>
                <td>{row['unsubscribed_at'].strftime('%d.%m.%Y %H:%M') if row['unsubscribed_at'] else '-'}</td>
                <td>{reason}</td>
                <td><span style="font-family:monospace; font-size:11px; color:#8899aa;">{token_display}</span></td>
                <td>{restore_btn}</td>
            </tr>
            '''
        html += '</table></div>'
        
        html += '''
        <script>
            async function restoreUser(userId) {
                if (!confirm('⚠️ Вы уверены, что хотите восстановить подписку пользователя #' + userId + '?')) return;
                
                const btn = event.target;
                btn.textContent = '⏳...';
                btn.disabled = true;
                
                try {
                    const response = await fetch('/unsubscribed/restore/' + userId, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    const data = await response.json();
                    if (data.success) {
                        alert('✅ ' + data.message);
                        window.location.reload();
                    } else {
                        alert('❌ Ошибка: ' + data.error);
                    }
                } catch (e) {
                    alert('❌ Ошибка: ' + e.message);
                } finally {
                    btn.textContent = '🔄 Восстановить';
                    btn.disabled = false;
                }
            }
        </script>
        '''
    else:
        html += '<div class="empty">😕 Отписавшихся пока нет</div>'
    
    html += '''
    <div style="margin-top: 20px;">
        <a href="/unsubscribed" class="glow-button" style="flex:0 1 auto; width:auto; padding: 8px 16px;">🔄 Обновить</a>
    </div>
    '''
    
    menu = MENU.replace('🚫 Отписались', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">🚫 Отписались</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

@app.get("/webhooks", response_class=HTMLResponse)
async def webhooks_page(request: Request):
    log_file = "/root/main-site/webhook.log"
    content = ""
    total = 0
    
    try:
        if os.path.exists(log_file):
            with open(log_file, "r") as f:
                content = f.read()
                total = len(content.split("===")) if "===" in content else 1
        else:
            content = "📭 Файл с вебхуками не найден. Ожидайте первые оплаты."
    except Exception as e:
        content = f"❌ Ошибка чтения файла: {e}"
    
    html = f'''
    <div class="page-title">📨 Вебхуки от CloudPayments</div>
    <div style="display: flex; gap: 15px; margin-bottom: 20px; flex-wrap: wrap;">
        <div class="stat-card" style="flex:1; min-width:120px;">
            <div class="stat-number">{total}</div>
            <div class="stat-label">Всего вебхуков</div>
        </div>
    </div>
    <div style="margin-bottom: 15px;">
        <a href="/webhooks" class="glow-button" style="flex:0 1 auto; width:auto; padding: 8px 16px;">🔄 Обновить</a>
    </div>
    <div style="background: #0a0a1a; padding: 20px; border-radius: 12px; border: 1px solid #7c3aed33; font-family: monospace; font-size: 14px; white-space: pre-wrap; word-break: break-all; max-height: 600px; overflow: auto; color: #cdd6f4;">
        {content}
    </div>
    '''
    
    menu = MENU.replace('📨 Вебхуки', '<span style="color:#ffaa44; text-shadow: 0 0 30px #ffaa4444;">📨 Вебхуки</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))


@app.get("/withdrawals", response_class=HTMLResponse)
async def withdrawals_page(request: Request):
    html = '<div class="page-title">💰 Снятия</div><div class="empty">🔧 Раздел в разработке</div>'
    menu = MENU.replace('💰 Снятия', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">💰 Снятия</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

@app.get("/monitoring", response_class=HTMLResponse)
async def monitoring_page(request: Request):
    cpu = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    content = f'''
    <div class="page-title">📊 Мониторинг сервера</div>
    <div class="stats">
        <div class="stat-card"><div class="stat-number">{cpu}%</div><div class="stat-label">CPU</div></div>
        <div class="stat-card"><div class="stat-number">{memory.percent}%</div><div class="stat-label">Память</div></div>
        <div class="stat-card"><div class="stat-number">{disk.percent}%</div><div class="stat-label">Диск</div></div>
    </div>
    '''
    
    menu = MENU.replace('📊 Мониторинг', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">📊 Мониторинг</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', content))

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    html = '''
    <div class="page-title">⚙️ Настройки</div>
    <div class="form-container">
        <h3 style="color: #7c3aed; margin-bottom: 15px;">Управление подтверждением</h3>
        <div style="display: flex; gap: 10px; flex-wrap: wrap;">
            <button onclick="setSetting('true')" class="glow-button" style="flex:1; border-color: #7c3aed; color: #7c3aed;">✅ Включить подтверждение</button>
            <button onclick="setSetting('false')" class="glow-button" style="flex:1; border-color: #ff4488; color: #ff4488;">❌ Отключить подтверждение</button>
        </div>
    </div>
    <script>
        async function setSetting(value) {
            try {
                const response = await fetch('/api/settings/update', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key: 'show_payment_checkboxes', value: value })
                });
                const data = await response.json();
                if (data.success) {
                    alert('✅ Настройка сохранена!');
                } else {
                    alert('❌ Ошибка: ' + (data.error || 'Неизвестная ошибка'));
                }
            } catch (e) {
                alert('❌ Ошибка: ' + e.message);
            }
        }
    </script>
    '''
    menu = MENU.replace('⚙️ Настройки', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">⚙️ Настройки</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))


@app.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request):
    total_users = 0
    job_id = request.query_params.get("job")
    job_data = None
    
    try:
        conn = await database.get_conn()
        try:
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            
            if not job_id:
                row = await conn.fetchrow(
                    "SELECT id FROM broadcast_jobs WHERE status IN ('running', 'pending', 'paused') ORDER BY id DESC LIMIT 1"
                )
                if row:
                    job_id = str(row['id'])
            
            if job_id:
                job_id = int(job_id)
                row = await conn.fetchrow(
                    "SELECT * FROM broadcast_jobs WHERE id = $1",
                    job_id
                )
                if row:
                    job_data = dict(row)
        finally:
            await database.release_conn(conn)
    except:
        pass
    
    result_html = ""
    
    if job_data:
        total = job_data.get('total_users', 0)
        sent = job_data.get('sent_users', 0)
        failed = job_data.get('failed_users', 0)
        blocked = job_data.get('blocked_users', 0)
        progress = job_data.get('progress', 0)
        processed = sent + failed + blocked
        remaining = max(0, total - processed)
        status_text = job_data.get('status', 'pending')
        
        status_labels = {
            'pending': '⏳ Ожидание',
            'running': '🔄 Выполняется',
            'paused': '⏸️ Приостановлено',
            'completed': '✅ Завершено',
            'failed': '❌ Ошибка',
            'cancelled': '⛔ Остановлена'
        }
        status_label = status_labels.get(status_text, status_text)
        status_class = f"status-{status_text}"
        
        result_html = f'''
        <div class="result-box" style="border-color: #7c3aed44; margin-top: 20px;" id="broadcast-stats">
            <h2 style="color: #7c3aed;">📊 Статистика рассылки - <span class="status-value {status_class}">{status_label}</span></h2>
            <div class="result-stats">
                <div class="stat-total"><div class="number">{total}</div><div class="label">Всего пользователей</div></div>
                <div class="stat-sent"><div class="number" style="color: #7c3aed;">{sent}</div><div class="label">✅ Успешно отправлено</div></div>
                <div class="stat-failed"><div class="number" style="color: #ff4488;">{failed}</div><div class="label">❌ Ошибок</div></div>
                <div class="stat-remaining"><div class="number" style="color: #ffaa44;">{remaining}</div><div class="label">⏳ Осталось</div></div>
            </div>
            <div class="progress-bar">
                <div class="progress-fill" style="width:{min(progress, 100)}%;"></div>
            </div>
            <p class="progress-text" style="text-align:center; color:#8899aa; font-size:13px;">{progress:.1f}% - Обработано: {processed} из {total}</p>
            
            <div style="margin-top: 15px; display: flex; gap: 10px; flex-wrap: wrap;">
                <button onclick="pauseBroadcast()" id="pauseBtn" 
                        style="flex:1; padding: 12px 20px; border: 2px solid #ffaa44; border-radius: 8px; 
                               background: transparent; color: #ffaa44; font-weight: 600; font-size: 14px; 
                               cursor: pointer; transition: all 0.3s ease;"
                        onmouseover="this.style.background='#ffaa4422'; this.style.boxShadow='0 0 30px #ffaa4455';"
                        onmouseout="this.style.background='transparent'; this.style.boxShadow='none';">
                    ⏸️ Поставить на паузу
                </button>
                <button onclick="deleteBroadcast()" id="deleteBtn" 
                        style="flex:1; padding: 12px 20px; border: 2px solid #ff4488; border-radius: 8px; 
                               background: transparent; color: #ff4488; font-weight: 600; font-size: 14px; 
                               cursor: pointer; transition: all 0.3s ease;"
                        onmouseover="this.style.background='#ff448822'; this.style.boxShadow='0 0 30px #ff448855';"
                        onmouseout="this.style.background='transparent'; this.style.boxShadow='none';">
                    🗑️ Удалить рассылку
                </button>
                <a href="/broadcast" class="glow-button" style="flex:1;">🔄 Обновить</a>
                <a href="/broadcast/list" class="glow-button" style="flex:1; border-color: #4488ff; color: #4488ff;">📋 Все рассылки</a>
            </div>
        </div>

        <script>
            let jobId = {job_id};

            async function updateStatus() {{
                try {{
                    const response = await fetch(`/broadcast/status/${{jobId}}`);
                    const data = await response.json();

                    if (data.success) {{
                        document.querySelector('.stat-total .number').textContent = data.total_users;
                        document.querySelector('.stat-sent .number').textContent = data.sent_users;
                        document.querySelector('.stat-failed .number').textContent = data.failed_users;
                        const remaining = data.total_users - data.sent_users - data.failed_users - (data.blocked_users || 0);
                        document.querySelector('.stat-remaining .number').textContent = Math.max(0, remaining);
                        document.querySelector('.progress-fill').style.width = data.progress + '%';
                        document.querySelector('.progress-text').textContent = data.progress.toFixed(1) + '% - Обработано: ' + (data.sent_users + data.failed_users + (data.blocked_users || 0)) + ' из ' + data.total_users;

                        const statusLabel = {{
                            'pending': '⏳ Ожидание',
                            'running': '🔄 Выполняется',
                            'paused': '⏸️ Приостановлено',
                            'completed': '✅ Завершено',
                            'failed': '❌ Ошибка',
                            'cancelled': '⛔ Остановлена'
                        }};
                        document.querySelector('.status-value').textContent = statusLabel[data.status] || data.status;
                        document.querySelector('.status-value').className = 'status-value status-' + data.status;
                    }}
                }} catch (e) {{
                    console.error('Ошибка обновления статуса:', e);
                }}
            }}

            async function pauseBroadcast() {{
                if (!confirm('⏸️ Вы уверены, что хотите поставить рассылку #${{jobId}} на паузу?')) return;
                
                const btn = document.getElementById('pauseBtn');
                btn.textContent = '⏳ Пауза...';
                btn.disabled = true;
                
                try {{
                    const response = await fetch(`/broadcast/pause/${{jobId}}`, {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }}
                    }});
                    const data = await response.json();
                    if (data.success) {{
                        alert('✅ ' + data.message);
                        window.location.reload();
                    }} else {{
                        alert('❌ Ошибка: ' + data.error);
                    }}
                }} catch (e) {{
                    alert('❌ Ошибка: ' + e.message);
                }} finally {{
                    btn.textContent = '⏸️ Поставить на паузу';
                    btn.disabled = false;
                }}
            }}

            async function deleteBroadcast() {{
                if (!confirm('🗑️ ВЫ УВЕРЕНЫ? Рассылка #${{jobId}} будет полностью удалена из БД!')) return;
                
                const btn = document.getElementById('deleteBtn');
                btn.textContent = '⏳ Удаление...';
                btn.disabled = true;
                
                try {{
                    const response = await fetch(`/broadcast/delete/${{jobId}}`, {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }}
                    }});
                    const data = await response.json();
                    if (data.success) {{
                        alert('✅ ' + data.message);
                        window.location.href = '/broadcast';
                    }} else {{
                        alert('❌ Ошибка: ' + data.error);
                    }}
                }} catch (e) {{
                    alert('❌ Ошибка: ' + e.message);
                }} finally {{
                    btn.textContent = '🗑️ Удалить рассылку';
                    btn.disabled = false;
                }}
            }}

            // Обновляем каждые 5 секунд
            setInterval(updateStatus, 5000);
            updateStatus();
        </script>
        '''
    
    html = f'''
    <div class="page-title">📢 Рассылка</div>
    <div class="stats">
        <div class="stat-card"><div class="stat-number">{total_users}</div><div class="stat-label">Всего пользователей</div></div>
    </div>
    <div class="form-container">
        <form action="/broadcast/send" method="post">
            <label for="message">📝 Текст сообщения:</label>
            <textarea id="message" name="message" rows="5" placeholder="Введите текст рассылки..."></textarea>
            <div class="mt-20">
                <button type="submit">🚀 Запустить рассылку</button>
            </div>
        </form>
    </div>
    {result_html}
    '''
    
    menu = MENU.replace('📢 Рассылка', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">📢 Рассылка</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

@app.post("/broadcast/send")
async def send_broadcast(request: Request, message: str = Form(...)):
    if not message or len(message.strip()) == 0:
        return RedirectResponse(url="/broadcast?error=empty", status_code=303)
    
    try:
        conn = await database.get_conn()
        try:
            rows = await conn.fetch("SELECT telegram_id FROM users")
            user_ids = [row['telegram_id'] for row in rows]
            
            if not user_ids:
                return RedirectResponse(url="/broadcast?error=no_users", status_code=303)
            
            await conn.execute("""
                INSERT INTO broadcast_jobs (message, total_users, user_ids_json, status, created_at)
                VALUES ($1, $2, $3::jsonb, 'pending', NOW())
            """, message, len(user_ids), json.dumps(user_ids))
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка отправки рассылки: {e}")
    
    return RedirectResponse(url="/broadcast?success=1", status_code=303)


@app.get("/broadcast/status/{job_id}")
async def get_broadcast_status(job_id: int):
    try:
        conn = await database.get_conn()
        try:
            row = await conn.fetchrow(
                "SELECT status, total_users, sent_users, failed_users, blocked_users, progress FROM broadcast_jobs WHERE id = $1",
                job_id
            )
            if row:
                return {
                    "success": True,
                    "status": row['status'],
                    "total_users": row['total_users'],
                    "sent_users": row['sent_users'],
                    "failed_users": row['failed_users'],
                    "blocked_users": row['blocked_users'],
                    "progress": float(row['progress']) if row['progress'] else 0
                }
            return {"success": False, "error": "Job not found"}
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка получения статуса рассылки: {e}")
        return {"success": False, "error": str(e)}


async def get_job_status(job_id: int):
    async with database.connection() as conn:
        row = await conn.fetchrow("SELECT * FROM broadcast_jobs WHERE id = $1", job_id)
        return dict(row) if row else None

@app.post("/broadcast/pause/{job_id}")
async def pause_broadcast(job_id: int):
    async with database.connection() as conn:
        job = await conn.fetchrow("SELECT * FROM broadcast_jobs WHERE id = $1", job_id)
        if not job:
            return JSONResponse(
                content={"success": False, "error": "Задача не найдена"},
                status_code=404
            )
        
        if job['status'] not in ('running', 'pending'):
            return JSONResponse(
                content={"success": False, "error": f"Нельзя поставить на паузу задачу со статусом {job['status']}"},
                status_code=400
            )
        
        await conn.execute(
            "UPDATE broadcast_jobs SET status = 'paused', paused_at = NOW() WHERE id = $1",
            job_id
        )
        
        return JSONResponse(
            content={
                "success": True,
                "message": f"Рассылка #{job_id} поставлена на паузу",
                "job_id": job_id
            }
        )

@app.post("/broadcast/delete/{job_id}")
async def delete_broadcast(job_id: int):
    async with database.connection() as conn:
        job = await conn.fetchrow("SELECT * FROM broadcast_jobs WHERE id = $1", job_id)
        if not job:
            return JSONResponse(
                content={"success": False, "error": "Задача не найдена"},
                status_code=404
            )
        
        await conn.execute("DELETE FROM broadcast_jobs WHERE id = $1", job_id)
        
        return JSONResponse(
            content={
                "success": True,
                "message": f"Рассылка #{job_id} удалена",
                "job_id": job_id
            }
        )

@app.post("/subscription/block/{user_id}")
async def block_subscription(user_id: int):
    """Блокирует подписку пользователя (меняет статус на 'cancelled')"""
    try:
        conn = await database.get_conn()
        try:
            # Проверяем, есть ли подписка
            row = await conn.fetchrow(
                "SELECT id, status FROM subscriptions WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
                user_id
            )
            if not row:
                return JSONResponse(
                    content={"success": False, "error": "Подписка не найдена"},
                    status_code=404
                )
            
            if row['status'] == 'cancelled':
                return JSONResponse(
                    content={"success": False, "error": "Подписка уже заблокирована"},
                    status_code=400
                )
            
            await conn.execute(
                "UPDATE subscriptions SET status = 'cancelled' WHERE user_id = $1",
                user_id
            )
            
            # Добавляем в отписавшиеся
            user_data = await database.get_user(user_id)
            if user_data:
                await database.unsubscribe_user(
                    user_id,
                    user_data.get('username', ''),
                    user_data.get('first_name', ''),
                    'admin_block'
                )
            
            return JSONResponse(
                content={"success": True, "message": f"Подписка пользователя {user_id} заблокирована"}
            )
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка блокировки подписки: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )


@app.get("/broadcast/list", response_class=HTMLResponse)
async def broadcast_list(request: Request):
    try:
        conn = await database.get_conn()
        try:
            rows = await conn.fetch("""
                SELECT * FROM broadcast_jobs 
                ORDER BY created_at DESC 
                LIMIT 50
            """)
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка загрузки истории: {e}")
        rows = []
    
    html = '<div class="page-title">📋 История рассылок</div>'
    
    if rows:
        html += '<div class="table-container"><table><tr><th>ID</th><th>Статус</th><th>Всего</th><th>Отправлено</th><th>Ошибок</th><th>Прогресс</th><th>Создана</th><th>Действие</th></tr>'
        for row in rows:
            status_color = {
                'pending': '#8899aa',
                'running': '#7c3aed',
                'paused': '#ffaa44',
                'completed': '#44ff88',
                'failed': '#ff4488',
                'cancelled': '#ff6644'
            }.get(row['status'], '#8899aa')
            
            status_labels = {
                'pending': '⏳ Ожидание',
                'running': '🔄 Выполняется',
                'paused': '⏸️ Пауза',
                'completed': '✅ Завершено',
                'failed': '❌ Ошибка',
                'cancelled': '⛔ Остановлена'
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
                <td><a href="/broadcast?job={row['id']}" style="color:#7c3aed;">Подробнее</a></td>
            </tr>
            '''
        html += '</table></div>'
    else:
        html += '<div class="empty">😕 Рассылок пока нет</div>'
    
    menu = MENU.replace('📢 Рассылка', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">📢 Рассылка</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

@app.post("/unsubscribed/restore/{user_id}")
async def restore_subscription(user_id: int):
    try:
        conn = await database.get_conn()
        try:
            # ===== 1. Проверяем, есть ли пользователь в отписавшихся =====
            unsub_row = await conn.fetchrow(
                "SELECT * FROM unsubscribed_users WHERE telegram_id = $1",
                user_id
            )
            if not unsub_row:
                return JSONResponse(
                    content={"success": False, "error": "Пользователь не найден в отписавшихся"},
                    status_code=404
                )
            
            card_token = unsub_row.get('card_token')
            subscription_id = unsub_row.get('subscription_id')
            
            # ===== 2. Восстанавливаем подписку в БД =====
            end_date = now_msk() + timedelta(days=7)
            
            # Проверяем, есть ли уже подписка
            existing = await conn.fetchrow(
                "SELECT id FROM subscriptions WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
                user_id
            )
            
            if existing:
                await conn.execute(
                    """
                    UPDATE subscriptions 
                    SET status = 'active', 
                        end_date = $1,
                        plan = 'HIGH',
                        card_token = $2,
                        subscription_id = $3
                    WHERE user_id = $4
                    """,
                    end_date, card_token, subscription_id, user_id
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO subscriptions (user_id, plan, status, end_date, card_token, subscription_id, created_at)
                    VALUES ($1, 'HIGH', 'active', $2, $3, $4, NOW())
                    """,
                    user_id, end_date, card_token, subscription_id
                )
            
            await conn.execute(
                "DELETE FROM unsubscribed_users WHERE telegram_id = $1",
                user_id
            )
            
            if card_token:
                try:
                    from cloudpayments import CloudPaymentsClient
                    cp_client = CloudPaymentsClient()
                    
                    result = await cp_client.create_subscription(
                        token=card_token,
                        account_id=str(user_id),
                        amount=69900,
                        interval_days=1,
                        max_periods=None
                    )
                    
                    if result["success"]:
                        new_subscription_id = result["subscription_id"]
                        await conn.execute(
                            "UPDATE subscriptions SET subscription_id = $1 WHERE user_id = $2",
                            new_subscription_id, user_id
                        )
                        print(f"✅ Восстановлена подписка в CloudPayments для {user_id}")
                    else:
                        print(f"❌ Ошибка восстановления подписки в CloudPayments: {result.get('error')}")
                except Exception as e:
                    print(f"❌ Ошибка при создании подписки в CloudPayments: {e}")
            
            return JSONResponse(
                content={
                    "success": True,
                    "message": f"Подписка пользователя {user_id} восстановлена"
                }
            )
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка восстановления подписки: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.post("/subscription/block/{user_id}")
async def block_subscription(user_id: int):
    try:
        conn = await database.get_conn()
        try:
            # ===== 1. Проверяем, есть ли подписка =====
            row = await conn.fetchrow(
                "SELECT id, card_token, subscription_id, status FROM subscriptions WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
                user_id
            )
            if not row:
                return JSONResponse(
                    content={"success": False, "error": "Подписка не найдена"},
                    status_code=404
                )
            
            if row['status'] == 'cancelled':
                return JSONResponse(
                    content={"success": False, "error": "Подписка уже заблокирована"},
                    status_code=400
                )
            
            user_data = await database.get_user(user_id)
            username = user_data.get('username', '') if user_data else ''
            first_name = user_data.get('first_name', '') if user_data else ''
            
            subscription_id = row.get('subscription_id')
            if subscription_id:
                try:
                    from cloudpayments import CloudPaymentsClient
                    cp_client = CloudPaymentsClient()
                    result = await cp_client.cancel_subscription(subscription_id)
                    if result["success"]:
                        print(f"✅ Отменена подписка в CloudPayments для {user_id}")
                    else:
                        print(f"❌ Ошибка отмены подписки в CloudPayments: {result.get('error')}")
                except Exception as e:
                    print(f"❌ Ошибка при отмене подписки: {e}")
            
            await conn.execute(
                "UPDATE subscriptions SET status = 'cancelled' WHERE user_id = $1",
                user_id
            )
            
            await conn.execute("""
                INSERT INTO unsubscribed_users (telegram_id, username, first_name, reason, unsubscribed_at, card_token, subscription_id)
                VALUES ($1, $2, $3, 'admin_block', NOW(), $4, $5)
                ON CONFLICT (telegram_id) DO UPDATE SET 
                    username = $2, 
                    first_name = $3, 
                    reason = 'admin_block', 
                    unsubscribed_at = NOW(),
                    card_token = $4,
                    subscription_id = $5
            """, user_id, username, first_name, row.get('card_token'), row.get('subscription_id'))
            
            return JSONResponse(
                content={"success": True, "message": f"Подписка пользователя {user_id} заблокирована"}
            )
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка блокировки подписки: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.get("/subgram", response_class=HTMLResponse)
async def subgram_page(request: Request):
    try:
        print("🔥 SUBGRAM PAGE STARTED")
        balance_data = await database.get_subgram_balance()
        print(f"🔥 balance_data: {balance_data}")
        
        balance = 0
        bots_info = []
        error_message = None
        
        if balance_data.get("status") == "ok":
            balance = balance_data.get("balance", 0)
            bots_info = balance_data.get("bots_info", [])
        else:
            error_message = balance_data.get("message", "Неизвестная ошибка")
        
        html = f'''
        <div class="page-title">📊 SubGram Статистика</div>
        
        <div class="stats">
            <div class="stat-card">
                <div class="stat-number">{balance} ₽</div>
                <div class="stat-label">Баланс аккаунта</div>
            </div>
        </div>
        '''
        
        if error_message:
            html += f'<div class="empty" style="color:#ff4488;">❌ Ошибка: {error_message}</div>'
        
        if bots_info:
            html += '''
            <div class="table-container">
                <table>
                    <tr>
                        <th>Бот</th>
                        <th>Подписчиков приведено</th>
                        <th>Заработано</th>
                    </tr>
            '''
            for bot in bots_info:
                html += f'''
                <tr>
                    <td>@{bot.get("bot_username", "—")}</td>
                    <td>{bot.get("total_followers", 0)}</td>
                    <td>{bot.get("revenue", 0)} ₽</td>
                </tr>
                '''
            html += '</table></div>'
        else:
            html += '<div class="empty">😕 Нет данных по ботам или они не подключены.</div>'
        
        menu = MENU.replace('📊 SubGram', '<span style="color:#ffaa44; text-shadow: 0 0 30px #ffaa4444;">📊 SubGram</span>')
        return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))
        
    except Exception as e:
        print(f"🔥 КРИТИЧЕСКАЯ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()
        return HTMLResponse(content=f"<h1>❌ Ошибка</h1><pre>{traceback.format_exc()}</pre>")

@app.get("/payments", response_class=HTMLResponse)
async def payments_page(request: Request):
    
    try:
        conn = await database.get_conn()
        try:
            # Общее количество платежей
            total_payments = await conn.fetchval("SELECT COUNT(*) FROM payment_events")
            
            # Общая сумма платежей (в рублях)
            total_amount = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM payment_events") / 100
            
            # Количество успешных платежей
            successful_payments = await conn.fetchval(
                "SELECT COUNT(*) FROM payment_events WHERE status = 'Completed'"
            )
            
            # Количество неудачных платежей
            failed_payments = await conn.fetchval(
                "SELECT COUNT(*) FROM payment_events WHERE status != 'Completed'"
            )
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка загрузки статистики платежей: {e}")
        total_payments = 0
        total_amount = 0
        successful_payments = 0
        failed_payments = 0

    html = f'''
    <div class="page-title">💳 Статистика CloudPayments</div>
    
    <div class="stats">
        <div class="stat-card">
            <div class="stat-number">{total_payments}</div>
            <div class="stat-label">Всего платежей</div>
        </div>
        <div class="stat-card">
            <div class="stat-number">{total_amount:.2f} ₽</div>
            <div class="stat-label">Общая сумма</div>
        </div>
        <div class="stat-card">
            <div class="stat-number" style="color:#44ff88;">{successful_payments}</div>
            <div class="stat-label">✅ Успешных</div>
        </div>
        <div class="stat-card">
            <div class="stat-number" style="color:#ff4488;">{failed_payments}</div>
            <div class="stat-label">❌ Неудачных</div>
        </div>
    </div>
    '''
    
    menu = MENU.replace('💳 Платежи', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">💳 Платежи</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
