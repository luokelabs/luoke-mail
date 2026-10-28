"""订阅精选 and the 记忆 page, as the App offers them to the page and to background sorting.

Reading is lowest-priority idle work, like judging old mail: it runs a few minutes at a time whenever nothing
else is running, and anything else asks it to stop after the current newsletter. Two runs feed it:
- "backlog": subscription mail the user chose in 清理 ("先挑出值得看的"), read once before it is cleared;
- "weekly": new mail from the 精选来源, queued at every sync, gathered into this week's picks.
What the user says about a piece (收藏 / 没兴趣) and what they open go to their memory (luoke-mem); 归档 only puts
a read piece aside."""
from __future__ import annotations

import datetime as dt
import os
import re
import time

from . import desktop, digest, editor, engines, memory, providers, samples, triage
from .mailbox import MailError
from .subscriptions import sender_key

DEMO = "demo"
SLICE_SECONDS = 240
WEEK_DAYS = 7
MONTHS_SHOWN = 60
NOTIFY_HOUR = 8                       # the weekly note goes out on the first background run after Monday 08:00
NOTE_KINDS = ("fact", "preference", "interest", "dislike", "taste", "term")   # every kind (ADR 0013), the input method's too
# sensitive context is kept marked, and the mail assistant may not read it back (top-level ADR 0004)
# where an understanding came from, by the records it cites (who said it)
SOURCES = {"own-site": "你的网站", "own-label": "你的邮箱标签", "sent-mail": "你发出的邮件", "mail-read": "你的阅读记录",
           "digest-open": "你的阅读记录", "digest-feedback": "你对精选的反馈", "received-mail": "你收到的邮件",
           "override": "你的操作", "mail-cleared": "你清理的邮件"}


# other luoke tools write to the same memory (top-level ADR 0002): their understandings are shared, their raw
# records are theirs -- shown here only as where they came from and how many
TOOL_NAMES = memory.luoke_mem.TOOL_NAMES        # one copy of the names users see: luoke-mem's (0.15.4)


def tool_name(tool: str | None) -> str:
    return TOOL_NAMES.get(tool or "", "其他螺壳工具")


def own_record(c: dict) -> bool:
    return c.get("tool", memory.TOOL) == memory.TOOL and c.get("text") is not None


def origin_text(n: dict) -> str:
    """「你填写的」, or what it was drawn from: 「根据…推断」 when it took effect by itself (from the user's own things),
    「根据…推测」 for a guess (「，已确认」 once the user said yes). Another tool's: 「来自螺壳输入法」."""
    if n["origin"] == "user":             # whichever page it was said on: the user's words belong to the memory (ADR 0009)
        return "你填写的"
    if n.get("tool") and n["tool"] != memory.TOOL:
        return f"来自{tool_name(n['tool'])}" + ("，已确认" if n["status"] == "active" and n["decided_at"] else "")
    topics = {c.get("topic") for c in n["cites"]}
    names = list(dict.fromkeys(v for k, v in SOURCES.items() if k in topics))[:3]
    names = names[:1] + [re.sub(r"^你的?", "", x) for x in names[1:]]          # 你的网站、邮箱标签和阅读记录
    guess = bool(n["decided_at"]) or n["status"] == "proposed"
    joined = "、".join(names[:-1]) + "和" + names[-1] if len(names) > 1 else "".join(names)
    text = ("根据" + joined if names else "") + ("推测" if guess else "推断")
    return text + ("，已确认" if n["status"] == "active" and n["decided_at"] else "")


class Curator:
    def __init__(self, app):
        self.app = app
        self.store = app.store

    # ---- what the user cares about, from their memory -------------------------------------------------
    def taste(self, acct: str) -> tuple[str, list[str], list[str]]:
        """(关注的话题 / 不感兴趣 block, titles saved, titles marked 没兴趣), newest first."""
        mem = self.app.memory_for(acct)
        latest: dict[str, dict] = {}
        for r in mem.records(topic="digest-feedback", limit=200):
            latest.setdefault(r["ref"], r)                      # newest first: the first one per piece counts
        liked = [r["data"]["title"] for r in latest.values() if (r["data"] or {}).get("value") == "useful" and r["data"].get("title")]
        disliked = [r["data"]["title"] for r in latest.values()
                    if (r["data"] or {}).get("value") == "dismissed" and r["data"].get("title")]
        # the memory works out what the digest goes by (luoke-mem 0.19: taste, from the mail assistant's records);
        # until it has, what was understood of the user's interests stands in (only what it is sure enough of)
        # what not to pick goes first, or a long list of tastes left it out of the 500 characters; interests only stand in
        # while there are no tastes (each topic tends to be both: prompt review, 10-22)
        kinds = ["dislike", "taste"] if mem.notes(kinds=["taste"]) else ["dislike", "interest"]
        return (mem.context(kinds, limit=500), liked[:digest.FEEDBACK_EXAMPLES], disliked[:digest.FEEDBACK_EXAMPLES])

    def _taste_mark(self, acct: str) -> str:
        mem = self.app.memory_for(acct)
        return "|".join(sorted(f"{n['kind']}:{n['text']}" for n in mem.notes(kinds=["taste", "interest", "dislike"])))[:4000]

    def follow_taste(self, acct: str) -> bool:
        """What the digest goes by changed (the memory works it out now, in its own time): choose again."""
        mark = self._taste_mark(acct)
        st = self.store.cache_get(acct, "editor")[0] or {}
        if st.get("taste") == mark:
            return False
        first = "taste" not in st                         # the first look only notes where things stand
        if not first:
            self.reedit(acct)
        st = self.store.cache_get(acct, "editor")[0] or {}
        st["taste"] = mark
        self.store.cache_set(acct, "editor", st)
        return not first

    # ---- the 精选 page ------------------------------------------------------------------------------------
    def now(self, acct: str) -> dt.datetime:
        return dt.datetime.combine(samples.TODAY, dt.time(9)) if acct == DEMO else dt.datetime.now()

    def view(self, acct: str) -> dict:
        mem = self.app.memory_for(acct)
        since = self._since(acct)
        todo = set(self.edit_pending(acct, waiting=True))
        week, week_pending = self._chosen(acct, "week", None, todo)
        months = []
        counts = self.store.digest_months(acct, since)
        for m in sorted(counts, reverse=True)[:MONTHS_SHOWN]:
            items, pending = self._chosen(acct, "month", m, todo)
            months.append({"month": m, "items": [self.public_item(acct, i) for i in items if not i.get("archived_at")],
                           "pending": pending})
        week_total, earlier_total = self.store.digest_counts(acct, since)
        edit_state, _ = self.store.cache_get(acct, "edit")
        state, _ = self.store.cache_get(acct, "read")
        busy = self.app.running_job()
        counts = self.store.digest_mail_counts(acct)
        sources = self.store.digest_sources(acct)
        return {
            # what the picks go by: the memory's 精选的口味, and what it understood of the user's interests
            "interests": list({n["text"]: n for n in mem.notes(kinds=["interest"]) + mem.notes(kinds=["taste"])}.values()),
            "dislikes": mem.notes(kinds=["dislike"]),
            "proposed": [],                               # the memory asks what is worth it: on the 记忆 page
            "sources": sources, "since": since,
            "reading": {"queued": counts.get("queued", 0), "read": counts.get("read", 0), "failed": counts.get("failed", 0),
                        "on": (state or {}).get("on", True), "problem": (state or {}).get("problem"),
                        "running": bool(busy and busy["kind"] == "read" and busy.get("account") == acct)},
            "week": [self.public_item(acct, i) for i in week if not i.get("archived_at")], "week_pending": week_pending,
            "week_total": week_total,
            "saved": [self.public_item(acct, i) for i in self.store.digest_saved(acct)],
            "archived": [self.public_item(acct, i) for i in self.store.digest_archived(acct)],
            "months": months, "earlier_total": earlier_total,
            "editing": {"pending": len(todo), "problem": (edit_state or {}).get("problem"),
                        "running": bool(busy and busy["kind"] == "edit" and busy.get("account") == acct)},
            "profile": self.app.profiler.view(acct),
            "has_model": self.app.has_model(),
        }

    def public_item(self, acct: str, it: dict) -> dict:
        mid = it.get("message_id") or ""
        gmail = acct != DEMO and self.app.provider(acct) == "gmail" and mid and not mid.strip("<>").startswith("uid-")
        return {**{k: it.get(k) for k in ("id", "title", "summary", "url", "host", "topics", "lasting", "score", "why",
                                          "date", "source", "feedback", "opened", "message_id", "also", "pick_why")},
                "archived": bool(it.get("archived_at")),
                "mail_url": providers.gmail_message_url(self.app.address(acct), mid, self.store.gmail_id(acct, mid))
                if gmail else None}

    def feedback(self, body: dict) -> dict:
        """收藏 / 没兴趣 (or taking it back): the user's own words about a piece, kept in their memory. A saved piece
        stays in 收藏 however old it gets (the value is still "useful": what it means to the picks has not changed)."""
        acct = self.app.account(body.get("source"))
        it = self.store.digest_item(int(body.get("id") or 0))
        if not it or it["account"] != acct:
            raise ValueError("内容不存在。")
        value = body.get("value") if body.get("value") in ("useful", "dismissed") else None
        self.store.digest_set(it["id"], feedback=value)
        title = memory.mask(it["title"]) or None                                # a code in a title: masked, the rest kept
        what = f"「{title}」" if title else "一条内容"
        text = {"useful": f"收藏了{what}", "dismissed": f"对{what}：没兴趣"}.get(value) or \
            (f"取消收藏{what}" if it["feedback"] == "useful" else f"撤回了对{what}的评价")
        self.app.memory_for(acct).record(
            "user", "statement", text,
            topic="digest-feedback", ref=str(it["id"]), happened_at=None, sensitive=memory.marks(title),
            data=memory.sourced(acct, {"value": value, "title": title, "source": it["source"], "by": "user",
                                       "sender": self.store.digest_sender(acct, it["message_id"]), **self._text_of(it)}))
        return self.public_item(acct, {**it, "feedback": value})

    @staticmethod
    def _text_of(it: dict) -> dict:
        """What the piece says, for the memory to read (ADR 0015): its title and the reader's summary of it."""
        text = " ".join(x for x in (it.get("title") or "", " ".join((it.get("summary") or "").split())) if x)
        return {"text": memory.mask(text)} if text else {}

    def archive(self, body: dict) -> dict:
        """归档: read, put aside -- off the lists, found again under 已归档. Not a judgement (that is 没兴趣), so
        nothing goes to the memory, and what the editor chose stays chosen."""
        acct = self.app.account(body.get("source"))
        it = self.store.digest_item(int(body.get("id") or 0))
        if not it or it["account"] != acct:
            raise ValueError("内容不存在。")
        at = time.strftime("%Y-%m-%d %H:%M:%S") if body.get("on") else None
        self.store.digest_set(it["id"], archived_at=at)
        return self.public_item(acct, {**it, "archived_at": at})

    def opened(self, body: dict) -> dict:
        """Opening a piece shows attention, nothing more (an activity, not a view)."""
        acct = self.app.account(body.get("source"))
        it = self.store.digest_item(int(body.get("id") or 0))
        if not it or it["account"] != acct:
            raise ValueError("内容不存在。")
        if not it["opened"]:
            self.store.digest_set(it["id"], opened=1)
            title = memory.mask(it["title"]) or None
            self.app.memory_for(acct).record("user", "activity", f"打开了：{title}" if title else "打开了一条内容", topic="digest-open",
                                             ref=str(it["id"]), data=memory.sourced(acct, {"title": title, "source": it["source"], "by": "user",
                                                                                         "sender": self.store.digest_sender(acct, it["message_id"]), **self._text_of(it)}),
                                             sensitive=memory.marks(title))
        return {"ok": True}

    def set_source(self, body: dict) -> dict:
        acct = self.app.account(body.get("source"))
        key = body.get("key") or ""
        if not key:
            raise ValueError("请选择发件人。")
        if body.get("on"):
            self.store.digest_add_source(acct, key, body.get("name"), "user")
        else:
            self.store.digest_remove_source(acct, key)
        return self.view(acct)

    def set_reading(self, body: dict) -> dict:
        acct = self.app.account(body.get("source"))
        on = bool(body.get("on"))
        if on:
            self.need_model()
        self.store.cache_set(acct, "read", {"on": on, "problem": None})
        if on:
            self.app.idle_step(prefer="read")
        else:
            busy = self.app.running_job()
            if busy and busy["kind"] == "read" and busy.get("account") == acct:
                busy["cancel"] = True
        return self.view(acct)

    def need_model(self) -> None:
        if not self.app.has_model():
            raise ValueError("请先选择本机模型。")

    # ---- feeding the reader ---------------------------------------------------------------------------------
    def read_from_clean(self, body: dict) -> dict:
        """「先挑出值得看的」 in 清理: the chosen groups' mail is read before it is cleared, and their senders
        become 精选来源, so what they send next lands in the weekly digest instead of the inbox list."""
        source, acct, scope = self.app._scope(body)
        self.need_model()
        keys = set(body.get("keys") or [])
        rows = [r for r in self.store.old_rows(acct, scope) if not triage.held(r)
                and f"{r['kind'] or 'pending'}|{self.app.old_key(r)}" in keys]
        if not rows:
            raise ValueError("请选择要读取的邮件。")
        for r in rows:
            self.store.digest_add_source(acct, sender_key(r), r["from_name"] or r["from_addr"], "clean")
        self.store.digest_queue(acct, [{**r, "source_key": sender_key(r), "name": r["from_name"] or r["from_addr"]}
                                       for r in rows], "backlog")
        self.store.cache_set(acct, "read", {"on": True, "problem": None})
        self.app.idle_step(prefer="read")
        return self.app.old_view(acct, scope)

    def queue_weekly(self, acct: str) -> int:
        """New mail from 精选来源 (after a sync): this week's digest reads it."""
        keys = {s["key"] for s in self.store.digest_sources(acct)}
        if not keys:
            return 0
        since = (self.now(acct) - dt.timedelta(days=WEEK_DAYS)).strftime("%Y-%m-%d")
        rows = [{"message_id": it["message_id"], "source_key": sender_key(it), "name": it["from_name"] or it["from_addr"],
                 "folder": "INBOX", "uid": it["uid"], "subject": it["subject"], "date": it["date"]}
                for it in self.store.items(acct, None, place=None) if sender_key(it) in keys and (it["date"] or "") >= since]
        return self.store.digest_queue(acct, rows, "weekly")

    def in_clean(self, acct: str, rows: list[dict]) -> dict[str, dict]:
        """Per 清理 group key: how much of it the model has read and how many messages hold a pick."""
        kept = self.kept_mids(acct)
        state = self.store.digest_states(acct)
        out: dict[str, dict] = {}
        for r in rows:
            st = state.get(r["message_id"])
            if not st:
                continue
            g = out.setdefault(f"{r['kind'] or 'pending'}|{self.app.old_key(r)}", {"queued": 0, "read": 0, "kept": 0})
            g["queued" if st == "queued" else "read"] += 1
            g["kept"] += int(r["message_id"] in kept)
        return out

    def kept_mids(self, acct: str) -> set[str]:
        """Mail that holds something worth keeping: what the editor chose, or a piece the user saved. Where
        the editor has not chosen yet, every candidate is kept for now -- nothing is trashed before it is judged."""
        since = self._since(acct)
        done = (self.store.cache_get(acct, "editor")[0] or {}).get("months") or {}
        counts = self.store.digest_months(acct, since)
        week_done = not any(k == "week" for k, _ in self.edit_pending(acct, waiting=True))
        kept = {i["message_id"] for i in self.store.digest_picked(acct, "pick_week")}
        for r in self.store.digest_candidates(acct, lasting=True):
            date = r["date"] or ""
            edited = week_done if date >= since else done.get(date[:7]) == counts.get(date[:7])
            if r["feedback"] == "useful" or r["pick_month"] or r["pick_week"] or not edited:
                kept.add(r["message_id"])
        return kept

    # ---- the editor (idle work): the few worth reading, out of everything worth a look ---------------------
    def _since(self, acct: str) -> str:
        return (self.now(acct) - dt.timedelta(days=WEEK_DAYS)).strftime("%Y-%m-%d")

    def _candidates(self, acct: str, kind: str, month: str | None) -> list[dict]:
        since = self._since(acct)
        if kind == "week":
            return self.store.digest_candidates(acct, since=since)
        y, m = int(month[:4]), int(month[5:7])
        after = f"{y + (m == 12)}-{m % 12 + 1:02d}-01"
        return self.store.digest_candidates(acct, since=f"{month}-01", before=min(after, since), lasting=True)

    def _signature(self, acct: str, kind: str, month: str | None) -> str:
        c = self._candidates(acct, kind, month)
        return f"{len(c)}:{max((i['id'] for i in c), default=0)}"

    def edit_pending(self, acct: str, waiting: bool = False) -> list[tuple[str, str | None]]:
        """What the editor has not chosen from yet, or whose candidates changed since: the last seven days first,
        then month by month, newest first. A period with mail still to be read waits for it (choosing again after
        every newsletter would be wasted), unless `waiting` asks for those too."""
        self.follow_taste(acct)
        st = self.store.cache_get(acct, "editor")[0] or {}
        since = self._since(acct)
        unread = set() if waiting else self.store.digest_unread_dates(acct)
        out: list[tuple[str, str | None]] = []
        if (self.store.digest_candidates(acct, since=since) and st.get("week") != self._signature(acct, "week", None)
                and not any(d >= since for d in unread)):
            out.append(("week", None))
        done = st.get("months") or {}
        counts = self.store.digest_months(acct, since)
        busy = {d[:7] for d in unread}
        out += [("month", m) for m in sorted(counts, reverse=True) if done.get(m) != counts[m] and m not in busy]
        return out

    def _chosen(self, acct: str, kind: str, month: str | None, todo: set | None = None) -> tuple[list[dict], bool]:
        """(what to show for a period, still waiting for the editor). Until the editor has chosen, the reader's
        own best are shown in its place."""
        quota = editor.WEEK_QUOTA if kind == "week" else editor.MONTH_QUOTA
        best = digest.picks(self._candidates(acct, kind, month), lasting_only=kind == "month")   # one story once
        pending = (kind, month) in (todo if todo is not None else set(self.edit_pending(acct, waiting=True)))
        if pending:
            return best[:quota], True
        col = "pick_week" if kind == "week" else "pick_month"
        return [i for i in best if i[col]], False

    def edit_next_account(self) -> str | None:
        if not (self.app.has_model() and self.app.link().ready("digest-pick")):
            return None
        for acct in self.app.idle_accounts():
            st = (self.store.cache_get(acct, "edit")[0] or {}) if acct else {}
            if acct and time.time() - (st.get("failed_at") or 0) > 60 and self.edit_pending(acct):
                return acct
        return None

    def edit_slice(self, acct: str, job: dict, seconds: float = SLICE_SECONDS) -> dict:
        link = self.app.link("background", job=job)
        taste, liked, disliked = self.taste(acct)
        t0, edited = time.time(), 0
        todo = self.edit_pending(acct)
        for i, (kind, month) in enumerate(todo):
            if job["cancel"] or time.time() - t0 > seconds:
                break
            job.update(stage="正在挑选：" + ("最近 7 天" if kind == "week" else f"{month[:4]} 年 {int(month[5:])} 月"),
                       done=i, total=len(todo))
            raw = self._candidates(acct, kind, month)
            cands = digest.picks(raw, lasting_only=kind == "month")
            quota = editor.WEEK_QUOTA if kind == "week" else editor.MONTH_QUOTA
            try:
                chosen = editor.edit(cands, quota, taste, liked, disliked, link, stop=lambda: job["cancel"])
            except engines.Busy:
                break                                    # the hub has more urgent work: this period comes again
            except engines.EngineError as ex:
                self.store.cache_update(acct, "edit", problem=str(ex), failed_at=time.time())
                break
            if chosen is None:                           # asked to make way: this period is chosen again later
                break
            self.store.digest_set_picks(acct, "pick_week" if kind == "week" else "pick_month", [r["id"] for r in raw],
                                        {it["id"]: why for it, why in chosen})
            st = self.store.cache_get(acct, "editor")[0] or {}
            if kind == "week":
                st["week"] = self._signature(acct, "week", None)
            else:
                st.setdefault("months", {})[month] = len(raw)
            self.store.cache_set(acct, "editor", st)
            self.store.cache_update(acct, "edit", problem=None)
            edited += 1
        return {"edited": edited}

    def reedit(self, acct: str, months_back: int = 3) -> None:
        """What the user cares about changed: choose again for the last seven days and the latest months (older
        months keep what was chosen; choosing them all again would take an hour of the model's time)."""
        st = self.store.cache_get(acct, "editor")[0] or {}
        st.pop("week", None)
        months = st.get("months") or {}
        for m in sorted(months, reverse=True)[:months_back]:
            months.pop(m, None)
        self.store.cache_set(acct, "editor", st)

    # ---- the reader (idle work) ----------------------------------------------------------------------------
    def next_account(self) -> str | None:
        if not (self.app.has_model() and self.app.link().ready("digest-read")):
            return None
        for acct in self.app.idle_accounts():
            if not acct:
                continue
            st, _ = self.store.cache_get(acct, "read")
            st = st or {}
            if st.get("on", True) and time.time() - (st.get("failed_at") or 0) > 60 and self.store.digest_mail_counts(acct).get("queued"):
                return acct
        return None

    def read_slice(self, acct: str, job: dict, seconds: float = SLICE_SECONDS) -> dict:
        link = self.app.link("background", job=job)
        taste, liked, disliked = self.taste(acct)
        today = self.now(acct).date()
        t0, read = time.time(), 0
        counts = self.store.digest_mail_counts(acct)
        total = sum(counts.values())
        mb = None
        try:
            if acct != DEMO:
                mb = self.app.open_mailbox(acct)
                mb.open()
            while not job["cancel"] and time.time() - t0 < seconds:
                batch = self.store.digest_next(acct, 3)
                if not batch:
                    break
                for row in batch:
                    if job["cancel"] or time.time() - t0 > seconds:
                        break
                    c = self.store.digest_mail_counts(acct)
                    job.update(stage=f"正在阅读：{(row['subject'] or '')[:30]}", done=c.get("read", 0) + c.get("failed", 0), total=total)
                    if acct == DEMO:
                        raw = samples.demo_newsletter(row)
                    else:
                        try:
                            raw = self._fetch(mb, row)
                        except MailError:              # the server dropped a connection left idle (功耗 rests): once more
                            mb.close()
                            mb = self.app.open_mailbox(acct)
                            mb.open()
                            raw = self._fetch(mb, row)
                    if raw is None:
                        self.store.digest_read(acct, row["message_id"], None, None, "邮件已不在邮箱中。")
                        continue
                    try:
                        items = digest.extract(digest.read(raw), taste, liked, disliked, today, link)
                    except engines.Busy:
                        return {"read": read}            # the hub has more urgent work: come back later
                    except engines.EngineError as ex:
                        self.store.cache_update(acct, "read", problem=str(ex), failed_at=time.time())
                        return {"read": read}
                    self.store.digest_read(acct, row["message_id"], items, link.name,
                                           None if items is not None else "模型未按要求返回结果。")
                    self.store.cache_update(acct, "read", problem=None)     # never "on": that is the user's switch
                    read += 1
        except MailError as ex:
            self.store.cache_update(acct, "read", problem=str(ex), failed_at=time.time())
        finally:
            if mb:
                mb.close()
        return {"read": read}

    @staticmethod
    def _fetch(mb, row: dict) -> bytes | None:
        """The message by its Message-ID: a UID is only a hint (mail changes folder, UIDs change). Inbox mail
        may have been archived since it was queued; then it is looked for in the archive."""
        def look(folder: str, uid: str | None) -> bytes | None:
            raw = mb.fetch_raw(uid, folder) if uid else None
            if raw and digest.read(raw)["message_id"] == row["message_id"]:
                return raw
            found = mb.locate(folder, row["message_id"])
            return mb.fetch_raw(found, folder) if found else None
        first = row["folder"] or "INBOX"
        raw = look(first, row["uid"])
        if raw or first != "INBOX":
            return raw
        try:
            return look(mb.folder_for("archive"), None)
        except MailError:                       # QQ / NetEase may have no archive folder at all
            return None

    # ---- the weekly note ----------------------------------------------------------------------------------
    def weekly_note(self, acct: str) -> tuple[str, str] | None:
        """Once a week, on the first background run after Monday 08:00: this week's picks, if there are any, and how
        many questions the memory would like to ask (the memory is the person's: said with the first mailbox)."""
        now = self.now(acct)
        start = digest.week_start(now)
        if now < start + dt.timedelta(hours=NOTIFY_HOUR):
            return None
        week = start.strftime("%Y-%m-%d")
        sent, _ = self.store.cache_get(acct, "digest_note")
        if sent == week:
            return None
        top, _ = self._chosen(acct, "week", None)
        accts = self.app.mail_accounts()
        guesses = self.counts()["proposed"] if accts and acct == accts[0] else 0
        if not top and not guesses:
            return None
        self.store.cache_set(acct, "digest_note", week)
        if not top:
            return f"有 {guesses} 个问题想问你", "在「记忆」中回答。"
        picks = "；".join(f"「{i['title'][:24]}」" for i in top[:2]) + (f"，另有 {len(top) - 2} 条" if len(top) > 2 else "")
        return f"本周订阅精选：{len(top)} 条", picks + (f"。另有 {guesses} 个问题想问你" if guesses else "")

    # ---- the 记忆 page ---------------------------------------------------------------------------------------
    def memory_view(self, source: str | None) -> dict:
        acct = DEMO if source == "demo" else self.app.account(source)
        mem = self.app.memory_for(acct)

        def pub(n: dict) -> dict:
            full = mem.note(n["id"])
            # another tool's records: where from and how many, never the text (luoke-mem 0.5 gives only that, in
            # cites_elsewhere; 0.4 still listed them among the cites)
            others: dict[str, int] = {}
            for c in [c for c in full["cites"] if not own_record(c)] + list(full.get("cites_elsewhere") or []):
                if (full["origin"] == "user" or full.get("decided_at")) and c.get("tool") == "luoke-mem":
                    continue                      # the user's own statement, kept by the memory: 「你填写的」 says it
                others[tool_name(c.get("tool"))] = others.get(tool_name(c.get("tool")), 0) + int(c.get("count") or 1)
            return {**n, "origin_text": origin_text(full), "learnt": n["origin"] == "inferred" and not n["decided_at"],
                    "cites": [{k: c.get(k) for k in ("text", "kind", "topic", "happened_at", "recorded_at")}
                              for c in full["cites"] if own_record(c)][:12],
                    "others": [{"source": s, "count": k} for s, k in others.items()]}
        return {"asks": self.asks(mem), "proposed": [],
                **{k: [pub(n) for n in mem.notes(kinds=[k]) if n.get("topic") != "site"] for k in NOTE_KINDS},
                "sites": self.app.profiler.sites(acct), "profile": self.app.profiler.view(acct),
                "learning": self.app.learner.view(acct),
                "whole": acct != DEMO and (memory.memory_entry() is not None if self._installed_memory()
                                           else memory.whole_memory_ui() is not None)}

    # ---- what the memory would like to ask, and what it suggests (ADR 0012: the memory decides what is worth it) --
    # the choices luoke-mem's reply() takes, by the words of the options it gives (0.18; 0.20 adds 「以前是，现在不是」)
    CHOICES = {"对": "yes", "不对": "no", "以前是，现在不是": "past", "以后再说": "later",
               "好": "useful", "有用": "useful", "不用": "useless", "没用": "useless", "别再提": "never"}
    DEFAULTS = {"question": (("对", "yes"), ("不对", "no"), ("以后再说", "later")),
                "suggestion": (("好", "useful"), ("不用", "useless"), ("别再提", "never"))}

    def asks(self, mem) -> list[dict]:
        """Open questions and suggestions that concern the mail assistant (`asks(tool=...)`), for the 记忆 page
        while luoke-agent is not there (ADR 0012 amendment 6). An older memory has none."""
        try:
            rows = mem.asks(tool=memory.TOOL)
        except (AttributeError, TypeError):
            return []
        out = []
        for a in rows:
            if a.get("kind") not in self.DEFAULTS or (a.get("kind") == "question" and not a.get("about_note")):
                continue                                  # a question of its own wants words: luoke-mem's page asks it
            given = (a.get("answer") or {}).get("options") or []
            pairs = [(l, self.CHOICES[l]) for l in given] if given and all(l in self.CHOICES for l in given) \
                else self.DEFAULTS[a["kind"]]
            out.append({"id": a["id"], "kind": a["kind"], "text": a.get("text") or "", "why": a.get("why") or "",
                        "about_kind": a.get("about_kind"), "options": [{"label": l, "choice": c} for l, c in pairs]})
        return out

    def _installed_memory(self) -> bool:
        """The app, on the user's own memory: 「查看整份记忆」 is the 螺壳记忆 they installed, as from the input method
        and the agent -- not the page of the library this app carries, which may be another version (top-level
        boundary audit, 2026-10-16). Run from source, or on a copy of the data, the library's own page."""
        return self.app.hub_only and os.path.realpath(self.app.mem.path) == os.path.realpath(memory.luoke_mem.default_path())

    def open_whole(self, body: dict) -> dict:
        """「查看整份记忆」: luoke-mem's own page, started (or found running) for the memory file luoke-mail uses --
        the real one, or the copy's when LUOKE_MAIL_HOME points at a copy. Not for the demo's memory."""
        acct = self.app.account(body.get("source"))
        if acct != DEMO and self._installed_memory():
            entry = memory.memory_entry()
            if not entry:
                raise ValueError("没有找到螺壳记忆。")
            if entry == memory.MEMORY_URL:
                desktop.open_url(entry)                   # 螺壳主控 starts the memory's page and opens the browser
            else:
                desktop.open_app(entry)
            return {"opened": True}
        ui = memory.whole_memory_ui()
        if acct == DEMO or ui is None:
            raise ValueError("整份记忆的页面需要更新的螺壳记忆（luoke-mem）。")
        try:
            return {"url": ui.ensure(self.app.mem.path)}
        except (RuntimeError, OSError) as ex:
            print(f"luoke-mail: luoke-mem ui did not start: {ex!r}", flush=True)
            raise ValueError("螺壳记忆的页面没能打开，请稍后再试。") from None

    def memory_change(self, body: dict) -> dict:
        source = body.get("source")
        acct = self.app.account(source)
        mem = self.app.memory_for(acct)
        op = body.get("op")
        message = ""
        text = " ".join(str(body.get("text") or "").split())[:200]
        try:
            target = mem.note(int(body["id"])) if body.get("id") else None
            if {body.get("kind"), (target or {}).get("kind")} & {"interest", "dislike"} or op == "forget_mine":
                self.reedit(acct)                 # what the user cares about changed: the picks are chosen again
            if op == "add":
                if body.get("kind") not in NOTE_KINDS or not text:
                    raise ValueError("请填写内容。")
                text = memory.mask(text)                     # a credential typed here: hidden where it stands
                mem.state(body["kind"], text, sensitive=memory.marks(text))
            elif op == "reply":
                ask = next((a for a in self.asks(mem) if a["id"] == int(body["id"])), None)
                choice = body.get("choice")
                if not ask or choice not in {o["choice"] for o in ask["options"]}:
                    raise ValueError("这个问题已经处理过了，请刷新页面。")
                mem.reply(ask["id"], choice)
                if ask.get("about_kind") in ("interest", "dislike", "taste"):
                    self.reedit(acct)
            elif op in ("confirm", "reject", "forget"):
                getattr(mem, op)(int(body["id"]))
                self.app.learner.refill(acct)                      # the next guess, if any
            elif op == "revise":
                if not text:
                    raise ValueError("请填写内容。")
                text = memory.mask(text)
                mem.revise(int(body["id"]), text, sensitive=memory.marks(text))   # the new words may bring a kind
            elif op == "forget_mine":
                # what the mail assistant wrote, and the understandings resting only on it; what the user said stays,
                # it is the memory's (top-level ADR 0009). Clearing the whole memory, or what other tools wrote, is
                # luoke-mem's (ADR 0002). The demo's memory is luoke-mail's own sample: all of it, started again
                if acct == DEMO:
                    self.app.reset_demo_memory()
                else:
                    mem.forget_tool(memory.TOOL)
            else:
                raise ValueError("不支持的操作。")
        except (KeyError, TypeError):
            raise ValueError("不支持的操作。") from None
        except ValueError as ex:
            if str(ex).startswith(("this note", "this question")):
                raise ValueError("这条记忆已经变更，请刷新页面。") from None
            raise
        return {**self.memory_view(source), "message": message}

    def counts(self) -> dict:
        """For the settings row: active notes per kind, and how many guesses wait for the user -- only the kinds
        the 记忆 page shows (other tools add their own, e.g. the input method's term), so the two always agree."""
        c = self.app.mem.counts()
        out = {k: (c.get(k) or {}).get("active", 0) for k in NOTE_KINDS}
        out["proposed"] = len(self.asks(self.app.mem))       # what the memory would like to ask about the mail
        return out
