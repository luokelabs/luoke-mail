# Copied from luoke-agent (luoke_agent/a2ui.py, commit e4e4d97): the tool protocol's reference code, stdlib only.
# Change it there and copy it again; do not edit this copy.
"""A2UI v0.9.1 卡片：按组件目录校验、应用更新、解析路径绑定（docs/工具协议.md §4）。

agent 收到卡片先过这里；工具的测试也可以用 `validate()` 查自己出的卡片。
和 Swift 的 LuokeA2UICore 是同一套规矩。只用标准库，Python 3.9 起。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

VERSION = "v0.9"
AGENT_CATALOG = "ai.luoke/a2ui/agent/1"
IME_CATALOG = "ai.luoke/a2ui/ime/1"
CATALOGS = {
    AGENT_CATALOG: frozenset({"Text", "Row", "Column", "List", "Card", "Divider", "Button", "CheckBox",
                              "ChoicePicker", "TextField", "DateTimeInput", "Icon"}),
    IME_CATALOG: frozenset({"Text", "CheckBox", "ChoicePicker", "Slider", "TextField", "Button", "Divider",
                            "Row", "Column"}),
}
MESSAGE_KINDS = ("createSurface", "updateComponents", "updateDataModel", "deleteSurface")
MAX_COMPONENTS = 200
MAX_TEXT = 2000
# agent 自己处理的动作（§5），工具的动作名不能以 agent. 开头
AGENT_EVENTS = frozenset({"agent.open_url", "agent.dismiss"})
# Icon 能用的名字，渲染器各自映射到系统符号
ICONS = frozenset({"mail", "calendar", "warning", "check", "close", "trash", "archive", "star", "reply",
                   "clock", "info", "person", "link", "memory", "question", "lightbulb"})
TEXT_VARIANTS = frozenset({"h1", "h2", "h3", "h4", "h5", "body", "caption"})


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


def get_path(data: Any, path: str) -> Any:
    """按 JSON 指针取值，取不到抛 KeyError。"""
    cur = data
    for t in _tokens(path):
        if isinstance(cur, dict) and t in cur:
            cur = cur[t]
        elif isinstance(cur, list) and t.isdigit() and int(t) < len(cur):
            cur = cur[int(t)]
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
        if isinstance(cur, list) and t.isdigit() and int(t) < len(cur):
            cur = cur[int(t)]
        elif isinstance(cur, dict):
            cur = cur.setdefault(t, {})
        else:
            raise KeyError(path)
    last = toks[-1]
    if isinstance(cur, list) and last.isdigit() and int(last) < len(cur):
        if remove:
            del cur[int(last)]
        else:
            cur[int(last)] = value
    elif isinstance(cur, dict):
        if remove:
            cur.pop(last, None)
        else:
            cur[last] = value
    else:
        raise KeyError(path)
    return data


def is_binding(v: Any) -> bool:
    return isinstance(v, dict) and set(v) == {"path"} and isinstance(v["path"], str)


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

    def __init__(self, surface_id: str, catalog_id: str):
        if catalog_id not in CATALOGS:
            raise CardError([f"不认识的组件目录：{catalog_id}"])
        self.surface_id = surface_id
        self.catalog_id = catalog_id
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
        if len(shown) > MAX_COMPONENTS:
            problems.append(f"组件超过 {MAX_COMPONENTS} 个")
        if len(self.components) > 5 * MAX_COMPONENTS:
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
            _check_props(cid, ctype, c, self.data, problems)
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


def build(messages: Any, catalog_id: str = AGENT_CATALOG) -> Surface:
    """一组新卡片的消息（第一条是 createSurface）→ 查过的 Surface。不行抛 CardError。"""
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
    surface = Surface(sid, catalog_id)
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


def validate(messages: Any, catalog_id: str = AGENT_CATALOG) -> List[str]:
    """给工具的测试用：没问题返回 []，否则返回问题列表。"""
    try:
        build(messages, catalog_id)
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


def _check_props(cid: str, ctype: str, c: dict, data: dict, problems: List[str]) -> None:
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
    elif ctype == "Button":
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
        if name == "agent.open_url":
            url = context.get("url")
            if not isinstance(url, str) or not url.startswith("https://") or len(url) > MAX_TEXT:
                problems.append(f"按钮 {cid}：agent.open_url 的 url 要是写死的 https 地址")
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
                if not is_binding(lbl) and not isinstance(lbl, str):
                    problems.append(f"组件 {cid}：每个选项要有 label")
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
