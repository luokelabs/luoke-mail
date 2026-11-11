"""Local web app: a JSON API plus static files, bound to 127.0.0.1 only.

Local-only is not enough on its own -- any website the user visits can make the browser send
requests to 127.0.0.1. So every /api call must carry a per-launch random token in a custom header
(cross-site pages cannot set it without a CORS preflight, which we never approve), and the Host
header must be ours (blocks DNS rebinding).

Every request names its inbox with `source`: a connected mailbox (its lowercased address), or "demo" (the
built-in sample inbox, which goes through the same code paths without touching any mailbox). "mailbox"
means the first connected one (pages from before several mailboxes could be connected).

The same server runs from the terminal or, with background sorting on, as a login item (`--service`).
One instance per data folder; starting a second one just opens the first one's page. See desktop.py.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import os
import secrets
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, agent, background, config, desktop, engines, gmailfilters, hub, learn, memory, pipeline, providers, samples, subscriptions, threads, triage
from .actions import DEMO, OPS, ActionError, Actions
from .agent import Agent
from .curator import Curator
from .learner import Learner
from .profiler import Profiler
from .review import Review
from .credstore import SecretUnavailable, default_store
from .desktop import Desktop, DesktopError
from .mailbox import KeychainLocked, MailError, Mailbox, find_unsubscribe, group_key, real_mid, same_mid
from .store import RULE_KINDS, Store

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".webp": "image/webp"}
WINDOW_DAYS = 31                # NetEase opens only the last 30 days to IMAP until the user changes it
WINDOW_NOTE = "网易邮箱目前只开放最近 30 天的邮件。要整理更早的邮件，请在网页版「设置 → POP3/SMTP/IMAP → 收取选项」中选择「收取全部邮件」。"
MAIL_SECRET = "imap"
ENGINE_SECRET = "engine"
# where a suggestion came from, shown only when it isn't the model's own ("rule-differs" has its own chip)
SUGGESTION_LABEL = {"user": "已手动修改", "rule": "按规则"}


ACT_WORDS = {"archive": "归档", "read": "标为已读", "label": "加标签"}


class App:
    def __init__(self, secret_store=None, state_store: Store | None = None, desktop_: Desktop | None = None,
                 service: bool = False, memory_=None, demo_memory_=None, hub_only: bool | None = None):
        config.private_dir()
        self.secrets = secret_store or default_store()
        self.cfg = config.load()
        # the app (螺壳邮件助手.app) comes with 螺壳主控 and only uses it: no model to pick (luoke-core V1 §8, owner
        # 2026-10-15); run from source, the model service can still be chosen directly (development, the lab)
        self.hub_only = desktop.packaged() if hub_only is None else hub_only
        if self.hub_only and self.cfg["engine"].get("via") != "hub":
            self.cfg["engine"]["via"] = "hub"
            config.save(self.cfg)
        self._model_note: tuple[float, dict | None] = (float("-inf"), None)
        self.store = state_store or Store()
        self.token = desktop.load_token()
        self.desktop = desktop_ or desktop.for_this_app(hub_link=lambda: hub.Hub(self.cfg["engine"].get("hub_socket")))
        self.service = service                 # running as the login item, not from a terminal
        self.auto = background.AutoSync(self)
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.port = None
        self.on_exit = None                    # set by serve(): stop serving
        self.exit_reason = None
        self._secret_known: dict[str, bool] = {}
        self.adopt_rules()
        self.store.settle_dates(triage.settle_dates)   # 0.17.10: dates once counted from the day of sorting
        self.mem = memory_ or memory.open_memory()            # the person's memory, shared by luoke tools (luoke-mem)
        self.demo_mem = demo_memory_ or memory.open_memory(demo=True)
        memory.seed_demo(self.demo_mem)
        if memory.migrate_profile(self.cfg, self.mem):
            config.save(self.cfg)
        self.curator = Curator(self)
        self.learner = Learner(self)
        self.learner.migrate()                 # 0.11: 了解你 switched on per mailbox -> the person's switch
        self.agent = Agent(self)               # what 螺壳 (luoke-agent) may say and do for the user
        self.profiler = Profiler(self)
        self.review = Review(self)             # 今日处理: what was done with each message today (ADR 0016)
        self._last_idle = None                 # idle work takes turns (see idle_step)
        self._asked: set[str] = set()          # idle work the user started here (the app leaves the rest to 主控)
        self.in_job = False                    # this process is 螺壳主控's idle job (run_idle_job): no demo
        self._seen = float("-inf")             # when a visible page last checked in (see watching)
        self._warmth = hub.Warmth()            # the model kept loaded while the page is open (螺壳主控)
        engines.set_power(self.cfg["power"]["level"])

    def memory_for(self, acct: str):
        return self.demo_mem if acct == DEMO else self.mem

    def reset_demo_memory(self) -> None:
        """The demo's memory is luoke-mail's own sample, never the user's: cleared whole by starting its file again,
        then seeded. (Since luoke-mem 0.15 what is said in it belongs to luoke-mem (top-level ADR 0009), so forgetting
        the mail assistant's part would leave that behind -- and clearing a whole memory is luoke-mem's own page's.)"""
        path = self.demo_mem.path
        self.demo_mem.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass
        self.demo_mem = memory.Memory(path, tool=memory.TOOL)
        memory.seed_demo(self.demo_mem)

    # ---- identity / secrets ----------------------------------------------------------------
    # Several mailboxes can be connected (cfg["accounts"], first first). Each is known by its lowercased address
    # ("acct"): the store keys everything by it, so mailboxes never mix; the keychain keeps each password under
    # the address as typed. The memory is the person's, shared by all of them.
    def mailbox_cfg(self, acct: str | None = None) -> dict | None:
        """The settings of a mailbox (None: the first one)."""
        accounts = self.cfg["accounts"]
        if acct is None:
            return accounts[0] if accounts else None
        return next((m for m in accounts if m["address"].lower() == acct), None)

    def mail_accounts(self) -> list[str]:
        """The mailboxes luoke can work on (connected, password saved), first first."""
        return [m["address"].lower() for m in self.cfg["accounts"] if self._has_secret(m["address"])]

    def _has_secret(self, address: str) -> bool:
        """Asked often (every idle tick): the keychain is asked once per address, again after a change. A keychain
        that cannot be read just now (the Mac locked) is not "no password": assumed saved, asked again next time."""
        if address not in self._secret_known:
            try:
                self._secret_known[address] = bool(self.secrets.get(MAIL_SECRET, address))
            except SecretUnavailable:
                return True
        return self._secret_known[address]

    def account(self, source: str | None) -> str:
        if source == "demo":
            return DEMO
        accts = self.mail_accounts()
        if source in (None, "", "mailbox") and accts:
            return accts[0]
        if str(source or "").lower() in accts:
            return str(source).lower()
        raise ValueError("请先连接邮箱，或使用示例邮件。" if not accts else "没有连接这个邮箱，请刷新页面。")

    def address(self, acct: str) -> str:
        """The address as the user typed it (Gmail links, the From of drafts)."""
        m = self.mailbox_cfg(acct) if acct != DEMO else None
        return m["address"] if m else samples.ME

    def provider(self, acct: str) -> str:
        """The demo inbox behaves like Gmail, so every Gmail-only feature can be tried on it."""
        m = self.mailbox_cfg(acct) if acct != DEMO else None
        return "gmail" if acct == DEMO else ((m or {}).get("provider") or "custom")

    def adopt_rules(self) -> None:
        """Rules from before they belonged to a mailbox (v0.2) go to the first one -- the only one back then."""
        demo_values = {(m.get(k) or "").lower() for m in samples.demo_inbox() + samples.demo_headers()
                       for k in ("from_addr", "from_domain", "list_id") if m.get(k)}
        first = self.mailbox_cfg()
        self.store.adopt_rules(first["address"].lower() if first else None, DEMO, demo_values)

    def mail_secret(self, acct: str | None = None) -> str | None:
        m = self.mailbox_cfg(acct)
        return self.secrets.get(MAIL_SECRET, m["address"]) if m else None

    def engine_key(self) -> str | None:
        base = self.cfg["engine"].get("base_url")
        try:
            return self.secrets.get(ENGINE_SECRET, base) if base else None
        except SecretUnavailable as ex:          # the Mac locked: not "no key" -- the work waits and goes on later
            raise KeychainLocked(str(ex)) from None

    def has_model(self) -> bool:
        """A way to a model is set up: 螺壳主控, or a model service and a model the user picked."""
        e = self.cfg["engine"]
        return e.get("via") == "hub" or bool(e.get("base_url") and e.get("model"))

    def link(self, priority="background", battery: bool = False, patient=None, job: dict | None = None) -> engines.Link:
        """How to ask for a model now. Through 螺壳主控 (luoke-core's task layer) once the user switched to it,
        otherwise the model service directly. `priority` -- "user-waiting" for an answer someone is watching,
        "background" for the rest, or a function deciding each time (a sync the user started counts as
        watched while the page is open); `battery` for background work the user started by hand; `patient`
        (is it cancelled?) for work that waits out a busy hub instead of giving up; `job` to show on the page
        that the hub is loading the model."""
        e = self.cfg["engine"]
        if e.get("via") == "hub":
            return hub.Hub(e.get("hub_socket"), priority, battery, patient, self._progress(job) if job is not None else None,
                           stop=(lambda: job["cancel"]) if job is not None else None)
        return engines.Direct(e.get("base_url") or "", e.get("model") or "", self.engine_key())

    @staticmethod
    def _progress(job: dict):
        """What the hub reports while a request waits, on the job's line: loading the model can take a while."""
        was = {}

        def show(stage: str, seconds: float | None = None) -> None:
            if stage in ("queued", "loading", "cooldown"):
                was.setdefault("stage", job.get("stage"))
                job["stage"] = ("正在加载模型…" if stage == "loading" else "等待模型…" if stage == "queued"
                                else f"螺壳在控制后台功耗，约 {max(1, round(seconds or 0))} 秒后继续…")
            elif "stage" in was:
                job["stage"] = was.pop("stage")
        return show

    def model_up(self) -> bool:
        """Is the way to a model there right now (background sorting asks before each run)."""
        e = self.cfg["engine"]
        if e.get("via") == "hub":
            try:
                hub.Hub(e.get("hub_socket")).tasks(fresh=True)
                return True
            except engines.EngineError:
                return False
        return engines.probe(e.get("base_url") or "", self.engine_key())["ok"]

    def watching(self) -> str:
        """The priority for work the user started: someone is looking while a visible page keeps checking in."""
        return "user-waiting" if time.monotonic() - self._seen < 15 else "background"

    def open_mailbox(self, acct: str | None = None) -> Mailbox:
        m = self.mailbox_cfg(acct)
        try:
            secret = self.mail_secret(acct)
        except SecretUnavailable as ex:
            raise KeychainLocked(str(ex)) from None
        if not (m and m.get("provider") and secret):
            raise MailError("请先填写邮箱地址和授权码。")
        return Mailbox(m["provider"], m["address"], secret, host=m.get("host"), port=m.get("port"))

    IRREVERSIBLE = ("trash", "spam")

    def actions(self, source: str | None, write: bool = True, irreversible: bool = False) -> Actions:
        """`write=False` for things that never touch the mailbox (drafting text, calendar files). What can be
        taken back (archive, mark read, star, label, a draft) needs nothing more than the click; 处理模式 is asked
        for only by what cannot, done to many messages at once (`irreversible`: moving mail to the trash or
        junk in bulk, clearing a sender's history -- top-level ADR 0016, 2026-10-19). Which mailbox is
        always named: with several, "the first one" would be someone else's mail (issue #1, 2026-10-16)."""
        if source in (None, "", "mailbox") and len(self.mail_accounts()) > 1:
            raise ValueError("没有指明是哪个邮箱，请刷新页面后重试。")
        acct = self.account(source)
        if irreversible and not (acct == DEMO or self.cfg["mode"]["write"]):
            raise ValueError("批量删除或移到垃圾邮件前，请先在「设置」中开启处理模式。")
        return Actions(self.store, acct, self.provider(acct), lambda: self.open_mailbox(acct), True)

    def accounts_view(self) -> list[dict]:
        return [{"key": m["address"].lower(), **{k: m.get(k) for k in ("provider", "address", "host", "port")}, "own": bool(m.get("own")),
                 "has_secret": self._has_secret(m["address"]), "synced": self.store.has_items(m["address"].lower()),
                 "window_note": WINDOW_NOTE if self.store.cache_get(m["address"].lower(), "window_only")[0] else None}
                for m in self.cfg["accounts"]]

    def state(self) -> dict:
        e = self.cfg["engine"]
        accounts = self.accounts_view()
        first = accounts[0] if accounts else {"provider": None, "address": None, "host": None, "port": 993, "has_secret": False}
        return {
            "version": __version__,
            "providers": providers.public_list(),
            "accounts": accounts,
            "mail": first,                    # the first mailbox: "is any mailbox connected", the wizard's form
            "engine": e, "profile": self.cfg["profile"], "fetch": self.cfg["fetch"], "mode": self.cfg["mode"],
            "machine": engines.machine_summary(),
            "store": {"kind": self.secrets.kind, "label": self.secrets.label, "plaintext": self.secrets.plaintext},
            "demo_today": samples.TODAY.isoformat(),
            "synced": {"demo": self.store.has_items(DEMO), **{a["key"]: a["synced"] for a in accounts}},
            "ops": OPS, "rule_kinds": RULE_KINDS, "actions": triage.ACTIONS, "rule_actions": triage.RULE_ACTIONS,
            "categories": triage.CATEGORIES, "gmail_filter_actions": gmailfilters.FILTER_LABELS,
            "auto": self.auto_view(), "memory": self.curator.counts(), "hub_only": self.hub_only, "hub_model": self.hub_model(),
            "notify": self.notify_view(),            # whether macOS shows the app's notifications (None: can't tell)
            "power": {"level": self.cfg["power"]["level"], "factor": engines.POWER.get(self.cfg["power"]["level"], 0.0)},
            "threads": {"on": self.cfg["threads"]["on"]},
            "flow": {"mark_read": self.cfg.get("flow", {}).get("mark_read", True)},
            # a notification asked for this page (今日处理): where the page opens, once, within the hour
            "start_page": self._start_page(),
            # said once, at the top of the inbox: 了解你 is on from the start (ADR 0013)
            "learn_intro": bool(self.cfg["learn"].get("on") and not self.cfg["learn"].get("intro_seen") and self.mail_accounts()),
        }

    def ping(self) -> dict:
        return {"ok": True, "pid": os.getpid(), "service": self.service, "version": __version__}

    def _start_page(self) -> str | None:
        hint = self.store.cache_get("", "open_page")[0] or {}
        return hint.get("page") if time.time() - (hint.get("at") or 0) < 3600 else None

    def activity(self, seen: bool = False) -> dict:
        """What the page checks every few seconds: a running job to follow, the idle work waiting behind it (both
        shown in 进行中 on every page), and background sorting's status. `seen`: the page is visible."""
        if seen:
            self._seen = time.monotonic()
            if self.cfg["engine"].get("via") == "hub":
                self._warmth.keep(self.cfg["engine"].get("hub_socket"))
        job = self.running_job()
        return {"job": self.public_job(job) if job else None, "queue": self.idle_queue(), "auto": self.auto_view(),
                "waiting_jobs": [{"id": j["id"], "kind": j["kind"], "account": j.get("account")}
                                 for j in self.queued_jobs() if not job or j["id"] != job["id"]],
                "guesses": self.curator.counts()["proposed"],       # the 记忆 badge: guesses waiting for the user
                "model": self.model_note(),                         # 螺壳主控 is not ready to sort yet, and why
                "waiting": {a: self.store.count_unanalyzed(a) for a in self.mail_accounts() + [DEMO]},
                "review": {a: self.review.unseen(a) for a in self.mail_accounts()}}     # 今日处理's badge

    # what 螺壳主控 says about sorting, in a sentence for the page (V1 §8: the task's status, reason, progress)
    NOT_READY = {        # (the inbox's sentence, the rail's few words)
        "downloading": ("螺壳正在下载模型{progress}。下载好以后自动开始分析。", "正在下载模型{progress}"),
        # also a stretch of 螺壳主控's first run (the engine waits behind the apps): nothing for the user to do yet
        "needs_download": ("螺壳还没有开始下载模型。下载好以后自动开始分析；一直没有开始的话，请打开菜单栏的螺壳查看。",
                           "等待下载模型"),
        "paused": ("螺壳暂时不能运行模型（{reason}）。恢复以后自动开始分析。", "模型暂停中"),
        "unsupported": ("这台 Mac 上暂时不能分析邮件（{reason}）。", "暂时不能分析邮件"),
    }

    def hub_model(self) -> dict | None:
        """Which model 螺壳主控 sorts with, and whether it is ready: {"name", "ready"} for the settings row
        (luoke-mail#6, brand spec: 「螺壳主控 · 模型名」 and a ready mark). The name is the task's `model_name`, the
        one the hub gives for people to read (luoke-core 5e67d4b); the combo is an opaque label, never taken apart
        (V1 §8). An older hub without it: no name shown. None when not on the hub or it cannot be asked now."""
        e = self.cfg["engine"]
        path = hub.socket_path(e.get("hub_socket")) if e.get("via") == "hub" else None
        if not path:
            return None
        try:
            t = hub.Hub(path).tasks().get("tasks", {}).get(f"{hub.ACCESSORY}.triage") or {}
        except engines.EngineError:
            return None
        return {"name": str(t.get("model_name") or ""), "ready": t.get("status") == "available"}

    def model_note(self) -> dict | None:
        """None when sorting can start now (or the model is a service the user picked); otherwise
        {"status", "text", "short", "progress"} -- what the inbox and the rail show while 螺壳主控 gets ready. Asked afresh
        every few seconds while not ready, so the page can start the waiting work the moment it is."""
        e = self.cfg["engine"]
        if e.get("via") != "hub":
            return None
        at, last = self._model_note
        if time.monotonic() - at < 4:
            return last
        path = hub.socket_path(e.get("hub_socket"))
        note = None
        if not path:
            note = {"status": "absent", "text": "螺壳没有在运行。打开螺壳后，邮件会接着分析。",
                    "short": "螺壳没有在运行", "progress": None}
        else:
            try:
                t = hub.Hub(path).tasks(fresh=last is not None).get("tasks", {}).get(f"{hub.ACCESSORY}.triage") or {}
            except engines.EngineError as ex:
                note = {"status": "error", "text": str(ex), "short": "连不上螺壳", "progress": None}
            else:
                status = t.get("status") or "unsupported"
                if status != "available":
                    p = t.get("progress")
                    fill = {"progress": f"（{round(p * 100)}%）" if isinstance(p, (int, float)) else "",
                            "reason": t.get("reason") or "原因不明"}
                    text, short = (x.format(**fill) for x in self.NOT_READY.get(status, self.NOT_READY["unsupported"]))
                    if status == "needs_download" and str(t.get("reason") or "").startswith("下载失败"):
                        text, short = "模型下载失败，请在菜单栏的螺壳里重试。下载好以后自动开始分析。", "模型下载失败"
                    note = {"status": status, "text": text, "short": short, "progress": p}
        self._model_note = (time.monotonic(), note)
        return note

    def idle_queue(self) -> dict:
        """How much idle work is waiting (switched on, not paused), and how long it holds back because the model
        did not fit."""
        out = {"older": 0, "judge": 0, "read": 0, "learn": 0, "wait": round(engines.wait_for())}
        for acct in self.mail_accounts() + [DEMO]:
            q, _ = self.store.cache_get(acct, "older")
            out["older"] += len((q or {}).get("uids") or []) + (len(self.store.unanalyzed(acct, older=True)) if q else 0)
            for scope in self.SCOPES:
                if self.judging(acct, scope):
                    out["judge"] += self.store.old_unjudged(acct, scope, triage.held)
            r, _ = self.store.cache_get(acct, "read")
            if (r or {}).get("on", True):
                out["read"] += self.store.digest_mail_counts(acct).get("queued", 0)
            if self.learner.switch(acct).get("on"):
                out["learn"] += len(self.learner.state(acct).get("sent_todo") or [])
        return out

    # ---- setup ----------------------------------------------------------------------------
    def save_mail(self, body: dict) -> dict:
        """Connect a mailbox, or change one (`key`: the one being changed). A new address is a new mailbox: the
        local data of the old one stays under its address until it is removed."""
        provider = body.get("provider")
        if provider not in providers.PROVIDERS:
            raise ValueError("请选择邮箱类型。")
        address = (body.get("address") or "").strip()
        if "@" not in address:
            raise ValueError("请填写完整的邮箱地址。")
        accounts = self.cfg["accounts"]
        old = self.mailbox_cfg(str(body.get("key") or "").lower()) if body.get("key") else None
        same = self.mailbox_cfg(address.lower())
        if same and same is not old:
            if old:
                raise ValueError("这个邮箱已经连接了。")
            old = same                                       # connecting it again: update it
        secret = (body.get("secret") or "").strip()
        if secret:
            self.secrets.set(MAIL_SECRET, address, secret)
        elif old and old["address"] != address:
            moved = self.secrets.get(MAIL_SECRET, old["address"])
            if moved:
                self.secrets.set(MAIL_SECRET, address, moved)
        if old and old["address"] != address:
            self.secrets.delete(MAIL_SECRET, old["address"])
        if not self.secrets.get(MAIL_SECRET, address):
            raise ValueError("请填写授权码。")
        host = (body.get("host") or "").strip() or None
        port = int(body.get("port") or providers.PROVIDERS[provider]["port"])
        entry = {"provider": provider, "address": address, "host": host, "port": port}
        if old:
            accounts[accounts.index(old)] = entry
        else:
            accounts.append(entry)
        self._secret_known.clear()
        config.save(self.cfg)
        self.adopt_rules()
        return {**self.state()["mail"], "key": address.lower(), "accounts": self.accounts_view()}

    def remove_mail(self, body: dict) -> dict:
        """Disconnect a mailbox: its password leaves the keychain and luoke's local data about it (the list,
        rules, 清理, 精选, the log) is deleted. The mailbox itself is not touched; what luoke learnt about the
        person stays in the memory (it is about them, not about one mailbox)."""
        acct = str(body.get("key") or "").lower()
        m = self.mailbox_cfg(acct)
        if not m:
            raise ValueError("没有连接这个邮箱，请刷新页面。")
        for j in self.jobs.values():                         # whatever runs or waits for this mailbox stops
            if j["status"] == "running" and j.get("account") == acct:
                j["cancel"] = True
        self.secrets.delete(MAIL_SECRET, m["address"])
        self.cfg["accounts"].remove(m)
        self._secret_known.clear()
        config.save(self.cfg)
        self.store.clear_account(acct)
        exiting = False
        if not self.cfg["accounts"] and self.cfg["auto"]["enabled"]:      # nothing left to sort in the background
            self.cfg["auto"]["enabled"] = False
            config.save(self.cfg)
            exiting = self._remove_login_item("disabled")
        return {"ok": True, "exiting": exiting, "accounts": self.accounts_view(), "message": f"已移除 {m['address']}"}

    def test_mail(self, body: dict | None = None) -> dict:
        acct = self.account((body or {}).get("source") or (body or {}).get("key"))
        t0 = time.time()
        with self.open_mailbox(acct) as mb:
            stats = mb.stats()
        return {"ok": True, "seconds": round(time.time() - t0, 1), **stats}

    def save_engine(self, body: dict) -> dict:
        if body.get("via") == "hub":               # 螺壳主控 picks the model; the direct choice is kept for going back
            if not self.hub_view().get("ok"):
                raise ValueError(hub.NOT_RUNNING)
            self.cfg["engine"]["via"] = "hub"
            config.save(self.cfg)
            return self.cfg["engine"]
        if self.hub_only:
            raise ValueError("螺壳邮件助手的模型由螺壳运行，不需要另外选择。")
        base = (body.get("base_url") or "").strip().rstrip("/")
        model = (body.get("model") or "").strip()
        if not base.startswith(("http://", "https://")) or not model:
            raise ValueError("请选择模型服务和模型。")
        key = (body.get("api_key") or "").strip()
        if key:
            self.secrets.set(ENGINE_SECRET, base, key)
        if body.get("clear_api_key"):
            self.secrets.delete(ENGINE_SECRET, base)
        self.cfg["engine"] = {"base_url": base, "model": model, "engine_name": body.get("engine_name"),
                              "has_api_key": bool(self.secrets.get(ENGINE_SECRET, base))}
        config.save(self.cfg)
        return self.cfg["engine"]

    def scan_engines(self, body: dict) -> dict:
        typed = {k.rstrip("/"): v for k, v in (body.get("keys") or {}).items() if isinstance(v, str) and v.strip()}
        extra = []
        custom = (body.get("custom") or "").strip().rstrip("/")
        if custom.startswith(("http://", "https://")):
            extra.append({"name": "自定义", "base_url": custom})
        e = self.cfg["engine"]
        if e.get("base_url"):
            extra.append({"name": e.get("engine_name") or "已保存", "base_url": e["base_url"]})
        keys = {}
        for eng in engines.KNOWN_ENGINES + extra:
            k = typed.get(eng["base_url"]) or self.secrets.get(ENGINE_SECRET, eng["base_url"])
            if k:
                keys[eng["base_url"]] = k.strip()
        return {**engines.scan(extra, keys), "hub": self.hub_view()}

    def hub_view(self) -> dict:
        """螺壳主控 as the model page shows it: found only when it is running here (until it ships, a hub means
        someone started one), and the tasks it does not offer (yet). Which model it uses is the
        hub's business: its label is recorded with each answer, never read for meaning (V1 §8)."""
        path = hub.socket_path(self.cfg["engine"].get("hub_socket"))
        if not path:
            return {"found": False}
        try:
            d = hub.Hub(path).tasks(fresh=True)
        except engines.EngineError as ex:
            return {"found": True, "ok": False, "error": str(ex)}
        tasks = d.get("tasks") or {}
        sort = tasks.get(f"{hub.ACCESSORY}.triage") or {}
        off = [t for t in engines.TASKS if (tasks.get(f"{hub.ACCESSORY}.{t}") or {}).get("status") != "available"]
        return {"found": True, "ok": True, "status": sort.get("status"), "reason": sort.get("reason"), "unavailable": off}

    def test_engine(self) -> dict:
        """Analyse one email with the saved model: the newest inbox message when a mailbox is connected."""
        if not self.has_model():
            raise ValueError("请先选择模型。")
        mail, today, me, source, warning = samples.build()[0]["mail"], samples.TODAY, samples.ME, "demo", None
        accts = self.mail_accounts()
        if accts:
            try:
                with self.open_mailbox(accts[0]) as mb:
                    newest = mb.fetch_recent(limit=1, unseen_only=False, days=None)
                if newest:
                    mail, today, me, source = newest[0], None, self.address(accts[0]), "inbox"
                else:
                    warning = "收件箱为空，已改用示例邮件测试。"
            except MailError as ex:
                warning = f"无法读取收件箱，已改用示例邮件测试。{ex}"
        r = triage.analyze(mail, self.link("user-waiting"), memory.about(self.mem if source == "inbox" else self.demo_mem),
                           self.cfg["profile"]["language"], today, me)
        return {"ok": True, "result": r, "source": source, "warning": warning,
                "mail": {k: mail.get(k) for k in ("subject", "from_name", "from_addr", "date", "body")}}

    def save_profile(self, body: dict) -> dict:
        p, f = self.cfg["profile"], self.cfg["fetch"]
        if body.get("signature") is not None:
            p["signature"] = str(body["signature"])[:60]
        for k, hi in (("limit", 500), ("days", 365)):
            if body.get(k) is not None:
                try:
                    f[k] = max(1, min(int(str(body[k]).strip()), hi))
                except ValueError:
                    raise ValueError("请填写数字。") from None
        config.save(self.cfg)
        return {"profile": p, "fetch": f}

    def save_power(self, body: dict) -> dict:
        level = body.get("level")
        if level not in engines.POWER:
            raise ValueError("请选择功耗。")
        self.cfg["power"]["level"] = level
        config.save(self.cfg)
        engines.set_power(level)
        return {"level": level, "factor": engines.POWER[level]}

    def save_mode(self, body: dict) -> dict:
        write = bool(body.get("write"))
        if write and not body.get("consent"):
            raise ValueError("请先确认说明。")
        self.cfg["mode"]["write"] = write
        config.save(self.cfg)
        return self.cfg["mode"]

    # ---- background jobs --------------------------------------------------------------------
    # whenever nothing else runs: judging old mail, reading newsletters, guessing 关注的话题, choosing the picks, 了解你
    IDLE_KINDS = ("older", "judge", "read", "profile", "edit", "learn")
    WAITING = "等待其他任务完成当前一步…"

    def running_job(self) -> dict | None:
        """The job that counts: what the user (or background sorting) started and is under way, else one waiting
        its turn, else idle work. Idle work asked to make way may still finish its current step while the new job
        waits for it."""
        running = [j for j in self.jobs.values() if j["status"] == "running"]
        mine = [j for j in running if j["kind"] not in self.IDLE_KINDS]
        return next((j for j in mine if not j.get("queued")), None) or next(iter(mine), None) or next(iter(running), None)

    def queued_jobs(self) -> list[dict]:
        return [self.public_job(j) for j in self.jobs.values() if j["status"] == "running" and j.get("queued")]

    # reading, not changing the mailbox: asked for while something else runs, they wait their turn (issue #4)
    QUEUE_KINDS = ("sync", "old_scan", "subscriptions", "filter_preview")
    JOB_NAMES = {"sync": "同步邮件", "auto": "后台自动整理", "old_scan": "清理：读取邮件", "clean": "清理：处理邮件",
                 "subscriptions": "订阅体检", "filter_preview": "预演过滤器"}

    def _start_job(self, kind: str, fn, **extra) -> dict:
        """Start `fn(job)` in a thread. Idle work has the lowest priority: anything else asks it to stop after
        the step it is in (a model call can take a minute) and starts at once, waiting for it -- the page shows
        that instead of making the user wait for an answer. Reading the mailbox (同步, 清理's reading, 订阅体检)
        asked for while the user's own work runs waits its turn, shown as waiting on every page; the same request
        again joins it. What changes the mailbox does not wait: it is asked again when the other is done."""
        with self.lock:
            busy = self.running_job()
            ahead = [j for j in self.jobs.values() if j["status"] == "running" and j["kind"] not in self.IDLE_KINDS]
            if ahead and kind not in self.IDLE_KINDS:
                same = next((j for j in ahead if j["kind"] == kind and all(j.get(k) == v for k, v in extra.items())), None)
                if same:
                    return {**self.public_job(same), "joined": True}
                if kind not in self.QUEUE_KINDS:
                    name = self.JOB_NAMES.get(busy["kind"], "另一项任务")
                    raise ValueError(f"「{name}」正在进行，完成后请再试一次。")
            elif busy and kind in self.IDLE_KINDS:
                raise ValueError("另一项任务正在进行，请稍后再试。")
            elif busy:
                busy["cancel"] = True
            jid = os.urandom(6).hex()
            stage = (f"等待「{self.JOB_NAMES.get(ahead[-1]['kind'], '其他任务')}」完成…" if ahead and kind not in self.IDLE_KINDS
                     else self.WAITING if busy else "准备中")
            job = {"id": jid, "kind": kind, "status": "running", "stage": stage, "done": 0, "total": 0, "result": None,
                   "error": None, "cancel": False, "started": time.strftime("%H:%M:%S"),
                   "queued": bool(ahead and kind not in self.IDLE_KINDS), **extra}
            self.jobs[jid] = job
        before = ahead if job["queued"] else [busy] if busy else []

        def run():
            idle = desktop.InstanceLock(config.CONFIG_DIR, "idle.lock") if kind in self.IDLE_KINDS else None
            try:
                while any(b["status"] == "running" for b in before) and not job["cancel"]:
                    time.sleep(0.2)
                if job["queued"]:
                    with self.lock:
                        job["queued"] = False
                        for j in self.jobs.values():            # idle work that slipped in between: it makes way
                            if j["status"] == "running" and j["kind"] in self.IDLE_KINDS:
                                j["cancel"] = True
                    while any(j["status"] == "running" and j["kind"] in self.IDLE_KINDS for j in self.jobs.values()) \
                            and not job["cancel"]:
                        job["stage"] = self.WAITING
                        time.sleep(0.2)
                if idle and not idle.acquire():             # 螺壳主控's idle job is at it: it takes its turn there
                    job["cancel"] = True
                if job["cancel"]:
                    job["status"] = "cancelled"
                    return
                job["stage"] = "准备中"
                job["result"] = fn(job)
                job["status"] = "cancelled" if job["cancel"] else "done"
            except Exception as ex:  # noqa: BLE001
                job["status"] = "error"
                job["error"] = str(ex) if isinstance(ex, (MailError, ValueError, engines.EngineError)) else f"{type(ex).__name__}: {ex}"
                desktop.log_crash(ex, f"job {kind}")
            finally:
                job["stage"] = ""
                if idle:
                    idle.release()
        threading.Thread(target=run, daemon=True).start()
        return self.public_job(job)

    @staticmethod
    def public_job(job: dict) -> dict:
        return {k: v for k, v in job.items() if k != "cancel"}

    def _pipeline(self, acct: str, mails: list[dict], job: dict, today, me: str, analyze: bool = True,
                  user: bool = False, older: bool = False) -> dict:
        """`user`: the user started it (同步新邮件) -- each message is one answer they watch while the page is
        open; background sorting is background, and waits out a busy hub."""
        p = self.cfg["profile"]
        link = self.link(self.watching if user else "background", battery=user, patient=lambda: job["cancel"], job=job)
        about = memory.about(self.memory_for(acct))
        sources = {s["key"] for s in self.store.digest_sources(acct)}

        def fn(item, rule):
            if item.get("auth") == "pass":        # the code may weigh the pass; only for a sender seen before
                item = {**item, "sender_seen": self.store.sender_seen(acct, triage.seen_as(item.get("from_addr")), item.get("message_id"))}
            return triage.analyze(item, link, about, p["language"], today, me, rule,
                                  digest_source=subscriptions.sender_key(item) in sources)

        def progress(label, done, total):
            job.update(stage=f"正在分析：{label[:30]}", done=done, total=total)
        # 螺壳主控 still downloading or checking: the mail waits for it instead of each message waiting in turn
        ready = analyze and link.ready("triage")
        return pipeline.sync(self.store, acct, mails, fn if ready else None, progress, lambda: job["cancel"], link, older)

    def sync_mailbox(self, acct: str, job: dict, analyze: bool = True, user: bool = False) -> dict:
        """Bring the list in step with the inbox and sort what's new (the manual sync and background sorting).
        Mail of the last `days` days is sorted one by one, read or not; only bodies not seen before are
        downloaded. Older mail is only counted here -- 清理旧邮件 clears it in bulk."""
        f = self.cfg["fetch"]
        job["stage"] = "正在读取新邮件"
        with self.open_mailbox(acct) as mb:
            recent = mb.recent_uids(f["days"])
        known = self.store.known_uids(acct)
        new, cut = self._fetch_new(acct, [u for u in recent if u not in known][:f["limit"]], job)
        job.update(stage="正在核对收件箱", done=0, total=0)
        mails: list[dict] = []
        with self.open_mailbox(acct) as mb:
            present = mb.inbox_uids()
            self._name_unnamed(acct, mb, present)
            self._group_mailings(acct, mb)
            self.store.cache_set(acct, "window_only", self.provider(acct) in ("163", "126") and bool(present)
                                 and not mb.count_before(WINDOW_DAYS))
            self._recheck_senders(acct, mb, present)
            gone = self.store.mark_missing(acct, present)
            self._handled_elsewhere(acct, mb, gone, present)
            self._queue_older(acct, present, set(recent))
            back = self._reopen_present(acct, mb, present)
            old = mb.count_before(f["days"])
            self._gmail_ids(acct, mb, present)
            mails += self._recent_archived(acct, mb)
        self.store.cache_set(acct, "old_count", {"count": old, "days": f["days"]})
        r = self._pipeline(acct, mails, job, None, acct, analyze, user)
        r = {**r, "fetched": r["fetched"] + new["fetched"], "new": r["new"] + new["new"]}
        since = (dt.datetime.now() - dt.timedelta(days=self.ARCHIVED_DAYS)).strftime("%Y-%m-%d %H:%M")
        self.store.settle_archived(acct, background.NEEDS_YOU, since)
        self.curator.queue_weekly(acct)
        self._mark_sorted_read(acct)
        auto = self._carry_out_habits(acct) + self._carry_out_own(acct)
        self._threads(acct, job, user)
        return {**r, "gone": len(gone), "back": back, "in_range": len(recent), "old": old, "auto": auto,
                **({"cut": cut} if cut else {})}

    MAILING_DAYS, MAILING_LEAST, MAILING_ASKS = 90, 5, 20

    def _group_mailings(self, acct: str, mb) -> None:
        """Most mailboxes add no list headers (139 none at all): a sender of five messages in 90 days whom
        the user never wrote to is a mailing, judged as one group. Who the user did write to is remembered."""
        self.store.group_known_senders(acct)
        since = (dt.datetime.now() - dt.timedelta(days=self.MAILING_DAYS)).strftime("%Y-%m-%d %H:%M")
        known = set(self.store.cache_get(acct, "wrote_to")[0] or [])
        ask = [a for a in self.store.frequent_senders(acct, since, self.MAILING_LEAST) if a not in known and a not in self._me(acct)]
        if not ask:
            return
        ask = ask[:self.MAILING_ASKS]
        wrote = mb.wrote_to(ask)
        self.store.cache_set(acct, "wrote_to", sorted(known | wrote))
        for address in ask:
            if address not in wrote:
                self.store.group_sender(acct, address)

    def _name_unnamed(self, acct: str, mb, present: set[str]) -> None:
        rows = self.store.unnamed(acct, present)
        got = mb.identities([r["uid"] for r in rows]) if rows else {}
        for r in rows:
            if r["uid"] in got:
                self.store.renamed(r["id"], got[r["uid"]]["message_id"], None if r["date"] else got[r["uid"]]["date"])

    FETCH_BATCH = 10

    def _fetch_new(self, acct: str, uids: list[str], job: dict) -> tuple[dict, str | None]:
        """Read the new mail a few at a time and keep each batch at once: a connection that stalls on the 37th of 100
        loses that batch at most, never what came before (luoke-mail#19: one timeout threw all of it away, and the
        page said 0 / 0 the whole time). A broken connection is opened once more and the reading goes on; if it
        breaks again, what was read is kept and the next sync goes on from there. Returns ({"fetched", "new"}, a
        sentence for the user when it was cut short)."""
        counts, i, retried, last = {"fetched": 0, "new": 0}, 0, False, None
        job.update(stage="正在读取新邮件", done=0, total=len(uids))
        while i < len(uids):
            try:
                with self.open_mailbox(acct) as mb:
                    while i < len(uids):
                        if job["cancel"]:
                            return counts, None
                        chunk = uids[i:i + self.FETCH_BATCH]
                        got = mb.fetch_messages(chunk)
                        for m in got:
                            counts["new"] += self.store.upsert_message(acct, m)[1]
                        self.store.gmail_ids_set(acct, {m["message_id"]: m["gm_msgid"] for m in got if m.get("gm_msgid")})
                        counts["fetched"] += len(got)
                        i += len(chunk)
                        retried = False
                        job.update(done=i)
            except MailError as ex:
                print(f"[luoke-mail] reading new mail, {i}/{len(uids)}: {type(ex).__name__}", file=sys.stderr)
                last = ex
                if retried:
                    break
                retried = True                      # once more, on a fresh connection
        if i < len(uids):
            if not counts["fetched"]:
                raise last                          # nothing read: the mailbox's own sentence, as before
            return counts, f"读到 {i} / {len(uids)} 封时连接中断，已读到的已保存，下次同步接着读。"
        return counts, None

    def _mark_sorted_read(self, acct: str) -> int:
        """What the model sorted is marked read (ADR 0016, 2026-10-19: 「AI 处理过的应该就是已读了」): the
        user reviews it in 今日处理 instead of in the mail app's unread count. Marked as the mail assistant's doing
        (`read_by_us`), so it is never taken for the user reading it -- not as a habit, not as a newsletter
        opened -- and taken off the unread baseline that tells what the user read elsewhere."""
        if not self.cfg.get("flow", {}).get("mark_read", True):
            return 0
        items = self.store.unread_sorted(acct)
        if not items:
            return 0
        at = time.strftime("%Y-%m-%d %H:%M:%S")
        if acct != DEMO:
            try:
                with self.open_mailbox(acct) as mb:
                    mb.store_flags("INBOX", [it["uid"] for it in items], "(\\Seen)")
            except MailError as ex:
                print(f"[luoke-mail] marking sorted mail read: {type(ex).__name__}", file=sys.stderr)
                return 0
            unseen = self.store.cache_get(acct, "unseen")[0]
            if unseen is not None:
                done = {it["uid"] for it in items}
                self.store.cache_set(acct, "unseen", [u for u in unseen if u not in done])
        # in the operation log like everything else that changes the mailbox, undone from there or per message in
        # 今日处理; never a mail-acted record (it is not the user's doing)
        self.store.log(acct, "seen", [it["id"] for it in items], True, f"分析后标为已读 {len(items)} 封",
                       [{"kind": "unseen", "message_id": it["message_id"], "uid": it["uid"]} for it in items if it.get("message_id")])
        for it in items:
            self.store.set_fields(it["id"], read_by_us=at)
        return len(items)

    # ---- unread mail from before the range ---------------------------------------------------------------------
    OLDER_BATCH = 10

    def _queue_older(self, acct: str, present: set[str], recent: set[str]) -> None:
        """Unread inbox mail older than the range is sorted too, once (2026-10-20: a couple of hundred such messages sat
        unread, never sorted): when a mailbox is first synced -- also the ones connected before this -- what is
        unread then and before the range is queued, newest first, and sorted as idle work (`_older_slice`), then
        marked read and shown in 今日处理 like the rest. Only that once: mail the user marks unread later is theirs.
        A wider range needs nothing of this -- the next syncs sort what it takes in, read or not."""
        if acct == DEMO or self.store.cache_get(acct, "older")[0] is not None:
            return
        unseen = set(self.store.cache_get(acct, "unseen")[0] or [])          # just read by _handled_elsewhere
        known = self.store.known_uids(acct)
        uids = sorted((unseen & present) - recent - known, key=int, reverse=True)
        self.store.cache_set(acct, "older", {"uids": uids, "total": len(uids), "at": time.strftime("%Y-%m-%d %H:%M")})

    def _older_next(self) -> str | None:
        if not self.has_model():
            return None
        for acct in self.mail_accounts():
            q = self.store.cache_get(acct, "older")[0] or {}
            if q.get("uids") or (q.get("total") and any(not it.get("error") for it in self.store.unanalyzed(acct, older=True))):
                return acct
        return None

    def _older_slice(self, acct: str, job: dict, seconds: float = 240) -> dict:
        """Sort the queued unread mail from before the range for up to `seconds`, OLDER_BATCH at a time: read
        (without marking it read), put on the list, sorted at background level -- 主控 makes way for the user --
        then marked read like any sorted mail. A batch taken in stays on the list until it is sorted."""
        link = self.link("background", job=job)
        if not link.ready("triage"):
            return {"sorted": 0}
        q = self.store.cache_get(acct, "older")[0] or {}
        uids, total, t0, done = list(q.get("uids") or []), q.get("total") or 0, time.time(), 0
        while not job["cancel"] and time.time() - t0 < seconds:
            mails = []
            left = [it for it in self.store.unanalyzed(acct, older=True) if not it.get("error")]
            if uids and not left:
                batch, uids = uids[:self.OLDER_BATCH], uids[self.OLDER_BATCH:]
                with self.open_mailbox(acct) as mb:
                    mails = mb.fetch_messages(batch)
                for m in mails:
                    m["older"] = True
                self.store.cache_update(acct, "older", uids=uids)
            elif not left:
                break
            job.update(stage="正在分析以前的未读邮件", done=total - len(uids), total=total)
            r = self._pipeline(acct, mails, job, None, acct, older=True)
            done += len(r["decided"])
            if r["waiting"] or (not r["decided"] and r["errors"]):
                break                                       # 主控 said not now, or the model failed: later
        if done:
            self._mark_sorted_read(acct)
        return {"sorted": done}

    ELSEWHERE_LOOKUPS = 30          # gone mail looked up in the trash per sync: archived or deleted

    def _handled_elsewhere(self, acct: str, mb, gone: list[int], present: set[str]) -> None:
        """What the user did to their mail outside the mail assistant -- archived, deleted or read it on the phone
        or Gmail's own page -- is theirs to learn habits from too (2026-10-19). What the mail assistant
        did itself never counts: only mail still on its list, and none that the operation log has touched lately
        (an interrupted move, an undo). Reading the trash and the flags is read-only."""
        if acct == DEMO:
            return
        since = (dt.datetime.now() - dt.timedelta(days=3)).strftime("%Y-%m-%d")
        ours = {i for e in self.store.log_entries(acct, limit=300)          # marking read moves nothing: not counted
                if (e.get("ts") or "") >= since and e["action"] != "seen" for i in e["item_ids"]}
        items = [it for it in (self.store.get(i) for i in gone) if it and it["id"] not in ours and it.get("place") == "inbox"]
        try:
            trash = mb.folder_for("trash")
        except MailError:
            trash = None
        moved: dict[str, list[dict]] = {"archive": [], "trash": []}
        for it in items[:self.ELSEWHERE_LOOKUPS] if trash else []:     # where it went is looked up, never guessed
            try:
                moved["trash" if mb.locate(trash, it["message_id"]) else "archive"].append(it)
            except MailError:
                continue
        unseen = mb.unseen_uids()
        before = self.store.cache_get(acct, "unseen")[0]
        self.store.cache_set(acct, "unseen", sorted(unseen))
        read = []
        if before is not None:                                # the first look only notes what is unread
            was = set(before) - unseen
            read = [it for it in self.store.items(acct, "pending")
                    if it.get("uid") in was and it.get("uid") in present and it["id"] not in ours and it.get("place") == "inbox"]
        for op, its in (("archive", moved["archive"]), ("trash", moved["trash"]), ("read", read)):
            if its:
                self._acted(acct, its, op, where="elsewhere")
        if self.provider(acct) == "gmail":
            self._labelled_elsewhere(acct, mb, moved["archive"], present, ours)

    def _labelled_elsewhere(self, acct: str, mb, archived: list[dict], present: set[str], ours: set[int]) -> None:
        """Gmail: labels the user put on their mail themselves (2026-10-19: a habit can be "某商城的促销都
        加上标签「购物」"). Mail still in the inbox: its labels now against the last look (the first look only notes
        them). Mail the user archived: its labels in All Mail -- labelling and archiving are often one move. Labels
        the mail assistant put on are its own (the operation log), and Gmail's system labels are not the user's."""
        def mine(labels) -> set[str]:
            return {x for x in labels or [] if not x.startswith("\\")}
        auto = {x.get("label") for x in (self.store.cache_get(acct, "auto_logs")[0] or {}).values() if x.get("label")}
        added: dict[str, list[dict]] = {}
        pending = [it for it in self.store.items(acct, "pending") if it.get("uid") in present and it["id"] not in ours]
        before = self.store.cache_get(acct, "labels")[0]
        now: dict[str, list[str]] = {}
        if pending:
            try:
                mb.select("INBOX", readonly=True)
                state = mb.state_of([it["uid"] for it in pending])
            except MailError:
                state = {}
            for it in pending:
                have = mine((state.get(it["uid"]) or {}).get("labels"))
                now[it["message_id"]] = sorted(have)
                if before is not None and it["message_id"] in before:
                    for label in have - set(before[it["message_id"]]) - auto:
                        added.setdefault(label, []).append(it)
        self.store.cache_set(acct, "labels", now)
        try:
            arch = mb.folder_for("archive")
        except MailError:
            arch = None
        for it in archived[:self.ELSEWHERE_LOOKUPS] if arch else []:
            try:
                uid = mb.locate(arch, it["message_id"])
                labels = mine((mb.state_of([uid]).get(uid) or {}).get("labels")) if uid else set()
            except MailError:
                continue
            for label in labels - set((before or {}).get(it["message_id"]) or []) - auto:
                added.setdefault(label, []).append(it)
        for label, its in added.items():
            self._acted(acct, its, "label", where="elsewhere", label=label)

    THREAD_DAYS, THREADS_PER_SYNC = 30, 10

    def _threads(self, acct: str, job: dict, user: bool = False) -> None:
        """Whose turn it is in the last month's conversations (2026-10-17): the inbox and the sent
        folder, grouped by subject and the other person; for each one the user wrote last and has waited on, and
        each long one, the model says who has to act now. A conversation is looked at again only when it has a
        new message; at most THREADS_PER_SYNC per sync. Never on mailbox errors -- it is a side matter of a sync."""
        if not self.cfg["threads"]["on"] or not self.has_model():
            return
        link = self.link(self.watching if user else "background", battery=user, patient=lambda: job["cancel"], job=job)
        if not link.ready("thread"):
            return                                        # 螺壳主控 has no such task (yet): nothing is asked
        today = (samples.TODAY if acct == DEMO else dt.date.today()).isoformat()
        since = (dt.date.fromisoformat(today) - dt.timedelta(days=self.THREAD_DAYS)).isoformat()
        received = [it for it in self.store.items(acct, None, place=None) if (it.get("date") or "") >= since]
        try:
            if acct == DEMO:
                sent = [m for m in samples.demo_sent() if (m.get("date") or "") >= since]
            else:
                with self.open_mailbox(acct) as mb:
                    folder = mb.folder_for("sent")
                    sent = mb.fetch_messages(mb.folder_uids(folder, self.THREAD_DAYS)[:60], folder)
        except (MailError, KeychainLocked) as ex:
            print(f"[luoke-mail] threads: {type(ex).__name__}", file=sys.stderr)
            return
        me = {acct.lower(), (self.address(acct) if acct != DEMO else samples.ME).lower()}
        convs = threads.collect(received, sent, me, today, learn.own_text)
        known = self.store.cache_get(acct, "threads")[0] or {}
        out, asked = {}, 0
        for c in convs:
            was = known.get(c["key"])
            if was and was.get("last_mid") == c["last_mid"]:
                out[c["key"]] = {**was, "items": c["items"], "waiting": c["waiting"]}
                continue
            if asked >= self.THREADS_PER_SYNC or job["cancel"]:
                continue                                  # next sync
            job["stage"] = "正在看往来邮件卡在谁那里"
            try:
                verdict = threads.judge(c["messages"], link)
            except engines.Busy:
                break
            except engines.EngineError as ex:
                print(f"[luoke-mail] threads: {type(ex).__name__}", file=sys.stderr)
                break
            asked += 1
            out[c["key"]] = {"last_mid": c["last_mid"], "last_date": c["last_date"], "with": c["with"], "name": c["name"],
                             "subject": c["subject"], "items": c["items"], "waiting": c["waiting"], **verdict}
        self.store.cache_set(acct, "threads", out)

    def waiting_on_others(self, acct: str) -> list[dict]:
        """Conversations the user wrote last in, waiting on the other side for WAIT_DAYS or more, newest first."""
        convs = (self.store.cache_get(acct, "threads")[0] or {}).values()
        return sorted(({"subject": c["subject"], "name": c["name"], "since": c.get("since") or c["last_date"],
                        "what": c.get("what") or ""} for c in convs if c.get("state") == "them" and c.get("waiting")),
                      key=lambda c: c["since"], reverse=True)

    def _thread_of(self, acct: str) -> dict[int, dict]:
        """item id -> where its conversation stands (them / you), for the card."""
        out = {}
        for c in (self.store.cache_get(acct, "threads")[0] or {}).values():
            if c.get("state") in ("them", "you"):
                for i in c.get("items") or []:
                    out[i] = {"state": c["state"], "label": threads.STATES[c["state"]], "what": c.get("what") or ""}
        return out

    def _carry_out_habits(self, acct: str) -> list[dict]:
        """Confirmed habits done unasked (top-level ADR 0016): mail of the sender and kind the user confirmed they
        always archive, mark read or label is archived, marked read or labelled after it is sorted -- without
        处理模式 (it can all be taken back), never mail that looks suspicious or needs the user (reply, schedule,
        follow up), always listed in 今日处理 and undoable. What is done is the rule's, not the user's: recorded as
        such for the memory."""
        if acct == DEMO:
            return []
        mem = self.memory_for(acct)
        habits: dict[tuple, list[dict]] = {}
        for h in memory.auto_habits(mem, acct):
            habits.setdefault((h["sender"], h["category"]), []).append(h)       # archive, and a label too, maybe
        if not habits:
            return []
        groups: dict[tuple, list[dict]] = {}
        undone = set(self.store.cache_get(acct, "auto_undone")[0] or [])     # the user took it back: not again
        applied = set(self.store.cache_get(acct, "auto_applied")[0] or [])   # labelled already: it stays in the inbox
        for it in self.store.items(acct, "pending"):
            if not it.get("action") or it.get("suspicious") or it["action"] in background.NEEDS_YOU or it["message_id"] in undone \
                    or self._not_routine(it):
                continue
            for h in habits.get((subscriptions.sender_key(it), it.get("category")), []):   # this sender's this kind only
                if f"{h['note_id']}:{it['message_id']}" not in applied:
                    groups.setdefault((h["note_id"], h["action"], h.get("label")), []).append(it)
        done = []
        acts = self.actions(acct)
        # labels first: archiving takes the mail out of the inbox, where a label is put on
        for (note_id, op, label), its in sorted(groups.items(), key=lambda g: g[0][1] != "label"):
            its = [it for it in its if (self.store.get(it["id"]) or {}).get("state") == "pending"]
            if not its:
                continue
            try:
                r = acts.execute([it["id"] for it in its], op, by="rule", label=label)
            except (MailError, ActionError) as ex:
                print(f"[luoke-mail] habit {note_id}: {type(ex).__name__}", file=sys.stderr)
                continue
            if not r.get("done"):
                continue
            key = subscriptions.sender_key(its[0])
            name = its[0].get("from_name") or its[0].get("from_addr") or key
            words = f"加上标签「{label}」" if op == "label" else ACT_WORDS[op]
            memory.happened(mem, acct, "mail-auto", f"按习惯把「{name}」的 {len(r['done'])} 封{words}", sender=key,
                            by="rule", data={"note_id": note_id, "action": op, "count": len(r["done"]), "label": label,
                                             "category": its[0].get("category"), "name": name})
            logs = self.store.cache_get(acct, "auto_logs")[0] or {}
            logs[str(r["log_id"])] = {"note_id": note_id, "sender": key, "op": op, "label": label}
            self.store.cache_set(acct, "auto_logs", dict(list(logs.items())[-200:]))
            mids = {self.store.get(i)["message_id"] for i in r["done"] if self.store.get(i)}
            applied |= {f"{note_id}:{m}" for m in mids}
            self.store.cache_set(acct, "auto_applied", sorted(applied)[-5000:])
            done.append({"log_id": r["log_id"], "count": len(r["done"]), "action": op, "name": name, "message": r["message"]})
        return done

    def own_mailbox(self, acct: str) -> bool:
        """螺壳's own mailbox (ADR 0016 第 5 条): one the user gave it, with what it should look at forwarded there."""
        return any(m["address"].lower() == acct and m.get("own") for m in self.cfg["accounts"])

    def set_own(self, body: dict) -> dict:
        acct = self.account(body.get("source"))
        for m in self.cfg["accounts"]:
            if m["address"].lower() == acct:
                m["own"] = bool(body.get("own"))
        config.save(self.cfg)
        return {"accounts": self.accounts_view()}

    OWN_OPS = {"archive": "archive", "ignore": "trash"}

    def _carry_out_own(self, acct: str) -> list[dict]:
        """In 螺壳's own mailbox, reading, archiving and deleting are all done unasked (ADR 0016 第 5 条,
        2026-10-19): what was sorted to archive is archived, what was sorted for the trash goes there -- never what
        looks suspicious or needs the user, each listed in 今日处理 and undone there until the trash empties."""
        if acct == DEMO or not self.own_mailbox(acct):
            return []
        undone = set(self.store.cache_get(acct, "auto_undone")[0] or [])
        groups: dict[str, list[int]] = {}
        for it in self.store.items(acct, "pending"):
            op = self.OWN_OPS.get(it.get("action") or "")
            if op and not it.get("suspicious") and it.get("analyzed_at") and it["message_id"] not in undone:
                groups.setdefault(op, []).append(it["id"])
        done = []
        acts = self.actions(acct)
        for op, ids in groups.items():
            try:
                r = acts.execute(ids, op, by="own")
            except (MailError, ActionError) as ex:
                print(f"[luoke-mail] own mailbox {op}: {type(ex).__name__}", file=sys.stderr)
                continue
            if r.get("done"):
                logs = self.store.cache_get(acct, "auto_logs")[0] or {}
                logs[str(r["log_id"])] = {"note_id": None, "sender": None, "op": op, "own": True}
                self.store.cache_set(acct, "auto_logs", dict(list(logs.items())[-200:]))
                done.append({"log_id": r["log_id"], "count": len(r["done"]), "action": op, "name": "螺壳的邮箱", "message": r["message"]})
        return done

    def undo(self, source, log_id: int, only: list[int] | None = None) -> dict:
        """Undo from the page (`only`: one message of it, from 今日处理). Undoing what a habit did is the user
        saying "not this time": recorded against it."""
        acct = self.account(source)
        e = self.store.log_entry(log_id)
        r = self.actions(source, write=False).undo(log_id, only)
        if e and e["action"] == "seen":                 # marked unread again: not marked read again
            for i in (only or e["item_ids"]):
                self.store.set_fields(i, read_by_us="unread")
        if e and e["action"] == "auto" and acct != DEMO:
            back = [it["message_id"] for it in (self.store.get(i) for i in (only or e["item_ids"])) if it]
            kept = (self.store.cache_get(acct, "auto_undone")[0] or []) + back
            self.store.cache_set(acct, "auto_undone", kept[-2000:])
            hit = (self.store.cache_get(acct, "auto_logs")[0] or {}).get(str(log_id))
            if hit and hit.get("note_id"):                  # a habit's work (not the own mailbox's): against it
                memory.happened(self.memory_for(acct), acct, "mail-undo-auto", "撤销了一次按习惯的自动处理", sender=hit["sender"],
                                data={"note_id": hit["note_id"]})
        return r

    ARCHIVED_DAYS, ARCHIVED_MOST = 7, 50
    RECHECK_MOST = 100

    def _recheck_senders(self, acct: str, mb: Mailbox, present: set[str]) -> None:
        """Mail sorted before the provider's verdict on the sender was kept (0.13) and still waiting for the user
        gets that verdict now; where it bears on the suggestion -- flagged suspicious though the domain checks
        out, or not flagged though it fails -- the mail is sorted again (2026-10-16: a card issuer's notice
        kept its phishing flag after the update)."""
        rows = [r for r in self.store.auth_unchecked(acct) if r["uid"] in present][:self.RECHECK_MOST]
        found = mb.sender_checks([r["uid"] for r in rows]) if rows else {}
        if not found:
            return
        verdicts, again = {}, []
        for r in rows:
            mid, verdict = found.get(r["uid"], (None, None))
            if not same_mid(mid, r["message_id"]):
                continue                                  # not the message we know under that UID: leave it
            verdicts[r["id"]] = verdict or "none"
            # a pass bears on a flag: the model is told it, or the code weighs it (settle_risks)
            if verdict in ("pass", "platform") and r["suspicious"] or verdict == "fail" and not r["suspicious"]:
                again.append(r["id"])
        self.store.set_auth(verdicts, again)

    def _recent_archived(self, acct: str, mb: Mailbox) -> list[dict]:
        """Mail the user archived from the last days -- often on the phone, before a sync saw it in the inbox -- is
        sorted too, so an appointment or a question in it is not missed (2026-10-15: a clinic booking).
        Only what needs the user stays on the page (settle_archived); nothing is moved back. Gmail for now."""
        folder, found = mb.archived_recent(self.ARCHIVED_DAYS)
        if not folder:
            return []
        known = self.store.known_mids(acct)
        new = [u for u, mid in found if mid and mid not in known][:self.ARCHIVED_MOST]
        mails = mb.fetch_messages(new, folder) if new else []
        self.store.gmail_ids_set(acct, {m["message_id"]: m["gm_msgid"] for m in mails if m.get("gm_msgid")})
        for m in mails:
            m.update(uid=None, place="archive")              # no inbox UID: the inbox's own bookkeeping leaves it alone
        return mails

    def _gmail_ids(self, acct: str, mb: Mailbox, present: set[str], most: int = 5000, moved_most: int = 100) -> None:
        """Gmail's own id for mail known from before (so 在 Gmail 中打开 opens the message itself), a few
        thousand per sync. The id stays the same when mail is archived, so 精选 mail that has left the inbox is
        found in the archive by its Message-ID (one search each, a hundred per sync). Mail found nowhere is
        marked, not looked for again."""
        if not mb.has("X-GM-EXT-1"):
            return
        by_folder, moved = self.store.gmail_ids_missing(acct, present)
        for folder, wanted in by_folder.items():
            uids = sorted(wanted, key=int, reverse=True)[:most]
            if not uids:
                continue
            found = mb.gmail_ids(folder, uids)
            self.store.gmail_ids_set(acct, {wanted[u]: found.get(u, "") for u in uids})
            most -= len(uids)
            if most <= 0:
                return
        if moved:
            archive = mb.folder_for("archive")
            where = {mid: mb.locate(archive, mid) for mid in moved[:moved_most]}
            found = mb.gmail_ids(archive, [u for u in where.values() if u])
            self.store.gmail_ids_set(acct, {mid: found.get(uid, "") if uid else "" for mid, uid in where.items()})

    def _reopen_present(self, acct: str, mb: Mailbox, present: set[str]) -> int:
        """The mailbox is the truth: mail recorded as handled that is still in the inbox goes back on the list
        (with its suggestion), so a move the server didn't carry out can't hide mail for good."""
        cands = self.store.handled_but_present(acct, present)
        if not cands:
            return 0
        mids = mb.message_ids([c["uid"] for c in cands])
        ids = [c["id"] for c in cands if mids.get(c["uid"]) == c["message_id"]]
        self.store.set_state(ids, "pending")
        return len(ids)

    def start_sync(self, body: dict) -> dict:
        acct = self.account(body.get("source"))
        if not self.has_model():
            raise ValueError("请先选择本机模型。")
        busy = self.running_job()
        if busy and busy["kind"] == "auto" and acct != DEMO and busy.get("account") in (None, acct):
            # background sorting is at this mailbox, or about to go through them all
            return {**self.public_job(busy), "joined": True}
        # at another mailbox: this one's sync waits its turn (luoke-mail#4), see _start_job

        def work(job):
            if acct == DEMO:
                self.store.clear_account(DEMO, keep_digest=True)      # the demo replays from scratch every time
                old = len(samples.demo_old_mail(self.cfg["fetch"]["days"]))
                self.store.cache_set(DEMO, "old_count", {"count": old, "days": self.cfg["fetch"]["days"]})
                inbox = samples.demo_inbox()
                r = self._pipeline(DEMO, inbox, job, samples.TODAY, samples.ME, user=True)
                self.curator.queue_weekly(DEMO)
                return {**r, "gone": 0, "in_range": len(inbox), "old": old}
            return self.sync_mailbox(acct, job, user=True)
        return self._start_job("sync", work, account=acct)

    # ---- 清理: old mail, judged message by message by the model ---------------------------------------------
    # scope "inbox": inbox mail older than the sorting window; scope "archive": archived mail. Headers only;
    # the model gives each message a kind; the kind decides what ticking it does and whether it is ticked.
    SCOPES = ("inbox", "archive")

    @staticmethod
    def old_key(m: dict) -> str:
        return group_key(m) or ("person:" + (m.get("from_addr") or "").lower())

    def _scope(self, body: dict) -> tuple[str, str, str]:
        """(source, account, scope): the source is the account itself ("demo" for the sample inbox)."""
        if body.get("source") in (None, "", "mailbox") and len(self.mail_accounts()) > 1:
            raise ValueError("没有指明是哪个邮箱，请刷新页面后重试。")
        acct = self.account(body.get("source"))
        scope = body.get("scope") or "inbox"
        if scope not in self.SCOPES:
            raise ValueError("未知的清理范围。")
        return acct, acct, scope

    @staticmethod
    def old_action(scope: str, kind: str | None, personal: bool = False) -> tuple[str, bool]:
        """(what ticking a group does, ticked by default) for a verdict. Archived mail can only go to the trash;
        old inbox mail goes to the trash if it is spent, to the archive otherwise -- except what may still be open.
        Mail with a person (`personal`: not bulk, or a reply / forward) is never suggested for the trash, whatever
        the verdict from its headers (luoke-mail#20: a colleague's 「Re: 上季度预算的几处疑问」 judged a one-off
        notice and ticked for the trash): old inbox mail of theirs is archived, archived mail of theirs kept."""
        if scope == "archive":
            return "trash", kind in triage.DELETE_KINDS and not personal
        if kind in triage.DELETE_KINDS and not personal:
            return "trash", True
        # not judged yet: archived only when the user ticks it (10-23, luoke-mail#32: 34,000 unjudged went
        # with 3,600 judged in one 处理 while the judging was paused)
        return "archive", bool(kind) and kind != "todo"

    REPLY_SUBJECT = re.compile(r"^\s*(re|fw|fwd|aw|wg|回复|答复|转发)\s*[:：]", re.I)

    # a sender address that is a system's, not a person's: its one-off notices may still go to the trash
    AUTOMATED = re.compile(r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|notif|notice|alert|service|support|info|news|mailer|"
                           r"account|billing|security|system|admin|hello|team|marketing|promo|update|bounce|postmaster|"
                           r"order|shop|store|receipt|bank|card|verify|auth|member|club|digest|weekly|daily|report)", re.I)

    def _personal_groups(self, rows: list[dict]) -> set[str]:
        """Group keys (old_key) of mail with a person: a reply or forward, or a message not sent in bulk from an
        address that is not a system's (a shop's service@ sends its one-off notices without list headers too)."""
        def person(r: dict) -> bool:
            if self.REPLY_SUBJECT.match(r.get("subject") or ""):
                return True
            local = (r.get("from_addr") or "").split("@")[0]
            return not r.get("bulk") and not r.get("list_id") and bool(local) and not self.AUTOMATED.match(local)
        return {self.old_key(r) for r in rows if person(r)}

    def start_old_scan(self, body: dict) -> dict:
        source, acct, scope = self._scope(body)

        def work(job):
            self._read_old(acct, scope, job)
            return self.old_view(acct, scope)
        return self._start_job("old_scan", work, account=acct, scope=scope)

    def _read_old(self, acct: str, scope: str, job: dict) -> None:
        """Read the headers of a scope's old mail into old_mail (headers only, the mailbox is only examined)."""
        days = self.cfg["fetch"]["days"]
        partial = False
        if acct == DEMO:
            folder, rows = ("INBOX", samples.demo_old_mail(days)) if scope == "inbox" else ("[Gmail]/All Mail", samples.demo_archive())
        else:
            stage = "正在读取收件箱中的旧邮件" if scope == "inbox" else "正在读取已归档邮件"
            job["stage"] = stage
            with self.open_mailbox(acct) as mb:
                folder, rows = mb.old_headers(scope, days, progress=lambda d, t: job.update(stage=stage, done=d, total=t))
                partial = bool(mb.unreturned)
        self.store.old_sync(acct, scope, rows, partial)
        if acct != DEMO:
            self.store.gmail_ids_set(acct, {r["message_id"]: r["gm_msgid"] for r in rows if r.get("gm_msgid")})
        self.store.cache_set(acct, f"scan:{scope}", {"at": time.strftime("%Y-%m-%d %H:%M"), "ts": time.time(),
                                                      "folder": folder, "days": days})
        if scope == "inbox":
            self.store.cache_set(acct, "old_count", {"count": len(rows), "days": days})

    # 清理's old inbox mail is read and judged by itself, in idle time (10-25, luoke-mail#33): it only
    # reads headers and asks the local model; nothing in the mailbox changes until the user ticks and confirms
    RESCAN_AFTER = 3600

    def judging(self, acct: str, scope: str) -> bool:
        """The model judges this scope's old mail in idle time: by default for old inbox mail, when the user
        switched it on for archived mail (often tens of thousands); a pause by the user holds for both."""
        j, _ = self.store.cache_get(acct, f"judge:{scope}")
        return bool((j or {}).get("on", scope == "inbox"))

    def _old_unread(self, acct: str) -> bool:
        """Old inbox mail the sync counted that 清理 has not read: never read, or read before the count moved
        (then again at most hourly, should the two keep disagreeing)."""
        told = (self.store.cache_get(acct, "old_count")[0] or {}).get("count") or 0
        scan, _ = self.store.cache_get(acct, "scan:inbox")
        if not scan:
            return told > 0
        if told == self.store.old_read(acct, "inbox"):
            return False
        return time.time() - (scan.get("ts") or 0) > self.RESCAN_AFTER

    def old_summary(self, acct: str) -> dict | None:
        """For the inbox's line about old mail: how many, and once judged, what is suggested for them."""
        old, _ = self.store.cache_get(acct, "old_count")
        if not old or not old.get("count") or not self.store.cache_get(acct, "scan:inbox")[0]:
            return old
        rows = [r for r in self.store.old_rows(acct, "inbox") if not triage.held(r)]
        personal = self._personal_groups(rows)
        out = {**old, "total": len(rows), "judged": 0, "archive": 0, "trash": 0, "todo": 0,
               "judging": self.judging(acct, "inbox")}
        for r in rows:
            if not r["kind"]:
                continue
            out["judged"] += 1
            action, suggested = self.old_action("inbox", r["kind"], self.old_key(r) in personal)
            key = "todo" if r["kind"] == "todo" else action if suggested else None
            if key:
                out[key] += 1
        return out

    def clean_note(self, acct: str) -> tuple[str, str] | None:
        """Once, when the old inbox mail has all been judged: what is suggested, for the user to look over."""
        ready, _ = self.store.cache_get(acct, "clean_ready")
        if not ready or ready.get("told"):
            return None
        self.store.cache_set(acct, "clean_ready", {**ready, "told": time.time()})
        s = self.old_summary(acct) or {}
        if not s.get("judged"):
            return None
        parts = [f"建议归档 {s['archive']} 封" if s.get("archive") else None,
                 f"删除 {s['trash']} 封" if s.get("trash") else None,
                 f"待你确认 {s['todo']} 封" if s.get("todo") else None]
        self.store.cache_set("", "open_page", {"page": "clean", "at": time.time()})
        return (f"超过 {s.get('days', 30)} 天的 {s['judged']} 封邮件已分析",
                "，".join(p for p in parts if p) + "。点此在「清理」中查看，确认后才会处理。")

    def old_view(self, acct: str, scope: str) -> dict:
        scan, _ = self.store.cache_get(acct, f"scan:{scope}")
        if not scan:
            return {"scanned": False, "scope": scope}
        rows = self.store.old_rows(acct, scope)
        personal = self._personal_groups(rows)
        groups: dict[str, dict] = {}
        flagged = 0
        for r in rows:
            if triage.held(r):                     # starred or kept by a label: never part of a bulk clean-up
                flagged += 1
                continue
            kind = r["kind"] or "pending"
            key = f"{kind}|{self.old_key(r)}"
            mine = self.old_key(r) in personal
            action, suggested = self.old_action(scope, r["kind"], mine)
            g = groups.setdefault(key, {"key": key, "kind": kind, "action": action, "suggested": suggested,
                                        # judged a one-off notice or an ad, but it is with a person: listed as such
                                        # (luoke-mail#20), its key and verdict unchanged
                                        "shown": "personal" if mine and r["kind"] in triage.DELETE_KINDS else None,
                                        "bulk": bool(r["bulk"]), "addr": r["from_addr"], "name": None, "count": 0, "size": 0,
                                        "unread": 0, "latest": "", "subjects": [], "labels": set()})
            g["count"] += 1
            g["size"] += r["size"] or 0
            g["unread"] += int(not r["seen"])
            g["labels"].update(r["labels"])
            g["subjects"].append((r["date"] or "", r["subject"] or "（无主题）"))
            if (r["date"] or "") >= g["latest"]:
                g["latest"], g["name"] = r["date"] or "", r["from_name"] or r["from_addr"]
        for g in groups.values():
            g["subjects"] = [x for _, x in sorted(g["subjects"], reverse=True)[:3]]
            g["labels"] = sorted(g["labels"])[:3]
        kept = [r for r in rows if not triage.held(r)]
        reading = self.curator.in_clean(acct, kept)
        for g in groups.values():
            g["digest"] = reading.get(g["key"])
        judge, _ = self.store.cache_get(acct, f"judge:{scope}")
        busy = self.running_job()
        keep, _ = self.store.cache_get(acct, f"keep:{scope}")
        # the inbox's bar counts old mail at every sync; the scan here may be days older. When they differ the page
        # reads again (10-23: 「1 封超过 30 天」 on the bar, nothing here from a scan of 10-06)
        told = (self.store.cache_get(acct, "old_count")[0] or {}).get("count") if scope == "inbox" else None
        stale = told is not None and told != self.store.old_read(acct, scope)   # not len(rows): those leave out the
        # older unread already listed in the inbox, so a mailbox with them looked stale on every visit
        return {"scanned": True, "stale": stale, "scope": scope, "at": scan["at"], "days": scan.get("days"), "folder": scan["folder"],
                "total": len(kept), "flagged": flagged, "size": sum(r["size"] or 0 for r in kept),
                "judged": sum(1 for r in kept if r["kind"]),
                "judging": {"on": self.judging(acct, scope), "problem": (judge or {}).get("problem"),
                            "running": bool(busy and busy["kind"] == "judge" and busy.get("account") == acct and busy.get("scope") == scope)},
                "kinds": triage.OLD_KINDS, "keep": keep or [],
                "reading_on": (self.store.cache_get(acct, "read")[0] or {}).get("on", True),
                "groups": sorted(groups.values(), key=lambda g: (-g["count"], g["latest"])), "provider": self.provider(acct)}

    def set_judging(self, body: dict) -> dict:
        """The user asks the model to look at every old message of a scope (or pauses it). It runs in the
        background, a few minutes at a time, whenever nothing else is running."""
        source, acct, scope = self._scope(body)
        if body.get("on") and not self.has_model():
            raise ValueError("请先选择本机模型。")
        self.store.cache_set(acct, f"judge:{scope}", {"on": bool(body.get("on")), "problem": None})
        if body.get("on"):
            self.idle_step(prefer="judge")
        else:
            busy = self.running_job()
            if busy and busy["kind"] == "judge" and busy.get("scope") == scope:
                busy["cancel"] = True
        return self.old_view(acct, scope)

    def _judge_next(self) -> tuple[str, str] | None:
        """(account, scope) the model should look at next: asked for, work left, the model set, and no failure in
        the last minute (the model service may be off). Old inbox mail first: it is what the user sees."""
        if not self.has_model():
            return None
        for acct in self.idle_accounts():
            for scope in self.SCOPES:
                j, _ = self.store.cache_get(acct, f"judge:{scope}")
                if not self.judging(acct, scope) or time.time() - ((j or {}).get("failed_at") or 0) <= 60:
                    continue
                if scope == "inbox" and self._old_unread(acct) or self.store.old_unjudged(acct, scope, triage.held):
                    return acct, scope
        return None

    def idle_accounts(self) -> list[str]:
        """Whose idle work there is: the demo's (when the user tries it) and every mailbox's -- not the demo's in
        螺壳主控's job, which runs when nobody is there."""
        return self.mail_accounts() if self.in_job else [DEMO] + self.mail_accounts()

    def idle_pending(self) -> bool:
        if engines.wait_for():
            return False
        return bool(self._here(self._idle_work()))

    def _here(self, work: list[tuple]) -> list[tuple]:
        """In the app, 螺壳主控 decides when idle work runs (its registered job `idle`, ADR 0006; the owner,
        2026-10-16): here only what the user just started (开始分析, turning 了解你 on, 重新推测...) carries on at
        once, and the demo, which only runs while someone tries it. Run from source, all of it, as before."""
        if not self.hub_only:
            return work
        self._asked &= {kind for kind, _, _ in work}         # what the user started is done: 主控 takes it from here
        return [w for w in work if w[0] in self._asked or w[2].get("account") == DEMO]

    def idle_step(self, prefer: str | None = None) -> None:
        """Start a slice of idle work if there is any and nothing else is running (the background loop calls this).
        The kinds take turns, a few minutes each, so a long queue (two thousand newsletters) cannot keep the others
        waiting for hours; what the user just asked for (`prefer`) goes first."""
        if prefer:
            self._asked.add(prefer)
        if self.running_job() or (engines.wait_for() and not prefer):   # the model did not fit, or the hub is busy: wait
            return
        work = self._here(self._idle_work())
        if not work:
            return
        after = [w for w in work if self.IDLE_KINDS.index(w[0]) > self.IDLE_KINDS.index(self._last_idle)] if self._last_idle else []
        kind, fn, extra = next((w for w in work if w[0] == prefer), None) or (after or work)[0]
        self._last_idle = kind
        try:
            self._start_job(kind, fn, **extra)
        except ValueError:
            pass

    def next_idle(self, kinds: tuple[str, ...] = IDLE_KINDS) -> tuple | None:
        """The next slice in turn among these kinds, for 螺壳主控's idle job (run_idle_job)."""
        work = [w for w in self._idle_work() if w[0] in kinds]
        if not work:
            return None
        after = [w for w in work if self.IDLE_KINDS.index(w[0]) > self.IDLE_KINDS.index(self._last_idle)] if self._last_idle else []
        step = (after or work)[0]
        self._last_idle = step[0]
        return step

    def _idle_work(self) -> list[tuple]:
        work = []
        acct = self._older_next()
        if acct:
            work.append(("older", lambda job, a=acct: self._older_slice(a, job), {"account": acct}))
        nxt = self._judge_next()
        if nxt:
            work.append(("judge", lambda job, a=nxt[0], s=nxt[1]: self._judge_slice(a, s, job), {"account": nxt[0], "scope": nxt[1]}))
        acct = self.curator.next_account()
        if acct:
            work.append(("read", lambda job, a=acct: self.curator.read_slice(a, job), {"account": acct}))
        acct = self.profiler.next_account()
        if acct:
            work.append(("profile", lambda job, a=acct: self.profiler.run(a, job), {"account": acct}))
        acct = self.curator.edit_next_account()
        if acct:
            work.append(("edit", lambda job, a=acct: self.curator.edit_slice(a, job), {"account": acct}))
        acct = self.learner.next_account()
        if acct:
            work.append(("learn", lambda job, a=acct: self.learner.slice(a, job), {"account": acct}))
        return work

    def _judge_slice(self, acct: str, scope: str, job: dict, seconds: float = 240) -> dict:
        """Judge unjudged old mail for up to `seconds`: biggest senders first, batches of up to 25 messages (mostly
        one sender, so the model sees the context). Stops early when asked to make way."""
        if scope == "inbox" and self._old_unread(acct):           # not read yet, or read before the count moved
            try:
                self._read_old(acct, scope, job)
            except MailError as ex:
                self.store.cache_update(acct, f"judge:{scope}", problem=str(ex), failed_at=time.time())
                return {"judged": 0}
        link = self.link("background", job=job)
        everything = self.store.old_rows(acct, scope)
        rows = [r for r in everything if r["kind"] is None and not triage.held(r)]
        sizes: dict[str, int] = {}
        for r in rows:
            sizes[self.old_key(r)] = sizes.get(self.old_key(r), 0) + 1
        rows.sort(key=lambda r: (-sizes[self.old_key(r)], self.old_key(r), r["date"] or ""))
        total = len([r for r in everything if not triage.held(r)])
        today = samples.TODAY if acct == DEMO else None
        t0, judged, i = time.time(), 0, 0
        while i < len(rows):
            if job["cancel"] or time.time() - t0 > seconds:
                break
            job.update(stage="正在分析", done=total - len(rows) + i, total=total)
            try:
                kinds, count = triage.classify_old(rows[i:], link, today, scope)
            except engines.Busy:
                break                                     # the hub has more urgent work: come back later
            except engines.EngineError as ex:
                self.store.cache_update(acct, f"judge:{scope}", problem=str(ex), failed_at=time.time())
                break
            batch, i = rows[i:i + count], i + count
            self.store.old_judge(acct, {batch[n - 1]["message_id"]: k for n, k in kinds.items()}, link.name)
            self.store.cache_update(acct, f"judge:{scope}", problem=None)     # never "on": that is the user's switch
            judged += len(kinds)
        if scope == "inbox" and judged and not self.store.old_unjudged(acct, scope, triage.held):
            self.store.cache_set(acct, "clean_ready", {"at": time.time()})   # all judged: told once (clean_note)
        return {"judged": judged}

    def start_old_apply(self, body: dict) -> dict:
        """Carry out the ticked groups: each goes to the archive or the trash, as its verdict says."""
        source, acct, scope = self._scope(body)
        keys = set(body.get("keys") or [])
        rows = [r for r in self.store.old_rows(acct, scope) if not triage.held(r)]
        picked = [r for r in rows if f"{r['kind'] or 'pending'}|{self.old_key(r)}" in keys]
        if not picked:
            raise ValueError("请选择要处理的邮件。")
        personal = self._personal_groups(rows)
        act_of = lambda r: self.old_action(scope, r["kind"], self.old_key(r) in personal)       # noqa: E731 -- as the page says
        trashing = any(act_of(r)[0] == "trash" for r in picked)
        acts = self.actions(acct, irreversible=trashing)     # archiving needs only the click; the trash, 处理模式
        if body.get("count") is not None and int(body["count"]) != len(picked):
            raise ValueError("列表有了变化，请重新确认要处理的邮件。")          # not more than the user saw
        # suggested groups left unticked are a decision too ("keep these"): they stay unticked next time
        suggested = {f"{r['kind'] or 'pending'}|{self.old_key(r)}" for r in rows if act_of(r)[1]}
        keep = (set(self.store.cache_get(acct, f"keep:{scope}")[0] or []) | (suggested - keys)) - keys
        self.store.cache_set(acct, f"keep:{scope}", sorted(keep))
        archive = [r for r in picked if act_of(r)[0] == "archive"]
        trash = [r for r in picked if act_of(r)[0] == "trash"]
        kept = self.curator.kept_mids(acct)               # holds a 订阅精选 pick: kept and starred, not trashed
        star = [r for r in trash if r["message_id"] in kept]
        trash = [r for r in trash if r["message_id"] not in kept]

        def work(job):
            def progress(done, total):
                job.update(stage=f"正在处理（{done}/{total}）", done=done, total=total)
            self.learner.before_clean(acct, trash, job)       # 先学后清: what goes to the trash is read first
            # 停止 between batches: what is done stays done and undoable (luoke-mail#32)
            r = acts.clear_old(archive, trash, progress, star, stop=lambda: job["cancel"])
            gone = set(r.get("trashed") or [])
            groups: dict[str, dict] = {}                      # the clean-up itself, per sender, as a record
            for row in trash:
                if row["message_id"] not in gone:
                    continue
                key = subscriptions.sender_key(row)               # list:<id> / from:<address>, as agreed with luoke-mem
                g = groups.setdefault(key, {"key": key, "name": row.get("from_name") or row.get("from_addr") or key,
                                            "addr": row.get("from_addr"), "count": 0,
                                            "category": triage.OLD_KINDS.get(row["kind"] or "", "邮件"), "by": "suggestion"})
                if f"{row['kind'] or 'pending'}|{self.old_key(row)}" not in suggested:
                    g["by"] = "user"                                  # ticked by the user, not by the suggestion
                g["count"] += 1
            self.learner.cleared(acct, list(groups.values()))
            if scope == "inbox":
                left = max(0, (self.store.cache_get(acct, "old_count")[0] or {}).get("count", 0) - r["moved"])
                self.store.cache_set(acct, "old_count", {"count": left, "days": self.cfg["fetch"]["days"]})
            return r
        return self._start_job("clean", work, account=acct)

    # ---- background sorting ------------------------------------------------------------------
    def start_auto_sync(self) -> dict:
        return self._start_job("auto", self.auto.run)

    def auto_accounts(self) -> list[str]:
        """The mailboxes background sorting works on, when it is on and a model is chosen: every connected one --
        also one whose password went missing, which is a mailbox problem to tell the user about."""
        if not (self.cfg["auto"]["enabled"] and self.has_model()):
            return []
        return [m["address"].lower() for m in self.cfg["accounts"]]

    def auto_view(self) -> dict:
        a = self.cfg["auto"]
        st = self.auto.overall()
        next_in = None
        if a["enabled"] and st.get("last_try"):
            next_in = max(0, round((st["last_try"] + a["every"] * 60 - time.time()) / 60))
        return {**a, "intervals": background.INTERVALS, "desktop": self.desktop.status(), "service": self.service,
                "last": st.get("last"), "next_in_min": next_in}

    def set_auto(self, body: dict) -> dict:
        a = self.cfg["auto"]
        every = int(body.get("every") or a["every"])
        if every not in background.INTERVALS:
            raise ValueError("请选择整理间隔。")
        if not body.get("enabled"):
            a.update(enabled=False, every=every)
            config.save(self.cfg)
            exiting = self._remove_login_item("disabled")
            return {"ok": True, "exiting": exiting, "handoff": False, "auto": self.auto_view(), "message": "已关闭后台自动整理"}
        was_on = a["enabled"]
        if not was_on and not body.get("consent"):
            raise ValueError("请先确认说明。")
        self.account(None)                         # some mailbox must be connected
        if not self.has_model():
            raise ValueError("请先选择本机模型。")
        a.update(enabled=True, every=every)
        config.save(self.cfg)
        handoff = False
        if self.desktop.supported and not self.service:
            t0 = time.time()
            try:
                self.desktop.enable(self.port)
                if not self.desktop.wait_for_service(t0):
                    self.desktop.disable()
                    self._print_service_log()
                    raise DesktopError("后台服务未能启动，已恢复原状。原因见终端输出。")
            except (DesktopError, OSError) as ex:
                a["enabled"] = False
                config.save(self.cfg)
                if isinstance(ex, OSError):
                    desktop.log_crash(ex, "login item")
                    raise DesktopError("无法添加登录项。") from ex
                raise
            handoff = True
            self.exit_soon("handoff")             # the service is waiting for this server to step aside
        if not was_on:   # now, while the user is looking: macOS asks once whether this app may notify
            self.desktop.notify(background.TITLE, "后台自动整理已开启",
                                f"{background.every_text(every)}整理一次，有需要处理的新邮件时会通知你。")
        msg = f"已开启后台自动整理，{background.every_text(every)}一次" + ("" if self.desktop.supported else "（程序运行期间）")
        return {"ok": True, "handoff": handoff, "exiting": False, "auto": self.auto_view(), "message": msg}

    def _remove_login_item(self, reason: str) -> bool:
        """Remove the login item and the launcher. Returns True when this process is going away with it."""
        if not self.desktop.status()["installed"]:
            return False
        if self.service:
            self.exit_soon(reason, self.desktop.disable)   # answer first: removing the login item ends this process
            return True
        self.desktop.disable()
        return False

    def exit_soon(self, reason: str, then=None) -> None:
        """Stop serving right after the current response went out."""
        self.exit_reason = reason

        def go():
            time.sleep(0.5)
            if then:
                then()
            if self.on_exit:
                self.on_exit()
        threading.Thread(target=go, daemon=True).start()

    def _print_service_log(self) -> None:
        try:
            with open(os.path.join(config.CONFIG_DIR, "service.log"), encoding="utf-8", errors="replace") as f:
                tail = f.read()[-2000:]
            print("[luoke-mail] background service log:\n" + tail, file=sys.stderr)
        except OSError:
            pass

    def start_subscriptions(self, body: dict) -> dict:
        acct = self.account(body.get("source"))
        days = max(7, min(int(body.get("days") or 60), 365))

        def work(job):
            if acct == DEMO:                     # the days asked for, as a mailbox would (luoke-mail#27)
                since = (samples.TODAY - dt.timedelta(days=days)).isoformat()
                headers = [h for h in samples.demo_headers() if (h.get("date") or "")[:10] >= since]
            else:
                job["stage"] = f"正在读取最近 {days} 天的邮件"
                with self.open_mailbox(acct) as mb:
                    headers = mb.fetch_headers(days=days)
                    todo = list(subscriptions.needs_body_check(headers, self.store.suspicious_senders(acct)).values())[:30]
                    for i, h in enumerate(todo, 1):
                        job.update(stage=f"正在查找退订方式（{i}/{len(todo)}）", done=i, total=len(todo))
                        raw = mb.fetch_raw(h["uid"]) if h.get("uid") else None
                        h["body_unsub"] = find_unsubscribe(raw) if raw else None
            headers = [h for h in headers if (h.get("from_addr") or "").lower() not in self._me(acct)]     # not a sender
            rows = subscriptions.summarize(headers, self.store.suspicious_senders(acct), days, self.store.read_by_us(acct))
            note = self._check_senders(acct, headers, rows, job)
            result = {"days": days, "scanned": len(headers), "senders": rows, "at": time.strftime("%Y-%m-%d %H:%M"), "model_note": note}
            self.store.cache_set(acct, "subscriptions", result)
            return self.subscriptions_view(acct)
        return self._start_job("subscriptions", work, account=acct)

    def _check_senders(self, acct: str, headers: list[dict], rows: list[dict], job: dict) -> str | None:
        """Before suggesting to unsubscribe (or to trash all future mail), the model reads each such sender's
        recent subjects: statements, login alerts and personal mail are rarely opened but worth keeping.
        Verdicts are kept per sender, so a re-scan only asks about new ones. Returns a note if it couldn't look."""
        todo = [r for r in rows if r["verdict"] in ("unsubscribe", "filter")]
        if not todo:
            return None
        if not self.has_model():
            return "未选择本机模型，本次建议仅依据收信和阅读情况。"
        link = self.link(self.watching, battery=True, patient=lambda: job["cancel"], job=job)
        known = self.store.cache_get(acct, "sender_kinds")[0] or {}
        need = [r for r in todo if r["key"] not in known]
        subjects = subscriptions.recent_subjects(headers, {r["key"] for r in need})
        note, i = None, 0
        while i < len(need):
            job.update(stage="正在分析发件人", done=i, total=len(need))
            try:
                kinds, count = triage.classify_senders(
                    [{**r, "subjects": subjects.get(r["key"]) or r["subjects"]} for r in need[i:i + triage.SENDER_BATCH]], link)
            except engines.EngineError as ex:
                note = f"{ex}本次建议仅依据收信和阅读情况。"
                break
            batch, i = need[i:i + count], i + count
            known.update({batch[n - 1]["key"]: k for n, k in kinds.items()})
        self.store.cache_set(acct, "sender_kinds", known)
        for r in todo:
            kind = known.get(r["key"])
            if kind and kind not in triage.UNSUB_SENDER_KINDS:
                r["verdict"] = "important"
                r["why"] = (f"已读 {r['read']}/{r['count']} 封，但内容" + ("不好归类" if kind == "other" else f"是{triage.SENDER_KINDS[kind]}")
                            + "，建议保留。")
        missed = sum(known.get(r["key"]) is None for r in todo)
        if missed and not note:                   # the model answered, but not about these
            note = f"有 {missed} 个发件人未能分析，它们的建议仅依据收信和阅读情况。"
        return note

    # ---- the workbench ----------------------------------------------------------------------
    def draft(self, src: str, item_id: int) -> dict:
        acct = self.account(src)
        acts = self.actions(src, write=False)
        text = acts.generate_draft(item_id, self.link("user-waiting"), memory.about(self.memory_for(acct)),
                                   self.cfg["profile"].get("signature") or "")
        return {"text": text, "note": self.draft_note(self.store.get(item_id), text)}

    def draft_note(self, it: dict, text: str) -> str | None:
        """Contact details the draft has and the mail never had (the user's own address and signature are theirs)."""
        return triage.draft_note(it, text, self.address(it["account"]), self.cfg["profile"].get("signature") or "")

    def public_item(self, it: dict) -> dict:
        src = (it.get("suggested_by") or "").split(":")[0]
        out = {k: it.get(k) for k in ("id", "subject", "from_name", "from_addr", "date", "body", "attachments", "action",
                                      "priority", "category", "summary", "reason", "due_date", "event", "reply_points",
                                      "suspicious", "error", "flagged", "draft_text", "state", "done_action", "group_key",
                                      "list_id")}
        out["source"] = src or None
        out["suggested_by"] = SUGGESTION_LABEL.get(src)
        today = samples.TODAY if it["account"] == DEMO else dt.date.today()
        out["past_event"] = self._past_event(it, today)          # an appointment already over
        out["in_calendar"] = None if out["past_event"] else self._in_calendar(it)
        out["events"] = self._events_view(it, today)
        out["overdue"] = bool(it.get("due_date") and it["due_date"][:10] < today.isoformat() and it.get("state") == "pending")
        out["priority"] = triage.priority_now(it, today.isoformat())     # past its date: not high any more (#80)
        # following the user's junk rule: carried out as 移到垃圾邮件, without calling it a scam
        rid = (it.get("suggested_by") or "").split(":")[1] if src == "rule" and ":" in (it.get("suggested_by") or "") else None
        out["junk"] = bool(rid and it.get("action") == "ignore" and any(
            str(r["id"]) == rid and r["action"] == "spam" for r in self.store.rules(it["account"])))
        out["can_unsubscribe"] = bool(it.get("unsub_http") or it.get("unsub_mailto"))
        out["draft_note"] = self.draft_note(it, it["draft_text"]) if it.get("draft_text") else None
        mid = it.get("message_id") or ""
        out["open_url"] = (providers.gmail_message_url(self.address(it["account"]), mid, self.store.gmail_id(it["account"], mid))
                           if it["account"] != DEMO and self.provider(it["account"]) == "gmail" and mid
                           and real_mid(mid) else None)
        return out

    DUE_AHEAD, DUE_BEHIND = 3, 7          # days: what is due soon, or was and still waits, comes first

    def due_soon(self, acct: str) -> list[dict]:
        """Mail that needs the user and has a day to it -- a bill's deadline, a form, a meeting to answer -- within
        the next DUE_AHEAD days, or past it by up to DUE_BEHIND and still waiting: soonest first. A default to start
        from (2026-10-17: coarse first, then closer by what the user does)."""
        today = samples.TODAY if acct == DEMO else dt.date.today()
        lo, hi = (today - dt.timedelta(days=self.DUE_BEHIND)).isoformat(), (today + dt.timedelta(days=self.DUE_AHEAD)).isoformat()
        out = []
        for it in self.store.items(acct, "pending"):
            coming = [e["date"] for e in triage.events_of(it) if e["date"][:10] >= today.isoformat()]     # the next leg
            when = it.get("due_date") or (coming[0] if coming and it.get("action") == "schedule" else None)
            if self._past_event(it, today) or self._in_calendar(it):
                continue                         # an appointment that has been, or is in the calendar already (10-22)
            if when and lo <= when[:10] <= hi and it.get("action") in background.NEEDS_YOU and not it.get("suspicious"):
                out.append({**it, "when": when[:10]})
        return sorted(out, key=lambda it: it["when"])

    @staticmethod
    def _past_event(it: dict, today: dt.date) -> str | None:
        """The date of a 加入日程 mail's event when it is already over (a reminder for yesterday's appointment), else
        None: such mail is shown as over, archived rather than put in the calendar."""
        if it.get("action") != "schedule":
            return None
        days = [e["date"][:10] for e in triage.events_of(it)] or [(it.get("due_date") or "")[:10]]
        day = max(days)                          # a return trip is not over until its last leg
        return day if day and day < today.isoformat() else None

    def _in_calendar(self, it: dict) -> dict | None:
        """The appointment a 加入日程 mail is about, when the user already put the same one in the calendar from
        another mail (same day, same start, same sender's domain): a reminder, or the booking confirmed twice.
        With several appointments: only when every one of them is there."""
        events = triage.events_of(it)
        if it.get("action") != "schedule" or not events:
            return None
        done = [self._added(it, ev) for ev in events]
        return done[0] if all(x and x.get("message_id") != it.get("message_id") for x in done) else None

    def _added(self, it: dict, ev: dict) -> dict | None:
        domain = (it.get("from_domain") or "").lower()
        for x in self.store.cache_get(it["account"], "calendar_added")[0] or []:
            if x.get("date") == ev.get("date") and (x.get("start") or "") == (ev.get("start") or "") and x.get("domain") == domain and domain:
                return {"date": x["date"], "start": x.get("start"), "message_id": x.get("message_id")}
        return None

    def _events_view(self, it: dict, today: dt.date) -> list[dict]:
        """A mail's appointments for its card: each with its own over / already-added state."""
        return [{**ev, "index": i, "past": ev["date"][:10] < today.isoformat(), "added": bool(self._added(it, ev))}
                for i, ev in enumerate(triage.events_of(it))]

    def list_items(self, source: str) -> dict:
        acct = self.account(source)
        due = self.due_soon(acct)
        where = self._thread_of(acct)
        pending = [{**self.public_item(i), "thread": where.get(i["id"])} for i in self.store.items(acct, "pending")]
        archived = [self.public_item(i) for i in self.store.items(acct, "pending", place="archive") if i["action"]]
        groups: dict[str, list[dict]] = {}
        for it in pending:
            if it["group_key"] and it["action"] and not it["suspicious"]:
                groups.setdefault(f"{it['group_key']}|{it['action']}", []).append(it)
        entries, seen = [], set()
        due_ids = {it["id"] for it in due}
        for it in pending:
            if it["id"] in due_ids:
                continue                                  # listed above, under 快到期
            gk = f"{it['group_key']}|{it['action']}" if it["group_key"] else None
            if gk and len(groups.get(gk, [])) > 1:
                if gk not in seen:
                    seen.add(gk)
                    members = groups[gk]
                    entries.append({"type": "group", "key": gk, "items": members, "count": len(members),
                                    "name": members[0]["from_name"] or members[0]["from_addr"]})
                continue
            entries.append({"type": "item", "item": it})
        counts = {a: 0 for a in triage.ACTIONS}
        counts["suspicious"] = 0
        counts["unanalyzed"] = 0
        for it in pending:
            if it["action"]:
                counts[it["action"]] += 1
            else:
                counts["unanalyzed"] += 1
            counts["suspicious"] += int(it["suspicious"])
        old = self.old_summary(acct)
        return {"account": acct, "entries": entries, "counts": counts, "pending": len(pending),
                "write": self.cfg["mode"]["write"] or acct == DEMO, "old": old,
                "archived": archived,                # archived by the user, but something in it is for them
                "auto": self._auto_done(acct),       # what confirmed habits did lately, to see and undo
                "due": [{**self.public_item(it), "when": it["when"], "thread": where.get(it["id"])} for it in due],
                "waiting": self.waiting_on_others(acct) if self.cfg["threads"]["on"] else []}

    def save_threads(self, body: dict) -> dict:
        """The setting: look at whose turn it is in conversations (reads the sent folder). On by default."""
        self.cfg["threads"]["on"] = bool(body.get("on"))
        config.save(self.cfg)
        return {"on": self.cfg["threads"]["on"]}

    def notify_view(self) -> dict | None:
        """For the settings: whether macOS lets the app's notifications through (asked at most once a minute)."""
        at, last = getattr(self, "_notify_seen", (0.0, None))
        if time.monotonic() - at < 60 and at:
            return last
        status = self.desktop.notify_status() if hasattr(self.desktop, "notify_status") else None
        self._notify_seen = (time.monotonic(), status)
        return status

    def test_notification(self, body: dict) -> dict:
        """设置里「发送测试通知」: the user sees whether notifications reach them (and macOS asks for leave if it
        never has); what came back is said, and the last few are listed with how each went out."""
        how = self.desktop.notify(background.TITLE, "测试通知", "看到这条，说明螺壳邮件助手的提醒能送到你面前。")
        recent = self.desktop.recent_notifications() if hasattr(self.desktop, "recent_notifications") else []
        last = recent[-1] if recent else {"how": how, "result": ""}
        self._notify_seen = (0.0, None)                    # asked again: the test may have brought macOS's prompt
        return {"how": last.get("how"), "result": last.get("result"), "recent": recent[-10:], "status": self.notify_view()}

    def notification_settings(self, body: dict) -> dict:
        """「打开通知设置」: System Settings at the app's notification page (macOS asks only once; after a no, only
        the user can allow them). Asked again next time the settings are drawn."""
        opened = self.desktop.open_notification_settings() if hasattr(self.desktop, "open_notification_settings") else False
        self._notify_seen = (0.0, None)
        return {"opened": opened}

    def save_flow(self, body: dict) -> dict:
        """The setting: mark sorted mail read (ADR 0016). On by default."""
        self.cfg.setdefault("flow", {})["mark_read"] = bool(body.get("mark_read"))
        config.save(self.cfg)
        return {"mark_read": self.cfg["flow"]["mark_read"]}

    def _auto_done(self, acct: str) -> list[dict]:
        since = (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        return [{"log_id": e["id"], "message": e["detail"], "at": e["ts"][:16]} for e in self.store.log_entries(acct, limit=50)
                if e["action"] == "auto" and not e["undone"] and (e["ts"] or "") >= since]

    # ---- corrections and rules (all undoable) -----------------------------------------------
    def apply_rules(self, acct: str) -> list[dict]:
        """Re-apply rules to pending items that weren't hand-edited; returns the snapshot of what changed."""
        changed = []
        for it in self.store.items(acct, "pending"):
            if (it.get("suggested_by") or "") == "user":
                continue
            rule = self.store.match_rule(it)
            # already by this rule, or the model saw this rule and deliberately differed: leave it
            if rule and not (it.get("suggested_by") or "").endswith(f":{rule['id']}"):
                changed += self.store.snapshot([it["id"]])
                self.store.set_suggestion(it["id"], pipeline.rule_suggestion(it, rule), f"rule:{rule['id']}")
        return changed

    def _add_rule(self, acct: str, kind: str, value: str, action: str) -> tuple[int, list[dict], dict | None]:
        """Add (or change) a rule of this inbox and re-apply rules to its pending mail.
        Returns (rule id, snapshot of changed suggestions, the rule it replaced if any)."""
        prev = next((r for r in self.store.rules(acct) if r["kind"] == kind and r["value"] == value.strip().lower()), None)
        rid = self.store.add_rule(acct, kind, value, action, triage.RULE_ACTIONS[action])
        return rid, self.apply_rules(acct), prev

    @staticmethod
    def _rule_undo(rid: int, prev: dict | None) -> list[dict]:
        return [{"kind": "restore_rule", "rule": prev}] if prev else [{"kind": "delete_rule", "id": rid}]

    def _gmail_note(self, rule: dict) -> str:
        """Deleting a rule here leaves the Gmail filter made from it; say so."""
        return "。已导入 Gmail 的过滤器仍然有效，如需停用请在 Gmail 中删除" if rule.get("exported_at") else ""

    def override(self, body: dict) -> dict:
        acct = self.account(body.get("source"))
        action = body.get("action")
        if action not in triage.ACTIONS:
            raise ValueError("未知的动作。")
        items = [it for it in (self.store.get(int(i)) for i in body.get("ids") or []) if it and it["account"] == acct]
        if not items:
            raise ValueError("请选择邮件。")
        snap = self.store.snapshot([it["id"] for it in items])
        for it in items:
            s = {"action": action, "priority": it.get("priority"), "category": it.get("category"), "summary": it.get("summary"),
                 "reason": "已手动改为「" + triage.ACTIONS[action] + "」", "due_date": it.get("due_date"),
                 "event": it.get("event"), "reply_points": it.get("reply_points"), "suspicious": False}
            self.store.set_suggestion(it["id"], s, "user")
        undo = [{"kind": "restore_suggestions", "items": snap}]
        rule, msg = None, f"已改为「{triage.ACTIONS[action]}」"
        kind = body.get("remember")
        if kind in RULE_KINDS:
            first = items[0]
            value = {"sender": first.get("from_addr"), "domain": first.get("from_domain"), "list": first.get("list_id")}[kind]
            if not value:
                raise ValueError("无法根据这封邮件创建规则。")
            rid, more, prev = self._add_rule(acct, kind, value, action)
            undo = [{"kind": "restore_suggestions", "items": snap + more}] + self._rule_undo(rid, prev)
            rule = {"id": rid, "kind": kind, "value": value}
            msg += f"，并创建规则：{RULE_KINDS[kind]}「{value}」"
        log_id = self.store.log(acct, "rule" if rule else "override", [it["id"] for it in items], True, msg, undo)
        if acct != DEMO:
            mem = self.memory_for(acct)
            for it in items:
                if it.get("action") and it["action"] != action:
                    memory.happened(mem, acct, "mail-override",
                                    f"把「{it.get('subject') or '（无主题）'}」从「{triage.ACTIONS.get(it['action'], it['action'])}」"
                                    f"改为「{triage.ACTIONS[action]}」", sender=subscriptions.sender_key(it),
                                    ref=it.get("message_id"), data={"from": self.AS_HANDLING.get(it["action"], it["action"]),
                                                                    "to": self.AS_HANDLING.get(action, action),
                                                                    "suggestion": {"from": it["action"], "to": action},
                                                                    "name": it.get("from_name") or it.get("from_addr"),
                                                                    "category": it.get("category")})
        return {"ok": True, "rule": rule, "message": msg, "log_id": log_id}

    OPS_WORDS = {"archive": "归档", "trash": "删除", "spam": "移到垃圾邮件", "read": "标为已读"}
    # a suggestion in the memory's words for what is done with mail (luoke-mem ACTION_NAMES): what the sorting calls
    # "ignore" is moving it to the trash; reply, schedule and follow up keep it in the inbox
    AS_HANDLING = {"archive": "archive", "ignore": "trash", "reply": "keep", "schedule": "keep", "follow_up": "keep"}

    def items_act(self, source, ids: list[int], op: str) -> dict:
        """Carry out what the user clicked on the list; what went is recorded for the memory, per sender."""
        acct = self.account(source)
        items = [it for it in (self.store.get(i) for i in ids) if it]
        r = self.actions(source, write=op != "skip", irreversible=op in self.IRREVERSIBLE and len(ids) > 1).execute(ids, op)
        if op in self.OPS_WORDS and acct != DEMO:
            done = set(r.get("done") or [])
            self._acted(acct, [it for it in items if it["id"] in done], op)
        return r

    # a sign-in, security or password notice is never routine, whatever habit its sender falls under (review
    # of what reached the memory, 10-22: an auto-archive habit for a mail service's notices would hide them)
    SECURITY_WORDS = re.compile(r"登录|登入|安全|密码|验证|验证码|异常|新设备|sign[- ]?in|log[- ]?in|security|password|verif|"
                                r"suspicious|unusual|new device|2fa|two[- ]factor|ログイン|セキュリティ|パスワード|認証", re.I)

    def _not_routine(self, it: dict) -> bool:
        """Left for the user even when a confirmed habit covers it: high priority, or a sign-in / security notice."""
        return it.get("priority") == "high" or bool(self.SECURITY_WORDS.search(it.get("subject") or ""))

    def _acted(self, acct: str, items: list[dict], op: str, where: str = "here", label: str | None = None) -> int:
        """What the user handled themselves, for the memory to learn habits from (ADR 0012 amended): one record per
        sender and kind of mail (the triage category) -- a habit is one sender's one kind (2026-10-19).
        `where`: "here" (clicked on this page) or "elsewhere" (Gmail's own page, the phone: seen at sync)."""
        per: dict[tuple, list[dict]] = {}
        for it in items:
            per.setdefault((subscriptions.sender_key(it), it.get("category")), []).append(it)
        mem = self.memory_for(acct)
        for (key, category), its in per.items():
            name = its[0].get("from_name") or its[0].get("from_addr") or key
            kind = f"{triage.CATEGORIES[category]}类" if category in triage.CATEGORIES else ""
            words = f"加上标签「{label}」" if op == "label" else self.OPS_WORDS[op]
            # changed and handled in one step (「改为…」 with 现在就…): the mail-override written a moment ago is the same
            # decision -- said so, for the memory not to count it twice (review, 10-22)
            same = sum(1 for x in its if (x.get("suggested_by") or "") == "user" and self.AS_HANDLING.get(x.get("action"), x.get("action")) == op)
            memory.happened(mem, acct, "mail-acted", f"把「{name}」的 {len(its)} 封{kind}邮件{words}", sender=key,
                            data={"action": op, "count": len(its), "name": name, "category": category, "where": where,
                                  **({"after_override": same} if same else {}),
                                  **({"label": label} if label else {}),
                                  "subjects": [memory.mask(m) for m in (" ".join((x.get("subject") or "").split())[:40]
                                                                        for x in its[:3]) if m]})
        return len(per)

    def add_rule(self, body: dict) -> dict:
        acct = self.account(body.get("source"))
        kind, value, action = body.get("kind"), (body.get("value") or "").strip(), body.get("action")
        if kind not in RULE_KINDS or not value or action not in triage.RULE_ACTIONS:
            raise ValueError("规则不完整：请选择类型、填写内容并选择处理方式。")
        rid, snap, prev = self._add_rule(acct, kind, value, action)
        msg = f"已添加规则：{RULE_KINDS[kind]}「{value}」→ {triage.RULE_ACTIONS[action]}" + (f"（{len(snap)} 封待处理邮件已更新）" if snap else "")
        undo = [{"kind": "restore_suggestions", "items": snap}] + self._rule_undo(rid, prev)
        log_id = self.store.log(acct, "rule", [], True, msg, undo)
        return {"ok": True, "id": rid, "applied": len(snap), "message": msg, "log_id": log_id}

    def _own_rule(self, acct: str, rule_id) -> dict:
        rule = self.store.rule(int(rule_id))
        if not rule or rule["account"] != acct:
            raise ValueError("规则不存在。")
        return rule

    def delete_rule(self, body: dict) -> dict:
        acct = self.account(body.get("source"))
        rule = self._own_rule(acct, body.get("id"))
        self.store.delete_rule(rule["id"])
        msg = (f"已删除规则：{RULE_KINDS[rule['kind']]}「{rule['value']}」→ {triage.RULE_ACTIONS.get(rule['action'], rule['action'])}"
               f"{self._gmail_note(rule)}")
        log_id = self.store.log(acct, "rule", [], True, msg, [{"kind": "restore_rule", "rule": rule}])
        return {"ok": True, "message": msg, "log_id": log_id}

    def rules_view(self, source: str | None) -> dict:
        acct = self.account(source)
        return {"rules": self.store.rules(acct), "gmail": self.provider(acct) == "gmail", "demo": acct == DEMO}

    # ---- before a rule becomes a Gmail filter: what it would catch (2026-10-17) -----------------------
    PREVIEW_DAYS, PREVIEW_JUDGE = 365, 75
    # what a filter must not sweep along: archiving hides what is still to do or an account's security notice;
    # deleting is wrong for anything that is not spent
    AT_RISK = {"archive": ("todo", "account"), "follow_up": ()}

    def start_filter_preview(self, body: dict) -> dict:
        """A rule about to be a Gmail filter, tried on the past year: how many messages each way of matching would
        catch -- this sender, the whole domain, the mailing list -- and, from a look at their subjects (the 清理
        judgement, the same task), how many of them it should not touch, with examples; and which way is best.
        Read-only. A job: the mailbox is read and the model asked."""
        acct = self.account(body.get("source"))
        rule = next((r for r in self.store.rules(acct) if r["id"] == int(body.get("id") or 0)), None)
        if not rule:
            raise ValueError("规则不存在。")

        def work(job):
            job["stage"] = "正在查找过去一年里会命中的邮件"
            if acct == DEMO:
                headers = samples.demo_headers() + samples.demo_archive()
            else:
                with self.open_mailbox(acct) as mb:
                    headers = mb.everywhere_headers(days=self.PREVIEW_DAYS, trash=False)
            return self._filter_preview(acct, rule, headers, job)
        return self._start_job("filter_preview", work, account=acct, rule=rule["id"])

    def _filter_preview(self, acct: str, rule: dict, headers: list[dict], job: dict) -> dict:
        def hit(kind: str, value: str):
            value = (value or "").lower()
            if kind == "list":
                return lambda h: (h.get("list_id") or "").lower() == value
            if kind == "domain":
                return lambda h: (h.get("from_domain") or "") == value or (h.get("from_domain") or "").endswith("." + value)
            return lambda h: (h.get("from_addr") or "").lower() == value
        own = [h for h in headers if hit(rule["kind"], rule["value"])(h)]
        ways = [(rule["kind"], rule["value"])]
        domains = sorted({h.get("from_domain") for h in own if h.get("from_domain")})
        lists = sorted({h.get("list_id") for h in own if h.get("list_id")})
        if rule["kind"] != "domain" and len(domains) == 1:
            ways.append(("domain", domains[0]))
        if rule["kind"] != "list" and len(lists) == 1:
            ways.append(("list", lists[0]))
        caught = {w: [h for h in headers if hit(*w)(h)] for w in ways}
        # one look at the subjects of everything any way would catch (newest first, a sample when there are many)
        union = {(h.get("message_id") or id(h)): h for w in ways for h in caught[w]}
        sample = sorted(union.values(), key=lambda h: h.get("date") or "", reverse=True)[:self.PREVIEW_JUDGE]
        kinds: dict = {}
        link = self.link(self.watching, battery=True, patient=lambda: job["cancel"], job=job) if self.has_model() else None
        if link and link.ready("cleanup"):
            i = 0
            while i < len(sample) and not job["cancel"]:
                job.update(stage="正在检查会不会误伤", done=i, total=len(sample))
                try:
                    found, count = triage.classify_old(sample[i:], link, None, "archive")
                except (engines.Busy, engines.EngineError):
                    break
                for n, k in found.items():
                    kinds[id(sample[i + n - 1])] = k
                i += count
        risky_kinds = self.AT_RISK.get(rule["action"]) if rule["action"] in self.AT_RISK else \
            tuple(k for k in triage.OLD_KINDS if k not in triage.DELETE_KINDS)
        judged = set(kinds)
        out = []
        for kind, value in ways:
            hs = caught[(kind, value)]
            bad = [h for h in hs if kinds.get(id(h)) in risky_kinds]
            out.append({"kind": kind, "value": value, "count": len(hs), "judged": sum(1 for h in hs if id(h) in judged),
                        "risky": len(bad), "examples": [h.get("subject") or "（无主题）" for h in bad[:3]],
                        "label": RULE_KINDS[kind]})
        return {"rule": {k: rule[k] for k in ("id", "kind", "value", "action")}, "ways": out, "checked": bool(judged),
                "advice": self._filter_advice(out)}

    @staticmethod
    def _filter_advice(ways: list[dict]) -> str:
        first = ways[0]
        if first["risky"]:
            return f"过去一年会命中 {first['count']} 封，其中 {first['risky']} 封看起来不该一起处理（例如「{first['examples'][0]}」），建议先不导出，或改成更窄的条件。"
        wider = [w for w in ways[1:] if w["count"] > first["count"]]
        safe = [w for w in wider if not w["risky"]]
        if safe:
            w = max(safe, key=lambda w: w["count"])
            return f"按{w['label']}「{w['value']}」会多命中 {w['count'] - first['count']} 封，没有发现不该处理的，可以放宽。"
        if wider:
            w = wider[0]
            return f"按{w['label']}「{w['value']}」会多命中 {w['count'] - first['count']} 封，其中 {w['risky']} 封不该一起处理，不建议放宽。"
        return f"过去一年会命中 {first['count']} 封，没有发现不该处理的。"

    def export_gmail_filters(self, body: dict) -> dict:
        """Build mailFilters.xml from this inbox's rules (all, only new ones, or the given ids)."""
        acct = self.account(body.get("source"))
        if self.provider(acct) != "gmail":
            raise ValueError("仅 Gmail 支持导入过滤器文件。")
        rules = self.store.rules(acct)
        if body.get("ids"):
            wanted = {int(i) for i in body["ids"]}
            rules = [r for r in rules if r["id"] in wanted]
        elif not body.get("include_exported"):
            rules = [r for r in rules if not r.get("exported_at")]
        ok, skipped = gmailfilters.split(rules)
        if not ok:
            raise ValueError("没有可导出的规则。" + ("「回复」「加入日程」类规则无法转为过滤器。" if skipped else ""))
        at = time.time()
        xml = gmailfilters.build_xml(ok, samples.ME if acct == DEMO else acct, at)
        self.store.mark_exported([r["id"] for r in ok])
        msg = f"已导出 {len(ok)} 条规则" + ("（示例）" if acct == DEMO else "")
        self.store.log(acct, "export", [], True, msg)
        return {"ok": True, "filename": gmailfilters.filename(at), "xml": xml, "count": len(ok),
                "skipped": [{"text": gmailfilters.describe(x["rule"]), "reason": x["reason"]} for x in skipped],
                "message": msg, "settings_url": None if acct == DEMO else providers.gmail_url(self.address(acct), "settings/filters")}

    # ---- subscription health -----------------------------------------------------------------
    def subscriptions_view(self, acct: str) -> dict:
        result, _ = self.store.cache_get(acct, "subscriptions")
        if not result:
            return {"senders": None}
        decisions = self.store.senders(acct)
        rules = {(r["kind"], r["value"]): r for r in self.store.rules(acct)}
        name = self.provider(acct)
        prov = providers.PROVIDERS.get(name, providers.PROVIDERS["custom"])
        result["provider"] = {"purge_days": prov["purge_days"], "block": prov["block"], "demo": acct == DEMO,
                              "gmail_filters": name == "gmail"}
        for row in result["senders"]:
            d = decisions.get(row["key"])
            rule = rules.get(("list", row["list_id"])) if row.get("list_id") else rules.get(("sender", (row.get("from_addr") or "").lower()))
            row["decision"] = d
            row["rule"] = {k: rule[k] for k in ("id", "kind", "value", "action", "exported_at")} if rule else None
            # mail that arrived well after the unsubscribe (a couple of days of in-flight mail is normal)
            row["still_sending"] = False
            # only a scan made after the unsubscribe can show new mail; demo dates are fictional
            if acct != DEMO and d and d["status"] in ("unsubscribed", "unsubscribe_opened") and row.get("last_date") \
                    and result.get("at", "") > d["at"][:16]:
                try:
                    last = dt.datetime.strptime(row["last_date"][:16], "%Y-%m-%d %H:%M")
                    row["still_sending"] = last > dt.datetime.strptime(d["at"][:16], "%Y-%m-%d %H:%M") + dt.timedelta(days=2)
                except ValueError:
                    pass
        return result

    def _me(self, acct: str) -> set[str]:
        """The user's own addresses in this mailbox: their mail is never one sender's history to clear."""
        return {acct.lower(), self.address(acct).lower()} if acct != DEMO else set()

    def sender_act(self, body: dict) -> dict:
        source, op = body.get("source"), body.get("op")
        acct = self.account(source)
        result, _ = self.store.cache_get(acct, "subscriptions")
        sub = next((r for r in (result or {}).get("senders", []) if r["key"] == body.get("key")), None)
        if not sub:
            raise ValueError("请先进行订阅体检。")
        label = sub.get("name") or sub.get("from_addr")
        prev = self.store.sender(acct, sub["key"])
        status_undo = {"kind": "sender_status", "key": sub["key"], "previous": prev}
        if op == "unsubscribe":
            r = self.actions(source).unsubscribe(sub)
        elif op == "archive_all":
            r = self.actions(source).archive_sender(sub)
        elif op == "history_preview":
            p = self.actions(source, write=False).history_preview(sub, self.curator.kept_mids(acct), self._me(acct))
            prov = self.subscriptions_view(acct)["provider"]
            return {**p, "purge_days": prov["purge_days"], "subscriptions": None}
        elif op == "trash_history":
            r = self.actions(source, irreversible=True).trash_history(sub, self.subscriptions_view(acct)["provider"]["purge_days"],
                                                   before=lambda headers: self.learner.before_clean(acct, headers),
                                                   keep=self.curator.kept_mids(acct), me=self._me(acct))
            self.learner.cleared(acct, [{"key": sub["key"], "name": label or sub["key"],
                                         "addr": sub.get("from_addr"), "category": "订阅", "count": r.get("count") or 0,
                                         "by": "user"}])
        elif op in ("archive_rule", "trash_rule", "spam_rule", "junk_rule"):
            op = "spam_rule" if op == "junk_rule" else op
            action = {"archive_rule": "archive", "trash_rule": "ignore", "spam_rule": "spam"}[op]
            kind, value = ("list", sub["list_id"]) if sub.get("list_id") else ("sender", sub["from_addr"])
            rid, snap, prev_rule = self._add_rule(acct, kind, value, action)
            self.store.set_sender(acct, sub["key"], op, label, str(rid))
            msg = (f"已创建规则：「{label}」的邮件{triage.RULE_ACTIONS[action]}"
                   + (f"（{len(snap)} 封待处理邮件已更新）" if snap else ""))
            undo = [{"kind": "restore_suggestions", "items": snap}, status_undo] + self._rule_undo(rid, prev_rule)
            r = {"ok": True, "message": msg, "log_id": self.store.log(acct, "rule", [], True, msg, undo)}
        elif op == "digest":
            self.store.digest_add_source(acct, sub["key"], label, "subscriptions")
            self.store.set_sender(acct, sub["key"], "digest", label, None)
            msg = f"已将「{label}」加入精选来源，之后的邮件会汇总到每周精选"
            r = {"ok": True, "message": msg, "log_id": self.store.log(acct, "sender", [], True, msg,
                                                                      [status_undo, {"kind": "digest_source", "key": sub["key"]}])}
        elif op in ("keep", "mark_unsubscribed", "reset"):
            status = {"keep": "keep", "mark_unsubscribed": "unsubscribed", "reset": None}[op]
            undo = [status_undo]
            msg = {"keep": f"已保留「{label}」", "mark_unsubscribed": f"已将「{label}」标记为已退订",
                   "reset": f"已将「{label}」移回建议处理"}[op]
            if op == "reset" and prev and prev["status"] in ("archive_rule", "trash_rule", "spam_rule", "junk_rule") and sub.get("rule"):
                rule = self.store.rule(sub["rule"]["id"])
                if rule and rule["account"] == acct:
                    self.store.delete_rule(rule["id"])
                    undo.append({"kind": "restore_rule", "rule": rule})
                    msg += "，并删除了对应的规则" + self._gmail_note(rule)
            if op == "reset" and prev and prev["status"] == "digest":
                self.store.digest_remove_source(acct, sub["key"])
                msg += "，并移出了精选来源"
            self.store.set_sender(acct, sub["key"], status, label, "manual" if status else None)
            r = {"ok": True, "message": msg, "log_id": self.store.log(acct, "sender", [], True, msg, undo)}
        else:
            raise ValueError("不支持的操作。")
        r["subscriptions"] = self.subscriptions_view(acct)
        return r

    def _stop_everything(self, wait: float = 60):
        """Before 全部清除: every job here is asked to stop, and waited for (a sync would go on writing mail into the
        new, empty store); 螺壳主控's idle job is waited out by taking its lock, held until the data is gone."""
        for j in self.jobs.values():
            if j["status"] == "running":
                j["cancel"] = True
        end = time.monotonic() + wait
        while any(j["status"] == "running" for j in self.jobs.values()) and time.monotonic() < end:
            time.sleep(0.2)
        idle = desktop.InstanceLock(config.CONFIG_DIR, "idle.lock")
        return idle if idle.acquire(wait=max(1.0, end - time.monotonic())) else None

    def forget(self, body: dict | None = None) -> dict:
        """Everything luoke-mail keeps on this computer. The memory is the user's, not luoke-mail's (top-level
        ADR 0002): what the mail assistant wrote there is forgotten only when the user ticks it (`memory`)."""
        exiting = self._remove_login_item("forget")
        idle = self._stop_everything()
        e = self.cfg["engine"]
        for m in self.cfg["accounts"]:
            self.secrets.delete(MAIL_SECRET, m["address"])
        self._secret_known.clear()
        if e.get("base_url"):
            self.secrets.delete(ENGINE_SECRET, e["base_url"])
        self.store.wipe()
        for name in ("account-salt", "service.log"):
            try:
                os.remove(os.path.join(config.CONFIG_DIR, name))
            except OSError:
                pass
        memory._salts.clear()
        if idle:
            idle.release()
        if (body or {}).get("memory"):
            self.mem.forget_tool(memory.TOOL)        # what luoke-mail put in the shared memory
        self.reset_demo_memory()                     # the demo's memory: luoke-mail's own sample, all of it
        config.remove_legacy()
        self.cfg = json.loads(json.dumps(config.DEFAULTS))
        config.save(self.cfg)
        return {"ok": True, "exiting": exiting}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "luoke-mail"

        def log_message(self, fmt, *args):  # keep the terminal quiet; errors still print
            pass

        def _send(self, status: int, payload, ctype="application/json; charset=utf-8"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                 "connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return host in {f"127.0.0.1:{app.port}", f"localhost:{app.port}"}

        def _authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Luoke-Token") or "", app.token)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ValueError("请求过大。")
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")

        def _route(self, fn):
            try:
                return self._send(200, fn())
            except (MailError, ValueError, ActionError, engines.EngineError, DesktopError) as ex:
                return self._send(400, {"error": str(ex)})
            except (BrokenPipeError, ConnectionResetError) as ex:
                # the page was closed or reloaded before the answer: one line, no traceback (luoke-mail#24)
                print(f"[luoke-mail] the page left before the answer: {type(ex).__name__}", file=sys.stderr)
            except Exception as ex:  # noqa: BLE001
                desktop.log_crash(ex, "a page request")
                return self._send(500, {"error": f"{type(ex).__name__}: {ex}"})

        def do_GET(self):
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, {"error": "bad host"})
            url = urllib.parse.urlparse(self.path)
            path, q = url.path, dict(urllib.parse.parse_qsl(url.query))
            if not path.startswith("/api/"):
                return self._static(path)
            if not self._authorized():
                return self._send(HTTPStatus.UNAUTHORIZED, {"error": "无法验证访问权限。请从「螺壳邮件助手」App 重新打开页面。"})
            src = q.get("source")
            if path.startswith("/api/jobs/"):
                job = app.jobs.get(path.rsplit("/", 1)[-1])
                return self._send(200, app.public_job(job)) if job else self._send(404, {"error": "任务不存在。"})
            routes = get_routes(app, q)
            fn = routes.get(path)
            return self._route(fn) if fn else self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, {"error": "bad host"})
            if not self._authorized():
                return self._send(HTTPStatus.UNAUTHORIZED, {"error": "无法验证访问权限。请从「螺壳邮件助手」App 重新打开页面。"})
            path = urllib.parse.urlparse(self.path).path
            try:
                b = self._body()
            except ValueError as ex:
                return self._send(400, {"error": str(ex)})
            routes = post_routes(app, b)
            fn = routes.get(path)
            return self._route(fn) if fn else self._send(404, {"error": "not found"})

        def _static(self, path: str):
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC_DIR, name))
            if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as fh:
                data = fh.read()
            return self._send(200, data, CONTENT_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

    return Handler


def get_routes(app: "App", q: dict) -> dict:
    """The page's GET /api routes, query `q` (the window's pages read the same views: agent.py)."""
    src = q.get("source")
    return {
        "/api/state": app.state,
        "/api/ping": app.ping,
        "/api/activity": lambda: app.activity(q.get("seen") == "1"),
        "/api/items": lambda: app.list_items(src),
        "/api/review": lambda: app.review.view(src, q.get("day")),
        "/api/log": lambda: {"entries": app.store.log_entries(app.account(src))},
        "/api/rules": lambda: app.rules_view(src),
        "/api/subscriptions": lambda: app.subscriptions_view(app.account(src)),
        "/api/old": lambda: app.old_view(app.account(src), q.get("scope") if q.get("scope") in App.SCOPES else "inbox"),
        "/api/digest": lambda: app.curator.view(app.account(src)),
        "/api/memory": lambda: app.curator.memory_view(src),
    }


def post_routes(app: "App", b: dict) -> dict:
    """The page's POST /api routes, body `b`: one table for the browser page and the window's pages (window.py),
    so both do the same thing the same way."""
    src = b.get("source")
    return {
        "/api/mail": lambda: app.save_mail(b),
        "/api/mail/test": lambda: app.test_mail(b),
        "/api/mail/remove": lambda: app.remove_mail(b),
        "/api/engines/scan": lambda: app.scan_engines(b),
        "/api/engine": lambda: app.save_engine(b),
        "/api/engine/test": app.test_engine,
        "/api/profile": lambda: app.save_profile(b),
        "/api/mode": lambda: app.save_mode(b),
        "/api/power": lambda: app.save_power(b),
        "/api/threads": lambda: app.save_threads(b),
        "/api/flow": lambda: app.save_flow(b),
        "/api/auto/test-notify": lambda: app.test_notification(b),
        "/api/auto/notify-settings": lambda: app.notification_settings(b),
        "/api/auto": lambda: app.set_auto(b),
        "/api/sync": lambda: app.start_sync(b),
        "/api/subscriptions": lambda: app.start_subscriptions(b),
        "/api/old/scan": lambda: app.start_old_scan(b),
        "/api/old/judge": lambda: app.set_judging(b),
        "/api/old/apply": lambda: app.start_old_apply(b),
        "/api/digest/read": lambda: app.curator.read_from_clean(b),
        "/api/digest/reading": lambda: app.curator.set_reading(b),
        "/api/digest/feedback": lambda: app.curator.feedback(b),
        "/api/digest/open": lambda: app.curator.opened(b),
        "/api/digest/archive": lambda: app.curator.archive(b),
        "/api/digest/source": lambda: app.curator.set_source(b),
        "/api/memory": lambda: app.curator.memory_change(b),
        "/api/memory/site": lambda: app.profiler.change_site(b),
        "/api/memory/learn": lambda: app.learner.set_on(b),
        "/api/learn/intro-seen": lambda: app.learner.intro_seen(),
        "/api/memory/whole": lambda: app.curator.open_whole(b),
        "/api/jobs/cancel": lambda: app.jobs.get(b.get("id"), {}).update(cancel=True) or {"ok": True},
        # 跳过 / 知道了 only takes it off the list: no processing mode needed
        "/api/items/act": lambda: app.items_act(src, [int(i) for i in b.get("ids") or []], b.get("op")),
        "/api/items/override": lambda: app.override(b),
        "/api/items/draft": lambda: app.draft(src, int(b["id"])),
        "/api/items/draft/save": lambda: app.actions(src).save_draft(int(b["id"]), b.get("text") or "",
                                                                      app.address(app.account(src)),
                                                                      app.cfg["profile"].get("signature") or ""),
        "/api/items/calendar": lambda: app.actions(src, write=False).calendar(int(b["id"]), b.get("index")),
        "/api/undo": lambda: app.undo(src, int(b["log_id"])),
        "/api/review/act": lambda: app.review.act(b),
        "/api/mail/own": lambda: app.set_own(b),
        "/api/review/seen": lambda: app.review.seen(b),
        "/api/start-page/clear": lambda: app.store.cache_set("", "open_page", None) or {"ok": True},
        "/api/senders/act": lambda: app.sender_act(b),
        "/api/rules": lambda: app.add_rule(b),
        "/api/rules/delete": lambda: app.delete_rule(b),
        "/api/rules/filter-preview": lambda: app.start_filter_preview(b),
        "/api/rules/gmail-filters": lambda: app.export_gmail_filters(b),
        "/api/forget": lambda: app.forget(b),
    }


class PageServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:      # socketserver's prints the exception's text
        desktop.log_crash(sys.exc_info()[1], "a page request")


def _bind(app: App, port: int, wait: float, exact: bool = False) -> ThreadingHTTPServer | None:
    """The service waits for its port (the terminal server it takes over is still letting go of it);
    after that, or from the terminal, the next free port will do -- unless a port was named: that one or none."""
    deadline = time.time() + wait
    while True:
        late = time.time() >= deadline
        for candidate in [port] + (list(range(port + 1, port + 20)) if late and not exact else []):
            try:
                return PageServer(("127.0.0.1", candidate), make_handler(app))
            except OSError:
                continue
        if late:
            return None
        time.sleep(0.5)


def _trim_log(path: str, limit: int = 1_000_000) -> None:
    """The old login item's log (launchd holds it open, so it is emptied in place). In 螺壳邮件助手.app the resident
    service writes it itself and keeps it small (desktop.log_to_file, an older file beside it): left alone here, or
    the line it has just written would go (review, 2026-10-21). The same inside 螺壳.app."""
    if desktop.launcher_path() or desktop.in_luoke():
        return
    try:
        if os.path.getsize(path) > limit:
            open(path, "w").close()
    except OSError:
        pass


IDLE_JOB_BUDGET = 25 * 60       # a little under the jobs' timeout in the manifest (30 minutes)
# 螺壳主控's idle jobs (packaging/accessory.json `jobs`, in this order; it takes turns among the ones due): each runs
# some kinds of idle work, in their turns
IDLE_JOBS = {"clean": ("older", "judge"), "digest": ("read", "edit"), "learn": ("learn", "profile")}


def run_idle_job(app: App | None = None, budget: float = IDLE_JOB_BUDGET, name: str | None = None) -> int:
    """螺壳主控's idle job `idle` (packaging/accessory.json `jobs`; luoke-core V1 §8, ADR 0006): started when the Mac
    is quiet, in a process of its own. The idle work takes its turns as in the app -- judging old mail in 清理,
    reading newsletters and choosing 精选, 推测关注的话题, 了解你 -- for every mailbox, never the demo. It stops at a
    finished step when 主控 says so (SIGTERM, or a request answered busy / paused), and exits 0 -- also when the
    app itself is at it (idle.lock) or there is nothing to do. Nothing of the mail goes to stderr, only reasons."""
    import signal
    app = app or App(service=True)
    app.in_job = True
    kinds = IDLE_JOBS.get(name or "", App.IDLE_KINDS)
    lock = desktop.InstanceLock(config.CONFIG_DIR, "idle.lock")
    if not lock.acquire():
        print("luoke-mail idle job: the app is doing idle work now", file=sys.stderr)
        return 0
    job = {"id": "idle-job", "kind": "idle", "status": "running", "stage": "", "done": 0, "total": 0, "cancel": False}
    try:
        signal.signal(signal.SIGTERM, lambda *_: job.update(cancel=True))
    except ValueError:
        pass                                          # not the main thread (tests)
    import contextlib
    import io
    t0, steps, why = time.monotonic(), 0, ""

    def saved() -> set[str]:
        return {a["address"].lower() for a in config.load()["accounts"]}
    saved_at_start = saved()
    try:
        # 主控 keeps this stderr in its log and the diagnostics: what the steps print (a server's reply, a site's
        # address) stays here; only the summary below goes out
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            while not job["cancel"] and time.monotonic() - t0 < budget:
                step = app.next_idle(kinds)
                if not step:
                    break
                kind, fn, extra = step
                if extra.get("account") in saved_at_start and extra["account"] not in saved():
                    why = "the mailbox was removed"             # removed in the app since this job started
                    break
                job.update(kind=kind, **extra)
                try:
                    fn(job)
                except KeychainLocked:
                    why = "keychain locked"
                    break
                except Exception as ex:  # noqa: BLE001 - the kind of failure, never its text
                    why = f"{kind}: {type(ex).__name__}"
                    break
                steps += 1
                if engines.wait_for():                    # 主控 said not now: stop here, it starts the job again later
                    why = "the hub said not now"
                    break
    finally:
        lock.release()
    print(f"luoke-mail idle job {name or 'all'}: {steps} step(s)" + (", asked to stop" if job["cancel"] else "") + (f"; {why}" if why else ""),
          file=sys.stderr)
    return 0


def forget_secrets(store=None) -> int:
    """For 螺壳主控's 「卸载…」, run before it deletes the app and ~/.luoke-mail: the passwords luoke-mail keeps in the
    keychain -- each mailbox's authorization code, a model service's key -- go too, since only luoke-mail knows
    which are its own (the settings say whose). Returns how many it asked to remove. `python3 -m luoke_mail
    --forget-secrets`."""
    cfg = config.load()
    store = store or default_store()
    n = 0
    for m in cfg.get("accounts") or []:
        if m.get("address"):
            store.delete(MAIL_SECRET, m["address"])
            n += 1
    if (cfg.get("engine") or {}).get("base_url"):
        store.delete(ENGINE_SECRET, cfg["engine"]["base_url"])
        n += 1
    return n


def serve(port: int = 8765, open_browser: bool = True, service: bool = False, exact: bool = False,
          body: desktop.BodyLink | None = None) -> int:
    """`body`: this process is part of 螺壳.app and that is the pipe to the body that started it. There is no one to hand
    over from: another instance on this data folder is said with ALREADY_RUNNING, and the body tries again later."""
    config.private_dir(config.CONFIG_DIR)       # started by 螺壳主控 at login, the folder may not be there yet
    lock = desktop.InstanceLock()
    if body:
        if not lock.acquire():
            print("[luoke-mail] another instance has this data folder", file=sys.stderr, flush=True)
            return ALREADY_RUNNING
        desktop.mark_service_started()
    elif service:
        held = lock.acquire()
        info = None if held else desktop.read_server_info()
        if info and info.get("service") and desktop.running():
            # another background instance already serves (started twice: the app at login and by hand): this
            # one is not needed. Said to 螺壳主控 first, or it would start this one again.
            print("后台自动整理已在运行。", file=sys.stderr, flush=True)
            launcher = desktop.launcher_path()
            if launcher:
                desktop.AppDesktop(launcher, hub_link=lambda: hub.Hub(config.load()["engine"].get("hub_socket"))).exiting()
            return 0
        _trim_log(os.path.join(config.CONFIG_DIR, "service.log"))
        desktop.mark_service_started()
        if not held:
            lock.acquire(wait=None)            # a server started from the terminal may still be handing over
    elif not lock.acquire():
        url = desktop.running()
        if not url:
            print("\n  螺壳邮件助手已在运行，但没有响应。请稍后重试。\n", flush=True)
            return 1
        print(f"\n  螺壳邮件助手已在运行{'，已在浏览器中打开' if open_browser else ''}：{url}\n", flush=True)
        if open_browser:
            desktop.open_url(url)
        return 0
    try:
        app = App(service=service)
    except memory.TooNew:                   # a bundled luoke-mem older than the memory file: say so, don't crash
        lock.release()
        if body:                            # starting it again won't help: 0 is the exit the body leaves alone until 螺壳 next starts
            body.tell({"op": "not-starting"})       # before the dialog: it stays up for as long as the user leaves it
            desktop.alert(memory.TOO_NEW_IN_LUOKE)
            return 0
        print(memory.TOO_NEW, file=sys.stderr, flush=True)
        return TOO_NEW_EXIT
    httpd = _bind(app, port, 30 if service and not body else 0, exact)
    if httpd is None:
        print(f"端口 {port} 已被占用。" if exact else f"端口 {port}–{port + 19} 均被占用，请用 --port 指定其他端口。", file=sys.stderr)
        return 1
    app.port = httpd.server_address[1]
    desktop.write_server_info(app.port, service)
    app.on_exit = lambda: threading.Thread(target=httpd.shutdown, daemon=True).start()
    url = desktop.page_url(app.port, app.token)
    if service:      # the log is a file: no token in it
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] luoke-mail {__version__} 在后台运行：http://127.0.0.1:{app.port}/", flush=True)
    else:
        print(f"\n  螺壳邮件助手 luoke-mail {__version__}\n\n  在浏览器中打开：{url}\n\n  （仅限本机访问，按 Ctrl+C 退出）\n", flush=True)
        if open_browser:
            threading.Timer(0.6, lambda: desktop.open_url(url)).start()
    app.auto.start()
    if agent.enabled():                        # LUOKE_MAIL_AGENT=0 turns it off
        app.agent.start()                      # the socket 螺壳 asks on: only the instance holding the lock
    if body:
        import signal
        signal.signal(signal.SIGTERM, lambda *_: app.on_exit())     # the finally below runs: agent.sock and the lock go cleanly
        body.on_open = lambda: desktop.open_url(url)
        body.tell({"op": "ready"})
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已退出。")
    finally:
        app.auto.stop()
        app.agent.close()
        httpd.server_close()
        lock.release()
    if service and app.exit_reason and isinstance(app.desktop, desktop.AppDesktop):
        app.desktop.exiting()                # stopped on purpose: 螺壳主控 must not start it again
    if app.exit_reason == "handoff":
        print(f"\n  已切换到后台运行，可以关闭此终端窗口。之后可从启动台或 Spotlight 打开「{app.desktop.launcher.name}」。\n",
              flush=True)
    elif app.exit_reason:
        print("\n  已退出。\n", flush=True)
    return 0


TOO_NEW_EXIT = 3
ALREADY_RUNNING = 3             # to 螺壳's body, the meaning all its helpers give this code


def open_or_start(open_browser: bool = True, port: int | None = None) -> int:
    """What opening 螺壳邮件助手.app does (`python -m luoke_mail --app`): open the page of the running instance;
    if none, start one in the background -- its own process, so the app is done at once and opening it again
    runs this again -- and open its page. Problems go to a dialog: the app has no terminal."""
    d = desktop.for_this_app(hub_link=lambda: hub.Hub(config.load()["engine"].get("hub_socket")))
    started = False
    if isinstance(d, desktop.AppDesktop) and d.migrate():
        # the first time the app opens: our old login item went (and its service); background sorting moves to
        # the hub. Without the hub running it still starts now, only not at the next login.
        try:
            d.enable(0)
        except desktop.DesktopError as ex:
            print(f"[luoke-mail] login item: {type(ex).__name__}", file=sys.stderr)
            d.kickstart()
        started = True                       # once: a second one would only wait for the first
    url, proc = desktop.running(), None
    if not url:
        if started:
            pass
        elif d.supported and d.status()["installed"]:
            d.kickstart()                    # background sorting is on: the resident instance is the one
        else:
            proc = desktop.start_detached([sys.executable, "-m", "luoke_mail", "--no-browser"] + (["--port", str(port)] if port else []))
        for _ in range(60):
            time.sleep(0.5)
            url = desktop.running()
            if url:
                break
            if proc is not None and proc.poll() is not None:
                desktop.alert(memory.TOO_NEW if proc.returncode == TOO_NEW_EXIT else
                              "螺壳邮件助手没能启动。详细信息在数据文件夹的 app.log 中。")
                return 1
    if not url:
        desktop.alert("螺壳邮件助手没有响应，请稍后重试。")
        return 1
    if open_browser:
        desktop.open_url(url)
    else:
        print(url, flush=True)
    return 0


def open_page() -> int:
    """What the launcher app runs: open the page of the running instance, starting the login item if needed."""
    url = desktop.running()
    if not url:
        d = Desktop()
        if d.supported and d.login_item.status()["installed"]:
            d.kickstart()
            for _ in range(40):
                time.sleep(0.5)
                url = desktop.running()
                if url:
                    break
    if not url:
        print("螺壳邮件助手未运行。请在终端运行 python3 -m luoke_mail 启动。", file=sys.stderr)
        return 1
    desktop.open_url(url)
    return 0
