"""Write paths against the fake servers: every action, its undo, and the provider quirks."""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import actions, pipeline, samples, subscriptions  # noqa: E402
from luoke_mail.mailbox import Mailbox, parse_message  # noqa: E402
from luoke_mail.store import Store  # noqa: E402
from fakes import FakeIMAP  # noqa: E402
from test_core import make_mail  # noqa: E402

ME = "me@example.com"


def mail(n, subject, sender="news@shop.example", **kw):
    raw = make_mail(subject, f"正文 {n}", sender=sender).replace(b"<abc123@corp.example>", f"<m{n}@x.example>".encode())
    return raw


class Rig:
    """A fake mailbox + a fresh store + Actions wired to it, with the inbox already synced."""

    def __init__(self, flavour, provider, msgs, **fake):
        self.srv = FakeIMAP(msgs, flavour=flavour, **fake).__enter__()
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        self.provider = provider
        self.acts = actions.Actions(self.store, ME, provider, self.open)
        with self.open() as mb:
            fetched = mb.fetch_recent(limit=50, unseen_only=False, days=None)
        self.ids = {}
        for m in fetched:
            i, _ = self.store.upsert_message(ME, m)
            self.ids[m["subject"]] = i

    def open(self):
        return Mailbox(self.provider, ME, "secret", host="127.0.0.1", port=self.srv.port, ssl=False, timeout=5)

    def close(self):
        self.srv.__exit__()
        self.store.close()


class GmailTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig("gmail", "gmail", [(mail(1, "促销一"), False), (mail(2, "促销二"), False), (mail(3, "账单"), True)])

    def tearDown(self):
        self.r.close()

    def test_archive_moves_to_all_mail_and_undo_restores(self):
        res = self.r.acts.execute([self.r.ids["促销一"], self.r.ids["促销二"]], "archive")
        self.assertEqual(len(res["done"]), 2)
        self.assertEqual(self.r.srv.subjects("INBOX"), ["账单"])
        self.assertEqual(len(self.r.srv.folders["[Gmail]/All Mail"]), 3)      # nothing deleted
        self.assertNotIn("UID EXPUNGE", self.r.srv.commands)
        self.assertIn("UID MOVE", self.r.srv.commands)                        # Gmail archive = move to All Mail
        self.assertEqual(self.r.srv.label_stores, [])                         # label stores: OK from Gmail, no effect
        self.assertEqual(self.r.store.get(self.r.ids["促销一"])["state"], "done")
        self.r.acts.undo(res["log_id"])
        self.assertCountEqual(self.r.srv.subjects("INBOX"), ["账单", "促销一", "促销二"])
        self.assertNotIn("\\Seen", self.r.srv.find("INBOX", "促销一")["flags"])
        self.assertEqual(self.r.store.get(self.r.ids["促销一"])["state"], "pending")
        with self.assertRaises(actions.ActionError):
            self.r.acts.undo(res["log_id"])                                   # twice is refused

    def test_trash_and_spam_and_undo(self):
        res = self.r.acts.execute([self.r.ids["促销一"]], "trash")
        self.assertEqual(self.r.srv.subjects("[Gmail]/Trash"), ["促销一"])
        self.assertIn("\\Seen", self.r.srv.folders["[Gmail]/Trash"][0]["flags"])
        self.r.acts.undo(res["log_id"])
        self.assertIn("促销一", self.r.srv.subjects("INBOX"))
        self.assertEqual(self.r.srv.subjects("[Gmail]/Trash"), [])
        self.r.acts.execute([self.r.ids["促销二"]], "spam")
        self.assertEqual(self.r.srv.subjects("[Gmail]/Spam"), ["促销二"])

    def test_flag_keeps_item_pending(self):
        res = self.r.acts.execute([self.r.ids["账单"]], "flag")
        self.assertIn("\\Flagged", self.r.srv.find("INBOX", "账单")["flags"])
        it = self.r.store.get(self.r.ids["账单"])
        self.assertTrue(it["flagged"])
        self.assertEqual(it["state"], "pending")
        self.r.acts.undo(res["log_id"])
        self.assertNotIn("\\Flagged", self.r.srv.find("INBOX", "账单")["flags"])

    def test_draft_reply_goes_to_drafts_threaded(self):
        res = self.r.acts.save_draft(self.r.ids["账单"], "好的，周五前给您。\n—— 小周", ME, "小周")
        drafts = self.r.srv.folders["[Gmail]/Drafts"]
        self.assertEqual(len(drafts), 1)
        d = parse_message(drafts[0]["raw"])
        self.assertTrue(d["subject"].startswith("Re: "))
        self.assertIn(b"In-Reply-To: <m3@x.example>", drafts[0]["raw"])
        self.assertIn("\\Draft", drafts[0]["flags"])
        self.assertIn("周五前", d["body"])
        self.r.acts.undo(res["log_id"])
        self.assertEqual(self.r.srv.folders["[Gmail]/Drafts"], [])

    def test_sync_drops_mail_handled_elsewhere(self):
        self.r.srv.folders["INBOX"] = [m for m in self.r.srv.folders["INBOX"] if m["mid"] != "<m1@x.example>"]
        with self.r.open() as mb:
            gone = self.r.store.mark_missing(ME, mb.inbox_uids())
        self.assertEqual(gone, [self.r.ids["促销一"]])
        self.assertEqual(self.r.store.get(self.r.ids["促销一"])["done_action"], "gone")
        self.assertEqual(self.r.store.get(self.r.ids["促销二"])["state"], "pending")

    def test_move_the_server_ignored_is_not_reported_as_done(self):
        """Real Gmail answered OK to an archive and changed nothing; the tool said "已归档" and hid the mail."""
        r = Rig("gmail", "gmail", [(mail(1, "促销一"), False), (mail(2, "促销二"), False)], ignore_moves=True)
        self.addCleanup(r.close)
        res = r.acts.execute([r.ids["促销一"], r.ids["促销二"]], "archive")
        self.assertFalse(res["ok"])
        self.assertEqual((res["done"], len(res["stuck"])), ([], 2))
        self.assertIn("未能归档", res["message"])
        self.assertIsNone(res["log_id"])                                      # nothing to undo
        self.assertEqual(r.store.get(r.ids["促销一"])["state"], "pending")      # still on the list

    def test_already_moved_mail_is_reported_not_failed(self):
        self.r.srv.folders["INBOX"] = [m for m in self.r.srv.folders["INBOX"] if m["mid"] != "<m1@x.example>"]
        res = self.r.acts.execute([self.r.ids["促销一"], self.r.ids["促销二"]], "archive")
        self.assertEqual(len(res["gone"]), 1)
        self.assertEqual(len(res["done"]), 1)


class HistoryTests(unittest.TestCase):
    """Clean up one sender's whole history: inbox + archived mail to the trash, undo puts each back."""

    def test_gmail_history_includes_archived_mail(self):
        msgs = [(mail(1, "旧推送", sender="promo@shop.example"), True), (mail(2, "新推送", sender="promo@shop.example"), False),
                (mail(3, "同事来信", sender="li@corp.example"), False)]
        r = Rig("gmail", "gmail", msgs)
        try:
            r.acts.execute([r.ids["旧推送"]], "archive")                    # now only in All Mail
            sub = {"key": "from:promo@shop.example", "from_addr": "promo@shop.example", "list_id": None, "name": "商城"}
            self.assertEqual(r.acts.history_preview(sub)["count"], 2)
            res = r.acts.trash_history(sub, 30)
            self.assertIn("30 天后自动清空", res["message"])
            self.assertCountEqual(r.srv.subjects("[Gmail]/Trash"), ["旧推送", "新推送"])
            self.assertEqual(r.srv.subjects("INBOX"), ["同事来信"])
            self.assertEqual(r.srv.subjects("[Gmail]/All Mail"), ["同事来信"])
            r.acts.undo(res["log_id"])
            self.assertCountEqual(r.srv.subjects("INBOX"), ["同事来信", "新推送"])      # inbox mail back in the inbox
            self.assertCountEqual(r.srv.subjects("[Gmail]/All Mail"), ["同事来信", "新推送", "旧推送"])  # archived stays archived
            self.assertEqual(r.srv.subjects("[Gmail]/Trash"), [])
        finally:
            r.close()

    def test_netease_history(self):
        r = Rig("netease", "163", [(mail(1, "推送一", sender="promo@shop.example"), False), (mail(2, "工作", sender="li@corp.example"), False)])
        try:
            sub = {"key": "from:promo@shop.example", "from_addr": "promo@shop.example", "list_id": None, "name": "商城"}
            res = r.acts.trash_history(sub, 7)
            self.assertEqual(r.srv.subjects("&XfJSIJZk-"), ["推送一"])
            self.assertEqual(r.srv.subjects("INBOX"), ["工作"])
            r.acts.undo(res["log_id"])
            self.assertCountEqual(r.srv.subjects("INBOX"), ["工作", "推送一"])
        finally:
            r.close()


def listed(n, subject, list_id, sender="news@shop.example"):
    return mail(n, subject, sender=sender).replace(b"Subject:", f"List-Id: <{list_id}>\r\nSubject:".encode(), 1)


class SearchValueTests(unittest.TestCase):
    """The List-Id / From / Message-ID searched for come from mail headers -- the sender writes them (issue #2,
    2026-10-16): they are quoted, an empty one searches nothing, and each hit is checked against its own header."""

    def setUp(self):
        self.r = Rig("gmail", "gmail", [
            (listed(1, "促销", "promo.shop.example"), False),
            (listed(2, "每日促销", "dailypromo.shop.example"), False),                 # contains the other's id
            (listed(3, "伪造的", 'evil" all "x', sender="evil@spam.example"), False),
            (mail(4, "同事来信", sender="li@corp.example"), False)])

    def tearDown(self):
        self.r.close()

    def archive_all(self, list_id=None, from_addr=None):
        return self.r.acts.archive_sender({"key": "k", "name": "某来源", "list_id": list_id, "from_addr": from_addr})

    def test_only_that_exact_list(self):
        self.assertEqual(self.archive_all("promo.shop.example")["count"], 1)
        self.assertCountEqual(self.r.srv.subjects("INBOX"), ["每日促销", "伪造的", "同事来信"])

    def test_a_list_id_with_quotes_is_one_value(self):
        self.assertEqual(self.archive_all('evil" all "x')["count"], 1)
        self.assertCountEqual(self.r.srv.subjects("INBOX"), ["促销", "每日促销", "同事来信"])

    def test_nothing_to_search_for_moves_nothing(self):
        for list_id, from_addr in ((None, ""), (None, "  "), ("promo\r\nA1 LOGOUT", None)):
            self.assertEqual(self.archive_all(list_id, from_addr)["count"], 0)
        self.assertEqual(len(self.r.srv.subjects("INBOX")), 4)
        self.assertEqual(self.r.acts.history_preview({"key": "k", "name": "x", "list_id": None, "from_addr": ""})["count"], 0)


class HistorySafetyTests(unittest.TestCase):
    """The review of 2026-10-17: clearing one sender's history leaves starred mail, kept labels, what 订阅精选
    kept and the user's own mail alone, and a connection that drops between batches keeps the undo of what went."""

    def setUp(self):
        self.r = Rig("gmail", "gmail", [(mail(n, f"推送{n}", sender="promo@shop.example"), True) for n in range(1, 6)])
        allmail = self.r.srv.folders["[Gmail]/All Mail"]
        by = {m["mid"]: m for m in allmail}
        by["<m2@x.example>"]["flags"].add("\\Flagged")                    # starred
        by["<m3@x.example>"]["labels"] = ["收藏"]                           # under a keep label
        self.sub = {"key": "from:promo@shop.example", "from_addr": "promo@shop.example", "list_id": None, "name": "商城"}

    def tearDown(self):
        self.r.close()

    def test_what_stays(self):
        keep = {"<m4@x.example>"}                                          # 订阅精选 kept something in it
        self.assertEqual(self.r.acts.history_preview(self.sub, keep)["count"], 2)
        res = self.r.acts.trash_history(self.sub, 30, keep=keep)
        self.assertEqual(res["count"], 2)
        self.assertCountEqual(self.r.srv.subjects("[Gmail]/Trash"), ["推送1", "推送5"])
        own = {**self.sub, "from_addr": ME}                                # the user listed as a sender: nothing
        self.assertEqual(self.r.acts.trash_history(own, 30, me={ME})["count"], 0)

    def test_a_drop_between_batches_keeps_the_undo(self):
        from unittest import mock
        self.r.srv.drop_at_move = 2                                         # the second batch never hears back
        with mock.patch.object(actions, "BATCH", 1):
            res = self.r.acts.trash_history(self.sub, 30)
        self.assertTrue(res["interrupted"])
        self.assertEqual(len(self.r.srv.subjects("[Gmail]/Trash")), 2)      # the first went, the second did too
        self.r.acts.undo(res["log_id"])
        self.assertEqual(self.r.srv.subjects("[Gmail]/Trash"), [])          # both are back
        self.assertEqual(len(self.r.srv.subjects("INBOX")), 5)


class QQTests(unittest.TestCase):
    """No MOVE, no UIDPLUS: COPY + \\Deleted on exactly our UIDs, never EXPUNGE."""

    def setUp(self):
        self.r = Rig("qq", "qq", [(mail(1, "促销一"), False), (mail(2, "工作"), False)])

    def tearDown(self):
        self.r.close()

    def test_trash_without_move(self):
        res = self.r.acts.execute([self.r.ids["促销一"]], "trash")
        self.assertEqual(self.r.srv.subjects("Deleted Messages"), ["促销一"])
        flagged = self.r.srv.find("INBOX", "促销一")
        self.assertIn("\\Deleted", flagged["flags"])                         # client will clean it up
        self.assertNotIn("\\Deleted", self.r.srv.find("INBOX", "工作")["flags"])
        self.assertEqual(self.r.srv.violations, [])                           # no MOVE / UID EXPUNGE / ID sent
        self.assertTrue(res["ok"])

    def test_copy_the_server_ignored_never_flags_the_original_deleted(self):
        """Without MOVE the original is flagged \\Deleted after COPY; if the copy never happened, the mail
        would be lost when the mail client cleans up. Only confirmed copies are flagged."""
        r = Rig("qq", "qq", [(mail(1, "促销一"), False)], ignore_moves=True)
        self.addCleanup(r.close)
        res = r.acts.execute([r.ids["促销一"]], "trash")
        self.assertNotIn("\\Deleted", r.srv.find("INBOX", "促销一")["flags"])
        self.assertEqual(len(res["stuck"]), 1)
        self.assertEqual(r.store.get(r.ids["促销一"])["state"], "pending")

    def test_archive_creates_folder_when_missing(self):
        self.r.acts.execute([self.r.ids["工作"]], "archive")
        self.assertIn("Archive", self.r.srv.folders)
        self.assertEqual(self.r.srv.subjects("Archive"), ["工作"])
        self.assertEqual(self.r.srv.violations, [])


class NetEaseTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig("netease", "163", [(mail(1, "促销一"), False)])

    def tearDown(self):
        self.r.close()

    def test_chinese_folder_names(self):
        res = self.r.acts.execute([self.r.ids["促销一"]], "trash")
        self.assertEqual(self.r.srv.subjects("&XfJSIJZk-"), ["促销一"])          # 已删除
        self.assertEqual(self.r.srv.subjects("INBOX"), [])                        # UIDPLUS: expunged our UID only
        self.r.acts.undo(res["log_id"])
        self.assertEqual(self.r.srv.subjects("INBOX"), ["促销一"])


class MessageIdSearchTests(unittest.TestCase):
    """issue #9 (2026-10-21): QQ's SEARCH never finds a Message-ID, 163's only with the HEADER key alone (the fake
    servers do the same). Starring and archiving still reach the mail -- by the UID the sync saw, checked -- and a
    163 archive without MOVE confirms its copy and leaves one, however often it is asked."""

    def check(self, flavour, provider, archive):
        r = Rig(flavour, provider, [(mail(1, "工作"), False), (mail(2, "另一封"), False)])
        self.addCleanup(r.close)
        res = r.acts.execute([r.ids["工作"]], "flag")
        self.assertEqual((res["done"], res["gone"]), ([r.ids["工作"]], []))         # not 「已不在收件箱」
        self.assertIn("\\Flagged", r.srv.find("INBOX", "工作")["flags"])
        r.acts.undo(res["log_id"])
        self.assertNotIn("\\Flagged", r.srv.find("INBOX", "工作")["flags"])
        res = r.acts.execute([r.ids["工作"]], "archive")
        self.assertEqual((res["done"], res["gone"], res["stuck"]), ([r.ids["工作"]], [], []))
        self.assertEqual(r.srv.subjects(archive), ["工作"])
        again = r.acts.execute([r.ids["工作"]], "archive")                           # asked twice: still one copy
        self.assertEqual(r.srv.subjects(archive), ["工作"])
        self.assertTrue(again["gone"])
        r.acts.undo(res["log_id"])
        live = [m for m in r.srv.folders["INBOX"] if m["mid"] == r.store.get(r.ids["工作"])["message_id"]
                and "\\Deleted" not in m["flags"]]
        self.assertEqual(len(live), 1)                     # back once (without UIDPLUS the old one stays flagged)

    def test_qq(self):
        self.check("qq", "qq", "Archive")

    def test_163(self):
        self.check("netease", "163", "Archive")


class PipelineTests(unittest.TestCase):
    """Every message is read by the model; a rule is context, not a shortcut."""

    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "p.db"))
        self.addCleanup(self.store.close)
        self.calls = []

    def analyze(self, item, rule):
        self.calls.append((item["subject"], rule and rule["value"]))
        urgent = "账单" in item["subject"] or "bill" in item["subject"].lower()
        return {"action": "follow_up" if urgent else "archive", "priority": "low", "category": "promotion", "summary": "推广",
                "reason": "群发", "due_date": None, "event": None, "reply_points": [], "suspicious": False, "seconds": 0.1}

    def test_every_message_is_read(self):
        inbox = samples.demo_inbox()
        stats = pipeline.sync(self.store, "demo", inbox, self.analyze)
        self.assertEqual((stats["new"], stats["by_model"]), (len(inbox), len(inbox)))
        self.assertEqual(len(self.calls), len(inbox))                          # bulk mail too: no group shortcut
        newsletter = next(m for m in inbox if m.get("list_id") == "weekly.aiweekly.example")
        pipeline.sync(self.store, "demo", [dict(newsletter, message_id="<next@x>", subject="This week in AI: next issue")], self.analyze)
        self.assertEqual(len(self.calls), len(inbox) + 1)                      # a later issue is read as well

    def test_rule_is_context_and_the_model_may_differ(self):
        self.store.add_rule("demo", "domain", "jd.example", "archive", "归档")
        jd = next(m for m in samples.demo_inbox() if m["from_addr"] == "order@jd.example")
        pipeline.sync(self.store, "demo", [dict(jd, message_id="<jd1@x>", subject="新品上市"),
                                           dict(jd, message_id="<jd2@x>", subject="您的白条账单本周到期")], self.analyze)
        self.assertEqual([c[1] for c in self.calls], ["jd.example", "jd.example"])     # the model was told the rule
        got = {i["message_id"]: i for i in self.store.items("demo")}
        self.assertEqual((got["<jd1@x>"]["action"], got["<jd1@x>"]["suggested_by"].split(":")[0]), ("archive", "rule"))
        self.assertEqual((got["<jd2@x>"]["action"], got["<jd2@x>"]["suggested_by"].split(":")[0]), ("follow_up", "rule-differs"))

    def test_model_off_everything_waits(self):
        self.store.add_rule("demo", "domain", "jd.example", "archive", "归档")
        stats = pipeline.sync(self.store, "demo", samples.demo_inbox()[:5], None)
        self.assertEqual((stats["waiting"], stats["by_rule"]), (5, 0))
        self.assertEqual(len(self.store.unanalyzed("demo")), 5)

    def test_rule_text_for_the_model(self):
        from luoke_mail import triage
        t = triage.rule_text({"kind": "sender", "value": "a@b.example", "action": "spam"})
        self.assertIn("发件人「a@b.example」", t)
        self.assertIn("一律移到垃圾邮件（action 设为 ignore）", t)      # not "suspicious": the user's junk isn't a scam
        msgs = triage.build_messages({"subject": "x", "body": "y"}, rule={"kind": "domain", "value": "b.example", "action": "ignore"})
        self.assertIn("一律「移到垃圾箱」", msgs[1]["content"])


class SeveralEventsTests(unittest.TestCase):
    def test_every_appointment_in_the_file_and_one_at_a_time(self):
        st = Store(os.path.join(tempfile.mkdtemp(), "u.db"))
        self.addCleanup(st.close)
        ev = {"title": "去程", "date": "2026-12-15", "start": "10:10", "end": "14:05",
              "more": [{"title": "回程", "date": "2026-12-22", "start": "15:30", "end": "21:15"}]}
        a = actions.Actions(st, "demo", None, None)
        it = {"id": 1, "message_id": "<a@b.example>", "subject": "机票", "from_domain": "fly.example", "event": ev}
        a._item = lambda _id: it
        both = a.calendar(1)["ics"]
        self.assertEqual(both.count("BEGIN:VEVENT"), 2)
        self.assertEqual(len(set(re.findall(r"UID:(\S+)", both))), 2)
        self.assertEqual(both.count("DTSTART:20261222T153000"), 1)
        self.assertNotIn("more", ev["more"][0])
        back = a.calendar(1, 1)["ics"]
        self.assertEqual(back.count("BEGIN:VEVENT"), 1)
        self.assertIn("DTEND:20261222T211500", back)
        self.assertEqual(re.search(r"UID:\S+", back).group(0), re.findall(r"UID:\S+", both)[1])    # imported twice: updated
        self.assertEqual(len(st.cache_get("demo", "calendar_added")[0]), 2)
        with self.assertRaises(actions.ActionError):
            a.calendar(1, 2)


class SubscriptionTests(unittest.TestCase):
    def test_read_by_the_assistant_is_not_read_by_the_user(self):
        hs = [{"from_addr": "news@shop.example", "from_domain": "shop.example", "bulk": True, "seen": True, "subject": "s",
               "date": "2026-10-01", "message_id": f"<{i}@x>", "unsub_http": "https://shop.example/u", "size": 1} for i in range(5)]
        self.assertEqual(subscriptions.summarize(hs, set(), 60)[0]["verdict"], "keep")
        row = subscriptions.summarize(hs, set(), 60, {h["message_id"] for h in hs})[0]
        self.assertEqual((row["read"], row["verdict"]), (0, "unsubscribe"))

    def test_body_methods_and_sizes(self):
        rows = {r["key"]: r for r in subscriptions.summarize(samples.demo_headers(), set(), 60)}
        gym = rows["from:news@gymcloud.example"]
        self.assertEqual((gym["method"], gym["verdict"]), ("body_link", "unsubscribe"))
        self.assertEqual(rows["from:promo@freshmart.example"]["method"], "reply")
        self.assertEqual(rows["from:notice@forum.example"]["verdict"], "filter")
        self.assertEqual(gym["bytes"], 11 * 95_000)
        st = Store(os.path.join(tempfile.mkdtemp(), "u.db"))
        self.addCleanup(st.close)
        a = actions.Actions(st, "demo", None, None)
        r = a.unsubscribe(gym)
        self.assertEqual((r["status"], r["open_url"]), ("unsubscribe_opened", gym["body_link"]))
        r = a.unsubscribe(rows["from:promo@freshmart.example"])
        self.assertTrue(r["mailto"].startswith("mailto:promo@freshmart.example?subject=TD"))

    def test_refused_one_click_falls_back_and_is_remembered(self):
        st = Store(os.path.join(tempfile.mkdtemp(), "o.db"))
        self.addCleanup(st.close)
        a = actions.Actions(st, "me@example.com", "gmail", None)
        sub = {"key": "from:info@kfc.example", "name": "KFC", "from_addr": "info@kfc.example", "method": "one_click",
               "unsub_http": "https://kfc.example/u/1"}
        real = subscriptions.one_click
        subscriptions.one_click = lambda url, timeout=15: (False, "HTTP 405")
        try:
            r = a.unsubscribe(sub)
        finally:
            subscriptions.one_click = real
        self.assertEqual((r["status"], r["open_url"]), ("unsubscribe_opened", "https://kfc.example/u/1"))
        self.assertIn("不支持一键退订", r["message"])
        self.assertNotIn("405", r["message"])
        self.assertEqual(st.sender("me@example.com", "from:info@kfc.example")["status"], "unsubscribe_opened")

    def test_spam_rule_is_junk_not_a_scam(self):
        st = Store(os.path.join(tempfile.mkdtemp(), "r.db"))
        self.addCleanup(st.close)
        st.add_rule("me@example.com", "sender", "x@bad.example", "spam", "当垃圾邮件")
        s = pipeline.rule_suggestion({"subject": "s", "category": "promotion"}, st.rules("me@example.com")[0])
        self.assertEqual((s["action"], s["suspicious"], s["category"]), ("ignore", False, "promotion"))

    def test_verdicts(self):
        rows = {r["key"]: r for r in subscriptions.summarize(samples.demo_headers(), {"security@icbc-verify.example"}, 60)}
        self.assertEqual(rows["list:promo.mall.example"]["verdict"], "unsubscribe")
        self.assertEqual(rows["list:promo.mall.example"]["method"], "mailto")
        self.assertEqual(rows["list:weekly.aiweekly.example"]["method"], "one_click")
        self.assertEqual(rows["list:mail.luoke.github.example"]["verdict"], "keep")
        self.assertEqual(rows["from:security@icbc-verify.example"]["verdict"], "never")

    def test_refuses_suspicious_and_non_https(self):
        st = Store(os.path.join(tempfile.mkdtemp(), "s.db"))
        self.addCleanup(st.close)
        a = actions.Actions(st, "demo", None, None)
        with self.assertRaises(actions.ActionError):
            a.unsubscribe({"suspicious": True, "method": "one_click", "unsub_http": "https://x"})
        ok, msg = subscriptions.one_click("http://insecure.example/u")
        self.assertFalse(ok)


class CalendarTests(unittest.TestCase):
    def test_google_url_and_ics(self):
        it = {"id": 7, "subject": "评审会", "summary": "项目评审", "from_addr": "pmo@corp.example"}
        ev = {"title": "项目评审会", "date": "2026-10-14", "start": "15:00", "end": None, "location": "3 楼"}
        url = actions.google_calendar_url(it, ev)
        self.assertIn("dates=20261014T150000%2F20261014T160000", url)
        ics = actions.event_ics(it, [ev])
        self.assertIn("DTSTART:20261014T150000", ics)
        self.assertIn("LOCATION:3 楼", ics)
        all_day = actions.event_ics(it, [dict(ev, start=None)])
        self.assertIn("DTSTART;VALUE=DATE:20261014", all_day)
        self.assertIn("DTEND;VALUE=DATE:20261015", all_day)

    def test_ics_keeps_one_uid_per_mail_and_folds_long_lines(self):
        """luoke-mail#27: a new UID each time put the same event in the calendar twice; Chinese DESCRIPTIONs ran
        past RFC 5545's 75 octets a line."""
        import re
        it = {"id": 7, "message_id": "<a@b.example>", "subject": "评审会" * 30, "summary": "项目评审" * 20}
        ev = {"title": "项目评审会", "date": "2026-10-14", "start": "15:00", "end": None, "location": None}
        one, two = actions.event_ics(it, [ev]), actions.event_ics(it, [ev])
        uid = re.search(r"UID:(\S+)", one).group(1)
        self.assertEqual(uid, re.search(r"UID:(\S+)", two).group(1))
        self.assertNotEqual(uid, re.search(r"UID:(\S+)", actions.event_ics({**it, "message_id": "<c@d.example>"}, [ev])).group(1))
        lines = one.split("\r\n")
        self.assertTrue(all(len(x.encode("utf-8")) <= 75 for x in lines))
        # a bare CR in the mail's text must not end the line and start a property of its own
        cr = actions.event_ics({**it, "subject": "会\rATTACH:http://x.example/a"}, [{**ev, "title": "评审\r\nORGANIZER:x"}])
        self.assertFalse([x for x in cr.replace("\r\n ", "").split("\r\n") if x.startswith(("ATTACH", "ORGANIZER"))])
        self.assertNotIn("\r", cr.replace("\r\n", ""))
        unfolded = one.replace("\r\n ", "")
        self.assertIn("SUMMARY:" + "项目评审会", unfolded)
        self.assertIn("项目评审" * 20, unfolded)                        # nothing lost, no character split


if __name__ == "__main__":
    unittest.main()
