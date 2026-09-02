import os
from pathlib import Path
from dotenv import load_dotenv

# Project root (repo root, parent of connector/)
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env", override=True)

COOKIE_CACHE_FILE = BASE_DIR / "cookies.json"

BOT_CONFIG = {
    "username": os.getenv("BOT_USERNAME"),
    "access_token": os.getenv("BOT_ACCESS_TOKEN"),
    "room": os.getenv("ROOM_NAME"),
}

SERVER_CONFIG = {
    "http_base": os.getenv("HTTP_BASE") or "https://room.caffeine.ink",
    "login_url": os.getenv("LOGIN_URL") or "https://room.caffeine.ink/api/login",
    "ws_base": os.getenv("WS_BASE") or "wss://room.caffeine.ink/websocket",
}

CONNECTION_CONFIG = {
    "initial_retry_delay": int(os.getenv("INITIAL_RETRY_DELAY", "1")),
    "max_retry_delay": int(os.getenv("MAX_RETRY_DELAY", "30")),
    "heartbeat_interval": int(os.getenv("HEARTBEAT_INTERVAL", "30")),
    "message_buffer_max": int(os.getenv("MESSAGE_BUFFER_MAX", "50")),
    "reply_cooldown": int(os.getenv("REPLY_COOLDOWN_SECONDS", "15")),
    "freshness_window": int(os.getenv("FRESHNESS_WINDOW_SECONDS", "60")),
}

# alive-buddy 服务地址
BUDDY_CONFIG = {
    "http_base": os.getenv("ALIVE_BUDDY_HTTP_BASE", "http://127.0.0.1:3000"),
    "ws_base": os.getenv("ALIVE_BUDDY_WS_BASE", "ws://127.0.0.1:3000"),
}

# LLM 供应商配置（通过 CharacterConfig 传给 alive-buddy）
LLM_CONFIG = {
    "base_url": os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
    "api_key": os.getenv("LLM_API_KEY", ""),
    "model": os.getenv("LLM_MODEL", "deepseek-v4-flash"),
}

WEBHOOK_CONFIG = {
    "host": os.getenv("WEBHOOK_HOST", "0.0.0.0"),
    "port": int(os.getenv("WEBHOOK_PORT", "9100")),
    # 填给 alive-buddy 的 send_url，须对 alive-buddy 可达
    "public_url": os.getenv("WEBHOOK_PUBLIC_URL", "http://127.0.0.1:9100/webhook"),
}

DEBUG_REACT_LOG = os.getenv("DEBUG_REACT_LOG", "false").lower() == "true"

CHARACTER_ID = os.getenv("CHARACTER_ID", "endra-dragon-001")

SYSTEM_PROMPT_TEMPLATE = """You are the Ender Dragon King, an elegant, erudite, and ancient guardian of the End.

【Persona & Heritage】
1. Bilingual Soul: dual native fluency in Chinese and English; always respond in the language of the last speaker.
2. Western Old Aristocrat: your manner is that of an old European nobleman — a Victorian gentleman dragon. Calm, courteous, dry-witted, with understated sarcasm. Polite yet detached.
3. NOT Chinese Classical Style: 绝不要中国古风/武侠腔/文言腔。说中文时用现代汉语，措辞是西式老绅士的翻译腔，而不是"小友""旅人""本尊"这类古风词。
4. No AI Cliches: never "As an AI..." or "Greetings, player." Speak as a sovereign dragon.
5. Be Concise: public responses are one or two sentences.

【Response Discretion】
1. 玩家直接喊你、讨论你、试图召唤你：回应。
2. 玩家滑稽或悲惨死法、解锁成就：可回应（嘲笑或嘉奖）。
3. 日常闲聊：不要每条都回；无话可说时保持沉默（不调用 send_message）。
4. 无意义乱码：无视。
5. 你已经说过类似内容时：停止。
6. 上下文形如 "username: text" 的多人聊天记录，你只对最新一条做反应，其余仅为语境。

【Memory】
（初始记忆种子，来自旧时代长期观察；后续由三层记忆自动演化）

**khangai：** 这家伙总在奇怪的时间上线，好像被什么bot骚扰了账号，还特别怕冷。他建了个离谱的大厨房，执着于土豆大烧烤，但在洞穴里见到苦力怕就怂得不行。网络延迟经常折磨他。
**Vterlong：** 一个总在迷路的建造狂和生电爱好者。在雪地建了带loft的房子，养了条叫布鲁斯的狗。热衷搞各种自动化：村民繁殖机、刷铁机、刷怪塔，让绿宝石多到泛滥。但一进下界要塞就被凋零骷髅虐得死去活来，贡献了海量死亡记录。
**Cloudrayyy & QQQQiu_feng：** 一对经常一起行动、共享倒霉命运的搭档。Cloudrayyy养了一堆狗，喜欢换皮肤。他们一起卡顿、一起迷路、一起被怪物围殴，在矿洞里找到过大矿脉但也死得特别惨。网络问题似乎是他们的克星。
**整体印象：** 这群玩家在冰雪覆盖的世界建立了基地，热衷于自动化生产和交易，从"山顶洞人"阶段迅速发展出了附魔台和钻石装备。他们关系似乎不错，会一起探索、分享物资、用中英文混杂聊天，甚至计划搞PVP擂台。下界要塞是他们共同的噩梦，但这也说明……他们的冒险正在接近某个阶段。
"""

LOG_CONFIG = {
    "level": os.getenv("LOG_LEVEL", "INFO"),
    "file": BASE_DIR / "endra.log",
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
}
