# utils.py
import uuid
import logging
from datetime import datetime

def generate_request_id():
    return f"req_{int(datetime.now().timestamp() * 1000)}_{uuid.uuid4().hex[:8]}"

def generate_conversation_id():
    return uuid.uuid4().hex

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    return logger 

def send_prompt_and_get_response(prompt):
    logger.info(f"Prompt to send: {prompt}")
    # Implementation of send_prompt_and_get_response function
    # This function should return the response received from the API
    # For now, we'll just return a placeholder
    return "Response from API" 