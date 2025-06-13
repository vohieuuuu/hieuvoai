import os
from datetime import datetime, timedelta
import undetected_chromedriver as uc
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from threading import Lock, Timer
from .email_accounts import EMAIL_ACCOUNTS
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import WebDriverException
import logging
import time
import random

PROFILE_BASE_DIR = os.path.abspath("chrome_profiles")
HEALTH_CHECK_INTERVAL = 300  # 5 phút
MAX_RETRIES = 3
RETRY_DELAY = 1

# Cấu hình pool
BASE_ACTIVE_PROFILES = 5  # Số lượng profile active cơ bản
MAX_ACTIVE_PROFILES = 15  # Số lượng profile active tối đa
SCALE_INCREMENT = 5  # Số lượng profile tăng mỗi lần scale
MAX_STANDBY_PROFILES = 5  # Số lượng profile standby tối đa
MIN_STANDBY_PROFILES = 3  # Số lượng profile standby tối thiểu
SCALE_THRESHOLD = 0.8  # Ngưỡng để scale up (80% active profiles đang sử dụng)
SCALE_DOWN_THRESHOLD = 0.3  # Ngưỡng để scale down (30% active profiles đang sử dụng)
MAX_TOTAL_PROFILES = 100  # Tổng số profile tối đa có thể tạo
SCALE_DOWN_INTERVAL = 1800  # 30 phút kiểm tra scale down một lần
IDLE_TIMEOUT = 900  # 15 phút không hoạt động sẽ đóng tab

# Quản lý pool driver
active_pool = {}  # conversation_id -> entry
standby_pool = []  # list các entry standby
pool_lock = Lock()
current_max_active = BASE_ACTIVE_PROFILES  # Số lượng profile active hiện tại

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

def check_driver_health(driver):
    try:
        # Thử truy cập một trang web đơn giản
        driver.get("https://www.google.com")
        return True
    except WebDriverException:
        return False

def _create_entry(email, password, chrome_options_func, login_func=None, standby=True):
    profile_dir = os.path.join(PROFILE_BASE_DIR, email)
    os.makedirs(profile_dir, exist_ok=True)
    options = chrome_options_func(profile_dir)
    
    for attempt in range(MAX_RETRIES):
        try:
            driver = uc.Chrome(
                options=options,
                driver_executable_path=ChromeDriverManager().install(),
                version_main=None
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
                "last_state_change": now,
                "health_check": now,
                "failed_attempts": 0,
                "usage_count": 0,
                "created_at": now,
                "last_tab_cleanup": now
            }
        except Exception as e:
            logger.error(f"[CREATE] Attempt {attempt + 1}/{MAX_RETRIES} failed for {email}: {str(e)}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
            else:
                raise

def _init_standby_pool(chrome_options_func, login_func=None):
    global standby_pool
    standby_pool = []
    logger.info("[INIT] Initializing standby Chrome pool...")
    
    # Chọn ngẫu nhiên một số email để tạo profile
    available_emails = EMAIL_ACCOUNTS.copy()
    random.shuffle(available_emails)
    
    for acc in available_emails[:MAX_STANDBY_PROFILES]:
        try:
            entry = _create_entry(acc["email"], acc["password"], chrome_options_func, login_func, standby=True)
            standby_pool.append(entry)
            logger.info(f"[INIT] Standby Chrome created for email={acc['email']}")
            time.sleep(2)
        except Exception as e:
            logger.error(f"[INIT][ERROR] Error creating standby driver for {acc['email']}: {str(e)}")
    
    logger.info(f"[INIT] Standby Chrome pool size: {len(standby_pool)}")

def _get_available_email():
    """Lấy email chưa được sử dụng từ pool"""
    used_emails = {e["email"] for e in active_pool.values()}
    standby_emails = {e["email"] for e in standby_pool}
    available_emails = [acc for acc in EMAIL_ACCOUNTS if acc["email"] not in used_emails and acc["email"] not in standby_emails]
    return random.choice(available_emails) if available_emails else None

def _should_scale():
    """Kiểm tra xem có cần scale up không"""
    global current_max_active
    total_profiles = len(active_pool) + len(standby_pool)
    if total_profiles >= MAX_TOTAL_PROFILES:
        return False
    
    active_usage = len(active_pool) / current_max_active
    if active_usage >= SCALE_THRESHOLD and current_max_active < MAX_ACTIVE_PROFILES:
        # Tăng giới hạn active profiles
        new_max = min(current_max_active + SCALE_INCREMENT, MAX_ACTIVE_PROFILES)
        if new_max > current_max_active:
            logger.info(f"[SCALE] Increasing max active profiles from {current_max_active} to {new_max}")
            current_max_active = new_max
            return True
    return False

def _should_scale_down():
    """Kiểm tra xem có cần scale down không"""
    global current_max_active
    total_profiles = len(active_pool) + len(standby_pool)
    if total_profiles <= MIN_STANDBY_PROFILES:
        return False
    
    active_usage = len(active_pool) / current_max_active
    if active_usage <= SCALE_DOWN_THRESHOLD and current_max_active > BASE_ACTIVE_PROFILES:
        # Giảm giới hạn active profiles
        new_max = max(current_max_active - SCALE_INCREMENT, BASE_ACTIVE_PROFILES)
        if new_max < current_max_active:
            logger.info(f"[SCALE] Decreasing max active profiles from {current_max_active} to {new_max}")
            current_max_active = new_max
            return True
    return False

def _cleanup_idle_tabs(driver):
    """Đóng các tab không hoạt động"""
    try:
        # Lấy danh sách các tab
        tabs = driver.window_handles
        if len(tabs) <= 1:
            return
        
        # Giữ tab đầu tiên, đóng các tab còn lại
        main_tab = tabs[0]
        for tab in tabs[1:]:
            driver.switch_to.window(tab)
            driver.close()
        
        # Chuyển về tab chính
        driver.switch_to.window(main_tab)
    except Exception as e:
        logger.error(f"[CLEANUP] Error cleaning up tabs: {str(e)}")

def _maintain_pool_size():
    """Duy trì kích thước pool và tự động scale"""
    global current_max_active
    with pool_lock:
        total_profiles = len(active_pool) + len(standby_pool)
        
        # Kiểm tra và điều chỉnh số lượng standby profiles
        if len(standby_pool) < MIN_STANDBY_PROFILES:
            needed = MIN_STANDBY_PROFILES - len(standby_pool)
            for _ in range(needed):
                if total_profiles >= MAX_TOTAL_PROFILES:
                    break
                acc = _get_available_email()
                if acc:
                    try:
                        entry = _create_entry(acc["email"], acc["password"], chrome_options_func, None, standby=True)
                        standby_pool.append(entry)
                        total_profiles += 1
                        logger.info(f"[POOL] Added new standby profile: {acc['email']}")
                    except Exception as e:
                        logger.error(f"[POOL] Failed to create standby profile: {str(e)}")
        
        # Kiểm tra và scale up nếu cần
        if _should_scale():
            scale_amount = min(SCALE_INCREMENT, MAX_TOTAL_PROFILES - total_profiles)
            for _ in range(scale_amount):
                acc = _get_available_email()
                if acc:
                    try:
                        entry = _create_entry(acc["email"], acc["password"], chrome_options_func, None, standby=True)
                        standby_pool.append(entry)
                        total_profiles += 1
                        logger.info(f"[SCALE] Scaled up with new profile: {acc['email']}")
                    except Exception as e:
                        logger.error(f"[SCALE] Failed to create scaled profile: {str(e)}")
        
        # Kiểm tra và scale down nếu cần
        if _should_scale_down():
            # Sắp xếp standby pool theo thời gian tạo
            standby_pool.sort(key=lambda x: x["created_at"])
            # Giữ lại MIN_STANDBY_PROFILES profile mới nhất
            while len(standby_pool) > MIN_STANDBY_PROFILES:
                entry = standby_pool.pop(0)
                try:
                    entry["driver"].quit()
                    logger.info(f"[SCALE] Scaled down and removed profile: {entry['email']}")
                except Exception as e:
                    logger.error(f"[SCALE] Error removing profile: {str(e)}")

def scale_down_task():
    """Task định kỳ kiểm tra và scale down"""
    while True:
        try:
            with pool_lock:
                if _should_scale_down():
                    logger.info("[SCALE] Starting scale down process")
                    _maintain_pool_size()
        except Exception as e:
            logger.error(f"[SCALE] Error in scale down task: {str(e)}")
        time.sleep(SCALE_DOWN_INTERVAL)

def cleanup_idle_tabs_task():
    """Task định kỳ dọn dẹp các tab không hoạt động"""
    while True:
        try:
            now = datetime.now()
            with pool_lock:
                # Kiểm tra active drivers
                for entry in active_pool.values():
                    if (now - entry["last_tab_cleanup"]).total_seconds() >= IDLE_TIMEOUT:
                        try:
                            _cleanup_idle_tabs(entry["driver"])
                            entry["last_tab_cleanup"] = now
                            logger.info(f"[CLEANUP] Cleaned up idle tabs for {entry['email']}")
                        except Exception as e:
                            logger.error(f"[CLEANUP] Error cleaning up tabs for {entry['email']}: {str(e)}")
                
                # Kiểm tra standby drivers
                for entry in standby_pool:
                    if (now - entry["last_tab_cleanup"]).total_seconds() >= IDLE_TIMEOUT:
                        try:
                            _cleanup_idle_tabs(entry["driver"])
                            entry["last_tab_cleanup"] = now
                            logger.info(f"[CLEANUP] Cleaned up idle tabs for standby {entry['email']}")
                        except Exception as e:
                            logger.error(f"[CLEANUP] Error cleaning up tabs for standby {entry['email']}: {str(e)}")
        except Exception as e:
            logger.error(f"[CLEANUP] Error in cleanup task: {str(e)}")
        time.sleep(IDLE_TIMEOUT)

def get_driver_for_conversation(conversation_id, chrome_options_func, login_func=None):
    global standby_pool, current_max_active
    now = datetime.now()
    with pool_lock:
        logger.info(f"[POOL] Standby pool size: {len(standby_pool)}, Active pool size: {len(active_pool)}, Current max active: {current_max_active}")
        
        # Kiểm tra giới hạn active profiles
        if len(active_pool) >= current_max_active:
            logger.warning(f"[POOL] Maximum active profiles reached ({current_max_active})")
            return None, None, None

        if conversation_id in active_pool:
            entry = active_pool[conversation_id]
            if check_driver_health(entry["driver"]):
                entry["last_active"] = now
                entry["failed_attempts"] = 0
                entry["usage_count"] += 1
                logger.info(f"[ACTIVE] Reusing active Chrome for conversation_id={conversation_id}, email={entry['email']}")
                return entry["driver"], entry["email"], entry["password"]
            else:
                logger.warning(f"[HEALTH] Unhealthy driver detected for {entry['email']}, creating new one")
                try:
                    entry["driver"].quit()
                except:
                    pass
                del active_pool[conversation_id]
        
        if standby_pool:
            entry = standby_pool.pop(0)
            if check_driver_health(entry["driver"]):
                entry["conversation_id"] = conversation_id
                entry["in_use"] = True
                entry["standby"] = False
                entry["last_active"] = now
                entry["has_been_active"] = True
                entry["last_state_change"] = now
                entry["failed_attempts"] = 0
                entry["usage_count"] += 1
                active_pool[conversation_id] = entry
                logger.info(f"[STANDBY] Assigning standby Chrome to conversation_id={conversation_id}, email={entry['email']}. Standby left: {len(standby_pool)}")
                
                # Dọn dẹp tab và duy trì pool
                _cleanup_idle_tabs(entry["driver"])
                entry["last_tab_cleanup"] = now
                _maintain_pool_size()
                
                return entry["driver"], entry["email"], entry["password"]
            else:
                logger.warning(f"[HEALTH] Unhealthy standby driver detected for {entry['email']}")
                try:
                    entry["driver"].quit()
                except:
                    pass
        
        # Tạo profile mới nếu còn slot
        acc = _get_available_email()
        if acc:
            try:
                entry = _create_entry(acc["email"], acc["password"], chrome_options_func, login_func, standby=False)
                entry["conversation_id"] = conversation_id
                entry["has_been_active"] = True
                entry["failed_attempts"] = 0
                entry["usage_count"] = 1
                active_pool[conversation_id] = entry
                logger.info(f"[NEW] Created new Chrome for conversation_id={conversation_id}, email={entry['email']}")
                return entry["driver"], entry["email"], entry["password"]
            except Exception as e:
                logger.error(f"Error creating driver for {acc['email']}: {str(e)}")
        
        logger.error(f"[ERROR] No available email account for conversation_id={conversation_id}")
        return None, None, None

def health_check_task():
    while True:
        try:
            now = datetime.now()
            with pool_lock:
                # Kiểm tra active drivers
                for cid, entry in list(active_pool.items()):
                    if not check_driver_health(entry["driver"]):
                        entry["failed_attempts"] += 1
                        if entry["failed_attempts"] >= MAX_RETRIES:
                            logger.error(f"[HEALTH] Driver for {entry['email']} failed health check {MAX_RETRIES} times")
                            try:
                                entry["driver"].quit()
                            except:
                                pass
                            del active_pool[cid]
                            # Tạo driver mới cho standby nếu còn slot
                            if len(standby_pool) < MAX_STANDBY_PROFILES:
                                try:
                                    new_entry = _create_entry(entry["email"], entry["password"], chrome_options_func, None, standby=True)
                                    standby_pool.append(new_entry)
                                except Exception as e:
                                    logger.error(f"[HEALTH] Failed to create new standby driver: {str(e)}")
                    else:
                        entry["failed_attempts"] = 0
                        entry["health_check"] = now

                # Kiểm tra standby drivers
                for entry in list(standby_pool):
                    if not check_driver_health(entry["driver"]):
                        entry["failed_attempts"] += 1
                        if entry["failed_attempts"] >= MAX_RETRIES:
                            logger.error(f"[HEALTH] Standby driver for {entry['email']} failed health check {MAX_RETRIES} times")
                            try:
                                entry["driver"].quit()
                            except:
                                pass
                            standby_pool.remove(entry)
                            # Tạo driver mới nếu còn slot
                            if len(standby_pool) < MAX_STANDBY_PROFILES:
                                try:
                                    new_entry = _create_entry(entry["email"], entry["password"], chrome_options_func, None, standby=True)
                                    standby_pool.append(new_entry)
                                except Exception as e:
                                    logger.error(f"[HEALTH] Failed to create new standby driver: {str(e)}")
                    else:
                        entry["failed_attempts"] = 0
                        entry["health_check"] = now

                # Duy trì kích thước pool và scale nếu cần
                _maintain_pool_size()

        except Exception as e:
            logger.error(f"[HEALTH] Error in health check task: {str(e)}")
        
        time.sleep(HEALTH_CHECK_INTERVAL)

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
                if entry["has_been_active"] and len(standby_pool) < MAX_STANDBY_PROFILES:
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
        
        # Cleanup standby pool
        new_standby_pool = []
        for entry in standby_pool:
            if entry["has_been_active"] and (now - entry["last_state_change"]).total_seconds() > timeout_minutes * 60:
                try:
                    entry["driver"].quit()
                    if len(new_standby_pool) < MAX_STANDBY_PROFILES:
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
        
        # Duy trì kích thước pool và scale nếu cần
        _maintain_pool_size() 