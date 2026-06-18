import os
import subprocess
import hmac
import hashlib
from flask import Flask, request, jsonify

app = Flask(__name__)

# Конфигурация
REPO_PATH = "/root/Anonimno-Artem-"
SECRET = "1f0r2a9g3p8o4i7n5t6"
BOT_SERVICE = "telegram-bot.service"

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    """Обрабатывает POST-запрос от GitHub"""
    
    # 1. Проверяем секретный токен (безопасность)
    signature = request.headers.get('X-Hub-Signature-256')
    if signature and SECRET:
        # Вычисляем подпись
        hash_object = hmac.new(
            SECRET.encode('utf-8'),
            msg=request.data,
            digestmod=hashlib.sha256
        )
        expected_signature = f"sha256={hash_object.hexdigest()}"
        
        # Сравниваем подписи
        if not hmac.compare_digest(signature, expected_signature):
            return jsonify({"error": "Invalid signature"}), 401
    
    # 2. Проверяем, что это событие push
    event_type = request.headers.get('X-GitHub-Event')
    if event_type != 'push':
        return jsonify({"message": "Ignored event"}), 200
    
    # 3. Выполняем git pull
    try:
        result = subprocess.run(
            ['git', 'pull'],
            cwd=REPO_PATH,
            capture_output=True,
            text=True,
            timeout=30
        )
        output = result.stdout + result.stderr
        print(f"Git pull output:\n{output}")
        
        if result.returncode != 0:
            return jsonify({"error": "Git pull failed", "details": output}), 500
            
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Git pull timeout"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
    # 4. Перезапускаем бота через systemd
    try:
        # restart бота
        subprocess.run(
            ['systemctl', 'restart', BOT_SERVICE],
            check=True,
            capture_output=True,
            text=True,
            timeout=10
        )
        print(f"✅ Бот {BOT_SERVICE} перезапущен успешно")
        
        return jsonify({"status": "success", "message": "Bot restarted"}), 200
        
    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"Failed to restart bot: {e.stderr}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Простая проверка, что обработчик жив"""
    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    # Запускаем Flask-сервер на всех интерфейсах, порт 5000
    # В продакшене лучше использовать Gunicorn или uWSGI
    app.run(host='0.0.0.0', port=5000, debug=False)