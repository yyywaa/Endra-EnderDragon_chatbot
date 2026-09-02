"""入口：编排启动顺序（webhook → buddy init/chat ws → 房间 ws）。"""
import asyncio
import sys

from .buddy_client import BuddyClient
from .config import WEBHOOK_CONFIG
from .logger import setup_logger
from .room_client import RoomClient
from .webhook import make_webhook_app, start_webhook

logger = setup_logger("main")


async def main():
    logger.info("Starting Endra connector...")

    buddy = BuddyClient()
    room = RoomClient(buddy)

    # 1. 起 webhook 监听（alive-buddy 的 send_url 指向这里）
    app = make_webhook_app(room.send_reply)
    await start_webhook(app, WEBHOOK_CONFIG["host"], WEBHOOK_CONFIG["port"])

    # 2. buddy 会话 + chat ws（后台自维护断线重连/重 init）
    buddy_task = asyncio.create_task(buddy.run())

    # 3. 等 buddy 就绪后再连聊天室，避免历史灌入时无处投递
    await buddy.wait_ready()

    # 4. 房间连接主循环（前台，Ctrl+C 退出）
    try:
        await room.run()
    finally:
        buddy_task.cancel()


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
