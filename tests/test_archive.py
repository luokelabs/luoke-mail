"""清理 → 已归档的邮件: every archived message is judged by the model (batches, mostly one sender each), the
verdicts are kept, judging gives way to everything else, and the trash runs in checked batches whose undo
also gives each message its labels back."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import actions, profiler, triage  # noqa: E402
from fakes import FakeChat  # noqa: E402
from test_old_mail import ME, Rig, mail, wait  # noqa: E402

FINANCE = "&jSJSoQ-"          # 财务, as Gmail sends label names (modified UTF-7)
ARCHIVED = [  # (subject, sender, Gmail category, labels, starred)
    ("618 狂欢提前购", "某东商城 <service@mall.example>", "promotions", [], False),
    ("订单 1001 电子发票", "某东商城 <service@mall.example>", "updates", [], False),
    ("您的订单已发货", "某东商城 <service@mall.example>", "updates", [], False),
    ("验证码 4821，5 分钟内有效", "某平台 <no-reply@verify.example>", "updates", [], False),
    ("信用卡 9 月电子账单", "某某银行 <notice@bank.example>", "updates", [FINANCE], False),
    ("租房合同（签字版）", "房东 <landlord@mail.example>", None, [], True),
]


def judge(body):
    """A fake model: kinds by keywords in each line of the batch."""
    lines = [ln for ln in body["messages"][1]["content"].splitlines() if ln[:1].isdigit()]
    kind = lambda ln: ("receipt" if "发票" in ln or "账单" in ln else "promotion" if "狂欢" in ln else
                       "notification" if "验证码" in ln or "发货" in ln else "other")
    return 200, json.dumps({"items": [{"n": i, "kind": kind(ln)} for i, ln in enumerate(lines, 1)]}, ensure_ascii=False)


class HeldTests(unittest.TestCase):
    def test_starred_or_filed_under_a_keep_label_is_never_part_of_a_clean_up(self):
        from luoke_mail import triage
        self.assertTrue(triage.held({"flagged": True, "labels": []}))
        self.assertTrue(triage.held({"flagged": False, "labels": ["收藏"]}))
        self.assertTrue(triage.held({"labels": ["Important Docs"]}))
        self.assertFalse(triage.held({"flagged": False, "labels": ["广告", "订阅"]}))


class ArchiveRig(Rig):
    def __init__(self, test, **kw):
        super().__init__(test, **kw)
        for i, (subject, sender, cat, labels, starred) in enumerate(ARCHIVED):
            uid = self.srv._add("[Gmail]/All Mail", mail(100 + i, subject, sender, 200 + i), {"\\Seen"} | ({"\\Flagged"} if starred else set()))
            m = self.srv.folders["[Gmail]/All Mail"][-1]
            m.update(category=cat, labels=labels)
            assert m["uid"] == uid
        self.srv.folders[FINANCE] = []

    def scan(self, source="mailbox"):
        job = wait(self.app, self.app.start_old_scan({"source": source, "scope": "archive"}))
        assert job["status"] == "done", job
        return job["result"]

    def judge(self, acct=ME, reply=judge):
        with FakeChat(reply) as chat:
            self.app.cfg["engine"]["base_url"] = chat.base_url
            self.app.store.cache_set(acct, "judge:archive", {"on": True})
            return self.app._judge_slice(acct, "archive", {"cancel": False})

    def view(self, acct=ME):
        return self.app.old_view(acct, "archive")

    def trash(self, keys, source="mailbox"):
        return wait(self.app, self.app.start_old_apply({"source": source, "scope": "archive", "keys": keys}))["result"]

    def key(self, kind, addr, acct=ME):
        """The group key for one verdict of one sender, as the view names it."""
        return next(g["key"] for g in self.view(acct)["groups"] if g["kind"] == kind and g["addr"] == addr)

    def all_mail(self):
        return sorted(self.srv.subjects("[Gmail]/All Mail"))


class IdleJobTests(unittest.TestCase):
    """In 螺壳邮件助手.app the idle work is 螺壳主控's job `idle` (2026-10-16; ADR 0006): the app carries
    on at once only with what the user just started, the job takes the rest when the Mac is quiet."""

    def test_the_app_leaves_idle_work_to_the_hub_except_what_the_user_just_started(self):
        r = ArchiveRig(self)
        r.scan()
        r.app.store.cache_set(ME, "judge:archive", {"on": True})        # switched on earlier: work waiting
        r.app.store.cache_update(ME, "profile", at=time.time(), records_v=profiler.RECORDS_V)   # the weekly records: written already
        r.app.cfg["learn"]["on"] = False                                     # 了解你 paused: only the work tested here
        r.app.hub_only = True
        with FakeChat(judge) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            self.assertFalse(r.app.idle_pending())
            r.app.idle_step()
            self.assertIsNone(r.app.running_job())                         # left to the job
            r.app.idle_step(prefer="judge")                                # 开始分析: at once, here
            job = r.app.running_job()
            self.assertEqual(job["kind"], "judge")
            wait(r.app, job)
        self.assertEqual(r.app.store.old_unjudged(ME, "archive", triage.held), 0)

    def test_the_job_does_the_idle_work_for_the_mailboxes_and_stops_cleanly(self):
        from luoke_mail import desktop, server
        r = ArchiveRig(self)
        r.scan()
        r.app.store.cache_set(ME, "judge:archive", {"on": True})
        r.scan("demo")
        r.app.store.cache_set("demo", "judge:archive", {"on": True})         # the demo: not the job's
        r.app.hub_only = True
        held = desktop.InstanceLock(server.config.CONFIG_DIR, "idle.lock")   # the app is at it: the job steps aside
        self.assertTrue(held.acquire())
        self.assertEqual(server.run_idle_job(r.app), 0)
        self.assertGreater(r.app.store.old_unjudged(ME, "archive", triage.held), 0)
        held.release()
        with FakeChat(judge) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            self.assertEqual(server.run_idle_job(r.app, name="digest"), 0)          # not this job's kind of work
            self.assertGreater(r.app.store.old_unjudged(ME, "archive", triage.held), 0)
            self.assertEqual(server.run_idle_job(r.app, name="clean"), 0)
        self.assertEqual(r.app.store.old_unjudged(ME, "archive", triage.held), 0)
        self.assertGreater(r.app.store.old_unjudged("demo", "archive", triage.held), 0)
        self.assertFalse(r.app.in_job and r.app.idle_accounts() != [ME])


class ArchiveTests(unittest.TestCase):
    def test_scan_reads_archived_mail_with_category_and_labels(self):
        r = ArchiveRig(self)
        v = r.scan()
        self.assertEqual((v["total"], v["flagged"], v["judged"]), (5, 1, 0))     # inbox mail is not archived; starred stays out
        rows = {x["subject"]: x for x in r.app.store.old_rows(ME, "archive")}
        self.assertEqual(rows["信用卡 9 月电子账单"]["labels"], ["财务"])
        self.assertEqual(rows["618 狂欢提前购"]["category"], "promotions")
        self.assertNotIn("本周例会改到周四", rows)
        self.assertTrue(all(g["kind"] == "pending" for g in v["groups"]))

    def test_every_message_judged_and_kept_across_scans(self):
        r = ArchiveRig(self)
        r.scan()
        self.assertEqual(r.judge()["judged"], 5)
        v = r.view()
        kinds = sorted((g["kind"], g["addr"], g["count"]) for g in v["groups"])
        self.assertEqual(kinds, [("notification", "no-reply@verify.example", 1), ("notification", "service@mall.example", 1),
                                 ("promotion", "service@mall.example", 1), ("receipt", "notice@bank.example", 1),
                                 ("receipt", "service@mall.example", 1)])                   # one sender, three verdicts
        bank = next(g for g in v["groups"] if g["addr"] == "notice@bank.example")
        self.assertEqual(bank["labels"], ["财务"])
        r.scan()                                                                            # a re-scan keeps verdicts
        self.assertEqual(r.view()["judged"], 5)

    def test_the_model_sees_labels_and_categories(self):
        r = ArchiveRig(self)
        r.scan()
        seen = []
        r.judge(reply=lambda b: (seen.append(b["messages"][1]["content"]), judge(b))[1])
        self.assertIn("标签：财务", seen[0])
        self.assertIn("Gmail 分类：推广", seen[0])

    def test_unusable_answers_keep_everything(self):
        r = ArchiveRig(self)
        r.scan()
        r.judge(reply=lambda b: (200, "我觉得都可以删"))
        self.assertEqual({g["kind"] for g in r.view()["groups"]}, {"other"})

    def test_model_off_is_reported_and_retried_later(self):
        r = ArchiveRig(self)
        r.scan()
        r.app.cfg["engine"]["base_url"] = "http://127.0.0.1:9/v1"
        r.app.store.cache_set(ME, "judge:archive", {"on": True})
        r.app.store.cache_update(ME, "profile", at=time.time(), records_v=profiler.RECORDS_V)   # the weekly records: written already
        r.app.cfg["learn"]["on"] = False                                     # 了解你 paused: only the work tested here
        self.assertTrue(r.app.idle_pending())
        r.app._judge_slice(ME, "archive", {"cancel": False})
        v = r.view()
        self.assertIn("无法连接模型服务", v["judging"]["problem"])
        self.assertFalse(r.app.idle_pending())                      # waits a minute before trying again

    def test_judging_gives_way_to_a_sync(self):
        r = ArchiveRig(self)
        r.scan()
        gate = threading.Event()

        def slow(body):
            gate.wait(5)
            return judge(body)
        with FakeChat(slow) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            r.app.store.cache_set(ME, "judge:archive", {"on": True})
            with unittest.mock.patch.object(triage, "OLD_BATCH", 1):
                r.app.idle_step()
                time.sleep(0.2)
                self.assertEqual(r.app.running_job()["kind"], "judge")
                threading.Timer(0.3, gate.set).start()
                job = r.app.start_sync({"source": "mailbox"})               # asks judging to stop, then runs
                self.assertEqual(job["kind"], "sync")
            wait(r.app, job)
        self.assertLess(r.app.store.old_unjudged(ME, "archive"), 5)                # it had judged some, and will resume

    def test_trash_in_checked_batches_and_undo_restores_labels(self):
        r = ArchiveRig(self)
        r.scan()
        r.judge()
        v = r.view()
        delete = [g["key"] for g in v["groups"] if g["kind"] in triage.DELETE_KINDS]
        keys = delete + [r.key("receipt", "notice@bank.example")]           # also the labelled statement
        res = r.trash(keys)
        self.assertEqual((res["moved"], res["stuck"], res["ok"]), (4, 0, True))
        self.assertNotIn("618 狂欢提前购", r.all_mail())
        self.assertIn("618 狂欢提前购", r.srv.subjects("[Gmail]/Trash"))
        self.assertIn("订单 1001 电子发票", r.all_mail())                       # the receipt was not ticked
        self.assertEqual(r.view()["total"], 1)
        r.app.actions("mailbox").undo(res["log_id"])
        self.assertIn("618 狂欢提前购", r.all_mail())
        self.assertEqual(r.srv.subjects(FINANCE), ["信用卡 9 月电子账单"])      # its label is back
        self.assertEqual(r.view()["total"], 5)

    def test_unticked_suggestions_stay_unticked(self):
        r = ArchiveRig(self)
        r.scan()
        r.judge()
        keep = sorted([r.key("notification", "no-reply@verify.example"), r.key("notification", "service@mall.example")])
        r.trash([r.key("promotion", "service@mall.example")])
        self.assertEqual(sorted(r.view()["keep"]), keep)

    def test_needs_processing_mode(self):
        r = ArchiveRig(self)
        r.scan()
        r.judge()
        key = r.key("promotion", "service@mall.example")
        r.app.cfg["mode"]["write"] = False
        with self.assertRaisesRegex(ValueError, "处理模式"):
            r.app.start_old_apply({"source": "mailbox", "scope": "archive", "keys": [key]})

    def test_demo(self):
        r = ArchiveRig(self)
        v = r.scan("demo")
        self.assertEqual(v["flagged"], 1)
        r.judge(actions.DEMO)
        v = r.view(actions.DEMO)
        self.assertEqual(v["judged"], v["total"])
        delete = [g["key"] for g in v["groups"] if g["kind"] in triage.DELETE_KINDS]
        res = r.trash(delete, "demo")
        self.assertTrue(res["ok"])
        left = r.view(actions.DEMO)
        self.assertFalse([g for g in left["groups"] if g["kind"] in triage.DELETE_KINDS])
        r.app.actions("demo").undo(res["log_id"])
        self.assertEqual(r.view(actions.DEMO)["total"], v["total"])


if __name__ == "__main__":
    unittest.main()


class SenderCheckTests(unittest.TestCase):
    """订阅体检: before suggesting to unsubscribe, the model reads the sender's recent subjects."""

    def run_check(self, r):
        job = wait(r.app, r.app.start_subscriptions({"source": "demo", "days": 60}))
        self.assertEqual(job["status"], "done", job)
        return {s["key"]: s for s in job["result"]["senders"]}, job["result"]

    def test_statements_and_alerts_are_not_suggested_for_unsubscribing(self):
        r = Rig(self)
        asked = []

        def reply(body):
            lines = [ln for ln in body["messages"][1]["content"].splitlines() if ln[:1].isdigit()]
            asked.extend(lines)
            kind = lambda ln: "transactional" if "论坛" in ln else "marketing"
            return 200, json.dumps({"items": [{"n": i, "kind": kind(ln)} for i, ln in enumerate(lines, 1)]})
        with FakeChat(reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            rows, result = self.run_check(r)
            self.assertIsNone(result["model_note"])
            forum = rows["from:notice@forum.example"]
            self.assertEqual(forum["verdict"], "important")
            self.assertIn("账单、订单和交易记录", forum["why"])
            self.assertEqual(rows["list:promo.mall.example"]["verdict"], "unsubscribe")
            self.assertTrue(any("你关注的话题" in ln for ln in asked))           # it saw the subjects
            n = len(asked)
            self.run_check(r)
            self.assertEqual(len(asked), n)                                     # remembered per sender

    def test_unsure_or_unanswered_is_not_suggested_for_unsubscribing_on_the_model_s_word(self):
        """The prompt says 拿不准 is other (with transactional, security): the app used to keep only the latter two
        and suggested unsubscribing from senders the model was unsure about. One it said nothing about keeps the
        suggestion from reading alone, and the page says so."""
        r = Rig(self)

        def reply(body):
            lines = [ln for ln in body["messages"][1]["content"].splitlines() if ln[:1].isdigit()]
            kind = lambda ln: "other" if "论坛" in ln else "marketing"
            return 200, json.dumps({"items": [{"n": i, "kind": kind(ln)} for i, ln in enumerate(lines, 1)
                                              if "promo@mall" not in ln]})
        with FakeChat(reply) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            rows, result = self.run_check(r)
        forum = rows["from:notice@forum.example"]
        self.assertEqual(forum["verdict"], "important")
        self.assertIn("不好归类", forum["why"])
        self.assertEqual(rows["list:promo.mall.example"]["verdict"], "unsubscribe")
        self.assertIn("有 1 个发件人未能分析", result["model_note"])

    def test_without_the_model_it_says_so(self):
        r = Rig(self)
        rows, result = self.run_check(r)                                        # the rig's model URL is dead
        self.assertIn("仅依据收信和阅读情况", result["model_note"])
        self.assertEqual(rows["list:promo.mall.example"]["verdict"], "unsubscribe")


class MigrationTests(unittest.TestCase):
    def test_0_6_archive_table_carries_over(self):
        import sqlite3
        from luoke_mail.store import Store
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE cache (account TEXT NOT NULL, name TEXT NOT NULL, value TEXT, at TEXT, PRIMARY KEY (account, name));
            CREATE TABLE archive (account TEXT NOT NULL, message_id TEXT NOT NULL, uid TEXT, folder TEXT, from_name TEXT,
              from_addr TEXT, list_id TEXT, bulk INTEGER DEFAULT 0, subject TEXT, date TEXT, size INTEGER DEFAULT 0,
              seen INTEGER DEFAULT 0, flagged INTEGER DEFAULT 0, category TEXT, labels_json TEXT, kind TEXT, judged_at TEXT,
              model TEXT, state TEXT NOT NULL DEFAULT 'archived', PRIMARY KEY (account, message_id));
            INSERT INTO archive (account, message_id, uid, folder, subject, kind, state) VALUES ('me', '<a@x>', '9', 'All', 's', 'receipt', 'archived');
            INSERT INTO cache VALUES ('me', 'archive_scan', '{"at": "x", "folder": "All"}', 'x'), ('me', 'judge', '{"on": true}', 'x');
        """)
        db.commit()
        db.close()
        st = Store(path)
        self.addCleanup(st.close)
        rows = st.old_rows("me", "archive")
        self.assertEqual([(r["message_id"], r["kind"], r["state"]) for r in rows], [("<a@x>", "receipt", "here")])
        self.assertEqual(st.cache_get("me", "scan:archive")[0]["folder"], "All")
        self.assertTrue(st.cache_get("me", "judge:archive")[0]["on"])
