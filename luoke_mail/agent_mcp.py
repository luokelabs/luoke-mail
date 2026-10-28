# Copied from luoke-agent (luoke_agent/mcp.py, commit e4e4d97): the tool protocol's reference code, stdlib only.
# Change it there and copy it again; do not edit this copy.
"""工具协议（docs/工具协议.md）的参考实现：MCP 2026-07-28 数据层的一个子集，HTTP/1.1 跑在 Unix socket 上。

服务端 `ToolServer` 给工具用（整个文件可以拷进工具自己的仓库），客户端 `ToolClient` 给 agent 用。
只用标准库，Python 3.9 起。
"""
from __future__ import annotations

import http.client
import itertools
import json
import os
import socket
import socketserver
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional

PROTOCOL_VERSION = "2026-07-28"
AGENT_PROTOCOL = 1  # 本协议的版本，清单 capabilities.protocol

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"
USER_ACTION = "ai.luoke/user-action"  # 会改动东西的调用必带：用户在哪张卡片上点的（§5）

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
RESOURCE_NOT_FOUND = -32002

MAX_BODY = 1 << 20  # 一条消息最多 1 MB

# 等多久（§2、§5）：读 outbox、列工具 10 秒不答就当没在运行；只读的调用 30 秒；会改动东西的 5 分钟
CONNECT_TIMEOUT = 10.0
LIST_TIMEOUT = 10.0
READ_CALL_TIMEOUT = 30.0
WRITE_CALL_TIMEOUT = 300.0


def outbox_uri(accessory: str) -> str:
    return f"luoke://{accessory}/outbox"


# ---------------------------------------------------------------- 服务端


class ToolError(Exception):
    """工具执行不了：给用户看的一句话 + 原因码（not_ready、needs_setup、stale、failed……），可以带一张替换的卡片。"""

    def __init__(self, code: str, message: str, card: Optional[list] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.card = card


ToolFn = Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]


class ToolServer:
    """把几个资源和工具挂在一个 Unix socket 上。

    工具函数 `fn(arguments, meta) -> dict`：返回的 dict 必须有 `text`（给用户看的一句话），
    其余键（`card`、`done`……）原样放进 `structuredContent`。执行不了就抛 `ToolError`。
    资源函数 `fn() -> 可以 JSON 序列化的值`。
    """

    def __init__(self, name: str, version: str):
        self.name = name
        self.version = version
        self._tools: Dict[str, dict] = {}
        self._resources: Dict[str, dict] = {}
        self._server: Optional[_UnixServer] = None
        self._thread: Optional[threading.Thread] = None
        self._socket_path: Optional[str] = None
        self._socket_ino: Optional[int] = None

    # 注册

    def tool(self, name: str, fn: ToolFn, *, title: str, description: str = "",
             input_schema: Optional[dict] = None, read_only: bool = True, destructive: bool = False) -> None:
        if name.startswith("agent."):
            raise ValueError("agent. 开头的动作名留给 agent 自己")
        schema = input_schema or {"type": "object", "properties": {}}
        self._tools[name] = {
            "fn": fn,
            "read_only": read_only,
            "spec": {
                "name": name,
                "title": title,
                "description": description,
                "inputSchema": schema,
                "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive},
            },
        }

    def resource(self, uri: str, fn: Callable[[], Any], *, name: str, mime_type: str = "application/json") -> None:
        self._resources[uri] = {"fn": fn, "spec": {"uri": uri, "name": name, "mimeType": mime_type}}

    # 处理一条 JSON-RPC 消息（不经 socket 也能直接测）

    def handle(self, message: Any) -> Optional[dict]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "不是 JSON-RPC 2.0 请求")
        rid = message.get("id")
        if "id" not in message:
            return None  # 通知：这个子集里没有要处理的
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(rid, INVALID_PARAMS, "params 要是对象")
        meta = params.get("_meta") or {}
        if not isinstance(meta, dict):
            return _error(rid, INVALID_PARAMS, "_meta 要是对象")
        version = meta.get(META_VERSION)
        if version is not None and version != PROTOCOL_VERSION:
            return _error(rid, INVALID_PARAMS, "不支持这个协议版本", {"supportedVersions": [PROTOCOL_VERSION]})
        method = message["method"]
        try:
            if method == "server/discover":
                return _result(rid, self._discover())
            if method == "resources/list":
                return _result(rid, {"resources": [r["spec"] for r in self._resources.values()]})
            if method == "resources/read":
                return self._read(rid, params)
            if method == "tools/list":
                return _result(rid, {"tools": [t["spec"] for t in self._tools.values()]})
            if method == "tools/call":
                return self._call(rid, params, meta)
        except Exception:  # 工具自己的毛病：不把细节交出去，工具自己记日志
            return _error(rid, INTERNAL_ERROR, "工具内部出错")
        return _error(rid, METHOD_NOT_FOUND, f"没有这个方法：{method}")

    def _discover(self) -> dict:
        caps: Dict[str, Any] = {}
        if self._tools:
            caps["tools"] = {}
        if self._resources:
            caps["resources"] = {}
        return {
            "supportedVersions": [PROTOCOL_VERSION],
            "capabilities": caps,
            "_meta": {META_SERVER: {"name": self.name, "version": self.version}},
        }

    def _read(self, rid: Any, params: dict) -> dict:
        uri = params.get("uri")
        res = self._resources.get(uri) if isinstance(uri, str) else None
        if res is None:
            return _error(rid, RESOURCE_NOT_FOUND, "没有这个资源", {"uri": uri})
        value = res["fn"]()
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return _result(rid, {"contents": [{"uri": uri, "mimeType": res["spec"]["mimeType"], "text": text}]})

    def _call(self, rid: Any, params: dict, meta: dict) -> dict:
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _error(rid, INVALID_PARAMS, f"没有这个工具：{name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(rid, INVALID_PARAMS, "arguments 要是对象")
        if not tool["read_only"] and not _valid_user_action(meta.get(USER_ACTION)):
            return _result(rid, _tool_error("needs_user_action", "这个操作要你在卡片上点过才能执行。"))
        missing = [k for k in tool["spec"]["inputSchema"].get("required", []) if k not in args]
        if missing:
            return _result(rid, _tool_error("invalid", "缺少参数：" + "、".join(missing)))
        try:
            out = tool["fn"](args, meta)
        except ToolError as e:
            return _result(rid, _tool_error(e.code, e.message, e.card))
        if not isinstance(out, dict) or not isinstance(out.get("text"), str) or not out["text"]:
            raise TypeError("工具函数要返回带 text 的 dict")
        structured = {k: v for k, v in out.items() if k != "text"}
        result = {"content": [{"type": "text", "text": out["text"]}], "isError": False}
        if structured:
            result["structuredContent"] = structured
        return _result(rid, result)

    # socket

    def start(self, socket_path: str) -> "ToolServer":
        """在后台线程里开始服务。socket 所在的目录不存在就建成 700；旧的 socket 没人在听就删掉。"""
        _prepare_socket(socket_path)
        server = _UnixServer(socket_path, _Handler)
        os.chmod(socket_path, 0o600)
        server.tool_server = self
        self._server = server
        self._socket_path = socket_path
        self._socket_ino = os.lstat(socket_path).st_ino
        self._thread = threading.Thread(target=server.serve_forever, name=f"{self.name}-mcp", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        """停下并删掉自己的 socket 文件（§2：工具退出时删掉）。"""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        path, ino = self._socket_path, self._socket_ino
        try:
            if path and os.lstat(path).st_ino == ino:
                os.unlink(path)
        except FileNotFoundError:
            pass
        self._server = None


def _valid_user_action(v: Any) -> bool:
    return isinstance(v, dict) and all(isinstance(v.get(k), str) and v[k] for k in ("surfaceId", "componentId", "at"))


def _tool_error(code: str, message: str, card: Optional[list] = None) -> dict:
    structured: Dict[str, Any] = {"code": code}
    if card is not None:
        structured["card"] = card
    return {"content": [{"type": "text", "text": message}], "structuredContent": structured, "isError": True}


def _result(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": {"resultType": "complete", **result}}


def _error(rid: Any, code: int, message: str, data: Any = None) -> dict:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _prepare_socket(path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(d):
        os.makedirs(d, mode=0o700)
    if not os.path.lexists(path):
        return
    if not stat.S_ISSOCK(os.lstat(path).st_mode):
        raise RuntimeError(f"{path} 已经存在，而且不是 socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(path)
    except (ConnectionRefusedError, FileNotFoundError):
        os.unlink(path)  # 上次没删掉的，没人在听
        return
    except socket.timeout:
        raise RuntimeError(f"{path} 上已经有进程，但不答复") from None
    finally:
        probe.close()
    raise RuntimeError(f"{path} 上已经有进程在服务")


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    tool_server: ToolServer

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return  # agent 等不及先走了（例如超时），不算出错
        print(f"{self.tool_server.name}: 服务出错 {type(exc).__name__}", file=sys.stderr)  # 只记类型，不记内容


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "luoke-tool"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # 不记请求：里面可能有内容
        pass

    def address_string(self) -> str:
        return "unix"

    def do_POST(self) -> None:
        if self.path != "/mcp":
            return self._send(404, b"")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return self._send(400, b"")
        if length <= 0 or length > MAX_BODY:
            return self._send(413 if length > MAX_BODY else 400, b"")
        body = self.rfile.read(length)
        try:
            message = json.loads(body)
        except ValueError:
            reply: Optional[dict] = _error(None, PARSE_ERROR, "不是 JSON")
        else:
            reply = self.server.tool_server.handle(message)  # type: ignore[attr-defined]
        if reply is None:
            return self._send(202, b"")
        self._send(200, json.dumps(reply, ensure_ascii=False).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # 订阅（§6）还没做
        self._send(405, b"")

    def _send(self, status: int, body: bytes, ctype: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True


# ---------------------------------------------------------------- 客户端


class NotRunning(Exception):
    """工具现在没在运行：socket 不在、连不上、连上了不答。不算出错（§2）。"""


class TimedOut(Exception):
    """请求发出去了，但在时限内没等到答复：工具可能已经执行了，结果不明。"""


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"{code} {message}")
        self.code = code
        self.message = message
        self.data = data


class ToolResult:
    """tools/call 的结果：`text` 给用户看，`structured` 是 structuredContent，`is_error` 表示没执行成。"""

    def __init__(self, text: str, structured: dict, is_error: bool):
        self.text = text
        self.structured = structured
        self.is_error = is_error

    @property
    def code(self) -> Optional[str]:
        return self.structured.get("code")

    @property
    def card(self) -> Optional[list]:
        return self.structured.get("card")

    @property
    def done(self) -> bool:
        return bool(self.structured.get("done"))


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, connect_timeout: float, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path
        self._connect_timeout = connect_timeout

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self._connect_timeout)
        try:
            s.connect(self._path)
        except OSError:
            s.close()
            raise
        s.settimeout(self.timeout)
        self.sock = s


class ToolClient:
    """agent 连一个工具。每个请求一条连接（无状态，§2）。"""

    def __init__(self, socket_path: str, *, client_name: str = "luoke-agent", client_version: str = "0"):
        self.socket_path = socket_path
        self._client = {"name": client_name, "version": client_version}
        self._ids = itertools.count(1)

    def request(self, method: str, params: Optional[dict] = None, *, timeout: float = LIST_TIMEOUT,
                extra_meta: Optional[dict] = None) -> dict:
        meta = {META_VERSION: PROTOCOL_VERSION, META_CLIENT: self._client, META_CAPABILITIES: {}}
        if extra_meta:
            meta.update(extra_meta)
        body = json.dumps({"jsonrpc": "2.0", "id": next(self._ids), "method": method,
                           "params": {**(params or {}), "_meta": meta}}, ensure_ascii=False).encode("utf-8")
        conn = _UnixConnection(self.socket_path, CONNECT_TIMEOUT, timeout)
        try:
            try:
                conn.connect()
            except OSError as e:  # 不在、拒绝、连不上
                raise NotRunning(type(e).__name__) from None
            try:
                conn.request("POST", "/mcp", body, {"Content-Type": "application/json"})
                resp = conn.getresponse()
                raw = resp.read(MAX_BODY + 1)
            except socket.timeout:
                if method == "tools/call":
                    raise TimedOut(method) from None
                raise NotRunning("timeout") from None
            except (ConnectionError, http.client.HTTPException) as e:
                raise NotRunning(type(e).__name__) from None
        finally:
            conn.close()
        if resp.status != 200:
            raise RpcError(INTERNAL_ERROR, f"HTTP {resp.status}")
        if len(raw) > MAX_BODY:
            raise RpcError(INTERNAL_ERROR, "答复太长")
        try:
            reply = json.loads(raw)
        except ValueError:
            raise RpcError(PARSE_ERROR, "答复不是 JSON") from None
        if not isinstance(reply, dict):
            raise RpcError(INVALID_REQUEST, "答复不是对象")
        if "error" in reply:
            err = reply["error"] if isinstance(reply["error"], dict) else {}
            raise RpcError(int(err.get("code", INTERNAL_ERROR)), str(err.get("message", "")), err.get("data"))
        result = reply.get("result")
        if not isinstance(result, dict):
            raise RpcError(INVALID_REQUEST, "答复里没有 result")
        return result

    def discover(self) -> dict:
        return self.request("server/discover")

    def read_json(self, uri: str) -> Any:
        result = self.request("resources/read", {"uri": uri})
        contents = result.get("contents") or []
        if not contents or not isinstance(contents[0], dict) or not isinstance(contents[0].get("text"), str):
            raise RpcError(INVALID_REQUEST, "资源没有文字内容")
        try:
            return json.loads(contents[0]["text"])
        except ValueError:
            raise RpcError(PARSE_ERROR, "资源不是 JSON") from None

    def outbox(self, accessory: str) -> List[dict]:
        data = self.read_json(outbox_uri(accessory))
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise RpcError(INVALID_REQUEST, "outbox 里没有 items")
        return [i for i in items if isinstance(i, dict)]

    def list_tools(self) -> List[dict]:
        tools = self.request("tools/list").get("tools") or []
        return [t for t in tools if isinstance(t, dict)]

    def call_tool(self, name: str, arguments: Optional[dict] = None, *, user_action: Optional[dict] = None,
                  timeout: Optional[float] = None) -> ToolResult:
        """`user_action` 只在用户亲手点了按钮时给（§5）；给了就按会改动东西的调用等 5 分钟。"""
        if timeout is None:
            timeout = WRITE_CALL_TIMEOUT if user_action else READ_CALL_TIMEOUT
        extra = {USER_ACTION: user_action} if user_action else None
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}},
                              timeout=timeout, extra_meta=extra)
        text = ""
        for c in result.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                text = c["text"]
                break
        structured = result.get("structuredContent")
        return ToolResult(text, structured if isinstance(structured, dict) else {}, bool(result.get("isError")))
