# selenium_worker.py
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException, WebDriverException
from .config import PROMPT_SELECTOR, LOADING_CSS, TIMEOUT, ELEMENT_WAIT_TIMEOUT, CAPTCHA_SELECTORS, TARGET_URL
import logging
import json
import re
from datetime import datetime
from seleniumwire import webdriver
from functools import wraps
import random

logger = logging.getLogger(__name__)

# Selectors
PROMPT_SELECTOR = "textarea[placeholder='Message NotebookLM']"
RESPONSE_SELECTOR = "div[data-message-author-role='model']"
LOADING_SELECTOR = "div.loading-indicator"  # Thêm selector cho loading indicator
MAX_RETRIES = 3
RETRY_DELAY = 1

def retry_on_failure(max_retries=MAX_RETRIES, delay=RETRY_DELAY):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    logger.warning(f"[RETRY] Attempt {attempt + 1}/{max_retries} failed: {str(e)}")
                    if attempt < max_retries - 1:
                        time.sleep(delay)
            logger.error(f"[ERROR] All retry attempts failed: {str(last_exception)}")
            raise last_exception
        return wrapper
    return decorator

class ResponseTracker:
    def __init__(self):
        self.last_response = None
        self.response_count = 0
        self.last_update = datetime.now()
        self.stable_count = 0
        self.max_stable_count = 3  # Số lần response giống nhau để xác định ổn định
        self.timeout = 30  # Timeout 30 giây
        self.check_interval = 0.3  # Kiểm tra mỗi 0.3 giây

    def update(self, new_response):
        now = datetime.now()
        if new_response != self.last_response:
            self.last_response = new_response
            self.response_count += 1
            self.last_update = now
            self.stable_count = 0
            return True
        else:
            self.stable_count += 1
            return False

    def is_stable(self):
        return self.stable_count >= self.max_stable_count

    def is_timeout(self):
        return (datetime.now() - self.last_update).total_seconds() > self.timeout

    def get_response(self):
        return self.last_response

# Hàm kiểm tra captcha
def is_captcha_present(drv):
    try:
        for selector in CAPTCHA_SELECTORS:
            elements = drv.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                return True
        return False
    except:
        return False

def handle_captcha(drv):
    if is_captcha_present(drv):
        logger.info("CAPTCHA detected! Waiting for manual intervention...")
        try:
            WebDriverWait(drv, 300).until_not(lambda d: is_captcha_present(d))
            logger.info("CAPTCHA solved!")
            return True
        except TimeoutException:
            logger.error("Timeout waiting for CAPTCHA solution")
            return False
    return True

def get_message_elements(drv):
    try:
        elements = drv.find_elements(By.CSS_SELECTOR, "mat-card-content.to-user-message-inner-content .paragraph.normal.ng-star-inserted")
        messages = []
        for i, e in enumerate(elements):
            try:
                text = e.text.strip()
                if text:
                    messages.append((str(i), text))
            except StaleElementReferenceException:
                continue
        logger.info(f"All answer messages: {[m[1] for m in messages]}")
        return messages
    except Exception as e:
        logger.error(f"Error getting answer messages: {str(e)}")
        return []

@retry_on_failure()
def wait_for_response(driver, tracker=None):
    if tracker is None:
        tracker = ResponseTracker()
    
    while not tracker.is_timeout():
        try:
            # Tìm tất cả các response
            responses = driver.find_elements(By.CSS_SELECTOR, "div[data-message-author-role='model']")
            if not responses:
                time.sleep(tracker.check_interval)
                continue
            
            # Lấy response mới nhất
            latest_response = responses[-1].text.strip()
            
            # Cập nhật tracker
            if tracker.update(latest_response):
                logger.info(f"[RESPONSE] New response detected (count: {tracker.response_count})")
            
            # Kiểm tra nếu response đã ổn định
            if tracker.is_stable():
                logger.info(f"[RESPONSE] Response stabilized after {tracker.response_count} changes")
                return latest_response
            
            time.sleep(tracker.check_interval)
            
        except WebDriverException as e:
            logger.error(f"[RESPONSE] Error waiting for response: {str(e)}")
            if tracker.is_timeout():
                break
            time.sleep(tracker.check_interval)
    
    if tracker.last_response:
        logger.warning("[RESPONSE] Timeout reached, returning last known response")
        return tracker.last_response
    
    raise TimeoutException("No response received within timeout period")

@retry_on_failure()
def send_prompt_and_get_response(driver, question):
    try:
        # Tạo tracker mới cho mỗi request
        tracker = ResponseTracker()
        
        # Tìm prompt box
        prompt_box = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div[contenteditable='true']"))
        )
        
        # Xóa nội dung cũ
        prompt_box.clear()
        
        # Gửi câu hỏi
        prompt_box.send_keys(question)
        time.sleep(0.5)  # Đợi một chút để đảm bảo câu hỏi được gửi
        prompt_box.send_keys(Keys.RETURN)
        
        # Đợi và lấy câu trả lời
        start_time = time.time()
        response = wait_for_response(driver, tracker)
        processing_time = time.time() - start_time
        
        logger.info(f"[RESPONSE] Got response in {processing_time:.2f}s after {tracker.response_count} changes")
        return response
        
    except Exception as e:
        logger.error(f"[ERROR] Failed to get response: {str(e)}")
        raise

def google_login_if_needed(driver, email, password):
    """Login vào Google nếu cần"""
    try:
        # Kiểm tra xem đã login chưa
        if "accounts.google.com" in driver.current_url:
            logger.info(f"Logging in with email: {email}")
            
            # Đợi và điền email
            email_input = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email']"))
            )
            email_input.clear()
            email_input.send_keys(email)
            email_input.send_keys(Keys.ENTER)
            
            # Đợi và điền password
            password_input = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
            )
            password_input.clear()
            password_input.send_keys(password)
            password_input.send_keys(Keys.ENTER)
            
            # Đợi cho đến khi login thành công
            WebDriverWait(driver, 30).until(
                lambda d: "accounts.google.com" not in d.current_url
            )
            logger.info("Login successful")
            
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        # Lưu screenshot khi có lỗi
        driver.save_screenshot(f"login_error_{email.replace('@', '_at_')}.png")
        raise 