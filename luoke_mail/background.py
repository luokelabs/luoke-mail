"""Background sorting: every N minutes read new mail, sort it, and send a notification when something
needs the user (reply / add to calendar / follow up). What it changes in the mailbox can all be taken back
(top-level ADR 0016): sorted mail is marked read, confirmed habits archive / mark read / label, and in 螺壳's
own mailbox sorted mail is archived or trashed -- each listed in 今日处理 and in the operation log. It never
trashes, marks as junk or unsubscribes in the user's own mailbox, and never sends. What rules say to archive or
trash still waits for the user's one click in the page.

"Tell me once": a problem (mailbox unreachable, model service off) is reported once until it goes away,
and a mailbox failure only after it happened twice in a row (Wi-Fi is often not back right after the Mac
wakes up). The state lives in the store's cache, so the schedule and these promises survive restarts.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
import traceback

from . import engines, triage
from .mailbox import KeychainLocked, MailError
from .store import now

NEEDS_YOU = ("reply", "schedule", "follow_up")
INTERVALS = (15, 30, 60, 120)        # minutes
FIRST_RUN_DELAY = 60                 # seconds after start-up: let the model service come up after login
REPORT_AFTER = 2                     # mailbox failures in a row before the user is told
NOTIFY_WITHIN_DAYS = 3               # only mail that arrived lately is news (a wider window sorts older mail too)
TITLE = "螺壳邮件助手"


def every_text(minutes: int) -> str:
    return f"每 {minutes // 60} 小时" if minutes >= 60 else f"每 {minutes} 分钟"


def needs_you(items) -> list[dict]:
    return [it for it in items if it.get("action") in NEEDS_YOU and not it.get("suspicious")]


def describe(items: list[dict]) -> tuple[str, str]:
    """Notification subtitle and body: how many, then the two most urgent (high priority, newest first)."""
    prio = {"high": 0, "medium": 1}
    items = sorted(items, key=lambda it: it.get("date") or "", reverse=True)
    items.sort(key=lambda it: prio.get(it.get("priority"), 2))
    parts = []
    for it in items[:2]:
        who = (it.get("from_name") or it.get("from_addr") or "").strip()[:12]
        subject = (it.get("subject") or "（无主题）").strip()[:24]
        parts.append(f"{triage.ACTIONS[it['action']]}：{who}「{subject}」")
    more = f"，另有 {len(items) - 2} 封" if len(items) > 2 else ""
    return f"{len(items)} 封新邮件需要处理", "；".join(parts) + more


class AutoSync:
    def __init__(self, app, clock=time.time, tick: float = 30):
        self.app = app
        self.clock = clock
        self.tick = tick
        self.started = clock()
        self._stop = threading.Event()
        self._thread = None

    # ---- schedule -------------------------------------------------------------------------------
    def start(self) -> None:
        if not self._thread:
            self._thread = threading.Thread(target=self._loop, name="luoke-auto", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # idle work (judging old mail in 清理, reading newsletters for 订阅精选) rides on the same loop at the
        # lowest priority, checked more often while there is some
        while not self._stop.wait(5 if self.app.idle_pending() else self.tick):
            try:
                if self.due():
                    self.app.start_auto_sync()
                else:
                    self.app.idle_step()
            except ValueError:
                pass                     # a sync the user started is running; try again at the next tick
            except Exception:  # noqa: BLE001 - the loop must survive anything
                traceback.print_exc()

    def state(self, acct: str) -> dict:
        st, _ = self.app.store.cache_get(acct, "auto")
        return st or {}

    def save(self, acct: str, st: dict) -> None:
        self.app.store.cache_set(acct, "auto", st)

    def overall(self) -> dict:
        """When the last run started, and what it found -- over every mailbox (one mailbox: its own words)."""
        accts = [m["address"].lower() for m in self.app.cfg["accounts"]]
        sts = [(a, self.state(a)) for a in accts]
        tries = [st.get("last_try") for _, st in sts if st.get("last_try")]
        lasts = [(a, st["last"]) for a, st in sts if st.get("last")]
        if not lasts:
            last = None
        elif len(accts) == 1:
            last = lasts[0][1]
        else:
            last = {"at": max(l["at"] for _, l in lasts), "ok": all(l["ok"] for _, l in lasts),
                    "text": "；".join(f"{self.app.address(a)}：{l['text']}" for a, l in lasts)}
        return {"last_try": min(tries) if len(tries) == len(accts) and tries else None, "last": last}

    def due(self) -> bool:
        accts = self.app.auto_accounts()
        if not accts or self.clock() - self.started < FIRST_RUN_DELAY:
            return False
        every = self.app.cfg["auto"]["every"] * 60
        tries = [self.state(a).get("last_try") for a in accts]
        return any(t is None or self.clock() - t >= every for t in tries)

    # ---- one run (inside a job, so the page shows its progress) ---------------------------------------
    def run(self, job: dict) -> dict:
        """Every mailbox in turn. One that cannot be reached does not hold up the others; the run fails only
        when none could be."""
        app = self.app
        accts = app.auto_accounts()
        if not accts:
            raise ValueError("后台自动整理未开启，或未选择本机模型。")
        try:
            model_up = app.model_up()
        except KeychainLocked as ex:          # the Mac is locked (the model service's key): next round, quietly
            for acct in accts:
                st = self.state(acct)
                st.update(last_try=self.clock(), last={"at": now()[:16], "ok": False, "text": str(ex)})
                self.save(acct, st)
            return {}
        total: dict = {}
        errors = []
        for acct in accts:
            if job["cancel"]:
                break
            job["account"] = acct
            try:
                r = self.run_one(acct, job, model_up, app.address(acct) if len(accts) > 1 else "")
            except MailError as ex:
                errors.append(ex)
                continue
            for k, v in r.items():
                if isinstance(v, int) and not isinstance(v, bool):
                    total[k] = total.get(k, 0) + v
        if errors and len(errors) == len(accts):
            raise errors[0]
        return total

    def run_one(self, acct: str, job: dict, model_up: bool, label: str = "") -> dict:
        """One mailbox. `label` (its address, when there are several) says in each notification which one."""
        app, store = self.app, self.app.store
        at = f"（{label}）" if label else ""
        st = self.state(acct)            # record the attempt first: a failing run must not retry at every tick
        st["last_try"] = self.clock()
        self.save(acct, st)
        try:
            stats = app.sync_mailbox(acct, job, analyze=model_up)
        except KeychainLocked as ex:          # the Mac is locked: nobody to tell, nothing wrong -- next round
            st["last"] = {"at": now()[:16], "ok": False, "text": str(ex)}
            self.save(acct, st)
            return {}
        except MailError as ex:
            st["fails"] = (st.get("fails") or 0) + 1
            problem = str(ex)
            st["last"] = {"at": now()[:16], "ok": False, "text": problem}
            if st["fails"] >= REPORT_AFTER and st.get("reported") != problem:
                app.desktop.notify(TITLE, "无法连接邮箱" + at, problem)
                st["reported"] = problem
            self.save(acct, st)
            raise
        st.update(fails=0, reported=None)

        notes = []
        waiting = len(store.unanalyzed(acct))
        model_failed = waiting and (not model_up or (stats["errors"] and not stats["by_model"]))
        if model_failed:
            hub = app.cfg["engine"].get("via") == "hub"
            why = (("螺壳没有运行。打开后，下次整理时会继续分析。" if hub else "本机模型服务未运行。启动后，下次整理时会继续分析。")
                   if not model_up else next((it["error"] for it in store.unanalyzed(acct) if it.get("error")), "模型未返回结果。"))
            if not st.get("model_reported"):
                app.desktop.notify(TITLE, f"{waiting} 封新邮件尚未分析{at}", why)
                st["model_reported"] = True
            down = "螺壳没有运行" if hub else "模型服务未运行"
            notes.append(f"{waiting} 封尚未分析（{down if not model_up else '模型出错'}）")
        else:
            st["model_reported"] = False

        decided = set(stats.pop("decided"))
        since = (dt.datetime.now() - dt.timedelta(days=NOTIFY_WITHIN_DAYS)).strftime("%Y-%m-%d %H:%M")
        fresh = needs_you(it for it in store.items(acct, "pending", place=None)          # archived ones too: the point
                          if it["id"] in decided and (it.get("date") or "") >= since)
        quiet = app.agent.listening()          # 螺壳 reads the outbox: it tells the user, merged with the rest
        if fresh and not quiet:
            subtitle, body = describe(fresh)
            app.desktop.notify(TITLE, subtitle + at, body)
        auto = stats.pop("auto", None) or []
        if auto:                                   # done unasked: always told, with where to undo it
            n = sum(a["count"] for a in auto)
            words = "、".join(dict.fromkeys(a["name"] for a in auto))
            app.store.cache_set("", "open_page", {"page": "review", "at": time.time()})
            app.desktop.notify(TITLE, f"已按你的习惯处理 {n} 封{at}", f"{words}。点此在「今日处理」中查看。")
            notes.append(f"按习惯自动处理 {n} 封")
        told = set(store.cache_get(acct, "due_told")[0] or [])
        soon = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        due = [it for it in app.due_soon(acct) if it["when"] <= soon and it["id"] not in told]
        if due and not quiet:                     # the day before, once each
            first = due[0]
            day = "今天" if first["when"] <= dt.date.today().isoformat() else "明天"
            app.desktop.notify(TITLE, f"{len(due)} 件事快到期{at}",
                               f"{day}：{first.get('subject') or '（无主题）'}" + (f"，另有 {len(due) - 1} 件" if len(due) > 1 else ""))
            store.cache_set(acct, "due_told", sorted(told | {it["id"] for it in due})[-1000:])
        weekly = app.curator.weekly_note(acct)
        if weekly and not quiet:
            app.desktop.notify(TITLE, weekly[0] + at, weekly[1])
        cleaned = app.clean_note(acct)                  # 清理's old inbox mail all judged: once (luoke-mail#33)
        if cleaned and not quiet:
            app.desktop.notify(TITLE, cleaned[0] + at, cleaned[1])
        daily = app.review.daily_note(acct)            # 今日处理, once each evening (ADR 0016)
        if daily and not quiet:
            app.desktop.notify(TITLE, daily[0] + at, daily[1])
        if stats["new"]:
            notes.insert(0, f"新邮件 {stats['new']} 封" + (f"，{len(fresh)} 封需要处理" if fresh else ""))
        if stats.get("cut"):
            notes.append(stats["cut"])                  # read in part: kept, the next round goes on (luoke-mail#19)
        st["last"] = {"at": now()[:16], "ok": not model_failed, "text": "；".join(notes) or "没有新邮件"}
        self.save(acct, st)
        return {**stats, "needs_you": len(fresh)}
