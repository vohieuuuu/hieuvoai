# server.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import threading
import time
from .config import MAX_WORKERS, QUEUE_TIMEOUT
from .utils import generate_conversation_id, setup_logging
from .session_manager import get_driver_for_conversation, cleanup_inactive_drivers, _init_standby_pool, chrome_options_func
from .selenium_worker import send_prompt_and_get_response, google_login_if_needed
import logging
import os
import shutil
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from functools import lru_cache
from threading import Thread

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)
logger = setup_logging()

# Định kỳ cleanup session không hoạt động
CLEANUP_INTERVAL = 900  # 15 phút

logging.getLogger("seleniumwire").setLevel(logging.WARNING)

# Cấu hình logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# Cache cho các câu hỏi thường gặp
@lru_cache(maxsize=100)
def get_cached_response(question):
    return None

def chrome_options_func(profile_dir):
    from seleniumwire import webdriver
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    import os
    
    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-logging")
    options.add_argument("--log-level=3")
    options.add_argument("--silent")
    options.add_argument("--window-size=300,300")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-software-rasterizer")
    
    # Thêm các options để tránh lỗi Win32
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    
    # Sử dụng ChromeDriver từ thư mục .wdm
    driver_path = os.path.join(os.environ['USERPROFILE'], '.wdm', 'drivers', 'chromedriver', 'win64', '137.0.7151.70', 'chromedriver-win32', 'chromedriver.exe')
    service = Service(executable_path=driver_path)
    
    return options, service

# Khởi tạo standby pool với login_func khi server khởi động
_init_standby_pool(chrome_options_func, login_func=google_login_if_needed)

def cleanup_loop():
    while True:
        cleanup_inactive_drivers(timeout_minutes=30, chrome_options_func=chrome_options_func, login_func=google_login_if_needed)
        time.sleep(CLEANUP_INTERVAL)

cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
cleanup_thread.start()

# Cleanup profile khi khởi động server (KHÔNG kill process toàn cục)
try:
    chrome_profiles_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../chrome_profiles"))
    if os.path.exists(chrome_profiles_dir):
        shutil.rmtree(chrome_profiles_dir)
        print(f"[CLEANUP] Deleted chrome_profiles at {chrome_profiles_dir}")
except Exception as e:
    print(f"[CLEANUP][ERROR] {e}")

def cleanup_task():
    while True:
        try:
            cleanup_inactive_drivers(timeout_minutes=15, chrome_options_func=chrome_options_func)
            time.sleep(60)  # Cleanup mỗi phút
        except Exception as e:
            logger.error(f"Error in cleanup task: {str(e)}")
            time.sleep(60)

@app.route('/ask', methods=['POST'])
def ask():
    try:
        data = request.get_json()
        if not data or 'question' not in data:
            return jsonify({"error": "Missing question in request"}), 400

        question = data['question']
        conversation_id = data.get('conversation_id', 'default')

        # Kiểm tra cache
        cached_response = get_cached_response(question)
        if cached_response:
            logger.info(f"[CACHE] Using cached response for question: {question[:50]}...")
            return jsonify({"response": cached_response})

        # Lấy driver từ pool
        try:
            driver, email, password = get_driver_for_conversation(
                conversation_id,
                chrome_options_func,
                None  # Không cần login_func vì đã có profile
            )
        except Exception as e:
            logger.error(f"[ERROR] Failed to get driver: {str(e)}")
            return jsonify({"error": "Failed to initialize browser"}), 500

        # Gửi câu hỏi và lấy câu trả lời
        try:
            response = send_prompt_and_get_response(driver, question)
            if response:
                # Cache câu trả lời
                get_cached_response.cache_clear()  # Xóa cache cũ
                get_cached_response(question)  # Thêm vào cache mới
                return jsonify({"response": response})
            else:
                return jsonify({"error": "No response received"}), 500
        except Exception as e:
            logger.error(f"[ERROR] Failed to get response: {str(e)}")
            return jsonify({"error": "Failed to get response"}), 500

    except Exception as e:
        logger.error(f"[ERROR] Unexpected error: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"})

if __name__ == '__main__':
    # Khởi tạo pool Chrome
    _init_standby_pool(chrome_options_func)
    
    # Bắt đầu cleanup task
    cleanup_thread = Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()
    
    # Chạy server
    app.run(host='0.0.0.0', port=5000) 