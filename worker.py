import os
import sys
import json
import asyncio
import time
import httpx
import psutil
from datetime import datetime, timedelta
import database
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

async def process_job(job):
    job_id = job['id']
    user_ids = json.loads(job['user_ids_json'])
    message = job['message']
    
    await update_job_status(job_id, 'running', started_at=datetime.now())
    
    total = len(user_ids)
    results = {'sent': 0, 'failed': 0, 'blocked': 0}
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "✍️ Ответить", "callback_data": "reply_anon"}],
            [{"text": "👀 Узнать кто это", "callback_data": "who_is_it", "style": "primary"}]
        ]
    }
    message_text = f"✉️ Вам пришло анонимное сообщение:\n\n{message}"
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i, user_id in enumerate(user_ids):
            if i % 10 == 0 and i > 0:
                cpu = psutil.cpu_percent()
                if cpu > 80:
                    await update_job_status(job_id, 'paused', paused_at=datetime.now())
                    while psutil.cpu_percent() > 60:
                        await asyncio.sleep(5)
                    await update_job_status(job_id, 'running')
            
            try:
                payload = {
                    "chat_id": user_id,
                    "text": message_text,
                    "parse_mode": "HTML",
                    "reply_markup": keyboard
                }
                response = await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json=payload
                )
                if response.status_code == 200:
                    results['sent'] += 1
                else:
                    if "bot was blocked" in str(response.json()).lower():
                        results['blocked'] += 1
                        user_data = await database.get_user(user_id)
                        if user_data:
                            await database.add_blocked_user(
                                user_id,
                                user_data.get('username', ''),
                                user_data.get('first_name', '')
                            )
                    else:
                        results['failed'] += 1
            except Exception:
                results['failed'] += 1
            
            if i % 5 == 0 or i == total - 1:
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
    
    await update_job_status(
        job_id, 'completed',
        sent_users=results['sent'],
        failed_users=results['failed'],
        blocked_users=results['blocked'],
        progress=100,
        finished_at=datetime.now()
    )
    print(f"✅ Задача {job_id} завершена")

async def update_job_status(job_id, status, **kwargs):
    conn = await database.get_conn()
    try:
        fields = []
        values = []
        for key, val in kwargs.items():
            fields.append(f"{key} = ${len(values)+1}")
            values.append(val)
        values.append(job_id)
        query = f"UPDATE broadcast_jobs SET {', '.join(fields)} WHERE id = ${len(values)}"
        await conn.execute(query, *values)
    finally:
        await database.release_conn(conn)

async def main_loop():
    print("🚀 Воркер запущен, ждём задачи...")
    while True:
        try:
            conn = await database.get_conn()
            job = await conn.fetchrow(
                "SELECT * FROM broadcast_jobs WHERE status = 'pending' ORDER BY created_at LIMIT 1"
            )
            await database.release_conn(conn)
            
            if job:
                print(f"📨 Найдена задача {job['id']}, начинаем обработку...")
                await process_job(job)
            else:
                await asyncio.sleep(5) 
        except Exception as e:
            print(f"❌ Ошибка: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main_loop())
