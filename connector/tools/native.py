"""原生工具 provider：萌娘百科 / Wikidata（维基）/ 币价。

为什么不用现成的 MCP server 包一层：这几个源的瓶颈是**域名可达性**而不是协议
（实测部署网络下 wikipedia/coingecko/binance/okx 均超时，moegirl/wikidata/gate/coinex 可达），
MCP 只是多一跳进程，救不了被墙的域名。这里直接做 HTTP provider，
base_url 全部可配，将来要换镜像或挂代理只需改 env。
"""
from typing import List

from ..logger import setup_logger
from .hub import Tool, ToolHub, json_get, truncate

logger = setup_logger("tools.native")


# ---------------------------------------------------------------- 萌娘百科

async def moegirl_search(args: dict, config: dict) -> str:
    """按关键词找条目名（萌娘百科禁用了 list=search，只能用 opensearch 拿标题）。"""
    query = str(args.get("query") or "").strip()
    limit = max(1, min(int(args.get("limit") or 5), 10))
    if not query:
        return "需要提供 query（要搜的条目名或关键词）。"

    base = config["moegirl_api_base"]
    data = await json_get(base, {
        "action": "opensearch",
        "format": "json",
        "search": query,
        "limit": limit,
        "namespace": 0,
        "redirects": "resolve",
    })
    # opensearch 返回 [query, [titles], [descriptions], [urls]]
    if not isinstance(data, list) or len(data) < 2 or not data[1]:
        return f"萌娘百科没有找到与「{query}」相关的条目。"
    titles: List[str] = data[1]
    descs = data[2] if len(data) > 2 and isinstance(data[2], list) else []
    urls = data[3] if len(data) > 3 and isinstance(data[3], list) else []

    lines = [f"萌娘百科候选（共 {len(titles)} 条）："]
    for i, title in enumerate(titles):
        desc = descs[i] if i < len(descs) and descs[i] else ""
        url = urls[i] if i < len(urls) else ""
        lines.append(f"{i + 1}. {title}{f'——{desc}' if desc else ''}{f'（{url}）' if url else ''}")
    lines.append("要读正文请用 moegirl_page 传条目名。")
    return "\n".join(lines)


async def moegirl_page(args: dict, config: dict) -> str:
    """读取萌娘百科条目的开头摘要。"""
    title = str(args.get("title") or "").strip()
    if not title:
        return "需要提供 title（条目名，可先用 moegirl_search 找）。"

    base = config["moegirl_api_base"]
    data = await json_get(base, {
        "action": "query",
        "format": "json",
        "prop": "extracts",
        "explaintext": 1,
        "exintro": 1,
        "redirects": 1,
        "titles": title,
    })
    pages = ((data.get("query") or {}).get("pages") or {})
    if not pages:
        return f"萌娘百科没有「{title}」这个条目。"
    page = next(iter(pages.values()))
    if "missing" in page:
        return f"萌娘百科没有「{title}」这个条目（可先用 moegirl_search 搜准确名称）。"

    extract = (page.get("extract") or "").strip()
    if not extract:
        return f"萌娘百科条目「{page.get('title')}」没有可读的纯文本摘要。"
    return f"萌娘百科《{page.get('title')}》开头：\n{extract}"


# ---------------------------------------------------------------- 维基 / Wikidata

# 只挑对聊天有用的属性，避免把整张实体表灌进上下文
_WIKI_PROPERTIES = {
    "P31": "类型",
    "P106": "职业",
    "P569": "出生",
    "P571": "创立/问世",
    "P577": "出版",
    "P495": "所属国家",
    "P17": "国家",
    "P136": "流派/类型",
    "P175": "表演者",
    "P170": "创作者",
    "P50": "作者",
    "P279": "上位类型",
}

_ENTITY_VALUE_PROPS = {"P31", "P106", "P495", "P17", "P136", "P175", "P170", "P50", "P279"}


def _pick_lang(mapping: dict, lang: str) -> str:
    """按 目标语言 → zh → en → 任意 的顺序取一个值。"""
    if not isinstance(mapping, dict):
        return ""
    for key in (lang, "zh", "zh-cn", "zh-hans", "en"):
        if key in mapping:
            value = mapping[key]
            if isinstance(value, dict) and value.get("value"):
                return str(value["value"])
    for value in mapping.values():
        if isinstance(value, dict) and value.get("value"):
            return str(value["value"])
    return ""


def _format_time(value: dict) -> str:
    raw = str(value.get("time") or "")
    if not raw:
        return ""
    digits = raw.lstrip("+").split("T")[0]
    precision = int(value.get("precision") or 11)
    if precision <= 9:
        return digits.split("-")[0]  # 只精确到年
    if precision == 10:
        return "-".join(digits.split("-")[:2])
    return digits


async def wiki_lookup(args: dict, config: dict) -> str:
    """在 Wikidata 上查一个实体的多语言标签、描述与少量关键属性。"""
    query = str(args.get("query") or "").strip()
    lang = str(args.get("lang") or config["wiki_lang"]).strip() or "zh"
    if not query:
        return "需要提供 query（要查的名称/概念）。"

    base = config["wiki_api_base"]

    # 1. 先搜实体
    search = await json_get(base, {
        "action": "wbsearchentities",
        "format": "json",
        "search": query,
        "language": lang,
        "uselang": lang,
        "limit": 3,
        "type": "item",
    })
    hits = search.get("search") or []
    if not hits:
        return f"Wikidata 上没有找到「{query}」。可以用英文名再试一次。"
    best = hits[0]
    qid = best.get("id")

    # 2. 拉实体详情
    entity_data = await json_get(base, {
        "action": "wbgetentities",
        "format": "json",
        "ids": qid,
        "props": "labels|descriptions|aliases|claims",
        "languages": f"{lang}|zh|en",
    })
    entity = (entity_data.get("entities") or {}).get(qid) or {}
    label = _pick_lang(entity.get("labels") or {}, lang) or best.get("label") or qid
    description = _pick_lang(entity.get("descriptions") or {}, lang) or best.get("description") or ""
    aliases = [
        a.get("value") for a in ((entity.get("aliases") or {}).get(lang) or [])
        if isinstance(a, dict) and a.get("value")
    ][:3]

    # 3. 解析属性；实体型取值需要再批量取一次标签
    claims = entity.get("claims") or {}
    picked = []
    need_labels = []
    for pid, cname in _WIKI_PROPERTIES.items():
        statements = claims.get(pid) or []
        if not statements:
            continue
        snak = (statements[0] or {}).get("mainsnak") or {}
        datavalue = snak.get("datavalue") or {}
        value = datavalue.get("value")
        if value is None:
            continue
        if pid in _ENTITY_VALUE_PROPS and isinstance(value, dict) and value.get("id"):
            need_labels.append(value["id"])
            picked.append((cname, value["id"]))
        elif isinstance(value, dict) and "time" in value:
            text = _format_time(value)
            if text:
                picked.append((cname, text))
        elif isinstance(value, (str, int, float)):
            picked.append((cname, str(value)))

    label_map = {}
    if need_labels:
        resolved = await json_get(base, {
            "action": "wbgetentities",
            "format": "json",
            "ids": "|".join(need_labels[:24]),
            "props": "labels",
            "languages": f"{lang}|zh|en",
        })
        for rid, rentity in (resolved.get("entities") or {}).items():
            label_map[rid] = _pick_lang((rentity or {}).get("labels") or {}, lang) or rid

    lines = [f"Wikidata：{label}（{qid}）"]
    if description:
        lines.append(f"简介：{description}")
    if aliases:
        lines.append(f"别名：{'、'.join(aliases)}")
    for cname, value in picked:  # 不做条数裁剪，属性表本身就只有十几项
        lines.append(f"{cname}：{label_map.get(value, value)}")
    if len(hits) > 1:
        others = [f"{h.get('label')}({h.get('id')})" for h in hits[1:] if h.get("label")]
        if others:
            lines.append(f"（同类候选：{'、'.join(others)}）")
    lines.append(f"条目：https://www.wikidata.org/wiki/{qid}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 币价

_CRYPTO_ALIASES = {
    "比特币": "BTC", "bitcoin": "BTC", "btc": "BTC",
    "以太坊": "ETH", "以太": "ETH", "ethereum": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL", "索拉纳": "SOL",
    "狗狗币": "DOGE", "dogecoin": "DOGE", "doge": "DOGE",
    "瑞波": "XRP", "ripple": "XRP", "xrp": "XRP",
    "币安币": "BNB", "bnb": "BNB",
    "ton": "TON", "toncoin": "TON",
    "艾达币": "ADA", "ada": "ADA", "cardano": "ADA",
    "波卡": "DOT", "dot": "DOT", "polkadot": "DOT",
    "莱特币": "LTC", "ltc": "LTC", "litecoin": "LTC",
    "trx": "TRX", "波场": "TRX", "tron": "TRX",
}


def _normalize_symbols(raw: str) -> List[str]:
    parts = [p for p in raw.replace("，", ",").replace("/", ",").replace(" ", ",").split(",") if p.strip()]
    out: List[str] = []
    for part in parts:
        key = part.strip().lower()
        symbol = _CRYPTO_ALIASES.get(key, part.strip().upper())
        if symbol and symbol not in out:
            out.append(symbol)
    return out[:10]


async def _gate_price(symbol: str, quote: str, config: dict) -> dict:
    data = await json_get(f"{config['crypto_api_base'].rstrip('/')}/api/v4/spot/tickers", {
        "currency_pair": f"{symbol}_{quote}",
    })
    if not isinstance(data, list) or not data:
        raise ValueError(f"Gate.io 没有 {symbol}_{quote} 这个交易对")
    row = data[0]
    return {
        "last": row.get("last"),
        "change": row.get("change_percentage"),
        "high": row.get("high_24h"),
        "low": row.get("low_24h"),
        "source": "Gate.io",
    }


async def _coinex_price(symbol: str, quote: str, config: dict) -> dict:
    data = await json_get(f"{config['crypto_fallback_base'].rstrip('/')}/v2/spot/ticker", {
        "market": f"{symbol}{quote}",
    })
    rows = data.get("data") if isinstance(data, dict) else None
    if not rows:
        raise ValueError(f"CoinEx 没有 {symbol}{quote} 这个交易对")
    row = rows[0] if isinstance(rows, list) else rows
    return {
        "last": row.get("last"),
        "change": None,  # CoinEx ticker 不含 24h 涨跌幅
        "high": row.get("high"),
        "low": row.get("low"),
        "source": "CoinEx",
    }


async def crypto_price(args: dict, config: dict) -> str:
    """查现货价格（Gate.io 主、CoinEx 备）。"""
    symbols = _normalize_symbols(str(args.get("symbols") or "BTC"))
    quote = str(args.get("quote") or "USDT").strip().upper() or "USDT"
    if not symbols:
        return "需要提供 symbols，例如 \"BTC,ETH\"。"

    lines = []
    for symbol in symbols:
        result = None
        errors = []
        for fetch in (_gate_price, _coinex_price):
            try:
                result = await fetch(symbol, quote, config)
                break
            except Exception as e:  # 主源失败自动切备源
                errors.append(str(e))
        if result is None:
            lines.append(f"{symbol}/{quote}：查询失败（{'；'.join(errors[:2])}）")
            continue
        detail = [f"{symbol}/{quote} {result['last']}"]
        if result.get("change"):
            detail.append(f"24h {result['change']}%")
        if result.get("high") and result.get("low"):
            detail.append(f"24h 高 {result['high']} / 低 {result['low']}")
        detail.append(f"来源 {result['source']}")
        lines.append("　".join(detail))

    return "实时行情（仅供闲聊参考，非投资建议）：\n" + "\n".join(lines)


# ---------------------------------------------------------------- 注册

def _bind(handler, config: dict):
    """把 config 绑进 handler，便于测试与多实例隔离。"""
    async def bound(args: dict) -> str:
        return await handler(args, config)
    return bound


def register_native_tools(hub: ToolHub):
    config = hub.config
    if not config.get("enabled"):
        return
    bind = lambda h: _bind(h, config)  # noqa: E731

    if config["moegirl_enabled"]:
        hub.register(Tool(
            name="moegirl_search",
            description="在萌娘百科搜索条目，返回候选条目名。想聊 ACG 梗、角色、作品时先用它找准确名称。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词或条目名，例如「初音未来」"},
                    "limit": {"type": "integer", "description": "返回候选数量，默认 5，上限 10"},
                },
                "required": ["query"],
            },
            handler=bind(moegirl_search),
        ))
        hub.register(Tool(
            name="moegirl_page",
            description="读取萌娘百科某个条目的开头摘要，用于了解角色/作品/梗的来历。",
            parameters={
                "type": "object",
                "properties": {"title": {"type": "string", "description": "条目名，建议先用 moegirl_search 确认"}},
                "required": ["title"],
            },
            handler=bind(moegirl_page),
        ))

    if config["wiki_enabled"]:
        hub.register(Tool(
            name="wiki_lookup",
            description="查一个实体（人物、作品、地点、概念）的多语言名称、简介与关键属性（类型/职业/创立时间等）。用于核对事实、避免记错。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要查的名称，中文或英文"},
                    "lang": {"type": "string", "description": "偏好语言，默认 zh"},
                },
                "required": ["query"],
            },
            handler=bind(wiki_lookup),
        ))

    if config["crypto_enabled"]:
        hub.register(Tool(
            name="crypto_price",
            description="查加密货币现货价格与 24 小时涨跌。有人在聊行情、或者你想拿现实世界的数字打比方时用。",
            parameters={
                "type": "object",
                "properties": {
                    "symbols": {"type": "string", "description": "逗号分隔，例如 \"BTC,ETH\"，最多 10 个"},
                    "quote": {"type": "string", "description": "计价货币，默认 USDT"},
                },
                "required": ["symbols"],
            },
            handler=bind(crypto_price),
            per_minute=3,  # 行情类工具限制更紧一些
        ))
