# Copied from luoke-agent (luoke_agent/a2ui.py, commit 15a9f75): the tool protocol's reference code, stdlib only.
# Change it there and copy it again; do not edit this copy.
# SPDX-License-Identifier: Apache-2.0 OR MIT
# Copyright 2026 螺壳实验室 Luoke Labs
"""A2UI v0.9.1 卡片：按组件目录校验、应用更新、解析路径绑定（docs/工具协议.md §4）。

agent 收到卡片先过这里；工具的测试也可以用 `validate()` 查自己出的卡片。
和 Swift 的 LuokeA2UICore 是同一套规矩。只用标准库，Python 3.9 起。
"""
from __future__ import annotations

import copy
import re
import unicodedata
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

VERSION = "v0.9"
AGENT_CATALOG = "ai.luoke/a2ui/agent/1"
IME_CATALOG = "ai.luoke/a2ui/ime/1"   # 输入法的目录：公开发布后它接 LuokeA2UI 之前，以 luoke-ime 的 Manifest.swift 为准
CATALOGS = {
    AGENT_CATALOG: frozenset({"Text", "Row", "Column", "List", "Card", "Divider", "Button", "CheckBox",
                              "ChoicePicker", "TextField", "DateTimeInput", "Icon", "ProgressBar"}),
    IME_CATALOG: frozenset({"Text", "CheckBox", "ChoicePicker", "Slider", "TextField", "Button", "Divider",
                            "Row", "Column"}),
}
MESSAGE_KINDS = ("createSurface", "updateComponents", "updateDataModel", "deleteSurface")
MAX_COMPONENTS = 200
MAX_PAGE_COMPONENTS = 400        # 应用页（§1a page/<id>）的上限，核心认 bigPages（§4，2026-11-10）
MAX_TEXT = 2000
# agent 自己处理的动作（§5），工具的动作名不能以 agent. 开头
AGENT_EVENTS = frozenset({"agent.open_url", "agent.open_page", "agent.dismiss", "agent.remind"})  # remind 只用在 agent 自己的卡片上
# agent.open_page 的页 id：同清单里 service.pages 的 id（apps.ID），不带应用名，开的只能是出这张卡片的应用自己的页
PAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# agent.open_page 的 args（§4 pageArgs）：最多 4 个键，键是小写字母开头的短名字，值不超过 200 字
PAGE_ARG_KEY = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
MAX_PAGE_ARGS = 4
MAX_PAGE_ARG = 200
# 页的 root 上的 refresh（§4 pageRefresh）：几秒以后再读，2 到 60
REFRESH_MIN, REFRESH_MAX = 2, 60
# 答复里的 file（§4 saveFile）：只认文字，UTF-8 不超过 512 KB（一条消息最多 1 MB）；文件名不超过 100 字
MAX_FILE_BYTES = 512 * 1024
MAX_FILE_NAME = 100
# 能存的几种文字文件和各自的扩展名（文件名要以它结尾，不分大小写）：别的类型一律不认，所以存不出 .command、.terminal 这类能执行的
FILE_TYPES = {"text/plain": ".txt", "text/csv": ".csv", "application/json": ".json", "text/markdown": ".md",
              "application/xml": ".xml"}
# Icon 能用的名字（§4「图标」）：Lucide 的名字（窗口带着这些图），和以前的名字（照旧认，画成对应的 Lucide 图标）
LUCIDE_ICONS = frozenset({"app-window", "archive", "brain", "calendar", "calendar-plus", "check", "chevron-down",
                          "chevron-right", "circle-help", "circle-minus", "clock", "copy", "cpu", "eye", "file",
                          "folder", "folder-open", "globe", "hard-drive", "inbox", "info", "key", "keyboard",
                          "lightbulb", "link", "lock", "mail", "package", "pencil-line", "power", "puzzle",
                          "refresh-cw", "reply", "settings", "shield-alert", "shield-check", "sparkles",
                          "square-arrow-out-up-right", "star", "trash-2", "trending-up", "triangle-alert",
                          "user", "x"})
ICON_ALIASES = {"warning": "triangle-alert", "close": "x", "trash": "trash-2", "person": "user", "memory": "brain",
                "question": "circle-help", "open": "square-arrow-out-up-right"}
ICONS = LUCIDE_ICONS | frozenset(ICON_ALIASES)
TEXT_VARIANTS = frozenset({"h1", "h2", "h3", "h4", "h5", "body", "caption", "strong", "badge"})
# 标签（variant badge）的颜色：不写是浅蓝（邮箱地址这类），danger 红（已过期），warning 橙（快到期），neutral 米色（大小这类不分好坏的）
TONES = frozenset({"danger", "warning", "neutral"})
# 这个核心认的 §4 后加的写法，放在每个请求的 `clientCapabilities.experimental["ai.luoke/a2ui"].features`，工具据此决定用不用
A2UI_FEATURES = ("placement", "badge", "strong", "maxLines", "optionIcons", "collapsed", "lucide", "tooltip",
                 "doneGroup", "itemIcons", "countLabel", "openPage", "subtitle", "countAlso",
                 "countWeights", "pageArgs", "bigPages", "secureField", "pageRefresh", "saveFile",
                 "neutralBadge", "chips", "pageSections")
# 应用页里一段的位置（§4「宽窗口的侧栏」）：只有 aside，写在根 Column 下面一层的 Column 上
PLACEMENTS = frozenset({"aside"})
# 应用页怎么排（§4 pageSections）：只有 sections（下面一层一段一张卡片），写在页的根 Column 上
LAYOUTS = frozenset({"sections"})


class CardError(ValueError):
    def __init__(self, problems: List[str]):
        super().__init__("；".join(problems))
        self.problems = problems


# ---------------------------------------------------------------- JSON 指针


def _tokens(path: str) -> List[str]:
    if path == "/" or path == "":
        return []
    if not path.startswith("/"):
        raise KeyError(path)
    return [t.replace("~1", "/").replace("~0", "~") for t in path[1:].split("/")]


_INDEX = re.compile(r"0|[1-9][0-9]*")


def _index(t: str, items: list) -> Optional[int]:
    """数组下标照 RFC 6901：`0`，或者不以 0 开头的 ASCII 数字，还要在数组里。别的写法（`00`、`+0`、`-0`、全角或别的文字的数字）
    取不到：不然 `/l/00` 取到的是 `/l/0`，密码框的路径按段比（`_covers`）就拦不住它。Swift 的 `pointerIndex` 同一个规矩。
    位数比数组长度的位数多的一定越界，不去换成整数：Python 3.11 起 `int()` 遇到 4300 位以上的数字串抛 ValueError，
    那样取值、写值和整页校验都会漏出异常，而不是当作取不到。"""
    if not _INDEX.fullmatch(t) or len(t) > len(str(len(items))):
        return None
    i = int(t)
    return i if i < len(items) else None


def get_path(data: Any, path: str) -> Any:
    """按 JSON 指针取值，取不到抛 KeyError。"""
    cur = data
    for t in _tokens(path):
        if isinstance(cur, dict) and t in cur:
            cur = cur[t]
        elif isinstance(cur, list) and (i := _index(t, cur)) is not None:
            cur = cur[i]
        else:
            raise KeyError(path)
    return cur


def set_path(data: dict, path: str, value: Any, remove: bool = False) -> dict:
    """返回改过的数据模型（`path` 是 `/` 时整份换掉）。中间缺的对象自动补上。"""
    toks = _tokens(path)
    if not toks:
        if remove:
            return {}
        if not isinstance(value, dict):
            raise KeyError("整份数据模型要是对象")
        return value
    cur: Any = data
    for t in toks[:-1]:
        if isinstance(cur, list) and (i := _index(t, cur)) is not None:
            cur = cur[i]
        elif isinstance(cur, dict):
            cur = cur.setdefault(t, {})
        else:
            raise KeyError(path)
    last = toks[-1]
    if isinstance(cur, list) and (i := _index(last, cur)) is not None:
        if remove:
            del cur[i]
        else:
            cur[i] = value
    elif isinstance(cur, dict):
        if remove:
            cur.pop(last, None)
        else:
            cur[last] = value
    else:
        raise KeyError(path)
    return data


_MISSING = object()


def merge_context(declared: Any, client: Any, data: dict) -> Any:
    """点按时的上下文：写死的值只认卡片上声明的，绑定的值用界面送来的（界面上用户改过的数据模型在它那边），
    界面没送就用这边的数据模型。这样界面改不了方案编号这类写死的值。"""
    if is_binding(declared):
        return copy.deepcopy(client) if client is not _MISSING else resolve(declared, data)
    if isinstance(declared, dict):
        c = client if isinstance(client, dict) else {}
        return {k: merge_context(v, c.get(k, _MISSING), data) for k, v in declared.items()}
    if isinstance(declared, list):
        c = client if isinstance(client, list) and len(client) == len(declared) else [_MISSING] * len(declared)
        return [merge_context(v, cv, data) for v, cv in zip(declared, c)]
    return copy.deepcopy(declared)


def is_binding(v: Any) -> bool:
    return isinstance(v, dict) and set(v) == {"path"} and isinstance(v["path"], str)


def bindings(value: Any) -> List[str]:
    """一个上下文里所有绑定的路径（`{"path": …}`），按出现的顺序。"""
    if is_binding(value):
        return [value["path"]]
    if isinstance(value, dict):
        return [p for v in value.values() for p in bindings(v)]
    if isinstance(value, list):
        return [p for v in value for p in bindings(v)]
    return []


def _secure_problems(cid: str, c: dict, secure: List[str]) -> List[str]:
    """一个组件碰没碰密码框（§4 secureField）的路径：
    - 交给工具的按钮，上下文只能正好引用密码框的路径，不能引用它上面一层（`/fields`、`/`：会连授权码整个交出去）；
    - `agent.` 开头的动作（`agent.open_page` 的 `args` 会进读页的查询串）不能引用密码框的路径，也不能引用它上面、下面一层；
    - 别的绑定（`Text` 的字、普通输入框的值、各种 `label`、选择的值、按钮的 `count`……）不能是密码框的路径或它上面一层，
      不然会明文显示出来。几个密码框绑同一个路径不算。"""
    out: List[str] = []
    ctype = c.get("component")
    name = _event_name(c) if ctype == "Button" else None
    if isinstance(name, str):
        for b in bindings(c["action"]["event"].get("context")):
            for p in secure:
                if name.startswith("agent."):
                    if _covers(b, p) or _covers(p, b):
                        out.append(f"按钮 {cid}：{name} 不能引用密码框的路径 {p}")
                elif b != p and _covers(b, p):
                    out.append(f"按钮 {cid}：上下文引用的 {b} 里有密码框（{p}），只能正好引用 {p}")
    rest = {k: v for k, v in c.items()
            if k != "action" and not (k == "value" and ctype == "TextField" and c.get("variant") == "obscured")}
    shown = bindings(rest)
    count = c.get("count") if ctype == "Button" else None
    if isinstance(count, dict) and isinstance(count.get("path"), str):
        shown.append(count["path"])
    for b in shown:
        for p in secure:
            if _covers(b, p):
                out.append(f"组件 {cid}：绑定的 {b} 会把密码框（{p}）里的字显示出来")
    return out


def _covers(outer: str, inner: str) -> bool:
    """`outer` 取到的值里有没有 `inner`：同一个路径，或者是它上面的一层（JSON 指针按段比）。"""
    try:
        a, b = _tokens(outer), _tokens(inner)
    except KeyError:
        return False
    return b[:len(a)] == a


# ---------------------------------------------------------------- agent.open_url 的网址

_LABEL = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
_NUMERIC = re.compile(r"^(?:[0-9]+|0x[0-9a-f]*)$")
_IPV4 = re.compile(r"^(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(?:\.(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])){3}$")
_PORT = re.compile(r"^[0-9]{1,5}$")
_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zs", "Zl", "Zp"})   # 控制字符、看不见的格式字符、落单的代理项、空白


def open_url_site(url: Any) -> Optional[Tuple[str, str]]:
    """`agent.open_url` 的网址 → (网站, 打开时交出去的网址)；认不了的是 None（工具协议 §5）。

    给用户看的网站和打开的网址出自这一次解析：主机名转成 punycode（ASCII），网址照解析的结果重新拼
    （主机用 ASCII 的那份，路径里的非 ASCII 字符编码掉）。不认：不是 https、反斜杠、空白和控制字符、
    userinfo（@）、空主机、主机里有 %、[、]，以及看着像数字却不是规范的 IPv4。Swift 的 LuokeA2UICore 照同样的规矩查
    （它不转 punycode，网站一律用 agent 写在按钮上的 `site`）。
    """
    if not isinstance(url, str) or len(url) > MAX_TEXT or not url.startswith("https://") or "\\" in url:
        return None
    if any(unicodedata.category(ch) in _BAD_CATEGORIES for ch in url):
        return None
    rest = url[len("https://"):]
    end = min([i for i in (rest.find(ch) for ch in "/?#") if i >= 0] + [len(rest)])
    authority, tail = rest[:end], rest[end:]
    if "@" in authority:
        return None
    host, port = authority, ""
    if ":" in authority:
        host, _, port = authority.rpartition(":")
        if not _PORT.match(port) or not 0 < int(port) < 65536:
            return None
        port = "" if int(port) == 443 else str(int(port))
    if not host or any(ch in host for ch in "%[]"):
        return None
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    labels = ascii_host.split(".")
    if not all(_LABEL.match(label) for label in labels):
        return None
    if _NUMERIC.match(labels[-1]) and not _IPV4.match(ascii_host):
        return None
    site = ascii_host + (f":{port}" if port else "")
    return site, "https://" + site + urllib.parse.quote(tail, safe="/?#[]@!$&'()*+,;=:%~")


def page_args_problem(args: Any) -> Optional[str]:
    """`agent.open_page` 取好值的 `args`、带参数读页的参数（§4 pageArgs）对不对：对是 None，不对是一句为什么。"""
    if not isinstance(args, dict):
        return "args 要是对象"
    if len(args) > MAX_PAGE_ARGS:
        return f"args 最多 {MAX_PAGE_ARGS} 个"
    for k, v in args.items():
        if not isinstance(k, str) or not PAGE_ARG_KEY.fullmatch(k):
            return "args 的键要是小写字母开头的短名字"
        if not isinstance(v, str):
            return f"args 的 {k} 要是文字"
        if len(v) > MAX_PAGE_ARG:
            return f"args 的 {k} 超过 {MAX_PAGE_ARG} 字"
    return None


def page_uri(page: str, args: Optional[Dict[str, str]] = None) -> str:
    """读这一页的资源：`page/<id>`，带参数时 `page/<id>?<键>=<值>&…`（键按字母顺序，值按 RFC 3986 百分号编码）。"""
    if not args:
        return f"page/{page}"
    return f"page/{page}?" + "&".join(f"{k}={urllib.parse.quote(args[k], safe='')}" for k in sorted(args))


def save_file(value: Any) -> Optional[dict]:
    """答复里的 `file`（§4 saveFile）：形状对就是 `{"name", "mediaType", "text"}`，不对是 None。
    `mediaType` 只认 FILE_TYPES 里的几种，文件名要以对应的扩展名结尾。"""
    if not isinstance(value, dict):
        return None
    name, media, text = value.get("name"), value.get("mediaType"), value.get("text")
    if not isinstance(name, str) or not 0 < len(name) <= MAX_FILE_NAME or name != name.strip() or name.startswith("."):
        return None
    if any(ch in "/:\\" or unicodedata.category(ch) in _BAD_CATEGORIES - {"Zs"} for ch in name):
        return None
    ext = FILE_TYPES.get(media) if isinstance(media, str) else None
    if ext is None or not name.lower().endswith(ext) or len(name) == len(ext):
        return None              # 类型不在名单里，或文件名不是它的扩展名
    if not isinstance(text, str):
        return None
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:       # 落单的代理项
        return None
    if size > MAX_FILE_BYTES:
        return None
    return {"name": name, "mediaType": media, "text": text}


def resolve(value: Any, data: dict) -> Any:
    """把按钮上下文里的 `{"path": …}` 换成数据模型里当时的值（取不到抛 KeyError）。"""
    if is_binding(value):
        return copy.deepcopy(get_path(data, value["path"]))
    if isinstance(value, dict):
        return {k: resolve(v, data) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, data) for v in value]
    return value


# ---------------------------------------------------------------- 一张卡片


class Surface:
    """一张卡片当前的样子：组件（按编号）和数据模型。按顺序 `apply` 消息，`check` 查整张。"""

    def __init__(self, surface_id: str, catalog_id: str, *, page: bool = False):
        """`page`：应用页（§1a page/<id>），不是卡片：上限 400 个组件，能写 refresh、密码框（§4，2026-11-10）。"""
        if catalog_id not in CATALOGS:
            raise CardError([f"不认识的组件目录：{catalog_id}"])
        self.surface_id = surface_id
        self.catalog_id = catalog_id
        self.page = page
        self.components: Dict[str, dict] = {}
        self.data: dict = {}
        self.deleted = False

    def apply(self, message: Any) -> None:
        kind, body = _unwrap(message)
        if body.get("surfaceId") != self.surface_id:
            raise CardError([f"{kind} 的 surfaceId 不是 {self.surface_id}"])
        if kind == "createSurface":
            raise CardError(["这张卡片已经建过了"])
        if kind == "deleteSurface":
            self.deleted = True
        elif kind == "updateComponents":
            comps = body.get("components")
            if not isinstance(comps, list) or not comps:
                raise CardError(["updateComponents 的 components 要是非空数组"])
            new = dict(self.components)
            for c in comps:
                if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not c["id"]:
                    raise CardError(["组件要是带 id 的对象"])
                new[c["id"]] = c
            self.components = new
        else:  # updateDataModel
            path = body.get("path", "/")
            if not isinstance(path, str):
                raise CardError(["updateDataModel 的 path 要是字符串"])
            try:
                self.data = set_path(copy.deepcopy(self.data), path, copy.deepcopy(body.get("value")),
                                     remove="value" not in body)
            except KeyError as e:
                raise CardError([f"updateDataModel 写不进 {e.args[0]}"]) from None

    def check(self) -> None:
        """整张卡片画得了吗：组件在目录里、属性类型对、引用和环、绑定的路径在、上限。不行抛 CardError。"""
        problems: List[str] = []
        if self.deleted:
            return
        if "root" not in self.components:
            raise CardError(["没有 id 为 root 的组件"])
        # 只查从 root 走得到的：更新换掉 root 以后，旧的组件还留着但不再画（A2UI 的更新是按编号增补）
        shown = _reachable(self.components)
        limit = MAX_PAGE_COMPONENTS if self.page else MAX_COMPONENTS
        if len(shown) > limit:
            problems.append(f"组件超过 {limit} 个")
        if len(self.components) > 5 * limit:
            problems.append("这张卡片累积的组件太多了，重新建一张")
        allowed = CATALOGS[self.catalog_id]
        parents: Dict[str, str] = {}
        for cid in shown:
            c = self.components[cid]
            ctype = c.get("component")
            if ctype not in allowed:
                problems.append(f"组件 {cid} 的类型 {ctype} 不在目录 {self.catalog_id} 里，可用的有：" + "、".join(sorted(allowed)))
                continue
            for child in _child_ids(c, problems, cid):
                if child not in self.components:
                    problems.append(f"组件 {cid} 引用的 {child} 不存在")
                elif child == "root":
                    problems.append("root 不能是别的组件的子组件")
                elif child in parents:
                    problems.append(f"组件 {child} 有两个父组件（{parents[child]}、{cid}）")
                else:
                    parents[child] = cid
            _check_props(cid, ctype, c, self.data, problems, self.page)
            if ctype == "Button" and _event_name(c) == "agent.open_url":
                label = self.components.get(c["child"]) if isinstance(c.get("child"), str) else None
                if not _literal_label(label, self.components):
                    problems.append(f"按钮 {cid}：agent.open_url 按钮的子组件要是写死文字的 Text，或一行「图标 + 写死文字的 Text」")
        root_is_column = self.components["root"].get("component") == "Column"
        for cid in shown:
            if "group" in self.components[cid]:
                _check_group(cid, self.components[cid], problems)
            if "subtitle" in self.components[cid]:
                _check_subtitle(cid, self.components[cid], problems)
            if "placement" in self.components[cid]:
                _check_placement(cid, self.components[cid], parents.get(cid), root_is_column, problems)
            if "refresh" in self.components[cid]:
                _check_refresh(cid, self.components[cid], self.page, problems)
            if "layout" in self.components[cid]:
                _check_layout(cid, self.components[cid], self.page, problems)
        secure = self.secure_paths() if self.page else []
        if secure:      # 密码框的路径别处不能碰（§4 secureField）
            for cid in shown:
                for p in _secure_problems(cid, self.components[cid], secure):
                    if p not in problems:
                        problems.append(p)
        problems.extend(_cycles({cid: self.components[cid] for cid in shown}))
        _check_text_lengths(self.data, "数据模型", problems)
        if problems:
            raise CardError(problems)

    def messages(self) -> List[dict]:
        """整张卡片现在的样子，作为一组新建消息（给界面用：先删后建，或第一次建）。"""
        out = [{"version": VERSION, "createSurface": {"surfaceId": self.surface_id, "catalogId": self.catalog_id}}]
        if self.components:
            out.append({"version": VERSION, "updateComponents": {"surfaceId": self.surface_id,
                                                                  "components": list(self.components.values())}})
        if self.data:
            out.append({"version": VERSION, "updateDataModel": {"surfaceId": self.surface_id, "path": "/",
                                                                 "value": copy.deepcopy(self.data)}})
        return out

    def secure_paths(self) -> List[str]:
        """画着的密码框（`TextField` 的 `"variant": "obscured"`，§4 secureField）绑定的路径。"""
        out = []
        for cid in _reachable(self.components):
            c = self.components[cid]
            if c.get("component") == "TextField" and c.get("variant") == "obscured" and is_binding(c.get("value")):
                out.append(c["value"]["path"])
        return out

    def button_event(self, component_id: str) -> Tuple[str, dict]:
        """现在画着的按钮声明的动作：（动作名，没解析的上下文）。"""
        if component_id not in _reachable(self.components):
            raise CardError([f"{component_id} 不在这张卡片上"])
        c = self.components[component_id]
        if c.get("component") != "Button":
            raise CardError([f"{component_id} 不是按钮"])
        event = c["action"]["event"]
        return event["name"], copy.deepcopy(event.get("context") or {})

    def find_action(self, component_id: str) -> Tuple[str, dict]:
        """用户点了哪个按钮：返回（动作名，解析好的上下文）。"""
        c = self.components.get(component_id)
        if not c or c.get("component") != "Button":
            raise CardError([f"{component_id} 不是这张卡片上的按钮"])
        event = c["action"]["event"]
        try:
            context = resolve(event.get("context") or {}, self.data)
        except KeyError as e:
            raise CardError([f"按钮 {component_id} 的上下文取不到 {e.args[0]}"]) from None
        return event["name"], context


def build(messages: Any, catalog_id: str = AGENT_CATALOG, *, page: bool = False) -> Surface:
    """一组新卡片的消息（第一条是 createSurface）→ 查过的 Surface。不行抛 CardError。
    `page`：这是应用页（§1a），不是卡片：照页的规矩查（§4 bigPages、pageRefresh、secureField）。"""
    if not isinstance(messages, list) or not messages:
        raise CardError(["卡片要是非空的消息数组"])
    kind, body = _unwrap(messages[0])
    if kind != "createSurface":
        raise CardError(["第一条消息要是 createSurface"])
    sid = body.get("surfaceId")
    if not isinstance(sid, str) or not sid:
        raise CardError(["createSurface 要有 surfaceId"])
    if body.get("catalogId") != catalog_id:
        raise CardError([f"catalogId 要是 {catalog_id}"])
    surface = Surface(sid, catalog_id, page=page)
    for m in messages[1:]:
        surface.apply(m)
    surface.check()
    return surface


def update(surface: Surface, messages: Any) -> Surface:
    """在一张已有的卡片上应用更新，查过再换上；不行抛 CardError，原卡片不变。"""
    if not isinstance(messages, list) or not messages:
        raise CardError(["更新要是非空的消息数组"])
    new = copy.deepcopy(surface)
    for m in messages:
        new.apply(m)
    new.check()
    return new


def validate(messages: Any, catalog_id: str = AGENT_CATALOG, *, page: bool = False) -> List[str]:
    """给工具的测试用：没问题返回 []，否则返回问题列表。查应用页（§1a page/<id>）时给 `page=True`。"""
    try:
        build(messages, catalog_id, page=page)
    except CardError as e:
        return e.problems
    return []


# ---------------------------------------------------------------- 细节


def _unwrap(message: Any) -> Tuple[str, dict]:
    if not isinstance(message, dict):
        raise CardError(["消息要是对象"])
    if message.get("version") != VERSION:
        raise CardError([f"消息的 version 要是 {VERSION}"])
    kinds = [k for k in MESSAGE_KINDS if k in message]
    if len(kinds) != 1 or not isinstance(message[kinds[0]], dict):
        raise CardError(["每条消息恰好是 createSurface、updateComponents、updateDataModel、deleteSurface 之一"])
    return kinds[0], message[kinds[0]]


def _child_ids(c: dict, problems: List[str], cid: str) -> List[str]:
    ctype = c.get("component")
    if ctype in ("Row", "Column", "List"):
        children = c.get("children")
        if isinstance(children, dict):
            problems.append(f"组件 {cid}：children 不支持模板，写成编号数组")
            return []
        if not isinstance(children, list) or not all(isinstance(x, str) for x in children):
            problems.append(f"组件 {cid}：children 要是编号数组")
            return []
        return list(children)
    if ctype in ("Card", "Button"):
        child = c.get("child")
        if not isinstance(child, str):
            problems.append(f"组件 {cid}：child 要是一个编号")
            return []
        return [child]
    return []


def _event_name(c: dict) -> Optional[str]:
    action = c.get("action")
    event = action.get("event") if isinstance(action, dict) else None
    return event.get("name") if isinstance(event, dict) else None


def _reachable(components: Dict[str, dict]) -> List[str]:
    """从 root 走得到的组件编号，按走到的顺序；遇到环不会死循环。"""
    seen: List[str] = []
    marked = set()
    stack = ["root"]
    while stack:
        cid = stack.pop()
        if cid in marked or cid not in components:
            continue
        marked.add(cid)
        seen.append(cid)
        stack.extend(reversed(_child_ids(components[cid], [], cid)))
    return seen


def _cycles(components: Dict[str, dict]) -> List[str]:
    found: List[str] = []
    state: Dict[str, int] = {}  # 1 正在走，2 走完

    def walk(cid: str) -> None:
        state[cid] = 1
        for child in _child_ids(components[cid], [], cid):
            if child not in components:
                continue
            if state.get(child) == 1:
                found.append(f"组件 {cid} 和 {child} 之间有环")
            elif state.get(child) is None:
                walk(child)
        state[cid] = 2

    for cid in components:
        if state.get(cid) is None:
            walk(cid)
    return found


def _check_text_lengths(value: Any, where: str, problems: List[str]) -> None:
    if isinstance(value, str):
        if len(value) > MAX_TEXT:
            problems.append(f"{where} 里有超过 {MAX_TEXT} 字的文字")
    elif isinstance(value, dict):
        for v in value.values():
            _check_text_lengths(v, where, problems)
    elif isinstance(value, list):
        for v in value:
            _check_text_lengths(v, where, problems)


def _check_group(cid: str, c: dict, problems: List[str]) -> None:
    """`group`：做完的卡片在「已经替你做了」里和同一组的合成一行（§4「后加的写法」）。只写在 root 上。"""
    g = c.get("group")
    if cid != "root":
        problems.append(f"组件 {cid}：group 只能写在 root 上")
        return
    if not isinstance(g, dict):
        problems.append("root 的 group 要是对象")
        return
    for k in ("key", "text"):
        if not isinstance(g.get(k), str) or not g[k].strip() or len(g[k]) > MAX_TEXT:
            problems.append(f"root 的 group.{k} 要是不空的文字")
    n = g.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        problems.append("root 的 group.n 要是正整数")
    for k in ("label", "badge"):
        if k in g and (not isinstance(g[k], str) or len(g[k]) > MAX_TEXT):
            problems.append(f"root 的 group.{k} 要是文字")


def _check_subtitle(cid: str, c: dict, problems: List[str]) -> None:
    """`subtitle`：卡片的副标题，一句分得清是哪一件的字（发件人、文件名），紧凑的表里跟在标题后面（§4「后加的写法」）。
    只写在 root 上，是一句不空的文字（不能绑定数据模型）。"""
    if cid != "root":
        problems.append(f"组件 {cid}：subtitle 只能写在 root 上")
        return
    v = c.get("subtitle")
    if not isinstance(v, str) or not v.strip():
        problems.append("root 的 subtitle 要是一句不空的文字")
    elif len(v) > MAX_TEXT:
        problems.append(f"root 的 subtitle 超过 {MAX_TEXT} 字")


def _literal_label(label: Any, components: dict) -> bool:
    """打开网页的按钮上的字是写死的：一个 Text，或一行里几个图标加一个 Text（§5）。"""
    def literal(x: Any) -> bool:
        return isinstance(x, dict) and x.get("component") == "Text" and isinstance(x.get("text"), str)
    if literal(label):
        return True
    if not (isinstance(label, dict) and label.get("component") == "Row" and isinstance(label.get("children"), list)):
        return False
    kids = [components.get(k) if isinstance(k, str) else None for k in label["children"]]
    texts = [k for k in kids if isinstance(k, dict) and k.get("component") == "Text"]
    return (len(texts) == 1 and literal(texts[0])
            and all(isinstance(k, dict) and k.get("component") in ("Icon", "Text") for k in kids))


def _check_refresh(cid: str, c: dict, page: bool, problems: List[str]) -> None:
    """`refresh`：应用页看得见时过几秒再读（§4 pageRefresh）。只写在页的 root 上，2 到 60 的整数。"""
    n = c.get("refresh")
    if cid != "root" or not page:
        problems.append(f"组件 {cid}：refresh 只能写在应用页的 root 上")
    elif isinstance(n, bool) or not isinstance(n, int) or not REFRESH_MIN <= n <= REFRESH_MAX:
        problems.append(f"root 的 refresh 要是 {REFRESH_MIN} 到 {REFRESH_MAX} 的整数（秒）")


def _check_layout(cid: str, c: dict, page: bool, problems: List[str]) -> None:
    """`layout`：应用页一段一张卡片（§4 pageSections）。只写在页的根 Column 上，值只有 sections。"""
    if cid != "root" or not page or c.get("component") != "Column":
        problems.append(f"组件 {cid}：layout 只能写在应用页的根 Column 上")
    elif not isinstance(c.get("layout"), str) or c["layout"] not in LAYOUTS:
        problems.append("root 的 layout 只能是 " + "、".join(sorted(LAYOUTS)))


def _check_open_page(cid: str, context: dict, problems: List[str]) -> None:
    """`agent.open_page` 的上下文：`page`（这个应用自己的页 id），可以带 `args`（§4 pageArgs）。"""
    page = context.get("page")
    if not set(context) <= {"page", "args"} or not isinstance(page, str) or not PAGE_ID.match(page):
        problems.append(f"按钮 {cid}：agent.open_page 只开这个应用自己的页，context 只写 page（页 id，不带应用名）和 args")
        return
    if "args" not in context:
        return
    args = context["args"]
    if not isinstance(args, dict) or len(args) > MAX_PAGE_ARGS:
        problems.append(f"按钮 {cid}：agent.open_page 的 args 要是对象，最多 {MAX_PAGE_ARGS} 个")
        return
    for k, v in args.items():
        if not PAGE_ARG_KEY.fullmatch(k):
            problems.append(f"按钮 {cid}：agent.open_page 的 args 的键要是小写字母开头的短名字")
        elif not is_binding(v) and not (isinstance(v, str) and len(v) <= MAX_PAGE_ARG):
            problems.append(f"按钮 {cid}：agent.open_page 的 args 的 {k} 要是不超过 {MAX_PAGE_ARG} 字的文字或 {{\"path\"}}")


def _check_placement(cid: str, c: dict, parent: Optional[str], root_is_column: bool, problems: List[str]) -> None:
    """`placement`：宽窗口里应用页的这一段放进侧栏（§4）。值只有 aside，只能写在根 Column 下面一层的 Column 上。"""
    if not isinstance(c.get("placement"), str) or c["placement"] not in PLACEMENTS:
        problems.append(f"组件 {cid}：placement 只能是 " + "、".join(sorted(PLACEMENTS)))
    elif c.get("component") != "Column" or parent != "root" or not root_is_column:
        problems.append(f"组件 {cid}：placement 只能写在根 Column 下面一层的 Column 上")


def _check_props(cid: str, ctype: str, c: dict, data: dict, problems: List[str], page: bool = False) -> None:
    def bound(v: Any) -> bool:
        if not is_binding(v):
            return False
        try:
            get_path(data, v["path"])
        except KeyError:
            problems.append(f"组件 {cid} 绑定的 {v['path']} 在数据模型里没有")
        return True

    def text(name: str, required: bool) -> None:
        v = c.get(name)
        if v is None:
            if required:
                problems.append(f"组件 {cid}（{ctype}）缺 {name}")
            return
        if bound(v):
            return
        if not isinstance(v, str):
            problems.append(f"组件 {cid}：{name} 要是文字或 {{\"path\"}}")
        elif len(v) > MAX_TEXT:
            problems.append(f"组件 {cid}：{name} 超过 {MAX_TEXT} 字")

    if ctype == "Text":
        text("text", True)
        if "variant" in c and c["variant"] not in TEXT_VARIANTS:
            problems.append(f"组件 {cid}：variant 只能是 " + "、".join(sorted(TEXT_VARIANTS)))
        if "tone" in c and (c.get("variant") != "badge" or not isinstance(c["tone"], str) or c["tone"] not in TONES):
            problems.append(f"组件 {cid}：tone 只能写在 variant badge 上，只能是 " + "、".join(sorted(TONES)))
        n = c.get("maxLines")
        if "maxLines" in c and (isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 20):
            problems.append(f"组件 {cid}：maxLines 要是 1 到 20 的整数")
    elif ctype == "Column" and "collapsed" in c:
        v = c["collapsed"]
        if not isinstance(v, str) or not v.strip():
            problems.append(f"组件 {cid}：collapsed 要是一句不空的文字（收起时显示的那一行）")
        elif len(v) > MAX_TEXT:
            problems.append(f"组件 {cid}：collapsed 超过 {MAX_TEXT} 字")
    elif ctype == "Button":
        text("tooltip", False)
        action = c.get("action")
        event = action.get("event") if isinstance(action, dict) else None
        if not isinstance(event, dict) or not isinstance(event.get("name"), str) or not event["name"]:
            problems.append(f"按钮 {cid} 要有 action.event.name")
            return
        context = event.get("context", {})
        if not isinstance(context, dict):
            problems.append(f"按钮 {cid} 的 context 要是对象")
            return
        _check_bindings(context, cid, data, problems)
        name = event["name"]
        if name.startswith("agent.") and name not in AGENT_EVENTS:
            problems.append(f"按钮 {cid}：agent. 开头的动作只有 " + "、".join(sorted(AGENT_EVENTS)))
        if name == "agent.open_url" and c.get("variant") == "switch":
            problems.append(f"按钮 {cid}：agent.open_url 不能画成开关：开关上看不到会去哪个网站")
        if name == "agent.open_url" and open_url_site(context.get("url")) is None:
            problems.append(f"按钮 {cid}：agent.open_url 的 url 要是写死的 https 地址（不带反斜杠、空白、控制字符、userinfo，主机不空）")
        if name == "agent.open_page":
            _check_open_page(cid, context, problems)
        if name == "agent.open_page" and c.get("variant") == "switch":
            problems.append(f"按钮 {cid}：agent.open_page 不能画成开关")
        if "count" in c:
            _check_count(c["count"], cid, data, problems)
    elif ctype == "ProgressBar":
        v = c.get("value")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
            problems.append(f"组件 {cid}：value 要是 0 到 1 的数")
    elif ctype == "CheckBox":
        text("label", True)
        v = c.get("value")
        if not bound(v) and not isinstance(v, bool):
            problems.append(f"组件 {cid}：value 要是真假或 {{\"path\"}}")
    elif ctype == "ChoicePicker":
        text("label", False)
        opts = c.get("options")
        if not isinstance(opts, list) or not opts:
            problems.append(f"组件 {cid}：options 要是非空数组")
            values: List[str] = []
        else:
            values = []
            for o in opts:
                if not isinstance(o, dict) or not isinstance(o.get("value"), str):
                    problems.append(f"组件 {cid}：每个选项要有字符串的 value")
                    continue
                values.append(o["value"])
                lbl = o.get("label")
                if not bound(lbl) and not isinstance(lbl, str):
                    problems.append(f"组件 {cid}：每个选项要有 label")
                if "icon" in o and o["icon"] not in ICONS:
                    problems.append(f"组件 {cid}：选项的图标名只能是 " + "、".join(sorted(ICONS)))
        v = c.get("value")
        if is_binding(v):
            try:
                cur = get_path(data, v["path"])
            except KeyError:
                problems.append(f"组件 {cid} 绑定的 {v['path']} 在数据模型里没有")
            else:
                if not isinstance(cur, list) or not all(isinstance(x, str) for x in cur):
                    problems.append(f"组件 {cid}：ChoicePicker 的值要是字符串数组（单选也一样）")
                elif values and any(x not in values for x in cur):
                    problems.append(f"组件 {cid}：当前值不在选项里")
        elif not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            problems.append(f"组件 {cid}：ChoicePicker 的值要是字符串数组（单选也一样）或 {{\"path\"}}")
    elif ctype == "TextField":
        text("label", True)
        text("value", False)
        if c.get("variant") == "obscured":       # 密码框（§4 secureField）：只在页上，绑定数据模型，工具给的值只能是空的
            v = c.get("value")
            if not page:
                problems.append(f"组件 {cid}：密码框（variant obscured）只能用在应用页上")
            elif not is_binding(v):
                problems.append(f"组件 {cid}：密码框的 value 要绑定数据模型（{{\"path\"}}）")
            else:
                try:
                    cur = get_path(data, v["path"])
                except KeyError:
                    cur = ""          # 上面 text() 已经记了没有这个路径
                if cur != "":
                    problems.append(f"组件 {cid}：密码框的初值只能是空字符串")
    elif ctype == "DateTimeInput":
        text("value", True)
    elif ctype == "Slider":
        for k in ("min", "max"):
            if k in c and not isinstance(c[k], (int, float)):
                problems.append(f"组件 {cid}：{k} 要是数")
        v = c.get("value")
        if not bound(v) and not isinstance(v, (int, float)):
            problems.append(f"组件 {cid}：value 要是数或 {{\"path\"}}")
        text("label", False)
    elif ctype == "Icon":
        if c.get("name") not in ICONS:
            problems.append(f"组件 {cid}：图标名只能是 " + "、".join(sorted(ICONS)))


def _check_count(count: Any, cid: str, data: dict, problems: List[str]) -> None:
    """按钮的 count（§4 countLabel）：`{"path": 一组选择, "value": 数哪个}`，path 指向「键 → 选了的值的数组」。
    §4 countAlso：可以再带 `"also": [别的值]` 和 `"alsoLabel": "按钮的另一句字"`（两样一起写）。
    §4 countWeights：可以再带 `"weights": {选择的键: 正整数}`（一项合了好几样时它算几样）。"""
    if not (isinstance(count, dict) and {"path", "value"} <= set(count) <= {"path", "value", "also", "alsoLabel", "weights"}
            and isinstance(count["path"], str) and isinstance(count["value"], str) and count["value"]):
        problems.append(f"按钮 {cid}：count 要是 {{\"path\", \"value\"}}（数一组选择里选了 value 的项）")
        return
    if ("also" in count) != ("alsoLabel" in count):
        problems.append(f"按钮 {cid}：count 的 also 和 alsoLabel 要一起写")
    if "also" in count:
        also = count["also"]
        if not (isinstance(also, list) and also and all(isinstance(v, str) and v for v in also)):
            problems.append(f"按钮 {cid}：count.also 要是不空的一组值（文字）")
    if "weights" in count:
        w = count["weights"]
        if not (isinstance(w, dict) and all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in w.values())):
            problems.append(f"按钮 {cid}：count.weights 要是对象，每个键（选择的键）对一个正整数")
    if "alsoLabel" in count:
        label = count["alsoLabel"]
        if not isinstance(label, str) or not label.strip():
            problems.append(f"按钮 {cid}：count.alsoLabel 要是一句不空的文字")
        elif len(label) > MAX_TEXT:
            problems.append(f"按钮 {cid}：count.alsoLabel 超过 {MAX_TEXT} 字")
    try:
        chosen = get_path(data, count["path"])
    except KeyError:
        problems.append(f"按钮 {cid}：count 绑定的 {count['path']} 在数据模型里没有")
        return
    if not isinstance(chosen, dict):
        problems.append(f"按钮 {cid}：count 的 path 要指向一组选择（键 → 选了的值的数组）")


def count_chosen(data: Any, count: dict) -> int:
    """count 现在数出几项：path 下每一项选了的值里有 value 的（窗口画按钮的字、0 项时按钮灰掉）。
    §4 countWeights：写了 `weights` 的键按它算几样（不写算 1）；`weights` 里有、选择里没有的键不管。"""
    try:
        chosen = get_path(data, count["path"])
    except (KeyError, TypeError):
        return 0
    if not isinstance(chosen, dict):
        return 0
    weights = count.get("weights") if isinstance(count.get("weights"), dict) else {}
    return sum(weights.get(k, 1) for k, v in chosen.items() if isinstance(v, list) and count["value"] in v)


def count_also(data: Any, count: dict) -> bool:
    """§4 countAlso：path 下有没有哪一项选了 `also` 里的值。有就是按钮能点、字换成 `alsoLabel`（`{n}` 照样只数 value），
    没有照 countLabel（0 项时灰掉）。"""
    also = count.get("also") if isinstance(count, dict) else None
    if not isinstance(also, list):
        return False
    try:
        chosen = get_path(data, count["path"])
    except (KeyError, TypeError):
        return False
    if not isinstance(chosen, dict):
        return False
    return any(isinstance(v, list) and any(x in also for x in v) for v in chosen.values())


def _check_bindings(value: Any, cid: str, data: dict, problems: List[str]) -> None:
    if is_binding(value):
        try:
            get_path(data, value["path"])
        except KeyError:
            problems.append(f"按钮 {cid} 的上下文绑定的 {value['path']} 在数据模型里没有")
    elif isinstance(value, dict):
        for v in value.values():
            _check_bindings(v, cid, data, problems)
    elif isinstance(value, list):
        for v in value:
            _check_bindings(v, cid, data, problems)
