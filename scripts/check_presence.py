"""在线名单核对：确认"在场感知闸门"数的是真人，而不是常驻 bot。

    docker compose exec endra-connector python scripts/check_presence.py

闸门靠 coffeeroom `GET /api/online-users` 判断房间里有没有人。如果某个**常驻 bot**
（例如把 MC 聊天转发进房间的桥接账号）也在名单里，闸门会永远认为"有人"、从而完全失效。
这个脚本用 bot 自己的 cookie 查一次，把名单摊开给你看，并指出可疑的常驻账号。

只读：只发一个 GET。
"""
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from connector.config import BOT_CONFIG, COOKIE_CACHE_FILE, PRESENCE_CONFIG  # noqa: E402


def load_cookie() -> str:
    try:
        data = json.loads(COOKIE_CACHE_FILE.read_text())
    except Exception as e:
        print(f"✗ 读取 cookie 缓存失败（{COOKIE_CACHE_FILE}）: {e}")
        return ""
    return data.get("cookie") or ""


def main() -> int:
    cookie = load_cookie()
    if not cookie:
        print("✗ 没有可用的 session cookie，无法查询在线名单")
        return 1

    room = (BOT_CONFIG.get("room") or "").lower()
    bot = (BOT_CONFIG.get("username") or "").lower()
    ignore = set(PRESENCE_CONFIG["ignore_users"] or [])

    url = PRESENCE_CONFIG["api_url"]
    print(f"接口：{url}\n本房间：{room}   Endra 自己：{bot or '（未配置 BOT_USERNAME）'}\n"
          f"忽略名单：{sorted(ignore) or '（空）'}\n")

    try:
        resp = requests.get(url, headers={"Cookie": cookie}, timeout=PRESENCE_CONFIG["timeout"])
    except Exception as e:
        print(f"✗ 请求失败：{e}")
        return 1
    if resp.status_code != 200:
        print(f"✗ HTTP {resp.status_code}（cookie 可能已失效）")
        return 1

    users = (resp.json() or {}).get("users") or []
    print(f"全站在线账号 {len(users)} 个：")
    channel_counter = Counter(str(u.get("channel") or "") for u in users)
    for u in users:
        name = str(u.get("username") or "")
        channel = str(u.get("channel") or "")
        marks = []
        if name.lower() == bot:
            marks.append("Endra 自己，不计入")
        if name.lower() in ignore:
            marks.append("已在忽略名单")
        in_room = channel.lower() == room
        if not in_room:
            marks.append("不在本房间")
        print(f"   {name:22s} channel={channel:12s} {'；'.join(marks)}")

    humans = [
        str(u.get("username")) for u in users
        if str(u.get("channel") or "").lower() == room
        and str(u.get("username") or "").lower() != bot
        and str(u.get("username") or "").lower() not in ignore
    ]

    print(f"\n闸门认定本房间真人：{humans or '无'}（{len(humans)} 人）")
    print("→ 0 人时：主动发言只保留「回应」与每日配额（默认 1 条/24h）")

    if humans:
        print("\n⚠ 注意：此刻有人在线。请在**确实没人**的时候再跑一次，确认会变成 0 人；")
        print("  如果任何时候跑都≥1 人，说明有常驻 bot 在线，把它加进 PRESENCE_IGNORE_USERS。")
    for name, count in channel_counter.items():
        if count == 1:
            continue
        print(f"⚠ channel={name} 有 {count} 个在线账号，若其中含桥接/多开会话请核对")

    print("\n✅ 核对完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
