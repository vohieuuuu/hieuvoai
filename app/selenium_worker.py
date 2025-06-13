# selenium_worker.py
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
from .config import PROMPT_SELECTOR, LOADING_CSS, TIMEOUT, ELEMENT_WAIT_TIMEOUT, CAPTCHA_SELECTORS, TARGET_URL
import logging

logger = logging.getLogger(__name__)

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

def send_prompt_and_get_response(drv, prompt):
    # Đảm bảo driver đang ở đúng trang public
    if not drv.current_url.startswith(TARGET_URL):
        drv.get(TARGET_URL)
        WebDriverWait(drv, ELEMENT_WAIT_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, PROMPT_SELECTOR))
        )
    logger.info(f"Processing prompt: {prompt[:50]}...")
    wait = WebDriverWait(drv, ELEMENT_WAIT_TIMEOUT)
    # Tìm và gửi prompt
    box = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, PROMPT_SELECTOR)))
    wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, PROMPT_SELECTOR)))
    box.clear()
    logger.info("Sending prompt")
    for char in prompt:
        box.send_keys(char)
        time.sleep(0.05)
    box.send_keys(Keys.ENTER)
    logger.info("Prompt sent successfully")
    # Đợi loading
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, LOADING_CSS)))
        wait.until(EC.invisibility_of_element_located((By.CSS_SELECTOR, LOADING_CSS)))
    except Exception:
        logger.warning("Loading indicator timeout")
    # Đợi message trả lời
    start_time = time.time()
    last_messages = []
    stable_time = 0
    STABLE_DURATION = 2
    while time.time() - start_time < TIMEOUT:
        messages = get_message_elements(drv)
        texts = [m[1] for m in messages]
        if texts:
            if last_messages == texts:
                stable_time += 0.5
            else:
                stable_time = 0
                last_messages = texts
            if stable_time >= STABLE_DURATION:
                logger.info("Response stabilized")
                final_text = "\n".join(texts)
                logger.info(f"Final message text: {repr(final_text)}")
                return final_text
        time.sleep(0.5)
    logger.warning("Timeout waiting for response")
    return ""

def google_login_if_needed(driver, email, password, timeout=60):
    from .config import TARGET_URL
    try:
        # Nếu đã đăng nhập rồi, vẫn phải chuyển hướng đến TARGET_URL
        if "accounts.google.com" not in driver.current_url and "ServiceLogin" not in driver.current_url:
            driver.get(TARGET_URL)
            return
        wait = WebDriverWait(driver, timeout)
        # Điền email
        try:
            email_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email']")))
            email_input.clear()
            email_input.send_keys(email)
            time.sleep(1)
            email_input.send_keys(Keys.ENTER)
            time.sleep(3)
        except Exception as e:
            logger.error(f"Failed to input email: {e}")
            driver.save_screenshot(f"login_error_{email.replace('@', '_at_')}_email.png")
            return
        # Kiểm tra xem có cần nhập password không
        try:
            password_input = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
            )
            # Add explicit wait for element to be interactable
            wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='password']")))
            
            # Scroll into view and wait a moment
            driver.execute_script("arguments[0].scrollIntoView(true);", password_input)
            time.sleep(1)
            
            # Try multiple times to interact with password field
            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    password_input.click()
                    time.sleep(0.5)
                    password_input.clear()
                    password_input.send_keys(password)
                    time.sleep(1)
                    password_input.send_keys(Keys.ENTER)
                    break
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise e
                    logger.warning(f"Attempt {attempt + 1} failed to input password, retrying...")
                    time.sleep(2)
            
            # Wait longer after submitting password
            time.sleep(5)
        except Exception as e:
            logger.error(f"Failed to input password: {e}")
            driver.save_screenshot(f"login_error_{email.replace('@', '_at_')}_pw.png")
            return
        # Kiểm tra xem đã đăng nhập thành công chưa
        try:
            wait.until(lambda d: "accounts.google.com" not in d.current_url)
            logger.info(f"Auto login for {email} successful!")
            driver.get(TARGET_URL)
            time.sleep(5)
            # Kiểm tra đã vào đúng TARGET_URL chưa
            if TARGET_URL.split("//",1)[-1].split("/",1)[0] in driver.current_url:
                logger.info("Successfully navigated to TARGET_URL")
            else:
                logger.warning("Failed to navigate to TARGET_URL")
                screenshot_path = f"notebooklm_navigation_error_{email.replace('@', '_at_')}.png"
                driver.save_screenshot(screenshot_path)
                logger.info(f"Saved navigation error screenshot to {screenshot_path}")
        except Exception as e:
            logger.error(f"Login verification failed for {email}: {e}")
            try:
                screenshot_path = f"login_error_{email.replace('@', '_at_')}.png"
                driver.save_screenshot(screenshot_path)
                logger.info(f"Saved error screenshot to {screenshot_path}")
            except:
                pass
    except Exception as e:
        logger.error(f"Auto login failed for {email}: {e}")
        try:
            screenshot_path = f"login_error_{email.replace('@', '_at_')}.png"
            driver.save_screenshot(screenshot_path)
            logger.info(f"Saved error screenshot to {screenshot_path}")
        except:
            pass 