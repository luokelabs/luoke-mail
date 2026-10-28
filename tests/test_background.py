"""Background sorting: the schedule, "notify about what needs you", "tell me once" about problems,
turning it on and off (the handoff to the login item) -- against the fake mailbox and model."""
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import background, config, review, server  # noqa: E402
from luoke_mail.credstore import FileStore  # noqa: E402
from luoke_mail.desktop import DesktopError  # noqa: E402
from luoke_mail.mailbox import Mailbox, MailError  # noqa: E402
from luoke_mail.store import Store  # noqa: E402
from fakes import FakeChat, FakeIMAP, suggestion  # noqa: E402
from test_core import make_mail  # noqa: E402

ME = "me@example.com"
DOWN = "http://127.0.0.1:9/v1"           # nothing listens there: the model service is "off"


def mail(n, subject, sender):
    return make_mail(subject, f"正文 {n}", sender=sender).replace(b"<abc123@corp.example>", f"<a{n}@x.example>".encode())


def triage_reply(body):
    text = body["messages"][1]["content"]
    if "促销" in text:
        return 200, suggestion("ignore", category="promotion", priority="low", due_date=None, reply_points=[])
    if "账户已冻结" in text:                       # for the harness: a phishing message (螺壳's urgent card)
        return 200, suggestion("ignore", category="phishing", priority="high", suspicious=True, due_date=None,
                               summary="冒充银行要你登录验证", reason="发件域名仿冒银行", reply_points=[])
    if "项目周会" in text:                         # for the harness: a meeting (螺壳's 加入日程 button)
        return 200, suggestion("schedule", category="work", priority="medium", due_date=None, summary="周四下午三点项目周会",
                               event_title="项目周会", event_date="2026-10-16", event_start="15:00", event_end="16:00",
                               reply_points=[])
    return 200, suggestion("reply")


class FakeDesktop:
    def __init__(self, supported=True):
        self.supported = supported
        self.installed = False
        self.service_starts = True
        self.notes, self.calls = [], []
        self.launcher = types.SimpleNamespace(name="螺壳邮件助手")

    def status(self):
        return {"supported": self.supported, "installed": self.installed, "stale": False, "app_name": "螺壳邮件助手"}

    def enable(self, port):
        self.calls.append(("enable", port))
        self.installed = True

    def disable(self):
        self.calls.append(("disable",))
        self.installed = False

    def wait_for_service(self, since, timeout=15):
        return self.service_starts

    def notify(self, title, subtitle, body):
        self.notes.append((subtitle, body))
        return "fake"


def private_config(test):
    """Keep this test's settings out of the config file other test modules read."""
    old = config.CONFIG_PATH
    config.CONFIG_PATH = os.path.join(tempfile.mkdtemp(), "config.json")
    test.addCleanup(setattr, config, "CONFIG_PATH", old)


def new_job():
    return {"cancel": False, "stage": "", "done": 0, "total": 0}


def no_evening_note(test):
    """The evening's 今日处理 note (after review.NOTIFY_HOUR) is tested on its own: here it never falls due, so these
    tests read the same at 20:30 as at noon."""
    p = mock.patch.object(review, "NOTIFY_HOUR", 24)
    p.start()
    test.addCleanup(p.stop)


class AutoSyncTests(unittest.TestCase):
    def setUp(self):
        private_config(self)
        no_evening_note(self)
        home = tempfile.mkdtemp()
        self.desk = FakeDesktop()
        self.app = a = server.App(secret_store=FileStore(os.path.join(home, "c.json")),
                                  state_store=Store(os.path.join(home, "s.db")), desktop_=self.desk)
        self.addCleanup(a.store.close)
        self.imap = FakeIMAP([(mail(1, "Q3 预算评审", "王总 <boss@corp.example>"), False),
                              (mail(2, "双十一促销", "某某商城 <news@shop.example>"), False)], flavour="gmail").__enter__()
        self.addCleanup(self.imap.__exit__)
        a.cfg["accounts"] = [{"provider": "gmail", "address": ME, "host": None, "port": 993}]
        a.secrets.set(server.MAIL_SECRET, ME, "secret")
        a.open_mailbox = lambda *_: Mailbox("gmail", ME, "secret", host="127.0.0.1", port=self.imap.port, ssl=False, timeout=5)
        a.cfg["engine"] = {"base_url": DOWN, "model": "test-model", "engine_name": None, "has_api_key": False}
        a.cfg["auto"] = {"enabled": True, "every": 30}

    def deliver(self, n, subject, sender):
        raw = mail(n, subject, sender)
        self.imap._add("INBOX", raw, set())
        self.imap._add("[Gmail]/All Mail", raw, set())

    def test_notifies_only_about_new_mail_that_needs_you(self):
        with FakeChat(triage_reply) as chat:
            self.app.cfg["engine"]["base_url"] = chat.base_url
            r = self.app.auto.run(new_job())
            self.assertEqual((r["new"], r["needs_you"]), (2, 1))
            self.assertEqual(self.desk.notes, [("1 封新邮件需要处理", "回复：王总「Q3 预算评审」")])
            self.assertEqual(self.app.auto_view()["last"]["text"], "新邮件 2 封，1 封需要处理")

            self.app.auto.run(new_job())                       # nothing new: quiet
            self.assertEqual(len(self.desk.notes), 1)
            self.assertEqual(self.app.auto_view()["last"]["text"], "没有新邮件")

            self.deliver(3, "合同请确认", "李律师 <lawyer@firm.example>")
            self.deliver(4, "又一个促销", "某某商城 <news@shop.example>")
            self.app.auto.run(new_job())
            self.assertEqual(self.desk.notes[-1], ("1 封新邮件需要处理", "回复：李律师「合同请确认」"))
        self.assertEqual(self.imap.violations, [])
        # what it sorted is marked read (ADR 0016: \\Seen, taken back from 今日处理); without a confirmed habit
        # nothing is moved, nothing removed, nothing written
        for cmd in ("UID MOVE", "UID EXPUNGE", "APPEND", "UID COPY"):
            self.assertNotIn(cmd, self.imap.commands)
        self.assertTrue(all("\\Seen" in m["flags"] for m in self.imap.folders["INBOX"]))

    def test_sync_puts_back_mail_recorded_as_handled_but_still_in_the_inbox(self):
        """The mailbox is the truth: a move that didn't happen (or mail moved back) can't hide mail for good."""
        a = self.app
        with FakeChat(triage_reply) as chat:
            a.cfg["engine"]["base_url"] = chat.base_url
            a.auto.run(new_job())
            items = {it["subject"]: it for it in a.store.items(ME, "pending")}
            a.store.set_state([items["Q3 预算评审"]["id"]], "done", "archive")     # what the label bug left behind
            a.store.set_state([items["双十一促销"]["id"]], "done", "draft")        # a reply draft: rightly still there
            r = a.sync_mailbox(ME, new_job())
        self.assertEqual(r["back"], 1)
        self.assertEqual(a.store.get(items["Q3 预算评审"]["id"])["state"], "pending")
        self.assertEqual(a.store.get(items["双十一促销"]["id"])["state"], "done")
        self.assertEqual(a.sync_mailbox(ME, new_job())["back"], 0)

    def test_model_off_keeps_mail_and_says_so_once(self):
        a = self.app
        a.add_rule({"source": "mailbox", "kind": "sender", "value": "news@shop.example", "action": "ignore"})
        r = a.auto.run(new_job())
        self.assertEqual((r["new"], r["by_rule"], r["waiting"]), (2, 0, 2))   # every message waits for the model, rule or not
        self.assertEqual(self.desk.notes, [("2 封新邮件尚未分析", "本机模型服务未运行。启动后，下次整理时会继续分析。")])
        self.assertFalse(a.auto_view()["last"]["ok"])
        a.auto.run(new_job())
        self.assertEqual(len(self.desk.notes), 1)                               # once per outage
        with FakeChat(triage_reply) as chat:
            a.cfg["engine"]["base_url"] = chat.base_url
            a.auto.run(new_job())                                               # the waiting mail is analysed now
        self.assertEqual(self.desk.notes[-1][0], "1 封新邮件需要处理")
        self.assertFalse(a.auto.state(ME)["model_reported"])
        self.assertTrue(a.auto_view()["last"]["ok"])

    def test_missing_password_is_a_mailbox_problem(self):
        self.app.secrets.delete(server.MAIL_SECRET, ME)
        del self.app.open_mailbox                              # the real one: no password -> MailError
        for _ in range(2):
            with self.assertRaises(MailError):
                self.app.auto.run(new_job())
        self.assertEqual(len(self.desk.notes), 1)
        self.assertIsNotNone(self.app.auto.state(ME)["last_try"])   # recorded, so it waits for the next interval
        self.assertFalse(self.app.auto.due())

    def test_a_locked_keychain_is_not_a_missing_password_and_tells_no_one(self):
        from luoke_mail.credstore import SecretUnavailable, KEYCHAIN_LOCKED

        class Locked:
            kind, label, plaintext = "keychain", "macOS 钥匙串", False

            def get(self, name, account):
                raise SecretUnavailable(KEYCHAIN_LOCKED)
        self.app.secrets, self.app._secret_known = Locked(), {}
        del self.app.open_mailbox                                    # the real one: it reads the keychain
        self.assertEqual(self.app.mail_accounts(), [ME])             # still connected, not 「授权码未保存」
        self.assertEqual(self.app._secret_known, {})                 # and asked again next time
        for _ in range(3):                                           # however many rounds while the Mac is locked
            self.app.auto.run(new_job())
        self.assertEqual(self.desk.notes, [])
        self.assertIn("钥匙串", self.app.auto_view()["last"]["text"])

    def test_mailbox_failure_reported_after_two_in_a_row_and_once(self):
        a = self.app
        working = a.open_mailbox

        def broken(*_):
            raise MailError("连接邮箱服务器超时。请检查网络，或稍后再试。")
        a.open_mailbox = broken
        with self.assertRaises(MailError):
            a.auto.run(new_job())
        self.assertEqual(self.desk.notes, [])                  # Wi-Fi right after wake-up: not worth a notification
        for _ in range(2):
            with self.assertRaises(MailError):
                a.auto.run(new_job())
        self.assertEqual(self.desk.notes, [("无法连接邮箱", "连接邮箱服务器超时。请检查网络，或稍后再试。")])
        a.open_mailbox = working
        a.auto.run(new_job())
        self.assertEqual(a.auto.state(ME)["fails"], 0)
        a.open_mailbox = broken
        for _ in range(2):
            with self.assertRaises(MailError):
                a.auto.run(new_job())
        self.assertEqual(len([n for n in self.desk.notes if n[0] == "无法连接邮箱"]), 2)   # a new outage is told again

    def test_schedule(self):
        t = [1000.0]
        a = self.app
        auto = background.AutoSync(a, clock=lambda: t[0])
        a.auto = auto
        self.assertFalse(auto.due())                           # first minute after start-up: the model may still be loading
        t[0] += background.FIRST_RUN_DELAY
        self.assertTrue(auto.due())
        auto.save(ME, {"last_try": t[0]})
        t[0] += 29 * 60
        self.assertFalse(auto.due())
        t[0] += 60
        self.assertTrue(auto.due())
        a.cfg["auto"]["enabled"] = False
        self.assertFalse(auto.due())

    def test_manual_sync_joins_background_sorting(self):
        a = self.app
        a.jobs["x"] = {"id": "x", "kind": "auto", "status": "running", "stage": "", "done": 0, "total": 0,
                       "result": None, "error": None, "cancel": False, "started": ""}
        self.assertTrue(a.start_sync({"source": "mailbox"})["joined"])
        self.assertEqual(a.start_sync({"source": "demo"})["stage"], "等待「后台自动整理」完成…")    # waits its turn
        self.assertTrue(a.start_auto_sync()["joined"])
        with self.assertRaisesRegex(ValueError, "「后台自动整理」正在进行"):
            a._start_job("clean", lambda job: None)                     # changing the mailbox does not wait
        a.jobs["x"]["status"] = "done"

    def test_describe_puts_urgent_first(self):
        items = [{"action": "follow_up", "priority": "low", "from_name": "物业", "subject": "缴费提醒", "date": "2026-10-06 09:00"},
                 {"action": "schedule", "priority": "high", "from_name": "HR", "subject": "周五团建", "date": "2026-10-05 09:00"},
                 {"action": "reply", "priority": "medium", "from_addr": "a@b.example", "subject": "x" * 40, "date": "2026-10-06 10:00"}]
        sub, body = background.describe(items)
        self.assertEqual(sub, "3 封新邮件需要处理")
        self.assertTrue(body.startswith("加入日程：HR「周五团建」；回复：a@b.example「" + "x" * 24 + "」"))
        self.assertTrue(body.endswith("，另有 1 封"))
        self.assertEqual(background.needs_you([{"action": "reply", "suspicious": True}, {"action": "archive"}]), [])


class SwitchTests(unittest.TestCase):
    """Turning background sorting on hands this server over to the login item; turning it off removes it."""

    def setUp(self):
        private_config(self)
        home = tempfile.mkdtemp()
        self.desk = FakeDesktop()
        self.app = a = server.App(secret_store=FileStore(os.path.join(home, "c.json")),
                                  state_store=Store(os.path.join(home, "s.db")), desktop_=self.desk)
        self.addCleanup(a.store.close)
        a.port = 8765
        self.stopped = threading.Event()
        a.on_exit = self.stopped.set

    def connect(self):
        a = self.app
        a.cfg["accounts"] = [{"provider": "gmail", "address": ME, "host": None, "port": 993}]
        a.secrets.set(server.MAIL_SECRET, ME, "secret")
        a.cfg["engine"] = {"base_url": DOWN, "model": "test-model", "engine_name": None, "has_api_key": False}

    def test_needs_mailbox_model_and_consent(self):
        with self.assertRaisesRegex(ValueError, "请先连接邮箱"):
            self.app.set_auto({"enabled": True, "every": 30, "consent": True})
        self.connect()
        with self.assertRaisesRegex(ValueError, "确认"):
            self.app.set_auto({"enabled": True, "every": 30})
        with self.assertRaisesRegex(ValueError, "间隔"):
            self.app.set_auto({"enabled": True, "every": 7, "consent": True})
        self.assertEqual(self.desk.calls, [])

    def test_enable_hands_over_to_the_login_item(self):
        self.connect()
        r = self.app.set_auto({"enabled": True, "every": 15, "consent": True})
        self.assertTrue(r["handoff"])
        self.assertEqual(self.desk.calls, [("enable", 8765)])
        self.assertTrue(self.stopped.wait(3))                  # this server steps aside for the service
        self.assertEqual(self.app.exit_reason, "handoff")
        self.assertEqual(self.desk.notes[0][0], "后台自动整理已开启")   # the permission prompt comes now, not with real mail
        self.assertEqual(config.load()["auto"], {"enabled": True, "every": 15})

    def test_service_that_does_not_start_is_rolled_back(self):
        self.connect()
        self.desk.service_starts = False
        with self.assertRaises(DesktopError):
            self.app.set_auto({"enabled": True, "every": 30, "consent": True})
        self.assertEqual(self.desk.calls, [("enable", 8765), ("disable",)])
        self.assertFalse(self.app.cfg["auto"]["enabled"])
        self.assertFalse(self.stopped.wait(0.8))               # still serving the page

    def test_running_as_service_changes_interval_in_place_and_exits_when_off(self):
        self.connect()
        a = self.app
        a.service, self.desk.installed = True, True
        a.cfg["auto"] = {"enabled": True, "every": 30}
        r = a.set_auto({"enabled": True, "every": 60})
        self.assertFalse(r["handoff"])
        self.assertEqual(self.desk.calls, [])
        self.assertEqual(self.desk.notes, [])                  # changing the interval is not news
        r = a.set_auto({"enabled": False})
        self.assertTrue(r["exiting"])
        self.assertTrue(self.stopped.wait(3))
        self.assertEqual(self.desk.calls, [("disable",)])

    def test_without_login_items_it_runs_while_open(self):
        self.connect()
        self.desk.supported = False
        r = self.app.set_auto({"enabled": True, "every": 30, "consent": True})
        self.assertFalse(r["handoff"])
        self.assertIn("程序运行期间", r["message"])
        self.assertEqual(self.app.auto_accounts(), [ME])

    def test_forget_removes_the_login_item(self):
        self.desk.installed = True
        r = self.app.forget()
        self.assertFalse(r["exiting"])
        self.assertEqual(self.desk.calls, [("disable",)])


if __name__ == "__main__":
    unittest.main()
