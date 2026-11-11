"""了解你, as the App runs it: turned on once on the 记忆 page, then idle work like judging old mail and reading
newsletters (the lowest priority of the three), a few minutes at a time, pausable.

Three passes, each with its own rule for what reaches the memory (see learn.py for why):
- sent:     the user's own mail, newest first (the last two years, at most SENT_LIMIT); a fact two messages
            agree on takes effect; on a one-value topic (职位, 单位…) the most recently evidenced value wins;
- received: once a month, organisations that wrote several times over months to someone who opened them;
            what they suggest becomes a guess the user confirms;
Everything drawn is handed to the memory, which grades it (ADR 0015). Handling habits are the
memory's to work out, from the mail-override and mail-acted records (luoke-mem 0.18, ADR 0012 amended)."""
from __future__ import annotations

import json
import sys
import time

from . import config, engines, learn, memory, samples
from .mailbox import MailError, parse_message
from .subscriptions import sender_key

DEMO = "demo"
SLICE_SECONDS = 240
SENT_DAYS, SENT_LIMIT = 730, 400
RECEIVED_EVERY, SENT_CHECK_EVERY = 7 * 86400, 86400    # weekly: NetEase empties its trash in 7 days
RECEIVED_SENDERS = 30
TEXT_CHARS = 800             # of a message's text in a record: the memory reads the original (ADR 0015)
AGREE = 2                    # messages that must agree before a fact from the user's own mail takes effect
MISS_LIMIT = 3               # tries, each after the usual wait, before a mail or organisation with no usable answer is let be


# the evidence of guesses already waiting is looked at again once when this goes up (0.13.15: counts kept;
# 0.13.16: guesses under 其他 about an organisation already known take its sort)
EVIDENCE_V = 2


def labelled(topic: str, text: str) -> str:
    """A fact that reads on its own, wherever it is shown (the memory page, 螺壳's cards): 「姓名：王小明」, not
    「王小明」 (the latter was seen on a card, 2026-10-17). Text that already says what it is stays."""
    label = topic.split(":")[0]
    return text if label == "其他" or "：" in text or ":" in text or label in text else f"{label}：{text}"

class Learner:
    def __init__(self, app):
        self.app = app
        self.store = app.store

    def state(self, acct: str) -> dict:
        """How far it got in one mailbox (what was read, when it last looked)."""
        return self.store.cache_get(acct, "learn")[0] or {}

    def switch(self, acct: str) -> dict:
        """On or off. The memory is the person's, so the switch is too: one for every connected mailbox, those added
        later included (2026-10-14). The demo keeps its own."""
        if acct == DEMO:
            st = self.state(DEMO)
            return {"on": bool(st.get("on")), "enabled_at": st.get("enabled_at")}     # the demo: when tried
        return self.app.cfg["learn"]

    def migrate(self) -> None:
        """0.11 and before: switched on per mailbox. On in any of them is the person's yes. 0.15: on unless the
        user paused it (ADR 0013, 2026-10-19) -- never switched on before is not a no."""
        sw = self.app.cfg["learn"]
        if not sw.get("enabled_at") and not sw.get("on") and not any(self.state(a).get("enabled_at") for a in self.app.mail_accounts()):
            sw.update(on=True, enabled_at=time.time())
            config.save(self.app.cfg)
            return
        if sw.get("enabled_at"):
            return
        states = [self.state(a) for a in self.app.mail_accounts()]
        started = [s["enabled_at"] for s in states if s.get("enabled_at")]
        if started:
            sw.update(on=any(s.get("on") for s in states), enabled_at=min(started))
            config.save(self.app.cfg)

    # ---- the switch -------------------------------------------------------------------------------------------
    def set_on(self, body: dict) -> dict:
        """暂停 / 继续. On by default (ADR 0013): what it decides is only whether the model reads the user's sent mail
        and the organisations' mail to learn about them; what happens to the mail is recorded either way."""
        source = body.get("source")
        acct = self.app.account(source)
        on = bool(body.get("on"))
        if on:
            self.app.curator.need_model()
        if acct == DEMO:
            first = {} if self.state(DEMO).get("enabled_at") or not on else {"enabled_at": time.time()}
            self.store.cache_update(DEMO, "learn", on=on, problem=None, **first)
        else:
            sw = self.app.cfg["learn"]
            sw.update(on=on, enabled_at=sw.get("enabled_at") or (time.time() if on else None))
            config.save(self.app.cfg)
            for a in self.app.mail_accounts():
                self.store.cache_update(a, "learn", problem=None)
        if on:
            self.app.idle_step(prefer="learn")
        else:
            busy = self.app.running_job()
            if busy and busy["kind"] == "learn" and (acct != DEMO or busy.get("account") == DEMO):
                busy["cancel"] = True
        return self.view(acct)

    def intro_seen(self) -> dict:
        self.app.cfg["learn"]["intro_seen"] = True
        config.save(self.app.cfg)
        return {"ok": True}

    def view(self, acct: str) -> dict:
        st = self.state(acct)
        busy = self.app.running_job()
        cands = self.store.learn_candidates(acct)
        sw = self.switch(acct)
        return {"enabled": bool(sw.get("enabled_at") or sw.get("on")), "on": bool(sw.get("on")), "problem": st.get("problem"),
                "running": bool(busy and busy["kind"] == "learn" and busy.get("account") == acct),
                "sent_total": st.get("sent_total") or 0, "sent_done": st.get("sent_done") or 0,
                "stage": self._next(acct, st, ignore_wait=True), "waiting": sum(1 for c in cands if c["state"] == "waiting"),
                "learnt": sum(1 for c in cands if c["state"] == "written")}

    # ---- idle work --------------------------------------------------------------------------------------------
    def _next(self, acct: str, st: dict, ignore_wait: bool = False) -> str | None:
        if not self.switch(acct).get("on"):
            return None
        if not ignore_wait and time.time() - (st.get("failed_at") or 0) < 60:
            return None
        now = time.time()
        if st.get("sent_todo"):
            return "sent"
        if now - (st.get("sent_checked_at") or 0) > SENT_CHECK_EVERY:
            return "sent_check"
        if now - (st.get("received_at") or 0) > RECEIVED_EVERY or (st.get("evidence_v") or 0) < EVIDENCE_V:
            return "received"
        return None

    def next_account(self) -> str | None:
        link = self.app.link() if self.app.has_model() else None
        if not (link and link.ready("learn-sent") and link.ready("learn-orgs")):
            return None
        for acct in self.app.idle_accounts():
            if acct and self._next(acct, self.state(acct)):
                return acct
        return None

    def slice(self, acct: str, job: dict, seconds: float = SLICE_SECONDS) -> dict:
        t0, steps = time.time(), 0
        mb = None
        try:
            while not job["cancel"] and time.time() - t0 < seconds:
                step = self._next(acct, self.state(acct))
                if not step:
                    break
                if acct != DEMO and mb is None:
                    mb = self.app.open_mailbox(acct)
                    mb.open()
                job["stage"] = {"sent": "正在阅读你发出的邮件", "sent_check": "正在查找你发出的邮件",
                                "received": "正在了解你收到的往来邮件"}[step]
                getattr(self, "_" + step)(acct, mb, job)
                self.store.cache_update(acct, "learn", problem=None)         # never "on": that is the user's switch
                steps += 1
        except engines.Busy:
            pass                                          # the hub has more urgent work: come back later
        except (engines.EngineError, MailError) as ex:
            self.store.cache_update(acct, "learn", problem=str(ex), failed_at=time.time())
        finally:
            if mb:
                mb.close()
            self.refill(acct)
        return {"steps": steps}

    # ---- the user's own mail ---------------------------------------------------------------------------------
    def _sent_check(self, acct: str, mb, job: dict) -> None:
        """Which sent mail is new since the last look (the first time: the last two years, newest first)."""
        st = self.state(acct)
        if acct == DEMO:
            uids = sorted((m["uid"] for m in samples.demo_sent()), key=int, reverse=True)
            folder = "demo"
        else:
            folder = mb.folder_for("sent")
            uids = mb.folder_uids(folder, SENT_DAYS)
        last = int(st.get("sent_max_uid") or 0)
        new = [u for u in uids if int(u) > last][:SENT_LIMIT] if last else uids[:SENT_LIMIT]
        self.store.cache_update(acct, "learn", sent_folder=folder, sent_todo=new, sent_checked_at=time.time(),
                                sent_total=(st.get("sent_total") or 0) + len(new),
                                sent_max_uid=max([last] + [int(u) for u in uids[:1]]))

    def _sent(self, acct: str, mb, job: dict) -> None:
        st = self.state(acct)
        todo = st["sent_todo"]
        link = self.app.link("background", job=job)
        batch = todo[:link.batch("learn-sent", st.get("sent_cap") or learn.SENT_BATCH)]
        job.update(done=st.get("sent_done") or 0, total=st.get("sent_total") or 0)
        if acct == DEMO:
            mails = [m for m in samples.demo_sent() if m["uid"] in batch]
            me = samples.ME
        else:
            mails = mb.fetch_messages(batch, st["sent_folder"])
            me = acct
        facts, count = learn.from_sent(mails, me, link) if mails else ([], 0)
        if mails and count < len(mails):          # one request could not carry them all: the rest comes next time
            batch = [u for u in batch if str(u) not in {str(m["uid"]) for m in mails[count:]}]
            mails = mails[:count]
        if facts is None:                         # no usable answer: these mails are not read yet
            if len(mails) > 1:
                self.store.cache_update(acct, "learn", sent_cap=len(mails) // 2)    # fewer at a time
                return
            self._unanswered(acct, "sent", str(mails[0]["uid"]))
            facts = []
        elif st.get("sent_cap"):
            self.store.cache_update(acct, "learn", sent_cap=None)
        touched = set()
        for f in facts:
            ev = [{"ref": mails[n - 1].get("message_id") or f"uid-{mails[n - 1]['uid']}", "subject": mails[n - 1].get("subject") or "（无主题）",
                   "date": (mails[n - 1].get("date") or "")[:16],
                   "text": memory.mask(learn.own_text(mails[n - 1])[:TEXT_CHARS])} for n in f["n"]]   # what the user wrote
            self.store.learn_add(acct, "sent", f["topic"], f["text"], ev)
            touched.add(f["topic"])
        for topic in touched:
            self._settle(acct, topic)
        contacts = set(st.get("contacts") or []) | {a.lower() for m in mails for a in (m.get("to") or []) + (m.get("cc") or [])}
        read = set(batch)
        self.store.cache_update(acct, "learn", sent_todo=[u for u in todo if u not in read], sent_done=(st.get("sent_done") or 0) + len(batch),
                                contacts=sorted(contacts)[:2000])

    def _settle(self, acct: str, topic: str) -> None:
        """Facts from the user's own mail that enough messages agree on take effect. On a one-value topic only the
        value with the most recent evidence does (the mail is read newest first, so an old job title found later
        must not replace the current one)."""
        mem = self.app.memory_for(acct)
        cands = [c for c in self.store.learn_candidates(acct, "sent") if c["topic"] == topic and len(c["evidence"]) >= AGREE]
        if topic.split(":")[0] in learn.SINGLE:
            cands = sorted(cands, key=lambda c: max(e["date"] for e in c["evidence"]))[-1:]
        for c in cands:
            # one statement per message and per thing said in it, at the time it was sent (ADR 0012 amended, agreed
            # with luoke-mem): what the user wrote, never the mail itself; `subject` is the stable key of the thing
            cites = [self._record(mem, acct, "user", "sent-mail", f"{e['ref']}#{topic}", labelled(topic, c["text"]), e["date"],
                                  {"subject": topic, "sender": "self", "by": "user", **({"text": e["text"]} if e.get("text") else {})})
                     for e in c["evidence"]]
            # sensitive context (health, money...) is marked; the memory then waits for the user's yes (ADR 0004)
            nid = mem.conclude("fact", labelled(topic, c["text"]), cites, topic=topic, why=f"{len(c['evidence'])} 封你发出的邮件里提到",
                               sensitive=memory.marks(c["text"]))
            self.store.learn_set(acct, "sent", c["topic"], c["norm"], state="written" if nid else "dropped", note_id=nid)

    @staticmethod
    def _record(mem, acct: str, speaker: str, topic: str, ref: str, text: str, when: str | None, data: dict | None = None) -> int:
        """One record per message (or sender) in each mailbox, shared by every note that rests on it. The same
        sender or Message-ID in another mailbox is another record: each says where it comes from (ADR 0010)."""
        found = [r for r in mem.records(topic=topic, ref=ref, limit=20) if memory.from_account(r, acct)
                 and all((r.get("data") or {}).get(k) == v for k, v in (data or {}).items())]      # same counts
        if found:
            return found[0]["id"]
        return mem.record(speaker, "statement", text, topic=topic, ref=ref, happened_at=(when or "")[:19] or None,
                          sensitive=memory.marks(text), data=memory.sourced(acct, data))

    # ---- what organisations wrote ----------------------------------------------------------------------------
    def _received(self, acct: str, mb, job: dict) -> None:
        if acct == DEMO:
            headers = samples.demo_headers() + samples.demo_archive()
        else:
            headers = mb.everywhere_headers(days=365, limit=3000)        # inbox, archive and trash (ADR 0012)
            archived = self.store.old_rows(acct, "archive")
            if not archived:
                _, archived = mb.old_headers("archive", self.app.cfg["fetch"]["days"])
            headers += archived
            ours = self.store.read_by_us(acct)          # marked read once sorted: not the user opening it
            headers = [{**h, "seen": False} if h.get("seen") and h.get("message_id") in ours else h for h in headers]
        self._refresh_evidence(acct, headers)
        self.store.cache_update(acct, "learn", evidence_at=time.time(), evidence_v=EVIDENCE_V)
        seen = set(self.state(acct).get("received_seen") or [])
        senders = self._senders(acct, headers, seen)        # only organisations not looked at before
        if not self._learn_orgs(acct, senders, seen, job, self.app.link("background", job=job)):
            return                                    # not marked done: the rest is looked at next time
        self.store.cache_update(acct, "learn", received_at=time.time())

    def _learn_orgs(self, acct: str, senders: list[dict], seen: set, job: dict, link) -> bool:
        """Ask about these organisations, a batch at a time; each batch answered is kept (received_seen). False when
        cancelled before the end."""
        i, size = 0, len(senders)
        while i < len(senders):
            if job["cancel"]:
                return False
            found, count = learn.from_received(senders[i:i + size], link)   # 螺壳主控 may say not now: what is done stays done
            batch = senders[i:i + count]
            if found is None:           # no usable answer is not 「nothing to learn」: kept as seen, they were never asked again
                if count > 1:
                    size = count // 2   # a shorter answer may come out whole
                    continue
                self._unanswered(acct, "received", batch[0]["key"])
                found = []
            size, i = len(senders), i + count
            for f in found:
                ev = [{"ref": batch[n - 1]["key"], "subject": batch[n - 1]["subjects"][0], "name": batch[n - 1]["name"],
                       "count": batch[n - 1]["count"], "date": batch[n - 1]["last"],
                       **{k: batch[n - 1].get(k) for k in ("first", "last", "months", "opened")}} for n in f["n"]]
                c = self.store.learn_add(acct, "received", f["topic"], f["text"], ev)
                if c["state"] == "pending":
                    self.store.learn_set(acct, "received", c["topic"], c["norm"], state="waiting")
            # kept after each batch, not at the end: a refusal (power cooldown, the user's own work) used to throw
            # the whole round away, and the same organisations were asked about again and again (2026-10-15)
            seen |= {s["key"] for s in batch}
            self.store.cache_update(acct, "learn", received_seen=sorted(seen)[-5000:])
        return True

    def _unanswered(self, acct: str, kind: str, key: str) -> None:
        """No usable answer about one mail or organisation on its own. The model may be at fault now rather than
        the item: it is asked again after the usual wait (the error), and let be after MISS_LIMIT tries (returns)."""
        missed = dict(self.state(acct).get(f"{kind}_missed") or {})
        missed[key] = missed.get(key, 0) + 1
        if missed[key] >= MISS_LIMIT:
            del missed[key]
        self.store.cache_update(acct, "learn", **{f"{kind}_missed": missed})
        if key in missed:
            raise engines.EngineError("本机模型暂时没有给出可用的结果。")

    # ---- before the trash: learn first, and the clean-up itself as a record (2026-10-16) -----------
    def before_clean(self, acct: str, headers: list[dict], job: dict | None = None) -> int:
        """Whatever goes wrong here, the clean-up the user asked for goes ahead (the review of 2026-10-17)."""
        try:
            return self._before_clean(acct, headers, job)
        except Exception as ex:  # noqa: BLE001 - learning is a side matter of a clean-up
            print(f"luoke-mail: learning before the clean-up skipped: {type(ex).__name__}", file=sys.stderr)
            return 0

    def _before_clean(self, acct: str, headers: list[dict], job: dict | None = None) -> int:
        """先学后清: mail about to go to the trash is looked at first for the organisations it shows -- 了解你's
        received-mail step on just these headers, organisations not looked at before -- since 了解你 never reads
        the trash and the provider empties it. Headers only; nothing is kept but what 了解你 keeps anyway. Only
        while 了解你 is on; when no model can answer now, the clean-up goes ahead (the user asked for it). Returns
        how many organisations were asked about."""
        if not self.switch(acct).get("on") or not headers or not self.app.has_model():
            return 0
        job = job if job is not None else {"cancel": False}
        seen = set(self.state(acct).get("received_seen") or [])
        senders = self._senders(acct, headers, seen)
        if not senders:
            return 0
        link = self.app.link(self.app.watching, battery=True, patient=lambda: job["cancel"], job=job)
        if not link.ready("learn-orgs"):
            return 0
        job["stage"] = "正在记下这些往来机构…"
        try:
            self._learn_orgs(acct, senders, seen, job, link)
        except (engines.EngineError, MailError) as ex:
            print(f"luoke-mail: learning before the clean-up skipped: {type(ex).__name__}", file=sys.stderr)
            return 0
        finally:
            self.refill(acct)
        return len(senders)

    def cleared(self, acct: str, groups: list[dict]) -> int:
        """Called after the mail has moved: a failure here must not turn a done clean-up into an error without
        its undo (the review of 2026-10-17)."""
        try:
            return self._cleared(acct, groups)
        except Exception as ex:  # noqa: BLE001
            print(f"luoke-mail: recording the clean-up skipped: {type(ex).__name__}", file=sys.stderr)
            return 0

    def _cleared(self, acct: str, groups: list[dict]) -> int:
        """清理本身记成信号: one record per sender cleared in bulk to the trash -- the sender and the kind of mail,
        never subjects or text -- for 推测关注的话题 / 不感兴趣 now, and the memory's own inference once 了解你 moves
        there (ADR 0005). Topic `mail-cleared`, agreed with luoke-mem: activity when the user picked it, a receipt
        when they accepted the suggestion. Always (ADR 0013). groups: [{key, name, addr, category, count, by}]."""
        mem = self.app.memory_for(acct)
        n = 0
        for g in groups:
            if not g.get("count"):
                continue
            words = memory.mask(f"清掉了「{g['name']}」的 {g['count']} 封{g['category']}")
            mem.record("user", "activity" if g["by"] == "user" else "receipt", words, topic="mail-cleared", ref=g["key"],
                       happened_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                       data=memory.sourced(acct, {"sender": g["key"], "name": g["name"], "domain": (g.get("addr") or "").rsplit("@", 1)[-1],
                                                  "category": g["category"], "count": g["count"], "action": "trash", "by": g["by"]}),
                       sensitive=memory.marks(words))
            n += 1
        return n

    def _senders(self, acct: str, headers: list[dict], seen: set | None = None) -> list[dict]:
        """Organisations worth asking about: several messages over at least two months, at least one opened, not
        suspicious, not a person the user writes to, not a newsletter or an advertiser."""
        skip = self.store.suspicious_senders(acct) | self._contacts(acct)
        subs = (self.store.cache_get(acct, "subscriptions")[0] or {}).get("senders", [])
        noise = ({sender_key(r) for scope in ("inbox", "archive") for r in self.store.old_rows(acct, scope)
                  if r["kind"] in ("promotion", "newsletter")} | {s["key"] for s in self.store.digest_sources(acct)}
                 | {s["key"] for s in subs if s.get("verdict") in ("unsubscribe", "filter", "never")})   # unread marketing
        groups: dict[str, list[dict]] = {}
        for h in headers:
            if (h.get("from_addr") or "").lower() in skip:
                continue
            groups.setdefault(sender_key(h), []).append(h)
        out = []
        for key, hs in groups.items():
            months = {(h.get("date") or "")[:7] for h in hs if h.get("date")}
            if key in noise or key in (seen or ()) or len(hs) < 3 or len(months) < 2 or not any(h.get("seen") for h in hs):
                continue
            hs.sort(key=lambda h: h.get("date") or "", reverse=True)
            subjects = list(dict.fromkeys((h.get("subject") or "").strip() for h in hs if h.get("subject")))
            out.append({"key": key, "name": hs[0].get("from_name") or hs[0].get("from_addr") or key, "addr": hs[0].get("from_addr") or "",
                        "count": len(hs), "first": min(months), "last": max(months), "months": len(months),
                        "opened": sum(1 for h in hs if h.get("seen")), "subjects": subjects[:6] or ["（无主题）"]})
        out.sort(key=lambda s: -s["count"])
        return out[:RECEIVED_SENDERS]

    def _contacts(self, acct: str) -> set[str]:
        """People the user writes to (from their sent mail): what they send is a conversation, not an organisation's
        records, and what a person is to the user is learnt from the user's own words only."""
        return set(self.state(acct).get("contacts") or [])

    # ---- guesses, a few at a time ----------------------------------------------------------------------------
    def refill(self, acct: str) -> int:
        """Hand what was drawn from organisations' mail to the memory: all of it. The memory grades each on its
        evidence and asks only what is worth asking (ADR 0015), so nothing is held back here to spare the user."""
        mem = self.app.memory_for(acct)
        made = 0
        for c in self.store.learn_candidates(acct, state="waiting"):
            # habits learnt before 0.14 wait no more: the memory works them out from mail-override itself
            nid = self._propose_received(mem, acct, c) if c["source"] == "received" else None
            self.store.learn_set(acct, c["source"], c["topic"], c["norm"], state="written" if nid else "dropped", note_id=nid)
            made += int(bool(nid))
        return made

    # a plain fact of these sorts may be taken down without asking when the evidence is strong (top-level ADR 0011,
    # luoke-mem 0.16): the memory decides, from the cited records' counts; the topic is the mail assistant's to map
    ABOUT = {"账户": "account", "服务": "service", "会员": "membership", "出行": "travel"}
    ORG = "机构:"

    def _propose_received(self, mem, acct: str, c: dict) -> int | None:
        cites = [self._record(mem, acct, "third_party", "received-mail", e["ref"], memory.mask(
            f"{e['name']}发来 {e['count']} 封，例如「{e['subject']}」"), e["date"],
            # how strong the evidence is, for the memory to weigh: how many, over how long, whether the user opened
            # them -- counts only, no text
            {**{k: e[k] for k in ("count", "first", "last", "months", "opened") if e.get(k) is not None},
             "sender": e["ref"], "by": "user"}) for e in c["evidence"]]
        # one organisation is one fact in the memory, whatever sort the model gave it (top-level ADR 0015 amended,
        # the owner 10-22): the memory's topic is 「机构:<name>」 -- the prefix tells it from a one-value topic such
        # as 职位 (agreed with luoke-mem 0.25.0); the sort stays here, for `about`
        kind, _, org = c["topic"].partition(":")
        return mem.propose("fact", c["text"], cites, topic=f"{self.ORG}{org}" if org else c["topic"], sensitive=memory.marks(c["text"]),
                           about=self._about(mem, c["topic"]), why="根据你收到的邮件")

    def _about(self, mem, topic: str) -> str | None:
        """The sort of plain fact a guess is, for the memory (ADR 0011): from its topic; or, filed under 其他, from a
        fact about the same organisation already in effect -- 「shop.example：有购物记录」 next to 「在 shop.example
        有购物账户」 is the same sort of thing, and was being asked about (2026-10-17). The memory's topic
        is 「机构:<name>」 since 0.17.2; older notes are 「sort:organisation」."""
        kind, _, org = topic.partition(":")
        if kind in self.ABOUT or not org:
            return self.ABOUT.get(kind)
        for n in mem.notes(kinds=["fact"], status=["active"], tool=memory.TOOL):
            k, _, o = (n.get("topic") or "").partition(":")
            if o == org and k in self.ABOUT:
                return self.ABOUT[k]
            if n.get("topic") == f"{self.ORG}{org}" and n.get("about") in self.ABOUT.values():
                return n["about"]
        return None

    BUSINESS = ("finance", "notification", "phishing")     # opening it is getting something done: no text kept
    BODIES_PER_RUN = 60

    def _reading(self, acct: str, headers: list[dict], mb=None) -> int:
        """How each newsletter was read, month by month (ADR 0015, agreed with luoke-mem): one record per sender and
        finished month -- how many came, how many the user opened, and the text of the newest opened (`mails`, not
        for business mail) -- wherever the mail is now (inbox, archive, trash). Written once: a month already
        recorded with the same counts (and its text) is not written again; one written before 0.15 without text
        is written once more with it, the same ref."""
        if acct == DEMO:
            return 0
        this_month = time.strftime("%Y-%m")
        groups: dict[tuple[str, str], list[dict]] = {}
        for h in headers:
            month = (h.get("date") or "")[:7]
            if (h.get("bulk") or h.get("list_id")) and month and month < this_month:
                groups.setdefault((sender_key(h), month), []).append(h)
        mem = self.app.memory_for(acct)
        kinds = self._kinds(acct)
        ours = self.store.read_by_us(acct)                  # marked read once sorted: not the user opening it
        budget = [self.BODIES_PER_RUN]                      # bodies fetched per run: a year's backlog over a few runs
        n = 0
        for (key, month), hs in groups.items():
            opened = sum(1 for h in hs if h.get("seen") and h.get("message_id") not in ours)
            ref = f"{key}@{month}"
            seen = sorted((h for h in hs if h.get("seen") and h.get("message_id") not in ours),
                          key=lambda h: h.get("date") or "", reverse=True)
            want_text = bool(mb and seen and kinds.get(key) not in self.BUSINESS)
            if any(memory.from_account(r, acct) and (r.get("data") or {}).get("count") == len(hs)
                   and (r.get("data") or {}).get("opened") == opened and (r["data"].get("mails") or not want_text)
                   for r in mem.records(topic="mail-read", ref=ref, limit=5)):
                continue                                    # written already, with its text when it has any
            mails = []
            if want_text:                                   # the newest opened: the memory reads what they hold (ADR 0015)
                if budget[0] <= 0:
                    self.store.cache_update(acct, "learn", reading_backlog=True)
                    continue                                # the rest next time
                mails = self._texts(mb, seen[:3])
                budget[0] -= len(seen[:3])
            name = hs[0].get("from_name") or hs[0].get("from_addr") or key
            # what was opened, for the memory to tell what the user reads it for (luoke-mem 0.19: topics)
            subjects = list(dict.fromkeys(" ".join((h.get("subject") or "").split())[:40]
                                          for h in sorted(hs, key=lambda h: h.get("date") or "", reverse=True)
                                          if h.get("seen") and h.get("message_id") not in ours and (h.get("subject") or "").strip()))
            subjects = [memory.mask(x) for x in subjects][:3]
            memory.happened(mem, acct, "mail-read", f"订阅「{name}」{month}：收到 {len(hs)} 封，打开 {opened} 封", sender=key,
                            ref=ref, when=max(h.get("date") or "" for h in hs),
                            data={"month": month, "count": len(hs), "opened": opened, "name": name, "category": kinds.get(key),
                                  **({"subjects": subjects} if subjects else {}), **({"mails": mails} if mails else {})})
            n += 1
        if budget[0] > 0:
            self.store.cache_update(acct, "learn", reading_backlog=False)
        return n

    @staticmethod
    def _texts(mb, hs: list[dict]) -> list[dict]:
        out = []
        for h in hs:
            try:
                raw = mb.fetch_raw(h["uid"], h["folder"]) if h.get("uid") and h.get("folder") else None
            except MailError:
                raw = None
            if raw:
                body = " ".join((parse_message(raw).get("body") or "").split())[:TEXT_CHARS]
                out.append({"subject": memory.mask(h.get("subject") or ""), "date": (h.get("date") or "")[:16],
                            "text": memory.mask(body), "text_by": "sender"})    # the sender's words, not the user's
        return out

    def _kinds(self, acct: str) -> dict[str, str]:
        """The kind of mail each sender mostly sends, as sorting judged it (triage category): opening a bill or a
        notification is getting something done, not reading (luoke-mem 0.20)."""
        count: dict[str, dict[str, int]] = {}
        for it in self.store.items(acct, None, None):
            if it.get("category"):
                c = count.setdefault(sender_key(it), {})
                c[it["category"]] = c.get(it["category"], 0) + 1
        return {k: max(c, key=c.get) for k, c in count.items()}

    def _refresh_evidence(self, acct: str, headers: list[dict]) -> int:
        """The organisations' counts as they are now, for every guess drawn from them -- also the ones learnt before
        the counts were kept (0.13.15) -- and a guess still waiting for the user is put to the memory again: with the
        evidence now strong enough, a plain fact takes effect (ADR 0011). Returns how many took effect."""
        groups: dict[str, list[dict]] = {}
        for h in headers:
            groups.setdefault(sender_key(h), []).append(h)
        now: dict[str, dict] = {}
        for key, hs in groups.items():
            months = sorted({(h.get("date") or "")[:7] for h in hs if h.get("date")})
            if months:
                now[key] = {"count": len(hs), "first": months[0], "last": months[-1], "months": len(months),
                            "opened": sum(1 for h in hs if h.get("seen")), "date": months[-1]}
        mem = self.app.memory_for(acct)
        taken = 0
        for c in self.store.learn_candidates(acct, "received"):
            ev = [{**e, **now[e["ref"]]} if e["ref"] in now else e for e in c["evidence"]]
            if ev != c["evidence"]:
                self.store.learn_set(acct, "received", c["topic"], c["norm"], evidence_json=json.dumps(ev, ensure_ascii=False))
            note = mem.note(c["note_id"]) if c.get("note_id") else None
            # still waiting, or kept aside as observed (luoke-mem 0.16.1): with more evidence it may take effect now
            if c["state"] == "written" and note and note.get("status") in ("proposed", "observed") and self._about(mem, c["topic"]):
                nid = self._propose_received(mem, acct, {**c, "evidence": ev})
                taken += int(bool(nid) and (mem.note(nid) or {}).get("status") == "active")
        return taken
