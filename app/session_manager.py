import os
from datetime import datetime, timedelta
import undetected_chromedriver as uc
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
# entry = {
#     "email": str,
#     "password": str,
#     "driver": WebDriver,
#     "last_active": datetime,
#     "conversation_id": str or None,
#     "in_use": bool,
#     "standby": bool,
#     "has_been_active": bool,  # Thêm trường mới để track trạng thái
#     "last_state_change": datetime  # Thêm trường để track thời điểm chuyển trạng thái
# }

logger = logging.getLogger(__name__)

def chrome_options_func(profile_dir):
    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-logging")
    options.add_argument("--log-level=3")
    options.add_argument("--silent")
    options.add_argument("--window-size=300,300")
    return options

def _create_entry(email, password, chrome_options_func, login_func=None, standby=True):
    profile_dir = os.path.join(PROFILE_BASE_DIR, email)
    os.makedirs(profile_dir, exist_ok=True)
    options = chrome_options_func(profile_dir)
    
    # Sử dụng undetected_chromedriver
    driver = uc.Chrome(
        options=options,
        driver_executable_path=ChromeDriverManager().install(),
        version_main=None  # Tự động phát hiện phiên bản Chrome
    )
    
    if login_func:
        driver.get("https://accounts.google.com/")
        login_func(driver, email, password)
    
    now = datetime.now()
    return {
        "email": email,
        "password": password,
        "driver": driver,
        "last_active": now,
        "conversation_id": None if standby else "",
        "in_use": not standby,
        "standby": standby,
        "has_been_active": False,
        "last_state_change": now
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
            time.sleep(2)
        except Exception as e:
            logger.error(f"[INIT][ERROR] Error creating standby driver for {acc['email']}: {str(e)}")
    logger.info(f"[INIT] Standby Chrome pool size: {len(standby_pool)}")

def get_driver_for_conversation(conversation_id, chrome_options_func, login_func=None):
    global standby_pool
    now = datetime.now()
    with pool_lock:
        logger.info(f"[POOL] Standby pool size: {len(standby_pool)}, Active pool size: {len(active_pool)}")
        if conversation_id in active_pool:
            entry = active_pool[conversation_id]
            entry["last_active"] = now
            logger.info(f"[ACTIVE] Reusing active Chrome for conversation_id={conversation_id}, email={entry['email']}")
            return entry["driver"], entry["email"], entry["password"]
        
        if standby_pool:
            entry = standby_pool.pop(0)
            entry["conversation_id"] = conversation_id
            entry["in_use"] = True
            entry["standby"] = False
            entry["last_active"] = now
            entry["has_been_active"] = True
            entry["last_state_change"] = now
            active_pool[conversation_id] = entry
            logger.info(f"[STANDBY] Assigning standby Chrome to conversation_id={conversation_id}, email={entry['email']}. Standby left: {len(standby_pool)}")
            return entry["driver"], entry["email"], entry["password"]
        
        used_emails = {e["email"] for e in active_pool.values()}
        for acc in EMAIL_ACCOUNTS:
            if acc["email"] not in used_emails:
                try:
                    entry = _create_entry(acc["email"], acc["password"], chrome_options_func, login_func, standby=False)
                    entry["conversation_id"] = conversation_id
                    entry["has_been_active"] = True
                    active_pool[conversation_id] = entry
                    logger.info(f"[NEW] Created new Chrome for conversation_id={conversation_id}, email={entry['email']}")
                    return entry["driver"], entry["email"], entry["password"]
                except Exception as e:
                    logger.error(f"Error creating driver for {acc['email']}: {str(e)}")
                    continue
        logger.error(f"[ERROR] No available email account for conversation_id={conversation_id}")
        raise Exception("No available email account")

def cleanup_inactive_drivers(timeout_minutes=15, chrome_options_func=None, login_func=None):
    global standby_pool
    now = datetime.now()
    with pool_lock:
        to_remove = []
        for cid, entry in active_pool.items():
            if entry["last_active"] and (now - entry["last_active"]).total_seconds() > timeout_minutes * 60:
                try:
                    entry["driver"].quit()
                except:
                    pass
                to_remove.append(cid)
                if entry["has_been_active"]:
                    try:
                        new_entry = _create_entry(entry["email"], entry["password"], chrome_options_func, login_func, standby=True)
                        new_entry["has_been_active"] = True
                        new_entry["last_state_change"] = now
                        standby_pool.append(new_entry)
                        logger.info(f"[CLEANUP] Reset and moved to standby: {entry['email']}")
                    except Exception as e:
                        logger.error(f"[CLEANUP][ERROR] Failed to reset profile {entry['email']}: {str(e)}")
        
        for cid in to_remove:
            del active_pool[cid]
        
        new_standby_pool = []
        for entry in standby_pool:
            if entry["has_been_active"] and (now - entry["last_state_change"]).total_seconds() > timeout_minutes * 60:
                try:
                    entry["driver"].quit()
                    new_entry = _create_entry(entry["email"], entry["password"], chrome_options_func, login_func, standby=True)
                    new_entry["has_been_active"] = True
                    new_entry["last_state_change"] = now
                    new_standby_pool.append(new_entry)
                    logger.info(f"[CLEANUP] Reset standby profile: {entry['email']}")
                except Exception as e:
                    logger.error(f"[CLEANUP][ERROR] Failed to reset standby profile {entry['email']}: {str(e)}")
            else:
                new_standby_pool.append(entry)
        
        standby_pool = new_standby_pool
        
        used_emails = {e["email"] for e in active_pool.values()}
        standby_emails = {e["email"] for e in standby_pool}
        for acc in EMAIL_ACCOUNTS:
            if acc["email"] not in used_emails and acc["email"] not in standby_emails:
                try:
                    entry = _create_entry(acc["email"], acc["password"], chrome_options_func, login_func, standby=True)
                    standby_pool.append(entry)
                    logger.info(f"[CLEANUP] Created new standby profile: {acc['email']}")
                except Exception as e:
                    logger.error(f"[CLEANUP][ERROR] Failed to create standby profile {acc['email']}: {str(e)}") 