import os
import sys
import json
import asyncio
import httpx
import psutil
from datetime import datetime
import database
from dotenv import load_dotenv
from time_utils import now_msk

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

async def get_job_status(job_id: int) -> str:
    conn = await database.get_conn()
    try:
        row = await conn.fetchrow("SELECT status FROM broadcast_jobs WHERE id = $1", job_id)
        return row['status'] if row else None
    finally:
        await database.release_conn(conn)

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

async def process_job(job):
    job_id = job['id']
    
    await update_job_status(job_id, 'running', started_at=now_msk())
    
    user_ids = json.loads(job['user_ids_json'])
    message = job['message']
    
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
            if i % 10 == 0:
                current_status = await get_job_status(job_id)
                if current_status == 'paused':
                    print(f"⏸️ Задача {job_id} поставлена на паузу. Остановка на {i} сообщении...")
                    await update_job_status(
                        job_id, 'paused',
                        sent_users=results['sent'],
                        failed_users=results['failed'],
                        blocked_users=results['blocked'],
                        progress=((i) / total) * 100 if total > 0 else 0
                    )
                    return
                elif current_status == 'cancelled':
                    print(f"⛔ Задача {job_id} отменена. Остановка...")
                    await update_job_status(
                        job_id, 'cancelled',
                        sent_users=results['sent'],
                        failed_users=results['failed'],
                        blocked_users=results['blocked'],
                        progress=((i) / total) * 100 if total > 0 else 0
                    )
                    return
            
            if i % 10 == 0 and i > 0:
                cpu = psutil.cpu_percent()
                if cpu > 80:
                    await update_job_status(job_id, 'paused', paused_at=now_msk())
                    while psutil.cpu_percent() > 60:
                        await asyncio.sleep(5)
                    current_status = await get_job_status(job_id)
                    if current_status == 'cancelled':
                        print(f"⛔ Задача {job_id} отменена во время паузы CPU. Остановка...")
                        return
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
                    error_text = str(response.json()).lower()
                    if "bot was blocked" in error_text or "forbidden" in error_text:
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
                progress = ((i + 1) / total) * 100 if total > 0 else 0
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
        finished_at=now_msk()
    )
    print(f"✅ Задача {job_id} завершена")

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
