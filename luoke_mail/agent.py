"""螺壳 (luoke-agent): what luoke-mail tells the user through the agent, and what the agent may do for them.

The tool protocol is luoke-agent's docs/工具协议.md (top-level ADR 0006): a subset of MCP over a Unix socket in the
data folder (`run/agent.sock`), served by the luoke-mail process that holds the instance lock.

- **outbox** (`luoke://luoke-mail/outbox`): the state of what luoke-mail wants to say now, not a stream. Per
  mailbox (「今天」's template, luoke-mail#81): the mail that needs the user and is high priority today (「要你决定」),
  one card for the rest -- what else needs them, what could be tidied, older suspicious mail (「顺便看看」) -- and each
  suspicious message just received (urgent). Each card comes with a plan.
- **plans** (store `agent_plans`): what a card proposes, kept across restarts -- the user may tap hours later.
  Only a plan luoke-mail made is carried out, only with the choices its card offered, only on the user's tap
  (`ai.luoke/user-action`, checked by the server), and only after checking again: the mail still where it was,
  processing mode on when it moves several to the trash or junk. It runs as the same operations the page uses (each with its undo), and is undone as one.
- **auto-done** (protocol §8a): what was done by habit today, one card, a [撤销] per habit; undone through App.undo,
  which tells the memory.
- **for the conversation**: `mail_search`, `mail_read` (mail text is data, marked `untrusted`) and `mail_plan`, which
  only makes a card. Nothing the conversation calls changes the mailbox; the tap does.
- **in 螺壳's main window** (protocol §1a): `summary` (the count and a line beside the app's name) and three base
  pages, `page/mailboxes`, `page/activity` (what was done, each with its undo) and `page/rules`. Read when opened, not
  sent as news. Editing rules and connecting a mailbox stay on luoke-mail's own page (`service.ui`): a mailbox's
  code is typed only there.
- **needs-access**: no mailbox yet, one low item with 去设置; put down, it stays down.
- Never through the agent: unsubscribing (it talks to a third party), switching processing mode on (the
  user's own setting). Processing mode is asked for only when a plan moves several messages to the trash or junk
  (top-level ADR 0016); background sorting only makes plans here.

On since the agent ships (beta 2; off in the first release, top-level ADR 0008): the manifest declares the
capabilities, `LUOKE_MAIL_AGENT=0` turns the socket off. In the app the socket turns away callers that don't look
like 螺壳's agent core (`Callers`): the tap mark is the caller's word until the agent signs taps. This keeps out
ordinary programs and connections made by mistake, not a program in the user's account set on posing as the agent.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import threading
import time

from . import agent_cards as cards
from . import agent_mcp, config, desktop, engines, memory, providers, triage
from .actions import ActionError
from .agent_mcp import USER_ACTION, ToolError, ToolServer
from .mailbox import MailError, parse_message, real_mid

ACCESSORY = "luoke-mail"
NEEDS_YOU = ("reply", "schedule", "follow_up")
SHOWN = 8                                  # messages with their own row on a card; the rest in one row
LISTENING = 600                            # the agent read the outbox this recently: luoke-mail's own notes wait
QUIET = 86400                              # mail the user chose to leave alone: not brought up again on its own for a day
READ_LIMIT = 4000                          # characters of a message given to the conversation
PLAN_MOST = SHOWN                          # messages one card proposes for, asked for from the conversation
HITS = 20                                  # search results unless the agent asks for fewer or more, up to MAX_HITS
MAX_HITS = 50
# what a flag means is in its reason, under the heading; a flag is often a notice or an ad (2026-11-06: 70 of 205
# reasons said nothing of a scam), so the heading doesn't call it one
HEADINGS = {"new-mail": "这批邮件我打算这样处理", "suspicious": "这封邮件判为可疑",
            "suspicious-old": "这几封邮件也判为可疑", "tidy": "这些邮件打算整理"}
STILL_HERE = 0.9                           # a loose row's plan stays while this much of its mail is still waiting
UNDONE = {"draft": "{} 封草稿删掉了", "archive": "{} 封回到收件箱", "trash": "{} 封回到收件箱", "spam": "{} 封回到收件箱",
          "flag": "{} 封的星标去掉了"}
DONE = {"archive": "归档", "trash": "删除", "spam": "移到垃圾邮件", "flag": "加星标", "draft": "写好草稿"}
OPTIONS = {
    "reply": [("draft", "写草稿"), ("flag", "加星标"), ("archive", "归档"), ("keep", "先不动")],
    "schedule": [("flag", "加星标"), ("archive", "归档"), ("keep", "先不动")],
    "follow_up": [("flag", "加星标"), ("archive", "归档"), ("keep", "先不动")],
}
SUSPICIOUS = [("spam", "移到垃圾邮件"), ("keep", "先不动")]     # never unsubscribe: it confirms the address
# A flag can be wrong (2026-11-06: a 9B flagged dozens of real notices), and a tap on 按这个处理 takes what is
# selected: so 先不动 is, and moving a message is the user's own pick.
SUSPICIOUS_PICK = "keep"
SUSPICIOUS_NEW = 2 * 86400                 # received this recently: a card of its own, urgent
SUSPICIOUS_URGENT = 3                      # at most this many such cards; more, and older, share one card that waits
ANY_MAIL = [("archive", "归档"), ("trash", "删除"), ("keep", "先不动")]
# 「今天」's template (protocol §4 later additions, luoke-mail#81): a choice's icon, a button's words for what it does
ICONS = {"draft": "pencil-line", "flag": "star", "archive": "archive", "trash": "trash-2", "spam": "shield-alert",
         "keep": "circle-minus"}
DOING = {"flag": "加星标", "draft": "写草稿", "archive": "归档", "trash": "删除", "spam": "移到垃圾邮件"}
DONE_GROUP = {"flag": "已加星标 {n} 封", "draft": "已写好草稿 {n} 封", "archive": "已归档 {n} 封",
              "trash": "{n} 封移到「已删除」", "spam": "已移到垃圾邮件 {n} 封"}
PART_LABEL = {"new-mail": "新邮件", "todo": "新邮件", "tidy": "整理", "sus": "可疑邮件", "suspicious": "可疑邮件"}
PAGES = {"accounts": "settings", "rules": "rules"}     # service.ui's page ids -> the page's own
OPENS_SOMETHING = ("mail_open", "mail_open_settings", "mail_open_page", "mail_open_calendar", "mail_show",
                   "mail_disconnect")   # protocol §5: only on a tap, whatever their readOnlyHint says
NEEDS_MAILBOX = "needs-access/mailbox"     # the outbox's item while no mailbox is connected


def enabled() -> bool:
    return os.environ.get("LUOKE_MAIL_AGENT") != "0"


def socket_path() -> str:
    return os.path.join(config.CONFIG_DIR, "run", "agent.sock")


def _plain(text: str | None, limit: int) -> str:
    """Card text: one line, short; credentials masked."""
    text = memory.mask(" ".join(str(text or "").split()))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _who(it: dict) -> str:
    return _plain(it.get("from_name") or it.get("from_addr") or "", 24)


def _received(items: list[dict]) -> str | None:
    """When the newest of these arrived, for an outbox item's `created`: the agent shows it, and the time the card
    was made says nothing (2026-11-06: 174 cards at 09:27)."""
    dates = sorted(it["date"] for it in items if it.get("date"))
    return dates[-1].replace(" ", "T") + ":00" if dates else None


def _iso(at: str) -> str:
    """A time as the store writes it, as the outbox says times (protocol §3)."""
    return at.replace(" ", "T")


def _due_badge(due: str | None) -> tuple[str, str | None] | None:
    """A row's deadline as the label after its subject, and its colour: red once past, orange today and tomorrow."""
    day = str(due or "")[:10]
    if len(day) != 10:
        return None
    today = time.strftime("%Y-%m-%d")
    if day < today:
        return f"{day[5:]} 已过", "danger"
    if day == today:
        return "今天截止", "warning"
    if day == time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400)):
        return "明天截止", "warning"
    return f"{day[5:]} 截止", None


def _apply_words(entries: dict, part: str | None = None) -> str:
    """The apply button says what its tap does with the picks the card starts with: 「加星标 4 · 写草稿 1」."""
    n: dict[str, int] = {}
    for e in entries.values():
        if part is None or e.get("part") == part:
            n[e["selected"]] = n.get(e["selected"], 0) + len(e["ids"])
    return " · ".join(f"{DOING[v]} {n[v]}" for v in DOING if n.get(v)) or "按选的处理"


def _subtitled(messages: list, text: str) -> list:
    """Protocol §4 `subtitle`: a line telling this card from others like it, on a core that draws it."""
    if text and "subtitle" in _features():
        for m in messages:
            for comp in (m.get("updateComponents") or {}).get("components") or []:
                if comp.get("id") == "root":
                    comp["subtitle"] = text
    return messages


def _was(items: list[dict]) -> dict:
    """What each message was suggested, whether it was suspicious and starred, when the card was made. A card can stay up
    while the user changes some of them on the page: those are the user's, and a tap leaves them alone."""
    return {str(it["id"]): [it.get("action"), bool(it.get("suspicious")), bool(it.get("flagged"))] for it in items}


def _changed(it: dict, entry: dict) -> bool:
    """Changed since the plan was made. A star counts only when it came after: a plan asked for in the conversation may
    be about starred mail. Plans from 0.19.38 kept no star (and only unstarred mail made cards), so a star there is new."""
    was = (entry.get("was") or {}).get(str(it["id"]))
    if was is None:
        return bool(it.get("flagged"))
    then = was[2] if len(was) > 2 else False
    return [it.get("action"), bool(it.get("suspicious"))] != was[:2] or bool(it.get("flagged")) != then


def _tidy_words(e: dict) -> str:
    """What one tap on 整理 does, in the button's own words: 「归档 2089 · 删除 24」."""
    return " · ".join(w for w in (f"归档 {e['archive']}" if e["archive"] else "",
                                  f"删除 {e['trash']}" if e["trash"] else "") if w)


def _reply_pick(it: dict) -> str:
    """A draft for a person writing to the user; a notice the model took for one wanting an answer (a sign-in alert,
    a document from a service) is marked to follow up instead."""
    return "draft" if it.get("category") in ("work", "personal") and not it.get("bulk") else "flag"


def _snippet(r: dict, words: list[str]) -> str:
    """A line of the body around the first word found; the summary, or nothing, when the body doesn't have it."""
    body = " ".join(str(r.get("body") or "").split())
    at = min((i for i in (body.lower().find(w.lower()) for w in words) if i >= 0), default=-1)
    if at < 0:
        return _plain(r.get("summary"), 120)
    start = max(at - 30, 0)
    return ("…" if start else "") + _plain(memory.mask(body)[start:start + 120], 120)


class Callers:
    """Who may connect to agent.sock (top-level security review S2). In the app: a process that looks like
    螺壳's agent core -- 螺壳.app's own Python, started by 螺壳.app itself, the app's signature intact and by the
    team that signed this one; an app without a team (ad hoc) passes nobody. A check against ordinary programs and
    mistaken connections: a program of the user's set on it can still pose as the core. Inside 螺壳.app the core is
    the one in this same app, wherever it was put. The development build (`ai.luoke.core.dev`, always ad hoc) keeps
    the path and parent check and skips the signature, as its agent core does. From a source tree (development,
    tests/ui_harness.py) any process of the user's: the socket file is 600."""

    def __init__(self, checked: bool):
        self.checked = checked
        self._passed: set = set()          # (螺壳.app, its seal's mtime) found signed as 螺壳; failures are asked again
        self._refused: set = set()

    def allow(self, pid: int | None) -> bool:
        if not self.checked:
            return True
        if pid is not None and self._from_luoke(pid):
            return True
        if pid not in self._refused:
            self._refused.add(pid)
            print(f"[luoke-mail] agent.sock: refused a process that is not 螺壳's agent (pid {pid})", file=sys.stderr)
        return False

    def _from_luoke(self, pid: int) -> bool:
        import plistlib
        inside = desktop.in_luoke()
        app = desktop.own_app() if inside else memory.hub_app()               # in 螺壳.app: that one, not the first found
        if not app:
            return False
        try:
            with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
                info = plistlib.load(f)
            main = os.path.realpath(os.path.join(app, "Contents", "MacOS", info["CFBundleExecutable"]))
        except (OSError, KeyError, ValueError, plistlib.InvalidFileException):
            return False
        core = os.path.realpath(os.path.join(app, "Contents", "Resources", "python", "bin", "python3"))
        if desktop.process_path(pid) != core or desktop.process_path(desktop.parent_pid(pid) or 0) != main:
            return False
        return (inside and info.get("CFBundleIdentifier") == memory.HUB_DEV_ID) or self._signed(app)

    def _signed(self, app: str) -> bool:
        try:
            key = (app, os.stat(os.path.join(app, "Contents", "_CodeSignature", "CodeResources")).st_mtime_ns)
        except OSError:
            return False
        if key in self._passed:                       # once per 螺壳 version: codesign takes a tenth of a second
            return True
        mine = desktop.own_app()
        team = desktop.team_of(mine) if mine else None
        if not team or not desktop.signed_as(app, memory.HUB_ID, team):
            return False                              # ad hoc: on the bundle id alone any app could claim to be 螺壳
        self._passed = {key}
        return True


class _Checked(agent_mcp._UnixServer):
    callers: Callers

    def verify_request(self, request, client_address) -> bool:
        try:
            return self.callers.allow(desktop.peer_pid(request))
        except Exception as e:                        # raised here, it would end the thread taking connections
            print(f"[luoke-mail] agent.sock: could not check a caller, turned away: {type(e).__name__}", file=sys.stderr)
            return False


_REQUEST = threading.local()     # what the core asking now draws (protocol §4 later additions), for the cards and pages


def _features() -> frozenset:
    return getattr(_REQUEST, "features", frozenset())


def _look(pid: str, it: dict | None = None) -> dict:
    """在邮件助手里看: the window's 收件箱 page (§5 agent.open_page), at this one mail when the core opens a page at
    one (§4 pageArgs); an older core has the tool open the browser page."""
    return _see(pid, "在邮件助手里看", one=it)


def _see(pid: str, label: str, *, one: dict | None = None, acct: str | None = None, lst: str | None = None) -> dict:
    """A button to the window's 收件箱: at one mail (`one`), or at a mailbox's list (`acct`, `lst`: needs, suspicious),
    on a core that opens a page with args; else at the page as it is. An older core has the browser page opened."""
    f = _features()
    if "openPage" not in f:
        return cards.button(label, "mail_open", {"plan_id": pid})
    ctx: dict = {"page": "inbox"}
    if "pageArgs" in f and (one or acct):
        ctx["args"] = {"id": str(one["id"])} if one else {"box": acct, **({"list": lst} if lst else {})}
    return cards.button(label, "agent.open_page", ctx, icon="inbox")


def _new_template() -> bool:
    """The core draws 「今天」's template (cards.NEW_TEMPLATE): labels, icons, a row's own buttons."""
    return cards.NEW_TEMPLATE <= _features()


def _to_settings(label: str = "打开设置", *, primary: bool = False) -> dict:
    if "openPage" in _features():
        return cards.button(label, "agent.open_page", {"page": "settings"}, primary=primary)
    return cards.button(label, "mail_open_settings", primary=primary)


class _Tools(ToolServer):
    """The protocol's ToolServer (agent_mcp.py, a copy kept as it is) with `Callers` asked at each connection, and
    what the asking core draws kept for the request (`_features`)."""

    def _read(self, rid, params, meta):
        _REQUEST.features = agent_mcp.features_of(meta)
        try:
            return super()._read(rid, params, meta)
        finally:
            _REQUEST.features = frozenset()

    def _call(self, rid, params, meta):
        _REQUEST.features = agent_mcp.features_of(meta)
        try:
            return super()._call(rid, params, meta)
        finally:
            _REQUEST.features = frozenset()

    def __init__(self, name: str, version: str, callers: Callers):
        super().__init__(name, version)
        self.callers = callers

    def start(self, socket_path: str) -> "ToolServer":       # ToolServer.start, with the checked server
        agent_mcp._prepare_socket(socket_path)
        server = _Checked(socket_path, agent_mcp._Handler)
        os.chmod(socket_path, 0o600)
        server.tool_server, server.callers = self, self.callers
        self._server, self._socket_path = server, socket_path
        self._socket_ino = os.lstat(socket_path).st_ino
        self._thread = threading.Thread(target=server.serve_forever, name=f"{self.name}-mcp", daemon=True)
        self._thread.start()
        return self

    def on_the_mac(self, *names: str) -> None:
        """These open something on this Mac: only on a tap, and greyed out on the phone (protocol §5)."""
        for name in names:
            self._tools[name]["spec"]["_meta"] = {"ai.luoke/where": "mac"}


class Agent:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.read_at = float("-inf")       # when the agent last read the outbox (monotonic)
        self.server = None
        from .window import window_for     # noqa: PLC0415
        from .window.pages import PAGES as WINDOW_PAGES     # noqa: PLC0415
        self.window, self.window_pages = window_for(app), WINDOW_PAGES

    # ---- the socket ------------------------------------------------------------------------------------
    def start(self, path: str | None = None, callers: Callers | None = None) -> None:
        s = _Tools(ACCESSORY, __import__("luoke_mail").__version__, callers or Callers(checked=desktop.packaged()))
        s.resource(f"luoke://{ACCESSORY}/outbox", self.outbox, name="要告诉你的事")
        s.resource("permissions", self.permissions, name="邮件助手的权限")
        s.resource("summary", self.summary, name="概览")
        for pid, title in self.window_pages:              # the window's pages (window/pages)
            s.resource(f"page/{pid}", lambda args, pid=pid: self.window.draw(pid, args, _features()), name=title, args=True)
        s.tool("mail_view", self.window_view, title="换一下页面上显示的内容",
               input_schema={"type": "object", "required": ["page", "button"],
                             "properties": {"page": {"type": "string"}, "button": {"type": "string"}}})
        s.tool("mail_do", self.window_do, title="照页面上点的按钮处理", read_only=False,
               input_schema={"type": "object", "required": ["page", "button"],
                             "properties": {"page": {"type": "string"}, "button": {"type": "string"},
                                            "fields": {"type": "object"}, "choices": {"type": "object"},
                                            "checks": {"type": "object"}, "secrets": {"type": "object"}}})
        s.tool("outbox_ack", self.ack, title="记下这件事已经说过",
               input_schema={"type": "object", "required": ["id", "state"],
                             "properties": {"id": {"type": "string"}, "state": {"enum": ["shown", "answered", "dismissed"]}}})
        s.tool("mail_apply_plan", self.apply_plan, title="按方案处理邮件", read_only=False,
               input_schema={"type": "object", "required": ["plan_id", "choices"],
                             "properties": {"plan_id": {"type": "string"}, "choices": {"type": "object"}}})
        s.tool("mail_undo", self.undo, title="撤销刚才的处理", read_only=False,
               input_schema={"type": "object", "required": ["execution_id"], "properties": {"execution_id": {"type": "string"}}})
        s.tool("mail_open", self.open_mail, title="在邮件助手里看",
               input_schema={"type": "object", "properties": {"plan_id": {"type": "string"}}})
        s.tool("mail_open_settings", self.open_settings, title="打开邮件助手的设置")
        s.tool("mail_open_calendar", self.open_calendar, title="把这封的日程交给「日历」", read_only=False,
               input_schema={"type": "object", "required": ["plan_id", "key"],
                             "properties": {"plan_id": {"type": "string"}, "key": {"type": "string"}}})
        s.tool("mail_search", self.search, title="搜索邮件",
               input_schema={"type": "object", "properties": {"query": {"type": "string"}, "since": {"type": "string"},
                                                              "until": {"type": "string"}, "limit": {"type": "integer"}}})
        s.tool("mail_show", self.show, title="打开这封邮件", read_only=False,
               input_schema={"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}})
        s.tool("mail_read", self.read, title="读一封邮件",
               input_schema={"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}})
        s.tool("mail_plan", self.plan, title="对选中的邮件出方案",
               input_schema={"type": "object", "required": ["ids"],
                             "properties": {"ids": {"type": "array", "items": {"type": "string"}}}})
        s.tool("mail_open_page", self.open_page, title="打开邮件助手",
               input_schema={"type": "object", "properties": {"page": {"type": "string"}}})
        s.tool("mail_disconnect", self.disconnect, title="到设置里移除邮箱", read_only=False)
        s.on_the_mac(*OPENS_SOMETHING)
        try:
            self.server = s.start(path or socket_path())
        except OSError as ex:                  # the agent cannot reach luoke-mail; the page works as always
            print(f"[luoke-mail] agent socket not started: {type(ex).__name__}", file=sys.stderr)

    def close(self) -> None:
        if self.server:
            self.server.close()
            self.server = None

    def listening(self) -> bool:
        """The agent read the outbox lately: it tells the user, luoke-mail's own notification would repeat it."""
        return time.monotonic() - self.read_at < LISTENING

    # ---- the outbox ------------------------------------------------------------------------------------
    def outbox(self) -> dict:
        """Per mailbox (luoke-mail#81, 「今天」's template): suspicious mail just received, urgent, a card each; the mail
        that needs the user most (high priority, not past its date: 「要你决定」); and one card for the rest of it --
        the rest that needs them, what could be tidied, older mail that looks suspicious (「顺便看看」)."""
        self.read_at = time.monotonic()
        out = []
        accts = self.app.mail_accounts()
        if not accts:
            asked = self._needs_mailbox()
            return {"items": [asked] if asked else []}
        for acct in accts:
            label = f"（{acct}）" if len(accts) > 1 else ""
            pending = self._pending(acct)
            urgent, older = self._suspicious(acct, [it for it in pending if it.get("suspicious")], label)
            out += urgent
            mail = [it for it in pending if not it.get("suspicious")]
            for item in (self._new_mail(acct, mail, label), self._mailbox(acct, mail, older, label),
                         self._auto_done(acct, label)):
                if item:
                    out.append(item)
            for gone in ("tidy", "suspicious-old"):    # cards before 0.19.47: their mail is on the mailbox's card now
                self._withdraw(acct, gone, set())
        return {"items": out}

    def _pending(self, acct: str) -> list[dict]:
        """Sorted mail still in the inbox and not starred to follow up: what the outbox and the count are about."""
        return [it for it in self.store.items(acct, "pending") if it.get("action") and not it.get("flagged")]

    @staticmethod
    def _needs_you(pending: list[dict]) -> list[dict]:
        prio, today = {"high": 0, "medium": 1}, time.strftime("%Y-%m-%d")
        needs = sorted((it for it in pending if it["action"] in NEEDS_YOU), key=lambda it: it.get("date") or "", reverse=True)
        needs.sort(key=lambda it: prio.get(triage.priority_now(it, today), 2))
        return needs

    def _needs_mailbox(self) -> dict | None:
        """Protocol §1a: nothing to sort until a mailbox is connected. The code is typed on luoke-mail's own page, so
        the card only leads there. The same item while it stands; put down, it is not said again."""
        state, at = self.store.cache_get("", "agent_mailbox_asked")
        if state == "down":
            return None
        if not state:
            self.store.cache_set("", "agent_mailbox_asked", "open")
            at = self.store.cache_get("", "agent_mailbox_asked")[1]
        said = "连上邮箱，新邮件就能在这里分拣好。授权码在邮件助手自己的页面里填，只存在这台 Mac 的钥匙串里。"
        card = cards.Card("needs-mailbox")
        card.heading("还没连邮箱")
        card.text(said, caption=True)
        card.buttons(_to_settings("去设置", primary=True) if "secureField" in _features()
                     else cards.button("去设置", "mail_open_page", {"page": "accounts"}, primary=True))
        return {"id": NEEDS_MAILBOX, "kind": "needs-access", "urgency": "low", "permission": "mailbox", "deadline": None,
                "expires": None, "created": _iso(at), "why_now": "连上邮箱才能分拣", "title": "还没连邮箱", "summary": said,
                "card": card.messages()}

    def permissions(self) -> dict:
        return {"mailbox": "granted" if self.app.mail_accounts() else "ask"}

    # ---- the main window: the count, and the base pages (protocol §1a) -----------------------------------
    def _waiting(self, acct: str) -> dict:
        """What waits for the user in this mailbox. Mail they chose to leave alone doesn't: it is theirs to come back to."""
        quiet = self._quiet(acct)
        pending = [it for it in self._pending(acct) if it["id"] not in quiet]
        return {"suspicious": [it for it in pending if it.get("suspicious")],
                "needs": self._needs_you([it for it in pending if not it.get("suspicious")]),
                "rest": [it for it in pending if not it.get("suspicious") and it["action"] in ("archive", "ignore")]}

    @staticmethod
    def _tally(needs: int, suspicious: int, rest: int) -> str:
        return "，".join(w for w in (f"{needs} 封要你处理" if needs else "", f"{suspicious} 封可疑" if suspicious else "",
                                    f"{rest} 封可以整理" if rest else "") if w)

    def summary(self) -> dict:
        """`count` is what the user has to answer: mail that needs them and mail that looks suspicious; what only
        could be tidied, or was done by habit, is in the line and not in the count."""
        accts = self.app.mail_accounts()
        if not accts:
            return {"count": 0, "text": "还没连邮箱"}
        waiting = [self._waiting(acct) for acct in accts]
        needs, suspicious, rest = (sum(len(w[k]) for w in waiting) for k in ("needs", "suspicious", "rest"))
        done = sum(len(e["item_ids"]) for acct in accts for e in self._auto_logs(acct))
        line = "，".join(w for w in (self._tally(needs, suspicious, rest), f"今天自动处理了 {done} 封" if done else "") if w)
        return {"count": needs + suspicious, "text": line or "没有要你处理的邮件"}

    def _offer(self, acct: str, kind: str, entries: dict, loose: tuple = ()) -> dict | None:
        """The plan for these messages and these choices: the same id while they stay the same. One the user
        answered or dismissed is not offered again; open ones for messages no longer here are withdrawn.

        A `loose` row (a long tail of mail still being sorted) doesn't change the plan while it is open: the card
        keeps the messages it was made with, the user's picks stay on it, and newer ones wait for the next card.
        It does change once most of its messages are gone (handled on the page), and once the plan is answered."""
        sig = hashlib.sha256(repr(sorted((k, () if k in loose else tuple(e["ids"]), tuple(e["allowed"]))
                                         for k, e in entries.items())).encode()).hexdigest()[:16]
        if not any(k in entries for k in loose):
            plan = self.store.plan_offer(f"{kind}-{sig}", acct, kind, entries)
            return plan if plan["state"] == "open" else None
        now = {i for k in loose if k in entries for i in entries[k]["ids"]}
        for pid in self.store.plans_open(acct, kind):
            plan = self.store.plan_get(pid)
            was = [i for k in loose for i in (plan["entries"].get(k) or {}).get("ids", [])]
            if pid.startswith(f"{kind}-{sig}-") and sum(i in now for i in was) >= STILL_HERE * len(was) > 0:
                return plan
        tail = hashlib.sha256(repr(sorted(now)).encode()).hexdigest()[:8]
        plan = self.store.plan_offer(f"{kind}-{sig}-{tail}", acct, kind, entries)
        return plan if plan["state"] == "open" else None

    @staticmethod
    def _rows(entries: dict, part: str | None = None, extra: dict | None = None) -> list[dict]:
        """A card's rows, from what the plan keeps: the same card again (after a restart, or to add a note). `part`:
        only the rows of that part of a mailbox's card; `extra`: a row's own button (加入日程), by key."""
        out = []
        for k, e in entries.items():
            if not e.get("options") or (part is not None and e.get("part") != part):
                continue
            row = {"key": k, "detail": e.get("detail") or "", "selected": e["selected"],
                   "options": [(o[0], o[1], ICONS.get(o[0])) for o in e["options"]]}
            if e.get("subject") is not None:           # plans before 0.19.47 keep only 「发件人：主题」
                row["who"], row["subject"] = e.get("who") or "", e["subject"]
            else:
                row["label"] = e["label"]
            due = _due_badge(e.get("due"))
            if due:
                row["due"], row["due_tone"] = due
            if extra and k in extra:
                row["extra"] = extra[k]
            out.append(row)
        return out

    def _quiet(self, acct: str) -> set:
        """Mail the user chose 先不动 for lately: not a new thing to say on its own."""
        q = self.store.cache_get(acct, "agent_quiet")[0] or {}
        return {int(i) for i, t in q.items() if time.time() - t < QUIET}

    def _withdraw(self, acct: str, kind: str, keep: set) -> None:
        for pid in self.store.plans_open(acct, kind):
            if pid not in keep:
                self.store.plan_set(pid, state="withdrawn")

    @staticmethod
    def _alike(needs: list[dict]) -> list[list[dict]]:
        """Rows of mail that needs the user: the same notice again and again (a CI failure four times) is one row.
        Alike: sent by a machine (`bulk`: a list, a no-reply address), from the same address, with the same suggestion
        and the same subject but for its numbers. A notice address sends different things (a security alert, a review
        asked for), and a person's 「第3季度报告」 and 「第4季度报告」 each want their own answer: those stay apart.
        Events are never put together: each has its own date."""
        rows: dict[tuple, list[dict]] = {}
        for it in needs:
            if it["action"] == "schedule" or not it.get("bulk"):
                key: tuple = ("one", it["id"])
            else:
                skeleton = re.sub(r"[\d#]+", "", " ".join(str(it.get("subject") or "").split()))
                key = ((it.get("from_addr") or "").lower(), it["action"], skeleton)
            rows.setdefault(key, []).append(it)
        return list(rows.values())

    # ---- the cards (luoke-mail#81: 「今天」's template, luoke-agent scripts/today_template.py) --------------------
    @staticmethod
    def _need_entry(row: list[dict], part: str | None = None) -> dict:
        """One row of mail that needs the user (several alike ones are one row), as the plan keeps it."""
        it = row[0]
        options = OPTIONS[it["action"]] if len(row) == 1 else [o for o in OPTIONS[it["action"]] if o[0] != "draft"]
        selected = _reply_pick(it) if it["action"] == "reply" and len(row) == 1 else "flag"
        dues = sorted(x["due_date"] for x in row if x.get("due_date"))
        who, subject = _who(it), _plain(it.get("subject"), 40) + (f"（共 {len(row)} 封）" if len(row) > 1 else "")
        e = {"ids": [x["id"] for x in row], "allowed": [v for v, _ in options], "selected": selected, "options": options,
             "who": who, "subject": subject, "label": f"{who}：{subject}", "due": dues[0] if dues else None,
             "was": _was(row), "detail": _plain(it.get("summary"), 120)}
        if part:
            e["part"] = part
        if it["action"] == "schedule" and (it.get("event") or {}).get("date"):
            e["event"] = it["id"]
        return e

    @staticmethod
    def _sus_entry(it: dict, part: str | None = None) -> dict:
        who, subject = _who(it), _plain(it.get("subject"), 40)
        e = {"ids": [it["id"]], "allowed": ["spam", "keep"], "selected": SUSPICIOUS_PICK, "options": SUSPICIOUS,
             "who": who, "subject": subject, "label": f"{who}：{subject}", "was": _was([it]),
             "detail": _plain(it.get("reason") or it.get("summary"), 120)}
        if part:
            e["part"] = part
        return e

    @staticmethod
    def _tidy_entry(rest: list[dict]) -> dict:
        archive = sum(1 for it in rest if it["action"] == "archive")
        senders: dict[str, int] = {}
        for it in rest:
            senders[_who(it)] = senders.get(_who(it), 0) + 1
        top = [w for w, _ in sorted(senders.items(), key=lambda kv: -kv[1])[:3] if w]
        return {"ids": [it["id"] for it in rest], "allowed": ["suggested", "keep"], "selected": "keep",
                "options": [("suggested", "照建议处理"), ("keep", "先不动")], "archive": archive,
                "trash": len(rest) - archive, "label": "这些邮件打算整理", "part": "tidy", "was": _was(rest),
                "detail": ("多是" + "、".join(top) + ("等" if len(senders) > 3 else "")) if top else ""}

    def _calendars(self, acct: str, pid: str, entries: dict, part: str | None = None) -> tuple[dict, list]:
        """加入日程 for the rows with an event: on the row itself (the template), else under the card (up to 3)."""
        keys = [k for k, e in entries.items() if e.get("event") and (part is None or e.get("part") == part)]
        new = _new_template()
        made = {}
        for k in keys[:SHOWN if new else 3]:
            it = self.store.get(entries[k]["event"])
            if it and it.get("state") == "pending":
                made[k] = self._calendar_button(acct, pid, k, it, alone=len(keys) == 1, compact=new)
        return (made, []) if new else ({}, list(made.values()))

    def _left_alone(self, acct: str, rows: list[list[dict]]) -> bool:
        """Every message on these rows was left alone lately (先不动): not brought up again by itself. New mail that
        needs the user brings them back with it."""
        return {it["id"] for row in rows for it in row} <= self._quiet(acct)

    def _new_mail(self, acct: str, pending: list[dict], label: str) -> dict | None:
        """「要你决定」: the mail that needs the user and is high priority today (`triage.priority_now`: not past its
        date), up to SHOWN rows. The rest of what needs them is on the mailbox's card (`_mailbox`)."""
        today = time.strftime("%Y-%m-%d")
        high = [it for it in self._needs_you(pending) if triage.priority_now(it, today) == "high"]
        shown = self._alike(high)[:SHOWN]
        if not shown or self._left_alone(acct, shown):
            self._withdraw(acct, "new-mail", set())
            return None
        entries = {f"m{row[0]['id']}": self._need_entry(row) for row in shown}
        plan = self._offer(acct, "new-mail", entries)
        self._withdraw(acct, "new-mail", {plan["plan_id"]} if plan else set())
        if not plan:
            return None
        pid = plan["plan_id"]
        mail = [it for row in shown for it in row]
        dues = sorted(it["due_date"] for it in mail if it.get("due_date"))
        heads = "；".join(f"{_who(row[0])}：{_plain(row[0].get('summary') or row[0].get('subject'), 40)}" for row in shown[:2])
        quiet = self._quiet(acct)
        total = sum(1 for it in high if it["id"] not in quiet)          # as summary's count: 先不动 isn't waiting
        more = max(total - sum(1 for it in mail if it["id"] not in quiet), 0)
        return {"id": f"new-mail/{pid}", "kind": "new-mail", "urgency": "normal",
                "deadline": f"{dues[0]}T23:59:00" if dues else None, "created": _received(mail) or _iso(plan["created_at"]),
                "why_now": f"分拣好了，{total} 封要你先处理" + (f"，最早的要在 {dues[0]} 前回" if dues else ""),
                "title": f"{total} 封邮件要你处理{label}",
                "summary": _plain(heads + (f"；还有 {more} 封" if more else "") + "。", 300),
                "card": self._decide_card(pid, acct, plan["entries"])}

    def _decide_card(self, pid: str, acct: str, entries: dict, note: str | None = None, update: bool = False) -> list:
        on_row, under = self._calendars(acct, pid, entries)
        n = sum(len(e["ids"]) for e in entries.values())
        total = len(self._waiting(acct)["needs"])
        if total > n:
            see = _see(pid, f"查看全部 {total} 封", acct=acct, lst="needs")
        else:
            see = _look(pid, {"id": next(iter(entries.values()))["ids"][0]}) if n == 1 else \
                _see(pid, "在邮件助手里看", acct=acct, lst="needs")
        msgs = cards.plan_card(pid, f"{n} 封要处理", self._rows(entries, extra=on_row), apply_event="mail_apply_plan",
                               plan_id=pid, apply_label=_apply_words(entries), note=note, badge=acct,
                               extra_buttons=under + [see] + ([_to_settings()] if note else []), update=update,
                               id_prefix=f"s{int(time.time())}-" if update else "", features=_features())
        return _subtitled(msgs, acct)

    def _mailbox(self, acct: str, pending: list[dict], older: list[dict], label: str) -> dict | None:
        """「顺便看看」: one card per mailbox, in parts -- 待处理 (what needs the user and isn't high today), 可以整理
        (what the model would archive or delete: one decision, and the button says how many it moves; 2026-11-06 one
        tap on a card about 8 messages moved 1,715), 判为可疑 (older mail that looks suspicious, folded: a flag can be
        wrong). One plan for the card; a part's buttons name the part, and a tap does only that part. The mail to tidy
        is the one the card was made with (`_offer`'s loose row)."""
        today, quiet = time.strftime("%Y-%m-%d"), self._quiet(acct)
        needs = [it for it in self._needs_you(pending) if triage.priority_now(it, today) != "high"]
        rest = [it for it in pending if it["action"] in ("archive", "ignore") and it["id"] not in quiet]
        todo = self._alike(needs)[:SHOWN]
        if self._left_alone(acct, todo):
            todo = []
        entries = {f"m{row[0]['id']}": self._need_entry(row, "todo") for row in todo}
        if rest:
            entries["tidy"] = self._tidy_entry(rest)
        for it in older[:SHOWN]:
            entries[f"s{it['id']}"] = self._sus_entry(it, "sus")
        plan = self._offer(acct, "mailbox", entries, loose=("tidy",)) if entries else None
        self._withdraw(acct, "mailbox", {plan["plan_id"]} if plan else set())
        if not plan:
            return None
        pid, entries = plan["plan_id"], plan["entries"]
        n_todo = sum(len(e["ids"]) for e in entries.values() if e.get("part") == "todo")
        tidy = entries.get("tidy")
        tally = "，".join(w for w in (f"{n_todo} 封待处理" if n_todo else "",
                                     f"{len(tidy['ids'])} 封可以整理" if tidy else "",
                                     f"{len(older)} 封判为可疑" if older else "") if w)
        heads = [f"{e['who']}：{e['subject']}" for e in entries.values() if e.get("part") == "todo"][:2]
        if tidy:
            heads.append(f"整理：{_tidy_words(tidy)}" + (f"，{tidy['detail']}" if tidy["detail"] else ""))
        newer = sum(1 for it in rest if it["id"] not in set(tidy["ids"])) if tidy else 0
        mail = [it for row in todo for it in row] + [it for it in rest if not tidy or it["id"] in set(tidy["ids"])] + older[:SHOWN]
        return {"id": f"mailbox/{pid}", "kind": "mailbox", "urgency": "low", "deadline": None,
                "created": _received(mail) or _iso(plan["created_at"]), "why_now": "不急，有空时看看",
                "title": tally + label,
                "summary": _plain("；".join(heads) + (f"；之后又分拣好 {newer} 封，下一张再说" if newer else "") + "。", 300),
                "card": self._mailbox_card(pid, acct, entries)}

    def _mailbox_card(self, pid: str, acct: str, entries: dict, note: str | None = None, note_part: str | None = None,
                      update: bool = False) -> list:
        parts = []
        todo = {k: e for k, e in entries.items() if e.get("part") == "todo"}
        if todo:
            on_row, under = self._calendars(acct, pid, entries, "todo")
            n, total = sum(len(e["ids"]) for e in todo.values()), len(self._waiting(acct)["needs"])
            see = _see(pid, f"查看全部 {total} 封" if total > n else "在邮件助手里看", acct=acct, lst="needs")
            parts.append({"part": "todo", "icon": "inbox", "title": "待处理", "items": self._rows(entries, "todo", on_row),
                          "buttons": [cards.button(_apply_words(entries, "todo"), "mail_apply_plan",
                                                   {"plan_id": pid, "part": "todo", "choices": cards.CHOICES},
                                                   primary=True, icon="check")] + under + [see]})
        tidy = entries.get("tidy")
        if tidy:
            later = "批量删除前，要先在邮件助手的设置里开启处理模式。" if tidy["trash"] > 1 and not self.app.cfg["mode"]["write"] else ""
            parts.append({"part": "tidy", "icon": "archive", "title": "可以整理",
                          "note": "".join(w for w in (tidy["detail"] + "。" if tidy["detail"] else "", later) if w) or None,
                          "buttons": [cards.button(_tidy_words(tidy), "mail_apply_plan",
                                                   {"plan_id": pid, "part": "tidy", "choices": {"tidy": ["suggested"]}},
                                                   primary=True, icon="archive"),
                                      cards.button("先不动", "mail_apply_plan",
                                                   {"plan_id": pid, "part": "tidy", "choices": {"tidy": ["keep"]}},
                                                   icon="circle-minus")]})
        sus = {k: e for k, e in entries.items() if e.get("part") == "sus"}
        if sus:
            shown = sum(len(e["ids"]) for e in sus.values())
            n = max(len(self._split_suspicious(acct)[1]), shown)
            parts.append({"part": "sus", "icon": "shield-alert", "title": "判为可疑", "collapsed": f"{n} 封，都先不动",
                          "items": self._rows(entries, "sus"),
                          "buttons": [cards.button(_apply_words(entries, "sus"), "mail_apply_plan",
                                                   {"plan_id": pid, "part": "sus", "choices": cards.CHOICES},
                                                   primary=True, icon="check"),
                                      _see(pid, f"其余 {n - shown} 封去邮件助手看" if n > shown else "在邮件助手里看",
                                           acct=acct, lst="suspicious")]})
        for p in parts:
            if p.pop("part") == note_part and note:
                p["note"], p["collapsed"] = note, None
                p["buttons"].append(_to_settings())
        msgs = cards.mailbox_card(pid, "邮箱", parts, badge=acct, update=update,
                                  id_prefix=f"s{int(time.time())}-" if update else "", features=_features())
        return _subtitled(msgs, acct)

    def _card_for(self, plan: dict, note: str | None = None, part: str | None = None) -> list:
        """A plan's card again, with a note (the plan stays, and the user's picks: the data model isn't sent)."""
        pid, acct, entries = plan["plan_id"], plan["account"], plan["entries"]
        if plan["kind"] == "new-mail":
            return self._decide_card(pid, acct, entries, note=note, update=True)
        if plan["kind"] == "mailbox":
            return self._mailbox_card(pid, acct, entries, note=note, note_part=part, update=True)
        return cards.plan_card(pid, HEADINGS.get(plan["kind"], HEADINGS["new-mail"]), self._rows(entries),
                               apply_event="mail_apply_plan", plan_id=pid, apply_label=_apply_words(entries), note=note,
                               update=True, id_prefix=f"s{int(time.time())}-", extra_buttons=[_to_settings()],
                               features=_features())

    def _calendar_button(self, acct: str, pid: str, key: str, it: dict, alone: bool = True, compact: bool = False) -> dict:
        """加入日程. `compact`: on the mail's own row, the date and time, 加入日程 in its tooltip (the template)."""
        ev = it.get("event") or {}
        when = " ".join(w for w in (str(ev.get("date") or "")[5:], ev.get("start") or "") if w)
        if compact:
            title, extra = when or "加入日程", {"icon": "calendar-plus", "tooltip": "加入日程"}
        else:
            title, extra = ("加入日程" if alone else f"加入日程：{when or _plain(_who(it), 10)}"), {}   # the subject is on the row above
        try:
            url = self.app.actions(acct, write=False).calendar(it["id"], adding=False).get("google_url")
        except ActionError:
            url = None
        if url and url.startswith("https://") and not (it.get("event") or {}).get("more"):    # several: the file has them all
            return cards.open_url_button(title, url, **extra)      # 螺壳 says where it goes before it opens
        return cards.button(title, "mail_open_calendar", {"plan_id": pid, "key": key}, **extra)

    def _split_suspicious(self, acct: str, items: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
        """Suspicious mail just received (a card each, urgent; at most SUSPICIOUS_URGENT) and the rest -- older mail,
        flagged when it was sorted again or by a newer prompt (2026-11-06: 180 urgent cards in the same second, the
        oldest from 2019, most of them real notices), which goes on the mailbox's card, folded."""
        if items is None:
            items = [it for it in self._pending(acct) if it.get("suspicious")]
        quiet = self._quiet(acct)
        items = sorted((it for it in items if it["id"] not in quiet), key=lambda it: it.get("date") or "", reverse=True)
        since = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - SUSPICIOUS_NEW))
        new = [it for it in items if (it.get("date") or "") >= since][:SUSPICIOUS_URGENT]
        return new, [it for it in items if it not in new]

    def _suspicious(self, acct: str, items: list[dict], label: str) -> tuple[list[dict], list[dict]]:
        """The urgent cards, and the older suspicious mail for the mailbox's card."""
        new, old = self._split_suspicious(acct, items)
        out, keep = [], set()
        for it in new:
            key = f"m{it['id']}"
            plan = self._offer(acct, "suspicious", {key: self._sus_entry(it)})
            if not plan:
                continue
            pid = plan["plan_id"]
            keep.add(pid)
            out.append({"id": f"suspicious/{pid}", "kind": "suspicious", "urgency": "urgent", "deadline": None,
                        "created": _received([it]) or _iso(plan["created_at"]), "why_now": "判为可疑，拿不准就别点里面的链接",
                        "title": "可疑邮件「" + _plain(f"{_who(it)}：{it.get('subject') or ''}", 54) + "」" + label,
                        "summary": _plain(f"{_who(it)}：{it.get('subject') or ''}", 200),
                        "card": self._suspicious_card(pid, key, it, acct)})
        self._withdraw(acct, "suspicious", keep)
        return out, old

    @staticmethod
    def _suspicious_card(pid: str, key: str, it: dict, acct: str) -> list:
        """One message, one decision: two buttons that do it, no picker and a button to tap after it."""
        who, subject = _who(it), _plain(it.get("subject"), 60) or "（无主题）"
        new = _new_template()
        card = cards.Card(pid)
        if new:
            card.heading("可疑邮件", badge=acct)
            line = ([card._add({"id": card._new_id("t"), "component": "Text", "text": who, "variant": "strong"}, top=False)]
                    if who else [])
            line.append(card._add({"id": card._new_id("t"), "component": "Text", "text": subject, "maxLines": 1}, top=False))
            card._add({"id": card._new_id("r"), "component": "Row", "children": line})
        else:
            card.heading(HEADINGS["suspicious"])
            card.text(f"{who}：{subject}" if who else subject)
        detail = _plain(it.get("reason") or it.get("summary"), 120)
        if detail:
            card.text(detail, caption=True)
        spam = cards.button("移到垃圾邮件", "mail_apply_plan", {"plan_id": pid, "choices": {key: ["spam"]}},
                            **({"icon": "shield-alert", "variant": "destructive"} if new else {}))
        keep = cards.button("先不动", "mail_apply_plan", {"plan_id": pid, "choices": {key: ["keep"]}},
                            **({"icon": "circle-minus"} if new else {}))
        look = _look(pid, it)
        card.buttons(spam, keep, look if new else {k: v for k, v in look.items() if k != "icon"})
        if "subtitle" in _features():
            card.subtitle = f"{who} · {subject}" if who else subject
        return card.messages()

    # ---- what was done unasked (protocol §8a, ADR 0016) --------------------------------------------------
    def _auto_logs(self, acct: str) -> list[dict]:
        """Today's work done by habit, still standing, that the user hasn't put down. Once a batch was said, a newer
        one replaces it: the card stays while it is the latest thing to say."""
        told = self.store.cache_get(acct, "agent_auto_told")[0] or {}
        today = time.strftime("%Y-%m-%d")
        logs = [e for e in self.store.log_entries(acct, limit=500) if e["action"] == "auto" and not e["undone"]
                and (e.get("ts") or "")[:10] == today and told.get(str(e["id"])) != "closed"]
        return [e for e in logs if str(e["id"]) not in told] or logs

    def _auto_card(self, surface: str, logs: list[dict], heading: str, update: bool = False) -> list:
        card = cards.Card(surface, id_prefix=f"u{int(time.time())}-" if update else "")
        card.heading(heading)
        for e in logs:
            card.text(_plain(e.get("detail"), 120))
            names = [it.get("subject") for it in (self.store.get(i) for i in e["item_ids"][:5]) if it]
            if names:
                card.text("、".join(_plain(n, 30) for n in names), caption=True)
            card.buttons(cards.button("撤销", "mail_undo", {"execution_id": f"auto:{e['id']}"}))
        return card.update_messages() if update else card.messages()

    def _auto_done(self, acct: str, label: str) -> dict | None:
        logs = self._auto_logs(acct)
        if not logs:
            self._withdraw(acct, "auto-done", set())
            return None
        ids = sorted(e["id"] for e in logs)
        plan = self._offer(acct, "auto-done", {"logs": {"ids": ids, "allowed": []}})
        self._withdraw(acct, "auto-done", {plan["plan_id"]} if plan else set())
        if not plan:
            return None
        count = sum(len(e["item_ids"]) for e in logs)
        heading = f"已自动处理 {count} 封"                # by habit, and in 螺壳's own mailbox
        return {"id": f"auto-done/{plan['plan_id']}", "kind": "auto-done", "urgency": "normal", "deadline": None,
                "created": _iso(plan["created_at"]), "why_now": "这些是自动做的，不对可以撤销", "title": heading + label,
                "summary": _plain("；".join(e.get("detail") or "" for e in logs), 300),
                "card": self._auto_card(plan["plan_id"], logs, heading)}

    def _auto_told(self, plan: dict, state: str) -> None:
        told = self.store.cache_get(plan["account"], "agent_auto_told")[0] or {}
        for i in plan["entries"]["logs"]["ids"]:
            if told.get(str(i)) != "closed":
                told[str(i)] = state
        self.store.cache_set(plan["account"], "agent_auto_told", dict(list(told.items())[-500:]))

    def ack(self, args: dict, meta: dict) -> dict:
        state, item = args.get("state"), str(args.get("id") or "")
        if state not in ("shown", "answered", "dismissed"):
            raise ToolError("invalid", "state 只能是 shown、answered、dismissed。")
        if item == NEEDS_MAILBOX:
            if state != "shown":
                self.store.cache_set("", "agent_mailbox_asked", "down")
            return {"text": "好的"}
        plan = self.store.plan_get(item.split("/", 1)[-1])
        if plan and plan["state"] == "open":
            if plan["kind"] == "auto-done":
                self._auto_told(plan, "shown" if state == "shown" else "closed")
            if state == "shown":
                self.store.plan_set(plan["plan_id"], shown_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            else:
                self.store.plan_set(plan["plan_id"], state="dismissed")
        return {"text": "好的"}

    # ---- carrying a plan out ---------------------------------------------------------------------------
    def apply_plan(self, args: dict, meta: dict) -> dict:
        pid, choices, part = str(args.get("plan_id") or ""), args.get("choices"), args.get("part")
        plan = self.store.plan_get(pid)
        if not plan or plan["state"] != "open":
            raise ToolError("stale", "这批邮件已经处理过了，或者已经有了变化。")
        if not isinstance(choices, dict) or set(choices) - set(plan["entries"]):
            raise ToolError("invalid", "选项不在卡片给出的范围里。")
        if part is not None and not any(e.get("part") == part for e in plan["entries"].values()):
            raise ToolError("invalid", "选项不在卡片给出的范围里。")
        picked = {}
        for key, e in plan["entries"].items():
            if part is not None and e.get("part") != part:
                continue                           # another part of the mailbox's card: its own button does it
            v = choices.get(key, [e["selected"]])
            if not isinstance(v, list) or len(v) != 1 or v[0] not in e["allowed"]:
                raise ToolError("invalid", "选项不在卡片给出的范围里。")
            picked[key] = v[0]
        acct = plan["account"]
        if acct not in self.app.mail_accounts():
            raise ToolError("stale", "这个邮箱已经不在邮件助手里了。")
        groups: dict[str, list[int]] = {}
        kept = []
        for key, choice in picked.items():
            for i in plan["entries"][key]["ids"]:
                it = self.store.get(i)
                if not it or it["account"] != acct or it["state"] != "pending":
                    groups.setdefault("_handled", []).append(i)
                    continue
                if _changed(it, plan["entries"][key]):      # changed on the page since the card was made: the user's
                    groups.setdefault("_changed", []).append(i)
                    continue
                op = {"suggested": "archive" if it.get("action") == "archive" else "trash"}.get(choice, choice)
                if op == "keep":
                    kept.append(i)
                else:
                    groups.setdefault(op, []).append(i)
        # several to the trash or junk can't be taken back: that waits for the user's own setting (ADR 0016)
        if len(groups.get("trash", [])) + len(groups.get("spam", [])) > 1 and not self.app.cfg["mode"]["write"]:
            raise ToolError("needs_setup", "先在邮件助手的设置里开启处理模式。",     # the plan stays, the user's picks too
                            card=self._card_for(plan, "批量删除前，先在邮件助手的设置里开启处理模式，再回来点。", part))
        self.store.plan_set(pid, state="running")
        acts = self.app.actions(acct)
        logs, said, failed = [], [], []
        try:
            for op in ("archive", "trash", "spam", "flag"):
                if groups.get(op):
                    r = acts.execute(groups[op], op)
                    if r.get("log_id"):
                        logs.append({"log": r["log_id"], "op": op, "n": len(r.get("done") or [])})
                    said.append(r["message"])
            drafts, odd = groups.get("draft", []), 0
            for i in drafts:
                try:
                    signature = self.app.cfg["profile"].get("signature") or ""
                    text = acts.generate_draft(i, self.app.link("user-waiting"), memory.about(self.app.mem), signature)
                    logs.append({"log": acts.save_draft(i, text, self.app.address(acct), signature)["log_id"], "op": "draft", "n": 1})
                    odd += bool(self.app.draft_note(self.store.get(i), text))
                except (engines.EngineError, ActionError) as ex:
                    failed.append(str(ex))
            if len(drafts) > len(failed):
                said.append(f"已写好 {len(drafts) - len(failed)} 封草稿，放在草稿箱，请检查后自行发送")
            if odd:
                said.append(f"其中 {odd} 封草稿里有原信没有的联系方式，发出前请核对")
        except Exception as ex:  # noqa: BLE001 - whatever stopped it, the plan must not stay "running"
            self.store.plan_set(pid, state="done" if logs else "open", logs=logs)
            if not logs:
                raise ToolError("failed", str(ex) if isinstance(ex, (MailError, ActionError)) else "没能处理，请稍后再试。")
            failed.append(str(ex) if isinstance(ex, (MailError, ActionError)) else "其余的没能处理")
        if groups.get("_handled"):
            said.append(f"{len(groups['_handled'])} 封已经处理过，没再动")
        if groups.get("_changed"):
            said.append(f"{len(groups['_changed'])} 封在邮件助手里改过，没再动")
        if failed:
            said.append("另有没做成的：" + failed[0])
        text = ("；".join(said) or "都先不动。").rstrip("。") + "。"
        self.store.plan_set(pid, state="done", logs=logs)
        if kept:                                   # 先不动: left alone, and not brought up again on its own
            q = self.store.cache_get(acct, "agent_quiet")[0] or {}
            q = {i: t for i, t in q.items() if time.time() - t < QUIET}
            q.update({str(i): time.time() for i in kept})
            self.store.cache_set(acct, "agent_quiet", q)
        ops = {entry["op"] for entry in logs}
        group = None
        if "doneGroup" in _features() and len(ops) == 1 and not (failed or kept or groups.get("_handled") or groups.get("_changed")):
            op = ops.pop()                         # 「已经替你做了」: one line per mailbox and result (protocol §4)
            group = cards.done_group(f"{acct}/{op}", DONE_GROUP[op], sum(entry["n"] for entry in logs),
                                     label=PART_LABEL.get(part or plan["kind"]), badge=acct)
        card = cards.result_card(pid, text, undo_event="mail_undo" if logs else None, undo_context={"execution_id": pid},
                                 group=group)
        return {"text": text, "card": card, "done": True}

    def undo(self, args: dict, meta: dict) -> dict:
        pid = str(args.get("execution_id") or "")
        if pid.startswith("auto:"):
            return self._undo_auto(pid[5:], meta)
        if pid.startswith("log:"):
            return self._undo_log(pid[4:])
        plan = self.store.plan_get(pid)
        if not plan or plan["state"] != "done" or not plan["logs"]:
            raise ToolError("stale", "这次处理已经撤销过了，或者没有可撤销的。")
        acts = self.app.actions(plan["account"], write=False)
        back: dict[str, int] = {}
        for entry in reversed(plan["logs"]):
            if (self.store.log_entry(entry["log"]) or {}).get("undone"):
                continue                       # taken back on its own, on 处理记录
            try:
                acts.undo(entry["log"])
            except (MailError, ActionError) as ex:
                raise ToolError("failed", str(ex))
            words = UNDONE.get(entry["op"], "{} 封回到原来的样子")
            back[words] = back.get(words, 0) + entry["n"]
        self.store.plan_set(pid, state="undone", logs=[])
        text = "已撤销：" + "，".join(w.format(n) for w, n in back.items() if n) + "。" if any(back.values()) else "已撤销。"
        out = {"text": text, "done": True}
        surface = (meta.get(USER_ACTION) or {}).get("surfaceId")
        if isinstance(surface, str):
            out["card"] = cards.result_card(surface, text)
        return out

    def _undo_log(self, log_id: str) -> dict:
        """One line of 处理记录 taken back, as the page's 撤销 does: App.undo tells the memory when a habit did it."""
        e = self.store.log_entry(int(log_id)) if log_id.isdigit() else None
        if not e or e["undone"] or not e["undo"] or e["account"] not in self.app.mail_accounts():
            raise ToolError("stale", "这次处理已经撤销过了，或者没有可撤销的。")
        try:
            r = self.app.undo(e["account"], e["id"])
        except (MailError, ActionError, ValueError) as ex:
            raise ToolError("failed", str(ex))
        return {"text": (r.get("message") or "已撤销").rstrip("。") + "。", "done": True}

    def _undo_auto(self, log_id: str, meta: dict) -> dict:
        """One habit's batch back: App.undo tells the memory, as a counter-example, and it isn't done to them again."""
        e = self.store.log_entry(int(log_id)) if log_id.isdigit() else None
        if not e or e["action"] != "auto" or e["undone"] or e["account"] not in self.app.mail_accounts():
            raise ToolError("stale", "这次处理已经撤销过了，或者没有可撤销的。")
        try:
            r = self.app.undo(e["account"], e["id"])
        except (MailError, ActionError, ValueError) as ex:
            raise ToolError("failed", str(ex))
        text = r.get("message") or "已撤销。"
        left = self._auto_logs(e["account"])
        out: dict = {"text": text, "done": not left}
        surface = (meta.get(USER_ACTION) or {}).get("surfaceId")
        if isinstance(surface, str):
            count = sum(len(x["item_ids"]) for x in left)
            out["card"] = (self._auto_card(surface, left, f"已自动处理 {count} 封", update=True) if left
                           else cards.result_card(surface, text))
        return out

    # ---- searching: only when the user asks, nothing logged (protocol §1b) ------------------------------
    def search(self, args: dict, meta: dict) -> dict:
        query = " ".join(str(args.get("query") or "").split())
        since, until = (str(args.get(k) or "")[:16].replace("T", " ") for k in ("since", "until"))
        if not (query or since or until):
            return {"text": "没有要搜的内容。", "hits": [], "card": []}
        try:
            limit = min(max(int(args.get("limit") or HITS), 1), MAX_HITS)
        except (TypeError, ValueError):
            limit = HITS
        words = query.split()
        rows = [(acct, r) for acct in self.app.mail_accounts() for r in self.store.search(acct, words, since, until, limit)]
        rows.sort(key=lambda x: x[1]["date"] or "", reverse=True)
        hits = [self._hit(acct, r, query) for acct, r in rows[:limit]]
        return {"text": f"找到 {len(hits)} 封邮件。" if hits else "没有找到邮件。", "hits": hits, "card": []}

    @staticmethod
    def _hit(acct: str, r: dict, query: str) -> dict:
        ident = f"{acct}|{r['message_id']}"
        who = _who(r)
        return {"id": ident, "source": acct, "kind": "mail", "title": _plain(r.get("subject"), 80) or "（无主题）",
                "line": who + ("（可疑）" if r.get("suspicious") else ""), "date": (r.get("date") or "")[:10],
                "snippet": _snippet(r, query.split()),
                "strong": bool(query) and query.lower() in (r.get("subject") or "").lower(),
                "actions": [{"name": "mail_show", "title": "打开", "args": {"id": ident}, "primary": True}]}

    def _hit_mail(self, ident) -> tuple[str, str, dict | None]:
        acct, _, mid = str(ident or "").partition("|")
        if acct not in self.app.mail_accounts() or not mid:
            raise ToolError("stale", "找不到这封邮件了。")
        return acct, mid, self.store.mail_by_id(acct, mid)

    def show(self, args: dict, meta: dict) -> dict:
        acct, mid, _ = self._hit_mail(args.get("id"))
        gid = self.store.gmail_id(acct, mid)
        if self.app.provider(acct) == "gmail" and (gid or real_mid(mid)):     # a made-up Message-ID finds nothing
            desktop.open_url(providers.gmail_message_url(self.app.address(acct), mid, gid))
            return {"text": "已在浏览器里打开这封邮件。"}
        self._page("inbox")
        return {"text": "已在浏览器里打开邮件助手。"}

    # ---- for the conversation: reading, and a plan on a card. Mail text is data; only the user's tap acts --
    def read(self, args: dict, meta: dict) -> dict:
        acct, mid, m = self._hit_mail(args.get("id"))
        if not m:
            raise ToolError("stale", "找不到这封邮件了。")
        body = m.get("body")
        if body is None:                       # old mail: only its headers are here; read as ever, nothing marked read
            try:
                with self.app.open_mailbox(acct) as mb:
                    raw = mb.fetch_raw(m["uid"], m.get("folder") or "INBOX")
            except MailError as ex:
                raise ToolError("not_ready", str(ex))
            parsed = parse_message(raw) if raw else {}
            if parsed.get("message_id") != mid:
                raise ToolError("stale", "找不到这封邮件了。")
            body = parsed.get("body")
        return {"text": f"邮件：{_plain(m.get('subject'), 60)}", "untrusted": True, "mail": {
            "from": _who(m), "address": m.get("from_addr"), "subject": memory.mask(m.get("subject")), "date": m.get("date"),
            "suspicious": bool(m.get("suspicious")), "summary": memory.mask(m.get("summary")),
            "due_date": m.get("due_date"), "body": memory.mask(body)[:READ_LIMIT]}}

    def plan(self, args: dict, meta: dict) -> dict:
        ids = args.get("ids")
        if not isinstance(ids, list) or not ids:
            raise ToolError("invalid", "没有选中邮件。")
        found = [self._hit_mail(i) for i in ids[:PLAN_MOST]]
        accts = {a for a, _, _ in found}
        if len(accts) > 1:
            raise ToolError("invalid", "一次只能处理一个邮箱里的邮件。")
        acct = accts.pop()
        entries = {}
        for _, _, it in found:
            if it and it.get("state") == "pending" and "id" in it:
                entries[f"m{it['id']}"] = self._choices(it)
        if not entries:
            raise ToolError("stale", "这些邮件已经处理过了，或者不在收件箱里。")
        # new each time: the same mail may be asked about again after a plan was answered
        pid = "chat-" + hashlib.sha256(f"{sorted(entries)}{time.time()}".encode()).hexdigest()[:16]
        self.store.plan_offer(pid, acct, "chat", entries)
        heading = f"这 {len(entries)} 封我打算这样处理"
        only = [it for _, _, it in found if it and it.get("state") == "pending" and "id" in it]
        return {"text": heading, "plan_id": pid, "card": cards.plan_card(
            pid, heading, self._rows(entries), apply_event="mail_apply_plan", plan_id=pid,
            apply_label=_apply_words(entries), badge=acct, features=_features(),
            extra_buttons=[_look(pid, only[0] if len(only) == 1 else None)])}

    @staticmethod
    def _choices(it: dict) -> dict:
        action = it.get("action")
        if it.get("suspicious"):
            options, selected = SUSPICIOUS, SUSPICIOUS_PICK
        elif action in OPTIONS:
            options, selected = OPTIONS[action], _reply_pick(it) if action == "reply" else "flag"
        else:
            options, selected = ANY_MAIL, {"archive": "archive", "ignore": "trash"}.get(action, "keep")
        who, subject = _who(it), _plain(it.get("subject"), 40)
        return {"ids": [it["id"]], "allowed": [v for v, _ in options], "selected": selected, "options": options,
                "who": who, "subject": subject, "label": f"{who}：{subject}", "due": it.get("due_date"),
                "detail": _plain(it.get("summary"), 120), "was": _was([it])}

    # ---- read-only: opening things ---------------------------------------------------------------------
    def _page(self, where: str) -> None:
        if self.app.port:
            desktop.open_url(desktop.page_url(self.app.port, self.app.token) + f"#{where}")

    # ---- the window's pages (window/) --------------------------------------------------------------------
    def window_view(self, args: dict, meta: dict) -> dict:
        from .window import Gone           # noqa: PLC0415
        try:
            self.window.go(str(args.get("page") or ""), str(args.get("button") or ""))
        except Gone:
            raise ToolError("stale", "页面已经变了，请再点一次。")
        return {"text": "好的。"}

    def window_do(self, args: dict, meta: dict) -> dict:
        """Only the button the user pressed (its id in `ai.luoke/user-action`) on a page this drew; what it does is
        looked up here, never taken from the call."""
        from .window import Gone           # noqa: PLC0415
        page, button = str(args.get("page") or ""), str(args.get("button") or "")
        ua = meta.get(USER_ACTION) or {}
        if ua.get("componentId") != button or str(ua.get("surfaceId") or "").rsplit("/", 1)[-1] != page:
            raise ToolError("invalid", "这个操作要在页面上点按钮才能执行。")
        try:
            out = self.window.do(page, button, {k: args[k] for k in ("fields", "choices", "checks", "secrets") if k in args},
                                 _features())
        except Gone:
            raise ToolError("stale", "页面已经变了，请再点一次。")
        except (ActionError, MailError, ValueError) as ex:
            raise ToolError("failed", str(ex))
        return out

    def open_mail(self, args: dict, meta: dict) -> dict:
        self._page("inbox")
        return {"text": "已在浏览器里打开邮件助手。"}

    def open_page(self, args: dict, meta: dict) -> dict:
        self._page(PAGES.get(str(args.get("page") or ""), "inbox"))
        return {"text": "已在浏览器里打开邮件助手。"}

    def disconnect(self, args: dict, meta: dict) -> dict:
        """Taken to the page, not done here: removing a mailbox deletes its data here, and the page asks first and
        says which mailbox."""
        self._page("settings")
        return {"text": "已打开邮件助手的设置，请在「邮箱」里选择要移除的邮箱。"}

    def open_settings(self, args: dict, meta: dict) -> dict:
        self._page("settings")
        return {"text": "已在浏览器里打开邮件助手的设置。"}

    def open_calendar(self, args: dict, meta: dict) -> dict:
        plan = self.store.plan_get(str(args.get("plan_id") or ""))
        entry = (plan or {}).get("entries", {}).get(str(args.get("key") or ""))
        if not entry:
            raise ToolError("stale", "找不到这封邮件了。")
        try:
            cal = self.app.actions(plan["account"], write=False).calendar(entry["ids"][0])
        except ActionError as ex:
            raise ToolError("failed", str(ex))
        folder = os.path.join(config.CONFIG_DIR, "calendar")
        os.makedirs(folder, 0o700, exist_ok=True)
        path = os.path.join(folder, cal["filename"])
        with open(path, "w", encoding="utf-8") as f:
            f.write(cal["ics"])
        desktop.open_url(path)                 # 「日历」 opens it; the user confirms there
        return {"text": "已交给「日历」，请在日历里确认。"}
