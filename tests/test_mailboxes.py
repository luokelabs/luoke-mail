"""Several mailboxes in one luoke: each keeps its own list, rules, 清理 and 精选 under its address; background
sorting goes through all of them and says which one a notification is about; one that cannot be reached does
not hold up the others; removing one deletes only its own local data; the memory is the person's, shared."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import config, server  # noqa: E402
from luoke_mail.credstore import FileStore  # noqa: E402
from luoke_mail.mailbox import Mailbox, MailError  # noqa: E402
from luoke_mail.store import Store  # noqa: E402
from fakes import FakeChat, FakeIMAP  # noqa: E402
from test_background import FakeDesktop, private_config, triage_reply  # noqa: E402
from test_old_mail import mail  # noqa: E402

WORK, HOME = "Work@Example.com", "me@gmail.com"


class TwoMailboxes:
    def __init__(self, test):
        private_config(test)
        d = tempfile.mkdtemp()
        self.srv = {}
        for addr, mails in ((WORK, [(mail(1, "周会改到周四", "王总 <boss@corp.example>", 2), False)]),
                            (HOME, [(mail(2, "机票确认", "某程 <trip@travel.example>", 1), False),
                                    (mail(3, "账单已出", "某银行 <bill@bank.example>", 3), False)])):
            self.srv[addr.lower()] = FakeIMAP(mails, user=addr, flavour="gmail").__enter__()
            test.addCleanup(self.srv[addr.lower()].__exit__)
        self.app = a = server.App(secret_store=FileStore(os.path.join(d, "c.json")), state_store=Store(os.path.join(d, "s.db")),
                                  desktop_=FakeDesktop())
        test.addCleanup(a.store.close)
        self.desk = a.desktop
        for addr in (WORK, HOME):
            a.save_mail({"provider": "gmail", "address": addr, "secret": "secret"})
        self.down = set()

        def open_mailbox(acct=None):
            acct = acct or a.mail_accounts()[0]
            if acct in self.down:
                raise MailError("连接邮箱服务器超时，请检查网络后重试。")
            return Mailbox("gmail", a.address(acct), "secret", host="127.0.0.1", port=self.srv[acct].port, ssl=False, timeout=5)
        a.open_mailbox = open_mailbox
        self.chat = FakeChat(triage_reply).__enter__()
        test.addCleanup(self.chat.__exit__)
        a.cfg["engine"] = {"base_url": self.chat.base_url, "model": "test-model", "engine_name": None, "has_api_key": False}

    def subjects(self, acct):
        return sorted(i["subject"] for i in self.app.store.items(acct.lower(), None))


class MailboxTests(unittest.TestCase):
    def test_each_mailbox_keeps_its_own_mail(self):
        r = TwoMailboxes(self)
        a = r.app
        self.assertEqual(a.mail_accounts(), [WORK.lower(), HOME])
        self.assertEqual([x["key"] for x in a.state()["accounts"]], [WORK.lower(), HOME])
        self.assertEqual(a.account("mailbox"), WORK.lower())                      # pages from before: the first one
        self.assertEqual(a.account(HOME), HOME)
        with self.assertRaises(ValueError):
            a.account("nobody@example.com")
        for acct in (WORK.lower(), HOME):
            a.sync_mailbox(acct, {"cancel": False})
        self.assertEqual(r.subjects(WORK), ["周会改到周四"])
        self.assertEqual(r.subjects(HOME), ["机票确认", "账单已出"])
        it = a.public_item(a.store.items(HOME, None)[0])
        self.assertTrue(it["open_url"].startswith(f"https://mail.google.com/mail/u/?authuser={HOME}#all/"))
        it = a.public_item(a.store.items(WORK.lower(), None)[0])
        self.assertIn("authuser=Work@Example.com", it["open_url"])                 # the address as typed

    def test_clean_up_moves_only_the_mail_of_the_mailbox_it_was_asked_for(self):
        """Issue #1 (2026-10-16): 清理 on the second mailbox went to the first, by UID."""
        from test_old_mail import wait
        r = TwoMailboxes(self)
        a = r.app
        a.cfg["mode"]["write"] = True
        work, home = r.srv[WORK.lower()], r.srv[HOME]
        for n in (10, 11):                                  # UIDs 2, 3 at work
            work._add("INBOX", mail(n, f"老合同{n}", "李律师 <lawyer@firm.example>", 90 + n), {"\\Seen"})
        for n in (12, 13):                                  # UIDs 3, 4 at home: 3 is in both
            home._add("INBOX", mail(n, f"旧推送{n}", "某某商城 <news@shop.example>", 60 + n, "promo.shop.example"), set())
        v = wait(a, a.start_old_scan({"source": HOME, "scope": "inbox"}))["result"]
        job = wait(a, a.start_old_apply({"source": HOME, "scope": "inbox", "keys": [g["key"] for g in v["groups"]]}))
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["result"]["moved"], 2)
        self.assertCountEqual(work.subjects("INBOX"), ["周会改到周四", "老合同10", "老合同11"])      # untouched
        self.assertCountEqual(home.subjects("INBOX"), ["机票确认", "账单已出"])
        self.assertEqual([e["account"] for e in a.store.log_entries(HOME)][:1], [HOME])
        with self.assertRaisesRegex(ValueError, "哪个邮箱"):
            a.actions("mailbox")                            # with several, a mailbox must be named
        with self.assertRaisesRegex(Exception, "不属于这个邮箱"):
            a.actions(WORK).clear_old(a.store.old_rows(HOME, "inbox", None), [])

    def test_memory_records_say_which_mailbox_they_come_from(self):
        """Top-level ADR 0010 (2026-10-16): each record says which mailbox it comes from -- an opaque key, never the
        address -- and the same message, sender or label in two mailboxes is two records. Sharing is unchanged."""
        from luoke_mail import memory
        from luoke_mail.learner import Learner
        from luoke_mail.profiler import Profiler
        r = TwoMailboxes(self)
        mem = r.app.mem
        work, home = memory.account_key(WORK), memory.account_key(HOME)
        self.assertNotEqual(work, home)
        self.assertEqual(memory.account_key(WORK.lower()), work)
        for k in (work, home):
            self.assertNotIn("example", k)
            self.assertNotIn("gmail", k)
        memory._salts.clear()                                      # read again from the data folder: the same keys
        self.assertEqual(memory.account_key(HOME), home)
        self.assertEqual(oct(os.stat(os.path.join(config.CONFIG_DIR, "account-salt")).st_mode & 0o777), "0o600")

        sent = [Learner._record(mem, acct, "user", "sent-mail", "<same@x.example>", "你发出的邮件：「周报」", None)
                for acct in (WORK.lower(), HOME)]
        self.assertNotEqual(sent[0], sent[1])
        self.assertEqual(Learner._record(mem, HOME, "user", "sent-mail", "<same@x.example>", "你发出的邮件：「周报」", None), sent[1])
        written = [Profiler._keep(mem, {"topic": "own-label", "ref": "财务", "record": "标签「财务」", "account": acct})
                   for acct in (WORK.lower(), HOME, HOME)]
        self.assertEqual(written, [True, True, False])                    # one per mailbox
        Profiler._keep(mem, {"topic": "own-site", "ref": "https://me.example", "record": "你的网站「我」"})
        label = {(x["data"] or {}).get("account"): x["id"] for x in mem.records(topic="own-label")}
        site = mem.records(topic="own-site")[0]["id"]
        got = {x["id"]: (x["data"] or {}).get("account") for x in mem.records() if x["topic"] in ("sent-mail", "own-label", "own-site")}
        self.assertEqual(got, {sent[0]: work, sent[1]: home, label[work]: work, label[home]: home, site: None})

        mem.forget_records(match={"account": work})                 # what one mailbox contributed can be forgotten
        self.assertEqual({x["id"] for x in mem.records() if x["topic"] in ("sent-mail", "own-label", "own-site")}, {sent[1], label[home], site})

    def test_reading_the_mailbox_waits_its_turn_behind_the_users_own_work(self):
        """Issue #4 (2026-10-17): 清理's reading asked for during a sync was turned away with no way back. Reading
        waits its turn (shown on every page), the same request again joins it; what changes the mailbox does not
        wait but says what to wait for."""
        import threading
        import time
        r = TwoMailboxes(self)
        a = r.app
        go = threading.Event()
        sync = a._start_job("sync", lambda job: go.wait(10) and {"ok": True}, account=HOME)
        scan = a.start_old_scan({"source": HOME, "scope": "inbox"})
        self.assertEqual(scan["stage"], "等待「同步邮件」完成…")
        self.assertTrue(scan["queued"])
        self.assertTrue(a.start_old_scan({"source": HOME, "scope": "inbox"})["joined"])        # no second one
        other = a.start_old_scan({"source": WORK, "scope": "inbox"})                            # another mailbox: its own
        self.assertNotEqual(other["id"], scan["id"])
        act = a.activity()
        self.assertEqual(act["job"]["id"], sync["id"])
        self.assertEqual([j["id"] for j in act["waiting_jobs"]], [scan["id"], other["id"]])
        with self.assertRaisesRegex(ValueError, "「同步邮件」正在进行，完成后请再试一次"):
            a._start_job("clean", lambda job: None)
        go.set()
        for _ in range(200):
            if all(a.jobs[j]["status"] != "running" for j in (sync["id"], scan["id"], other["id"])):
                break
            time.sleep(0.05)
        self.assertEqual([a.jobs[j]["status"] for j in (sync["id"], scan["id"], other["id"])], ["done", "done", "done"])
        self.assertEqual(a.activity()["waiting_jobs"], [])

    def test_a_sync_joins_background_sorting_only_at_its_own_mailbox(self):
        """Issue #4 (10-21/22): with background sorting at one mailbox, 同步新邮件 in another was folded into
        it and never ran. It waits its turn now, shown with its own mailbox; at the same mailbox it still joins."""
        import threading
        import time
        r = TwoMailboxes(self)
        a = r.app
        go = threading.Event()
        auto = a._start_job("auto", lambda job: go.wait(10) and {"ok": True})
        a.jobs[auto["id"]]["account"] = WORK.lower()                 # background sorting at the work mailbox
        self.assertTrue(a.start_sync({"source": WORK})["joined"])
        home = a.start_sync({"source": HOME})
        self.assertFalse(home.get("joined"))
        self.assertTrue(home["queued"])
        self.assertEqual(home["account"], HOME.lower())
        self.assertEqual(a.activity()["waiting_jobs"], [{"id": home["id"], "kind": "sync", "account": HOME.lower()}])
        self.assertEqual(a.start_sync({"source": HOME})["id"], home["id"])     # asked again: the same one
        go.set()
        for _ in range(200):
            if a.jobs[home["id"]]["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(a.jobs[home["id"]]["status"], "done")
        self.assertIn("机票确认", r.subjects(HOME))

    def test_removing_a_mailbox_takes_its_rules_and_clearing_everything_stops_the_work_first(self):
        """The review of 2026-10-17: a removed mailbox's rules came back when it was added again; 全部清除 left a
        running sync writing into the new store, and the account key and the log behind."""
        import threading
        import time
        from luoke_mail import memory
        r = TwoMailboxes(self)
        a = r.app
        a._add_rule(HOME, "sender", "news@shop.example", "archive")
        self.assertTrue(a.store.rules(HOME))
        a.remove_mail({"key": HOME})
        self.assertEqual(a.store.rules(HOME), [])
        memory.account_key(WORK)                                           # the salt exists now
        ran = threading.Event()

        def busy(job):
            ran.set()
            while not job["cancel"]:
                time.sleep(0.05)
        j = a._start_job("sync", busy, account=WORK.lower())
        ran.wait(5)
        a.forget({})
        self.assertNotEqual(a.jobs[j["id"]]["status"], "running")          # stopped before the data went
        self.assertFalse(os.path.exists(os.path.join(config.CONFIG_DIR, "account-salt")))

    def test_background_sorting_goes_through_all_and_one_down_does_not_hold_up_the_others(self):
        r = TwoMailboxes(self)
        a = r.app
        a.cfg["auto"]["enabled"] = True
        self.assertEqual(a.auto_accounts(), [WORK.lower(), HOME])
        r.down.add(WORK.lower())
        out = a.auto.run({"cancel": False, "stage": ""})
        self.assertEqual(out["new"], 2)                                            # the other one was sorted
        self.assertEqual(r.subjects(HOME), ["机票确认", "账单已出"])
        self.assertTrue(any(n[0].endswith(f"（{HOME}）") for n in r.desk.notes))   # says which mailbox
        self.assertIn(f"{WORK}：", a.auto_view()["last"]["text"])
        r.down.add(HOME)
        with self.assertRaises(MailError):                                        # none could be reached
            a.auto.run({"cancel": False, "stage": ""})

    def test_idle_work_looks_at_every_mailbox(self):
        r = TwoMailboxes(self)
        a = r.app
        a.store.cache_set(HOME, "judge:archive", {"on": True})
        a.store.old_sync(HOME, "archive", [{"message_id": "<x@y>", "uid": "1", "folder": "[Gmail]/All Mail", "from_addr": "a@b.example",
                                            "subject": "s", "date": "2026-01-01 08:00"}])
        self.assertEqual(a._judge_next(), (HOME, "archive"))

    def test_adding_changing_and_removing(self):
        r = TwoMailboxes(self)
        a = r.app
        with self.assertRaises(ValueError):                                       # a new mailbox needs its password
            a.save_mail({"provider": "qq", "address": "new@qq.com"})
        with self.assertRaises(ValueError):                                       # changing one into another connected one
            a.save_mail({"key": HOME, "provider": "gmail", "address": WORK})
        a.save_mail({"provider": "gmail", "address": HOME, "host": None})          # connecting it again: updated, not doubled
        self.assertEqual(len(a.cfg["accounts"]), 2)
        a.sync_mailbox(HOME, {"cancel": False})
        a.sync_mailbox(WORK.lower(), {"cancel": False})
        a.mem.state("fact", "我是产品经理")
        out = a.remove_mail({"key": HOME})
        self.assertEqual([x["key"] for x in out["accounts"]], [WORK.lower()])
        self.assertIsNone(a.secrets.get(server.MAIL_SECRET, HOME))
        self.assertEqual(r.subjects(HOME), [])                                     # its local data is gone
        self.assertEqual(r.subjects(WORK), ["周会改到周四"])                        # the other one's is not
        self.assertEqual([n["text"] for n in a.mem.notes(kinds=["fact"])], ["我是产品经理"])   # the person's memory stays
        self.assertFalse(out["exiting"])
        a.cfg["auto"]["enabled"] = True
        out = a.remove_mail({"key": WORK.lower()})                                 # the last one: nothing left to sort
        self.assertFalse(a.cfg["auto"]["enabled"])
        self.assertEqual(a.state()["accounts"], [])
        with self.assertRaises(ValueError):
            a.account(None)


class ConfigTests(unittest.TestCase):
    def test_one_mailbox_from_before_becomes_the_first_of_the_list(self):
        private_config(self)
        with open(config.CONFIG_PATH, "w") as f:
            json.dump({"version": 2, "mail": {"provider": "gmail", "address": HOME, "host": None, "port": 993}}, f)
        cfg = config.load()
        self.assertEqual(cfg["accounts"], [{"provider": "gmail", "address": HOME, "host": None, "port": 993}])
        self.assertNotIn("mail", cfg)
        config.save(cfg)
        self.assertEqual(config.load()["accounts"][0]["address"], HOME)


if __name__ == "__main__":
    unittest.main()
