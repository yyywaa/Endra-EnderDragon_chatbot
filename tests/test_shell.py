"""只读 shell 工具的加固测试。

重点不是"功能能不能跑"，而是**这些口子必须被堵住**：shell 元字符、路径逃逸、
敏感文件、危险 flag、凭据泄漏、越权命令。聊天消息是不可信输入，
任何一条漏掉都等于把 RCE / 密钥外泄的通道交给了一个会公开说话的角色。
"""
import asyncio
import os
import shutil
import unittest
from pathlib import Path

from connector.config import TOOLS_CONFIG
from connector.tools import shell as shell_mod
from connector.tools.hub import Tool, ToolHub, build_hub
from connector.tools.shell import ReadOnlyShell

ROOT = Path(__file__).resolve().parent / "fixtures" / "readonly_root"


def shell_config(**overrides) -> dict:
    base = {
        **TOOLS_CONFIG,
        "enabled": True,
        "shell_enabled": True,
        "shell_allow_files": False,
        "shell_root": str(ROOT),
        "shell_timeout": 5,
        "shell_max_output": 20000,
        "shell_timezone": "",
        "shell_per_minute": 3,
        "shell_per_day": 40,
    }
    base.update(overrides)
    return base


def make_shell(runner=None, **overrides) -> ReadOnlyShell:
    return ReadOnlyShell(shell_config(**overrides), runner=runner)


class TestCommandAllowlist(unittest.TestCase):
    def test_info_commands_allowed_without_file_access(self):
        sh = make_shell()
        for line in ("date -u", "uptime", "df -h", "free -m", "uname -a", "whoami", "id -u", "nproc"):
            argv, error = sh.validate(line)
            self.assertIsNone(error, f"{line} 应被放行: {error}")
            self.assertEqual(argv[0], line.split()[0])

    def test_file_commands_require_explicit_opt_in(self):
        sh = make_shell()
        _, error = sh.validate("ls -l")
        self.assertIsNotNone(error)
        self.assertIn("READONLY_SHELL_ALLOW_FILES", error)

        sh_files = make_shell(shell_allow_files=True)
        argv, error = sh_files.validate("ls -l")
        self.assertIsNone(error)
        self.assertEqual(argv, ["ls", "-l"])

    def test_arbitrary_commands_rejected(self):
        sh = make_shell(shell_allow_files=True)
        for line in ("rm -rf /", "bash -c whoami", "sh", "python -c 'print(1)'", "curl http://x",
                     "wget http://x", "nc -l 1234", "dd if=/dev/zero of=/tmp/x", "env", "printenv",
                     "chmod 777 notes.txt", "sudo ls", "kill 1"):
            _, error = sh.validate(line)
            self.assertIsNotNone(error, f"{line} 必须被拒绝")

    def test_shell_metacharacters_rejected(self):
        sh = make_shell(shell_allow_files=True)
        for line in ("ls; rm -rf /", "ls | wc -l", "cat notes.txt > out.txt", "ls && whoami",
                     "cat $(echo notes.txt)", "cat `echo notes.txt`", "ls ${HOME}", "ls\nwhoami"):
            _, error = sh.validate(line)
            self.assertIsNotNone(error, f"{line} 必须被拒绝")
            self.assertIn("元字符", error)

    def test_dangerous_flags_rejected(self):
        sh = make_shell(shell_allow_files=True)
        for line in ("find . -exec rm {} ;", "find . -delete", "find . -name x -execdir cat {} ;"):
            _, error = sh.validate(line)
            self.assertIsNotNone(error, f"{line} 必须被拒绝")

    def test_unlisted_flags_rejected(self):
        sh = make_shell(shell_allow_files=True)
        _, error = sh.validate("ls --color=always")
        self.assertIsNotNone(error)
        self.assertIn("不允许参数", error)

    def test_value_flags_must_be_simple(self):
        sh = make_shell(shell_allow_files=True)
        argv, error = sh.validate("head -n 20 notes.txt")
        self.assertIsNone(error)
        self.assertEqual(argv, ["head", "-n", "20", "notes.txt"])

        _, error = sh.validate("head -n $(id) notes.txt")
        self.assertIsNotNone(error)  # 元字符先被拦下

        _, error = sh.validate("head -n ../etc notes.txt")
        self.assertIsNotNone(error)

    def test_git_only_allows_safe_subcommands(self):
        sh = make_shell(shell_allow_files=True)
        self.assertIsNone(sh.validate("git status")[1])
        self.assertIsNone(sh.validate("git log --oneline")[1])
        for line in ("git show HEAD", "git push", "git checkout main", "git log --all"):
            _, error = sh.validate(line)
            self.assertIsNotNone(error, f"{line} 必须被拒绝")


class TestPathSandbox(unittest.TestCase):
    def test_paths_outside_root_rejected(self):
        sh = make_shell(shell_allow_files=True)
        for line in ("cat ../notes.txt", "cat /etc/passwd", "ls /", "cat ../../../../etc/hosts",
                     "ls /app", "cat ~/x"):
            _, error = sh.validate(line)
            self.assertIsNotNone(error, f"{line} 必须被拒绝")

    def test_sensitive_files_rejected_even_inside_root(self):
        sh = make_shell(shell_allow_files=True)
        for line in ("cat .env", "cat secrets/token.txt", "ls secrets", "cat ./secrets/token.txt",
                     "head -n 5 .env", "grep KEY .env"):
            _, error = sh.validate(line)
            self.assertIsNotNone(error, f"{line} 必须被拒绝")

    def test_proc_and_dev_rejected(self):
        sh = make_shell(shell_allow_files=True)
        for line in ("cat /proc/self/environ", "cat /proc/1/cmdline", "ls /dev"):
            _, error = sh.validate(line)
            self.assertIsNotNone(error, f"{line} 必须被拒绝")

    def test_deny_list_covers_key_material(self):
        for name in ("cookies.json", "id_rsa", ".env.local", "server.pem", "app.key", ".netrc"):
            self.assertTrue(
                any(p.search(name) for p in shell_mod._DENY_NAME_PATTERNS),
                f"{name} 应命中敏感文件名单",
            )

    def test_legitimate_paths_pass(self):
        sh = make_shell(shell_allow_files=True)
        for line in ("cat notes.txt", "ls", "ls -l .", "grep 笔记 notes.txt", "find . -name notes.txt"):
            _, error = sh.validate(line)
            self.assertIsNone(error, f"{line} 应被放行: {error}")


class TestExecution(unittest.TestCase):
    def test_no_shell_is_ever_used(self):
        """命令以 argv 直接执行，且 cwd/环境都不继承宿主。"""
        seen = {}

        def fake_runner(argv):
            seen["argv"] = argv
            return 0, "ok", ""

        sh = make_shell(runner=fake_runner)
        result = asyncio.run(sh.execute({"command": "date -u"}))
        self.assertIn("ok", result)
        self.assertEqual(seen["argv"], ["date", "-u"])

    def test_child_env_is_scrubbed_of_credentials(self):
        os.environ["LLM_API_KEY"] = "sk-secret-should-not-leak"
        os.environ["BOT_ACCESS_TOKEN"] = "OAT-secret"
        os.environ["COOKIE"] = "session=secret"
        try:
            env = make_shell()._child_env()
        finally:
            for key in ("LLM_API_KEY", "BOT_ACCESS_TOKEN", "COOKIE"):
                os.environ.pop(key, None)

        self.assertNotIn("LLM_API_KEY", env)
        self.assertNotIn("BOT_ACCESS_TOKEN", env)
        self.assertNotIn("COOKIE", env)
        self.assertEqual(set(env) - {"TZ"}, {"PATH", "LANG", "LC_ALL", "HOME"})

    def test_rejected_command_never_reaches_runner(self):
        calls = []

        def fake_runner(argv):
            calls.append(argv)
            return 0, "should not run", ""

        sh = make_shell(runner=fake_runner, shell_allow_files=True)
        result = asyncio.run(sh.execute({"command": "cat .env"}))
        self.assertEqual(calls, [])
        self.assertIn("没有执行", result)

    def test_output_cap(self):
        def fake_runner(argv):
            return 0, "x" * 500, ""

        sh = make_shell(runner=fake_runner, shell_max_output=100)
        result = asyncio.run(sh.execute({"command": "uptime"}))
        self.assertIn("已截断", result)
        self.assertLess(len(result), 300)

    def test_no_cap_when_disabled(self):
        def fake_runner(argv):
            return 0, "y" * 500, ""

        sh = make_shell(runner=fake_runner, shell_max_output=0)
        result = asyncio.run(sh.execute({"command": "uptime"}))
        self.assertNotIn("已截断", result)
        self.assertEqual(len(result), 500 + len("$ uptime\n"))

    def test_timeout_is_reported(self):
        import time

        def slow_runner(argv):
            time.sleep(2)
            return 0, "late", ""

        sh = make_shell(runner=slow_runner, shell_timeout=0.2)
        result = asyncio.run(sh.execute({"command": "uptime"}))
        self.assertIn("超时", result)

    def test_stderr_is_surfaced(self):
        def fake_runner(argv):
            return 1, "", "permission denied"

        sh = make_shell(runner=fake_runner)
        result = asyncio.run(sh.execute({"command": "uptime"}))
        self.assertIn("permission denied", result)

    def test_real_command_smoke(self):
        """真的执行一次系统信息命令（不经 fake runner）。"""
        if not shutil.which("date"):
            self.skipTest("环境里没有 date")
        result = asyncio.run(make_shell().execute({"command": "date -u"}))
        self.assertIn("$ date -u", result)
        self.assertNotIn("没有执行", result)


class TestRegistration(unittest.TestCase):
    def test_not_registered_when_disabled(self):
        async def scenario():
            hub = await build_hub(shell_config(shell_enabled=False))
            try:
                self.assertNotIn("readonly_shell", hub.tool_names())
            finally:
                await hub.aclose()

        asyncio.run(scenario())

    def test_registered_with_info_commands_only_by_default(self):
        async def scenario():
            hub = await build_hub(shell_config())
            try:
                self.assertIn("readonly_shell", hub.tool_names())
                tool = hub.get("readonly_shell")
                self.assertIsNotNone(tool)
                self.assertNotIn("ls", tool.description.split("白名单：")[1].split("；")[0])
                self.assertIn("date", tool.description)
            finally:
                await hub.aclose()

        asyncio.run(scenario())

    def test_file_mode_registered_when_enabled(self):
        async def scenario():
            hub = await build_hub(shell_config(shell_allow_files=True))
            try:
                self.assertIn("ls", hub.get("readonly_shell").description)
            finally:
                await hub.aclose()

        asyncio.run(scenario())

    def test_missing_root_degrades_to_info_only(self):
        async def scenario():
            hub = await build_hub(shell_config(shell_allow_files=True, shell_root="/nonexistent/readonly"))
            try:
                self.assertIn("readonly_shell", hub.tool_names())
                self.assertNotIn("ls ", hub.get("readonly_shell").description)
            finally:
                await hub.aclose()

        asyncio.run(scenario())

    def test_tool_is_reachable_through_hub_with_rate_limit(self):
        async def scenario():
            hub = await build_hub(shell_config(shell_per_minute=1, per_minute=0, per_day=0))
            try:
                first = await hub.call("readonly_shell", {"command": "date -u"})
                self.assertNotIn("没有执行", first)
                second = await hub.call("readonly_shell", {"command": "date -u"})
                self.assertIn("每分钟最多 1 次", second)
            finally:
                await hub.aclose()

        asyncio.run(scenario())


class TestToolDefinition(unittest.TestCase):
    def test_definition_is_minimal_and_readonly_named(self):
        tool = make_shell().tool()
        self.assertEqual(tool.name, "readonly_shell")
        self.assertEqual(tool.parameters["required"], ["command"])
        self.assertIn("只读", tool.description)
        self.assertEqual(tool.definition()["type"], "function")


if __name__ == "__main__":
    unittest.main()


class TestRateLimitSemantics(unittest.TestCase):
    """显式 0 必须真的表示"不限"，不能被 or 吞成默认值。"""

    def test_explicit_zero_means_unlimited(self):
        async def scenario():
            hub = await build_hub(shell_config(shell_per_minute=0, shell_per_day=0,
                                               per_minute=0, per_day=0, daily_total=0))
            try:
                for _ in range(6):
                    out = await hub.call("readonly_shell", {"command": "whoami"})
                    self.assertNotIn("超限", out)
                    self.assertNotIn("上限", out)
            finally:
                await hub.aclose()

        asyncio.run(scenario())

    def test_configured_limit_is_respected(self):
        async def scenario():
            hub = await build_hub(shell_config(shell_per_minute=2, per_minute=0, per_day=0, daily_total=0))
            try:
                self.assertNotIn("超限", await hub.call("readonly_shell", {"command": "whoami"}))
                self.assertNotIn("超限", await hub.call("readonly_shell", {"command": "whoami"}))
                self.assertIn("每分钟最多 2 次", await hub.call("readonly_shell", {"command": "whoami"}))
            finally:
                await hub.aclose()

        asyncio.run(scenario())

    def test_tool_without_own_limit_falls_back_to_global(self):
        hub = ToolHub(shell_config(per_minute=1, per_day=0, daily_total=0))
        hub.register(Tool(name="t", description="d", parameters={},
                          handler=lambda a: asyncio.sleep(0, result="ok")))
        self.assertEqual(asyncio.run(hub.call("t", {})), "ok")
        self.assertIn("每分钟最多 1 次", asyncio.run(hub.call("t", {})))
