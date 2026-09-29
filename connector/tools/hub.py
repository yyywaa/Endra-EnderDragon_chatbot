"""工具中枢：把外部能力暴露成 reAct 可调用的 function 工具。

设计要点
--------
1. **工具在本层执行**（Python/connector），alive-buddy 只拿到 OpenAI 风格的 function 定义，
   通过 `POST /tools/call` 回调过来执行。理由：
   - DeepSeek 的 Responses API 会忽略 `mcp` 等内置工具，只支持 `function`，服务端不会替我们调；
   - 密钥、代理、网络可达性都留在 connector 一层，buddy 不需要任何新依赖；
   - 便于统一做限流、超时、结果截断与日志。
2. **任何失败都必须降级成可读的 observation 文本**：工具报错不该把 reAct 循环打挂，
   角色应该能"看到"失败并自然地绕过它。
3. **成本护栏**：工具结果会进入 LLM 上下文，所以结果长度、单工具频率、全天总量都在这里限制。
"""
import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

import requests

from ..config import TOOLS_CONFIG
from ..logger import setup_logger

logger = setup_logger("tools")


async def json_get(url: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
    """在线程里发一个 GET 并返回 JSON；失败抛异常，由调用方决定如何降级。"""
    def _run():
        resp = requests.get(
            url,
            params=params,
            timeout=timeout or TOOLS_CONFIG["timeout"],
            headers={"User-Agent": TOOLS_CONFIG["user_agent"]},
        )
        resp.raise_for_status()
        return resp.json()

    return await asyncio.to_thread(_run)


def truncate(text: str, limit: Optional[int] = None) -> str:
    """按长度上限裁剪工具结果。

    注意：这不是内容审查，而是上下文成本护栏——工具结果会原样进入 LLM 上下文，
    每条都要按 token 付费。`limit <= 0` 表示不限制长度。
    """
    if limit is None:
        limit = TOOLS_CONFIG["result_max_chars"]
    if limit <= 0:
        return text.strip()
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…（内容过长已截断，可用 TOOL_RESULT_MAX_CHARS=0 关闭截断）"


@dataclass
class Tool:
    """一个可被 reAct 调用的工具。"""

    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], Awaitable[str]]
    # None = 使用全局默认；0 = 该工具显式不限流
    per_minute: Optional[int] = None
    per_day: Optional[int] = None
    # 是否交给模型审查层过一遍（白名单之外的语义防线）
    guarded: bool = False

    def definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class _Usage:
    minute: deque = field(default_factory=deque)
    day: deque = field(default_factory=deque)


class ToolHub:
    def __init__(self, config: Optional[dict] = None, clock: Optional[Callable[[], float]] = None):
        self.config = config or TOOLS_CONFIG
        self._clock = clock or time.time
        self._tools: Dict[str, Tool] = {}
        self._usage: Dict[str, _Usage] = defaultdict(_Usage)
        self._total_day: deque = deque()
        self._lock = asyncio.Lock()
        self.mcp_clients: List = []  # 由 mcp.register_mcp_tools 填充，用于关闭长连接
        self.guard = None  # connector.guard.ToolGuard，在 build_hub 里装配
        # 熔断状态：连续失败达到阈值就暂时停用该工具，避免对着不通的数据源反复白等
        self._failures: Dict[str, int] = defaultdict(int)
        self._open_until: Dict[str, float] = {}
        self.guard_broken = False  # 审查层装配失败：受审工具按保守策略拒绝

    # ---- 注册与导出 ----

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled")) and bool(self._tools)

    def definitions(self) -> List[dict]:
        """给 alive-buddy 的 extend_tool_list（OpenAI function 定义）。"""
        if not self.enabled:
            return []
        return [t.definition() for t in self._tools.values()]

    def tool_names(self) -> List[str]:
        return sorted(self._tools)

    # ---- 限流 ----

    def _check_quota(self, tool: Tool) -> Optional[str]:
        now = self._clock()
        usage = self._usage[tool.name]

        while usage.minute and now - usage.minute[0] > 60:
            usage.minute.popleft()
        while usage.day and now - usage.day[0] > 86400:
            usage.day.popleft()
        while self._total_day and now - self._total_day[0] > 86400:
            self._total_day.popleft()

        per_minute = self.config["per_minute"] if tool.per_minute is None else tool.per_minute
        per_day = self.config["per_day"] if tool.per_day is None else tool.per_day

        if self.config["daily_total"] and len(self._total_day) >= self.config["daily_total"]:
            return f"今日全部工具调用已达上限（{self.config['daily_total']} 次），请不要再调用工具。"
        if per_minute and len(usage.minute) >= per_minute:  # 0 视为不限
            return (f"工具 {tool.name} 本分钟额度已用完（最多 {per_minute} 次）。"
                    "**立刻重试不会成功**，请改用其他工具或直接开口说话。")
        if per_day and len(usage.day) >= per_day:
            return f"工具 {tool.name} 今日调用已达上限（{per_day} 次）。"

        usage.minute.append(now)
        usage.day.append(now)
        self._total_day.append(now)
        return None

    # ---- 熔断 ----

    def _circuit_message(self, name: str) -> Optional[str]:
        """熔断打开时返回一句立刻可读的话（不再发起真实请求）。"""
        until = self._open_until.get(name)
        if until is None:
            return None
        now = self._clock()
        if now >= until:
            # 冷却结束：半开，放行一次探测
            self._open_until.pop(name, None)
            self._failures[name] = self._failures.get(name, 0) - 1 if self._failures.get(name) else 0
            return None
        left = int(until - now)
        return (
            f"工具 {name} 最近连续失败多次（数据源可能不通），已暂时停用，约 {left} 秒后自动重试。"
            "**立刻重试不会成功**，请改用其他工具，或直接基于已知信息说话。"
        )

    def _record_success(self, name: str):
        self._failures[name] = 0
        self._open_until.pop(name, None)

    def _record_failure(self, name: str, why: str):
        threshold = int(self.config.get("circuit_threshold") or 0)
        self._failures[name] = self._failures.get(name, 0) + 1
        if threshold and self._failures[name] >= threshold:
            cooldown = float(self.config.get("circuit_cooldown") or 600)
            self._open_until[name] = self._clock() + cooldown
            logger.warning(
                f"[Tool] {name} 连续失败 {self._failures[name]} 次（{why}），"
                f"熔断 {cooldown:g}s，期间不再发起请求"
            )

    # ---- 调用 ----

    async def call(self, name: str, arguments: Optional[dict] = None) -> str:
        """执行一次工具调用，永远返回可读文本（异常也被翻译成 observation）。"""
        tool = self._tools.get(name)
        if tool is None:
            return f"没有名为 {name} 的工具。可用工具：{', '.join(self.tool_names()) or '（无）'}。"

        async with self._lock:
            quota_error = self._check_quota(tool)
        if quota_error:
            logger.warning(f"[Tool] 限流拒绝 {name}: {quota_error}")
            return quota_error

        args = arguments or {}

        drained = self._circuit_message(name)
        if drained:
            logger.warning(f"[Tool] 熔断中，跳过 {name}")
            return drained

        # 模型审查层：白名单挡的是"机制上不可能"，这一层挡的是"机制合法但意图可疑"
        needs_review = tool.guarded or (self.guard is not None and self.guard.should_review(name))
        if self.guard_broken and needs_review:
            logger.error(f"[Guard] 审查层不可用，拒绝受审工具 {name}")
            return "安全审查层当前不可用，按保守策略拒绝这次调用。可以如实说明你暂时无法操作。"
        if self.guard is not None and needs_review:
            verdict = await self.guard.review(name, args)
            if not verdict.allowed:
                logger.warning(f"[Guard] 拒绝 {name}: {verdict.reason}")
                return verdict.reason
        started = self._clock()
        logger.info(f"[Tool] 调用 {name} args={str(args)[:160]}")
        try:
            result = await asyncio.wait_for(tool.handler(args), timeout=self.config["timeout"] + 3)
            self._record_success(name)
            text = truncate(result, self.config["result_max_chars"])
            elapsed = self._clock() - started
            logger.info(f"[Tool] 完成 {name} 用时 {elapsed:.1f}s 返回 {len(text)} 字")
            return text
        except asyncio.TimeoutError:
            self._record_failure(name, "超时")
            logger.warning(f"[Tool] {name} 超时")
            return f"工具 {name} 超时未返回（{self.config['timeout']}s），这个方向暂时查不到，换个方式吧。"
        except Exception as e:
            self._record_failure(name, type(e).__name__)
            logger.warning(f"[Tool] {name} 失败: {e}")
            return f"工具 {name} 执行失败：{e}"


    async def aclose(self):
        """关闭 MCP 长连接（进程退出时调用）。"""
        for client in self.mcp_clients:
            try:
                await client.close()
            except Exception as e:
                logger.warning(f"[Tool] 关闭 MCP 客户端失败: {e}")
        self.mcp_clients = []


async def build_hub(config: Optional[dict] = None) -> ToolHub:
    """按配置装配工具集（native providers + 可选 MCP server）。"""
    from .mcp import register_mcp_tools
    from .minecraft import register_minecraft_tools
    from .native import register_native_tools
    from .shell import register_shell_tool

    hub = ToolHub(config)
    try:
        from ..conversation import conversation_log
        from ..guard import ToolGuard

        # 注意用 hub.config 而不是形参 config：生产入口是 build_hub()，形参为 None
        hub.guard = ToolGuard(hub.config, conversation_provider=conversation_log.recent)
        if hub.guard.enabled:
            logger.info(
                f"[Guard] 审查层已就绪（模型={hub.guard.model or '未配置'}，"
                f"受审工具={hub.config.get('guard_tools') or '仅显式标记'}，"
                f"失败策略={hub.guard.fail_mode}）"
            )
    except Exception as e:
        # 装配失败绝不等于"放行"：受审工具一律按保守策略拒绝
        logger.critical(f"[Tool] 审查层装配失败，受审工具将被拒绝执行: {e}")
        hub.guard_broken = True
    register_native_tools(hub)
    register_shell_tool(hub)
    register_minecraft_tools(hub)
    hub.mcp_clients = await register_mcp_tools(hub)
    if hub.tool_names():
        logger.info(f"[Tool] 已注册工具: {', '.join(hub.tool_names())}")
    else:
        logger.info("[Tool] 未注册任何工具（TOOLS_ENABLED 或各 provider 开关可能为 false）")
    return hub
