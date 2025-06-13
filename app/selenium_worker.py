# selenium_worker.py
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
from .config import PROMPT_SELECTOR, LOADING_CSS, TIMEOUT, ELEMENT_WAIT_TIMEOUT, CAPTCHA_SELECTORS, TARGET_URL
import logging
import json
import re
from datetime import datetime
from seleniumwire import webdriver

logger = logging.getLogger(__name__)

# Selectors
PROMPT_SELECTOR = "textarea[placeholder='Message NotebookLM']"
RESPONSE_SELECTOR = "div[data-message-author-role='model']"
LOADING_SELECTOR = "div.loading-indicator"  # Thêm selector cho loading indicator

class ResponseTracker:
    def __init__(self):
        self.last_response = None
        self.response_count = 0
        self.last_update = datetime.now()
        self.stable_count = 0
        self.max_stable_count = 3  # Số lần response không đổi để xác định là ổn định

    def update(self, new_response):
        if new_response != self.last_response:
            self.last_response = new_response
            self.response_count += 1
            self.last_update = datetime.now()
            self.stable_count = 0
            logger.info(f"[TRACKER] New response detected. Count: {self.response_count}")
        else:
            self.stable_count += 1
            logger.info(f"[TRACKER] Response unchanged. Stable count: {self.stable_count}")

    def is_stable(self):
        return self.stable_count >= self.max_stable_count

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

def wait_for_response(driver, timeout=15, tracker=None):
    start_time = time.time()
    last_response = None
    stable_count = 0
    max_stable_count = 3

    while time.time() - start_time < timeout:
        try:
            responses = driver.find_elements(By.CSS_SELECTOR, RESPONSE_SELECTOR)
            if responses:
                current_response = responses[-1].text.strip()
                if tracker:
                    tracker.update(current_response)
                    if tracker.is_stable():
                        logger.info(f"[RESPONSE] Stable response found after {tracker.response_count} changes")
                        return tracker.get_response()
                else:
                    if current_response == last_response:
                        stable_count += 1
                        if stable_count >= max_stable_count:
                            logger.info(f"[RESPONSE] Stable response found after {stable_count} unchanged checks")
                            return current_response
                    else:
                        stable_count = 0
                        last_response = current_response
            time.sleep(0.3)  # Giảm thời gian sleep để tăng tốc độ phản hồi
        except Exception as e:
            logger.error(f"[ERROR] Error while waiting for response: {str(e)}")
            time.sleep(0.3)

    logger.warning(f"[TIMEOUT] No stable response after {timeout} seconds")
    return last_response if last_response else None

def send_prompt_and_get_response(driver, prompt):
    try:
        logger.info(f"[PROMPT] Sending prompt: {prompt[:50]}...")
        logger.info(f"[URL] Current URL: {driver.current_url}")

        # Tạo tracker cho request này
        tracker = ResponseTracker()

        # Tìm và gửi prompt
        box = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, PROMPT_SELECTOR))
        )
        box.clear()
        box.send_keys(prompt)
        box.send_keys(Keys.ENTER)
        logger.info("[PROMPT] Prompt sent successfully")

        # Đợi và lấy response
        start_time = time.time()
        response = wait_for_response(driver, timeout=15, tracker=tracker)
        end_time = time.time()

        if response:
            logger.info(f"[RESPONSE] Got response in {end_time - start_time:.2f} seconds")
            logger.info(f"[RESPONSE] Response length: {len(response)}")
            return response
        else:
            logger.warning("[RESPONSE] No response received")
            return None

    except Exception as e:
        logger.error(f"[ERROR] Error in send_prompt_and_get_response: {str(e)}")
        return None

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