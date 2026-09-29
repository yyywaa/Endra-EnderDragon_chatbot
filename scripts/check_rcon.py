"""RCON 连通性自检：在 connector 容器里（或宿主机上）跑一次即可。

    docker compose exec endra-connector python scripts/check_rcon.py

会依次检查：
  1. 配置里读到的 host/port（密码不回显）；
  2. 逐个候选地址的 TCP 连通性（host.docker.internal / 172.17.0.1 / 127.0.0.1）；
  3. RCON 认证 + `list` 命令，打印在线玩家；
  4. 「只踢 bot」的判定结果（哪些名字被认定为机器人、哪些被保护）。

只读：不会下发 kick 之类的命令。
"""
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector.config import TOOLS_CONFIG  # noqa: E402
from connector.mc_rcon import RconAuthError, RconError, rcon_command  # noqa: E402
from connector.tools.minecraft import MinecraftTools, parse_player_count, parse_player_list  # noqa: E402

CANDIDATES = ("host.docker.internal", "172.17.0.1", "127.0.0.1")


def probe(host: str, port: int, timeout: float = 3.0) -> str:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return "可连接"
    except Exception as e:
        return f"失败（{type(e).__name__}）"


def main() -> int:
    port = int(TOOLS_CONFIG["mc_rcon_port"])
    password = TOOLS_CONFIG["mc_rcon_password"]
    host = TOOLS_CONFIG["mc_rcon_host"]

    print(f"配置：host={host} port={port} password={'已设置' if password else '未设置'}")
    if not password:
        print("✗ 未设置 MC_RCON_PASSWORD（在服务器 /data/NEKO/server.properties 里找 rcon.password）")
        return 1

    print("\n[1] 候选地址连通性")
    reachable = []
    for candidate in dict.fromkeys((host, *CANDIDATES)):
        result = probe(candidate, port)
        print(f"    {candidate}:{port} -> {result}")
        if result == "可连接":
            reachable.append(candidate)
    if not reachable:
        print("✗ 没有一个地址能连上 RCON。检查：rcon 是否启用、端口是否正确、"
              "compose 是否加了 extra_hosts: host.docker.internal:host-gateway")
        return 1

    print("\n[2] RCON 认证与 list")
    working_host = host if host in reachable else reachable[0]
    try:
        output = rcon_command(working_host, port, password, "list", TOOLS_CONFIG["mc_rcon_timeout"])
    except RconAuthError:
        print("✗ 密码错误（server.properties 的 rcon.password 与 .env 不一致）")
        return 1
    except RconError as e:
        print(f"✗ 通信失败：{e}")
        return 1

    names = parse_player_list(output)
    print(f"✓ 认证成功。在线 {parse_player_count(output)} 人：{output or '（无输出）'}")
    if working_host != host:
        print(f"提示：建议把 MC_RCON_HOST 改成 {working_host}（当前 {host} 不可达）")

    print("\n[3] 踢人权限判定（只判定，不下发命令）")
    tools = MinecraftTools({**TOOLS_CONFIG, "mc_rcon_host": working_host})
    for name in names:
        kind = tools.classify(name)
        label = {"bot": "可踢（机器人）", "protected": "受保护，永不可踢", "human": "玩家，不可踢"}[kind]
        print(f"    {name}: {label}")
    print(f"    可踢名单配置：{tools.kickable_bots() or '（空 —— 当前谁都不能踢）'}")
    print(f"    mc_kick 开关：{'开' if TOOLS_CONFIG['mc_kick_enabled'] else '关'}")

    print("\n✅ 自检完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
