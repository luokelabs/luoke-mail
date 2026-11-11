"""The mail assistant inside 螺壳's window (decided 2026-11-09: everything the browser page does is a
page in the window; the browser page stays only for what the window cannot do yet).

Each page is `page/<id>` (protocol §1a), drawn from the same views the browser page reads (server.get_routes) and
acting through the same routes (server.post_routes). Two tools behind the buttons (agent.py):

- `mail_view` (read-only): what the page shows -- which mailbox, which list, the next screen, one mail opened. Kept
  here per page (one person, one window), so the window re-reading the page after a press (界面接口 §4) shows it.
- `mail_do` (a change, only on the user's tap): looked up by the pressed button's id among the buttons this page
  was drawn with (kit.Page.do). The agent passes which button was pressed, never what it does.

The last few drawings of a page are kept: a button pressed just as the page was redrawn does what it said.
"""
from __future__ import annotations

import collections
import os
import threading
import time
from typing import Callable, Dict, Optional

from . import kit
from ..actions import ActionError
from ..mailbox import MailError

KEEP_DRAWINGS = 8
INPUT_LIMIT = 2000

# what a page's buttons may do: the browser page's POST routes the window offers (server.post_routes). Not here:
# saving a mailbox (`connect_mail` below: only from a password field, §4 secureField), choosing a model service
# (螺壳 runs the model), opening the whole memory (luoke-mem's page)
OPS = frozenset({
    "/api/sync", "/api/jobs/cancel", "/api/items/act", "/api/items/override", "/api/items/draft",
    "/api/items/draft/save", "/api/undo", "/api/review/act", "/api/review/seen",
    "/api/subscriptions", "/api/senders/act", "/api/old/scan", "/api/old/judge", "/api/old/apply",
    "/api/digest/read", "/api/digest/reading", "/api/digest/feedback", "/api/digest/open", "/api/digest/archive",
    "/api/digest/source", "/api/rules", "/api/rules/delete", "/api/rules/filter-preview",
    "/api/memory", "/api/memory/site", "/api/memory/learn", "/api/learn/intro-seen",
    "/api/mode", "/api/power", "/api/threads", "/api/flow", "/api/auto", "/api/auto/notify-settings",
    "/api/auto/test-notify", "/api/profile", "/api/mail/own", "/api/mail/remove", "/api/forget",
})


def _calendar(w: "Window", args: dict, features: frozenset) -> dict:
    """加入日程: the appointment as a calendar file handed to 「日历」 (as the agent's card does); the user confirms there."""
    import os                                   # noqa: PLC0415
    from .. import config, desktop              # noqa: PLC0415
    index = args.get("index")
    app = w.app
    cal = app.actions(args.get("source"), write=False).calendar(int(args["id"]), int(index) if index is not None else None)
    folder = os.path.join(config.CONFIG_DIR, "calendar")
    os.makedirs(folder, 0o700, exist_ok=True)
    path = os.path.join(folder, cal["filename"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(cal["ics"])
    desktop.open_url(path)
    return {"message": "已交给「日历」，请在日历里确认。"}


def _open_browser(w: "Window", args: dict, features: frozenset) -> dict:
    """What the window cannot do yet opens the browser page there: adding a mailbox (its app password may not go on
    a page, protocol §8). `page` is one of the browser page's own (static/main.js)."""
    from .. import desktop                      # noqa: PLC0415
    where = args.get("page") if args.get("page") in ("settings", "mail") else "settings"
    desktop.open_url(desktop.page_url(w.app.port, w.app.token) + f"#{where}")
    return {"message": "已在浏览器里打开邮件助手。"}


def _connect_mail(w: "Window", args: dict, features: frozenset) -> dict:
    """保存并测试连接 (static/setup.js): the app password comes from the page's password field (§4 secureField) in
    this one press, goes to the keychain (App.save_mail) and nowhere else -- not into a note, a view or an answer."""
    if "secureField" not in features:
        raise ValueError("这个窗口不能填授权码，请在浏览器里添加邮箱。")
    form = w.views.setdefault("settings", {})
    form["typed"] = str(args.get("address") or "").strip()[:200]     # the address, to show again if it fails
    saved = w.app.save_mail({k: args.get(k) for k in ("key", "provider", "address", "secret", "host", "port")})
    form["add"] = saved["key"]                                        # saved: a retry changes this one
    r = w.app.test_mail({"key": saved["key"]})
    for k in ("add", "kind", "typed"):
        form.pop(k, None)
    w.views.setdefault("*", {})["account"] = saved["key"]
    return {"message": f"连接成功。收件箱共 {r.get('total', 0)} 封，未读 {r.get('unseen', 0)} 封。"}


def _gmail_filters(w: "Window", args: dict, features: frozenset) -> dict:
    """导出到 Gmail (static/gmail.js): mailFilters.xml handed to the window's save dialog (§4 saveFile); an old core
    cannot take a file, so it goes into 「下载」 and the sentence says so."""
    r = w.app.export_gmail_filters(args)
    if "saveFile" in features:
        return {"message": r["message"] + "。请在 Gmail 设置中导入。",
                "file": {"name": r["filename"], "mediaType": "application/xml", "text": r["xml"]}}
    path = _downloads(r["filename"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(r["xml"])
    return {"message": f"{r['message']}，已存到「下载」：{os.path.basename(path)}。请在 Gmail 设置中导入。"}


def _downloads(name: str) -> str:
    """A free name in 「下载」 (LUOKE_DOWNLOADS in tests: tests/isolate.py)."""
    folder = os.environ.get("LUOKE_DOWNLOADS") or os.path.expanduser("~/Downloads")
    os.makedirs(folder, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(name))
    path, n = os.path.join(folder, stem + ext), 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(folder, f"{stem} ({n}){ext}")
    return path


# what only the window does (not a browser route)
LOCAL = {"calendar": _calendar, "open_browser": _open_browser, "connect_mail": _connect_mail,
         "gmail_filters": _gmail_filters}


# what a page says after a change whose route answers without a sentence (the browser page says it in its toast)
SAID = {"/api/mode": "处理模式已更新。", "/api/flow": "已保存。", "/api/threads": "已保存。", "/api/power": "已保存。",
        "/api/profile": "已保存。", "/api/auto": "已保存。", "/api/auto/notify-settings": "已保存。",
        "/api/auto/test-notify": "已发送测试通知。", "/api/digest/feedback": "已记下。", "/api/digest/archive": "已归档。",
        "/api/digest/open": "已打开。", "/api/digest/reading": "已更新。", "/api/digest/source": "已更新。",
        "/api/memory/site": "已更新。", "/api/memory/learn": "已更新。", "/api/review/seen": "已记下。"}


class Gone(Exception):
    """The button is not on any drawing kept: the page has changed; the window reads it again."""


class Window:
    def __init__(self, app, builders: Dict[str, Callable]):
        self.app = app
        self.builders = builders                   # page id -> fn(window, page, view)
        self.views: Dict[str, dict] = {}           # page id -> what it shows; "*" for all pages (the mailbox)
        self.notes: Dict[str, dict] = {}           # page id -> the last result said on it {"text", "undo"}
        self.confirming: Dict[str, dict] = {}      # page id -> an action waiting for 确认
        self._drawn: Dict[str, "collections.OrderedDict[int, kit.Page]"] = {}
        self._gen = 0
        self._lock = threading.Lock()

    # ---- what is shown ----------------------------------------------------------------------------------
    def view(self, page: str) -> dict:
        return {**self.views.get("*", {}), **self.views.get(page, {})}

    def source(self, page: str) -> str:
        """The mailbox this page shows (the browser page's `source`): the one chosen, else the first."""
        accts = self.app.mail_accounts()
        chosen = self.views.get("*", {}).get("account")
        return chosen if chosen in accts else (accts[0] if accts else "demo")

    def draw(self, page: str, args: Optional[dict] = None, features: frozenset = frozenset()) -> list:
        """`args`: what the page was opened with (§4 pageArgs), `features`: what this core draws (kit.Page)."""
        if page not in self.builders:
            raise KeyError(page)
        with self._lock:
            self._gen += 1
            p = kit.Page(page, self._gen, features=features, args=args)
        self._frame(p)
        try:
            self.builders[page](self, p, self.view(page))
        except kit.Full:
            pass                                    # the builder says what is left before it fills the page
        p.finish()
        with self._lock:
            kept = self._drawn.setdefault(page, collections.OrderedDict())
            kept[p.gen] = p
            while len(kept) > KEEP_DRAWINGS:
                kept.popitem(last=False)
        return p.messages()

    def _frame(self, p: kit.Page) -> None:
        """What every page starts with: the last result (with 撤销 when it can be undone) and a change waiting
        for 确认."""
        wait = self.confirming.get(p.page)
        if wait:
            p.add(p.card(p.text(wait["confirm"]),
                         p.row(p.do("确认", wait["op"], wait["args"], primary=True), p.go("取消", {"_cancel": True}))))
        note = self.notes.get(p.page)
        if note:
            undo = note.get("undo")
            p.add(p.row(p.text(note["text"]),
                        p.do("撤销", "/api/undo", {"source": undo[0], "log_id": undo[1]}, quiet=True) if undo else None,
                        p.go("知道了", {"_clear_note": True})))

    # ---- presses -----------------------------------------------------------------------------------------
    def _find(self, page: str, button: str) -> dict:
        with self._lock:
            for p in reversed(list(self._drawn.get(page, {}).values())):
                if button in p.actions:
                    return {**p.actions[button], "_allowed": p.allowed()}
        raise Gone()

    def go(self, page: str, button: str) -> None:
        act = self._find(page, button)
        if act["kind"] != "go":
            raise Gone()
        changes = dict(act["view"])
        if changes.pop("_cancel", None):
            self.confirming.pop(page, None)
        if changes.pop("_clear_note", None):
            self.notes.pop(page, None)
        shared = {k: changes.pop(k) for k in ("account",) if k in changes}
        if shared:
            self.views.setdefault("*", {}).update(shared)
        if changes:
            self.views.setdefault(page, {}).update(changes)

    def do(self, page: str, button: str, inputs: dict, features: frozenset = frozenset()) -> dict:
        act = self._find(page, button)
        if act["kind"] != "do" or (act["op"] not in OPS and act["op"] not in LOCAL):
            raise Gone()
        args = _fill(act["args"], inputs, act["inputs"], act["_allowed"])
        if act.get("confirm"):
            self.confirming[page] = {"confirm": act["confirm"], "op": act["op"], "args": args}
            return {"text": "请在页面上确认。", "confirm": True}
        self.confirming.pop(page, None)
        from ..server import post_routes            # noqa: PLC0415 -- server imports this module
        try:
            out = (LOCAL[act["op"]](self, args, frozenset(features)) if act["op"] in LOCAL
                   else post_routes(self.app, args)[act["op"]]()) or {}
        except (MailError, ValueError, ActionError) as ex:
            self.notes[page] = {"text": str(ex), "at": time.time()}
            raise
        text = out.get("message") or out.get("text") or SAID.get(act["op"]) or (
            "已开始，进度在页面上方。" if out.get("job") or out.get("id") and out.get("kind") else "好了。")
        undo = (args.get("source") or self.source(page), out["log_id"]) if out.get("log_id") and out.get("ok", True) \
            and act["op"] != "/api/undo" else None
        self.notes[page] = {"text": text, "undo": undo, "at": time.time()}
        return {"text": text, **({"job": out["job"]} if isinstance(out.get("job"), str) else {}),
                **({"file": out["file"]} if isinstance(out.get("file"), dict) else {})}


def _fill(args, inputs: dict, kinds: list, allowed: dict):
    """args with {"$input": "<kind>/<key>"} replaced by what the user typed, ticked or picked on the page. Only the
    inputs the button reads; a pick must be one of the options the page showed; text is cut to INPUT_LIMIT."""
    def value(ref: str):
        kind, _, key = ref.partition("/")
        if kind not in kinds or not isinstance(inputs.get(kind), dict):
            raise ValueError("页面上的内容没有一起交回来，请再点一次。")
        v = inputs[kind].get(key)
        if kind in ("fields", "secrets"):
            return str(v or "")[:INPUT_LIMIT]
        if kind == "checks":
            if key not in (allowed.get("$checks") or []):
                raise ValueError("选项不在页面给出的范围里。")
            return bool(v)
        picked = v[0] if isinstance(v, list) and len(v) == 1 else None
        if picked not in (allowed.get(key) or []):
            raise ValueError("选项不在页面给出的范围里。")
        return picked

    if isinstance(args, dict):
        if set(args) == {"$input"}:
            return value(str(args["$input"]))
        if set(args) == {"$checked"}:            # the keys of the ticked boxes among these, as a list
            ref = str(args["$checked"])
            ticked = inputs.get("checks") if "checks" in kinds else None
            if not isinstance(ticked, dict):
                raise ValueError("页面上的内容没有一起交回来，请再点一次。")
            drawn = set(allowed.get("$checks") or [])         # only boxes this page drew
            return [k[len(ref):] for k, v in ticked.items() if k in drawn and k.startswith(ref) and v is True]
        return {k: _fill(v, inputs, kinds, allowed) for k, v in args.items()}
    if isinstance(args, list):
        return [_fill(v, inputs, kinds, allowed) for v in args]
    return args


def window_for(app) -> Window:
    from .pages import BUILDERS                 # noqa: PLC0415
    return Window(app, BUILDERS)
