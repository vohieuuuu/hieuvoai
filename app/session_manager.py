import os
from datetime import datetime, timedelta
from seleniumwire import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from threading import Lock
from .email_accounts import EMAIL_ACCOUNTS
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
import logging
import time

PROFILE_BASE_DIR = os.path.abspath("chrome_profiles")

# Quản lý pool driver
active_pool = {}  # conversation_id -> entry
standby_pool = []  # list các entry standby
pool_lock = Lock()

# Chuẩn hóa entry
# entry = {"email", "password", "driver", "last_active", "conversation_id", "in_use", "standby"}

logger = logging.getLogger(__name__)

def chrome_options_func(profile_dir):
    from seleniumwire import webdriver
    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--profile-directory=Default")
    # options.add_argument("--disable-gpu")  # thử bỏ
    # options.add_argument("--disable-dev-shm-usage")  # thử bỏ
    # options.add_argument("--no-sandbox")  # thử bỏ
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-logging")
    options.add_argument("--log-level=3")
    options.add_argument("--silent")
    options.add_argument("--window-size=300,300")
    # options.add_argument("--remote-debugging-port=0")  # thử bỏ
    return options

def _create_entry(email, password, chrome_options_func, login_func=None, standby=False):
    profile_dir = os.path.join(PROFILE_BASE_DIR, email)
    os.makedirs(profile_dir, exist_ok=True)
    options = chrome_options_func(profile_dir)
    options.add_argument("--remote-debugging-port=0")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-breakpad")
    options.add_argument("--disable-component-extensions-with-background-pages")
    options.add_argument("--disable-features=TranslateUI")
    options.add_argument("--disable-ipc-flooding-protection")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--enable-features=NetworkService,NetworkServiceInProcess")
    options.add_argument("--force-color-profile=srgb")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--mute-audio")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    if login_func:
        driver.get("https://accounts.google.com/")
        login_func(driver, email, password)
    return {
        "email": email,
        "password": password,
        "driver": driver,
        "last_active": datetime.now(),
        "conversation_id": None if standby else "",
        "in_use": not standby,
        "standby": standby
    }

def _init_standby_pool(chrome_options_func, login_func=None):
    global standby_pool
    standby_pool = []
    logger.info("[INIT] Initializing standby Chrome pool...")
    for acc in EMAIL_ACCOUNTS:
        try:
            entry = _create_entry(acc["email"], acc["password"], chrome_options_func, login_func, standby=True)
            standby_pool.append(entry)
            logger.info(f"[INIT] Standby Chrome created for email={acc['email']}")
            time.sleep(2)  # Thêm delay giữa các lần tạo Chrome
        except Exception as e:
            logger.error(f"[INIT][ERROR] Error creating standby driver for {acc['email']}: {str(e)}")
    logger.info(f"[INIT] Standby Chrome pool size: {len(standby_pool)}")

def get_driver_for_conversation(conversation_id, chrome_options_func, login_func=None):
    global standby_pool
    now = datetime.now()
    with pool_lock:
        logger.info(f"[POOL] Standby pool size: {len(standby_pool)}, Active pool size: {len(active_pool)}")
        # Nếu conversation_id đã có driver active
        if conversation_id in active_pool:
            entry = active_pool[conversation_id]
            entry["last_active"] = now
            logger.info(f"[ACTIVE] Reusing active Chrome for conversation_id={conversation_id}, email={entry['email']}")
            return entry["driver"], entry["email"], entry["password"]
        # Nếu chưa có, lấy standby từ pool
        if standby_pool:
            entry = standby_pool.pop(0)
            entry["conversation_id"] = conversation_id
            entry["in_use"] = True
            entry["standby"] = False
            entry["last_active"] = now
            active_pool[conversation_id] = entry
            logger.info(f"[STANDBY] Assigning standby Chrome to conversation_id={conversation_id}, email={entry['email']}. Standby left: {len(standby_pool)}")
            return entry["driver"], entry["email"], entry["password"]
        # Nếu không còn standby, tạo mới nếu còn email chưa dùng
        used_emails = {e["email"] for e in active_pool.values()}
        for acc in EMAIL_ACCOUNTS:
            if acc["email"] not in used_emails:
                try:
                    entry = _create_entry(acc["email"], acc["password"], chrome_options_func, login_func, standby=False)
                    entry["conversation_id"] = conversation_id
                    active_pool[conversation_id] = entry
                    logger.info(f"[NEW] Created new Chrome for conversation_id={conversation_id}, email={entry['email']}")
                    return entry["driver"], entry["email"], entry["password"]
                except Exception as e:
                    logger.error(f"Error creating driver for {acc['email']}: {str(e)}")
                    continue
        logger.error(f"[ERROR] No available email account for conversation_id={conversation_id}")
        raise Exception("No available email account")

def cleanup_inactive_drivers(timeout_minutes=2, standby_timeout=10, chrome_options_func=None, login_func=None):
    global standby_pool
    now = datetime.now()
    with pool_lock:
        # Cleanup active drivers
        to_remove = []
        for cid, entry in active_pool.items():
            if entry["last_active"] and (now - entry["last_active"]).total_seconds() > timeout_minutes * 60:
                try:
                    entry["driver"].quit()
                except:
                    pass
                to_remove.append(cid)
        for cid in to_remove:
            del active_pool[cid]
        # Cleanup standby pool
        new_standby_pool = []
        for entry in standby_pool:
            if entry["last_active"] and (now - entry["last_active"]).total_seconds() > standby_timeout * 60:
                try:
                    entry["driver"].quit()
                except:
                    pass
            else:
                new_standby_pool.append(entry)
        standby_pool = new_standby_pool
        # Nếu standby_pool bị rỗng, tạo lại standby cho các email chưa dùng
        used_emails = {e["email"] for e in active_pool.values()}
        standby_emails = {e["email"] for e in standby_pool}
        for acc in EMAIL_ACCOUNTS:
            if acc["email"] not in used_emails and acc["email"] not in standby_emails:
                try:
                    entry = _create_entry(acc["email"], acc["password"], chrome_options_func, login_func, standby=True)
                    standby_pool.append(entry)
                except Exception as e:
                    logger.error(f"Error creating standby driver for {acc['email']}: {str(e)}")

def send_prompt_and_get_response(driver, prompt, query_id):
    logger.info(f"Current URL: {driver.current_url}")
    logger.info(f"Prompt to send: {prompt}")
    driver.save_screenshot(f"before_send_prompt_{query_id}.png")
    box = WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, PROMPT_SELECTOR))
    )
    box.clear()
    box.send_keys(prompt)
    box.send_keys(Keys.ENTER)
    logger.info("Prompt sent successfully")
    # ... tiếp tục lấy kết quả ... 