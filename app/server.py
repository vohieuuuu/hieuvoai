# server.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import threading
import time
from .config import MAX_WORKERS, QUEUE_TIMEOUT
from .utils import generate_conversation_id, setup_logging
from .session_manager import get_driver_for_conversation, cleanup_inactive_drivers, _init_standby_pool
from .selenium_worker import send_prompt_and_get_response, google_login_if_needed
import logging
import os
import shutil
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)
logger = setup_logging()

# Định kỳ cleanup session không hoạt động
CLEANUP_INTERVAL = 900  # 15 phút

logging.getLogger("seleniumwire").setLevel(logging.WARNING)

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

@app.route("/query", methods=["POST"])
def query_api():
    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "").strip()
    conversation_id = data.get("conversation_id")
    if not prompt:
        return jsonify(error="Empty prompt"), 400
    
    # Tạo query_id duy nhất cho mỗi request
    query_id = f"{conversation_id}_{int(time.time())}"
    
    # Nếu không có conversation_id, tạo mới
    new_conversation = False
    if not conversation_id:
        conversation_id = generate_conversation_id()
        new_conversation = True
    
    # Lấy hoặc tạo driver cho conversation_id
    driver, email, password = get_driver_for_conversation(
        conversation_id, chrome_options_func, login_func=google_login_if_needed
    )
    
    # Gửi prompt và lấy kết quả với query_id
    answer = send_prompt_and_get_response(driver, prompt, query_id)
    
    response = {"answer": answer, "conversation_id": conversation_id, "email": email}
    if new_conversation:
        response["new_conversation"] = True
    return jsonify(response), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5678, debug=False) 