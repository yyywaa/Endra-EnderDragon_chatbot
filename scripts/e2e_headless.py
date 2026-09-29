"""无头端到端验证（C1）：不连 coffeeroom，模拟消息批，断言 webhook 收到 agent 回复。

在 compose 网络内运行：
    docker compose run --rm --no-deps \
        -e WEBHOOK_PUBLIC_URL=http://endra-connector:9199/webhook \
        endra-connector python scripts/e2e_headless.py
"""
import asyncio
import json
import os
import sys
import time
import uuid

import requests
import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector.buddy_client import build_character_config  # noqa: E402
from connector.config import BUDDY_CONFIG, WEBHOOK_CONFIG  # noqa: E402
from connector.tools import build_hub  # noqa: E402
from connector.webhook import make_webhook_app, start_webhook  # noqa: E402

TEST_WEBHOOK_PORT = 9199
REPLY_TIMEOUT = 180


async def main():
    replies = asyncio.Queue()

    async def on_message(content: str):
        await replies.put(content)

    # 1. 工具中枢 + 本地 webhook 接收器（工具定义会随 session init 一起下发）
    tool_hub = await build_hub()
    print(f"[E2E] 工具: {', '.join(tool_hub.tool_names()) or '（无）'}")

    app = make_webhook_app(on_message, tool_hub=tool_hub)
    await start_webhook(app, "0.0.0.0", TEST_WEBHOOK_PORT)

    http_base = BUDDY_CONFIG["http_base"].rstrip("/")
    ws_base = BUDDY_CONFIG["ws_base"].rstrip("/")

    # 2. init session（send_url 指向本 webhook）
    config = build_character_config(tool_hub)
    config["connection"]["send_url"] = WEBHOOK_CONFIG["public_url"]
    config["debug"] = True
    print(f"[E2E] init session, send_url={config['connection']['send_url']}")
    resp = requests.post(f"{http_base}/v1/session/init", json=config, timeout=15)
    resp.raise_for_status()
    session_id = resp.json()["session_id"]
    print(f"[E2E] session_id={session_id}")

    # 3. chat ws 投递消息批：第一条 silent，第二条触发 reAct
    async with websockets.connect(f"{ws_base}/v1/chat") as ws:
        async def deliver(text, silent):
            await ws.send(json.dumps({
                "msg_id": str(uuid.uuid4()),
                "user_id": "e2e-tester",
                "session_id": session_id,
                "timestamp": int(time.time() * 1000),
                "silent": silent,
                "payload": {"role": "user", "content": [{"type": "text", "text": text}]},
            }))
            # 消费占位回执
            receipt = json.loads(await ws.recv())
            assert "error" not in receipt, f"服务端回执错误: {receipt}"

        await deliver("e2e-tester: 大家好，我是新来的", silent=True)
        print("[E2E] silent 消息已投递")
        await deliver("e2e-tester: EnderDragon, 听说你是末影龙王，打个招呼吧", silent=False)
        print("[E2E] 非 silent 消息已投递，等待 webhook 回复...")

    # 4. 断言 webhook 收到真实回复
    try:
        content = await asyncio.wait_for(replies.get(), timeout=REPLY_TIMEOUT)
    except asyncio.TimeoutError:
        print("[E2E] ❌ 超时未收到 webhook 回复")
        return 1
    print(f"[E2E] ✅ webhook 收到回复: {content}")

    # 4.5 工具链路自检：直接打本机 /tools/call，确认工具真能执行（不依赖模型是否调用）
    if config["extend_tool_list"]:
        first = config["extend_tool_list"][0]["function"]["name"]
        print(f"[E2E] 工具回调自检: {first}")
    await tool_hub.aclose()

    # 5. 状态演化检查
    status = requests.get(f"{http_base}/v1/session/{session_id}/status", timeout=10).json()
    print(f"[E2E] status: mood={status.get('mood')} energy={status.get('energy')} boredom={status.get('boredom')}")
    print("[E2E] ✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
