import asyncio
import json
import os
from wakaq import WakaQ, task
import httpx
import psutil
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))

wakaq = WakaQ(
    queues=[
        (0, 'high-priority'),
        (1, 'medium-priority'),
        'low-priority',
    ],
    concurrency="cores*4",
    soft_timeout=3600,
    hard_timeout=7200,
    max_retries=3,
    redis_host=REDIS_HOST,
    redis_port=REDIS_PORT,
    redis_db=REDIS_DB,
)

async def update_job_status(job_id: int, status: str, **kwargs):
    import database
    conn = await database.get_conn()
    try:
        fields = []
        values = []
        for key, val in kwargs.items():
            fields.append(f"{key} = ${len(values)+1}")
            values.append(val)
        values.append(job_id)
        if fields:
            query = f"UPDATE broadcast_jobs SET {', '.join(fields)} WHERE id = ${len(values)}"
            await conn.execute(query, *values)
        else:
            await conn.execute("UPDATE broadcast_jobs SET status = $1 WHERE id = $2", status, job_id)
    finally:
        await database.release_conn(conn)

def get_cpu_usage():
    return psutil.cpu_percent(interval=0.5)

def get_memory_usage():
    return psutil.virtual_memory().percent

@wakaq.task(queue='medium-priority', max_retries=3)
async def broadcast_task(user_ids_json: str, message: str, job_id: int):
    """Задача для рассылки - выполняется в воркере"""
    await send_broadcast_with_monitoring(user_ids_json, message, job_id)

async def send_broadcast_with_monitoring(user_ids_json: str, message: str, job_id: int):
    """Отправляет рассылку с мониторингом"""
    user_ids = json.loads(user_ids_json)
    total = len(user_ids)
    
    results = {
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
    
    message_text = f"✉️ Вам пришло анонимное сообщение:\n\n{message}"
    
    await update_job_status(job_id, 'running', started_at=asyncio.get_event_loop().time())
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i, user_id in enumerate(user_ids):
            if i % 10 == 0 and i > 0:
                cpu = get_cpu_usage()
                memory = get_memory_usage()
                await update_job_status(job_id, 'running', cpu_usage=cpu, memory_usage=memory)
                
                if cpu > 80:
                    await update_job_status(job_id, 'paused', paused_at=asyncio.get_event_loop().time())
                    while get_cpu_usage() > 60:
                        await asyncio.sleep(5)
                    await update_job_status(job_id, 'running')
            
            try:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
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
                        "bot was blocked", "forbidden", "user is deactivated",
                        "bot was kicked", "chat not found", "can't send messages to user"
                    ]):
                        results['blocked'] += 1
                        import database
                        user_data = await database.get_user(user_id)
                        if user_data:
                            await database.add_blocked_user(
                                user_id,
                                user_data.get('username', ''),
                                user_data.get('first_name', '')
                            )
                    else:
                        results['failed'] += 1
                        results['errors'].append(f"User {user_id}: {response.status_code}")
                        
            except Exception as e:
                results['failed'] += 1
                results['errors'].append(f"User {user_id}: {str(e)[:50]}")
            
            if i % 50 == 0 or i == total - 1:
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
        finished_at=asyncio.get_event_loop().time()
    )