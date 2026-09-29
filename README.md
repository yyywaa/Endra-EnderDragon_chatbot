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
├── main.py            # 入口：工具中枢 → webhook → buddy init/chat ws → 房间 ws
├── room_client.py     # coffeeroom 连接层：过滤/缓冲/冷却/silent 批处理/重连/在场闸门
├── presence.py        # 在场感知：查本房间在线真人，喂给出站闸门
├── buddy_client.py    # alive-buddy 客户端：init（含工具定义/采样/记忆窗口）· chat ws 投递
├── webhook.py         # POST /webhook 收发言；POST /tools/call 承接工具回调
├── tools/             # 工具中枢：hub（限流/超时/降级）· native（萌百/Wikidata/币价）· mcp
├── session_manager.py # coffeeroom 鉴权（oa_ticket 换 cookie、OAT 自动续签）
├── config.py          # 全部走 env（含人设提示词）
└── logger.py
tests/                 # 消息过滤/批次/冷却/在场闸门/工具层/webhook 路由
scripts/e2e_headless.py  # 无头端到端验证（不连 coffeeroom）
Dockerfile             # connector 镜像
docker-compose.yml     # connector + alive-buddy + ml-sidecar
```

## 工具（萌娘百科 / 维基 / 币价 / MCP）

Endra 现在能主动查资料再开口，而不是凭印象编。**工具在 connector 侧执行**，
alive-buddy 只通过 `extend_tool_list` 拿到 function 定义，调用时回调 `POST /tools/call`：

```
reAct ──function_call──▶ alive-buddy 的 RemoteTool ──POST /tools/call──▶ connector
                                                                        ├─ native: 萌百 / Wikidata / 币价
                                                                        └─ MCP server（stdio / streamable HTTP）
```

这样做有三个理由：DeepSeek 的 Responses API **忽略 `mcp` 等内置工具**（只支持 `function`），
服务端不会替我们调；密钥与出网策略集中在一层；alive-buddy 不需要任何新依赖。

内置工具（`TOOLS_ENABLED=true` 时默认开启）：

| 工具 | 说明 |
|---|---|
| `moegirl_search` / `moegirl_page` | 萌娘百科条目搜索与开头摘要（ACG 作品、角色、梗） |
| `wiki_lookup` | Wikidata 实体资料：多语言名称、简介、类型/职业/创立时间等关键属性 |
| `crypto_price` | 加密货币现货价与 24h 涨跌（Gate.io 主、CoinEx 备） |

护栏：单工具每分钟/每天次数、全天总量、单次超时；任何失败都降级成一段可读文本，
让角色自然地表示"查不到"，而不是把 reAct 打挂。**默认不截断、不过滤工具结果**
（`TOOL_RESULT_MAX_CHARS=0`），只有想压 token 成本时才设上限。

## 部署网络实测（重要）

这些域名在本地实测的可达性决定了我选的源，不是随便挑的：

| 源 | 结果 |
|---|---|
| `zh.moegirl.org.cn/api.php` | ✅ 通（注意 `list=search` 被官方禁用，走 `opensearch` + `prop=extracts`） |
| `www.wikidata.org/w/api.php` | ✅ 通（**维基百科正文域名不通**，所以"维基"落在 Wikidata 上） |
| `api.gateio.ws` / `api.coinex.com` | ✅ 通 |
| `zh.wikipedia.org` / `api.wikimedia.org` | ❌ 超时 |
| CoinGecko / Binance / OKX / Kraken / HTX / MEXC / Bitget | ❌ 超时 |

要换成自己的镜像或代理，只需改 `WIKI_API_BASE` / `CRYPTO_API_BASE` 等 env，无需改代码。

### MCP

任意 MCP server 都能挂（`MCP_SERVERS` 填 JSON 数组，stdio 用 `command`/`args`，远程用 `url`）：

```bash
MCP_SERVERS=[{"name":"fs","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/data"]}]
MCP_SERVERS=[{"name":"remote","url":"https://example.com/mcp","allow":["search"]}]
```

- 工具名默认加 server 名前缀（`fs_read_file`）避免与内置工具冲突，`"prefix": false` 可关。
- `allow` / `deny` 是可选的收窄手段，**默认不限制**。
- 需要 `mcp` SDK（已写进 `requirements.txt`）；未安装时自动跳过并打日志，不影响其他工具。
- 连接失败（命令不存在、握手超时）只跳过该 server，不影响 native 工具。


## 在场感知（防止对着空频道自言自语）

活跃度来自 alive-buddy 的主动脉搏（`PULSE_INTERVAL_MS`，默认 60s 一拍），
房间没人时它会一直自说自话——看上去频道很热闹，实际上没有听众。
connector 因此在**出站最后一米**加了一道闸门：

- **在线人数**取自 coffeeroom `GET /api/online-users`（返回全部房间的在线用户 + `channel`），
  复用已持有的 session cookie，不需要 MC 侧 RCON。只数本房间、排除 Endra 自身与
  `PRESENCE_IGNORE_USERS` 里的桥接/bot 账号；结果缓存 `PRESENCE_CACHE_TTL_SECONDS`。
- **有人在线** → 发言全部放行。
- **没人在线** → 只保留"回应"和极小配额：
  - 距最近一条真人消息 `PRESENCE_REACTIVE_WINDOW_SECONDS` 以内的发言算回应，永远放行（真人来了绝不会被静音）；
  - 其余主动发言受 `QUIET_PROACTIVE_DAILY_QUOTA`（默认 1 条 / `QUIET_PROACTIVE_WINDOW_HOURS`=24h 滚动窗口）限制，超出即丢弃并打日志。
- **名单查询失败** → 按 `PRESENCE_FAIL_MODE` 处理：`quota`（默认，等同空房间）或 `open`（放行）。
- 入站消息照旧投递给 alive-buddy（silent 记忆不受影响），闸门只管"要不要发进房间"。
- 配额计数在内存里（重启清零）。空房间 + `restart: unless-stopped` 的稳定部署下无影响；
  若进程频繁重启，可把 `_quiet_sends` 落盘到 `secrets/` 卷。

> 已知权衡：闸门在 connector，alive-buddy 的脉搏仍在空转——被丢弃的发言
> 已经烧过 LLM，且会被它自己记成"我说过的话"。要彻底省 token 需在 alive-buddy 侧
> 增加 presence 上报并暂停脉搏（见 alive-buddy `docs/ENDRA_MIGRATION.md`）。

### 部署后先核对在线名单

闸门依赖 `channel` 字段认房间。上线前用 bot 自己的 cookie 跑一次，确认返回结构与本房间账号：

```bash
curl -s -H "Cookie: session=<bot cookie>" https://room.caffeine.ink/api/online-users | head -c 500
```

日志里每次名单变化都会打一行 `[Presence] 房间 <room> 在线真人（N）: ...（全站在线账号共 M）`：
- 若 N 恒等于 M（或始终含某个固定账号），说明该账号是 MC 桥接/其他 bot，把它加进 `PRESENCE_IGNORE_USERS`；
- 若 M>0 而 N 恒为 0 但房间里明明有人，检查 `ROOM_NAME` 与接口 `channel` 是否同名大小写不同。


## 为什么它不再复读（alive-buddy 侧配套改动）

"信息量低、总重复"是**机制问题**，不是人设问题。定位到四处成因，已随本次一并修掉：

| 成因 | 修法 |
|---|---|
| `react.ts` 只注入 L1 最近 20 条，而 `send_message`/`internal_monologue` 都往 L1 写 assistant 消息 → 空频道时上下文几乎全是自己的回声 | 上下文窗口可配（默认 30）；内部独白按预算裁剪（默认只留最新 1 条，`-1` 关闭裁剪） |
| L2 剧情梗概**全代码无人读回**，L3 又依赖未部署的 Chroma → 早期记忆等于死的，只剩最近几十条可讲 | `prepareContext` 显式回灌最近 L2 梗概 |
| 每次主动唤醒都投喂同一句固定提示 → 近似输入产出近似输出 | 新增 `impulse.ts`：按"话题域 × 言语行为"抽刺激源，并附上自己最近说过的话做反重复约束 |
| 采样 `presence_penalty/frequency_penalty` 拉满 1.0，只压字面重复、还让话更虚 | 改为 0.4 / 0.6，温度 0.8→0.9，全部走 env |

人设侧（`connector/config.py` 的 `SYSTEM_PROMPT_TEMPLATE`）新增【Substance】【Range】【Anti-Repetition】【Tools】四节：
要求每次开口至少交付一件具体的东西、题材覆盖星象/炼金/地质/词源/音乐/宴席等 Minecraft 之外的方向、
开口前先比对自己最近说过的话，并说明何时该调工具。

## 部署

alive-buddy 仓库需与本仓库并排放置（compose 的 build context 指向 `../alive-buddy`）。

```bash
# 0. 两个仓库并排 clone
#    git clone https://github.com/yyywaa/Endra-EnderDragon_chatbot.git
#    git clone https://github.com/yyywaa/alive-buddy.git

# 1. 凭据
cp .env.example .env       # 填 BOT_USERNAME / BOT_ACCESS_TOKEN / ROOM_NAME / LLM_API_KEY

# 2. cookie 缓存挂载点：必须先建文件。
#    若宿主机只有 secrets/ 目录，Docker 会把挂载点当成目录创建，
#    connector 就再也写不进 cookies.json（会话无法自续签）。
mkdir -p secrets && [ -f secrets/cookies.json ] || echo '{}' > secrets/cookies.json

# 3. 构建（connector 镜像含 mcp SDK，用于 MCP_SERVERS）
docker compose build

# 4. 先起不需要聊天室凭据的两个服务
docker compose up -d ml-sidecar alive-buddy

# 5. 无头联调（不连 coffeeroom，验证 init/工具定义/发言回传全链路）
docker compose run --rm --no-deps --name endra-e2e \
  -e WEBHOOK_PUBLIC_URL=http://endra-e2e:9199/webhook \
  endra-connector python scripts/e2e_headless.py

# 6. 起 connector
docker compose up -d
```

上线后核对三件事：

```bash
docker compose logs --tail=50 endra-connector | grep -E "已注册工具|Presence|下发"
#   ├─ [Tool] 已注册工具: crypto_price, fx_echo, moegirl_page, moegirl_search, wiki_lookup …
#   └─ [Buddy] 下发 N 个工具定义 / [Presence] 房间 <room> 在线真人（N）…
curl -s http://127.0.0.1:9100/health        # {"ok":true,"tools":[...]}

# 工具回调自检（容器内直接打自己的端点）
docker compose exec endra-connector python -c "
import requests,json
print(requests.post('http://127.0.0.1:9100/tools/call',
      json={'name':'crypto_price','arguments':{'symbols':'btc'}},timeout=30).json()['content'])"
```

alive-buddy 的角色记忆与状态持久化在 `buddy-data` 卷（`data/characters`，SQLite），容器重建不失忆；但 session 是内存态，alive-buddy 重启后 connector 会自动重新 init。

> 注意：connector 重启会重新 init session，人设/采样/记忆窗口等**改动需要重启才生效**（记忆按 `reassignSession` 继承，不会失忆）。


## 本地开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
python -m connector.main
```
