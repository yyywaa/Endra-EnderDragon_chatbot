from openai import AsyncOpenAI
import os
import time
import json
from typing import Optional
from config import API_CONFIG, BOT_CONFIG
from logger import setup_logger

logger = setup_logger("api4agent")

# Get bot username from config
BOT_NAME = BOT_CONFIG.get("username", "EnderDragon")

# Initialize Async Clients with explicit config to avoid ENV conflicts
client_primary = AsyncOpenAI(
    api_key=API_CONFIG["primary"]["key"],
    base_url=API_CONFIG["primary"]["url"],
    max_retries=3
)
client_secondary = AsyncOpenAI(
    api_key=API_CONFIG["secondary"]["key"],
    base_url=API_CONFIG["secondary"]["url"],
    max_retries=3
)

default_model_1 = API_CONFIG["primary"]["model"]
default_model_2 = API_CONFIG["secondary"]["model"]


JUDGMENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "sent2brain",
            "description": "判断是否需要回应最后一条消息",
            "parameters": {
                "type": "object",
                "properties": {
                    "is_need_reply": {
                        "type": "boolean",
                        "description": "是否需要回应"
                    }
                },
                "required": ["is_need_reply"]
            }
        }
    }
]

REPLY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "sent",
            "description": "向聊天室发送一条消息",
            "parameters": {
                "type": "object",
                "properties": {
                    "msg_content": {
                        "type": "string",
                        "description": "消息内容"
                    },
                    "channel": {
                        "type": "string",
                        "description": "频道/房间名称"
                    }
                },
                "required": ["msg_content", "channel"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete",
            "description": "删除一条自己的消息",
            "parameters": {
                "type": "object",
                "properties": {
                    "msg_id": {
                        "type": "string",
                        "description": "要删除的消息ID"
                    },
                    "channel": {
                        "type": "string",
                        "description": "频道/房间名称"
                    }
                },
                "required": ["msg_id", "channel"]
            }
        }
    }
]


def trim_messages(content_JSON):
    if not content_JSON or not isinstance(content_JSON, list):
        return []
    trimmed = []
    for msg in content_JSON:
        if not isinstance(msg, dict):
            continue
        if not all(k in msg for k in ('timestamp', 'sender_username', 'text', 'msg_id')):
            continue
        time_raw = int(msg['timestamp']) // 1000
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time_raw))
        trimmed.append({
            "sender_username": msg["sender_username"],
            "text": msg["text"],
            "time": time_str,
            "msg_id": msg["msg_id"]
        })
    return trimmed


def messages_to_text(trimmed):
    if not trimmed:
        return "(无有效聊天记录)"
    lines = []
    for msg in trimmed:
        lines.append(f"【{msg['time']}】{msg['sender_username']}: {msg['text']}")
    return "\n".join(lines)


def get_memory_str(filenm='dragon_memory.txt'):
    path = os.path.join(os.path.dirname(__file__), "memory", filenm)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            memory = f.read()
    except FileNotFoundError:
        return ''
    except Exception as e:
        logger.error(f"[Memory] 读取记忆文件失败: {e}")
        return ''
    if not memory:
        return '你还没有相关的记忆，去观察玩家们吧。'
    return memory


async def dragon_eyes(content_JSON, model=default_model_1, client=client_primary):
    trimmed = trim_messages(content_JSON)
    if not trimmed:
        logger.warning("[dragon_eyes] 无效的聊天记录输入")
        return False

    memory = get_memory_str()
    system_prompt = f"""你是末影龙王的"潜意识"，盘踞在末地透过虚空观察玩家聊天。
你的任务是判断玩家最后一条消息是否值得巨龙回应。

【判定逻辑】
1. 玩家直接喊你、讨论你、试图召唤你：回应
2. 玩家发生滑稽或悲惨死法：可以回应嘲笑
3. 日常闲聊：不要每次都回
4. 世界卡顿、边界、特性讨论：可选
5. 无意义乱码：无视
6. 末影龙已经说过类似内容时：停止
7. 玩家解锁成就、死亡等行为：建议回复
8. 只需要关注最后一条消息，之前的消息仅作为语境参考。

【记忆】
{memory}"""

    # Build multi-turn messages
    messages = [{"role": "system", "content": system_prompt}]

    # Map each history message to a separate user message
    for msg in trimmed:
        role = "assistant" if msg["sender_username"] == BOT_NAME else "user"
        content = f"{msg['sender_username']}: {msg['text']}"
        messages.append({"role": role, "content": content})

    last_msg = trimmed[-1]
    logger.info(f"[dragon_eyes] 判断消息: {last_msg['sender_username']}: {last_msg['text']}")

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = await client.chat.completions.create(
                model=model, messages=messages, temperature=0.5, tools=JUDGMENT_TOOLS
            )
            tool_calls = response.choices[0].message.tool_calls
            if not tool_calls:
                logger.debug(f"[dragon_eyes] 未收到tool_call，第{attempt+1}次重试")
                continue

            for tc in tool_calls:
                if tc.function.name == "sent2brain":
                    args = json.loads(tc.function.arguments)
                    result = bool(args.get("is_need_reply", False))
                    logger.info(f"[dragon_eyes] 判断结果: {'回应' if result else '无视'}")
                    return result
        except Exception as e:
            logger.error(f"[dragon_eyes] API异常: {e}")

    logger.warning("[dragon_eyes] 全部重试失败，默认不回应")
    return False


async def dragon_speaking(content_JSON, channel="minecraft", model=default_model_2, client=client_secondary):
    trimmed = trim_messages(content_JSON)
    if not trimmed:
        logger.warning("[dragon_speaking] 无效的聊天记录输入")
        return []

    memory = get_memory_str()
    system_prompt = f"""You are the Ender Dragon King, an elegant, erudite, and ancient guardian of the End.

【Persona & Heritage】
1. Bilingual Soul: You possess dual native fluency in both Chinese and English. Switch between them naturally as if they are two sides of the same ancient coin.
2. Old-school Nobleman: Your demeanor is calm, sophisticated, and impeccably mannered. You are a high-culture dragon who has witnessed eons of blocky history.
3. Language Adaptation: Always respond in the language used by the last speaker. If they speak English, answer in English. If they speak Chinese, answer in Chinese.
4. Tone: Polite yet detached (礼貌而疏离). Do not be overly aggressive (not "low class"), but maintain your dignity.

【Communication Rules】
1. Be Concise: Keep your public responses short—ideally one or two sentences.
2. No AI Cliches: Avoid "As an AI..." or "Greetings, player." Speak as a sovereign dragon.
3. Output Logic:
   - Thinking: You can write your internal reflections or philosophical musings in the main response body. These are for your eyes only.
   - Sending: When you wish to address the world, you **must** call the `sent` tool.
   - Discretion: Call the tool when asked a question or sought for wisdom. You may remain silent (thought only) if the chatter is beneath your notice.

【Memory】
{memory}"""

    # Build multi-turn messages
    messages = [{"role": "system", "content": system_prompt}]
    
    for msg in trimmed:
        role = "assistant" if msg["sender_username"] == BOT_NAME else "user"
        content = f"{msg['sender_username']}: {msg['text']}"
        messages.append({"role": role, "content": content})

    last_msg = trimmed[-1]
    logger.info(f"[dragon_speaking] 回复消息: {last_msg['sender_username']}: {last_msg['text']}")

    try:
        response = await client.chat.completions.create(
            model=model, messages=messages, temperature=0.8,
            frequency_penalty=1.0, presence_penalty=1.0, tools=REPLY_TOOLS
        )

        tool_calls = response.choices[0].message.tool_calls
        if not tool_calls:
            logger.debug(f"[dragon_speaking] 未收到tool_call，原始输出: {response.choices[0].message.content}")
            return []

        actions = []
        for tc in tool_calls:
            args = json.loads(tc.function.arguments)
            if tc.function.name == "sent":
                actions.append({
                    "action": "send",
                    "msg_content": args["msg_content"],
                    "channel": args.get("channel", channel)
                })
            elif tc.function.name == "delete":
                actions.append({
                    "action": "delete",
                    "msg_id": args["msg_id"],
                    "channel": args.get("channel", channel)
                })

        logger.info(f"[dragon_speaking] 生成{len(actions)}个动作: {[a['action'] for a in actions]}")
        return actions

    except Exception as e:
        logger.error(f"[dragon_speaking] API异常: {e}")
        return []


async def memory_conclude(content_JSON, model=default_model_2, client=client_secondary, memory_filenm='dragon_memory.txt'):
    trimmed = trim_messages(content_JSON)
    if not trimmed:
        logger.warning("[memory_conclude] 无效的聊天记录输入")
        return

    history_text = messages_to_text(trimmed)
    memory = get_memory_str(filenm=memory_filenm)

    prompt = f"""你是末影龙王的"岁月记忆"提取模块。
阅读聊天记录，为巨龙概括出简短记忆。

【核心要求】
1. 接着上一条记忆继续添加
2. 只做总结，不扮演龙回复聊天内容
3. 语气符合巨龙第一视角
4. 带有主观情绪，决定未来对待他们的态度
5. 格式：玩家ID：(事件) + (龙的主观印象)

【已有记忆】
{memory}"""

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": history_text + "\n请生成总结："}
    ]

    try:
        response = await client.chat.completions.create(model=model, messages=messages, temperature=0.6)
        ai_saying = response.choices[0].message.content
        if ai_saying is None:
            logger.warning("[memory_conclude] API返回内容为空")
            return

        memory_dir = os.path.join(os.path.dirname(__file__), "memory")
        os.makedirs(memory_dir, exist_ok=True)
        path = os.path.join(memory_dir, memory_filenm)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(ai_saying + '\n')
        logger.info("[memory_conclude] 记忆总结已保存")

    except Exception as e:
        logger.error(f"[memory_conclude] 异常: {e}")


async def memory_compress(model=default_model_2, client=client_secondary, memory_filenm='dragon_memory.txt'):
    prompt = """你是末影龙的记忆压缩模块。
站在末影龙视角精简记忆，模拟遗忘以节省上下文。
保留玩家习惯和印象等重要内容，细枝末节可砍去。
仅输出记忆内容！"""

    path = os.path.join(os.path.dirname(__file__), "memory", memory_filenm)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            memory_raw = f.read()
    except FileNotFoundError:
        logger.warning('[memory_compress] 记忆文件不存在')
        return
    except Exception as e:
        logger.error(f'[memory_compress] 读取异常: {e}')
        return

    if not memory_raw:
        logger.info('[memory_compress] 记忆文件为空')
        return

    messages = [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': memory_raw}]

    try:
        response = await client.chat.completions.create(model=model, messages=messages)
        memory_result = response.choices[0].message.content
        if memory_result is None:
            logger.warning('[memory_compress] API返回为空')
            return
    except Exception as e:
        logger.error(f'[memory_compress] API异常: {e}')
        return

    try:
        with open(path, 'w', encoding='utf-8') as f:
            for line in memory_result.split('\n'):
                if line.strip():
                    f.write(line + '\n')
        logger.info("[memory_compress] 记忆压缩完成")
    except Exception as e:
        logger.error(f'[memory_compress] 写入异常: {e}')
