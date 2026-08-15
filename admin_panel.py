import os
import uuid
import psutil
import re
import aiohttp
from html import escape
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


async def ensure_admin_indexes() -> None:
    """Create indexes used by admin list pages when they are missing.

    The migration is idempotent and tolerates optional tables/columns from
    older installations, so the panel can start while the schema is upgraded.
    """
    statements = (
        "CREATE INDEX IF NOT EXISTS idx_users_registered_at ON users (registered_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages (created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages (sender_id)",
        "CREATE INDEX IF NOT EXISTS idx_messages_recipient_id ON messages (recipient_id)",
        "CREATE INDEX IF NOT EXISTS idx_subscriptions_status_created_at ON subscriptions (status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_subscriptions_user_created_at ON subscriptions (user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_payment_events_status_created_at ON payment_events (status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_payment_events_user_created_at ON payment_events (user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_broadcast_jobs_status_created_at ON broadcast_jobs (status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_blocked_users_blocked_at ON blocked_users (blocked_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_unsubscribed_users_unsubscribed_at ON unsubscribed_users (unsubscribed_at DESC)",
    )
    conn = await database.get_conn()
    try:
        for statement in statements:
            try:
                await conn.execute(statement)
            except Exception as exc:
                # Older installations may not have every optional table/column.
                print(f"⚠️ Не удалось создать индекс админки: {exc}", flush=True)
    finally:
        await database.release_conn(conn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Инициализация БД...", flush=True)
    await database.init_pool()
    await database.ensure_broadcast_schema()
    await ensure_admin_indexes()
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
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Вход в админ-панель</title>
    <script>
        (() => {
            try {
                const savedTheme = localStorage.getItem('anonimno-admin-theme');
                const systemTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
                document.documentElement.dataset.theme = savedTheme === 'dark' || savedTheme === 'light' ? savedTheme : systemTheme;
            } catch (_) {
                document.documentElement.dataset.theme = 'light';
            }
        })();
    </script>
    <style>
        :root {
            color-scheme: light;
            --ink: #183332; --muted: #71817f; --line: #dce7e4; --accent: #118b7e; --accent-dark: #0d7168;
            --primary-bg: #118b7e; --primary-hover: #0d7168;
            --canvas: #f2f6f5; --surface: rgba(255,255,255,.96); --field: #fbfdfc; --field-focus: #ffffff;
            --label: #435b59; --error: #b54747; --login-border: rgba(24,51,50,.09); --login-shadow: rgba(26,63,60,.12);
        }
        :root[data-theme="dark"] {
            color-scheme: dark;
            --ink: #ecf5f3; --muted: #96aaa7; --line: #2b3d3e; --accent: #43b8a9; --accent-dark: #65cbbc;
            --primary-bg: #247f75; --primary-hover: #1d6c64;
            --canvas: #0d1516; --surface: rgba(20,32,34,.97); --field: #111d1f; --field-focus: #172628;
            --label: #b4c5c2; --error: #ef8b91; --login-border: rgba(155,195,188,.14); --login-shadow: rgba(0,0,0,.34);
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--canvas); color: var(--ink); display: grid; place-items: center; min-height: 100vh; padding: 24px; }
        body::before { content: ""; position: fixed; inset: 0; pointer-events: none; background: radial-gradient(circle at 18% 12%, rgba(17,139,126,.10), transparent 32%), radial-gradient(circle at 84% 82%, rgba(44,109,105,.08), transparent 28%); }
        .login-box {
            position: relative; z-index: 1; background: var(--surface); border-radius: 22px; padding: 38px; max-width: 430px; width: 100%; border: 1px solid var(--login-border); box-shadow: 0 24px 70px var(--login-shadow);
        }
        .brand-mark { width: 48px; height: 48px; margin: 0 auto 20px; display: grid; place-items: center; border-radius: 14px; color: white; background: var(--primary-bg); font-size: 22px; font-weight: 800; box-shadow: 0 10px 24px rgba(17,139,126,.22); }
        .login-box h1 { color: var(--ink); font-size: 27px; text-align: center; margin-bottom: 8px; letter-spacing: -.02em; }
        .login-box .subtitle { color: var(--muted); text-align: center; margin-bottom: 30px; font-size: 14px; line-height: 1.5; }
        .login-box label { display: block; color: var(--label); margin-bottom: 7px; font-weight: 650; font-size: 13px; }
        .login-box input[type="text"],
        .login-box input[type="password"] {
            width: 100%; padding: 12px 14px; border-radius: 11px; border: 1px solid var(--line); background: var(--field); color: var(--ink); font-size: 15px; transition: .2s ease; margin-bottom: 17px;
        }
        .login-box input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 4px rgba(17,139,126,.10); background: var(--field-focus); }
        .login-box button {
            width: 100%; padding: 13px; background: var(--primary-bg); color: white; border: none; border-radius: 11px; font-size: 15px; font-weight: 750; cursor: pointer; transition: .2s ease;
        }
        .login-box button:hover { background: var(--primary-hover); transform: translateY(-1px); box-shadow: 0 10px 24px rgba(17,139,126,.18); }
        .login-box .error { color: var(--error); text-align: center; margin-top: 14px; font-size: 13px; }
        @media (max-width: 480px) { .login-box { padding: 30px 22px; } }
    </style>
</head>
<body>
    <div class="login-box">
        <div class="brand-mark">A</div>
        <h1>Панель управления</h1>
        <p class="subtitle">Войдите, чтобы управлять ботом и отслеживать ключевые показатели.</p>
        <form action="/admin/login" method="post">
            <label for="username">Логин</label>
            <input id="username" type="text" name="username" placeholder="Введите логин" autocomplete="username" required>
            <label for="password">Пароль</label>
            <input id="password" type="password" name="password" placeholder="Введите пароль" autocomplete="current-password" required>
            <button type="submit">Войти</button>
        </form>
        <div id="error" class="error"></div>
    </div>
</body>
</html>
'''

MENU = '''
<div class="sidebar-brand">
    <div class="brand-symbol">A</div>
    <div><strong>Anonimno</strong><span>Панель управления</span></div>
</div>
<nav class="sidebar-nav" aria-label="Основная навигация">
    <div class="nav-section">
        <div class="nav-section-title">Основное</div>
        <a class="nav-link" data-nav href="/"><span class="nav-icon">⌂</span><span>Обзор</span></a>
        <a class="nav-link" data-nav href="/users"><span class="nav-icon">◎</span><span>Пользователи</span></a>
        <a class="nav-link" data-nav href="/messages"><span class="nav-icon">≋</span><span>Сообщения</span></a>
    </div>
    <div class="nav-section">
        <div class="nav-section-title">Монетизация</div>
        <a class="nav-link" data-nav href="/subscriptions"><span class="nav-icon">◇</span><span>Подписки</span></a>
        <a class="nav-link" data-nav href="/payments"><span class="nav-icon">₽</span><span>Платежи</span></a>
        <a class="nav-link" data-nav href="/subgram"><span class="nav-icon">◉</span><span>SubGram</span></a>
    </div>
    <div class="nav-section">
        <div class="nav-section-title">Коммуникации</div>
        <a class="nav-link" data-nav href="/broadcast"><span class="nav-icon">✦</span><span>Рассылки</span></a>
        <a class="nav-link" data-nav href="/blocked"><span class="nav-icon">⊘</span><span>Чёрный список</span></a>
        <a class="nav-link" data-nav href="/unsubscribed"><span class="nav-icon">○</span><span>Отписавшиеся</span></a>
    </div>
    <div class="nav-section">
        <div class="nav-section-title">Система</div>
        <a class="nav-link" data-nav href="/monitoring"><span class="nav-icon">⌁</span><span>Мониторинг</span></a>
        <a class="nav-link" data-nav href="/webhooks"><span class="nav-icon">⇄</span><span>Вебхуки</span></a>
        <a class="nav-link" data-nav href="/settings"><span class="nav-icon">⚙</span><span>Настройки</span></a>
    </div>
</nav>
<div class="sidebar-footer">
    <a class="nav-link logout-link" href="/admin/logout"><span class="nav-icon">↪</span><span>Выйти</span></a>
</div>
'''

BASE_HTML = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Anonimno — панель управления</title>
    <script>
        (() => {
            try {
                const savedTheme = localStorage.getItem('anonimno-admin-theme');
                const systemTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
                document.documentElement.dataset.theme = savedTheme === 'dark' || savedTheme === 'light' ? savedTheme : systemTheme;
            } catch (_) {
                document.documentElement.dataset.theme = 'light';
            }
        })();
    </script>
    <style>
        :root {
            color-scheme: light;
            --canvas: #f3f7f6; --surface: #ffffff; --surface-soft: #f8fbfa; --ink: #183332; --muted: #738380;
            --line: #dce7e4; --accent: #118b7e; --accent-dark: #0d7168; --accent-soft: #e8f5f2;
            --primary-bg: #118b7e; --primary-hover: #0d7168;
            --danger: #c34f55; --warning: #bb792d; --success: #2f8b66; --sidebar-width: 264px;
            --sidebar: #fbfdfc; --topbar: rgba(243,247,246,.88); --topbar-line: rgba(220,231,228,.9);
            --nav-title: #94a19f; --nav-text: #526765; --nav-hover: #f0f5f3; --logout: #9b4b4f;
            --line-strong: #bdcfcb; --row-line: #edf2f0; --row-hover: #fbfdfc; --field: #fbfdfc;
            --table-head: #f8fbfa; --table-text: #324947; --label: #465e5b; --button-surface: #ffffff; --button-hover: #f7faf9;
            --neutral-badge: #f1f4f3; --neutral-badge-text: #5d706d; --danger-soft: #fff5f5; --danger-line: #dbaeb1;
            --warning-soft: #fffaf3; --warning-hover: #fff5e8; --warning-line: #ead6b9;
            --card-shadow: 0 7px 24px rgba(34,72,68,.035); --card-shadow-hover: 0 10px 28px rgba(34,72,68,.06);
        }
        :root[data-theme="dark"] {
            color-scheme: dark;
            --canvas: #0d1516; --surface: #142022; --surface-soft: #182628; --ink: #edf6f4; --muted: #94aaa6;
            --line: #293a3c; --accent: #43b8a9; --accent-dark: #72d1c4; --accent-soft: #173b38;
            --primary-bg: #247f75; --primary-hover: #1d6c64;
            --danger: #ef8b91; --warning: #e7ac64; --success: #6dcca3;
            --sidebar: #101b1d; --topbar: rgba(13,21,22,.88); --topbar-line: rgba(41,58,60,.92);
            --nav-title: #708582; --nav-text: #a8bbb8; --nav-hover: #192729; --logout: #e58b90;
            --line-strong: #3c5354; --row-line: #243436; --row-hover: #172426; --field: #111d1f;
            --table-head: #111d1f; --table-text: #c7d6d3; --label: #b1c3c0; --button-surface: #172426; --button-hover: #1d2e30;
            --neutral-badge: #1d2c2e; --neutral-badge-text: #aabbb8; --danger-soft: #301d20; --danger-line: #704147;
            --warning-soft: #2d261b; --warning-hover: #382d1d; --warning-line: #685033;
            --card-shadow: 0 8px 28px rgba(0,0,0,.18); --card-shadow-hover: 0 12px 34px rgba(0,0,0,.25);
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html { background: var(--canvas); }
        body { font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--canvas); color: var(--ink); line-height: 1.45; }
        a { color: var(--accent); }
        .sidebar { position: fixed; inset: 0 auto 0 0; z-index: 40; width: var(--sidebar-width); display: flex; flex-direction: column; background: var(--sidebar); border-right: 1px solid var(--line); }
        .sidebar-brand { height: 76px; display: flex; align-items: center; gap: 12px; padding: 0 22px; border-bottom: 1px solid var(--line); }
        .brand-symbol { width: 38px; height: 38px; display: grid; place-items: center; border-radius: 11px; color: white; background: var(--primary-bg); font-weight: 800; box-shadow: 0 8px 18px rgba(17,139,126,.18); }
        .sidebar-brand strong { display: block; font-size: 15px; letter-spacing: -.01em; }
        .sidebar-brand span { display: block; color: var(--muted); font-size: 11px; margin-top: 1px; }
        .sidebar-nav { flex: 1; overflow-y: auto; padding: 18px 14px; }
        .nav-section + .nav-section { margin-top: 19px; }
        .nav-section-title { padding: 0 11px 7px; color: var(--nav-title); font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: .11em; }
        .nav-link { display: flex; align-items: center; gap: 11px; min-height: 40px; padding: 8px 11px; border-radius: 10px; color: var(--nav-text); text-decoration: none; font-size: 13px; font-weight: 620; transition: .16s ease; }
        .nav-link span { color: inherit !important; text-shadow: none !important; }
        .nav-link:hover { color: var(--ink); background: var(--nav-hover); }
        .nav-link.active { color: var(--accent-dark); background: var(--accent-soft); }
        .nav-icon { width: 22px; text-align: center; font-size: 16px; font-weight: 500; }
        .sidebar-footer { padding: 14px; border-top: 1px solid var(--line); }
        .logout-link { color: var(--logout); }
        .main-shell { min-height: 100vh; margin-left: var(--sidebar-width); }
        .topbar { position: sticky; top: 0; z-index: 30; height: 76px; display: flex; align-items: center; justify-content: space-between; padding: 0 32px; background: var(--topbar); border-bottom: 1px solid var(--topbar-line); backdrop-filter: blur(14px); }
        .topbar-context { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 13px; }
        .topbar-actions { display: flex; align-items: center; gap: 16px; margin-left: auto; }
        .live-dot { width: 8px; height: 8px; border-radius: 50%; background: #3ba77b; box-shadow: 0 0 0 4px rgba(59,167,123,.12); }
        .mobile-menu-button { display: none; width: 40px; height: 40px; padding: 0; border: 1px solid var(--line); border-radius: 10px; background: var(--button-surface); color: var(--ink); }
        .theme-toggle { min-height: 38px; padding: 8px 12px; border-color: var(--line); background: var(--button-surface); color: var(--ink); box-shadow: var(--card-shadow); }
        .theme-toggle:hover { border-color: var(--line-strong); background: var(--button-hover); color: var(--ink); }
        .theme-toggle-icon { width: 18px; font-size: 16px; line-height: 1; }
        .theme-toggle-label { font-size: 12px; }
        .sidebar-overlay { display: none; position: fixed; inset: 0; z-index: 35; background: rgba(10,35,33,.32); }
        .container { max-width: 1480px; margin: 0 auto; padding: 30px 32px 48px; }
        .page-title { font-size: 25px; font-weight: 760; margin-bottom: 22px; color: var(--ink); letter-spacing: -.025em; }
        .page-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 24px; }
        .page-heading h1 { font-size: 27px; line-height: 1.2; letter-spacing: -.03em; }
        .page-heading p { color: var(--muted); font-size: 13px; margin-top: 7px; }
        .page-actions { display: flex; flex-wrap: wrap; gap: 8px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; margin-bottom: 22px; }
        .stat-card { min-height: 128px; background: var(--surface); padding: 19px; border-radius: 15px; text-align: left; border: 1px solid var(--line); box-shadow: var(--card-shadow); }
        .stat-card:hover { border-color: var(--line-strong); box-shadow: var(--card-shadow-hover); }
        .stat-number { font-size: 29px; line-height: 1.1; font-weight: 770; color: var(--ink); letter-spacing: -.025em; }
        .stat-label { font-size: 12px; color: var(--muted); margin-top: 8px; }
        .stat-card .icon { width: 30px; height: 30px; margin-bottom: 17px; display: grid; place-items: center; border-radius: 9px; background: var(--accent-soft); color: var(--accent-dark); font-size: 14px; }
        .quick-grid { display: grid; grid-template-columns: 1.35fr .65fr; gap: 14px; }
        .system-status-grid { display: grid; gap: 9px; margin-top: 18px; }
        .system-status-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 11px 12px; border: 1px solid var(--line); border-radius: 11px; background: var(--surface-soft); font-size: 13px; }
        .status-pill { display: inline-flex; align-items: center; gap: 6px; font-weight: 750; font-size: 12px; }
        .status-pill::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
        .status-ok { color: var(--success); }
        .status-degraded { color: var(--warning); }
        .status-down { color: var(--danger); }
        .panel { background: var(--surface); border: 1px solid var(--line); border-radius: 15px; padding: 22px; box-shadow: var(--card-shadow); }
        .panel-title { font-size: 15px; font-weight: 740; margin-bottom: 6px; }
        .panel-copy { color: var(--muted); font-size: 13px; }
        .quick-links { display: grid; gap: 8px; margin-top: 18px; }
        .quick-link { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 13px; color: var(--ink); text-decoration: none; background: var(--surface-soft); border: 1px solid var(--line); border-radius: 11px; font-size: 13px; font-weight: 650; }
        .quick-link:hover { border-color: var(--line-strong); background: var(--accent-soft); }
        .table-container { background: var(--surface); border-radius: 15px; overflow-x: auto; border: 1px solid var(--line); box-shadow: var(--card-shadow); }
        table { width: 100%; min-width: 720px; border-collapse: collapse; font-size: 13px; }
        th { background: var(--table-head); color: var(--muted); font-weight: 750; font-size: 10px; text-transform: uppercase; letter-spacing: .07em; padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--line); }
        td { padding: 12px 14px; border-bottom: 1px solid var(--row-line); font-size: 13px; color: var(--table-text); word-break: break-word; }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: var(--row-hover); }
        .badge { display: inline-block; padding: 4px 9px; border-radius: 999px; font-size: 10px; font-weight: 750; border: 1px solid transparent; white-space: nowrap; }
        .badge-text { background: var(--accent-soft); color: var(--accent-dark); border-color: var(--line); }
        .badge-photo, .badge-video, .badge-voice, .badge-sticker, .badge-video_note { background: var(--neutral-badge); color: var(--neutral-badge-text); border-color: var(--line); }
        .form-container { background: var(--surface); padding: 22px; border-radius: 15px; border: 1px solid var(--line); box-shadow: var(--card-shadow); }
        label { display: block; margin-bottom: 7px; color: var(--label); font-weight: 680; font-size: 13px; }
        textarea, input[type="text"] {
            width: 100%; padding: 11px 13px; border-radius: 11px; border: 1px solid var(--line); background: var(--field); color: var(--ink); font-size: 14px; font-family: inherit; transition: .18s ease;
        }
        textarea { min-height: 132px; resize: vertical; line-height: 1.55; }
        textarea:focus, input[type="text"]:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 4px rgba(17,139,126,.09); background: var(--surface); }
        button, .button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 39px; padding: 9px 15px; border: 1px solid var(--primary-bg); border-radius: 10px; background: var(--primary-bg); color: white; text-decoration: none; font-weight: 700; font-size: 13px; cursor: pointer; transition: .17s ease; width: auto; }
        button:hover, .button:hover { background: var(--primary-hover); border-color: var(--primary-hover); transform: translateY(-1px); }
        button:disabled { opacity: .55; cursor: wait; transform: none; }
        .button-secondary { color: var(--table-text); background: var(--button-surface); border-color: var(--line); }
        .button-secondary:hover { color: var(--ink); background: var(--button-hover); border-color: var(--line-strong); }
        .button-danger { color: var(--danger); background: var(--button-surface); border-color: var(--danger-line); }
        .button-danger:hover { color: var(--danger); background: var(--danger-soft); border-color: var(--danger-line); }
        .button-warning { color: var(--warning); background: var(--warning-soft); border-color: var(--warning-line); }
        .button-warning:hover { color: var(--warning); background: var(--warning-hover); border-color: var(--warning-line); }
        .mt-20 { margin-top: 15px; }
        .mb-20 { margin-bottom: 15px; }
        .empty { text-align: center; padding: 46px 18px; color: var(--muted); font-size: 13px; background: var(--surface); border: 1px dashed var(--line-strong); border-radius: 15px; }
        .result-box { background: var(--surface); padding: 22px; border-radius: 15px; border: 1px solid var(--line) !important; box-shadow: var(--card-shadow); }
        .result-box h2 { color: var(--ink) !important; font-size: 16px; margin-bottom: 18px; }
        .result-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 15px; }
        .result-stats > div { padding: 13px; text-align: left; background: var(--surface-soft); border: 1px solid var(--line); border-radius: 11px; }
        .result-stats .number { font-size: 24px; font-weight: 760; color: var(--ink) !important; }
        .result-stats .label { color: var(--muted); font-size: 10px; margin-top: 5px; }
        .result-message { background: var(--surface-soft); padding: 12px; border-radius: 10px; margin-bottom: 15px; border: 1px solid var(--line); word-break: break-word; font-size: 13px; }
        .glow-button { display: inline-flex; align-items: center; justify-content: center; padding: 9px 15px; min-height: 39px; border: 1px solid var(--line) !important; border-radius: 10px; color: var(--table-text) !important; background: var(--button-surface); text-decoration: none; transition: .17s ease; text-align: center; font-size: 13px; font-weight: 700; width: auto; }
        .glow-button:hover { color: var(--ink) !important; background: var(--button-hover); border-color: var(--line-strong) !important; transform: translateY(-1px); }
        .progress-bar { width: 100%; height: 9px; background: var(--line); border-radius: 999px; overflow: hidden; margin: 14px 0 9px; }
        .progress-fill { height: 100%; background: var(--accent); transition: width .5s ease; border-radius: 999px; width: 0%; }
        .progress-text { color: var(--muted) !important; }
        .status-running { color: var(--accent); }
        .status-paused { color: var(--warning); }
        .status-completed { color: var(--success); }
        .status-pending { color: var(--muted); }
        .status-failed, .status-cancelled { color: var(--danger); }
        .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 20px; }
        .detail-item { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid var(--row-line); }
        .detail-item .label { color: var(--muted); }
        .detail-item .value { color: var(--ink); font-weight: 650; }
        .updating { opacity: 0.5; transition: opacity 0.3s; }
        @media (max-width: 980px) {
            .sidebar { transform: translateX(-102%); transition: transform .22s ease; box-shadow: 16px 0 40px rgba(20,55,52,.13); }
            body.sidebar-open .sidebar { transform: translateX(0); }
            body.sidebar-open .sidebar-overlay { display: block; }
            .main-shell { margin-left: 0; }
            .mobile-menu-button { display: inline-flex; align-items: center; justify-content: center; }
            .topbar { height: 68px; padding: 0 20px; }
            .container { padding: 24px 20px 42px; }
        }
        @media (max-width: 700px) {
            .page-heading { flex-direction: column; }
            .page-heading h1, .page-title { font-size: 22px; }
            .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
            .stat-card { min-height: 110px; padding: 15px; }
            .stat-number { font-size: 24px; }
            .stat-card .icon { margin-bottom: 12px; }
            .quick-grid { grid-template-columns: 1fr; }
            .result-stats { grid-template-columns: repeat(2, 1fr); }
            .form-container, .result-box, .panel { padding: 17px; }
            .detail-grid { grid-template-columns: 1fr; }
            .topbar-context span:last-child { display: none; }
            .topbar-actions { gap: 10px; }
            .theme-toggle-label { display: none; }
            .theme-toggle { width: 40px; padding: 0; }
        }
        @media (max-width: 430px) {
            .container { padding: 20px 14px 36px; }
            .stats { grid-template-columns: 1fr; }
            .stat-card { min-height: 96px; }
            .page-actions, .page-actions .button, .page-actions .glow-button { width: 100%; }
        }
    </style>
</head>
<body>
    <div class="sidebar-overlay" id="sidebar-overlay"></div>
    <aside class="sidebar" id="sidebar">MENU_PLACEHOLDER</aside>
    <main class="main-shell">
        <header class="topbar">
            <button class="mobile-menu-button" id="mobile-menu-button" type="button" aria-label="Открыть меню">☰</button>
            <div class="topbar-actions">
                <div class="topbar-context"><span class="live-dot"></span><span>Система работает</span></div>
                <button class="theme-toggle" id="theme-toggle" type="button" aria-pressed="false">
                    <span class="theme-toggle-icon" id="theme-toggle-icon" aria-hidden="true">☾</span>
                    <span class="theme-toggle-label" id="theme-toggle-label">Тёмная тема</span>
                </button>
            </div>
        </header>
        <div class="container"><div id="content">CONTENT_PLACEHOLDER</div></div>
    </main>
    <script>
        (() => {
            const path = window.location.pathname;
            document.querySelectorAll('[data-nav]').forEach((link) => {
                const target = link.getAttribute('href');
                const active = target === '/' ? path === '/' : path === target || path.startsWith(target + '/');
                link.classList.toggle('active', active);
                if (active) link.setAttribute('aria-current', 'page');
            });
            const openButton = document.getElementById('mobile-menu-button');
            const overlay = document.getElementById('sidebar-overlay');
            const closeSidebar = () => document.body.classList.remove('sidebar-open');
            openButton?.addEventListener('click', () => document.body.classList.toggle('sidebar-open'));
            overlay?.addEventListener('click', closeSidebar);
            document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeSidebar(); });

            const themeStorageKey = 'anonimno-admin-theme';
            const themeButton = document.getElementById('theme-toggle');
            const themeIcon = document.getElementById('theme-toggle-icon');
            const themeLabel = document.getElementById('theme-toggle-label');
            const renderThemeButton = () => {
                const dark = document.documentElement.dataset.theme === 'dark';
                themeIcon.textContent = dark ? '☀' : '☾';
                themeLabel.textContent = dark ? 'Светлая тема' : 'Тёмная тема';
                themeButton.setAttribute('aria-label', dark ? 'Включить светлую тему' : 'Включить тёмную тему');
                themeButton.setAttribute('title', dark ? 'Включить светлую тему' : 'Включить тёмную тему');
                themeButton.setAttribute('aria-pressed', dark ? 'true' : 'false');
            };
            themeButton?.addEventListener('click', () => {
                const nextTheme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
                document.documentElement.dataset.theme = nextTheme;
                try { localStorage.setItem(themeStorageKey, nextTheme); } catch (_) {}
                renderThemeButton();
            });
            renderThemeButton();
        })();
    </script>
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
    if request.url.path in {"/admin/login", "/health"}:
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


@app.get("/health")
async def health_check():
    """Return a lightweight status suitable for monitoring services."""
    try:
        conn = await database.get_conn()
        try:
            await conn.fetchval("SELECT 1")
            worker_alive = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM service_heartbeats
                    WHERE service = 'worker'
                      AND heartbeat_at >= NOW() - INTERVAL '2 minutes'
                )
                """
            )
        finally:
            await database.release_conn(conn)
    except Exception as exc:
        print(f"❌ Health check: {exc}", flush=True)
        return JSONResponse(
            status_code=503,
            content={"status": "down", "database": "error", "worker": "unknown"},
        )

    status = "ok" if worker_alive else "degraded"
    return JSONResponse(
        status_code=200 if worker_alive else 503,
        content={
            "status": status,
            "database": "ok",
            "worker": "ok" if worker_alive else "stale",
        },
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    total_users = 0
    total_messages = 0
    total_subscriptions = 0
    worker_alive = False
    
    try:
        conn = await database.get_conn()
        try:
            stats = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM users) AS total_users,
                    (SELECT COUNT(*) FROM messages) AS total_messages,
                    (
                        SELECT COUNT(*)
                        FROM subscriptions
                        WHERE status = 'active'
                          AND (end_date IS NULL OR end_date > NOW())
                    ) AS total_subscriptions,
                    EXISTS (
                        SELECT 1
                        FROM service_heartbeats
                        WHERE service = 'worker'
                          AND heartbeat_at >= NOW() - INTERVAL '2 minutes'
                    ) AS worker_alive
                """
            )
            total_users = stats["total_users"]
            total_messages = stats["total_messages"]
            total_subscriptions = stats["total_subscriptions"]
            worker_alive = bool(stats["worker_alive"])
        finally:
            await database.release_conn(conn)
    except Exception as e:
        print(f"❌ Ошибка БД в дашборде: {e}", flush=True)
        import traceback
        traceback.print_exc()

    worker_status = "ok" if worker_alive else "degraded"
    worker_label = "Работает" if worker_alive else "Нет свежего сигнала"
    content = f'''
    <div class="page-heading">
        <div>
            <h1>Обзор</h1>
            <p>Ключевые показатели бота и быстрый доступ к ежедневным действиям.</p>
        </div>
        <div class="page-actions">
            <a class="button" href="/broadcast">Создать рассылку</a>
        </div>
    </div>
    <div class="stats">
        <div class="stat-card"><span class="icon">◎</span><div class="stat-number">{total_users}</div><div class="stat-label">Всего пользователей</div></div>
        <div class="stat-card"><span class="icon">◇</span><div class="stat-number">{total_subscriptions}</div><div class="stat-label">Активные подписки</div></div>
        <div class="stat-card"><span class="icon">≋</span><div class="stat-number">{total_messages}</div><div class="stat-label">Сообщений отправлено</div></div>
    </div>
    <div class="quick-grid">
        <section class="panel">
            <div class="panel-title">Быстрые действия</div>
            <p class="panel-copy">Самые частые операции собраны в одном месте.</p>
            <div class="quick-links">
                <a class="quick-link" href="/broadcast"><span>Запустить новую рассылку</span><span>→</span></a>
                <a class="quick-link" href="/subscriptions"><span>Проверить подписки</span><span>→</span></a>
                <a class="quick-link" href="/payments"><span>Открыть платежную статистику</span><span>→</span></a>
            </div>
        </section>
        <section class="panel">
            <div class="panel-title">Состояние системы</div>
            <p class="panel-copy">Проверяется автоматически при открытии и обновляется раз в минуту.</p>
            <div class="system-status-grid">
                <div class="system-status-row"><span>Админ-панель</span><span class="status-pill status-ok" id="admin-health">Работает</span></div>
                <div class="system-status-row"><span>База данных</span><span class="status-pill status-ok" id="database-health">Подключена</span></div>
                <div class="system-status-row"><span>Worker рассылок</span><span class="status-pill status-{worker_status}" id="worker-health">{worker_label}</span></div>
            </div>
            <div class="quick-links">
                <a class="quick-link" href="/monitoring"><span>Открыть мониторинг сервера</span><span>→</span></a>
            </div>
        </section>
    </div>
    <script>
        async function refreshSystemHealth() {{
            try {{
                const response = await fetch('/health', {{ cache: 'no-store' }});
                const data = await response.json();
                const worker = document.getElementById('worker-health');
                const database = document.getElementById('database-health');
                worker.textContent = data.worker === 'ok' ? 'Работает' : 'Нет свежего сигнала';
                worker.className = 'status-pill status-' + (data.worker === 'ok' ? 'ok' : 'degraded');
                database.textContent = data.database === 'ok' ? 'Подключена' : 'Ошибка';
                database.className = 'status-pill status-' + (data.database === 'ok' ? 'ok' : 'down');
            }} catch (error) {{
                document.getElementById('database-health').textContent = 'Нет связи';
                document.getElementById('database-health').className = 'status-pill status-down';
            }}
        }}
        window.setInterval(refreshSystemHealth, 60000);
    </script>
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
    try:
        current_value = await database.get_setting("show_payment_checkboxes", "true")
        confirmation_enabled = str(current_value).lower() == "true"
        status_label = "Включено" if confirmation_enabled else "Отключено"
        status_class = "status-ok" if confirmation_enabled else "status-down"
    except Exception as exc:
        print(f"⚠️ Не удалось получить настройку подтверждения: {exc}", flush=True)
        confirmation_enabled = None
        status_label = "Не удалось проверить"
        status_class = "status-degraded"

    html = '''
    <div class="page-title">⚙️ Настройки</div>
    <div class="form-container">
        <h3 style="margin-bottom: 7px;">Подтверждение оплаты</h3>
        <p style="color: var(--muted); font-size: 13px; margin-bottom: 18px;">Показывает пользователю элементы подтверждения после оплаты.</p>
        <div class="system-status-row" style="margin-bottom: 18px;">
            <span>Текущий статус</span>
            <span id="confirmation-status" class="status-pill __STATUS_CLASS__">__STATUS_LABEL__</span>
        </div>
        <div class="setting-actions">
            <button id="enable-confirmation" onclick="setSetting('true')" class="glow-button setting-choice __ENABLE_ACTIVE__" aria-pressed="__ENABLE_PRESSED__">✅ Включить</button>
            <button id="disable-confirmation" onclick="setSetting('false')" class="glow-button setting-choice __DISABLE_ACTIVE__" aria-pressed="__DISABLE_PRESSED__">❌ Отключить</button>
        </div>
        <div id="setting-feedback" class="setting-feedback" role="status" aria-live="polite"></div>
    </div>
    <style>
        .setting-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
        .setting-choice { width: 100%; }
        .setting-choice.is-active { background: var(--accent-soft); border-color: var(--accent) !important; color: var(--accent-dark) !important; box-shadow: 0 0 0 3px rgba(17, 139, 126, .08); }
        #disable-confirmation.is-active { background: var(--danger-soft); border-color: var(--danger-line) !important; color: var(--danger) !important; box-shadow: 0 0 0 3px rgba(184, 75, 81, .07); }
        .setting-feedback { min-height: 20px; margin-top: 12px; color: var(--success); font-size: 12px; font-weight: 650; }
        .setting-feedback.is-error { color: var(--danger); }
        @media (max-width: 560px) { .setting-actions { grid-template-columns: 1fr; } }
    </style>
    <script>
        const enableButton = document.getElementById('enable-confirmation');
        const disableButton = document.getElementById('disable-confirmation');
        const statusElement = document.getElementById('confirmation-status');
        const feedbackElement = document.getElementById('setting-feedback');

        function renderSetting(enabled) {
            statusElement.textContent = enabled ? 'Включено' : 'Отключено';
            statusElement.className = 'status-pill ' + (enabled ? 'status-ok' : 'status-down');
            enableButton.classList.toggle('is-active', enabled);
            disableButton.classList.toggle('is-active', !enabled);
            enableButton.setAttribute('aria-pressed', enabled ? 'true' : 'false');
            disableButton.setAttribute('aria-pressed', enabled ? 'false' : 'true');
        }

        async function setSetting(value) {
            const enabled = value === 'true';
            enableButton.disabled = true;
            disableButton.disabled = true;
            feedbackElement.textContent = 'Сохраняем…';
            feedbackElement.classList.remove('is-error');
            try {
                const response = await fetch('/api/settings/update', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key: 'show_payment_checkboxes', value: value })
                });
                const data = await response.json();
                if (!response.ok || !data.success) {
                    throw new Error(data.error || 'Не удалось сохранить настройку');
                }
                renderSetting(enabled);
                feedbackElement.textContent = '✓ Настройка сохранена';
            } catch (e) {
                feedbackElement.textContent = 'Ошибка: ' + e.message;
                feedbackElement.classList.add('is-error');
            } finally {
                enableButton.disabled = false;
                disableButton.disabled = false;
            }
        }
    </script>
    '''
    html = (html
        .replace('__STATUS_CLASS__', status_class)
        .replace('__STATUS_LABEL__', status_label)
        .replace('__ENABLE_ACTIVE__', 'is-active' if confirmation_enabled is True else '')
        .replace('__DISABLE_ACTIVE__', 'is-active' if confirmation_enabled is False else '')
        .replace('__ENABLE_PRESSED__', 'true' if confirmation_enabled is True else 'false')
        .replace('__DISABLE_PRESSED__', 'true' if confirmation_enabled is False else 'false'))
    menu = MENU.replace('⚙️ Настройки', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">⚙️ Настройки</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))


@app.post("/api/settings/update")
async def update_setting(request: Request):
    try:
        body = await request.json()
        key = body.get("key")
        value = str(body.get("value", "")).lower()

        if key != "show_payment_checkboxes":
            return JSONResponse(
                content={"success": False, "error": "Неизвестная настройка"},
                status_code=400,
            )
        if value not in {"true", "false"}:
            return JSONResponse(
                content={"success": False, "error": "Некорректное значение"},
                status_code=400,
            )

        await database.set_setting(key, value)
        return JSONResponse(content={"success": True, "value": value})
    except Exception as exc:
        print(f"❌ Не удалось сохранить настройку подтверждения: {exc}", flush=True)
        return JSONResponse(
            content={"success": False, "error": "Не удалось сохранить настройку"},
            status_code=500,
        )


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
            'cancelled': '⛔ Остановлена',
            'archived': '📦 Архив'
        }
        status_label = status_labels.get(status_text, status_text)
        status_class = f"status-{status_text}"
        
        result_html = f'''
        <div class="result-box" id="broadcast-stats">
            <div class="page-heading" style="margin-bottom:18px;">
                <div>
                    <h2>Рассылка #{job_id} · <span class="status-value {status_class}">{status_label}</span></h2>
                    <p>Статус автоматически обновляется каждые 5 секунд.</p>
                </div>
            </div>
            <div class="result-stats">
                <div class="stat-total"><div class="number">{total}</div><div class="label">Получателей</div></div>
                <div class="stat-sent"><div class="number">{sent}</div><div class="label">Отправлено</div></div>
                <div class="stat-failed"><div class="number">{failed}</div><div class="label">Ошибок</div></div>
                <div class="stat-remaining"><div class="number">{remaining}</div><div class="label">Осталось</div></div>
            </div>
            <div class="progress-bar">
                <div class="progress-fill" style="width:{min(progress, 100)}%;"></div>
            </div>
            <p class="progress-text" style="font-size:12px;">{progress:.1f}% · Обработано {processed} из {total}</p>
            
            <div class="page-actions" style="margin-top:18px;">
                <button onclick="pauseBroadcast()" id="pauseBtn" class="button-warning"
                        style="display: {'inline-flex' if status_text in ('running', 'pending') else 'none'};">
                    Поставить на паузу
                </button>
                <button onclick="archiveBroadcast()" id="archiveBtn" class="button-danger"
                        style="display: {'none' if status_text == 'archived' else 'inline-flex'};">
                    Архивировать задачу
                </button>
                <a href="/broadcast" class="glow-button">Обновить</a>
                <a href="/broadcast/list" class="glow-button">Открыть историю</a>
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
                            'cancelled': '⛔ Остановлена',
                            'archived': '📦 Архив'
                        }};
                        document.querySelector('.status-value').textContent = statusLabel[data.status] || data.status;
                        document.querySelector('.status-value').className = 'status-value status-' + data.status;

                        const pauseBtn = document.getElementById('pauseBtn');
                        if (pauseBtn) {{
                            pauseBtn.style.display = ['running', 'pending'].includes(data.status) ? 'inline-flex' : 'none';
                        }}
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
                    btn.textContent = 'Поставить на паузу';
                    btn.disabled = false;
                }}
            }}

            async function archiveBroadcast() {{
                if (!confirm('📦 Переместить рассылку #${{jobId}} в архив? История сохранится.')) return;

                const btn = document.getElementById('archiveBtn');
                btn.textContent = '⏳ Архивация...';
                btn.disabled = true;

                try {{
                    const response = await fetch(`/broadcast/archive/${{jobId}}`, {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }}
                    }});
                    const data = await response.json();
                    if (data.success) {{
                        alert('✅ ' + data.message);
                        window.location.href = '/broadcast/list';
                    }} else {{
                        alert('❌ Ошибка: ' + data.error);
                    }}
                }} catch (e) {{
                    alert('❌ Ошибка: ' + e.message);
                }} finally {{
                    btn.textContent = 'Архивировать задачу';
                    btn.disabled = false;
                }}
            }}

            // Обновляем каждые 5 секунд
            setInterval(updateStatus, 5000);
            updateStatus();
        </script>
        '''
    
    html = f'''
    <div class="page-heading">
        <div>
            <h1>Рассылки</h1>
            <p>Создавайте сообщения и отслеживайте выполнение каждой задачи.</p>
        </div>
        <div class="page-actions">
            <a href="/broadcast/list" class="button button-secondary">История рассылок</a>
        </div>
    </div>
    <div class="stats">
        <div class="stat-card"><span class="icon">◎</span><div class="stat-number">{total_users}</div><div class="stat-label">Доступно получателей</div></div>
    </div>
    {result_html}
    <div class="form-container" style="margin-top:14px;">
        <div class="panel-title">Новая рассылка</div>
        <p class="panel-copy" style="margin-bottom:18px;">Сообщение будет поставлено в очередь для всех доступных пользователей.</p>
        <form action="/broadcast/send" method="post">
            <label for="message">Текст сообщения</label>
            <textarea id="message" name="message" rows="5" placeholder="Введите текст рассылки..." required></textarea>
            <div class="mt-20">
                <button type="submit">Запустить рассылку</button>
            </div>
        </form>
    </div>
    '''
    
    menu = MENU.replace('📢 Рассылка', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">📢 Рассылка</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

@app.post("/broadcast/send")
async def send_broadcast(request: Request, message: str = Form(...)):
    if not message or len(message.strip()) == 0:
        return RedirectResponse(url="/broadcast?error=empty", status_code=303)

    try:
        async with database.connection() as conn:
            async with conn.transaction():
                job_id = await conn.fetchval(
                    """
                    INSERT INTO broadcast_jobs (message, status, created_at)
                    VALUES ($1, 'pending', NOW())
                    RETURNING id
                    """,
                    message.strip(),
                )
                await conn.execute(
                    """
                    INSERT INTO broadcast_job_recipients (job_id, user_id)
                    SELECT $1, users.telegram_id
                    FROM users
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM unsubscribed_users
                        WHERE unsubscribed_users.telegram_id = users.telegram_id
                    )
                    ON CONFLICT (job_id, user_id) DO NOTHING
                    """,
                    job_id,
                )
                total_users = await conn.fetchval(
                    "SELECT COUNT(*) FROM broadcast_job_recipients WHERE job_id = $1",
                    job_id,
                )
                if not total_users:
                    await conn.execute("DELETE FROM broadcast_jobs WHERE id = $1", job_id)
                    return RedirectResponse(url="/broadcast?error=no_users", status_code=303)
                await conn.execute(
                    "UPDATE broadcast_jobs SET total_users = $2 WHERE id = $1",
                    job_id,
                    total_users,
                )
    except Exception as e:
        print(f"❌ Ошибка отправки рассылки: {e}")
        return RedirectResponse(url="/broadcast?error=send_failed", status_code=303)
    
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
        async with conn.transaction():
            job = await conn.fetchrow("SELECT status FROM broadcast_jobs WHERE id = $1 FOR UPDATE", job_id)
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
            await conn.execute(
                """
                UPDATE broadcast_job_recipients
                SET status = 'pending', updated_at = NOW()
                WHERE job_id = $1 AND status = 'processing'
                """,
                job_id,
            )
        
        return JSONResponse(
            content={
                "success": True,
                "message": f"Рассылка #{job_id} поставлена на паузу",
                "job_id": job_id
            }
        )

@app.post("/broadcast/archive/{job_id}")
async def archive_broadcast(job_id: int):
    async with database.connection() as conn:
        async with conn.transaction():
            job = await conn.fetchrow("SELECT id FROM broadcast_jobs WHERE id = $1 FOR UPDATE", job_id)
            if not job:
                return JSONResponse(
                    content={"success": False, "error": "Задача не найдена"},
                    status_code=404
                )

            await conn.execute(
                """
                UPDATE broadcast_jobs
                SET status = 'archived', archived_at = NOW(), heartbeat_at = NULL
                WHERE id = $1
                """,
                job_id,
            )
            await conn.execute(
                """
                UPDATE broadcast_job_recipients
                SET status = 'cancelled', updated_at = NOW()
                WHERE job_id = $1 AND status = 'processing'
                """,
                job_id,
            )

        return JSONResponse(
            content={
                "success": True,
                "message": f"Рассылка #{job_id} перемещена в архив",
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
    
    html = '''
    <div class="page-heading">
        <div>
            <h1>История рассылок</h1>
            <p>Последние 50 задач с итоговыми статусами и прогрессом.</p>
        </div>
        <div class="page-actions">
            <a href="/broadcast" class="button">Новая рассылка</a>
        </div>
    </div>
    '''
    
    if rows:
        html += '<div class="table-container"><table><tr><th>ID</th><th>Статус</th><th>Всего</th><th>Отправлено</th><th>Ошибок</th><th>Прогресс</th><th>Создана</th><th>Действие</th></tr>'
        for row in rows:
            status_color = {
                'pending': '#8899aa',
                'running': '#7c3aed',
                'paused': '#ffaa44',
                'completed': '#44ff88',
                'failed': '#ff4488',
                'cancelled': '#ff6644',
                'archived': '#708090'
            }.get(row['status'], '#8899aa')
            
            status_labels = {
                'pending': '⏳ Ожидание',
                'running': '🔄 Выполняется',
                'paused': '⏸️ Пауза',
                'completed': '✅ Завершено',
                'failed': '❌ Ошибка',
                'cancelled': '⛔ Остановлена',
                'archived': '📦 Архив'
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
                <td><a href="/broadcast?job={row['id']}" style="font-weight:700; text-decoration:none;">Подробнее →</a></td>
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

        bot_info = bots_info[0] if bots_info else None

        html = f'''
        <div class="page-title">📊 SubGram Статистика</div>

        <div class="stats">
            <div class="stat-card">
                <div class="stat-number">{balance} ₽</div>
                <div class="stat-label">Баланс аккаунта</div>
            </div>
        '''

        if bot_info:
            html += f'''
            <div class="stat-card">
                <div class="stat-number">{bot_info.get("total_followers", 0)}</div>
                <div class="stat-label">Подписчиков приведено</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{bot_info.get("revenue", 0)} ₽</div>
                <div class="stat-label">Заработано</div>
            </div>
            '''

        html += '</div>'

        if error_message:
            html += f'<div class="empty" style="color:#ff4488;">❌ Ошибка: {error_message}</div>'

        if not bots_info:
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
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        page = 1

    page_size = 20
    payments = []
    has_next_page = False
    api_error = None
    try:
        now = now_msk()
        period_end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        period_start = period_end - timedelta(days=1)

        # All user payments must belong to a user known by the bot. Starting
        # from the first local record avoids thousands of empty API requests
        # while still covering the complete history of this project.
        conn = await database.get_conn()
        try:
            first_record = await conn.fetchval(
                """
                SELECT LEAST(
                    COALESCE((SELECT MIN(registered_at) FROM users), NOW()),
                    COALESCE((SELECT MIN(created_at) FROM subscriptions), NOW())
                )
                """
            )
        finally:
            await database.release_conn(conn)

        if first_record:
            if first_record.tzinfo is None:
                first_record = first_record.replace(tzinfo=now.tzinfo)
            period_start = first_record.astimezone(now.tzinfo).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(days=1)

        result = await CloudPaymentsClient().list_payments_all_time(
            created_date_gte=period_start,
            created_date_lte=period_end,
        )
        if result.get("success"):
            api_payments = result.get("payments", [])
            page_offset = (page - 1) * page_size
            payments = api_payments[page_offset:page_offset + page_size]
            has_next_page = page_offset + page_size < len(api_payments)
        else:
            api_error = result.get("error") or "Не удалось получить операции"
    except Exception as e:
        print(f"❌ Ошибка загрузки операций CloudPayments: {e}", flush=True)
        api_error = "CloudPayments временно недоступен"

    total_payments = len(payments)
    total_amount = sum(float(item.get("Amount") or 0) for item in payments)
    successful_payments = sum(1 for item in payments if item.get("Status") == "Completed")
    failed_payments = total_payments - successful_payments

    html = f'''
    <div class="page-heading">
        <div>
            <h1>💳 Платежи CloudPayments</h1>
            <p>Все операции из CloudPayments за всё время. Показаны успешные и неуспешные транзакции.</p>
        </div>
        <div class="page-actions">
            <a class="button button-secondary" href="/payments">Обновить</a>
        </div>
    </div>

    <div class="stats">
        <div class="stat-card"><div class="stat-number">{total_payments}</div><div class="stat-label">Операций на странице</div></div>
        <div class="stat-card"><div class="stat-number">{total_amount:.2f} ₽</div><div class="stat-label">Сумма на странице</div></div>
        <div class="stat-card"><div class="stat-number" style="color:var(--success);">{successful_payments}</div><div class="stat-label">✅ Успешных</div></div>
        <div class="stat-card"><div class="stat-number" style="color:var(--danger);">{failed_payments}</div><div class="stat-label">❌ Неуспешных</div></div>
    </div>
    '''

    if api_error:
        html += f'<div class="empty">⚠️ Не удалось получить операции CloudPayments: {escape(str(api_error))}</div>'
    elif payments:
        html += '''
        <div class="table-container">
            <table>
                <tr><th>Пользователь</th><th>Сумма</th><th>Статус</th><th>Дата</th><th>TransactionId</th></tr>
        '''
        for payment in payments:
            account_id = escape(str(payment.get("AccountId") or "—"))
            transaction_id = escape(str(payment.get("TransactionId") or "—"))
            status = str(payment.get("Status") or "Unknown")
            status_text = "✅ Успешный" if status == "Completed" else f"❌ {status}"
            status_class = "status-ok" if status == "Completed" else "status-down"
            amount = float(payment.get("Amount") or 0)
            created_at = payment.get("CreatedDateIso") or payment.get("CreatedDate") or "—"
            created_at = escape(str(created_at).replace("T", " ")[:19])
            html += f'''
                <tr>
                    <td><strong>{account_id}</strong></td>
                    <td>{amount:.2f} ₽</td>
                    <td><span class="status-pill {status_class}">{escape(status_text)}</span></td>
                    <td>{created_at}</td>
                    <td>{transaction_id}</td>
                </tr>
            '''
        html += '</table></div>'
        previous = f'<a class="button button-secondary" href="/payments?page={page - 1}">← Предыдущая</a>' if page > 1 else ''
        next_page = f'<a class="button button-secondary" href="/payments?page={page + 1}">Следующая →</a>' if has_next_page else ''
        html += f'<div class="page-actions" style="justify-content:space-between;margin-top:16px;"><span class="panel-copy">Страница {page}</span><div style="display:flex;gap:8px;">{previous}{next_page}</div></div>'
    else:
        html += '<div class="empty">За всё время операций CloudPayments нет.</div>'

    menu = MENU.replace('💳 Платежи', '<span style="color:#7c3aed; text-shadow: 0 0 30px #7c3aed44;">💳 Платежи</span>')
    return HTMLResponse(content=BASE_HTML.replace('MENU_PLACEHOLDER', menu).replace('CONTENT_PLACEHOLDER', html))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
