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

logger = logging.getLogger(__name__)

# Selectors
PROMPT_SELECTOR = "textarea[placeholder='Ask a question or enter a prompt here']"
RESPONSE_SELECTOR = "div.markdown-content"
LOADING_SELECTOR = "div.loading-indicator"  # Thêm selector cho loading indicator

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

def wait_for_response(driver, timeout=30):
    """Đợi cho đến khi có response mới"""
    start_time = time.time()
    last_response = None
    
    while time.time() - start_time < timeout:
        try:
            # Đợi loading indicator biến mất
            WebDriverWait(driver, 5).until_not(
                EC.presence_of_element_located((By.CSS_SELECTOR, LOADING_SELECTOR))
            )
            
            # Lấy response hiện tại
            response_elements = driver.find_elements(By.CSS_SELECTOR, RESPONSE_SELECTOR)
            if response_elements:
                current_response = response_elements[-1].text.strip()
                if current_response != last_response:
                    # Đợi thêm 1 giây để đảm bảo response đã hoàn chỉnh
                    time.sleep(1)
                    return current_response
            last_response = current_response if response_elements else None
        except Exception as e:
            logger.debug(f"Waiting for response: {str(e)}")
        time.sleep(0.5)
    
    raise TimeoutError("Timeout waiting for response")

def send_prompt_and_get_response(driver, prompt, query_id=None):
    """
    Gửi prompt và lấy response tương ứng
    query_id: ID duy nhất cho mỗi request để tracking
    """
    try:
        logger.info(f"[{query_id}] Sending prompt: {prompt}")
        logger.info(f"[{query_id}] Current URL: {driver.current_url}")
        
        # Đợi và tìm prompt box
        box = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, PROMPT_SELECTOR))
        )
        
        # Clear và gửi prompt
        box.clear()
        box.send_keys(prompt)
        box.send_keys(Keys.ENTER)
        
        # Đánh dấu thời điểm gửi prompt
        send_time = datetime.now()
        logger.info(f"[{query_id}] Prompt sent at {send_time}")
        
        # Đợi và lấy response
        response = wait_for_response(driver)
        response_time = datetime.now()
        
        # Log thông tin timing
        time_diff = (response_time - send_time).total_seconds()
        logger.info(f"[{query_id}] Response received after {time_diff} seconds")
        
        return response
        
    except Exception as e:
        logger.error(f"[{query_id}] Error in send_prompt_and_get_response: {str(e)}")
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