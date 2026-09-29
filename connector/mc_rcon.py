"""极简 Source RCON 客户端（纯标准库，不引入依赖）。

协议（Source RCON）：
    包 = int32 长度 | int32 请求 id | int32 类型 | 正文 | \\x00 | \\x00
    类型：3=AUTH，2=EXECCOMMAND，0=RESPONSE_VALUE，2=AUTH_RESPONSE
认证：发 3 带密码；服务端回 id=-1 表示密码错，回 id=我方 id 表示成功。
命令：发 2 带命令；服务端可能拆成多个包，这里"先等首个包、再以短超时收尾"聚合。

只实现"连上→认证→跑一条命令→断开"，因为工具层只允许固定命令（list / kick），
绝不提供命令透传——RCON 等于服务器控制台，透传等于把 op/ban/stop 全交出去。
"""
import socket
import struct
from typing import List, Optional

AUTH = 3
EXECCOMMAND = 2
AUTH_RESPONSE = 2
RESPONSE_VALUE = 0


class RconError(Exception):
    """RCON 通信失败（连不上、超时、协议异常）。"""


class RconAuthError(RconError):
    """RCON 密码错误。"""


def _pack(request_id: int, packet_type: int, body: str) -> bytes:
    payload = body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<ii", request_id + 0, packet_type) + payload


def _send(sock: socket.socket, request_id: int, packet_type: int, body: str):
    payload = body.encode("utf-8") + b"\x00\x00"
    packet = struct.pack("<iii", 4 + 4 + len(payload), request_id, packet_type) + payload
    sock.sendall(packet)


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise RconError("连接被服务端关闭")
        buf += chunk
    return buf


def _recv_packet(sock: socket.socket) -> (int, int, str):
    header = _recv_exactly(sock, 4)
    (length,) = struct.unpack("<i", header)
    if length < 10 or length > 4096 * 16:
        raise RconError(f"响应长度异常: {length}")
    body = _recv_exactly(sock, length)
    request_id, packet_type = struct.unpack("<ii", body[:8])
    text = body[8:-2].decode("utf-8", errors="replace")
    return request_id, packet_type, text


class RconClient:
    def __init__(self, host: str, port: int, password: str, timeout: float = 5.0):
        self.host = host
        self.port = int(port)
        self.password = password or ""
        self.timeout = float(timeout)
        self._sock: Optional[socket.socket] = None
        self._request_id = 0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- 连接与认证 ----

    def connect(self):
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise RconError(f"连不上 RCON {self.host}:{self.port}（{e}）") from e
        self._sock.settimeout(self.timeout)
        self._authenticate()

    def _authenticate(self):
        self._request_id += 1
        request_id = self._request_id
        _send(self._sock, request_id, AUTH, self.password)

        while True:
            try:
                resp_id, _resp_type, _text = _recv_packet(self._sock)
            except socket.timeout as e:
                raise RconError("RCON 认证超时（服务器未响应）") from e
            if resp_id == -1:
                raise RconAuthError("RCON 密码错误")
            if resp_id == request_id:
                return

    # ---- 执行命令 ----

    def execute(self, command: str) -> str:
        """跑一条命令并返回聚合后的文本输出。"""
        if self._sock is None:
            raise RconError("尚未连接")
        self._request_id += 1
        request_id = self._request_id
        _send(self._sock, request_id, EXECCOMMAND, command)

        chunks: List[str] = []
        try:
            _resp_id, _resp_type, text = _recv_packet(self._sock)
            if text.strip():
                chunks.append(text)
        except socket.timeout as e:
            raise RconError(f"命令 `{command}` 响应超时") from e

        # 多包响应：首个包之后再以短超时收尾（RCON 没有明确的结束标记）
        self._sock.settimeout(min(0.35, self.timeout))
        try:
            while True:
                _resp_id, _resp_type, text = _recv_packet(self._sock)
                if text.strip():
                    chunks.append(text)
        except (socket.timeout, RconError):
            pass
        finally:
            self._sock.settimeout(self.timeout)

        return "\n".join(chunks).strip()

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None


def rcon_command(host: str, port: int, password: str, command: str, timeout: float = 5.0) -> str:
    """开一次连接、跑一条命令、断开。"""
    client = RconClient(host, port, password, timeout)
    try:
        client.connect()
        return client.execute(command)
    finally:
        client.close()
