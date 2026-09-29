"""入口：编排启动顺序（工具中枢 → webhook → buddy init/chat ws → 房间 ws）。"""
import asyncio
import sys

from .buddy_client import BuddyClient
from .config import WEBHOOK_CONFIG
from .logger import setup_logger
from .room_client import RoomClient
from .tools import build_hub
from .webhook import make_webhook_app, start_webhook

logger = setup_logger("main")


async def main():
    logger.info("Starting Endra connector...")

    # 1. 先装配工具中枢（含 MCP 连接），工具定义要在 buddy init 时一起下发
    tool_hub = await build_hub()

    buddy = BuddyClient(tool_hub=tool_hub)
    room = RoomClient(buddy)

    # 2. 起 webhook 监听（alive-buddy 的 send_url 指向 /webhook，工具回调走 /tools/call）
    app = make_webhook_app(room.send_reply, tool_hub=tool_hub)
    await start_webhook(app, WEBHOOK_CONFIG["host"], WEBHOOK_CONFIG["port"])

    # 3. buddy 会话 + chat ws（后台自维护断线重连/重 init）
    buddy_task = asyncio.create_task(buddy.run())

    # 4. 等 buddy 就绪后再连聊天室，避免历史灌入时无处投递
    await buddy.wait_ready()

    # 5. 房间连接主循环（前台，Ctrl+C 退出）
    try:
        await room.run()
    finally:
        buddy_task.cancel()
        await tool_hub.aclose()


def entrypoint():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.critical(f"Unhandled exception: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()
