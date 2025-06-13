# server.py

import os
import time
import random
import traceback
import atexit
import threading
import logging
from typing import Optional, Dict, Tuple, List
from functools import lru_cache
from datetime import datetime, timedelta
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor
import uuid

from flask import Flask, request, jsonify
from flask_cors import CORS
from seleniumwire import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException, StaleElementReferenceException

# -------------------
# CẤU HÌNH LOGGING
# -------------------
logging.basicConfig(
    level=logging.INFO,  # Chỉ log INFO trở lên
    format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# -------------------
# CẤU HÌNH CHUNG
# -------------------
TARGET_URL = (
    "https://notebooklm.google.com/notebook/"
    "664e57ea-4ccb-4f56-b662-6e90a55226a5"
)
PROXY = "103.82.27.49:37938:sp06v210-37938:GEDAS"
LOADING_CSS = "div.loading-indicator"

# Cấu hình timeout và retry
TIMEOUT = 60  # Timeout cho mỗi operation
MAX_RETRIES = 3  # Số lần retry tối đa
CACHE_TTL = 3600  # Cache TTL trong giây
CAPTCHA_RETRY_DELAY = 300  # Thời gian chờ sau khi gặp CAPTCHA (5 phút)
MAX_WORKERS = 3  # Số lượng worker threads tối đa
QUEUE_TIMEOUT = 300  # Timeout cho queue (5 phút)
ELEMENT_WAIT_TIMEOUT = 10  # Timeout cho việc đợi element

# Các selector để phát hiện CAPTCHA
CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']",
    "div.g-recaptcha",
    "div.recaptcha-checkbox-border",
    "div[data-sitekey]",
    "iframe[title*='reCAPTCHA']"
]

# Cập nhật selector cho element nhập prompt
PROMPT_SELECTOR = "textarea.query-box-input[aria-label='Hộp truy vấn']"

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# Khởi tạo queue và thread pool
request_queue = Queue()
response_dict: Dict[str, Tuple[Optional[str], threading.Event]] = {}
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
driver_pool: Dict[int, Optional[webdriver.Chrome]] = {}
driver_lock = threading.Lock()
response_cache: Dict[str, tuple[str, datetime]] = {}
last_captcha_time: Optional[datetime] = None

# Thư mục Chrome profile để duy trì session
PROFILE_DIR = os.path.abspath("chrome_profile")

class RequestContext:
    """Class để theo dõi context của mỗi request"""
    def __init__(self, request_id: str, prompt: str):
        self.request_id = request_id
        self.prompt = prompt
        self.start_time = time.time()
        self.message_count_before = 0
        self.last_message_id = None
        self.query_id = str(uuid.uuid4())[:8]  # Unique ID cho câu hỏi
        self.response_id = None  # ID của câu trả lời tương ứng
        logger.info(f"Created new request context: {self.request_id} with query_id: {self.query_id}")

def is_captcha_present(drv: webdriver.Chrome) -> bool:
    """Kiểm tra xem có CAPTCHA không"""
    try:
        for selector in CAPTCHA_SELECTORS:
            elements = drv.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                return True
        return False
    except:
        return False

def handle_captcha(drv: webdriver.Chrome) -> bool:
    """Xử lý khi gặp CAPTCHA"""
    global last_captcha_time
    
    if is_captcha_present(drv):
        print("CAPTCHA detected! Waiting for manual intervention...")
        last_captcha_time = datetime.now()
        
        # Chờ người dùng giải CAPTCHA thủ công
        try:
            WebDriverWait(drv, 300).until_not(
                lambda d: is_captcha_present(d)
            )
            print("CAPTCHA solved!")
            return True
        except TimeoutException:
            print("Timeout waiting for CAPTCHA solution")
            return False
    return True

def should_wait_for_captcha() -> bool:
    """Kiểm tra xem có nên chờ thêm không sau khi gặp CAPTCHA"""
    if last_captcha_time is None:
        return False
    
    time_since_captcha = datetime.now() - last_captcha_time
    return time_since_captcha.total_seconds() < CAPTCHA_RETRY_DELAY

def get_cached_response(prompt: str) -> Optional[str]:
    """Lấy kết quả từ cache nếu còn hạn"""
    if prompt in response_cache:
        response, timestamp = response_cache[prompt]
        if datetime.now() - timestamp < timedelta(seconds=CACHE_TTL):
            return response
        del response_cache[prompt]
    return None

def cache_response(prompt: str, response: str):
    """Lưu kết quả vào cache"""
    response_cache[prompt] = (response, datetime.now())

def init_browser(headless: bool = False) -> webdriver.Chrome:
    """Khởi tạo trình duyệt với các tối ưu hóa"""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    options = webdriver.ChromeOptions()
    
    # Tối ưu hóa Chrome
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-logging")
    options.add_argument("--log-level=3")
    options.add_argument("--silent")
    
    # Bỏ các option tắt hình ảnh và JS để dễ theo dõi
    # options.add_argument("--disable-images")
    # options.add_argument("--disable-javascript")
    
    if headless:
        options.add_argument("--headless")
    
    # Random UA
    try:
        from fake_useragent import UserAgent
        ua = UserAgent().random
    except:
        ua = random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/114.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/14.1 Safari/605.1.15",
        ])
    options.add_argument(f"user-agent={ua}")

    # Proxy auth
    parts = PROXY.split(":")
    if len(parts) == 4:
        ip, port, user, pwd = parts
        proxy_url = f"http://{user}:{pwd}@{ip}:{port}"
        sw_opts = {'proxy': {'http': proxy_url, 'https': proxy_url, 'no_proxy': 'localhost,127.0.0.1'}}
    else:
        sw_opts = {}

    drv = webdriver.Chrome(options=options, seleniumwire_options=sw_opts)
    drv.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"}
    )
    drv.maximize_window()
    return drv

def get_driver(worker_id: int) -> webdriver.Chrome:
    """Lấy hoặc tạo driver cho worker"""
    with driver_lock:
        if worker_id not in driver_pool or driver_pool[worker_id] is None:
            logger.info(f"Creating new driver for worker {worker_id}")
            # Chạy ở chế độ có giao diện
            driver_pool[worker_id] = init_browser(headless=False)
        return driver_pool[worker_id]

def get_message_elements(drv: webdriver.Chrome) -> List[Tuple[str, str, str]]:
    """Lấy tất cả message elements với ID và nội dung (lấy text sâu trong các thẻ con)"""
    try:
        elements = drv.find_elements(By.CSS_SELECTOR, "div.message-text-content")
        messages = []
        for i, e in enumerate(elements):
            try:
                # Lấy text của tất cả thẻ con (span, div, v.v.)
                child_texts = [child.get_attribute("innerText") for child in e.find_elements(By.XPATH, ".//*") if child.get_attribute("innerText")]
                text = " ".join(child_texts).strip()
                if not text:
                    # Nếu không có thẻ con, lấy text của chính element
                    text = e.get_attribute("innerText").strip()
                if text:
                    msg_id = None
                    if "[ID:" in text and "]" in text:
                        start = text.find("[ID:") + 4
                        end = text.find("]", start)
                        if start > 3 and end > start:
                            msg_id = text[start:end]
                            text = text[end + 1:].strip()
                    messages.append((str(i), msg_id, text))
            except StaleElementReferenceException:
                continue
        logger.info(f"All messages: {[m[2] for m in messages]}")
        return messages
    except Exception as e:
        logger.error(f"Error getting messages: {str(e)}")
        return []

def wait_for_new_messages(drv: webdriver.Chrome, context: RequestContext, timeout: int = TIMEOUT) -> List[str]:
    """Đợi và lấy các message mới cho request cụ thể, chỉ trả về khi message đã ổn định và không phải là prompt"""
    start_time = time.time()
    last_count = context.message_count_before
    found_response = False
    last_message_text = None
    stable_time = 0
    STABLE_DURATION = 2  # Số giây message không đổi để coi là đã xong

    logger.info(f"Waiting for response to request {context.request_id} (query_id: {context.query_id})")

    while time.time() - start_time < timeout and not found_response:
        try:
            messages = get_message_elements(drv)
            current_count = len(messages)

            if current_count > last_count:
                # Bỏ qua message là prompt (câu hỏi)
                filtered = [m for m in messages[last_count:] if context.prompt.strip() not in m[2]]
                if filtered:
                    _, msg_id, text = filtered[-1]
                else:
                    _, msg_id, text = messages[-1]

                if last_message_text == text:
                    stable_time += 0.5
                else:
                    stable_time = 0
                    last_message_text = text

                if stable_time >= STABLE_DURATION:
                    found_response = True
                    logger.info(f"Response stabilized for request {context.request_id}")
                    logger.info(f"Final message text: {repr(text)}")
                    return [text]

            time.sleep(0.5)
        except Exception as e:
            logger.error(f"Error waiting for messages: {str(e)}")
            time.sleep(0.5)

    if not found_response:
        logger.warning(f"Timeout waiting for response to request {context.request_id}")
    return []

def send_prompt_and_get_response(drv: webdriver.Chrome, context: RequestContext) -> str:
    """Gửi prompt và lấy phản hồi với tracking cho request cụ thể"""
    logger.info(f"Processing request {context.request_id} with prompt: {context.prompt[:50]}...")
    
    for attempt in range(MAX_RETRIES):
        try:
            # Kiểm tra cache trước
            cached_response = get_cached_response(context.prompt)
            if cached_response:
                logger.info(f"Using cached response for request {context.request_id}")
                return cached_response

            # Kiểm tra CAPTCHA
            if should_wait_for_captcha():
                wait_time = CAPTCHA_RETRY_DELAY - (datetime.now() - last_captcha_time).total_seconds()
                if wait_time > 0:
                    logger.info(f"Waiting {wait_time:.0f} seconds after CAPTCHA...")
                    time.sleep(wait_time)

            # Lấy số lượng message hiện tại
            messages = get_message_elements(drv)
            context.message_count_before = len(messages)
            logger.info(f"Current message count: {context.message_count_before}")

            # Kiểm tra CAPTCHA trước khi gửi prompt
            if not handle_captcha(drv):
                logger.warning(f"CAPTCHA detected for request {context.request_id}")
                return "CAPTCHA detected and not solved"

            # Tìm và gửi prompt với ID
            wait = WebDriverWait(drv, ELEMENT_WAIT_TIMEOUT)
            try:
                # Đợi element xuất hiện và có thể tương tác
                box = wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, PROMPT_SELECTOR))
                )
                wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, PROMPT_SELECTOR))
                )
                
                # Clear và gửi prompt
                box.clear()
                # Thêm ID vào prompt
                prompt_with_id = f"{context.prompt} [ID:{context.query_id}]"
                logger.info(f"Sending prompt with ID for request {context.request_id}")
                
                # Gửi từng ký tự để tránh lỗi
                for char in prompt_with_id:
                    box.send_keys(char)
                    time.sleep(0.05)  # Thêm delay nhỏ giữa các ký tự
                
                box.send_keys(Keys.ENTER)
                logger.info("Prompt sent successfully")
                
            except TimeoutException as e:
                logger.error(f"Timeout waiting for prompt input: {str(e)}")
                raise
            except Exception as e:
                logger.error(f"Error sending prompt: {str(e)}")
                raise

            # Đợi loading và phản hồi
            try:
                logger.debug("Waiting for loading indicator...")
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, LOADING_CSS)))
                wait.until(EC.invisibility_of_element_located((By.CSS_SELECTOR, LOADING_CSS)))
            except TimeoutException:
                logger.warning("Loading indicator timeout")

            # Kiểm tra CAPTCHA sau khi gửi prompt
            if not handle_captcha(drv):
                logger.warning(f"CAPTCHA detected after sending prompt for request {context.request_id}")
                return "CAPTCHA detected after sending prompt"

            # Đợi và lấy các message mới
            new_messages = wait_for_new_messages(drv, context)
            if new_messages:
                response = "\n".join(new_messages)
                logger.info(f"Got response for request {context.request_id}")
                logger.info(f"Returning response: {repr(response)}")
                cache_response(context.prompt, response)
                return response

            if attempt < MAX_RETRIES - 1:
                logger.warning(f"Retry attempt {attempt + 1} for request {context.request_id}")
                continue

            logger.error(f"Timeout waiting for response to request {context.request_id}")
            return "Timeout waiting for response"

        except WebDriverException as e:
            logger.error(f"WebDriver error on attempt {attempt + 1}: {str(e)}")
            if attempt < MAX_RETRIES - 1:
                continue
            raise

    logger.error(f"Failed after all retry attempts for request {context.request_id}")
    return "Failed after all retry attempts"

def worker_function(worker_id: int):
    """Worker function để xử lý request từ queue"""
    logger.info(f"Starting worker {worker_id}")
    driver = get_driver(worker_id)
    
    while True:
        try:
            # Lấy request từ queue với timeout
            request_id, prompt = request_queue.get(timeout=QUEUE_TIMEOUT)
            logger.info(f"Worker {worker_id} processing request {request_id}")
            context = RequestContext(request_id, prompt)
            
            try:
                # Xử lý request
                response = send_prompt_and_get_response(driver, context)
                
                # Lưu kết quả và thông báo đã hoàn thành
                if request_id in response_dict:
                    event = response_dict[request_id][1]
                    response_dict[request_id] = (response, event)
                    event.set()
                logger.info(f"Completed request {request_id}")
                
            except Exception as e:
                # Xử lý lỗi
                logger.error(f"Error processing request {request_id}: {str(e)}")
                if request_id in response_dict:
                    event = response_dict[request_id][1]
                    response_dict[request_id] = (f"Error: {str(e)}", event)
                    event.set()
            
            finally:
                request_queue.task_done()
                
        except Empty:
            # Queue timeout, kiểm tra xem có cần thoát không
            if not any(not event.is_set() for _, event in response_dict.values()):
                logger.info(f"Worker {worker_id} exiting due to queue timeout")
                break
        except Exception as e:
            logger.error(f"Worker {worker_id} error: {str(e)}")
            time.sleep(1)

def start_workers():
    """Khởi động worker threads"""
    logger.info(f"Starting {MAX_WORKERS} workers")
    for i in range(MAX_WORKERS):
        executor.submit(worker_function, i)

def generate_request_id() -> str:
    """Tạo ID duy nhất cho request"""
    return f"req_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"

@app.before_first_request
def initialize():
    """Khởi tạo driver và workers khi server start"""
    logger.info("Initializing server...")
    start_workers()

@app.route("/health", methods=["GET"])
def health():
    """Kiểm tra tình trạng server"""
    if not any(driver for driver in driver_pool.values() if driver):
        logger.error("No drivers initialized")
        return jsonify(status="error", message="Driver not initialized"), 500
    
    try:
        # Kiểm tra kết nối
        for driver in driver_pool.values():
            if driver:
                driver.current_url
        captcha_status = "captcha_present" if any(is_captcha_present(driver) for driver in driver_pool.values() if driver) else "ok"
        logger.info(f"Health check: {captcha_status}")
        return jsonify(status=captcha_status), 200
    except:
        logger.error("Health check failed")
        return jsonify(status="error", message="Driver not responding"), 500

@app.route("/query", methods=["OPTIONS", "POST"])
def query_api():
    """API endpoint để gửi prompt và nhận phản hồi"""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "").strip()
    
    if not prompt:
        logger.warning("Received empty prompt")
        return jsonify(error="Empty prompt"), 400

    # Tạo request ID và thêm vào queue
    request_id = generate_request_id()
    logger.info(f"Received new request {request_id}")
    response_dict[request_id] = (None, threading.Event())
    request_queue.put((request_id, prompt))

    # Chờ kết quả với timeout
    try:
        logger.info(f"Waiting for response event for request {request_id}")
        event = response_dict[request_id][1]
        event.wait(timeout=QUEUE_TIMEOUT)
        logger.info(f"Event set for request {request_id}")
        response, _ = response_dict[request_id]
        logger.info(f"Response for request {request_id}: {response}")
        del response_dict[request_id]
        
        if response is None:
            logger.error(f"No response for request {request_id}")
            return jsonify(error="No response"), 500
        if response.startswith("Error:"):
            logger.error(f"Request {request_id} failed: {response}")
            return jsonify(error=response[7:]), 500
        logger.info(f"Request {request_id} completed successfully")
        return jsonify(answer=response), 200
        
    except Exception as e:
        if request_id in response_dict:
            del response_dict[request_id]
        logger.error(f"Request {request_id} failed with exception: {str(e)}")
        return jsonify(error=str(e)), 500

@atexit.register
def cleanup():
    """Dọn dẹp khi server tắt"""
    logger.info("Cleaning up...")
    # Đóng tất cả drivers
    for driver in driver_pool.values():
        if driver:
            try:
                driver.quit()
            except:
                pass
    
    # Đóng thread pool
    executor.shutdown(wait=True)
    logger.info("Cleanup completed")

if __name__ == "__main__":
    logger.info("Starting server...")
    app.run(host="0.0.0.0", port=5678, debug=True)