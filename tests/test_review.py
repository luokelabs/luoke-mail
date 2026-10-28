"""今日处理 (top-level ADR 0016, 2026-10-19): sorted mail is marked read, confirmed habits archive, and each
message's outcome is listed for the day -- taken back or changed in one click, a change recorded for the memory;
once each evening the user is told how many there were."""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import memory, review  # noqa: E402
from luoke_mail.mailbox import utf7_encode  # noqa: E402
from fakes import FakeChat  # noqa: E402
from test_background import triage_reply  # noqa: E402
from test_old_mail import ME, Rig, mail, wait  # noqa: E402

HABIT = [{"note_id": 7, "sender": "list:promo.shop.example", "category": "promotion", "action": "archive", "text": "某某商城的推广邮件都归档"}]


class ReviewTests(unittest.TestCase):
    def rig(self):
        r = Rig(self)
        r.app.cfg["mode"]["write"] = False                     # nothing here needs 处理模式
        r.srv._add("INBOX", mail(90, "周末促销", "某某商城 <news@shop.example>", 0, "promo.shop.example"), set())
        self.chat = FakeChat(triage_reply).__enter__()
        self.addCleanup(self.chat.__exit__)
        r.app.cfg["engine"]["base_url"] = self.chat.base_url
        p = mock.patch.object(memory, "auto_habits", lambda mem, acct: HABIT)
        p.start()
        self.addCleanup(p.stop)
        r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        return r

    def test_every_message_of_the_day_and_what_became_of_it(self):
        r = self.rig()
        v = r.app.review.view(ME)
        results = {x["subject"]: x["result"] for x in v["items"]}
        self.assertEqual(results["周末促销"], "auto_archive")                  # the habit's work
        self.assertEqual(results["双十一预热"], "waiting")                       # needs a reply: left for the user
        self.assertTrue(all(r_ in ("auto_archive", "waiting", "kept", "suspicious") for r_ in results.values()), results)
        self.assertEqual(v["items"][0]["result"] in ("suspicious", "waiting"), True)   # what needs the user first
        self.assertEqual(v["total"], len(v["items"]))
        self.assertIn("\\Seen", r.srv.find("INBOX", "双十一预热")["flags"])        # what was sorted is marked read

    def test_a_habits_work_is_undone_one_message_at_a_time_and_counts_against_it(self):
        r = self.rig()
        x = next(x for x in r.app.review.view(ME)["items"] if x["subject"] == "周末促销")
        v = r.app.review.act({"source": ME, "id": x["id"], "op": "undo"})
        self.assertIn("周末促销", r.inbox())
        self.assertEqual(next(y for y in v["items"] if y["id"] == x["id"])["result"], "kept")
        self.assertEqual(r.app.mem.records(topic="mail-undo-auto")[0]["data"]["note_id"], 7)   # feedback for the memory
        again = r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        self.assertEqual(again["auto"], [])                                     # not done to it again
        with self.assertRaises(ValueError):
            r.app.review.act({"source": ME, "id": x["id"], "op": "undo"})

    def test_marked_unread_stays_unread(self):
        r = self.rig()
        x = next(x for x in r.app.review.view(ME)["items"] if x["subject"] == "双十一预热")
        before = len(r.app.mem.records(topic="mail-acted"))
        r.app.review.act({"source": ME, "id": x["id"], "op": "unread"})
        self.assertNotIn("\\Seen", r.srv.find("INBOX", "双十一预热")["flags"])
        r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        self.assertNotIn("\\Seen", r.srv.find("INBOX", "双十一预热")["flags"])     # not marked read again
        self.assertEqual(len(r.app.mem.records(topic="mail-acted")), before)      # nor taken for the user reading it
        mid = r.app.store.get(x["id"])["message_id"]
        self.assertNotIn(mid, r.app.store.read_by_us(ME))                       # if the user opens it now, they did

    def test_marking_read_is_in_the_operation_log_and_undone_from_there(self):
        r = self.rig()
        seen = next(e for e in r.app.store.log_entries(ME) if e["action"] == "seen")
        self.assertTrue(seen["undoable"] and seen["detail"].startswith("分析后标为已读"))
        r.app.undo(ME, seen["id"])
        self.assertNotIn("\\Seen", r.srv.find("INBOX", "双十一预热")["flags"])
        r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        self.assertNotIn("\\Seen", r.srv.find("INBOX", "双十一预热")["flags"])    # not marked read again

    def test_told_once_each_evening_and_the_badge_clears_when_looked_at(self):
        r = self.rig()
        self.assertGreater(r.app.review.unseen(ME), 0)
        r.app.review.seen({"source": ME})
        self.assertEqual(r.app.review.unseen(ME), 0)
        evening = dt.datetime.combine(dt.date.today(), dt.time(20, 30))
        noon = dt.datetime.combine(dt.date.today(), dt.time(12))
        with mock.patch.object(review.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = noon
            self.assertIsNone(r.app.review.daily_note(ME))
            clock.now.return_value = evening
            title, body = r.app.review.daily_note(ME)
            self.assertIn("今天处理了", title)
            self.assertIn("按习惯处理 1 封", body)
            self.assertIsNone(r.app.review.daily_note(ME))                       # once a day
        self.assertEqual(r.app.state()["start_page"], "review")                  # the notification opens the page


class NotRoutineTests(unittest.TestCase):
    """review of what reached the memory (10-22)."""

    def test_a_habit_leaves_a_sign_in_notice_for_the_user(self):
        r = Rig(self)
        r.app.cfg["mode"]["write"] = False
        r.srv._add("INBOX", mail(90, "周末促销", "某某商城 <news@shop.example>", 0, "promo.shop.example"), set())
        r.srv._add("INBOX", mail(91, "新设备登录提醒（促销活动账户）", "某某商城 <news@shop.example>", 0, "promo.shop.example"), set())
        chat = FakeChat(triage_reply).__enter__()
        self.addCleanup(chat.__exit__)
        r.app.cfg["engine"]["base_url"] = chat.base_url
        p = mock.patch.object(memory, "auto_habits", lambda mem, acct: HABIT)
        p.start()
        self.addCleanup(p.stop)
        r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        self.assertNotIn("周末促销", r.inbox())                       # the habit's work
        self.assertIn("新设备登录提醒（促销活动账户）", r.inbox())         # a sign-in notice is never routine

    def test_changed_and_handled_in_one_step_says_so(self):
        r = ReviewTests.rig(self)
        it = next(i for i in r.app.store.items(ME, "pending") if i["subject"] == "双十一预热")
        r.app.override({"source": ME, "ids": [it["id"]], "action": "archive"})
        r.app.actions(ME).execute([it["id"]], "archive")
        r.app._acted(ME, [r.app.store.get(it["id"])], "archive")
        rec = r.app.mem.records(topic="mail-acted")[0]
        self.assertEqual(rec["data"]["after_override"], 1)


class ResultCodeTests(unittest.TestCase):
    rig = ReviewTests.rig

    def test_only_known_results_reach_the_page(self):
        """2026-10-21: mail the user archived elsewhere is settled as done_action "quiet" (or "expired"), and the
        page showed "you_quiet". Every result is one the page names; anything else is plain "done"."""
        r = self.rig()
        items = r.app.store.items(ME, "pending")
        quiet, odd = items[0], items[1]
        r.app.store.set_state([quiet["id"]], "done", "quiet")
        r.app.store.set_state([odd["id"]], "done", "something-new")
        got = {x["id"]: x["result"] for x in r.app.review.view(ME)["items"]}
        self.assertEqual(got[quiet["id"]], "elsewhere")
        self.assertEqual(got[odd["id"]], "you_done")
        known = set(review.YOURS) | {"done"}
        for res in got.values():
            self.assertTrue(res in ("elsewhere", "kept", "waiting", "suspicious", "unsorted")
                            or (res.startswith("you_") and res[4:] in known)
                            or (res.startswith("auto_") and res[5:] in review.AUTOS), res)


class SampleDayTests(unittest.TestCase):
    def test_the_sample_inbox_is_late_by_its_own_day(self):
        """luoke-mail#22 (10-23): 今日处理 dated the sample inbox by the real day and called 「Q3 数据能明早
        给我吗？」 overdue, while the inbox, on the sample's own day, listed it as due tomorrow."""
        from luoke_mail import samples
        from fakes import suggestion
        r = Rig(self)
        tomorrow = (samples.TODAY + dt.timedelta(days=1)).isoformat()
        with FakeChat(lambda body: (200, suggestion("reply", due_date=tomorrow))) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            wait(r.app, r.app.start_sync({"source": "demo"}))
        lates = {x["late"] for x in r.app.review.view("demo")["items"] if x["result"] == "waiting"}
        self.assertEqual(lates, {"due"})                                    # not 「已过期」 as by today's date


class JunkRulePageTests(unittest.TestCase):
    rig = ReviewTests.rig

    def test_a_junk_rules_mail_is_moved_to_junk_without_being_called_a_scam(self):
        r = self.rig()
        it = next(i for i in r.app.store.items(ME, "pending") if i["subject"] == "双十一预热")
        r.app.store.add_rule(ME, "list", "promo.shop.example", "spam", "某某商城")
        rule = r.app.store.rules(ME)[0]
        from luoke_mail import pipeline
        r.app.store.set_suggestion(it["id"], pipeline.rule_suggestion(it, rule), f"rule:{rule['id']}")
        view = r.app.public_item(r.app.store.get(it["id"]))
        self.assertEqual((view["action"], view["suspicious"], view["junk"]), ("ignore", False, True))
        other = next(i for i in r.app.store.items(ME, "pending") if i["id"] != it["id"])
        self.assertFalse(r.app.public_item(other)["junk"])


class OlderUnreadTests(unittest.TestCase):
    """Unread mail from before the range is sorted once, as idle work, then marked read and shown in 今日处理 (the
    owner, 2026-10-20)."""

    rig = ReviewTests.rig

    def job(self):
        return {"cancel": False, "stage": "", "done": 0, "total": 0}

    def test_queued_once_and_sorted_as_idle_work_not_by_the_sync(self):
        r = self.rig()
        q = r.app.store.cache_get(ME, "older")[0]
        self.assertEqual(q["total"], 2)                                         # 旧推送一, 旧推送三: unread, before the range
        subjects = {it["subject"] for it in r.app.store.items(ME, None)}
        self.assertNotIn("旧推送一", subjects)                                   # the sync itself does not wait on them
        self.assertIn("older", [w[0] for w in r.app._idle_work()])
        self.assertEqual(r.app._older_slice(ME, self.job())["sorted"], 2)
        older = {it["subject"]: it for it in r.app.store.items(ME, None) if it.get("older")}
        self.assertEqual(set(older), {"旧推送一", "旧推送三"})                     # read old mail is left alone
        self.assertTrue(all(it["analyzed_at"] for it in older.values()))
        self.assertIn("\\Seen", r.srv.find("INBOX", "旧推送一")["flags"])        # sorted, so marked read
        self.assertNotIn("older", [w[0] for w in r.app._idle_work()])
        # mail the user marks unread later is theirs: not queued again
        r.srv.find("INBOX", "旧推送二")["flags"].discard("\\Seen")
        r.app.sync_mailbox(ME, self.job())
        self.assertEqual(r.app.store.cache_get(ME, "older")[0]["uids"], [])
        self.assertNotIn("旧推送二", {it["subject"] for it in r.app.store.items(ME, None)})

    def test_in_todays_review_on_their_own_with_how_late_they_are(self):
        r = self.rig()
        r.app._older_slice(ME, self.job())
        v = r.app.review.view(ME)
        older = [x for x in v["items"] if x["older"]]
        self.assertEqual(len(older), 2)
        self.assertEqual(v["counts"]["older"], 2)
        self.assertTrue(all(x["result"] == "waiting" for x in older), older)
        late = {x["subject"]: x["late"] for x in older}
        self.assertEqual(late["旧推送一"], "old")                                 # months unanswered
        # sent a year ago, the model's date a month from now (no year in the mail) counts from the day it was sent:
        # last year's, long past (triage.settle_dates, 10-23)
        self.assertEqual(late["旧推送三"], "overdue")
        self.assertFalse(any(x["older"] for x in v["items"] if x["subject"] == "双十一预热"))

    def test_not_also_in_cleanup(self):
        r = self.rig()
        r.app._older_slice(ME, self.job())
        wait(r.app, r.app.start_old_scan({"source": ME, "scope": "inbox"}))
        rows = {x["subject"] for x in r.app.store.old_rows(ME, "inbox")}
        self.assertNotIn("旧推送一", rows)
        self.assertIn("旧推送二", rows)                                           # read, never sorted: still 清理's

    def test_late_says_overdue_and_due(self):
        today = dt.date(2026, 10, 20)
        late = review.Review._late
        it = {"state": "pending", "action": "reply", "date": "2026-10-19 10:00"}
        self.assertEqual(late({**it, "due_date": "2026-10-01"}, today), "overdue")
        self.assertEqual(late({**it, "due_date": "2026-10-22"}, today), "due")
        self.assertIsNone(late({**it, "due_date": None}, today))
        self.assertEqual(late({**it, "date": "2026-06-01 10:00"}, today), "old")
        self.assertIsNone(late({**it, "action": "archive", "due_date": "2026-10-01"}, today))


class OwnMailboxTests(unittest.TestCase):
    """ADR 0016 第 5 条 (2026-10-19): in 螺壳's own mailbox, reading, archiving and deleting are done
    unasked -- each listed in 今日处理 and undone there."""

    def test_sorted_mail_is_archived_or_trashed_by_itself_and_undone_from_the_review(self):
        r = Rig(self)
        r.app.cfg["mode"]["write"] = False
        r.srv._add("INBOX", mail(92, "冬季促销", "某某商城 <news@shop.example>", 0, "promo.shop.example"), set())
        chat = FakeChat(triage_reply).__enter__()
        self.addCleanup(chat.__exit__)
        r.app.cfg["engine"]["base_url"] = chat.base_url
        r.app.set_own({"source": ME, "own": True})
        self.assertTrue(r.app.accounts_view()[0]["own"])
        res = r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        self.assertTrue(any(a["action"] == "trash" for a in res["auto"]))
        self.assertNotIn("冬季促销", r.inbox())                                  # a promotion: to the trash
        self.assertIn("冬季促销", r.srv.subjects("[Gmail]/Trash"))
        self.assertIn("双十一预热", r.inbox())                                   # needs a reply: left for the user
        x = next(x for x in r.app.review.view(ME)["items"] if x["subject"] == "冬季促销")
        self.assertEqual(x["result"], "auto_trash")
        r.app.review.act({"source": ME, "id": x["id"], "op": "undo"})
        self.assertIn("冬季促销", r.inbox())
        r.app.set_own({"source": ME, "own": False})
        self.assertFalse(r.app.own_mailbox(ME))


class LabelTests(unittest.TestCase):
    """The owner, 2026-10-19: labelling is one of the things a confirmed habit does by itself; what the user labels
    in Gmail is learnt from."""

    def sync(self, r):
        return r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})

    def rig(self, habits):
        r = Rig(self)
        r.srv._add("INBOX", mail(91, "秋季促销", "某某商城 <news@shop.example>", 0, "promo.shop.example"), set())
        chat = FakeChat(triage_reply).__enter__()
        self.addCleanup(chat.__exit__)
        r.app.cfg["engine"]["base_url"] = chat.base_url
        p = mock.patch.object(memory, "auto_habits", lambda mem, acct: habits)
        p.start()
        self.addCleanup(p.stop)
        return r

    def test_a_label_habit_labels_once_and_is_undone_one_message_at_a_time(self):
        r = self.rig([{**HABIT[0], "note_id": 8, "action": "label", "label": "购物"}])
        self.sync(r)
        m = r.srv.find("INBOX", "秋季促销")
        self.assertIn(utf7_encode("购物"), m["labels"])                       # labelled, still in the inbox
        x = next(x for x in r.app.review.view(ME)["items"] if x["subject"] == "秋季促销")
        self.assertEqual((x["result"], x["label"]), ("auto_label", "购物"))
        self.assertEqual(self.sync(r)["auto"], [])                            # not labelled again
        r.app.review.act({"source": ME, "id": x["id"], "op": "undo"})
        self.assertNotIn(utf7_encode("购物"), r.srv.find("INBOX", "秋季促销")["labels"])
        self.assertEqual(self.sync(r)["auto"], [])                            # taken back: not again
        self.assertFalse(any(x["data"].get("label") == "购物" and x["data"]["where"] == "elsewhere"
                             for x in r.app.mem.records(topic="mail-acted")))   # its own label is not the user's

    def test_labels_the_user_puts_on_in_gmail_are_learnt_from(self):
        r = self.rig([])
        mine = lambda: [x for x in r.app.mem.records(topic="mail-acted") if memory.from_account(x, ME) and x["id"] > start]  # noqa: E731
        start = max([x["id"] for x in r.app.mem.records()] or [0])
        self.sync(r)
        self.sync(r)                                                          # the first look at it notes its labels
        self.assertEqual(mine(), [])
        r.srv.find("INBOX", "秋季促销").setdefault("labels", []).append(utf7_encode("购物"))
        self.sync(r)
        got = [(x["data"]["action"], x["data"].get("label"), x["data"]["where"]) for x in mine()]
        self.assertEqual(got, [("label", "购物", "elsewhere")])
        m = r.srv.find("INBOX", "双十一预热")                                  # labelled and archived in one move
        r.srv._gmail_move("INBOX", m, "[Gmail]/All Mail", False)
        r.srv.find("[Gmail]/All Mail", "双十一预热").setdefault("labels", []).append(utf7_encode("工作"))
        self.sync(r)
        got = sorted((x["data"]["action"], x["data"].get("label") or "") for x in mine())
        self.assertEqual(got, [("archive", ""), ("label", "工作"), ("label", "购物")])


if __name__ == "__main__":
    unittest.main()
