# Copied from luoke-agent (luoke_agent/cards.py, commit 15a9f75): the tool protocol's reference code, stdlib only.
# Change it there and copy it again; do not edit this copy.
# SPDX-License-Identifier: Apache-2.0 OR MIT
# Copyright 2026 螺壳实验室 Luoke Labs
"""拼 A2UI 卡片的小帮手（docs/工具协议.md §4）。

只用标准库、不依赖本包别的文件，可以整个拷进工具自己的仓库。出的卡片用组件目录
`ai.luoke/a2ui/agent/1`；想查对不对，用 luoke_agent.a2ui.validate()。

    card = Card("plan-42")
    card.heading("这 2 封我打算这样处理")
    card.choice("m1", "王老师：答辩时间？（周五前回）", [("draft", "写草稿"), ("star", "加星标"), ("keep", "先不动")], "draft")
    card.buttons(button("写草稿 1", "mail_apply_plan", {"plan_id": "plan-42", "choices": CHOICES}, primary=True))
    messages = card.messages()

每个 `choice` 的当前值放在数据模型的 `/choices/<key>`（字符串数组，单选也一样），按钮的上下文里写
`CHOICES`（即 `{"path": "/choices"}`），agent 点按时换成 `{"m1": ["draft"], …}` 交回来。

「今天」的新模板（§4「后加的写法」）：`plan_card` 一张卡片一列邮件，`mailbox_card` 一个邮箱一张、
分几块（待处理 / 可以整理 / 判为可疑）。旧的核心不认新写法：把请求 `_meta` 交给 `host_features`，结果传给 `features=`，
核心没列全就出原来的样子。
"""
from __future__ import annotations

import contextlib
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

CATALOG = "ai.luoke/a2ui/agent/1"
VERSION = "v0.9"
CHOICES = {"path": "/choices"}
# 新模板要用到的 §4 后加的写法：核心在 `clientCapabilities.experimental["ai.luoke/a2ui"].features` 里都列了才出新样子
NEW_TEMPLATE = frozenset({"badge", "strong", "maxLines", "optionIcons", "lucide", "tooltip"})
# 只有新的核心认得的图标名（以前的名字新旧都认）：旧的核心上出卡片时不带这些
OLD_ICONS = frozenset({"mail", "calendar", "warning", "check", "close", "trash", "archive", "star", "reply", "clock",
                       "info", "person", "link", "memory", "question", "lightbulb", "folder", "settings", "open"})


def host_features(meta: Optional[dict]) -> frozenset:
    """请求的 `params._meta` 里核心认的 §4 后加的写法（`clientCapabilities.experimental["ai.luoke/a2ui"].features`）；
    旧的核心不写，是空集合。"""
    caps = (meta or {}).get("io.modelcontextprotocol/clientCapabilities") or {}
    feats = ((caps.get("experimental") or {}).get("ai.luoke/a2ui") or {}).get("features") or []
    return frozenset(f for f in feats if isinstance(f, str))


def button(label: str, event: str, context: Optional[dict] = None, *, primary: bool = False,
           icon: Optional[str] = None, tooltip: Optional[str] = None, variant: Optional[str] = None) -> dict:
    """一个按钮的说明，交给 `Card.buttons`。`event` 是工具的 MCP 工具名，或 agent 的 `agent.open_url`。
    `icon`：Lucide 的图标名，画在字前面；`tooltip`：悬停提示和读屏念的字（按钮上的字太短时写清做什么）。"""
    spec = {"label": label, "event": event, "context": context or {}, "primary": primary}
    if icon:
        spec["icon"] = icon
    if tooltip:
        spec["tooltip"] = tooltip
    if variant:
        spec["variant"] = variant
    return spec


def open_url_button(label: str, url: str, *, icon: Optional[str] = None, tooltip: Optional[str] = None) -> dict:
    """打开网页的按钮：窗口把去哪个网站写进悬停提示和读屏念的字，用户点了再开（§5）。只认 https。"""
    if not url.startswith("https://"):
        raise ValueError("只认 https 地址")
    return button(label, "agent.open_url", {"url": url}, icon=icon, tooltip=tooltip)


def split_label(label: str) -> Tuple[str, str]:
    """「发件人：主题」拆成两段；没有冒号就只有主题。"""
    for sep in ("：", ":"):
        who, found, subject = label.partition(sep)
        if found and who.strip() and subject.strip():
            return who.strip(), subject.strip()
    return "", label


class Card:
    def __init__(self, surface_id: str, *, id_prefix: str = ""):
        """`id_prefix`：换掉同一张卡片时用不同的前缀，免得新旧组件撞编号。"""
        self.surface_id = surface_id
        self._prefix = id_prefix
        self._components: List[dict] = []
        self._top: List[str] = []
        self._data: Dict[str, Any] = {}
        self._n = 0
        self.done_group: Optional[dict] = None   # 做完的卡片在「已经替你做了」里的分组（root 上的 group，§4）
        self.refresh: Optional[int] = None        # 应用页看得见时过几秒再读（root 上的 refresh，§4 pageRefresh；只用在页上）
        # 卡片的副标题（root 上的 subtitle，§4）：一句分得清是哪一件的字（发件人和主题、文件名），紧凑的「今天」里跟在标题后面。
        # 核心列了 "subtitle"（`host_features`）才写
        self.subtitle: Optional[str] = None

    def _new_id(self, prefix: str) -> str:
        self._n += 1
        return f"{self._prefix}{prefix}{self._n}"

    def _add(self, comp: dict, top: bool = True) -> str:
        self._components.append(comp)
        if top:
            self._top.append(comp["id"])
        return comp["id"]

    def heading(self, text: str, *, badge: Optional[str] = None) -> str:
        """卡片标题；`badge`（例如邮箱地址）是标题后面的一个标签。"""
        h = {"id": self._new_id("h"), "component": "Text", "text": text, "variant": "h4"}
        if not badge:
            return self._add(h)
        self._add(h, top=False)
        b = self._add({"id": self._new_id("t"), "component": "Text", "text": badge, "variant": "badge"}, top=False)
        return self._add({"id": self._new_id("r"), "component": "Row", "children": [h["id"], b]})

    def section(self, icon: str, title: str) -> str:
        """卡片里一块的小标题（图标 + 字）。前面通常有一条 `divider`。"""
        i = self._add({"id": self._new_id("i"), "component": "Icon", "name": icon}, top=False)
        t = self._add({"id": self._new_id("t"), "component": "Text", "text": title}, top=False)
        return self._add({"id": self._new_id("r"), "component": "Row", "children": [i, t]})

    @contextlib.contextmanager
    def group(self, *, collapsed: Optional[str] = None, placement: Optional[str] = None) -> Iterator[None]:
        """把这一段里加的包成一个 `Column`：`collapsed` 是收起时显示的那一句（点开才看得到里面），
        `placement="aside"` 是宽窗口里放进侧栏的一段（应用页，只能在最外层）。"""
        before = len(self._top)
        yield
        kids = self._top[before:]
        del self._top[before:]
        if not kids:
            return
        comp: Dict[str, Any] = {"id": self._new_id("g"), "component": "Column", "children": kids}
        if collapsed:
            comp["collapsed"] = collapsed
        if placement:
            comp["placement"] = placement
        self._add(comp)

    def mail_items(self, items: Iterable[dict]) -> str:
        """一封一行的列表：每项 `{"key", "options": [(值, 字, 图标)], "selected"}`，再加 `who`、`subject`（或一个
        「发件人：主题」的 `label`），可以带 `why`（一行理由）、`due`（截止标签的字）和 `due_tone`（"danger" / "warning"）、
        `extra`（这一行右边的一个小按钮，例如 `open_url_button("11-12 16:00", …, icon="calendar-plus", tooltip="加入日程")`）。"""
        return self._add({"id": self._new_id("l"), "component": "List",
                          "children": [self._mail_item(it) for it in items]})

    def _mail_item(self, it: dict) -> str:
        who, subject = it.get("who"), it.get("subject")
        if who is None and subject is None:
            who, subject = split_label(it["label"])
        line = []
        if who:
            line.append(self._add({"id": self._new_id("t"), "component": "Text", "text": who, "variant": "strong"}, top=False))
        line.append(self._add({"id": self._new_id("t"), "component": "Text", "text": subject or "", "maxLines": 1}, top=False))
        if it.get("due"):
            due = {"id": self._new_id("t"), "component": "Text", "text": it["due"], "variant": "badge"}
            if it.get("due_tone"):
                due["tone"] = it["due_tone"]
            line.append(self._add(due, top=False))
        left = [self._add({"id": self._new_id("r"), "component": "Row", "children": line}, top=False)]
        why = it.get("why") or it.get("detail")
        if why:
            left.append(self._add({"id": self._new_id("t"), "component": "Text", "text": why, "variant": "caption",
                                   "maxLines": 1}, top=False))
        col = self._add({"id": self._new_id("c"), "component": "Column", "children": left}, top=False)
        label = f"{who}：{subject}" if who else (subject or "")
        right = [self._choice(it["key"], label, it["options"], it["selected"], top=False)]
        if it.get("extra"):
            right.append(self._button(it["extra"]))
        return self._add({"id": self._new_id("r"), "component": "Row", "justify": "spaceBetween",
                          "children": [col] + right}, top=False)

    def text(self, text: str, *, caption: bool = False) -> str:
        comp = {"id": self._new_id("t"), "component": "Text", "text": text}
        if caption:
            comp["variant"] = "caption"
        return self._add(comp)

    def divider(self) -> str:
        return self._add({"id": self._new_id("d"), "component": "Divider"})

    def choice(self, key: str, label: str, options: Sequence[Tuple[str, ...]], selected: str) -> str:
        """一项可选的处理：`options` 是（值，给用户看的字[，Lucide 图标名]），`selected` 是建议的那个值。"""
        return self._choice(key, label, options, selected, top=True)

    def _choice(self, key: str, label: str, options: Sequence[Tuple[str, ...]], selected: str, *, top: bool) -> str:
        values = [o[0] for o in options]
        if selected not in values:
            raise ValueError(f"{selected} 不在选项里")
        self._data.setdefault("choices", {})[key] = [selected]
        opts = []
        for o in options:
            opt = {"label": o[1], "value": o[0]}
            if len(o) > 2 and o[2]:
                opt["icon"] = o[2]
            opts.append(opt)
        return self._add({
            "id": f"{self._prefix}c-{key}", "component": "ChoicePicker", "label": label,
            "options": opts, "value": {"path": f"/choices/{key}"},
        }, top=top)

    def checkbox(self, key: str, label: str, value: bool = False) -> str:
        """一个勾选框，值放在数据模型的 `/checks/<key>`；按钮上下文里写 `{"path": "/checks"}` 一起取。"""
        self._data.setdefault("checks", {})[key] = value
        return self._add({"id": f"{self._prefix}k-{key}", "component": "CheckBox", "label": label,
                          "value": {"path": f"/checks/{key}"}})

    def text_field(self, key: str, label: str, value: str = "", *, secure: bool = False) -> str:
        """一个输入框，值放在数据模型的 `/fields/<key>`；按钮上下文里写 `{"path": "/fields/<key>"}` 取它。
        `secure`：密码框（§4 secureField，只在应用页上、核心认 `secureField` 时用），初值一律是空的。"""
        self._data.setdefault("fields", {})[key] = "" if secure else value
        comp = {"id": f"{self._prefix}f-{key}", "component": "TextField", "label": label, "value": {"path": f"/fields/{key}"}}
        if secure:
            comp["variant"] = "obscured"
        return self._add(comp)

    def buttons(self, *specs: dict) -> str:
        """一排按钮。"""
        return self._add({"id": self._new_id("r"), "component": "Row", "children": [self._button(s) for s in specs]})

    def _button(self, spec: dict) -> str:
        bid = self._new_id("b")
        tid = bid + "-text"
        comp = {"id": bid, "component": "Button", "child": tid,
                "action": {"event": {"name": spec["event"], "context": spec["context"]}}}
        if spec.get("primary"):
            comp["variant"] = "primary"
        elif spec.get("variant"):
            comp["variant"] = spec["variant"]      # 例如 borderless：每一行上的小动作
        if spec.get("tooltip"):
            comp["tooltip"] = spec["tooltip"]
        if spec.get("icon"):                       # 字前面一个图标：子组件是一行「图标 + 字」（§4「图标按钮」）
            rid = bid + "-row"
            comp["child"] = rid
            self._add(comp, top=False)
            self._add({"id": bid + "-icon", "component": "Icon", "name": spec["icon"]}, top=False)
            self._add({"id": tid, "component": "Text", "text": spec["label"]}, top=False)
            self._add({"id": rid, "component": "Row", "children": [bid + "-icon", tid]}, top=False)
        else:
            self._add(comp, top=False)
            self._add({"id": tid, "component": "Text", "text": spec["label"]}, top=False)
        return bid

    def _root_and_components(self) -> List[dict]:
        root: Dict[str, Any] = {"id": "root", "component": "Column", "children": list(self._top)}
        if self.done_group:
            root["group"] = self.done_group
        if self.subtitle:
            root["subtitle"] = self.subtitle
        if self.refresh:
            root["refresh"] = self.refresh
        return [root] + self._components

    def messages(self) -> List[dict]:
        """一张新卡片：createSurface + updateComponents (+ updateDataModel)。"""
        out = [{"version": VERSION, "createSurface": {"surfaceId": self.surface_id, "catalogId": CATALOG}}]
        out.extend(self.update_messages())
        return out

    def update_messages(self, *, include_data: bool = True) -> List[dict]:
        """把同一张卡片换成现在这个样子（执行完换成结果时用）：不带 createSurface。
        `include_data=False`：不动数据模型，用户已经改过的选项留着。"""
        out = [{"version": VERSION, "updateComponents": {"surfaceId": self.surface_id,
                                                         "components": self._root_and_components()}}]
        if self._data and include_data:
            out.append({"version": VERSION, "updateDataModel": {"surfaceId": self.surface_id, "path": "/",
                                                                 "value": self._data}})
        return out


def _plain(spec: dict) -> dict:
    """旧的核心上出的按钮：去掉它不认的（Lucide 才有的图标名、悬停提示）。"""
    out = dict(spec)
    if out.get("icon") not in OLD_ICONS:
        out.pop("icon", None)
    out.pop("tooltip", None)
    return out


def _old_items(card: Card, items: Iterable[dict]) -> None:
    for it in items:
        label = it.get("label") or "：".join(x for x in (it.get("who"), it.get("subject")) if x)
        if it.get("due"):
            label += f"（{it['due']}）"
        card.choice(it["key"], label, [o[:2] for o in it["options"]], it["selected"])
        if it.get("why") or it.get("detail"):
            card.text(it.get("why") or it.get("detail"), caption=True)


def plan_card(surface_id: str, title: str, items: Iterable[dict], *, apply_event: str, plan_id: str,
              apply_label: str, note: Optional[str] = None, badge: Optional[str] = None,
              extra_buttons: Sequence[dict] = (), update: bool = False, id_prefix: str = "",
              features: Optional[Iterable[str]] = None) -> List[dict]:
    """「这几封我打算这样处理」：一张卡片一列邮件（每项见 `Card.mail_items`）。`badge` 是标题后面的标签（邮箱地址）。

    主按钮写清做什么（`apply_label`，例如「加星标 4 · 写草稿 1」），调 `apply_event`，上下文是
    `{"plan_id": …, "choices": {每项的 key: [选中的值]}}`。`update=True` 时出的是换掉同一张卡片的消息（例如执行不了时
    带上一句说明重画），配一个新的 `id_prefix`；不动数据模型，用户已经改过的选项留着。
    `features`：`host_features(请求的 _meta)`；不给当新的核心，给了没列全就出原来的样子。
    """
    card = Card(surface_id, id_prefix=id_prefix)
    items = list(items)
    if features is not None and not NEW_TEMPLATE <= set(features):
        card.heading(title + (f"（{badge}）" if badge else ""))
        if note:
            card.text(note, caption=True)
        _old_items(card, items)
        card.buttons(button(apply_label, apply_event, {"plan_id": plan_id, "choices": CHOICES}, primary=True),
                     *[_plain(b) for b in extra_buttons])
    else:
        card.heading(title, badge=badge)
        if note:
            card.text(note, caption=True)
        card.mail_items(items)
        card.buttons(button(apply_label, apply_event, {"plan_id": plan_id, "choices": CHOICES}, primary=True,
                            icon="check"), *extra_buttons)
    return card.update_messages(include_data=False) if update else card.messages()


def mailbox_card(surface_id: str, title: str, parts: Iterable[dict], *, badge: Optional[str] = None,
                 update: bool = False, id_prefix: str = "", features: Optional[Iterable[str]] = None) -> List[dict]:
    """一个邮箱一张、分几块的卡片（「顺便看看」里的「邮箱」）。每块
    `{"icon", "title", "items"?（同 plan_card）, "note"?（一行小字）, "collapsed"?（先收起，这一句是收起时显示的）,
    "buttons": [按钮]}`，没有东西的块别传。几块的选项都在同一个数据模型里，按钮上下文写 `CHOICES` 时交回所有块的选择，
    工具按自己的 key 取。`features` 同 `plan_card`。"""
    card = Card(surface_id, id_prefix=id_prefix)
    new = features is None or NEW_TEMPLATE <= set(features)
    card.heading(title, badge=badge) if new else card.heading(title + (f"（{badge}）" if badge else ""))
    for part in parts:
        card.divider()
        if new:
            card.section(part["icon"], part["title"])
        else:
            card.text(part["title"], caption=True)
        with card.group(collapsed=part.get("collapsed") if new else None):
            if part.get("note"):
                card.text(part["note"], caption=True)
            if part.get("items"):
                card.mail_items(part["items"]) if new else _old_items(card, part["items"])
            if part.get("buttons"):
                card.buttons(*(part["buttons"] if new else [_plain(b) for b in part["buttons"]]))
    return card.update_messages(include_data=False) if update else card.messages()


def done_group(key: str, text: str, n: int, *, label: Optional[str] = None, badge: Optional[str] = None) -> dict:
    """`result_card(group=…)` 用：「已经替你做了」里 `key` 一样的合成一行（「可疑邮件 · 已移到垃圾邮件 4 封」+ 标签），
    `text` 里的 `{n}` 换成各张的 `n` 加起来，点一次撤销撤整行。旧的核心不认，每张照旧各占一行。"""
    g: Dict[str, Any] = {"key": key, "text": text, "n": n}
    if label:
        g["label"] = label
    if badge:
        g["badge"] = badge
    return g


def result_card(surface_id: str, text: str, *, undo_event: Optional[str] = None,
                undo_context: Optional[dict] = None, details: Sequence[str] = (),
                buttons: Sequence[dict] = (), id_prefix: Optional[str] = None,
                group: Optional[dict] = None) -> List[dict]:
    """执行完：把原来那张卡片换成结果（一句话、几行细节），有撤销就带 [撤销]，还可以再加几个按钮。
    `group`：`done_group(…)`，在「已经替你做了」里和同一组的合成一行。"""
    if id_prefix is None:
        id_prefix = "res-" if undo_event else "end-"
    card = Card(surface_id, id_prefix=id_prefix)
    card.done_group = group
    card.text(text)
    for d in details:
        card.text(d, caption=True)
    specs = list(buttons)
    if undo_event:
        specs.insert(0, button("撤销", undo_event, undo_context or {}))
    if specs:
        card.buttons(*specs)
    return card.update_messages()
