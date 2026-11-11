# Copied from luoke-agent (luoke_agent/mcp.py, commit 15a9f75): the tool protocol's reference code, stdlib only.
# Change it there and copy it again; do not edit this copy.
# SPDX-License-Identifier: Apache-2.0 OR MIT
# Copyright 2026 螺壳实验室 Luoke Labs
"""工具协议（docs/工具协议.md）的参考实现：MCP 2026-07-28 数据层的一个子集，HTTP/1.1 跑在 Unix socket 上。

服务端 `ToolServer` 给工具用（整个文件可以拷进工具自己的仓库），客户端 `ToolClient` 给 agent 用。
只用标准库，Python 3.9 起。
"""
from __future__ import annotations

import base64
import http.client
import itertools
import json
import os
import socket
import socketserver
import stat
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional

PROTOCOL_VERSION = "2026-07-28"
AGENT_PROTOCOL = 1  # 本协议的版本，清单 capabilities.protocol

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"
# 告诉工具这个核心认 §4 后加的哪些卡片写法（工具协议 §4「后加的写法」）：工具看到了再用，旧的核心照原来的写法收
# 和 a2ui.A2UI_FEATURES 一样（测试核对）；这个文件给应用单独拷，不引 a2ui
CLIENT_CAPABILITIES = {"experimental": {"ai.luoke/a2ui": {"features": ["placement", "badge", "strong", "maxLines",
                                                                       "optionIcons", "collapsed", "lucide", "tooltip",
                                                                       "doneGroup", "itemIcons", "countLabel", "openPage",
                                                                       "subtitle", "countAlso", "countWeights",
                                                                       "pageArgs", "bigPages", "secureField", "pageRefresh",
                                                                       "saveFile", "neutralBadge", "chips", "pageSections"]}}}
USER_ACTION = "ai.luoke/user-action"  # 会改动东西的调用必带：用户在哪张卡片上点的（§5）

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
RESOURCE_NOT_FOUND = -32002
BAD_TOKEN = -32001          # 工具要口令，没带或不对（螺壳搜索的发布版，hostlink.py）

TOKEN_META = "ai.luoke/token"

MAX_BODY = 1 << 20  # 一条消息最多 1 MB

# 等多久（§2、§5）：读 outbox、列工具 10 秒不答就当没在运行；只读的调用 30 秒；会改动东西的 5 分钟
CONNECT_TIMEOUT = 10.0
LIST_TIMEOUT = 10.0
READ_CALL_TIMEOUT = 30.0
WRITE_CALL_TIMEOUT = 300.0


def outbox_uri(accessory: str) -> str:
    return f"luoke://{accessory}/outbox"


def features_of(meta: Any) -> frozenset:
    """请求的 `params._meta` 里核心认的 §4 后加的写法：`clientCapabilities.experimental["ai.luoke/a2ui"].features`
    （同 cards.host_features；这个文件给工具单独拷，不引 cards）。旧的核心不写，是空集合。"""
    caps = meta.get(META_CAPABILITIES) if isinstance(meta, dict) else None
    exp = caps.get("experimental") if isinstance(caps, dict) else None
    a2 = exp.get("ai.luoke/a2ui") if isinstance(exp, dict) else None
    feats = a2.get("features") if isinstance(a2, dict) else None
    return frozenset(f for f in feats if isinstance(f, str)) if isinstance(feats, list) else frozenset()


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
    资源函数 `fn() -> 可以 JSON 序列化的值`；登记时写了 `args=True` 的是 `fn(参数)`：带参数读的页
    （`page/<id>?<键>=<值>&…`，§4 pageArgs）把解开的参数（键 → 文字）交给它，没带是 `{}`。
    登记时写了 `features=True` 的再多收一个关键字参数 `features`：这次请求的核心认的 §4 后加的写法
    （`features_of(_meta)`，frozenset），页、画卡片的 outbox 照它决定用不用新写法。没写的照旧，不收。
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

    def resource(self, uri: str, fn: Callable[..., Any], *, name: str, mime_type: str = "application/json",
                 args: bool = False, features: bool = False) -> None:
        """`args=True`：读的时候可以带参数（`<uri>?<键>=<值>&…`），`fn` 收一个 dict。没写的照旧不收参数，带了也当没带。
        `features=True`：`fn` 再收关键字参数 `features`（这次请求的核心认的写法，§4「后加的写法」）。"""
        self._resources[uri] = {"fn": fn, "args": args, "features": features,
                                "spec": {"uri": uri, "name": name, "mimeType": mime_type}}

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
                return self._read(rid, params, meta)
            if method == "tools/list":
                return _result(rid, {"tools": [t["spec"] for t in self._tools.values()]})
            if method == "tools/call":
                return self._call(rid, params, meta)
        except Exception as e:  # 工具自己的毛病：细节不交给 agent，记进工具自己的 stderr（它的日志），不然就悄悄没了
            print(f"{self.name}: {self._which(method, params)} 出错 {type(e).__name__}（{_where(e)}）", file=sys.stderr)
            return _error(rid, INTERNAL_ERROR, "工具内部出错")
        return _error(rid, METHOD_NOT_FOUND, f"没有这个方法：{method}")

    def _which(self, method: str, params: dict) -> str:
        """出错的是哪个：方法，加上工具名或资源。只写这里登记过的名字，不写 agent 送来的任意字符串；
        带参数读的页只写页（`page/<id>`），不写参数（工具协议 §1a：参数里是用户填的字）"""
        key = params.get("name") if method == "tools/call" else params.get("uri") if method == "resources/read" else None
        if method == "resources/read" and isinstance(key, str):
            key = key.split("?", 1)[0]
        known = self._tools if method == "tools/call" else self._resources
        return f"{method} {key}" if isinstance(key, str) and key in known else method

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

    def _read(self, rid: Any, params: dict, meta: dict) -> dict:
        uri = params.get("uri")
        res = self._resources.get(uri) if isinstance(uri, str) else None
        query = ""
        if res is None and isinstance(uri, str) and "?" in uri:      # 带参数读（§4 pageArgs）
            base, query = uri.split("?", 1)
            res = self._resources.get(base)
        if res is None:
            return _error(rid, RESOURCE_NOT_FOUND, "没有这个资源", {"uri": uri})
        call: List[Any] = []
        if res["args"]:
            try:
                pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query))
            except ValueError:
                pairs = []
            call.append(dict(pairs))
        value = res["fn"](*call, features=features_of(meta)) if res["features"] else res["fn"](*call)
        if isinstance(value, (bytes, bytearray)):     # 图（缩略图）：base64 的 blob
            blob = base64.b64encode(bytes(value)).decode("ascii")
            return _result(rid, {"contents": [{"uri": uri, "mimeType": res["spec"]["mimeType"], "blob": blob}]})
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


def _where(e: BaseException, frames: int = 3) -> str:
    """出错的地方，最里面的在前：文件名:行 函数。和 handle_error 一样只记位置和类型，不记错误信息：信息里可能带着邮件、文件的内容"""
    tb = traceback.extract_tb(e.__traceback__)[-frames:]
    return " ← ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(tb)) or "位置不明"


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


class _Lines:
    """一行一行地读一条连接。`next()`：下一行（不含换行）；读到结尾、一个字节都没有是 None；
    超过 limit 还没有换行就把读到的交出去（调用方判太长）。一次读到的几行都留着，不丢。"""

    def __init__(self, conn: socket.socket, limit: int):
        self._conn = conn
        self._limit = limit
        self._buf = bytearray()
        self._eof = False

    def next(self) -> Optional[bytes]:
        while True:
            i = self._buf.find(b"\n")
            if i >= 0:
                line = bytes(self._buf[:i])
                del self._buf[:i + 1]
                return line
            if self._eof or len(self._buf) > self._limit:
                line, self._buf = bytes(self._buf), bytearray()
                return line or None
            chunk = self._conn.recv(65536)
            if not chunk:
                self._eof = True
            self._buf += chunk


def _is_notification(raw: bytes) -> bool:
    try:
        msg = json.loads(raw)
    except ValueError:
        return False
    return isinstance(msg, dict) and "id" not in msg and "method" in msg


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
    """agent 连一个工具。每个请求一条连接（无状态，§2）。

    两种连法：
    - 给 `socket_path`：自己连工具的 Unix socket，上面跑 HTTP/1.1（§2）；
    - 另给 `opener`：每个请求调它一次，拿一条已经接好的连接（螺壳本体起的工具进程的标准输入输出，`hostlink.py`），
      在上面写一行 JSON-RPC、读回一行就关（§2a）。这时 `socket_path` 只是个名字。
    """

    def __init__(self, socket_path: str, *, client_name: str = "luoke-agent", client_version: str = "0",
                 check: Optional[Callable[[str], bool]] = None, opener: Optional[Callable[[], socket.socket]] = None,
                 token: Optional[Callable[[bool], Optional[str]]] = None):
        self.socket_path = socket_path
        self._client = {"name": client_name, "version": client_version}
        self._check = check              # 每次连之前查一下 socket，不过就当没在运行
        self._opener = opener
        self._token = token              # 工具要的口令（hostlink.HostToken.get）：每个请求带在 _meta 里；参数 True 是再要一次
        self._ids = itertools.count(1)

    def request(self, method: str, params: Optional[dict] = None, *, timeout: float = LIST_TIMEOUT,
                extra_meta: Optional[dict] = None) -> dict:
        """工具答「口令不对」（它重起过，口令换了）：再要一次口令，换了就重发一次；还不对就当它不在。
        它在执行之前就拒了，重发不会做两遍。"""
        token = self._token(False) if self._token else None
        try:
            return self._request(method, params, timeout, extra_meta, token)
        except RpcError as e:
            if e.code != BAD_TOKEN or self._token is None:
                raise
        fresh = self._token(True)
        if fresh is None or fresh == token:
            raise NotRunning("口令不对")
        try:
            return self._request(method, params, timeout, extra_meta, fresh)
        except RpcError as e:
            if e.code == BAD_TOKEN:
                raise NotRunning("口令不对") from None
            raise

    def _request(self, method: str, params: Optional[dict], timeout: float, extra_meta: Optional[dict],
                 token: Optional[str]) -> dict:
        meta = {META_VERSION: PROTOCOL_VERSION, META_CLIENT: self._client, META_CAPABILITIES: CLIENT_CAPABILITIES}
        if extra_meta:
            meta.update(extra_meta)
        if token is not None:
            meta[TOKEN_META] = token
        body = json.dumps({"jsonrpc": "2.0", "id": next(self._ids), "method": method,
                           "params": {**(params or {}), "_meta": meta}}, ensure_ascii=False).encode("utf-8")
        raw = self._over_line(method, body, timeout) if self._opener else self._over_http(method, body, timeout)
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

    def _over_http(self, method: str, body: bytes, timeout: float) -> bytes:
        if self._check is not None and not self._check(self.socket_path):
            raise NotRunning("socket")
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
        return raw

    def _over_line(self, method: str, body: bytes, timeout: float) -> bytes:
        """§2a：拿一条接好的连接，写一行请求、关掉写的一头（工具读到结尾就知道只有这一条），读回一行。
        中途收到的通知（没有 id 的消息）跳过。"""
        try:
            conn = self._opener()     # 给不了（工具没带、没开、本体不在）就是 NotRunning
        except OSError as e:
            raise NotRunning(type(e).__name__) from None
        try:
            conn.settimeout(timeout)
            try:
                conn.sendall(body + b"\n")
                conn.shutdown(socket.SHUT_WR)
                lines = _Lines(conn, MAX_BODY)
                while True:
                    raw = lines.next()
                    if raw is None:
                        raise NotRunning("没有答复")   # 工具没答就退了
                    if not _is_notification(raw):
                        return raw
            except socket.timeout:
                if method == "tools/call":
                    raise TimedOut(method) from None
                raise NotRunning("timeout") from None
            except OSError as e:
                raise NotRunning(type(e).__name__) from None
        finally:
            conn.close()

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

    def read_blob(self, uri: str, *, timeout: float = 10.0) -> bytes:
        """读一个答 `blob`（base64）的资源，例如搜索结果的缩略图 `thumb/<id>`（工具协议 §1b）。"""
        result = self.request("resources/read", {"uri": uri}, timeout=timeout)
        contents = result.get("contents") or []
        blob = contents[0].get("blob") if contents and isinstance(contents[0], dict) else None
        if not isinstance(blob, str):
            raise RpcError(INVALID_REQUEST, "资源没有 blob")
        try:
            return base64.b64decode(blob, validate=True)
        except ValueError:
            raise RpcError(PARSE_ERROR, "blob 不是 base64") from None

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
