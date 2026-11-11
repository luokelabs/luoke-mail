#!/usr/bin/env python3
"""录给 luoke-agent 的场景样例（fixtures/agent/，工具协议 §11）：在假数据上起真的邮件助手，时钟停在固定的时间，照 agent 的
做法一个请求一个请求地问，把答复存成 JSON。agent 拿它们当样例数据，不用自己猜邮件助手会发什么。

    python3 scripts/record_agent_fixtures.py            重录，写进 fixtures/agent/
    python3 scripts/record_agent_fixtures.py --check    重录进临时目录，和仓库里的比；不一样就列出差别、退出码 1
        [--only <场景>] [--keep]

只用标准库。旁边要有 luoke-mem（和跑测试一样）。一个场景一个进程：`LUOKE_MAIL_HOME` 指到新建的临时目录，用测试的假邮箱
（tests/fakes.py 的 FakeIMAP）和假模型（FakeChat，答复写在下面的 `MAILS` 里，不连真模型），时区 Asia/Shanghai，`Clock` 把
`time`、`datetime` 的现在停住（程序本身没有改时钟的开关）。服务是真的：`App` 和 `agent.sock`，请求经那条 socket 发。
不碰家目录，不连主控。答复里的方案编号按出现的先后换成 000000000001、000000000002……，版本号换成 $VERSION；资源的
`contents[0].text` 在线上是一段 JSON 文字，这里解开成 JSON 值好读。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import difflib
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from luoke_mail.agent_mcp import CLIENT_CAPABILITIES  # noqa: E402 -- what the agent says it draws (§4 features)
OUT = os.path.join(ROOT, "fixtures", "agent")
MANIFEST = os.path.join(ROOT, "packaging", "accessory.json")
ACCESSORY = "luoke-mail"
OUTBOX = f"luoke://{ACCESSORY}/outbox"
PROTOCOL = "2026-07-28"
USER_ACTION = "ai.luoke/user-action"
TZ = "Asia/Shanghai"
PAGES = ("today", "inbox", "clean", "digest", "subs", "rules", "activity", "settings")   # window/pages.PAGES
ME = "me@example.com"
UTC = datetime.timezone.utc
SHANGHAI = datetime.timezone(datetime.timedelta(hours=8))
T0 = datetime.datetime(2026, 10, 20, 1, 30, tzinfo=UTC)   # 上海时间早上 9:30：夜里到的信刚分拣好
TIMEOUT = 120
REAL_TIME = time.time   # 等子进程用真的时钟


class Failed(Exception):
    pass


def iso(t: datetime.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def at(minutes: int) -> datetime.datetime:
    return T0 + datetime.timedelta(minutes=minutes)


class Clock:
    """停住的现在。`time.monotonic` 不动：等待、超时照常走。"""

    def __init__(self, t: datetime.datetime):
        self.t = t.timestamp()

    def set(self, t: datetime.datetime) -> None:
        self.t = t.timestamp()

    def install(self) -> None:
        clock = self
        localtime, gmtime, strftime = time.localtime, time.gmtime, time.strftime
        real_datetime, real_date = datetime.datetime, datetime.date

        class Like(type):
            """`isinstance(x, datetime.date)` 对没换过的类的实例也算数。"""

            def __instancecheck__(cls, obj: Any) -> bool:
                return isinstance(obj, cls.__mro__[1])

        class FrozenDatetime(real_datetime, metaclass=Like):
            @classmethod
            def now(cls, tz: Any = None) -> datetime.datetime:
                return real_datetime.fromtimestamp(clock.t, tz)

            @classmethod
            def today(cls) -> datetime.datetime:
                return real_datetime.fromtimestamp(clock.t)

            @classmethod
            def utcnow(cls) -> datetime.datetime:
                return real_datetime.fromtimestamp(clock.t, UTC).replace(tzinfo=None)

        class FrozenDate(real_date, metaclass=Like):
            @classmethod
            def today(cls) -> datetime.date:
                return real_date.fromtimestamp(clock.t)

        time.time = lambda: clock.t
        time.time_ns = lambda: int(clock.t * 1_000_000_000)
        time.localtime = lambda secs=None: localtime(clock.t if secs is None else secs)
        time.gmtime = lambda secs=None: gmtime(clock.t if secs is None else secs)
        time.strftime = lambda fmt, t=None: strftime(fmt, localtime(clock.t) if t is None else t)
        datetime.datetime, datetime.date = FrozenDatetime, FrozenDate


# ---------------------------------------------------------------- 假数据
#
# 主题 → （发件人、几天前到的、List-Id、模型给的建议）。全是编造的，域名都是 .example。

MAILS: Dict[str, Tuple[str, float, Optional[str], Dict[str, Any]]] = {
    "答辩时间确认": ("王老师 <wang@school.example>", 0.3, None,
               {"action": "reply", "priority": "high", "category": "work", "summary": "王老师问答辩时间，周五前回",
                "reason": "要你定时间", "due_date": "2026-10-23", "reply_points": ["周三下午可以"]}),
    "项目周会": ("项目组 <team@corp.example>", 0.3, None,
             {"action": "schedule", "priority": "medium", "category": "work", "summary": "周四下午三点的周会", "reason": "会议邀请",
              "due_date": None, "event_title": "项目周会", "event_date": "2026-10-22", "event_start": "15:00", "event_end": "16:00",
              "reply_points": []}),
    "合同扫描件": ("李律师 <li@law.example>", 0.4, None,
              {"action": "follow_up", "priority": "medium", "category": "work", "summary": "律师发来合同扫描件", "reason": "等你看过再回",
               "due_date": None, "reply_points": []}),
    "本周精选": ("周刊 <news@weekly.example>", 0.5, "weekly.example",
             {"action": "archive", "priority": "low", "category": "newsletter", "summary": "周刊", "reason": "订阅",
              "due_date": None, "reply_points": []}),
    "双十一预告": ("某某商城 <news@shop.example>", 0.5, "promo.shop.example",
              {"action": "ignore", "priority": "low", "category": "promotion", "summary": "促销", "reason": "广告",
               "due_date": None, "reply_points": []}),
    "周末促销": ("某某商城 <news@shop.example>", 0.6, "promo.shop.example",
             {"action": "ignore", "priority": "low", "category": "promotion", "summary": "促销", "reason": "广告",
              "due_date": None, "reply_points": []}),
    "新品上架": ("某某书店 <news@books.example>", 0.6, "promo.books.example",
             {"action": "ignore", "priority": "low", "category": "promotion", "summary": "促销", "reason": "广告",
              "due_date": None, "reply_points": []}),
    "账户已冻结": ("某银行 <sec@bank-verify.example>", 0.2, None,
              {"action": "ignore", "priority": "high", "category": "phishing", "summary": "冒充银行要你登录验证",
               "reason": "发件域名仿冒银行", "due_date": None, "suspicious": True, "risks": ["冒充", "可疑链接"], "reply_points": []}),
}
DRAFT = "王老师好，周三下午可以。"


def model(body: dict) -> Tuple[int, str]:
    """假模型：分拣照 `MAILS` 里写的答，写草稿答固定的一段。"""
    from fakes import suggestion
    text = json.dumps(body.get("messages"), ensure_ascii=False)
    if "起草一封回复邮件" in text:
        return 200, json.dumps({"body": DRAFT}, ensure_ascii=False)
    for subject, m in MAILS.items():
        if subject in text:
            return 200, suggestion(**m[3])
    return 200, suggestion("archive", priority="low", category="other", summary="其他", reason="", due_date=None, reply_points=[])


# ---------------------------------------------------------------- 一个场景怎么录


def words(comps: Dict[str, dict], cid: str) -> str:
    """按钮上的字：子组件的字，或者图标旁边那段字（工具协议 §4「图标按钮」）。"""
    child = comps.get(comps[cid].get("child")) or {}
    if child.get("component") == "Row":
        return next((comps[k].get("text", "") for k in child.get("children", []) if (comps.get(k) or {}).get("component") == "Text"), "")
    return child.get("text", "")


class Recorder:
    """一个场景：一个邮件助手，一步一步问。造数据时做的记进 `produced`，之后的每个请求记进 `steps`。"""

    def __init__(self, name: str, clock: Clock, work: str):
        self.name, self.clock, self.work = name, clock, work
        self.now = T0
        self.rid = 0
        self.records: List[Tuple[str, dict]] = []
        self.produced: List[str] = ["LUOKE_MAIL_HOME 指到新建的临时目录；假邮箱是 tests/fakes.py 的 FakeIMAP（gmail），"
                                    "假模型是 FakeChat，分拣和草稿的答复写在 scripts/record_agent_fixtures.py 的 `MAILS` 里"]
        self.steps: List[dict] = []
        self.surfaces: Dict[str, Any] = {}
        self.outbox_cards: Dict[str, Any] = {}
        self.outbox: List[dict] = []
        self.cleanups: List[Callable[[], None]] = []
        import agent_a2ui
        from fakes import FakeChat, FakeIMAP
        from test_background import FakeDesktop
        from test_core import make_mail
        from luoke_mail import agent_mcp, server
        from luoke_mail.credstore import FileStore
        from luoke_mail.mailbox import Mailbox
        from luoke_mail.store import Store
        self.a2ui, self.make_mail, self.agent_mcp = agent_a2ui, make_mail, agent_mcp
        self.srv = FakeIMAP([], flavour="gmail").__enter__()
        self.cleanups.append(lambda: self.srv.__exit__(None, None, None))
        self.chat = FakeChat(model).__enter__()
        self.cleanups.append(lambda: self.chat.__exit__(None, None, None))
        self.app = a = server.App(secret_store=FileStore(os.path.join(work, "secrets.json")),
                                  state_store=Store(os.path.join(work, "state.db")), desktop_=FakeDesktop())
        self.cleanups.append(a.store.close)
        a.open_mailbox = lambda *_: Mailbox("gmail", ME, "secret", host="127.0.0.1", port=self.srv.port, ssl=False, timeout=5)
        a.cfg["engine"] = {"base_url": self.chat.base_url, "model": "test-model", "engine_name": None, "has_api_key": False}
        a.cfg["mode"]["write"] = True
        a.agent.start()
        self.cleanups.append(a.agent.close)
        self.sock = a.agent.server._socket_path
        self.n = 0

    # 造数据

    def connect(self) -> None:
        self.app.cfg["accounts"] = [{"provider": "gmail", "address": ME, "host": None, "port": 993}]
        from luoke_mail import server
        self.app.secrets.set(server.MAIL_SECRET, ME, "secret")
        self.produced.append(f"连上了假邮箱 {ME}（Gmail，处理模式开着）")

    def deliver(self, *subjects: str) -> None:
        for s in subjects:
            sender, days, list_id, _ = MAILS[s]
            self.n += 1
            raw = self.make_mail(s, f"正文 {self.n}", sender=sender, days_ago=days)
            raw = raw.replace(b"<abc123@corp.example>", f"<m{self.n}@x.example>".encode())
            if list_id:
                raw = raw.replace(b"Subject:", f"List-Id: <{list_id}>\r\nSubject:".encode(), 1)
            self.srv._add("INBOX", raw, set())
        self.produced.append("收件箱里放了：" + "、".join(subjects))

    def habit(self, text: str, sender: str, category: str, name: str) -> None:
        """用户确认过的处理习惯，经 luoke-mem 的接口记成一条（邮件助手下一次同步时自己做）。"""
        from luoke_mail import memory
        mem = self.app.mem
        rid = mem.record("user", "activity", f"你把「{name}」的邮件归档了", topic="mail-acted")
        nid = mem.propose("preference", text, [rid], data={"account": memory.account_key(ME), "sender": sender,
                                                         "category": category, "action": "archive", "name": name})
        mem.confirm(nid)
        self.produced.append(f"记忆里有一条用户确认过的习惯：「{text}」")

    def rule(self, kind: str, value: str, action: str) -> None:
        self.app.store.add_rule(ME, kind, value, action)
        self.produced.append(f"有一条规则：{kind} {value} → {action}")

    def sync(self) -> None:
        self.clock.set(T0)
        self.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        self.produced.append(f"{iso(T0)} 同步了一次（App.sync_mailbox：真的读信、分拣、做习惯里的事）")

    # 时钟、请求

    def at(self, t: datetime.datetime) -> None:
        self.now = t
        self.clock.set(t)

    def step(self, what: str, file: str) -> None:
        self.steps.append({"clock": iso(self.now), "do": what, "file": file})

    @staticmethod
    def message(method: str, params: Optional[dict], user_action: Optional[dict], rid: int) -> dict:
        meta: Dict[str, Any] = {"io.modelcontextprotocol/protocolVersion": PROTOCOL,
                                "io.modelcontextprotocol/clientInfo": {"name": "luoke-agent", "version": "fixtures"},
                                "io.modelcontextprotocol/clientCapabilities": CLIENT_CAPABILITIES}
        if user_action:
            meta[USER_ACTION] = user_action
        return {"jsonrpc": "2.0", "id": rid, "method": method, "params": {**(params or {}), "_meta": meta}}

    def post(self, message: dict) -> dict:
        """经 agent.sock 发一条，和 agent 一样：HTTP/1.1，`POST /mcp`。"""
        conn = self.agent_mcp._UnixConnection(self.sock, 10.0, 300.0)
        try:
            conn.request("POST", "/mcp", json.dumps(message, ensure_ascii=False).encode("utf-8"),
                         {"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        if resp.status != 200:
            raise Failed(f"{self.name}：{message['method']} 答 HTTP {resp.status}")
        return json.loads(body)

    def request(self, file: str, what: str, method: str, params: Optional[dict] = None,
                user_action: Optional[dict] = None) -> dict:
        if any(f == file for f, _ in self.records):
            raise Failed(f"{self.name}：{file} 录了两次")
        self.rid += 1
        req = self.message(method, params, user_action, self.rid)
        reply = self.post(req)
        if "error" in reply:
            raise Failed(f"{self.name}：{method} 出错 {reply['error']}")
        shown = json.loads(json.dumps(reply))
        for c in (shown.get("result") or {}).get("contents") or []:
            if c.get("mimeType") == "application/json" and isinstance(c.get("text"), str):
                c["text"] = json.loads(c["text"])
        self.records.append((file, {"request": req, "reply": shown}))
        self.step(what, file)
        return reply["result"]

    def surface_id(self, card: List[dict]) -> str:
        return card[0]["createSurface"]["surfaceId"]

    def read(self, uri: str, file: str, what: str) -> Any:
        result = self.request(file, what, "resources/read", {"uri": uri})
        value = json.loads(result["contents"][0]["text"])
        if uri == OUTBOX:
            self.outbox = value["items"]
            for it in self.outbox:
                # 照 agent：outbox 里的卡片和上次读到的一字不差就不重画，点按答复换上的那张留着
                sid = self.surface_id(it["card"])
                if sid not in self.surfaces or self.outbox_cards.get(sid) != it["card"]:
                    self.surfaces[sid] = self.a2ui.build(it["card"])
                self.outbox_cards[sid] = it["card"]
        elif uri.startswith("page/"):
            self.surfaces[uri[len("page/"):]] = self.a2ui.build(value)
        return value

    def pages(self, suffix: str = "") -> None:
        for page in PAGES:
            self.read("page/" + page, f"read-page-{page}{suffix}.json", f"读基础页 page/{page}")

    def snapshot(self, suffix: str = "", *, static: bool = True) -> None:
        """agent 连上一个工具、打开它的页时读的那些。"""
        if static:
            self.request("server-discover.json", "server/discover", "server/discover")
            self.request("tools-list.json", "列工具（agent 认 readOnlyHint）", "tools/list")
            self.request("resources-list.json", "列资源", "resources/list")
        self.read(OUTBOX, f"read-outbox{suffix}.json", "读 outbox（每分钟一次）")
        self.read("summary", f"read-summary{suffix}.json", "读概览（侧栏的计数和一句话）")
        self.read("permissions", f"read-permissions{suffix}.json", "读权限的状态")
        self.pages(suffix)

    def item(self, kind: str) -> dict:
        for it in self.outbox:
            if it["kind"] == kind:
                return it
        raise Failed(f"{self.name}：outbox 里没有 {kind}；有 {[i['kind'] for i in self.outbox]}")

    def button(self, surface: Any, event: str, label: str) -> str:
        comps = surface.components
        for cid in self.a2ui._reachable(comps):
            c = comps[cid]
            if c.get("component") == "Button" and c["action"]["event"]["name"] == event and label in words(comps, cid):
                return cid
        raise Failed(f"{self.name}：{surface.surface_id} 上没有 {event}（{label}）")

    def click(self, file: str, what: str, *, event: str, label: str = "", item: Optional[dict] = None, on: Optional[str] = None,
              page: Optional[str] = None, choose: Optional[Dict[str, str]] = None) -> dict:
        """用户在核心手上的卡片或基础页上点了一个按钮：`on` 是卡片编号（点完答复换上的那张还叫它）；上下文照
        agent 的 `a2ui.merge_context` 换好，带上 user-action 转给工具。"""
        sid = page or on or self.surface_id((item or {}).get("card") or [{}])
        surface = self.surfaces.get(sid)
        if surface is None:
            raise Failed(f"{self.name}：手上没有 {sid}")
        for key, value in (choose or {}).items():
            surface.data["choices"][key] = [value]
        cid = self.button(surface, event, label)
        name, declared = surface.button_event(cid)
        args = self.a2ui.merge_context(declared, {}, surface.data)
        ua = {"surfaceId": sid, "componentId": cid, "at": self.now.astimezone(SHANGHAI).isoformat(timespec="seconds")}
        result = self.request(file, what, "tools/call", {"name": name, "arguments": args}, user_action=ua)
        if result.get("isError"):
            raise Failed(f"{self.name}：{file} 没做成：{result['content'][0]['text']}")
        card = (result.get("structuredContent") or {}).get("card")
        if card and page is None:
            self.surfaces[sid] = self.a2ui.update(surface, card)   # agent 用答复的卡片换掉原来那张；基础页不画，重新读
        return result

    def close(self) -> None:
        for fn in reversed(self.cleanups):
            fn()


# ---------------------------------------------------------------- 场景


def no_mailbox(r: Recorder) -> None:
    r.at(T0)
    r.snapshot()


def new_mail(r: Recorder) -> None:
    r.connect()
    r.deliver("答辩时间确认", "项目周会", "合同扫描件", "本周精选", "双十一预告")
    r.sync()
    r.at(T0)
    r.snapshot()
    card = r.item("new-mail")
    r.at(at(5))
    r.click("call-mail_apply_plan.json", "用户照默认的选项点「要你决定」那张的「写草稿 1」", event="mail_apply_plan", item=card)
    r.read(OUTBOX, "read-outbox-after-apply.json", "点完再读 outbox")
    r.read("summary", "read-summary-after-apply.json", "点完再读概览")
    r.pages("-after-apply")
    r.at(at(6))
    r.click("call-mail_undo.json", "用户在结果卡片上点「撤销」", event="mail_undo", on=r.surface_id(card["card"]))
    r.read(OUTBOX, "read-outbox-after-undo.json", "撤销完再读 outbox")
    r.read("summary", "read-summary-after-undo.json", "撤销完再读概览")
    r.pages("-after-undo")
    r.at(at(7))
    r.click("call-mail_apply_plan-tidy.json", "用户在「邮箱」卡片的「可以整理」点「归档 1 · 删除 1」", event="mail_apply_plan",
            label="归档", item=r.item("mailbox"))
    r.read(OUTBOX, "read-outbox-after-tidy.json", "整理完再读 outbox")


def suspicious(r: Recorder) -> None:
    r.connect()
    r.deliver("账户已冻结", "答辩时间确认")
    r.sync()
    r.at(T0)
    r.snapshot()
    card = r.item("suspicious")
    r.at(at(2))
    r.request("call-outbox_ack-shown.json", "agent 把急事说给用户以后记一下（outbox_ack shown）", "tools/call",
              {"name": "outbox_ack", "arguments": {"id": card["id"], "state": "shown"}})
    r.click("call-mail_apply_plan.json", "用户在急事卡片上点「移到垃圾邮件」", event="mail_apply_plan", label="移到垃圾邮件", item=card)
    r.read(OUTBOX, "read-outbox-after-apply.json", "点完再读 outbox")
    r.read("summary", "read-summary-after-apply.json", "点完再读概览")
    r.pages("-after-apply")


def auto_done(r: Recorder) -> None:
    r.connect()
    r.habit("某某商城的推广邮件都归档", "list:promo.shop.example", "promotion", "某某商城")
    r.habit("某某书店的推广邮件都归档", "list:promo.books.example", "promotion", "某某书店")
    r.rule("sender", "news@weekly.example", "archive")
    r.deliver("周末促销", "新品上架", "答辩时间确认")
    r.sync()
    r.at(at(1))
    r.snapshot()
    card = r.item("auto-done")
    r.at(at(10))
    r.click("call-mail_undo-card.json", "用户在自动处理的卡片上点第一组的「撤销」", event="mail_undo", item=card)
    r.read(OUTBOX, "read-outbox-after-undo-card.json", "点完再读 outbox（还剩一组）")
    r.read("summary", "read-summary-after-undo-card.json", "点完再读概览")
    r.pages("-after-undo-card")
    r.at(at(11))
    r.click("call-mail_do-page.json", "用户在「处理记录」页上点另一组的「撤销」", event="mail_do", label="撤销", page="activity")
    r.read("page/activity", "read-page-activity-after-undo-page.json", "点完窗口重新读这一页")
    r.read(OUTBOX, "read-outbox-after-undo-page.json", "点完再读 outbox（都撤了，这件撤下）")
    r.read("summary", "read-summary-after-undo-page.json", "点完再读概览")


def search(r: Recorder) -> None:
    r.connect()
    r.deliver("答辩时间确认", "项目周会", "账户已冻结")
    r.sync()
    r.at(T0)
    r.snapshot()
    r.at(at(3))
    r.request("call-mail_search.json", "用户问螺壳「答辩」：搜索框调 mail_search（只读，参数照 agent 的 `search`）", "tools/call",
              {"name": "mail_search", "arguments": {"query": "答辩", "limit": 10}})


class Scenario:
    def __init__(self, sid: str, title: str, situation: str, window: List[str], run: Callable[[Recorder], None]):
        self.id, self.title, self.situation, self.window, self.run = sid, title, situation, window, run


SCENARIOS = [
    Scenario("no-mailbox", "还没连邮箱",
             "刚装好，还没连邮箱。邮件助手在运行，要等用户在自己的页面里填授权码。",
             ["「今天 · 要你决定」里一张「还没连邮箱」，按钮「去设置」（`mail_open_page`，`{\"page\": \"accounts\"}`，打开邮件助手自己的页面）。",
              "应用页的 `needs` 里有 `mailbox`；权限页这一行 `ask`，能「打开」，不能「移除」。",
              "概览写「还没连邮箱」，计数 0；三页都是一句话加「去设置」。"],
             no_mailbox),
    Scenario("new-mail", "新邮件分拣好了",
             "夜里到了五封：一封要回、一封周会、一封律师发来的合同，一封周刊打算归档，一封促销打算删除。早上分拣好了。",
             ["两件（luoke-mail#81，「今天」的新模板）：`new-mail`、`normal`，在「要你决定」：只放优先级高、没过期限的（这里是王老师那封），"
              "标题「1 封要处理」后面一个邮箱地址的标签，一行发件人（加粗）、主题和截止的标签，选择框带图标；主按钮写清做什么（「写草稿 1」），"
              "旁边「查看全部 3 封」（`agent.open_page` 到 `inbox`，`args` 是 `box` 和 `list: needs`，收件箱切到这个邮箱、开着「需要你处理」）。",
              "`mailbox`、`low`，在「顺便看看」：一个邮箱一张「邮箱」，分块：「待处理」（周会、合同，每封一个选择框；周会那行右边一个「10-22 15:00」"
              "加入日程的按钮，`agent.open_url`，悬停提示「加入日程」），「可以整理」（一句「多是…」，按钮「归档 1 · 删除 1」「先不动」）。"
              "有较早的可疑邮件时还有「判为可疑」一块，`collapsed` 先收起。每块的按钮上下文带 `part`，只做这一块。",
              "点「写草稿 1」把 `/choices` 换成当时的选项转给 `mail_apply_plan`：答复带 `done: true`，卡片换成结果和 [撤销]（结果只有一种时 root 带 `group`，"
              "「已经替你做了」里同一个邮箱、同一种结果合成一行），这一件从 outbox 撤下。",
              "点 [撤销]（`mail_undo`，参数是方案编号）：邮件回到收件箱，答复带 `done: true`。同一批不再出方案（用户答过了），"
              "所以概览还数着它们、outbox 里没有，见下面「要对齐的」。",
              "「邮箱」卡片上点「归档 1 · 删除 1」（上下文 `{\"plan_id\", \"part\": \"tidy\", \"choices\": {\"tidy\": [\"suggested\"]}}`）：只整理这一块，"
              "卡片换成结果和 [撤销]；下一次读 outbox，「邮箱」卡片重出，只剩「待处理」。"],
             new_mail),
    Scenario("suspicious", "可疑邮件",
             "一封冒充银行的邮件被判成可疑，同时来了一封要回的信。",
             ["一件 `suspicious`、`urgent`：急事横幅，按「主动提醒」的设置发通知。卡片标题「可疑邮件」后面一个邮箱地址的标签，一行发件人（加粗）和主题，"
              "一行理由；每个选项一个按钮，只有「移到垃圾邮件」（`destructive`，浅红底）「先不动」，永远不给退订；root 带 `subtitle`（发件人 · 主题）。",
              "`outbox_ack` `shown` 只是记账：agent 说过了，邮件助手不再发自己的通知。",
              "点了以后答复带 `done: true`，这一件从 outbox 撤下，结果卡的 root 带 `group`（`<邮箱>/spam`）；概览的「可疑」减一。"],
             suspicious),
    Scenario("auto-done", "按习惯自动处理了",
             "用户确认过两条处理习惯（两家店的推广邮件都归档），另有一条规则。同步时两家各来一封推广，邮件助手自己归档了，又来了一封要回的信。",
             ["一件 `auto-done`、`normal`，在「已经替你做了」：每组一行说明和 [撤销]（`mail_undo`，参数 `auto:<记录>`）。",
              "卡片上撤一组：答复换上只剩另一组的卡片（`done: false`）；这件还在 outbox 里，标题的数字减一。",
              "「处理记录」页上撤（`mail_undo`，参数 `log:<记录>`，`surfaceId` 是页 id `activity`）：答复里的卡片不画，窗口重新读这一页，"
              "那一行写「已撤销」，按钮没了；两组都撤了，这件从 outbox 撤下。",
              "「规则」页列着两条习惯和一条规则，「编辑规则」打开邮件助手自己的页面。"],
             auto_done),
    Scenario("search", "问螺壳搜邮件",
             "收件箱里有几封信，用户在问螺壳里搜「答辩」。",
             ["`mail_search` 答 `hits`（工具协议 §1b），`card` 是空的：窗口的 `HitsList` 做好之前，agent 的 `_search_one` 不画这个来源，见下面「要对齐的」。",
              "每条的 `actions` 是 `mail_show`（打开这封），`source` 是邮箱地址。"],
             search),
]


# ---------------------------------------------------------------- 换掉随机的、跟机器有关的


HASH = re.compile(r"(?<=[/-])[0-9a-f]{16}(?![0-9A-Za-z])")


def normalise(r: Recorder, home: str) -> Dict[str, str]:
    """场景里每个文件的文字：路径换成占位，方案编号按先后编号（同一个场景里前后对得上），版本号换成 $VERSION。"""
    spellings = {r.work, home}
    spellings |= {p[len("/private"):] for p in spellings if p.startswith("/private/")}
    pairs = [(p, "~/.luoke-mail" if p.endswith(home) else "$TMPDIR") for p in sorted(spellings, key=len, reverse=True)]
    mapping: Dict[str, str] = {}

    def renumber(m: re.Match) -> str:
        return mapping.setdefault(m.group(0), f"{len(mapping) + 1:012d}")

    out: Dict[str, str] = {}
    for file, obj in r.records:
        if file == "server-discover.json":
            obj["reply"]["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["version"] = "$VERSION"
        text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
        for real, placeholder in pairs:
            text = text.replace(real, placeholder)
        out[file] = HASH.sub(renumber, text) + "\n"
    return out


GENERIC_USERS = {"admin", "user", "test", "dev", "root", "fixture", "app", "mac"}


def leaks(text: str) -> List[str]:
    """不该出现在样例里的：真实的家目录、临时目录、这台 Mac 的用户名（整个词）。"""
    bad = [b for b in ("/Users/", "/private/var/folders", "/var/folders", "/private/tmp", "/tmp/") if b in text]
    user = getpass.getuser()
    if len(user) >= 3 and user.lower() not in GENERIC_USERS and re.search(r"(?<![A-Za-z0-9])" + re.escape(user) + r"(?![A-Za-z0-9])", text):
        bad.append("用户名")
    return bad


def scenario_json(sc: Scenario, r: Recorder) -> dict:
    return {
        "id": sc.id,
        "title": sc.title,
        "situation": sc.situation,
        "window": sc.window,
        "produced": {
            "fixture": r.produced,
            "changes": [],
            "mcp": "真的 App 和 agent.sock（luoke_mail/agent.py），一个常驻的服务；时钟由录制脚本的 `Clock` 停住",
            "tz": TZ,
            "user_action_at": "点按时的 at 是那一步的时钟，按 Asia/Shanghai 写",
        },
        "steps": r.steps,
        "normalised": {
            "000000000001…": "方案编号（new-mail-…、suspicious-…、auto-done-… 后面的 16 位十六进制），按在本场景里出现的先后编号",
            "$VERSION": "邮件助手的版本号（server/discover、manifest.json）：每次发版都变，比对时不看",
            "~/.luoke-mail": "LUOKE_MAIL_HOME（临时目录）",
            "$TMPDIR": "录的时候的临时目录",
            "resources/read": "contents[0].text 解开成了 JSON 值；线上是 JSON 文字",
        },
    }


def record_one(name: str) -> Dict[str, str]:
    """子进程：录一个场景。环境（LUOKE_MAIL_HOME、TZ、HOME）由 `spawn` 给好；先停住时钟，再导入邮件助手。"""
    clock = Clock(T0)
    clock.install()
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    sc = next(s for s in SCENARIOS if s.id == name)
    home = os.environ["LUOKE_MAIL_HOME"]
    r = Recorder(sc.id, clock, os.path.dirname(home))
    try:
        sc.run(r)
        files = normalise(r, home)
        with open(MANIFEST, encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["version"] = "$VERSION"
        files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        files["scenario.json"] = json.dumps(scenario_json(sc, r), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        for file, text in files.items():
            bad = leaks(text)
            if bad:
                raise Failed(f"{sc.id}/{file} 里有不该出现的：{bad}")
        return files
    finally:
        r.close()


def spawn(name: str, keep: bool) -> Dict[str, str]:
    """起一个进程录一个场景。socket 的路径有长度上限，所以临时目录放在 /tmp 下。"""
    work = os.path.realpath(tempfile.mkdtemp(prefix="lmf", dir="/tmp"))
    home = os.path.join(work, "home")
    not_home = os.path.join(work, "not-home")
    os.makedirs(home)
    os.makedirs(not_home)
    env = {k: v for k, v in os.environ.items() if not k.startswith("LUOKE_")}
    env.update(TZ=TZ, HOME=not_home, LUOKE_MAIL_HOME=home, LUOKE_HUB_SOCKET="/nonexistent/luoke-hub-for-fixtures.sock",
               LUOKE_HUB_APPS="/nonexistent/luoke-hub-apps-for-fixtures", PYTHONDONTWRITEBYTECODE="1")
    result = os.path.join(work, "result.json")
    try:
        p = subprocess.run([sys.executable, "-B", os.path.abspath(__file__), "--one", name, "--to", result], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=TIMEOUT, cwd=work)
        if p.returncode != 0:
            raise Failed(f"{name}：退出码 {p.returncode}\n{p.stdout[-3000:]}")
        with open(result, encoding="utf-8") as f:
            return json.load(f)
    except subprocess.TimeoutExpired:
        raise Failed(f"{name}：{TIMEOUT} 秒没录完") from None
    finally:
        if keep:
            print(f"{name}：假数据留在 {work}", file=sys.stderr)
        else:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- 汇总（README 由这里生成）


def section(item: dict) -> str:
    """照 luoke-agent 现在的分段规则（agent.py 的 `_section`）：auto-done → 已经替你做了，urgent → 急事，
    needs-access → 要你决定，low → 顺便看看，别的 → 要你决定。"""
    if item["kind"] == "auto-done":
        return "已经替你做了（done）"
    if item["urgency"] == "urgent":
        return "急事（urgent）"
    if item["kind"] == "needs-access":
        return "要你决定（decide）"
    if item["urgency"] == "low":
        return "顺便看看（later）"
    return "要你决定（decide）"


def card_lines(msgs: List[dict]) -> List[str]:
    comps: Dict[str, dict] = {}
    data: Dict[str, Any] = {}
    for m in msgs:
        if "updateComponents" in m:
            comps.update({c["id"]: c for c in m["updateComponents"]["components"]})
        if "updateDataModel" in m:
            data = m["updateDataModel"]["value"]
    out: List[str] = []

    def label(b: dict) -> str:
        return words(comps, b["id"])

    def walk(cid: str) -> None:
        c = comps.get(cid) or {}
        kind = c.get("component")
        kids = c.get("children", [])
        if kind == "Row" and all((comps.get(x) or {}).get("component") == "Button" for x in kids):
            out.append("按钮：" + "、".join(f"「{label(comps[b])}」→ `{comps[b]['action']['event']['name']}`" for b in kids))
        elif kind in ("Column", "Row", "List"):
            if c.get("collapsed"):
                out.append(f"（收起：{c['collapsed']}）")
            for x in kids:
                walk(x)
        elif kind == "Button":
            out.append(f"按钮：「{label(c)}」→ `{c['action']['event']['name']}`")
        elif kind == "Text":
            t = c["text"].replace("\n", " / ")
            out.append(("**" + t + "**") if c.get("variant") == "h4" else t)
        elif kind == "ChoicePicker":
            key = c["value"]["path"].rsplit("/", 1)[-1]
            picked = (data.get("choices") or {}).get(key, ["?"])[0]
            out.append(f"选项：{c['label']}（{'/'.join(o['value'] for o in c['options'])}，默认 {picked}）")
    walk("root")
    return out


def describe(sc: Scenario, files: Dict[str, str], scenario: dict) -> str:
    def load(name: str) -> Any:
        return json.loads(files[name])

    def resource(name: str) -> Any:
        return load(name)["reply"]["result"]["contents"][0]["text"]

    lines = [f"## `{sc.id}`：{sc.title}", "", sc.situation, ""]
    lines += ["**怎么录的**：" + "；".join(scenario["produced"]["fixture"]) + "。", ""]
    summary, perms = resource("read-summary.json"), resource("read-permissions.json")
    lines += ["**邮件助手报的**", "", f"- 概览（`summary`）：count {summary['count']}，「{summary['text']}」",
              "- 权限（`permissions`）：" + "、".join(f"`{k}` {v}" for k, v in sorted(perms.items()))]
    items = resource("read-outbox.json")["items"]
    lines.append(f"- outbox：{len(items)} 件")
    for it in items:
        lines.append(f"  - `{it['id']}`：`{it['kind']}`、`{it['urgency']}` → {section(it)}")
        lines.append(f"    - 标题：{it['title']}")
        lines.append(f"    - 摘要：{it['summary']}")
        lines.append(f"    - 卡片（surface `{it['card'][0]['createSurface']['surfaceId']}`）：")
        lines += [f"      - {x}" for x in card_lines(it["card"])]
    for page in PAGES:
        lines.append(f"- 基础页 `page/{page}`：")
        lines += [f"  - {x}" for x in card_lines(resource(f"read-page-{page}.json"))]
    lines.append("")
    lines += ["**窗口上**", ""] + [f"- {w}" for w in sc.window] + [""]
    lines += ["**文件**（按请求的先后）", "", "| 文件 | 请求 | 什么时候 |", "|---|---|---|"]
    for st in scenario["steps"]:
        req = load(st["file"])["request"]
        p = req["params"]
        what = req["method"] + (f" `{p['uri']}`" if "uri" in p else f" `{p['name']}`" if "name" in p else "")
        if USER_ACTION in p["_meta"]:
            what += "（带 user-action）"
        lines.append(f"| `{st['file']}` | {what} | {st['clock']}：{st['do']} |")
    for st in scenario["steps"]:
        if st["file"].startswith("call-"):
            res = load(st["file"])["reply"]["result"]
            sc_ = res.get("structuredContent") or {}
            tail = [f"done: {str(sc_['done']).lower()}"] if "done" in sc_ else []
            if sc_.get("card"):
                tail.append("新卡片" if "createSurface" in sc_["card"][0] else "更新原卡片")
            said = res["content"][0]["text"].replace("\n", " / ")
            lines += ["", f"`{st['file']}` 答：「{said}」" + ("（" + "，".join(tail) + "）" if tail else "")]
    return "\n".join(lines) + "\n"


HEADER = """# 给 agent 的场景样例

邮件助手（luoke-mail）对 agent 那一路（工具协议版本 1：MCP、outbox、A2UI 卡片、基础页、权限）在几种用户处境下**真实**的答复。
都是 `scripts/record_agent_fixtures.py` 在假数据上起真的服务录的，不是手写的：agent 拿它们当样例数据和测试的输入，就是两边之间的
接口说明。代码改了 agent 看到的东西，`--check` 就不过，要重录、提交，并且在 PR 里说清 agent 那边要不要跟着改。

一个场景一个进程：`LUOKE_MAIL_HOME` 指到新建的临时目录，真的 `App` 和 `agent.sock`，请求经那条 socket 发；假邮箱是
`tests/fakes.py` 的 FakeIMAP，假模型是 FakeChat（答复写在录制脚本的 `MAILS` 里，不连真模型），时钟和时区固定。

**本文件也是录的时候生成的**，不要手改；要改说明，改 `scripts/record_agent_fixtures.py` 里的场景。

## 重录、核对

```bash
python3 scripts/record_agent_fixtures.py            # 重录，写进 fixtures/agent/（旁边要有 luoke-mem）
python3 scripts/record_agent_fixtures.py --check    # 重录进临时目录和这里比，不一样就列出差别、退出码 1
python3 scripts/record_agent_fixtures.py --only new-mail --keep   # 只录一个场景，留下假数据看看
```

`tests/test_agent_fixtures.py` 跑 `--check`，所以 `python3 -m unittest discover -s tests` 里就有这一步。

## 每个场景里有什么

一个场景一个文件夹，一个请求一个文件，名字照请求起：

| 文件 | 请求 |
|---|---|
| `manifest.json` | 配件清单 `packaging/accessory.json`（主控经 `GET /accessories` 交给 agent 的那份，`capabilities` 在里面） |
| `scenario.json` | 这个场景：处境、怎么录的（假数据、时钟、时区、每一步）、窗口上应该是什么样 |
| `server-discover.json` | `server/discover` |
| `tools-list.json` | `tools/list`（`annotations.readOnlyHint`、只在 Mac 上的 `_meta.ai.luoke/where`） |
| `resources-list.json` | `resources/list` |
| `read-outbox*.json` | `resources/read` `luoke://luoke-mail/outbox`（agent 每分钟读） |
| `read-summary*.json` | `resources/read` `summary` |
| `read-permissions*.json` | `resources/read` `permissions` |
| `read-page-<页>*.json` | `resources/read` `page/<页>`：今日处理、收件箱、清理、精选、订阅体检、规则、处理记录、设置（`window/pages`） |
| `call-<工具>*.json` | `tools/call`：卡片、页上的点按（带 `ai.luoke/user-action`），或只读的调用 |

带 `-after-…` 的是点完以后再读的。每个文件是 `{"request": …, "reply": …}`：`request` 是 agent 发的那条 JSON-RPC
（`_meta` 里照 agent 带协议版本、`clientInfo`、`clientCapabilities`，点按的再带 `ai.luoke/user-action`），`reply` 是邮件助手的答复。
JSON 按键排序、缩进两格。

**和线上不一样的地方**（重放时要换回去）：

- `resources/read` 的 `contents[0].text` 在线上是一段 JSON **文字**（`mimeType: application/json`），这里解开成了 JSON 值；
  重放时 `json.dumps` 回去。
- 方案编号（`new-mail-…`、`suspicious-…`、`auto-done-…` 后面的 16 位十六进制）按在场景里出现的先后换成 `000000000001`、
  `000000000002`……；同一个场景里前后对得上（outbox 里的方案编号就是点按时发的那个），不同场景之间不对应。
- 版本号换成 `$VERSION`（`server/discover` 的 `serverInfo`、`manifest.json` 的 `version`）：邮件助手几乎每个 PR 升一次版本，
  不换的话每个 PR 都要重录，看不出 agent 看到的东西有没有变。
- 路径：`LUOKE_MAIL_HOME` 是 `~/.luoke-mail`，别的临时目录是 `$TMPDIR`。
- 时钟停在固定的时间（各场景的 `scenario.json`），时区是 Asia/Shanghai：`created` 这类写的是上海的本机时间，不带时区。

## 重放时要知道的

- agent 要问的：`tools/list`（认 `readOnlyHint`）、`luoke://luoke-mail/outbox`、`summary`、`permissions`；打开应用页时读
  `page/<页>`。`server/discover`、`resources/list` 邮件助手都答，agent 现在不一定问。
- 会改动东西的（清单 `actions` 里的那几个）只认带 `ai.luoke/user-action`（`surfaceId` 是方案编号或页 id、`componentId`、`at`）的调用，
  没带的答 `isError: true`、`structuredContent.code: "needs_user_action"`。`outbox_ack` 是只读的记账。
- 按钮的 `event.context` 里的 `{"path": "/choices"}` 换成点的时候卡片数据模型里的值，方案编号这类写死的值照卡片上的。
- `mail_undo` 的 `execution_id` 有三种：方案编号（方案的结果卡片上的）、`auto:<记录>`（自动处理的卡片上的）、`log:<记录>`（「处理记录」页上的）。
- 点按的答复：`content[0].text` 一句话；`structuredContent.card` 是**更新原卡片**的消息（没有 `createSurface`）；`done: true` 表示
  办完了。基础页上点的，答复里的卡片不画，窗口重新读那一页。
- 没录的：`mail_open`、`mail_open_settings`、`mail_open_page`、`mail_show`、`mail_open_calendar`、`mail_disconnect`（都打开真的
  浏览器或「日历」，答一句「已在浏览器里打开……」，没有卡片）；`mail_read`、`mail_plan`（对话里用的；`mail_plan` 答一张新卡片）。
- 「今天」里分在哪一段，下面照 luoke-agent 现在的分段规则（`luoke_agent/agent.py` 的 `_section`）写的，不是邮件助手定的。

## 要对齐的

- **`count` 和 outbox 可能对不上**：撤销以后邮件回到收件箱，概览和「邮箱」页照实数着它们；同一批邮件用户答过了，不再出方案，outbox 里
  没有这件（`new-mail` 的 `-after-undo`）。有新邮件来才一起出新方案。
- **搜索的结果窗口还画不出来**：`mail_search` 答 `hits`、`card` 是空的（工具协议 §1b，`card` 只是后备）；agent 的 `_search_one`
  没有卡片就不画，要等窗口的 `HitsList`。
- **`created` 没带时区**：邮件的写收到的时间，`needs-access`、`auto-done` 和没有收到时间的写这一件出来的时间，都是
  `2026-10-20T03:42:00` 这样。工具协议写「不带时区的按本机时间」，邮件助手一直这样写。
- 邮件助手这边要改的，改了样例跟着变，agent 别照着它排版：`auto-done` 的摘要和每组的说明都写「按你的习惯自动归档 1 封」，
  分不出是哪条习惯（工具协议 §8a 要说按了哪几条），只有下面一行的主题看得出来。

## 场景
"""


def readme(recorded: Dict[str, Dict[str, str]]) -> str:
    parts = [HEADER]
    for sc in SCENARIOS:
        files = recorded[sc.id]
        parts.append(describe(sc, files, json.loads(files["scenario.json"])))
    return "\n".join(parts)


def record_all(names: List[str], keep: bool) -> Dict[str, Dict[str, str]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as pool:
        futures = {n: pool.submit(spawn, n, keep) for n in names}
        return {n: f.result() for n, f in futures.items()}


def write_tree(dest: str, recorded: Dict[str, Dict[str, str]], names: List[str]) -> None:
    for name in names:
        d = os.path.join(dest, name)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        for f, text in recorded[name].items():
            with open(os.path.join(d, f), "w", encoding="utf-8") as out:
                out.write(text)


def committed() -> Dict[str, str]:
    out = {}
    for dirpath, _, names in os.walk(OUT):
        for n in names:
            p = os.path.join(dirpath, n)
            with open(p, encoding="utf-8") as f:
                out[os.path.relpath(p, OUT)] = f.read()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="录给 luoke-agent 的场景样例（fixtures/agent/）")
    ap.add_argument("--check", action="store_true", help="重录进临时目录，和 fixtures/agent/ 比")
    ap.add_argument("--only", action="append", choices=[s.id for s in SCENARIOS], help="只录这个场景（可以给几次）")
    ap.add_argument("--keep", action="store_true", help="录完不删假数据")
    ap.add_argument("--one", help=argparse.SUPPRESS)   # 子进程：录这一个
    ap.add_argument("--to", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.one:
        files = record_one(a.one)
        with open(a.to, "w", encoding="utf-8") as f:
            json.dump(files, f, ensure_ascii=False)
        return 0
    names = a.only or [s.id for s in SCENARIOS]
    t = REAL_TIME()
    try:
        recorded = record_all(names, a.keep)
    except Failed as e:
        print(f"没录成：{e}", file=sys.stderr)
        return 2
    if a.only:
        # 只录了几个：README 用录好的和仓库里其余的拼
        old = committed()
        for sc in SCENARIOS:
            if sc.id not in recorded:
                recorded[sc.id] = {k.split("/", 1)[1]: v for k, v in old.items() if k.startswith(sc.id + "/")}
    new: Dict[str, str] = {f"{n}/{f}": text for n in names for f, text in recorded[n].items()}
    complete = all(recorded.get(sc.id) for sc in SCENARIOS)
    if complete:
        new["README.md"] = readme(recorded)
    if a.check:
        old = committed()
        keys = sorted(set(new) | {k for k in old if k == "README.md" and complete or k.split("/", 1)[0] in names})
        diff: List[str] = []
        for k in keys:
            if old.get(k) == new.get(k):
                continue
            if k not in old:
                diff.append(f"多了 {k}（仓库里没有）")
            elif k not in new:
                diff.append(f"少了 {k}（这次没录出来）")
            else:
                d = list(difflib.unified_diff(old[k].splitlines(), new[k].splitlines(), f"fixtures/agent/{k}（仓库里的）",
                                              f"fixtures/agent/{k}（这次录的）", n=2, lineterm=""))
                diff += d[:80] + ([f"…（{k} 还有 {len(d) - 80} 行差别）"] if len(d) > 80 else [])
        if diff:
            print("\n".join(diff))
            print(f"\n给 agent 的样例变了（{len(keys)} 个文件里有差别的见上）。是有意的改动就重录："
                  f"python3 scripts/record_agent_fixtures.py，提交 fixtures/agent/，并在 PR 里说清 agent 那边要不要跟着改。")
            return 1
        print(f"给 agent 的样例和仓库里的一样（{len(names)} 个场景，{len(new)} 个文件，{REAL_TIME() - t:.0f} 秒）")
        return 0
    os.makedirs(OUT, exist_ok=True)
    write_tree(OUT, recorded, names)
    if complete:
        with open(os.path.join(OUT, "README.md"), "w", encoding="utf-8") as f:
            f.write(new["README.md"])
    print(f"录好了：{len(names)} 个场景，{len(new)} 个文件，写在 fixtures/agent/（{REAL_TIME() - t:.0f} 秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
