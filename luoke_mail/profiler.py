"""The user's own things, kept in their memory as records: their websites' posts, the labels they made, the
threads they started. luoke-mem works out 关注的话题, 不感兴趣 and 精选的口味 from these and the mail
assistant's other records (luoke-mem 0.19, top-level ADR 0012 amended: only the memory concludes); this part
used to ask the model itself (task `interests`) and does not any more.

Idle work, no model: it runs when the user adds a website and once a week (also writing how each newsletter
was read, month by month). Each thing is one record, written once (the same label or sender in two mailboxes is two, ADR 0010)."""
from __future__ import annotations

import re
import time

from . import memory, profile, samples
from .mailbox import NOREPLY, SPECIAL_NAMES, SPECIAL_USE, MailError

DEMO = "demo"
WEEK = 7 * 86400
SITE_EVERY = 30 * 86400
RETRY = 300
SITE_POSTS, LABELS, SENT_SUBJECTS = 30, 40, 60
RECORDS_V = 3                    # 2: labels and site posts dated by when they happened (2026-10-19); 3: mail-contact
SENT_DAYS, SENT_LIMIT = 730, 2000   # the user's own Sent: subjects (the newest SENT_SUBJECTS) and who was written to
REPLY = re.compile(r"^\s*(re|fw|fwd|回复|答复|转发)\s*[:：]", re.I)
AUTO_REPLY = re.compile(r"^\s*(自动回复|自动答复|auto(matic)?[- ]?reply|out of (the )?office|外出|休假)", re.I)
CONTACT_SUBJECTS = 5               # a mail-contact record's subjects: the user's own, newest first


def _shared(acct: str) -> str:
    """The user's websites are the person's, not one mailbox's: what was read from them is kept once."""
    return acct if acct == DEMO else "*"


class Profiler:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.fetch = profile.fetch_site          # tests read a fake site

    def state(self, acct: str) -> dict:
        return self.store.cache_get(acct, "profile")[0] or {}

    # ---- the user's own websites (kept in their memory: other luoke tools may use them too) ------------------
    def sites(self, acct: str) -> list[dict]:
        cache = self.store.cache_get(_shared(acct), "sites")[0] or {}
        out = []
        for n in self.app.memory_for(acct).notes(kinds=["fact"]):
            m = re.search(r"https?://\S+", n["text"]) if n.get("topic") == "site" else None
            if m:
                info = cache.get(m.group(0)) or {}
                out.append({"id": n["id"], "url": m.group(0), "title": info.get("title") or "", "posts": len(info.get("posts") or []),
                            "problem": info.get("problem")})
        return out

    def change_site(self, body: dict) -> dict:
        """Add a site (read at once, so the user knows whether it could be read) or remove one."""
        source = body.get("source")
        acct = self.app.account(source)
        mem = self.app.memory_for(acct)
        message = ""
        if body.get("op") == "add":
            url = " ".join(str(body.get("url") or "").split())
            if url and "://" not in url:
                url = "https://" + url
            if url.rstrip("/") in {s["url"].rstrip("/") for s in self.sites(acct)}:
                raise ValueError("这个网址已经添加过了。")
            try:
                info = self.fetch(url)
            except ValueError:
                raise
            except Exception as ex:  # noqa: BLE001 - unreachable, refused, not found: said in plain words
                print(f"luoke-mail: reading a site failed: {type(ex).__name__}", flush=True)
                raise ValueError("无法打开这个网址，请检查后重试。") from None
            mem.state("fact", f"我的网站：{url}", topic="site", record_text=f"填写了自己的网站：{url}")
            self._keep_site(acct, url, info)
            name = f"「{info['title']}」" if info.get("title") else "网站"
            message = (f"已读取{name}，找到 {len(info['posts'])} 篇文章。" if info["posts"]
                       else f"已添加{name}，但没有找到文章列表。")
            self.app.idle_step(prefer="profile")
        elif body.get("op") == "remove":
            site = next((s for s in self.sites(acct) if s["id"] == int(body.get("id") or 0)), None)
            if not site:
                raise ValueError("这个网址已经删除了，请刷新页面。")
            mem.forget(site["id"])
            cache = self.store.cache_get(_shared(acct), "sites")[0] or {}
            cache.pop(site["url"], None)
            self.store.cache_set(_shared(acct), "sites", cache)
            self.store.cache_update(acct, "profile", sites=sorted(s["url"] for s in self.sites(acct)))
        else:
            raise ValueError("不支持的操作。")
        return {**self.app.curator.memory_view(source), "message": message}

    def _keep_site(self, acct: str, url: str, info: dict | None, problem: str | None = None) -> None:
        cache = self.store.cache_get(_shared(acct), "sites")[0] or {}
        old = cache.get(url) or {}
        cache[url] = {**old, **(info or {}), "fetched_at": time.time(), "problem": problem}
        self.store.cache_set(_shared(acct), "sites", cache)

    # ---- when ---------------------------------------------------------------------------------------------
    def due(self, acct: str, ignore_wait: bool = False) -> str | None:
        st = self.state(acct)
        if not ignore_wait and time.time() - (st.get("failed_at") or 0) < RETRY:
            return None
        urls = sorted(s["url"] for s in self.sites(acct))
        if acct == DEMO and not (urls or self.app.learner.switch(DEMO).get("on")):
            return None                                     # the demo: only while someone tries it
        if not st.get("at"):
            return "first"
        if self.app.learner.state(acct).get("reading_backlog") and time.time() - st["at"] > 3600:
            return "backlog"                                # last year's reading, its text a few dozen at a time
        if (st.get("records_v") or 1) < RECORDS_V:
            return "again"                              # written the old way: once more, the new way
        if set(urls) - set(st.get("sites") or []):
            return "sites"
        if time.time() - st["at"] > WEEK:
            return "weekly"                                 # how each newsletter was read last month, new labels
        return None

    def next_account(self) -> str | None:
        for acct in self.app.idle_accounts():
            if acct and self.due(acct):                      # mail_accounts: only mailboxes with a password
                return acct
        return None

    def view(self, acct: str) -> dict:
        busy = self.app.running_job()
        return {"running": bool(busy and busy["kind"] == "profile" and busy.get("account") == acct),
                "sites": len(self.sites(acct))}

    # ---- a run --------------------------------------------------------------------------------------------
    def run(self, acct: str, job: dict) -> dict:
        mem = self.app.memory_for(acct)
        urls = sorted(s["url"] for s in self.sites(acct))
        try:
            job["stage"] = "正在整理你的资料"
            self._refresh_sites(acct, job)
            evidence = self._evidence(acct)
        except MailError as ex:
            self.store.cache_update(acct, "profile", problem=str(ex), failed_at=time.time())
            return {"made": 0}
        made = 0
        for ev in evidence:
            if job["cancel"]:
                return {"made": made}
            made += int(self._keep(mem, ev))
        self.store.cache_update(acct, "profile", at=time.time(), at_iso=time.strftime("%Y-%m-%d %H:%M:%S"), sites=urls,
                                asked=False, problem=None, failed_at=0, made=made, records_v=RECORDS_V)
        return {"made": made}

    def _refresh_sites(self, acct: str, job: dict) -> None:
        cache = self.store.cache_get(_shared(acct), "sites")[0] or {}
        for s in self.sites(acct):
            if job["cancel"] or time.time() - ((cache.get(s["url"]) or {}).get("fetched_at") or 0) < SITE_EVERY:
                continue
            try:
                self._keep_site(acct, s["url"], self.fetch(s["url"]))
            except Exception as ex:  # noqa: BLE001 - kept from last time; the page says it could not be read
                print(f"luoke-mail: reading a site failed: {type(ex).__name__}", flush=True)
                self._keep_site(acct, s["url"], None, problem="无法打开")

    # ---- the records: the user's own things only -------------------------------------------------------------
    def _evidence(self, acct: str) -> list[dict]:
        """[{"topic", "ref", "record" (its words), "when", "account" (none for the user's website), "data"}]. How
        newsletters were read is kept month by month by 了解你 (`mail-read`); 精选's feedback and the clean-ups are
        records already."""
        out: list[dict] = []
        cache = self.store.cache_get(_shared(acct), "sites")[0] or {}
        for s in self.sites(acct):
            info = cache.get(s["url"]) or {}
            title = info.get("title") or s["url"]
            if info.get("about"):
                out.append({"topic": "own-site", "ref": s["url"], "record": f"你的网站「{title}」：{info['about']}"})
            for p in (info.get("posts") or [])[:SITE_POSTS]:
                out.append({"topic": "own-site", "ref": p.get("url") or s["url"], "when": p.get("date"),
                            "record": f"你的网站上的文章：「{p['title']}」" + (f"｜{p['summary']}" if p.get("summary") else "")})
        labels, sent = self._mailbox(acct)
        for name, count, examples, first, last in labels[:LABELS]:
            # when it was last in use -- the newest mail under it -- not when this was written: a label last used ten
            # years ago is not what the user cares about now (2026-10-19). A label seen only in the folder
            # list says nothing of when: not written
            if not last:
                continue
            out.append({"topic": "own-label", "ref": name, "account": acct, "when": last,
                        "data": {"name": name, "count": count, "first": first[:10], "last": last[:10]},
                        "record": f"标签「{name}」" + (f"：{'、'.join(examples)}" if examples else "")})
        for h in sent[:SENT_SUBJECTS]:
            out.append({"topic": "sent-mail", "ref": h.get("message_id") or f"uid-{h.get('uid')}", "when": h.get("date"),
                        "account": acct, "data": {"subject": h["subject"], "sender": "self"},
                        "record": f"你发起的邮件：「{h['subject']}」"})
        return out

    def _mailbox(self, acct: str) -> tuple[list[tuple[str, int, list[str], str, str]], list[dict]]:
        """(the user's labels with counts and examples, subjects the user wrote); on the way, how each newsletter
        was read month by month (`mail-read`, Learner._reading: no model, so not waiting for 了解你's turn) and who
        the user wrote to (`mail-contact`, `_contacts`). Read-only."""
        rows = self.store.old_rows(acct, "archive") + self.store.old_rows(acct, "inbox")
        if acct == DEMO:
            rows = rows or samples.demo_archive()
            sent = [{**m, "message_id": m.get("message_id") or f"uid-{m['uid']}"} for m in samples.demo_sent()]
            return self._labels(rows, []), self._own_subjects(sent)
        with self.app.open_mailbox(acct) as mb:
            self.app.learner._reading(acct, mb.everywhere_headers(days=365, limit=3000), mb)
            folders = [] if any(r.get("labels") for r in rows) else mb.folders()
            try:
                sent = mb.fetch_headers(days=SENT_DAYS, limit=SENT_LIMIT, folder=mb.folder_for("sent"), recipients=True)
            except MailError:
                sent = []
        self._contacts(acct, sent)
        return self._labels(rows, folders), self._own_subjects(sent)

    def _contacts(self, acct: str, sent: list[dict]) -> int:
        """Who the user writes to, month by month (`mail-contact`, agreed with luoke-mem 2026-11-09: people's names
        for the voice input's hot words). One record per address the user wrote to (To or Cc) and finished month:
        the display name last written for it, how many as To and as Cc, and up to CONTACT_SUBJECTS subjects the user
        wrote to them that month, newest first (`subjects`, for the memory's 常谈的话题: not a reply's or a forward's,
        which are someone else's words, nor an auto-reply's; masked). A month whose counts changed is written
        again under the same ref (the memory reads the newest). What was written is kept here (cache `contacts`), so
        a person the user told the memory to forget -- their records deleted there -- is not written back from the
        same old months; only mail the user sends them afterwards brings them back. The user's own addresses,
        no-reply addresses and whoever wrote to the user are not kept. No model, no mailbox change. How many written."""
        if acct == DEMO or not sent:
            return 0
        own = {a.lower() for a in self.app.mail_accounts()} | {acct} | {(h.get("from_addr") or "").lower() for h in sent}
        this_month, groups = time.strftime("%Y-%m"), {}
        for h in sorted(sent, key=lambda h: h.get("date") or ""):
            month = (h.get("date") or "")[:7]
            if not month or month >= this_month:
                continue
            for r in h.get("recipients") or []:
                addr = r["addr"]
                if addr in own or NOREPLY.search(addr.split("@")[0] + "@"):
                    continue
                g = groups.setdefault((addr, month), {"to": 0, "cc": 0, "name": "", "when": "", "subjects": []})
                g[r["as"]] += 1
                subject = " ".join((h.get("subject") or "").split())[:40]
                if subject and subject != "（无主题）" and not REPLY.match(subject) and not AUTO_REPLY.match(subject):
                    g["subjects"] = [subject] + [x for x in g["subjects"] if x != subject]
                g["name"] = r["name"] or g["name"]
                g["when"] = h["date"]
        mem, made = self.app.memory_for(acct), 0
        written = self.store.cache_get(acct, "contacts")[0] or {}
        for (addr, month), g in sorted(groups.items()):
            ref, count = f"{addr}@{month}", g["to"] + g["cc"]
            if written.get(ref) == [g["to"], g["cc"]]:
                continue                                    # written already (and maybe forgotten since): not again
            name = memory.mask(g["name"])
            memory.happened(mem, acct, "mail-contact", f"写信给 {f'{name} <{addr}>' if name else addr}：{month} 共 {count} 封",
                            sender=addr, ref=ref, when=g["when"],
                            data={"name": name, "month": month, "count": count, "to": g["to"], "cc": g["cc"],
                                  **({"subjects": [memory.mask(x) for x in g["subjects"][:CONTACT_SUBJECTS]]} if g["subjects"] else {})})
            written[ref] = [g["to"], g["cc"]]
            made += 1
        if made:
            self.store.cache_set(acct, "contacts", written)
        return made

    @staticmethod
    def _labels(rows: list[dict], folders: list[dict]) -> list[tuple[str, int, list[str], str, str]]:
        """Labels the user made: (name, how many, two subjects, oldest and newest mail's date) from the mail already
        read in 清理, else the folder list (no count, no dates)."""
        found: dict[str, list[dict]] = {}
        for r in rows:
            for name in r.get("labels") or []:
                found.setdefault(name, []).append(r)
        if found:
            out = []
            for name, rs in sorted(found.items(), key=lambda x: -len(x[1])):
                rs.sort(key=lambda r: r.get("date") or "", reverse=True)
                examples = list(dict.fromkeys((r.get("subject") or "").strip()[:30] for r in rs if r.get("subject")))[:2]
                dates = [r["date"] for r in rs if r.get("date")]
                out.append((name, len(rs), examples, min(dates) if dates else "", max(dates) if dates else ""))
            return out
        special = {n for names in SPECIAL_NAMES.values() for n in names} | {"inbox", "outbox", "notes", "收件箱"}
        out = []
        for f in folders:
            leaf = f["name"].split(f["delim"])[-1] if f["delim"] else f["name"]
            if (any(flag in f["flags"] for flags in SPECIAL_USE.values() for flag in flags) or "\\noselect" in f["flags"]
                    or f["name"].startswith("[Gmail]") or leaf.strip().lower() in special):
                continue
            out.append((f["name"], 0, [], "", ""))
        return out

    @staticmethod
    def _own_subjects(sent: list[dict]) -> list[dict]:
        """New threads the user started (a reply's subject is the other person's words), newest first, once each."""
        out, seen = [], set()
        for h in sorted(sent, key=lambda h: h.get("date") or "", reverse=True):
            subject = " ".join((h.get("subject") or "").split())
            if subject and not REPLY.match(subject) and subject.lower() not in seen:
                seen.add(subject.lower())
                out.append({**h, "subject": subject[:60]})
        return out

    # ---- written once ---------------------------------------------------------------------------------------
    @staticmethod
    def _keep(mem, ev: dict) -> bool:
        """Write the record unless it is kept already (from the same mailbox: ADR 0010). True when written."""
        when = (ev.get("when") or "")[:19] or None
        for r in mem.records(topic=ev["topic"], ref=ev["ref"], limit=20):
            if r["speaker"] == "user" and memory.from_account(r, ev.get("account")):
                # kept before with the wrong time (when it was written, 0.14.0 and before): put right where the
                # memory lets a tool correct its own record's time
                redate = getattr(mem, "redate", None)
                if when and redate and (r.get("happened_at") or "")[:10] != when[:10]:
                    redate(r["id"], when)
                return False
        words = memory.mask(ev["record"])
        data = {**(ev.get("data") or {}), "by": "user"}
        data = {k: v for k, v in data.items() if v is not None}
        mem.record("user", "statement", words, topic=ev["topic"], ref=ev["ref"], happened_at=when,
                   sensitive=memory.marks(words), data=memory.sourced(ev["account"], data) if ev.get("account") else data)
        return True
