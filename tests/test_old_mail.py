"""The whole inbox counts: recent mail is sorted one by one (only unseen bodies are downloaded), older mail
is cleared in bulk by source -- headers only, in batches, every batch checked, undone in batches."""
import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import actions, config, server  # noqa: E402
from luoke_mail.credstore import FileStore  # noqa: E402
from luoke_mail.mailbox import Mailbox  # noqa: E402
from luoke_mail.store import Store  # noqa: E402
from fakes import FakeChat, FakeIMAP, suggestion  # noqa: E402
from test_background import FakeDesktop, no_evening_note, private_config, triage_reply  # noqa: E402
from test_core import make_mail  # noqa: E402

ME = "me@example.com"


def mail(n, subject, sender, days_ago=0, list_id=None):
    raw = make_mail(subject, f"正文 {n}", sender=sender, days_ago=days_ago)
    raw = raw.replace(b"<abc123@corp.example>", f"<o{n}@x.example>".encode())
    if list_id:
        raw = raw.replace(b"Subject:", f"List-Id: <{list_id}>\r\nSubject:".encode(), 1)
    return raw


MAILS = [  # (raw, seen) -- two recent, the rest older than the 30-day window
    (mail(1, "本周例会改到周四", "王总 <boss@corp.example>", 2), False),
    (mail(2, "双十一预热", "某某商城 <news@shop.example>", 3, "promo.shop.example"), False),
    (mail(3, "旧推送一", "某某商城 <news@shop.example>", 45, "promo.shop.example"), False),
    (mail(4, "旧推送二", "某某商城 <news@shop.example>", 80, "promo.shop.example"), True),
    (mail(5, "旧推送三", "某某商城 <news@shop.example>", 400, "promo.shop.example"), False),
    (mail(6, "去年的聚餐", "张伟 <zhangwei@mail.example>", 300), True),
    (mail(7, "合同扫描件（留着）", "李律师 <lawyer@firm.example>", 200), True),
]


def wait(app, job):
    for _ in range(400):
        if app.jobs[job["id"]]["status"] != "running":
            return app.jobs[job["id"]]
        time.sleep(0.02)
    raise AssertionError("job did not finish")


class Rig:
    def __init__(self, test, flavour="gmail", provider="gmail", **fake):
        private_config(test)
        home = tempfile.mkdtemp()
        self.srv = FakeIMAP(MAILS, flavour=flavour, **fake).__enter__()
        test.addCleanup(self.srv.__exit__)
        for m in self.srv.folders["INBOX"]:
            if b"lawyer@" in m["raw"]:
                m["flags"].add("\\Flagged")                          # starred: never part of a bulk clean-up
        self.app = a = server.App(secret_store=FileStore(os.path.join(home, "c.json")),
                                  state_store=Store(os.path.join(home, "s.db")), desktop_=FakeDesktop())
        test.addCleanup(a.store.close)
        a.cfg["accounts"] = [{"provider": provider, "address": ME, "host": None, "port": 993}]
        a.secrets.set(server.MAIL_SECRET, ME, "secret")
        a.open_mailbox = lambda *_: Mailbox(provider, ME, "secret", host="127.0.0.1", port=self.srv.port, ssl=False, timeout=5)
        a.cfg["engine"] = {"base_url": "http://127.0.0.1:9/v1", "model": "test-model", "engine_name": None, "has_api_key": False}
        a.cfg["mode"]["write"] = True

    def inbox(self):
        return sorted(self.srv.subjects("INBOX"))


class SyncWindowTests(unittest.TestCase):
    def test_mail_archived_on_the_phone_is_sorted_and_what_needs_the_user_is_shown(self):
        """The owner archived a clinic booking on the phone before a sync saw it in the inbox, and was never told to
        put it in the calendar (2026-10-15). Mail archived in the last 7 days is sorted too; only what needs the user
        is shown, apart from the inbox list; nothing is moved back."""
        r = Rig(self)
        allmail = "[Gmail]/All Mail"
        r.srv._add(allmail, mail(60, "项目周会邀请", "项目组 <team@corp.example>", 1), {"\\Seen"})        # schedule
        r.srv._add(allmail, mail(61, "双十一促销", "某某商城 <news@shop.example>", 2, "promo.shop.example"), {"\\Seen"})
        r.srv._add(allmail, mail(62, "上个月的项目周会", "项目组 <team@corp.example>", 10), {"\\Seen"})    # too old
        with FakeChat(triage_reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            r.app.sync_mailbox(ME, {"cancel": False})
            asked = len(chat.requests)
            view = r.app.list_items("mailbox")
            self.assertEqual([it["subject"] for it in view["archived"]], ["项目周会邀请"])
            self.assertNotIn("项目周会邀请", [e["item"]["subject"] for e in view["entries"] if e["type"] == "item"])
            self.assertFalse(any("上个月的项目周会" in b["messages"][1]["content"] for b in chat.requests))
            self.assertIn("项目周会邀请", r.srv.subjects(allmail))                  # still archived: nothing moved
            self.assertNotIn("项目周会邀请", r.inbox())
            r.app.sync_mailbox(ME, {"cancel": False})                               # seen before: not asked again
            self.assertEqual(len(chat.requests), asked)
            it = view["archived"][0]
            r.app.cfg["mode"]["write"] = False                                      # read-only: 知道了 still works
            r.app.actions("mailbox", write=False).execute([it["id"]], "skip")       # (it changes nothing in the mailbox)
            r.app.cfg["mode"]["write"] = True
            self.assertEqual(r.app.list_items("mailbox")["archived"], [])
            r.srv._add("INBOX", mail(60, "项目周会邀请", "项目组 <team@corp.example>", 1), set())   # moved back
            r.app.sync_mailbox(ME, {"cancel": False})
            view = r.app.list_items("mailbox")
            self.assertIn("项目周会邀请", [e["item"]["subject"] for e in view["entries"] if e["type"] == "item"] + [it["subject"] for it in view["due"]])

    def test_gmails_verdict_on_the_sender_reaches_the_model(self):
        r = Rig(self)
        verdict = (b"Authentication-Results: mx.google.com; dkim=pass header.i=@welcome.bank.example; "
                   b"dmarc=pass (p=REJECT) header.from=welcome.bank.example\r\n")
        r.srv._add("INBOX", verdict + mail(90, "Your decision is ready", "Card Services <accounts@welcome.bank.example>", 0), set())
        with FakeChat(triage_reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            r.app.sync_mailbox(ME, {"cancel": False})
        it = next(i for i in r.app.store.items(ME, "pending") if i["subject"] == "Your decision is ready")
        self.assertEqual(it["auth"], "pass")
        asked = next(b["messages"][1]["content"] for b in chat.requests if "Your decision is ready" in b["messages"][1]["content"])
        self.assertIn("这封邮件确实来自 welcome.bank.example", asked)

    def test_mail_sorted_before_the_verdict_was_kept_is_checked_and_sorted_again(self):
        """2026-10-16: a card issuer's notice sorted by 0.12 kept its phishing flag after the update."""
        r = Rig(self)
        verdict = (b"Authentication-Results: mx.google.com; dkim=pass header.i=@welcome.bank.example; "
                   b"dmarc=pass (p=REJECT) header.from=welcome.bank.example\r\n")
        r.srv._add("INBOX", verdict + mail(90, "Your decision is ready", "Card Services <accounts@welcome.bank.example>", 0), set())
        with FakeChat(triage_reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            r.app.sync_mailbox(ME, {"cancel": False})
        card = next(i for i in r.app.store.items(ME, "pending") if i["subject"] == "Your decision is ready")
        with r.app.store.lock:                                     # as 0.12 left them: no verdict, one flagged
            r.app.store.db.execute("UPDATE items SET auth=NULL WHERE account=?", (ME,))
            r.app.store.db.execute("UPDATE items SET suspicious=1 WHERE id=?", (card["id"],))
            r.app.store.db.commit()
        with FakeChat(triage_reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            r.app.sync_mailbox(ME, {"cancel": False})
            r.app.sync_mailbox(ME, {"cancel": False})              # once is enough
        asked = [b["messages"][1]["content"] for b in chat.requests]
        self.assertEqual(len(asked), 1)
        self.assertIn("这封邮件确实来自 welcome.bank.example", asked[0])
        items = {i["subject"]: i for i in r.app.store.items(ME, "pending")}
        self.assertEqual(items["Your decision is ready"]["auth"], "pass")
        self.assertFalse(items["Your decision is ready"]["suspicious"])
        self.assertEqual(items["本周例会改到周四"]["auth"], "none")      # no verdict: kept as it was, not asked again

    def test_recent_mail_is_sorted_older_mail_is_counted(self):
        r = Rig(self)
        with FakeChat(triage_reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            s = r.app.sync_mailbox(ME, {"cancel": False})
            self.assertEqual((s["new"], s["in_range"], s["old"]), (2, 2, 5))   # read or not, the last 30 days
            self.assertEqual(r.app.list_items("mailbox")["old"], {"count": 5, "days": 30})
            fetches = r.srv.commands.count("UID FETCH")
            s = r.app.sync_mailbox(ME, {"cancel": False})
        self.assertEqual(s["new"], 0)
        self.assertLessEqual(r.srv.commands.count("UID FETCH") - fetches, 1)  # no body again (one more: the labels)

    def test_config_from_before_moves_to_the_whole_inbox(self):
        private_config(self)
        with open(config.CONFIG_PATH, "w") as f:
            json.dump({"fetch": {"limit": 200, "unseen_only": True, "days": 7}}, f)
        self.assertEqual(config.load()["fetch"], {"limit": 200, "days": 30})
        with open(config.CONFIG_PATH, "w") as f:
            json.dump({"fetch": {"limit": 50, "unseen_only": True, "days": 14}}, f)
        self.assertEqual(config.load()["fetch"], {"limit": 50, "days": 14})   # a choice the user made stays
        config.save(config.load())
        with open(config.CONFIG_PATH) as f:
            self.assertEqual(json.load(f)["version"], config.VERSION)


class MalformedHeaderTests(unittest.TestCase):
    """luoke-mail#29: one message whose display name decodes to a line break failed every sync and every 清理 scan
    (and everything after it was never read). It is read leniently now, on one line."""
    BAD = (b"From: =?UTF-8?B?5LyY5oOg5Lit5b+DCuWuouacjQ==?= <shop@promo.example>\r\n"
           b"To: =?UTF-8?B?5LyY5oOg5Lit5b+DCuWuouacjQ==?= <me@example.com>\r\n"
           b"Subject: =?UTF-8?B?5LyY5oOg?=\r\nMessage-ID: <bad@promo.example>\r\nDate: %s\r\n\r\nbody\r\n")

    def check(self, flavour, provider):
        from email.utils import formatdate
        r = Rig(self, flavour, provider)
        for i in range(12):
            r.srv._add("INBOX", mail(800 + i, f"正常 {i}", "同事 <c@corp.example>", 1), set())
        r.srv._add("INBOX", self.BAD % formatdate(time.time() - 86400).encode(), set())       # among the new mail
        r.srv._add("INBOX", self.BAD.replace(b"bad@", b"old@") % formatdate(time.time() - 200 * 86400).encode(), set())
        res = r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0}, analyze=False)
        self.assertIsNone(res.get("cut"))
        names = {it["from_name"] for it in r.app.store.items(ME)}
        self.assertIn("优惠中心 客服", names)                                       # read, on one line
        self.assertEqual(len(r.app.store.items(ME)), 15)                            # 2 + 12 + the bad one
        job = wait(r.app, r.app.start_old_scan({"source": ME, "scope": "inbox"}))
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertIn("old@promo.example", {x["message_id"].strip("<>") for x in r.app.store.old_rows(ME, "inbox")})
        job = wait(r.app, r.app.start_subscriptions({"source": ME, "days": 365}))
        self.assertEqual(job["status"], "done", job.get("error"))

    def test_gmail(self):
        self.check("gmail", "gmail")

    def test_qq(self):
        self.check("qq", "qq")

    def test_163(self):
        self.check("netease", "163")


class PersonalOldMailTests(unittest.TestCase):
    """luoke-mail#20: mail with a person is never ticked for the trash, whatever its headers were judged to be."""

    def test_people_and_replies_are_archived_not_trashed(self):
        r = Rig(self)
        r.srv._add("INBOX", mail(700, "Re: 上季度预算的几处疑问", "王磊 <wanglei@corp.example>", 90), {"\\Seen"})
        wait(r.app, r.app.start_old_scan({"source": ME, "scope": "inbox"}))
        rows = r.app.store.old_rows(ME, "inbox")
        r.app.store.old_judge(ME, {x["message_id"]: "notification" for x in rows}, "test")     # all called one-off notices
        groups = {g["name"]: g for g in r.app.old_view(ME, "inbox")["groups"]}
        for name in ("王磊", "张伟"):                                            # a reply, and a person's own mail
            self.assertEqual(groups[name]["action"], "archive", name)
            self.assertEqual(groups[name]["shown"], "personal", name)        # listed as 和人的往来, not 一次性通知 (#20)
        self.assertIsNone(groups["某某商城"]["shown"])
        self.assertEqual((groups["某某商城"]["action"], groups["某某商城"]["suggested"]), ("trash", True))   # a mailing list
        self.assertEqual(r.app.old_action("archive", "notification", personal=True), ("trash", False))


class StalledReadingTests(unittest.TestCase):
    """luoke-mail#19: new mail is read ten at a time and kept as it comes; a dropped connection is opened once more,
    and if it drops again, what was read stays and the next sync goes on from there."""

    def rig(self):
        r = Rig(self)
        for i in range(23):
            r.srv._add("INBOX", mail(600 + i, f"新邮件 {i}", "同事 <c@corp.example>", 1), set())
        return r

    def job(self):
        return {"cancel": False, "stage": "", "done": 0, "total": 0}

    def test_one_drop_is_read_through(self):
        r = self.rig()
        r.srv.drop_body_at = {15}
        job = self.job()
        res = r.app.sync_mailbox(ME, job, analyze=False)
        self.assertEqual((res["new"], res.get("cut")), (25, None))
        self.assertEqual(len(r.app.store.items(ME)), 25)

    def test_two_drops_keep_what_was_read_and_the_next_sync_goes_on(self):
        r = self.rig()
        r.srv.drop_body_at = {15, 16}
        res = r.app.sync_mailbox(ME, self.job(), analyze=False)
        self.assertEqual(res["new"], 10)                                 # the first ten, kept
        self.assertIn("10 / 25", res["cut"])
        self.assertEqual(len(r.app.store.items(ME)), 10)
        res = r.app.sync_mailbox(ME, self.job(), analyze=False)          # the connection is fine again
        self.assertEqual((res["new"], res.get("cut")), (15, None))
        self.assertEqual(len(r.app.store.items(ME)), 25)

    def test_nothing_read_is_the_mailboxs_error(self):
        r = self.rig()
        r.srv.drop_body_at = {1, 2}
        with self.assertRaises(server.MailError):
            r.app.sync_mailbox(ME, self.job(), analyze=False)

    def test_reading_shows_how_far_it_got(self):
        r = self.rig()
        seen = []
        job = self.job()
        real = r.app.store.upsert_message
        r.app.store.upsert_message = lambda acct, m: (seen.append((job["done"], job["total"])), real(acct, m))[1]
        r.app.sync_mailbox(ME, job, analyze=False)
        self.assertIn((10, 25), seen)                                     # not 0 / 0 the whole time


class OldMailTests(unittest.TestCase):
    """清理 → 收件箱里的旧邮件 (scope "inbox")."""

    def scan(self, r, source="mailbox"):
        job = wait(r.app, r.app.start_old_scan({"source": source, "scope": "inbox"}))
        self.assertEqual(job["status"], "done", job)
        return job["result"]

    def apply(self, r, keys, source="mailbox"):
        job = wait(r.app, r.app.start_old_apply({"source": source, "scope": "inbox", "keys": keys}))
        self.assertEqual(job["status"], "done", job)
        return job["result"]

    @staticmethod
    def keys(v):
        return [g["key"] for g in v["groups"] if g["suggested"]]

    @staticmethod
    def every(v):
        """All groups, as a user who ticks the unjudged ones too (they are not ticked for them, luoke-mail#32)."""
        return [g["key"] for g in v["groups"]]

    def test_scan_groups_by_source_and_keeps_starred_out(self):
        r = Rig(self)
        v = self.scan(r)
        self.assertEqual((v["total"], v["flagged"], v["judged"]), (4, 1, 0))
        got = {g["key"]: (g["count"], g["unread"], g["action"], g["suggested"]) for g in v["groups"]}
        # not judged yet: archived if ticked, not ticked for the user (10-23, luoke-mail#32)
        self.assertEqual(got, {"pending|list:promo.shop.example": (3, 2, "archive", False),
                               "pending|person:zhangwei@mail.example": (1, 0, "archive", False)})
        self.assertEqual(next(g for g in v["groups"] if g["bulk"])["subjects"][0], "旧推送一")    # the latest first
        self.assertNotIn("FETCH BODY", " ".join(r.srv.commands))                               # headers only

    def test_a_scan_older_than_the_inbox_count_is_read_again(self):
        """The owner, 10-23: the inbox bar said one message was over 30 days old (counted at the sync), 清理 said
        none (its scan was from 10-06). The view says when the two disagree, and the page reads again."""
        r = Rig(self)
        v = self.scan(r)
        self.assertFalse(v["stale"])                                     # a fresh scan agrees with the count
        r.app.store.cache_set(ME, "old_count", {"count": v["total"] + v["flagged"] + 1, "days": 30})   # a sync saw one more
        self.assertTrue(r.app.old_view(ME, "inbox")["stale"])
        self.assertFalse(self.scan(r)["stale"])

    def test_the_model_sends_spent_mail_to_the_trash_and_flags_open_matters(self):
        r = Rig(self)
        self.scan(r)
        kind = lambda ln: "promotion" if "推送" in ln else "todo" if "聚餐" in ln else "other"

        def reply(body):
            lines = [ln for ln in body["messages"][1]["content"].splitlines() if ln[:1].isdigit()]
            return 200, json.dumps({"items": [{"n": i, "kind": kind(ln)} for i, ln in enumerate(lines, 1)]})
        with FakeChat(reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            r.app._judge_slice(ME, "inbox", {"cancel": False})
        v = r.app.old_view(ME, "inbox")
        got = {g["key"]: (g["action"], g["suggested"]) for g in v["groups"]}
        self.assertEqual(got, {"promotion|list:promo.shop.example": ("trash", True),
                               "todo|person:zhangwei@mail.example": ("archive", False)})           # left for the user
        res = self.apply(r, self.keys(v))
        self.assertEqual((res["moved"], res["ok"]), (3, True))
        self.assertEqual(sorted(r.srv.subjects("[Gmail]/Trash")), ["旧推送一", "旧推送三", "旧推送二"])
        self.assertIn("去年的聚餐", r.inbox())
        r.app.actions("mailbox").undo(res["log_id"])
        self.assertIn("旧推送一", r.inbox())

    def test_archive_in_checked_batches_and_undo_in_batches(self):
        r = Rig(self)
        v = self.scan(r)
        self.assertEqual(self.keys(v), [])                                 # nothing judged: nothing ticked
        res = self.apply(r, self.every(v))
        self.assertEqual((res["moved"], res["stuck"], res["ok"]), (4, 0, True))
        self.assertEqual(r.inbox(), sorted(["本周例会改到周四", "双十一预热", "合同扫描件（留着）"]))   # starred stays
        self.assertEqual(len(r.srv.folders["[Gmail]/All Mail"]), 7)          # archived, nothing deleted
        self.assertEqual(r.srv.label_stores, [])
        self.assertEqual(r.app.list_items("mailbox")["old"]["count"], 1)
        self.assertEqual(r.app.old_view(ME, "inbox")["keep"], [])
        searches = r.srv.commands.count("UID SEARCH")
        r.app.actions("mailbox").undo(res["log_id"])
        self.assertEqual(len(r.inbox()), 7)
        self.assertLess(r.srv.commands.count("UID SEARCH") - searches, 4)     # by COPYUID, not one search per mail
        flags = {s: m["flags"] for s, m in zip(r.srv.subjects("INBOX"), r.srv.folders["INBOX"])}
        self.assertNotIn("\\Seen", flags["旧推送一"])                          # unread before, unread again
        self.assertIn("\\Seen", flags["旧推送二"])                             # read before, read again
        self.assertEqual(r.app.old_view(ME, "inbox")["total"], 4)            # and listed again

    def batched(self, r):
        """One message a batch, so a stop or a crash can fall between batches."""
        old = actions.BATCH
        actions.BATCH = 1
        self.addCleanup(setattr, actions, "BATCH", old)
        return [x for x in r.app.store.old_rows(ME, "inbox") if not server.triage.held(x)]

    def test_processing_stops_between_batches_and_what_was_done_can_be_undone(self):
        """luoke-mail#32: 37,000 messages ran 20 minutes and nothing could stop it."""
        r = Rig(self)
        self.scan(r)
        rows = self.batched(r)
        asked = []
        res = r.app.actions("mailbox").clear_old(rows, [], stop=lambda: asked.append(1) or len(asked) > 2)
        self.assertTrue(res["stopped"])
        self.assertEqual(res["moved"], 2)
        self.assertIn("已停止", res["message"])
        self.assertEqual(len(r.inbox()), 5)                                   # two of the four went, then it stopped
        entry = r.app.store.log_entries(ME)[0]
        self.assertTrue(entry["undoable"])
        self.assertEqual(entry["detail"], res["message"])
        r.app.actions("mailbox").undo(res["log_id"])
        self.assertEqual(len(r.inbox()), 7)
        self.assertEqual(r.app.old_view(ME, "inbox")["total"], 4)

    def test_a_process_that_quits_half_way_leaves_what_it_moved_undoable(self):
        """luoke-mail#32: the undo was written only at the end; quitting 螺壳邮件助手 (or an upgrade restarting it)
        half-way left the moved mail with no way back."""
        r = Rig(self)
        self.scan(r)
        rows = self.batched(r)
        real, calls = Mailbox.move, []

        def move(mb, *a, **k):
            calls.append(1)
            if len(calls) == 3:
                raise SystemExit("the process goes away")                     # not a MailError: nothing catches it
            return real(mb, *a, **k)
        from unittest import mock
        with mock.patch.object(Mailbox, "move", move), self.assertRaises(SystemExit):
            r.app.actions("mailbox").clear_old(rows, [])
        self.assertEqual(len(r.inbox()), 5)
        entry = r.app.store.log_entries(ME)[0]
        self.assertTrue(entry["undoable"])
        self.assertIn("清理未完成：已处理 2 封", entry["detail"])
        self.assertEqual(len(r.app.store.old_rows(ME, "inbox", "moved")), 2)    # the local list knows too
        r.app.actions("mailbox").undo(entry["id"])
        self.assertEqual(len(r.inbox()), 7)
        self.assertEqual(r.app.old_view(ME, "inbox")["total"], 4)

    def test_unticked_sources_stay_unticked(self):
        r = Rig(self)
        self.scan(r)
        self.apply(r, ["pending|list:promo.shop.example"])
        self.assertEqual(self.scan(r)["keep"], [])          # unjudged were never ticked for the user: nothing to remember

    def test_a_uid_that_now_holds_another_message_is_left_alone(self):
        r = Rig(self)
        v = self.scan(r)
        m = next(m for m in r.srv.folders["INBOX"] if m["mid"] == "<o3@x.example>")
        m["raw"] = mail(99, "别人的信", "张伟 <zhangwei@mail.example>", 45)             # same UID, another message
        m["mid"] = "<o99@x.example>"
        res = self.apply(r, ["pending|list:promo.shop.example"])
        self.assertEqual(res["moved"], 2)
        self.assertIn("别人的信", r.srv.subjects("INBOX"))
        self.assertNotIn("<o3@x.example>", [x["message_id"] for x in r.app.store.old_rows(ME, "inbox", "moved")])

    def test_only_what_the_user_confirmed(self):
        """The review of 2026-10-17: the page sends how many it showed; a list that changed since is not carried out."""
        r = Rig(self)
        v = self.scan(r)
        g = next(g for g in v["groups"] if g["key"] == "pending|list:promo.shop.example")
        with self.assertRaisesRegex(ValueError, "列表有了变化"):
            r.app.start_old_apply({"source": "mailbox", "scope": "inbox", "keys": [g["key"]], "count": g["count"] + 1})
        job = wait(r.app, r.app.start_old_apply({"source": "mailbox", "scope": "inbox", "keys": [g["key"]], "count": g["count"]}))
        self.assertEqual(job["result"]["moved"], g["count"])

    def test_starred_since_the_scan_is_left_alone(self):
        """The review of 2026-10-17: the user starred one on the phone after the scan -- it stays."""
        r = Rig(self)
        self.scan(r)
        next(m for m in r.srv.folders["INBOX"] if m["mid"] == "<o3@x.example>")["flags"].add("\\Flagged")
        res = self.apply(r, ["pending|list:promo.shop.example"])
        self.assertEqual(res["moved"], 2)
        self.assertIn("旧推送一", r.srv.subjects("INBOX"))

    def test_a_drop_between_batches_keeps_the_undo(self):
        from unittest import mock
        r = Rig(self)
        self.scan(r)
        r.srv.drop_at_move = 2
        with mock.patch.object(actions, "BATCH", 1):
            res = self.apply(r, ["pending|list:promo.shop.example"])
        self.assertTrue(res["interrupted"])
        self.assertIn("结果未能确认", res["message"])
        r.app.actions(ME).undo(res["log_id"])
        self.assertEqual(sorted(s for s in r.srv.subjects("INBOX") if "旧推送" in s), ["旧推送一", "旧推送三", "旧推送二"])

    def test_without_move_the_copy_is_confirmed_and_undo_finds_each_mail(self):
        r = Rig(self, flavour="qq", provider="qq")
        self.scan(r)
        res = self.apply(r, ["pending|list:promo.shop.example"])
        self.assertEqual(res["moved"], 3)
        self.assertEqual(sorted(r.srv.subjects("Archive")), ["旧推送一", "旧推送三", "旧推送二"])
        self.assertEqual(r.srv.violations, [])
        r.app.actions("mailbox").undo(res["log_id"])
        live = [s for s, m in zip(r.srv.subjects("INBOX"), r.srv.folders["INBOX"]) if "\\Deleted" not in m["flags"]]
        self.assertEqual(sorted(live).count("旧推送一"), 1)

    def test_a_server_that_ignores_the_move_is_reported(self):
        r = Rig(self, ignore_moves=True)
        self.scan(r)
        res = self.apply(r, ["pending|list:promo.shop.example"])
        self.assertEqual((res["moved"], res["stuck"], res["ok"]), (0, 3, False))
        self.assertIn("未能处理", res["message"])
        self.assertIsNone(res["log_id"])

    def test_only_the_trash_needs_processing_mode(self):
        """ADR 0016 (2026-10-19): archiving can be taken back and needs only the click; moving many
        messages to the trash waits for 处理模式."""
        r = Rig(self)
        self.scan(r)
        r.app.cfg["mode"]["write"] = False
        rows = r.app.store.old_rows(ME, "inbox")
        r.app.store.old_judge(ME, {rows[0]["message_id"]: "promotion"}, "m")
        trash = f"promotion|{r.app.old_key(rows[0])}"
        with self.assertRaisesRegex(ValueError, "处理模式"):
            r.app.start_old_apply({"source": "mailbox", "scope": "inbox", "keys": [trash]})
        res = self.apply(r, ["pending|list:promo.shop.example"])            # unjudged: archived
        self.assertTrue(res["moved"])

    def test_demo(self):
        r = Rig(self)
        v = self.scan(r, "demo")
        self.assertEqual(v["flagged"], 1)
        res = self.apply(r, self.every(v), "demo")
        self.assertTrue(res["ok"])
        self.assertEqual(r.app.old_view(actions.DEMO, "inbox")["groups"], [])
        r.app.actions("demo").undo(res["log_id"])
        self.assertEqual(len(r.app.old_view(actions.DEMO, "inbox")["groups"]), len(v["groups"]))
        self.assertEqual(len(self.scan(r, "demo")["groups"]), len(v["groups"]))       # a rescan agrees


class AutoCleanTests(unittest.TestCase):
    """luoke-mail#33 (10-25): old inbox mail is read and judged by itself in idle time, the inbox's line
    and one notification say what is suggested, and nothing in the mailbox moves until the user confirms."""

    @staticmethod
    def judge_reply(body):
        kind = lambda ln: "promotion" if "推送" in ln else "todo" if "聚餐" in ln else "other"   # noqa: E731
        lines = [ln for ln in body["messages"][1]["content"].splitlines() if ln[:1].isdigit()]
        return 200, json.dumps({"items": [{"n": i, "kind": kind(ln)} for i, ln in enumerate(lines, 1)]})

    def synced(self):
        r = Rig(self)
        r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0}, analyze=False)
        return r

    def test_read_and_judged_in_idle_time_then_told_once(self):
        r = self.synced()
        self.assertTrue(r.app.judging(ME, "inbox"))                          # on by default
        self.assertFalse(r.app.judging(ME, "archive"))                       # archived mail: the user's click
        self.assertEqual(r.app._judge_next(), (ME, "inbox"))                 # not read yet: reading comes first
        before = r.inbox()
        with FakeChat(self.judge_reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            r.app._judge_slice(ME, "inbox", {"cancel": False})
        self.assertEqual(r.inbox(), before)                                  # read and judged; nothing moved
        s = r.app.list_items(ME)["old"]
        self.assertEqual((s["total"], s["judged"], s["trash"], s["todo"], s["archive"]), (4, 4, 3, 1, 0))
        self.assertIsNone(r.app._judge_next())                               # nothing left
        title, body = r.app.clean_note(ME)
        self.assertIn("4 封邮件已分析", title)
        self.assertIn("移到垃圾箱 3 封", body)
        self.assertIn("确认后才会处理", body)
        self.assertEqual(r.app._start_page(), "clean")                      # the notification opens 清理
        self.assertIsNone(r.app.clean_note(ME))                              # once

    def test_a_pause_holds(self):
        r = self.synced()
        r.app.set_judging({"source": ME, "scope": "inbox", "on": False})
        self.assertFalse(r.app.judging(ME, "inbox"))
        self.assertIsNone(r.app._judge_next())

    def test_old_mail_also_listed_in_the_inbox_is_not_a_reason_to_read_again(self):
        """Older unread mail sorted into the inbox list is left out of 清理's rows, not out of what was read: the
        count and the reading agree, so neither the page nor idle time reads again and again."""
        r = self.synced()
        wait(r.app, r.app.start_old_scan({"source": ME, "scope": "inbox"}))
        row = r.app.store.old_rows(ME, "inbox")[0]
        r.app.store.upsert_message(ME, {"message_id": row["message_id"], "uid": row["uid"], "subject": row["subject"],
                                        "from_addr": row["from_addr"]})
        self.assertFalse(r.app.old_view(ME, "inbox")["stale"])
        self.assertFalse(r.app._old_unread(ME))


class GmailLinkTests(unittest.TestCase):
    """在 Gmail 中打开: the message itself (by Gmail's own id, read over IMAP), in the connected account -- not a
    search in whichever Google account the browser signed in first."""

    def sync(self, r):
        with FakeChat(triage_reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            return r.app.sync_mailbox(ME, {"cancel": False})

    @staticmethod
    def items(r):
        return [r.app.public_item(i) for i in r.app.store.items(ME, None)]

    def test_opens_the_message_in_the_connected_account(self):
        r = Rig(self)
        self.sync(r)
        it = next(i for i in self.items(r) if i["subject"] == "本周例会改到周四")
        m = next(m for m in r.srv.folders["INBOX"] if m["mid"] == "<o1@x.example>")
        self.assertEqual(it["open_url"], f"https://mail.google.com/mail/u/?authuser={ME}#all/{r.srv.gm_msgid(m):x}")

    def test_mail_known_from_before_gets_its_id_once(self):
        r = Rig(self)
        self.sync(r)
        r.app.store.db.execute("DELETE FROM gmail_ids")                      # as after an upgrade from 0.8
        r.app.store.db.commit()
        it = self.items(r)[0]
        self.assertIn("#search/rfc822msgid%3A", it["open_url"])            # unknown: a search, still in the right account
        self.assertIn(f"authuser={ME}", it["open_url"])
        self.sync(r)
        self.assertIn("#all/", self.items(r)[0]["open_url"])
        fetches = r.srv.commands.count("UID FETCH")
        self.sync(r)
        self.assertLessEqual(r.srv.commands.count("UID FETCH") - fetches, 1)  # looked up once (one more: the labels)

    def test_newsletters_and_mail_gone_from_its_folder(self):
        r = Rig(self)
        all_mail = "[Gmail]/All Mail"
        for n in (50, 51):
            r.srv._add(all_mail, mail(n, f"周刊 {n}", "某周刊 <weekly@news.example>", 100 - n, "weekly.news.example"), {"\\Seen"})
        assert wait(r.app, r.app.start_old_scan({"source": "mailbox", "scope": "archive"}))["status"] == "done"
        self.assertTrue(r.app.store.gmail_id(ME, "<o50@x.example>"))         # read with the headers, at no cost
        rows = [row for row in r.app.store.old_rows(ME, "archive") if row["message_id"] in ("<o50@x.example>", "<o51@x.example>")]
        r.app.store.digest_queue(ME, [{**row, "source_key": "list:weekly.news.example", "name": "某周刊"} for row in rows], "backlog")
        r.app.store.db.execute("DELETE FROM gmail_ids")                      # as after an upgrade from 0.8
        r.app.store.db.commit()
        r.srv.folders[all_mail] = [m for m in r.srv.folders[all_mail] if m["mid"] != "<o50@x.example>"]   # gone since
        self.sync(r)
        self.assertIsNone(r.app.store.gmail_id(ME, "<o50@x.example>"))      # a search link then
        self.assertTrue(r.app.store.gmail_id(ME, "<o51@x.example>"))        # 精选 links open the message
        self.assertFalse([m for m in r.app.store.old_rows(ME, "archive") if r.app.store.gmail_id(ME, m["message_id"])
                          and m["message_id"] not in ("<o51@x.example>",)])  # 清理 shows no links: not looked up
        fetches = r.srv.commands.count("UID FETCH")
        self.sync(r)
        self.assertLessEqual(r.srv.commands.count("UID FETCH") - fetches, 1)  # not looked for again (one more: the labels)

    def test_weekly_newsletters_archived_since_are_found_in_the_archive(self):
        r = Rig(self)
        m = next(m for m in r.srv.folders["INBOX"] if m["mid"] == "<o3@x.example>")
        r.app.store.digest_queue(ME, [{"message_id": m["mid"], "source_key": "list:promo.shop.example", "name": "某某商城",
                                       "folder": "INBOX", "uid": str(m["uid"]), "subject": "旧推送一", "date": "2026-09-01 08:00"}], "weekly")
        r.srv.folders["INBOX"].remove(m)                                     # archived after it was read
        self.sync(r)
        self.assertEqual(r.app.store.gmail_id(ME, m["mid"]), str(r.srv.gm_msgid(m)))

    def test_other_mailboxes_are_never_asked(self):
        for flavour, provider in (("qq", "qq"), ("netease", "163")):
            r = Rig(self, flavour=flavour, provider=provider)
            self.sync(r)
            self.assertNotIn("X-GM", " ".join(r.srv.commands))
            self.assertTrue(all(i["open_url"] is None for i in self.items(r)))

    def test_without_an_address_the_first_account(self):
        from luoke_mail import providers
        self.assertEqual(providers.gmail_message_url(None, "<a@b>", "255"), "https://mail.google.com/mail/u/0/#all/ff")
        self.assertEqual(providers.gmail_url("me+x@gmail.com", "settings/filters"),
                         "https://mail.google.com/mail/u/?authuser=me%2Bx@gmail.com#settings/filters")


class NotifyTests(unittest.TestCase):
    def test_only_lately_arrived_mail_is_news(self):
        r = Rig(self)
        no_evening_note(self)
        r.app.cfg["auto"] = {"enabled": True, "every": 30}
        r.srv._add("INBOX", mail(8, "上周的问题还没回", "王总 <boss@corp.example>", 10), set())
        with FakeChat(triage_reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            r.app.auto.run({"cancel": False, "stage": "", "done": 0, "total": 0})
        notes = r.app.desktop.notes
        self.assertEqual(len(notes), 1)
        self.assertIn("本周例会改到周四", notes[0][1])
        self.assertNotIn("上周的问题还没回", notes[0][1])                     # sorted, on the list, but not news


if __name__ == "__main__":
    unittest.main()


class ThreadTests(unittest.TestCase):
    """Whose turn it is (2026-10-17): the user asked 王总 something four days ago and heard nothing --
    listed as waiting on him; a conversation where the other side asked last shows 「现在等你」 on its card. A
    conversation is asked about once until it has a new message; the setting turns it off."""

    def test_waiting_on_them_and_waiting_on_you(self):
        from luoke_mail import threads
        r = Rig(self)
        sent = "[Gmail]/Sent Mail"

        def mine(n, to, subject, body, days):
            raw = make_mail(subject, body, sender=f"我 <{ME}>", days_ago=days)
            raw = raw.replace(b"To: me@example.com", f"To: {to}".encode()).replace(b"<abc123@corp.example>", f"<s{n}@me.example>".encode())
            r.srv._add(sent, raw, {"\\Seen"})
        mine(1, "wang@corp.example", "Q3 预算", "王总，Q3 预算请您确认，周五前要报上去。", 4)
        r.srv._add("INBOX", mail(70, "周会材料", "李雷 <lilei@corp.example>", 5), {"\\Seen"})
        mine(2, "lilei@corp.example", "Re: 周会材料", "收到，我先看看。", 4)
        r.srv._add("INBOX", mail(71, "Re: 周会材料", "李雷 <lilei@corp.example>", 2), {"\\Seen"})
        asked = []

        def reply(body):
            system, user = body["messages"][0]["content"], body["messages"][1]["content"]
            if system.startswith("下面是用户和别人之间的一串往来邮件"):
                asked.append(user)
                if "Q3" in user:
                    return 200, json.dumps({"state": "them", "since": "2026-10-13", "what": "等王总确认 Q3 预算"}, ensure_ascii=False)
                return 200, json.dumps({"state": "you", "since": "2026-10-15", "what": "李雷问材料能不能用"}, ensure_ascii=False)
            return triage_reply(body)
        job = {"cancel": False, "stage": "", "done": 0, "total": 0}
        with FakeChat(reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            r.app.sync_mailbox(ME, job)
            view = r.app.list_items(ME)
            self.assertEqual([(c["name"], c["what"]) for c in view["waiting"]], [("wang@corp.example", "等王总确认 Q3 预算")])
            card = next(e["item"] for e in view["entries"] if e["type"] == "item" and e["item"]["subject"] == "Re: 周会材料")
            self.assertEqual(card["thread"], {"state": "you", "label": "现在等你", "what": "李雷问材料能不能用"})
            n = len(asked)
            r.app.sync_mailbox(ME, job)
            self.assertEqual(len(asked), n)                                   # nothing new: not asked again
            r.app.save_threads({"on": False})
            self.assertEqual(r.app.list_items(ME)["waiting"], [])
        self.assertEqual(threads.subject_key("回复：Re: [组内] Q3 预算 "), "q3 预算")

    def test_an_unusable_answer_never_says_it_is_the_users_turn(self):
        from luoke_mail import engines, threads
        with FakeChat(lambda body: (200, "不知道")) as chat:
            got = threads.judge([{"me": False, "name": "某人", "date": "2026-10-10", "subject": "x", "text": "y"}],
                                engines.Direct(chat.base_url, "m"))
        self.assertEqual(got["state"], "done")


class DueSoonTests(unittest.TestCase):
    """「快到期」 (2026-10-17): what needs the user and has a day to it comes first, and background
    sorting tells it once, the day before."""

    def test_soonest_first_and_told_once(self):
        r = Rig(self)
        tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        later = (dt.date.today() + dt.timedelta(days=20)).isoformat()

        def reply(body):
            text = body["messages"][1]["content"]
            if "例会" in text:
                return 200, suggestion("follow_up", due_date=tomorrow, summary="明天前确认例会时间")
            return 200, suggestion("reply", due_date=later)
        with FakeChat(reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
            view = r.app.list_items(ME)
            self.assertEqual([(it["subject"], it["when"]) for it in view["due"]], [("本周例会改到周四", tomorrow)])
            listed = [e["item"]["subject"] for e in view["entries"] if e["type"] == "item"]
            self.assertNotIn("本周例会改到周四", listed)                       # once, above
            r.app.cfg["auto"]["enabled"] = True
            r.app.auto.run_one(ME, {"cancel": False, "stage": "", "done": 0, "total": 0}, True)
            r.app.auto.run_one(ME, {"cancel": False, "stage": "", "done": 0, "total": 0}, True)
        told = [n for n in r.app.desktop.notes if "快到期" in n[0]]
        self.assertEqual(len(told), 1)
        self.assertIn("明天：本周例会改到周四", told[0][1])

    def test_the_same_appointment_put_in_the_calendar_once(self):
        """The owner, 10-22: a booking, its change notice and the day-before reminder each offered 添加到日历. Put in
        the calendar from one of them, the others about the same appointment say 已在日程中 and leave 快到期."""
        r = Rig(self)
        day = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        for i, subject in enumerate(("预约完成", "预约变更", "明日のご予約の案内")):
            r.srv._add("INBOX", mail(900 + i, subject, "诊所 <no-reply@clinic.example>", 2 - i), set())

        def reply(body):
            if "诊所" in body["messages"][1]["content"] or "clinic" in body["messages"][1]["content"]:
                return 200, suggestion("schedule", due_date=None, event_title="复诊", event_date=day, event_start="10:00")
            return 200, suggestion("reply")
        with FakeChat(reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        clinic = [it for it in r.app.store.items(ME) if it["from_domain"] == "clinic.example"]
        self.assertEqual(len(clinic), 3)
        self.assertEqual(len([it for it in r.app.due_soon(ME) if it["from_domain"] == "clinic.example"]), 3)
        r.app.actions(ME, write=False).calendar(clinic[0]["id"])
        views = {it["id"]: r.app.public_item(r.app.store.get(it["id"])) for it in clinic}
        self.assertIsNone(views[clinic[0]["id"]]["in_calendar"])                   # the one put in
        self.assertTrue(all(views[it["id"]]["in_calendar"] for it in clinic[1:]))
        self.assertEqual([it["id"] for it in r.app.due_soon(ME) if it["from_domain"] == "clinic.example"], [clinic[0]["id"]])

    def test_an_appointment_that_has_been_is_over_not_due(self):
        """The owner, 10-22: yesterday's clinic reminders sat in 快到期 with 添加到日历. A 加入日程 mail whose day has
        passed is over: not in 快到期, shown as such, archived rather than put in the calendar; a bill past its
        deadline stays, marked overdue (luoke-mail#22)."""
        r = Rig(self)
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()

        def reply(body):
            text = body["messages"][1]["content"]
            if "例会" in text:
                return 200, suggestion("schedule", due_date=None, event_title="复诊", event_date=yesterday, event_start="10:00")
            return 200, suggestion("follow_up", due_date=yesterday)
        with FakeChat(reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
            view = r.app.list_items(ME)
        self.assertNotIn("本周例会改到周四", [it["subject"] for it in view["due"]])
        appt = next(e["item"] for e in view["entries"] if e["type"] == "item" and e["item"]["subject"] == "本周例会改到周四")
        self.assertEqual(appt["past_event"], yesterday)
        bill = next(it for it in view["due"] if it["subject"] == "双十一预热")
        self.assertTrue(r.app.public_item(r.app.store.get(bill["id"]))["overdue"])


class FilterPreviewTests(unittest.TestCase):
    """Before a rule becomes a Gmail filter (2026-10-17): how many messages of the past year each way of
    matching would catch, how many of them it should not touch (the 清理 judgement), and whether to widen."""

    def test_counts_risks_and_advice(self):
        r = Rig(self)
        for n, (subject, sender, lst) in enumerate([("周末大促", "某某商城 <news@shop.example>", "promo.shop.example"),
                                                     ("新品上架", "某某商城 <news@shop.example>", "promo.shop.example"),
                                                     ("账户安全提醒", "某某商城 <security@shop.example>", None),
                                                     ("订单已发货", "某某商城 <order@shop.example>", None)], 90):
            r.srv._add("INBOX", mail(n, subject, sender, 20 + n, lst), {"\\Seen"})
        rid, _, _ = r.app._add_rule(ME, "sender", "news@shop.example", "archive")

        def judge(body):
            lines = [ln for ln in body["messages"][1]["content"].splitlines() if ln[:1].isdigit()]
            kind = lambda ln: "account" if "安全" in ln else "notification" if "发货" in ln else "promotion"  # noqa: E731
            return 200, json.dumps({"items": [{"n": i, "kind": kind(ln)} for i, ln in enumerate(lines, 1)]})
        with FakeChat(judge) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            job = wait(r.app, r.app.start_filter_preview({"source": ME, "id": rid}))
        self.assertEqual(job["status"], "done", job)
        ways = {w["kind"]: w for w in job["result"]["ways"]}
        self.assertEqual(ways["sender"]["count"], 2 + 3)                       # the two new ones and three from before
        self.assertEqual(ways["sender"]["risky"], 0)
        self.assertEqual(ways["domain"]["count"], 2 + 3 + 2)                    # the whole shop.example
        self.assertEqual((ways["domain"]["risky"], ways["domain"]["examples"]), (1, ["账户安全提醒"]))
        self.assertIn("list", ways)
        self.assertTrue(job["result"]["checked"])
        self.assertIn("不建议放宽", job["result"]["advice"])
        self.assertEqual(r.srv.violations, [])


class AutoHabitTests(unittest.TestCase):
    """Top-level ADR 0012 amended (2026-10-17): a habit the user confirmed -- 「某某商城的邮件都归档」 --
    is carried out after sorting: only with 处理模式 on, never on mail that needs the user or looks suspicious,
    logged as the rule's doing, shown and undoable; an undo is recorded against the habit and not redone."""

    HABIT = [{"note_id": 7, "sender": "list:promo.shop.example", "category": "promotion", "action": "archive",
              "text": "某某商城的推广邮件都归档"}]

    def check(self, flavour, provider):
        from unittest import mock
        from luoke_mail import memory
        r = Rig(self, flavour=flavour, provider=provider)
        import email
        import email.policy
        live = lambda: [str(email.message_from_bytes(m["raw"], policy=email.policy.default).get("Subject", ""))  # noqa: E731
                        for m in r.srv.folders["INBOX"] if "\\Deleted" not in m["flags"]]                  # QQ keeps the moved original flagged
        r.srv._add("INBOX", mail(80, "周末促销", "某某商城 <news@shop.example>", 1, "promo.shop.example"), set())
        r.app.cfg["learn"] = {"on": True, "enabled_at": 1}
        job = {"cancel": False, "stage": "", "done": 0, "total": 0}
        with mock.patch.object(memory, "auto_habits", lambda mem, acct: self.HABIT), FakeChat(triage_reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            res = r.app.sync_mailbox(ME, job)
            self.assertEqual(sum(a["count"] for a in res["auto"]), 1)
            self.assertNotIn("周末促销", live())
            self.assertIn("双十一预热", live())                         # needs a reply: never done unasked
            auto = r.app.list_items(ME)["auto"]
            self.assertEqual(len(auto), 1)
            self.assertIn("按你的习惯自动归档", auto[0]["message"])
            rec = r.app.mem.records(topic="mail-auto")[0]["data"]
            self.assertEqual((rec["by"], rec["note_id"], rec["action"]), ("rule", 7, "archive"))
            self.assertEqual(r.app.mem.records(topic="mail-acted"), [])        # the rule's doing, not the user's
            r.app.undo(ME, auto[0]["log_id"])
            self.assertIn("周末促销", live())
            self.assertEqual(r.app.mem.records(topic="mail-undo-auto")[0]["data"]["note_id"], 7)
            self.assertEqual(r.app.list_items(ME)["auto"], [])
            again = r.app.sync_mailbox(ME, job)
            self.assertEqual(again["auto"], [])                               # taken back: not done again
            self.assertIn("周末促销", live())
            r.srv._add("INBOX", mail(81, "下周促销", "某某商城 <news@shop.example>", 0, "promo.shop.example"), set())
            r.app.cfg["mode"]["write"] = False                # archiving can be taken back: no 处理模式 needed (ADR 0016)
            self.assertEqual(sum(a["count"] for a in r.app.sync_mailbox(ME, job)["auto"]), 1)
            self.assertNotIn("下周促销", live())
        self.assertEqual(r.srv.violations, [])

    def test_gmail(self):
        self.check("gmail", "gmail")

    def test_a_habit_is_one_senders_one_kind_of_mail(self):
        """The owner, 2026-10-19: archiving a shop's promotions says nothing of its receipts or security alerts."""
        from luoke_mail import memory

        class Mem:
            def notes(self, kinds, confidence):
                d = {"account": memory.account_key(ME), "sender": "list:promo.shop.example", "action": "archive"}
                return [{"id": 1, "auto": True, "text": "a", "data": {**d, "category": "promotion"}},
                        {"id": 2, "auto": True, "text": "b", "data": d},                      # no kind: not done unasked
                        {"id": 3, "auto": True, "text": "c", "data": {**d, "category": "finance", "action": "trash"}}]
        self.assertEqual([(h["note_id"], h["category"]) for h in memory.auto_habits(Mem(), ME)], [(1, "promotion")])
        r = Rig(self, flavour="gmail", provider="gmail")
        r.app.cfg["mode"]["write"] = True
        bill = {"id": 1, "category": "finance", "action": "archive", "suspicious": 0, "message_id": "<b@x>",
                "list_id": "promo.shop.example", "from_addr": "news@shop.example"}
        from unittest import mock
        with mock.patch.object(memory, "auto_habits", lambda mem, acct: self.HABIT), \
                mock.patch.object(r.app.store, "items", lambda acct, state: [bill]):
            self.assertEqual(r.app._carry_out_habits(ME), [])                # the same shop's bill: left alone

    def test_qq(self):
        self.check("qq", "qq")

    def test_netease(self):
        self.check("netease", "163")


class InterruptedTests(unittest.TestCase):
    """The mailbox connection drops after a move was carried out, before the result came back (seen on real
    Gmail, 2026-10-08): the action is recorded as unconfirmed with its undo, the mail stays on the list, and
    doing it again skips what already happened."""

    def check(self, flavour, provider):
        r = Rig(self, flavour=flavour, provider=provider)
        with FakeChat(triage_reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        item = next(it for it in r.app.store.items(ME) if it["subject"] == "双十一预热")
        acts = r.app.actions("mailbox")
        r.srv.drop_after_move = 1
        res = acts.execute([item["id"]], "archive")
        self.assertTrue(res["interrupted"])
        self.assertIn("结果未能确认", res["message"])
        self.assertEqual(r.app.store.get(item["id"])["state"], "pending")   # nothing claims it happened
        archive = "[Gmail]/All Mail" if flavour == "gmail" else "Archive"
        if flavour == "gmail":
            self.assertNotIn("双十一预热", r.inbox())                      # MOVE: it did happen on the server
        else:
            self.assertIn("双十一预热", r.inbox())                         # COPY then clean-up: only the copy happened
            self.assertEqual(r.srv.subjects(archive).count("双十一预热"), 1)
        acts.undo(res["log_id"])                                              # the undo is there and works
        self.assertEqual(r.inbox().count("双十一预热"), 1)                   # back once, never twice
        if flavour != "gmail":
            self.assertNotIn("双十一预热", r.srv.subjects(archive))         # the stray copy is gone
        r.srv.drop_after_move = 1
        acts.execute([item["id"]], "archive")
        again = acts.execute([item["id"]], "archive")                        # doing it again finishes it, once
        self.assertTrue(again["ok"])
        self.assertNotIn("双十一预热", r.inbox())
        if flavour != "gmail":
            self.assertEqual(r.srv.subjects(archive).count("双十一预热"), 1)
        self.assertEqual(r.srv.violations, [])

    def test_gmail(self):
        self.check("gmail", "gmail")

    def test_netease(self):
        self.check("netease", "163")

    def test_nothing_asked_yet_is_a_plain_error(self):
        r = Rig(self)
        with FakeChat(triage_reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        item = r.app.store.items(ME)[0]
        r.app.open_mailbox = lambda *_: (_ for _ in ()).throw(server.MailError("连接邮箱服务器超时，请检查网络后重试。"))
        with self.assertRaises(server.MailError):
            r.app.actions("mailbox").execute([item["id"]], "archive")
        self.assertEqual([e for e in r.app.store.log_entries(ME) if e["action"] != "seen"], [])   # (sorted, marked read)
