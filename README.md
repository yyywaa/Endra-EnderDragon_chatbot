# Endra connector（重构版）

coffeeroom 聊天室 / Minecraft 服务器中的末影龙聊天机器人。

**架构**：本仓库已退化为薄连接层——只保留 coffeeroom 协议处理（鉴权、心跳、缓冲、去重、冷却），"大脑"（人设、记忆、回复决策、主动发言）全部委托给 [alive-buddy](https://github.com/yyywaa/alive-buddy)。重构依据见 alive-buddy 仓库 `docs/ENDRA_MIGRATION.md`。

```
coffeeroom (wss://room.caffeine.ink)
   ↑↓ ws (Cookie 鉴权)
endra-connector (本仓库)
   ↑↓ ws  ws://alive-buddy:3000/v1/chat        投递聊天消息（silent 批处理）
   ↑↓ http POST /webhook                       接收 agent 发言并转发进聊天室
alive-buddy API (Fastify, :3000)  +  ML sidecar (FastAPI, :8001)
```

## 目录结构

```
connector/
├── main.py            # 入口：webhook → buddy init/chat ws → 房间 ws
├── room_client.py     # coffeeroom 连接层：过滤/缓冲/冷却/silent 批处理/重连
├── buddy_client.py    # alive-buddy 客户端：init / chat ws 投递 / status / debug
├── webhook.py         # POST /webhook 接收 agent 发言
├── session_manager.py # coffeeroom 鉴权（oa_ticket 换 cookie、OAT 自动续签）
├── config.py          # 全部走 env
└── logger.py
tests/                 # 消息过滤、批次 silent 标记、冷却窗口、断线重 init
scripts/e2e_headless.py  # 无头端到端验证（不连 coffeeroom）
Dockerfile             # connector 镜像
docker-compose.yml     # connector + alive-buddy + ml-sidecar
```

## 部署

alive-buddy 仓库需与本仓库并排放置（compose 的 build context 指向 `../alive-buddy`）。

```bash
cp .env.example .env  # 填入 coffeeroom 凭据与 LLM key
docker compose build
docker compose up -d ml-sidecar alive-buddy

# 无头联调（可选，不连聊天室验证全链路）
docker compose run --rm --no-deps --name endra-e2e \
  -e WEBHOOK_PUBLIC_URL=http://endra-e2e:9199/webhook \
  endra-connector python scripts/e2e_headless.py

docker compose up -d
```

alive-buddy 的角色记忆与状态持久化在 `buddy-data` 卷（`data/characters`，SQLite），容器重建不失忆；但 session 是内存态，alive-buddy 重启后 connector 会自动重新 init。

## 本地开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
python -m connector.main
```
