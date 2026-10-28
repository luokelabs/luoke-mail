# Copied from luoke-agent (luoke_agent/cards.py, commit e4e4d97): the tool protocol's reference code, stdlib only.
# Change it there and copy it again; do not edit this copy.
"""拼 A2UI 卡片的小帮手（docs/工具协议.md §4）。

只用标准库、不依赖本包别的文件，可以整个拷进工具自己的仓库。出的卡片用组件目录
`ai.luoke/a2ui/agent/1`；想查对不对，用 luoke_agent.a2ui.validate()。

    card = Card("plan-42")
    card.heading("这 2 封我打算这样处理")
    card.choice("m1", "王老师：答辩时间？（周五前回）", [("draft", "写草稿"), ("star", "待跟进"), ("keep", "先不动")], "draft")
    card.buttons(button("按这个处理", "mail_apply_plan", {"plan_id": "plan-42", "choices": CHOICES}, primary=True))
    messages = card.messages()

每个 `choice` 的当前值放在数据模型的 `/choices/<key>`（字符串数组，单选也一样），按钮的上下文里写
`CHOICES`（即 `{"path": "/choices"}`），agent 点按时换成 `{"m1": ["draft"], …}` 交回来。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

CATALOG = "ai.luoke/a2ui/agent/1"
VERSION = "v0.9"
CHOICES = {"path": "/choices"}


def button(label: str, event: str, context: Optional[dict] = None, *, primary: bool = False) -> dict:
    """一个按钮的说明，交给 `Card.buttons`。`event` 是工具的 MCP 工具名，或 agent 的 `agent.open_url`。"""
    return {"label": label, "event": event, "context": context or {}, "primary": primary}


def open_url_button(label: str, url: str) -> dict:
    """打开网页的按钮：agent 会在文字后面加上网站，用户点了再开（§5）。只认 https。"""
    if not url.startswith("https://"):
        raise ValueError("只认 https 地址")
    return button(label, "agent.open_url", {"url": url})


class Card:
    def __init__(self, surface_id: str, *, id_prefix: str = ""):
        """`id_prefix`：换掉同一张卡片时用不同的前缀，免得新旧组件撞编号。"""
        self.surface_id = surface_id
        self._prefix = id_prefix
        self._components: List[dict] = []
        self._top: List[str] = []
        self._data: Dict[str, Any] = {}
        self._n = 0

    def _new_id(self, prefix: str) -> str:
        self._n += 1
        return f"{self._prefix}{prefix}{self._n}"

    def _add(self, comp: dict, top: bool = True) -> str:
        self._components.append(comp)
        if top:
            self._top.append(comp["id"])
        return comp["id"]

    def heading(self, text: str) -> str:
        return self._add({"id": self._new_id("h"), "component": "Text", "text": text, "variant": "h4"})

    def text(self, text: str, *, caption: bool = False) -> str:
        comp = {"id": self._new_id("t"), "component": "Text", "text": text}
        if caption:
            comp["variant"] = "caption"
        return self._add(comp)

    def divider(self) -> str:
        return self._add({"id": self._new_id("d"), "component": "Divider"})

    def choice(self, key: str, label: str, options: Sequence[Tuple[str, str]], selected: str) -> str:
        """一项可选的处理：`options` 是（值，给用户看的字），`selected` 是建议的那个值。"""
        values = [v for v, _ in options]
        if selected not in values:
            raise ValueError(f"{selected} 不在选项里")
        self._data.setdefault("choices", {})[key] = [selected]
        return self._add({
            "id": f"{self._prefix}c-{key}", "component": "ChoicePicker", "label": label,
            "options": [{"label": lbl, "value": v} for v, lbl in options],
            "value": {"path": f"/choices/{key}"},
        })

    def buttons(self, *specs: dict) -> str:
        """一排按钮。"""
        ids = []
        for spec in specs:
            bid = self._new_id("b")
            tid = bid + "-text"
            comp = {"id": bid, "component": "Button", "child": tid,
                    "action": {"event": {"name": spec["event"], "context": spec["context"]}}}
            if spec.get("primary"):
                comp["variant"] = "primary"
            self._add(comp, top=False)
            self._add({"id": tid, "component": "Text", "text": spec["label"]}, top=False)
            ids.append(bid)
        return self._add({"id": self._new_id("r"), "component": "Row", "children": ids})

    def _root_and_components(self) -> List[dict]:
        return [{"id": "root", "component": "Column", "children": list(self._top)}] + self._components

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


def plan_card(surface_id: str, title: str, items: Iterable[dict], *, apply_event: str, plan_id: str,
              apply_label: str = "按这个处理", note: Optional[str] = None,
              extra_buttons: Sequence[dict] = (), update: bool = False, id_prefix: str = "") -> List[dict]:
    """"这几封我打算这样处理"：每项 `{"key", "label", "options": [(值, 字)], "selected"}`，可以带 `detail`（一行小字）。

    主按钮调 `apply_event`，上下文是 `{"plan_id": …, "choices": {每项的 key: [选中的值]}}`。
    `update=True` 时出的是换掉同一张卡片的消息（例如执行不了时带上一句说明重画），配一个新的 `id_prefix`；
    不动数据模型，用户已经改过的选项留着。
    """
    card = Card(surface_id, id_prefix=id_prefix)
    card.heading(title)
    if note:
        card.text(note, caption=True)
    for it in items:
        card.choice(it["key"], it["label"], it["options"], it["selected"])
        if it.get("detail"):
            card.text(it["detail"], caption=True)
    card.buttons(button(apply_label, apply_event, {"plan_id": plan_id, "choices": CHOICES}, primary=True),
                 *extra_buttons)
    return card.update_messages(include_data=False) if update else card.messages()


def result_card(surface_id: str, text: str, *, undo_event: Optional[str] = None,
                undo_context: Optional[dict] = None, details: Sequence[str] = (),
                buttons: Sequence[dict] = (), id_prefix: Optional[str] = None) -> List[dict]:
    """执行完：把原来那张卡片换成结果（一句话、几行细节），有撤销就带 [撤销]，还可以再加几个按钮。"""
    if id_prefix is None:
        id_prefix = "res-" if undo_event else "end-"
    card = Card(surface_id, id_prefix=id_prefix)
    card.text(text)
    for d in details:
        card.text(d, caption=True)
    specs = list(buttons)
    if undo_event:
        specs.insert(0, button("撤销", undo_event, undo_context or {}))
    if specs:
        card.buttons(*specs)
    return card.update_messages()
