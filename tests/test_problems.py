"""Why something stopped, as a code the window words (top-level ADR 0002): each failure of the model or the mailbox
carries its own, a stopped job and a stopped background task keep it, and the rest is told by its kind."""
from __future__ import annotations

import imaplib
import os
import socket
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import engines, hub, mailbox, problems, triage  # noqa: E402
from test_old_mail import ME, Rig  # noqa: E402


class CodeTests(unittest.TestCase):
    def test_each_failure_says_why_in_a_code(self):
        code = problems.code_of
        self.assertEqual(code(mailbox.friendly_error(socket.timeout(), "gmail")), "mail_timeout")
        self.assertEqual(code(mailbox.friendly_error(ConnectionRefusedError(), "qq")), "mail_unreachable")
        self.assertEqual(code(mailbox.friendly_error(imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials"),
                                                     "gmail")), "login_refused")
        self.assertEqual(code(mailbox.friendly_error(imaplib.IMAP4.error("[WEBALERT https://x] log in via web browser"),
                                                     "gmail")), "mail_login_blocked")
        self.assertEqual(code(mailbox.friendly_error(imaplib.IMAP4.error("Unsafe Login. Please contact"), "163")),
                         "mail_unsafe_login")
        self.assertEqual(code(mailbox.KeychainLocked("钥匙串暂时无法读取")), "keychain_locked")
        self.assertEqual(code(mailbox.MailError("x")), "mail_error")
        self.assertEqual(code(hub._refusal("paused", {})), "paused")
        self.assertEqual(code(hub._refusal("needs_user", {})), "memory_full")
        self.assertEqual(code(hub._refusal("input_too_long", {})), "input_too_long")
        with self.assertRaises(engines.EngineError) as down:
            hub.Hub("/nonexistent/luoke-hub-for-tests.sock").tasks()
        self.assertEqual(code(down.exception), "hub_not_running")
        self.assertEqual(code(engines.EngineError("x")), "model_error")
        self.assertEqual((code(ValueError("x")), code(OSError("x")), code(KeyError("x"))), ("invalid", "network", "error"))

    def test_a_job_that_stopped_keeps_the_code(self):
        rig = Rig(self)
        app = rig.app

        def work(job):
            raise mailbox.LoginRefused("授权码不正确")
        jid = app._start_job("sync", work, account=ME)["id"]
        for _ in range(100):
            if app.jobs[jid]["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual((app.jobs[jid]["status"], app.jobs[jid]["error_code"]), ("error", "login_refused"))

    def test_the_subscription_check_says_in_data_how_many_senders_the_model_did_not_look_at(self):
        app = Rig(self).app
        rows = [{"key": f"from:{n}@x.example", "verdict": "unsubscribe", "subjects": ["促销"], "read": 0, "count": 9}
                for n in ("a", "b")] + [{"key": "from:c@x.example", "verdict": "keep", "subjects": [], "read": 5, "count": 5}]
        job = {"cancel": False}
        with mock.patch.object(app, "has_model", lambda: False):
            note, check = app._check_senders(ME, [], [dict(r) for r in rows], job)
        self.assertIn("未选择本机模型", note)
        self.assertEqual(check, {"reason": "no_model", "problem_code": None, "unchecked": 2, "of": 2})
        down = engines.EngineError("螺壳没有运行。", "hub_not_running")
        with mock.patch.object(app, "has_model", lambda: True), \
                mock.patch.object(triage, "classify_senders", mock.Mock(side_effect=down)):
            note, check = app._check_senders(ME, [], [dict(r) for r in rows], job)
        self.assertEqual(check, {"reason": "model_error", "problem_code": "hub_not_running", "unchecked": 2, "of": 2})
        with mock.patch.object(app, "has_model", lambda: True), \
                mock.patch.object(triage, "classify_senders", lambda batch, link: ({1: "marketing"}, len(batch))):
            note, check = app._check_senders(ME, [], [dict(r) for r in rows], job)
        self.assertEqual(check, {"reason": "unanswered", "problem_code": None, "unchecked": 1, "of": 2})


if __name__ == "__main__":
    unittest.main()
