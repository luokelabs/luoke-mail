"""今日处理: what the mail assistant did with each message today, for the user to look over and put right (top-level
ADR 0016, 2026-10-19: 「AI 处理过的应该就是已读了，然后根据学习的结论该归档归档，事后我自己 review AI 的处理
是不是对……每天给我一个让我 review 今天自动处理结果的页面，包括没自动处理的也算处理结果」).

Every message sorted or handled that day is listed with its outcome -- archived by a habit, marked read and left in
the inbox, waiting for the user (reply, schedule, follow up), suspicious, handled by the user here or elsewhere --
and each can be taken back or changed in one click. A change is what the memory learns from: undoing a habit's
work is recorded against the habit (App.undo), changing a suggestion is a mail-override (App.override)."""
from __future__ import annotations

import datetime as dt
import time

from . import background, samples
from .mailbox import MailError

DEMO = "demo"
# what a done message's done_action may say, as the page names it; any other value is shown as plain "done", never as
# a code (2026-10-21: mail the user archived elsewhere is settled as "quiet" / "expired" and showed as "you_quiet")
YOURS = ("archive", "trash", "spam", "complete", "draft", "skip", "skipped", "read", "flag")
AUTOS = ("archive", "trash", "read", "label", "complete")
ELSEWHERE = ("gone", "quiet", "expired")       # left the inbox outside the mail assistant; archived ones settled
NOTIFY_HOUR = 20          # the day's review is offered once, in the evening
DAYS_SHOWN = 7


class Review:
    def __init__(self, app):
        self.app = app
        self.store = app.store

    # ---- what happened to each message on a day ---------------------------------------------------------------
    def _auto(self, acct: str, day: str) -> dict[int, dict]:
        """{item id: the habit's log entry that day} -- what was done unasked."""
        back = set(self.store.cache_get(acct, "auto_undone")[0] or [])      # taken back, one at a time (App.undo)
        out = {}
        for e in self.store.log_entries(acct, limit=500):
            if e["action"] == "auto" and (e.get("ts") or "")[:10] == day and not e["undone"]:
                for i in e["item_ids"]:
                    it = self.store.get(i)
                    if it and it["message_id"] not in back:
                        out.setdefault(i, e)
        return out

    def items(self, acct: str, day: str) -> list[dict]:
        auto = self._auto(acct, day)
        logs = self.store.cache_get(acct, "auto_logs")[0] or {}
        rows = [it for it in self.store.items(acct, None, None)
                if (it.get("analyzed_at") or "")[:10] == day or (it.get("done_at") or "")[:10] == day or it["id"] in auto]
        out = []
        for it in rows:
            e = auto.get(it["id"])
            how = logs.get(str(e["id"])) or {} if e else {}
            if e and how.get("op") == "label":
                result = "auto_label"                      # labelled by a habit, still in the inbox
            elif e and it["state"] == "done":
                result = "auto_" + (it.get("done_action") if it.get("done_action") in AUTOS else "archive")
            elif it["state"] == "done" and it.get("done_action") in ELSEWHERE:
                result = "elsewhere"                       # handled in Gmail or on the phone
            elif it["state"] in ("done", "skipped"):
                result = "you_" + (it.get("done_action") if it.get("done_action") in YOURS else "done")
            elif it.get("suspicious"):
                result = "suspicious"
            elif it.get("action") in background.NEEDS_YOU:
                result = "waiting"
            elif not it.get("analyzed_at"):
                result = "unsorted"
            else:
                result = "kept"                            # sorted, marked read, left in the inbox
            out.append({"id": it["id"], "subject": it.get("subject") or "（无主题）",
                        "from": it.get("from_name") or it.get("from_addr") or "", "date": it.get("date"),
                        "action": it.get("action"), "category": it.get("category"), "summary": it.get("summary"),
                        "result": result, "log_id": e["id"] if e else None, "place": it.get("place"), "label": how.get("label"),
                        "read_by_us": bool(it.get("read_by_us")) and it.get("read_by_us") != "unread",
                        "suspicious": bool(it.get("suspicious")),
                        "state": it["state"]})
        # how late, as the inbox counts it: the sample inbox lives on its own day (luoke-mail#22, 10-23);
        # which day it was handled stays the real one
        today = samples.TODAY if acct == DEMO else dt.date.today()
        for x, it in zip(out, rows):
            x["older"] = bool(it.get("older"))             # unread from before the range, sorted as idle work
            x["due_date"], x["late"] = it.get("due_date"), self._late(it, today)
        order = {"suspicious": 0, "waiting": 1}               # what needs the user first, then newest first
        out.sort(key=lambda x: x["date"] or "", reverse=True)
        return sorted(out, key=lambda x: order.get(x["result"], 2))

    @staticmethod
    def _late(it: dict, today: dt.date) -> str | None:
        """For mail that waits on the user: "overdue" (its date has passed), "due" (within three days), "old" (it
        came more than a month ago and still waits) -- so what sat unanswered for months stands out."""
        if it["state"] != "pending" or it.get("action") not in background.NEEDS_YOU:
            return None
        try:
            due = dt.date.fromisoformat((it.get("due_date") or "")[:10]) if it.get("due_date") else None
        except ValueError:
            due = None
        if due and due < today:
            return "overdue"
        if due and (due - today).days <= 3:
            return "due"
        try:
            came = dt.date.fromisoformat((it.get("date") or "")[:10])
        except ValueError:
            return None
        return "old" if (today - came).days > 30 else None

    def view(self, source: str | None, day: str | None = None) -> dict:
        acct = self.app.account(source)
        today = dt.date.today()
        day = day or today.isoformat()
        items = self.items(acct, day)
        counts: dict[str, int] = {}
        for x in items:
            if x["older"]:
                counts["older"] = counts.get("older", 0) + 1
            k = "auto" if x["result"].startswith("auto_") else "you" if x["result"].startswith("you_") else x["result"]
            counts[k] = counts.get(k, 0) + 1
        days = [(today - dt.timedelta(days=k)).isoformat() for k in range(DAYS_SHOWN)]
        return {"day": day, "days": days, "items": items, "counts": counts, "total": len(items),
                "mark_read": self.app.cfg.get("flow", {}).get("mark_read", True)}

    # ---- putting it right ---------------------------------------------------------------------------------------
    def act(self, body: dict) -> dict:
        """`op`: "undo" (a habit's archiving, this message only), "unread" (mark it unread again; it is not marked
        read again). Changing the suggestion, archiving or deleting go through the usual endpoints."""
        source = body.get("source")
        acct = self.app.account(source)
        it = self.store.get(int(body.get("id") or 0))
        if not it or it["account"] != acct:
            raise ValueError("这封邮件已不在列表中，请刷新页面。")
        op = body.get("op")
        if op == "undo":
            e = next((e for d in {(it.get("done_at") or "")[:10], (it.get("analyzed_at") or "")[:10], body.get("day") or "",
                                  dt.date.today().isoformat()} if d for e in [self._auto(acct, d).get(it["id"])] if e), None)
            if not e:
                raise ValueError("这封邮件没有被自动处理，或者已经撤销。")
            r = self.app.undo(source, e["id"], [it["id"]])
            return {**self.view(source, body.get("day")), "message": "已撤销，邮件回到收件箱" if r.get("ok") else r.get("message")}
        if op == "unread":
            if acct != DEMO and it.get("uid"):
                try:
                    with self.app.open_mailbox(acct) as mb:
                        mb.store_flags("INBOX", [it["uid"]], "(\\Seen)", add=False)
                except MailError as ex:
                    raise ValueError(str(ex)) from None
            self.store.set_fields(it["id"], read_by_us="unread")       # not marked read again
            return {**self.view(source, body.get("day")), "message": "已标为未读"}
        raise ValueError("不支持的操作。")

    # ---- telling the user ---------------------------------------------------------------------------------------
    def seen(self, body: dict) -> dict:
        acct = self.app.account(body.get("source"))
        self.store.cache_set(acct, "review_seen", time.strftime("%Y-%m-%d %H:%M:%S"))
        return {"ok": True}

    def unseen(self, acct: str) -> int:
        """Today's handled mail the user has not looked at in 今日处理 yet (the rail's badge)."""
        since = self.store.cache_get(acct, "review_seen")[0] or ""
        day = dt.date.today().isoformat()
        return sum(1 for x in self.items(acct, day) if self._when(x) > since)

    def _when(self, x: dict) -> str:
        it = self.store.get(x["id"]) or {}
        return max(it.get("analyzed_at") or "", it.get("done_at") or "")

    def daily_note(self, acct: str) -> tuple[str, str] | None:
        """Once a day, after NOTIFY_HOUR: how many were handled today, for the user to look over."""
        now = dt.datetime.now()
        if now.hour < NOTIFY_HOUR:
            return None
        day = now.date().isoformat()
        if self.store.cache_get(acct, "review_told")[0] == day:
            return None
        v = self.view(acct, day)
        if not v["total"]:
            return None
        self.store.cache_set(acct, "review_told", day)
        c = v["counts"]
        parts = [f"按习惯处理 {c['auto']} 封" if c.get("auto") else None, f"等你处理 {c['waiting']} 封" if c.get("waiting") else None,
                 f"可疑 {c['suspicious']} 封" if c.get("suspicious") else None]
        self.store.cache_set("", "open_page", {"page": "review", "at": time.time()})
        return f"今天处理了 {v['total']} 封邮件", "；".join(p for p in parts if p) or "点此在「今日处理」中查看。"
