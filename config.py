import os
from pathlib import Path
from dotenv import load_dotenv

# Project Root
BASE_DIR = Path(__file__).parent

# Load environment variables from .env file
# Priority: .env > environment (if user still uses old path)
ENV_PATH = BASE_DIR / ".env"
OLD_ENV_PATH = BASE_DIR / "gitignore" / "environment.env"

if ENV_PATH.exists():
    load_dotenv(ENV_PATH, override=True)
elif OLD_ENV_PATH.exists():
    load_dotenv(OLD_ENV_PATH, override=True)
else:
    load_dotenv(override=True)  # Fallback to default search

COOKIE_CACHE_FILE = BASE_DIR / "cookies.json"

API_CONFIG = {
    "primary": {
        "key": os.getenv("API_KEY_PRIMARY") or os.getenv("KEY_DEFAULT_1"),
        "url": os.getenv("API_URL_PRIMARY") or os.getenv("AI_URL_1"),
        "model": os.getenv("API_MODEL_PRIMARY") or "qwen3.5-flash",
    },
    "secondary": {
        "key": os.getenv("API_KEY_SECONDARY") or os.getenv("KEY_DEFAULT_2"),
        "url": os.getenv("API_URL_SECONDARY") or os.getenv("AI_URL_2"),
        "model": os.getenv("API_MODEL_SECONDARY") or "qwen-plus-character",
    },
    "deepseek": {
        "key": os.getenv("DEEPSEEK_API_KEY") or os.getenv("KEY_DEFAULT"),
        "url": os.getenv("DEEPSEEK_API_URL") or os.getenv("AI_URL"),
        "model_chat": "deepseek-chat",
        "model_reasoner": "deepseek-reasoner",
    }
}

BOT_CONFIG = {
    "username": os.getenv("BOT_USERNAME"),
    "access_token": os.getenv("BOT_ACCESS_TOKEN"),
    "room": os.getenv("ROOM_NAME"),
}

SERVER_CONFIG = {
    "http_base": os.getenv("HTTP_BASE") or "https://room.caffeine.ink",
    "login_url": os.getenv("LOGIN_URL") or "https://room.caffeine.ink/api/login",
    "ws_base": "wss://room.caffeine.ink/websocket",
}

CONNECTION_CONFIG = {
    "initial_retry_delay": 1,
    "max_retry_delay": 30,
    "heartbeat_interval": 30,
    "message_buffer_max": 50,
    "memory_interval": 50,
    "reply_cooldown": 15,
}

LOG_CONFIG = {
    "level": os.getenv("LOG_LEVEL", "INFO"),
    "file": BASE_DIR / "endra.log",
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
}
