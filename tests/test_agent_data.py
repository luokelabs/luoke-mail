"""The raw data 螺壳's window builds the mail assistant's pages from (ADR 0002, docs/agent-data.md): read-only JSON,
no A2UI and no sentences put together, credentials from mail masked; and the actions, each on the user's tap."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
import test_agent  # noqa: E402 -- its setUp; not its tests, which run there
from fakes import FakeChat  # noqa: E402
from test_agent import user_action  # noqa: E402
from test_background import triage_reply  # noqa: E402
from test_old_mail import ME, mail  # noqa: E402
from luoke_mail import desktop, memory, server, subscriptions  # noqa: E402
from luoke_mail.mailbox import LoginRefused  # noqa: E402

HABIT = [{"note_id": 7, "sender": "list:promo.shop.example", "category": "promotion", "action": "archive",
          "text": "某某商城的推广邮件都归档"}]
TAP = user_action("luoke-agent/page/mail", "x")


class DataTests(unittest.TestCase):
    def setUp(self):
        test_agent.AgentTests.setUp(self)

    def data(self, name: str, **args):
        q = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in sorted(args.items()))
        return self.c.read_json(f"data/{name}" + (f"?{q}" if q else ""))

    def item(self, subject: str) -> dict:
        return next(it for it in self.r.app.store.items(ME, None, None) if it["subject"] == subject)

    def call(self, name: str, args: dict, tap=TAP):
        return self.c.call_tool(name, args, user_action=tap)

    # ---- data/accounts ----------------------------------------------------------------------------------------
    def test_accounts_say_how_each_mailbox_is(self):
        a = self.data("accounts")["accounts"][0]
        self.assertEqual((a["id"], a["name"], a["status"], a["error"]), (ME, None, "ok", None))
        self.r.app.cfg["accounts"][0]["name"] = "工作"
        self.r.app.store.cache_set(ME, "mail_status", {"ok": False, "auth": True, "error": "授权码不对"})
        a = self.data("accounts")["accounts"][0]
        self.assertEqual((a["name"], a["status"], a["error"]), ("工作", "needs_auth", "授权码不对"))
        self.r.app.store.cache_set(ME, "mail_status", {"ok": False, "auth": False, "error": "连不上"})
        self.assertEqual(self.data("accounts")["accounts"][0]["status"], "error")

    def test_a_sync_keeps_when_it_went_through_and_a_refused_login(self):
        with FakeChat(triage_reply) as chat:
            self.r.app.cfg["engine"]["base_url"] = chat.base_url
            self.r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        a = self.data("accounts")["accounts"][0]
        self.assertEqual(a["status"], "ok")
        self.assertTrue(a["last_sync"])
        with mock.patch.object(self.r.app, "open_mailbox", side_effect=LoginRefused("授权码已失效")):
            with self.assertRaises(LoginRefused):
                self.r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        a2 = self.data("accounts")["accounts"][0]
        self.assertEqual((a2["status"], a2["error"]), ("needs_auth", "授权码已失效"))
        self.assertEqual(a2["last_sync"], a["last_sync"])                    # the last one that went through

    # ---- the mailboxes themselves -------------------------------------------------------------------------------
    SECRET = "pw-not-on-any-page-1234"

    def connect(self, **args):
        return self.call("mail_connect", {"provider": "custom", "address": "lucy@corp.example", "host": "127.0.0.1",
                                          "port": 993, "secret": self.SECRET, **args})

    def test_a_mailbox_is_connected_and_its_password_goes_to_the_keychain_only(self):
        r = self.connect()
        self.assertFalse(r.is_error, r.text)
        self.assertIn("连接成功", r.text)
        self.assertEqual(r.structured["account"], "lucy@corp.example")
        app = self.r.app
        self.assertEqual(app.secrets.get(server.MAIL_SECRET, "lucy@corp.example"), self.SECRET)
        self.assertIn("lucy@corp.example", [a["id"] for a in self.data("accounts")["accounts"]])
        seen = json.dumps([r.text, r.structured, self.data("accounts"), self.data("settings"), app.cfg],
                          ensure_ascii=False, default=str)
        self.assertNotIn(self.SECRET, seen)
        again = self.call("mail_connect", {"key": ME, "provider": "custom", "address": ME, "host": "127.0.0.1",
                                           "port": 993, "secret": "a-new-one"})
        self.assertFalse(again.is_error, again.text)                           # 改授权码: the same mailbox, a new password
        self.assertEqual(app.secrets.get(server.MAIL_SECRET, ME), "a-new-one")

    def test_connecting_needs_the_tap_a_password_and_a_live_instance(self):
        args = {"provider": "custom", "address": "lucy@corp.example", "host": "127.0.0.1", "port": 993,
                "secret": self.SECRET}
        r = self.c.call_tool("mail_connect", args)                              # not from a tap: refused by the server
        self.assertEqual(r.structured.get("code") if r.structured else None, "needs_user_action")
        self.assertNotIn(self.SECRET, r.text)
        none = self.call("mail_connect", {"provider": "gmail", "address": "lucy@gmail.example"})
        self.assertTrue(none.is_error)
        self.assertIn("授权码", none.text)
        self.assertIsNone(self.r.app.mailbox_cfg("lucy@gmail.example"))
        self.assertTrue(self.connect(secret=["not", "text"]).is_error)
        self.assertTrue(self.connect(secret="x" * 600).is_error)
        self.assertEqual(self.call("mail_connect", {**args, "key": "nobody@example.com"}).structured["code"], "stale")
        self.assertNotIn("lucy@corp.example", self.r.app.mail_accounts())
        self.assertIsNone(self.r.app.secrets.get(server.MAIL_SECRET, "lucy@corp.example"))

    def test_a_refused_login_is_said_and_the_mailbox_stays_to_be_corrected(self):
        with mock.patch.object(self.r.app, "test_mail", side_effect=LoginRefused("授权码不对。")):
            r = self.connect()
        self.assertTrue(r.is_error)
        self.assertIn("已保存，连接失败", r.text)
        self.assertNotIn(self.SECRET, r.text)
        self.assertIn("lucy@corp.example", self.r.app.mail_accounts())
        self.assertEqual(r.structured, {"code": "failed", "saved": True, "account": "lucy@corp.example"})
        self.assertNotIn("saved", self.connect(secret="x" * 600).structured)      # refused before saving: no mark

    def test_the_kinds_of_mailbox_come_with_how_to_get_the_password(self):
        from luoke_mail import providers
        got = self.data("providers")["providers"]
        self.assertEqual([p["key"] for p in got], list(providers.PROVIDERS))
        gmail = got[0]
        self.assertEqual((gmail["name"], gmail["secret_label"]), ("Gmail", "应用专用密码"))
        self.assertTrue(gmail["steps"])
        self.assertTrue(any(l.get("account") for l in gmail["links"]))
        self.assertEqual(set(gmail), {"key", "name", "host", "port", "secret_label", "secret_hint", "steps", "links",
                                      "notes", "trouble", "purge_days", "block"})
        self.assertEqual(got[-1]["key"], "custom")

    def test_a_mailbox_is_removed_with_what_is_kept_about_it(self):
        self.connect()
        r = self.call("mail_remove", {"account": "lucy@corp.example"})
        self.assertFalse(r.is_error, r.text)
        self.assertEqual((r.structured["account"], r.structured["exiting"]), ("lucy@corp.example", False))
        self.assertNotIn("lucy@corp.example", self.r.app.mail_accounts())
        self.assertIsNone(self.r.app.secrets.get(server.MAIL_SECRET, "lucy@corp.example"))
        self.assertEqual(self.call("mail_remove", {"account": "lucy@corp.example"}).structured["code"], "stale")
        self.assertEqual(self.c.call_tool("mail_remove", {"account": ME}).structured["code"], "needs_user_action")
        self.assertIn(ME, self.r.app.mail_accounts())
        tools = {t["name"]: t for t in self.c.list_tools()}
        self.assertTrue(tools["mail_remove"]["annotations"]["destructiveHint"])
        self.assertTrue(tools["mail_forget"]["annotations"]["destructiveHint"])

    def test_everything_is_cleared_and_the_memory_only_when_ticked(self):
        self.connect()
        with mock.patch.object(self.r.app.mem, "forget_tool") as forgot:
            r = self.call("mail_forget", {})
        self.assertFalse(r.is_error, r.text)
        forgot.assert_not_called()
        self.assertEqual(self.r.app.mail_accounts(), [])
        self.assertIsNone(self.r.app.secrets.get(server.MAIL_SECRET, ME))
        self.assertIsNone(self.r.app.secrets.get(server.MAIL_SECRET, "lucy@corp.example"))
        with mock.patch.object(self.r.app.mem, "forget_tool") as forgot:
            self.assertIn("写入记忆的内容已一并删除", self.call("mail_forget", {"memory": True}).text)
        forgot.assert_called_once_with(memory.TOOL)

    def test_a_new_server_needs_the_password_typed_again(self):
        """Security review 11-13: the saved password went to whatever host a change named, untyped."""
        self.connect()
        app = self.r.app
        same = {"key": "lucy@corp.example", "provider": "custom", "address": "lucy@corp.example", "host": "127.0.0.1",
                "port": 993}
        with mock.patch.object(app, "test_mail", return_value={"total": 0, "unseen": 0}) as tried:
            for change in ({"host": "imap.elsewhere.example"}, {"port": 1993}, {"provider": "qq", "host": None}):
                r = self.call("mail_connect", {**same, **change})
                self.assertTrue(r.is_error, change)
                self.assertIn("换了服务器，请重新填写授权码", r.text)
            tried.assert_not_called()
            m = app.mailbox_cfg("lucy@corp.example")
            self.assertEqual((m["provider"], m["host"], m["port"]), ("custom", "127.0.0.1", 993))
            self.assertFalse(self.call("mail_connect", {**same, "host": "127.0.0.1"}).is_error)   # the same: kept
            r = self.call("mail_connect", {**same, "host": "imap.elsewhere.example", "secret": "typed-again"})
        self.assertFalse(r.is_error, r.text)
        self.assertEqual(app.mailbox_cfg("lucy@corp.example")["host"], "imap.elsewhere.example")
        self.assertEqual(app.secrets.get(server.MAIL_SECRET, "lucy@corp.example"), "typed-again")

    def test_an_address_or_password_with_a_newline_is_refused(self):
        """Security review 11-13: the address reaches `security -i`, which runs a second line as a command."""
        for args in ({"address": "lucy@corp.example\nlist-keychains"}, {"host": "127.0.0.1\rx"},
                     {"secret": "pw\ndelete-generic-password"}):
            r = self.connect(**args)
            self.assertTrue(r.is_error, args)
            self.assertNotIn("delete-generic-password", r.text)
        self.assertEqual(self.r.app.cfg["accounts"][1:], [])
        self.assertIsNone(self.r.app.secrets.get(server.MAIL_SECRET, "lucy@corp.example"))

    def test_a_changed_address_takes_what_was_kept_about_the_old_one(self):
        self.connect()
        app = self.r.app
        app._add_rule("lucy@corp.example", "sender", "news@shop.example", "archive")
        app.store.cache_set("lucy@corp.example", "mail_status", {"ok": True})
        with mock.patch.object(app, "test_mail", return_value={"total": 0, "unseen": 0}):
            r = self.call("mail_connect", {"key": "lucy@corp.example", "provider": "custom", "address": "lucy2@corp.example",
                                           "host": "127.0.0.1", "port": 993})
        self.assertFalse(r.is_error, r.text)
        self.assertEqual(app.store.rules("lucy@corp.example"), [])
        self.assertEqual(app.store.cache_get("lucy@corp.example", "mail_status")[0], None)
        self.assertEqual(app.secrets.get(server.MAIL_SECRET, "lucy2@corp.example"), self.SECRET)
        self.assertIsNone(app.secrets.get(server.MAIL_SECRET, "lucy@corp.example"))

    def test_a_mailbox_whose_password_was_lost_can_be_removed_or_given_one(self):
        self.connect()
        app = self.r.app
        app.secrets.delete(server.MAIL_SECRET, "lucy@corp.example")
        app._secret_known.clear()
        self.assertNotIn("lucy@corp.example", app.mail_accounts())
        with mock.patch.object(app, "test_mail", return_value={"total": 0, "unseen": 0}):
            r = self.call("mail_connect", {"key": "lucy@corp.example", "provider": "custom", "address": "lucy@corp.example",
                                           "host": "127.0.0.1", "port": 993, "secret": "found-it"})
        self.assertFalse(r.is_error, r.text)
        app.secrets.delete(server.MAIL_SECRET, "lucy@corp.example")
        app._secret_known.clear()
        r = self.call("mail_remove", {"account": "lucy@corp.example"})
        self.assertFalse(r.is_error, r.text)
        self.assertIsNone(app.mailbox_cfg("lucy@corp.example"))

    def test_calendar_files_and_logs_go_with_the_mailbox_and_with_everything(self):
        """Security review 11-13: the .ics handed to 「日历」 carry the subject, summary and place."""
        from luoke_mail import config
        home = config.CONFIG_DIR
        os.makedirs(os.path.join(home, "calendar"), exist_ok=True)
        os.makedirs(os.path.join(home, "run"), exist_ok=True)
        mine = os.path.join(home, "calendar", f"luoke-{self.r.app.store.item_ids(ME)[0]}.ics")
        other = os.path.join(home, "calendar", "luoke-987654.ics")
        logs = [os.path.join(home, n) for n in ("app.log", "app.log.1", "service.log", "service.log.1",
                                                "notification.txt", os.path.join("run", "notifications.json"))]
        for f in [mine, other] + logs:
            open(f, "w").close()
        self.connect()
        self.assertFalse(self.call("mail_remove", {"account": ME}).is_error)
        self.assertFalse(os.path.exists(mine))
        self.assertTrue(os.path.exists(other))                                 # not this mailbox's
        with mock.patch.object(self.r.app.mem, "forget_tool") as forgot:
            r = self.call("mail_forget", {"memory": "false"})                  # not a ticked box
        self.assertFalse(r.is_error, r.text)
        forgot.assert_not_called()
        self.assertNotIn("记忆", r.text)
        self.assertFalse(os.path.exists(os.path.join(home, "calendar")))
        self.assertEqual([f for f in logs if os.path.exists(f)], [])

    # ---- data/today -------------------------------------------------------------------------------------------
    def test_today_gives_what_was_done_what_to_decide_and_the_replies(self):
        self.r.srv._add("INBOX", mail(90, "周末促销", "某某商城 <news@shop.example>", 0, "promo.shop.example"), set())
        with mock.patch.object(memory, "auto_habits", lambda mem, acct: HABIT), FakeChat(triage_reply) as chat:
            self.r.app.cfg["engine"]["base_url"] = chat.base_url
            self.r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        t = self.data("today", account=ME)
        habit = next(s for s in t["handled"] if s["by"] == "habit")
        self.assertEqual((habit["op"], habit["count"], habit["undoable"]), ("archive", 1, True))
        self.assertEqual(habit["senders"], [{"name": "某某商城", "address": "news@shop.example"}])
        self.assertEqual(([m["subject"] for m in habit["mails"]], habit["more"]), (["周末促销"], 0))
        m = self.data("mail", id=habit["mails"][0]["id"])                    # archived: still opens, with its text
        self.assertEqual((m["subject"], m["ways"]), ("周末促销", []))
        self.assertTrue(m["body_text"])
        self.assertEqual(t["decide"][0]["ways"], ["spam", "skip"])           # suspicious first
        self.assertTrue(all(d["suggested"] == d["ways"][0] for d in t["decide"]))
        self.assertNotIn("答辩时间确认", [d["subject"] for d in t["decide"]])  # a reply is not decided here
        self.assertEqual(t["replies"], sum(1 for x in self.r.app.agent.waiting(ME)["needs"] if x["action"] == "reply"))
        self.assertNotIn("482913", json.dumps(t, ensure_ascii=False))       # a code from mail, masked
        for words in ("今天", "替你", "封"):                                  # data, not sentences
            self.assertNotIn(words, json.dumps({k: v for k, v in t.items() if k != "decide"}, ensure_ascii=False))
        self.assertEqual(self.data("today", account="all")["replies"], t["replies"])          # one mailbox here

    def test_an_unknown_mailbox_is_refused(self):
        self.assertEqual(self.data("today", account="nobody@example.com")["error"]["code"], "invalid")
        self.assertEqual(self.data("mail", id="999999")["error"]["code"], "stale")

    # ---- data/inbox -------------------------------------------------------------------------------------------
    def test_the_inbox_lists_by_kind_due_soon_first(self):
        box = self.data("inbox", account=ME)
        self.assertEqual(box["list"], "reply")
        self.assertEqual([r["subject"] for r in box["items"]], ["答辩时间确认"])
        line = box["items"][0]
        self.assertEqual((line["kind"], line["account"], line["ask_text"]), ("reply", ME, "王老师问答辩时间，周五前回"))
        self.assertEqual(box["counts"]["reply"], 1)
        needs = self.data("inbox", account=ME, list="needs")
        self.assertIn("账户已冻结", [r["subject"] for r in needs["items"]])
        self.assertEqual(len(needs["items"]), box["counts"]["needs"])
        everything = self.data("inbox", account="all", list="all")
        self.assertEqual(len(everything["items"]), sum(box["counts"][k] for k in ("needs", "direct", "unsorted")))
        self.assertNotIn("482913", json.dumps(everything, ensure_ascii=False))

    def test_switching_lists_reads_only_what_a_line_shows(self):
        """A tab of the inbox reads the waiting mail without bodies, appointments, draft notes, the line about old
        mail or the conversations: those took seconds a tab for a big Gmail inbox (11-18). A brief mail says what
        the full one says about everything a line shows."""
        app = self.r.app
        full = {it["id"]: app.public_item(it) for it in app.store.items(ME, "pending")}
        brief = {it["id"]: app.public_item(it, brief=True) for it in app.store.items(ME, "pending", body=False)}
        self.assertEqual(set(full), set(brief))
        for i, b in brief.items():
            self.assertNotIn("body", b)
            self.assertEqual(b, {k: full[i][k] for k in b})
        before = {w: self.data("inbox", account="all", list=w) for w in ("reply", "needs", "suspicious", "all")}
        slow = mock.Mock(side_effect=AssertionError("not for a list of lines"))
        with mock.patch.object(app, "list_items", slow), mock.patch.object(app, "old_summary", slow), \
                mock.patch.object(app, "draft_note", slow), mock.patch.object(app, "_events_view", slow):
            for w, box in before.items():
                self.assertEqual(self.data("inbox", account="all", list=w), box)

    def test_a_draft_is_written_then_saved_into_the_mailbox(self):
        it = self.item("答辩时间确认")
        with FakeChat(test_agent.drafts) as chat:
            self.r.app.cfg["engine"]["base_url"] = chat.base_url
            r = self.call("mail_draft", {"account": ME, "id": it["id"]})
        self.assertFalse(r.is_error, r.text)
        self.assertIn("周三下午可以", r.structured["draft_text"])
        self.assertTrue(self.data("inbox", account=ME)["items"][0]["draft"])
        r = self.call("mail_draft_save", {"account": ME, "id": it["id"], "text": "王老师好，周三下午可以。"})
        self.assertFalse(r.is_error, r.text)
        self.assertTrue(self.call("mail_draft_save", {"account": ME, "id": it["id"], "text": " "}).is_error)

    def test_a_suggestion_is_changed_and_a_sync_started_and_stopped(self):
        it = self.item("合同扫描件")
        r = self.call("mail_change", {"account": ME, "ids": [it["id"]], "action": "archive"})
        self.assertFalse(r.is_error, r.text)
        self.assertEqual(self.r.app.store.get(it["id"])["action"], "archive")
        with mock.patch.object(self.r.app, "start_sync", lambda body: {"id": "j1"}):
            r = self.call("mail_sync", {"account": ME})
        self.assertEqual(r.structured["job"], "j1")
        self.r.app.jobs["j2"] = {"id": "j2", "kind": "sync", "status": "running", "cancel": False}
        self.assertFalse(self.call("mail_job_stop", {"id": "j2"}).is_error)
        self.assertTrue(self.r.app.jobs["j2"]["cancel"])
        self.r.app.jobs.pop("j2")
        self.assertTrue(self.call("mail_job_stop", {"id": "j2"}).is_error)

    # ---- data/digest ------------------------------------------------------------------------------------------
    def test_the_pieces_of_the_week_and_what_is_said_of_them(self):
        from test_window_settings import _pieces
        _pieces(self.r.app, ["本地模型的新进展", "邮件协议小史"])
        d = self.data("digest", account=ME)
        self.assertEqual(d["range"], "week")
        p = next(x for x in d["items"] if x["title"] == "本地模型的新进展")
        self.assertEqual((p["url"], p["host"], p["summary_text"], p["feedback"]),
                         ("https://news.example/a", "news.example", "本地模型的新进展的摘要", None))
        self.assertEqual(d["counts"], {"saved": 0, "archived": 0, "sources": 1})
        self.assertEqual(len(self.data("digest", account=ME, range="today")["items"]), 2)     # read out today
        self.assertFalse(self.call("mail_digest_feedback", {"account": ME, "id": p["id"], "value": "useful"}).is_error)
        self.assertEqual([x["title"] for x in self.data("digest", account=ME, range="saved")["items"]], ["本地模型的新进展"])
        self.assertFalse(self.call("mail_digest_archive", {"account": ME, "id": p["id"], "on": True}).is_error)
        self.assertEqual(self.data("digest", account=ME)["counts"]["archived"], 1)
        self.assertNotIn("本地模型的新进展", [x["title"] for x in self.data("digest", account=ME)["items"]])
        self.assertFalse(self.call("mail_digest_open", {"account": ME, "id": p["id"]}).is_error)
        self.assertTrue(self.r.app.store.digest_item(p["id"])["opened"])
        self.assertEqual(self.data("digest", account=ME, range="lately")["error"]["code"], "invalid")
        self.assertTrue(self.call("mail_digest_open", {"account": ME, "id": 999999}).is_error)

    def test_the_sources_and_a_piece_opened_by_reading_it(self):
        from test_window_settings import _pieces
        _pieces(self.r.app, ["本地模型的新进展"])
        d = self.data("digest", account=ME)
        self.assertEqual([(s["key"], s["name"], s["origin"]) for s in d["sources"]], [("list:weekly.news.example", "周刊", "user")])
        p = d["items"][0]
        self.assertFalse(p["opened"])
        with mock.patch.object(self.r.app.curator, "opened", wraps=self.r.app.curator.opened) as opened:
            x = self.data("piece", account=ME, id=p["id"])
        self.assertEqual((x["title"], x["opened"]), ("本地模型的新进展", True))
        opened.assert_called_once()
        self.assertTrue(self.data("digest", account=ME)["items"][0]["opened"])
        self.assertEqual(self.data("piece", account=ME, id=999999)["error"]["code"], "stale")
        self.assertFalse(self.call("mail_digest_source", {"account": ME, "key": "list:weekly.news.example", "on": False}).is_error)
        self.assertEqual(self.data("digest", account=ME)["sources"], [])

    def test_a_plain_http_link_is_not_given(self):
        from test_window_settings import _pieces
        _pieces(self.r.app, ["只有明文链接"], url="http://plain.example/x")
        p = next(x for x in self.data("digest", account=ME)["items"] if x["title"] == "只有明文链接")
        self.assertEqual((p["url"], p["host"]), (None, None))

    # ---- data/clean -------------------------------------------------------------------------------------------
    def checked(self, senders):
        self.r.app.store.cache_set(ME, "subscriptions", {"days": 60, "scanned": 50, "senders": senders,
                                                         "at": "2026-11-09 09:00", "model_note": None})

    def to_unsubscribe(self, account: str) -> list:
        """The senders the check suggests unsubscribing from, not decided on (the page's first group)."""
        return [x for x in self.data("clean", account=account)["subscriptions"]["senders"] if x["group"] == "unsubscribe"]

    def test_clean_before_anything_was_looked_at(self):
        self.assertEqual(self.data("clean", account=ME), {"subscriptions": None, "archive": None})
        self.assertEqual(self.data("old", account=ME, scope="archive")["scanned_at"], None)
        self.assertEqual(self.data("old")["account"], ME)                     # no account: the first, as every read
        self.assertEqual(self.data("old", account="all")["error"]["code"], "invalid")
        self.assertEqual(self.data("old", account=ME, scope="trash")["error"]["code"], "invalid")

    def test_the_whole_subscription_check_by_group_and_a_decision_taken_back(self):
        from test_window_clean import SHOP, sender
        self.checked([SHOP, sender("one@x.example", "早报", method="one_click", unsub_http="https://x.example/u"),
                      sender("bad@x.example", "冒充", verdict="never", suspicious=True),
                      sender("often@x.example", "常读", verdict="keep", subjects=["您的验证码是 482913"]),
                      sender("mt@x.example", "邮件退订", method="mailto", unsub_mailto="mailto:leave@x.example?subject=TD")])
        s = self.data("clean", account=ME)["subscriptions"]
        self.assertEqual(s["scanned"], 50)
        by = {x["name"]: x for x in s["senders"]}
        self.assertEqual({n: x["group"] for n, x in by.items()},
                         {"某某商城": "no_way", "早报": "unsubscribe", "冒充": "suspicious", "常读": "keep", "邮件退订": "unsubscribe"})
        self.assertEqual([x["group"] for x in s["senders"]][:3], ["unsubscribe", "unsubscribe", "no_way"])
        self.assertEqual((by["某某商城"]["size"], by["某某商城"]["subjects"]), (40000, ["双十一预热", "旧推送一"]))
        self.assertEqual(by["邮件退订"]["mailto"], "mailto:leave@x.example?subject=TD")
        self.assertNotIn("482913", json.dumps(by["常读"], ensure_ascii=False))
        shop = by["某某商城"]["key"]
        self.assertFalse(self.call("mail_sender_rule", {"account": ME, "key": shop, "action": "trash"}).is_error)
        x = next(x for x in self.data("clean", account=ME)["subscriptions"]["senders"] if x["key"] == shop)
        self.assertEqual((x["group"], x["decision"]["status"], x["rule"]["action"]), ("handled", "trash_rule", "ignore"))
        self.assertTrue(x["decision"]["at"])
        r = self.call("mail_sender_reset", {"account": ME, "key": shop})
        self.assertFalse(r.is_error, r.text)
        x = next(x for x in self.data("clean", account=ME)["subscriptions"]["senders"] if x["key"] == shop)
        self.assertEqual((x["group"], x["decision"], x["rule"]), ("no_way", None, None))
        self.assertEqual(self.r.app.store.rules(ME), [])                       # the rule it made went with it
        self.assertFalse(self.call("mail_undo_step", {"account": ME, "log_id": r.structured["log_id"]}).is_error)
        self.assertEqual(len(self.r.app.store.rules(ME)), 1)
        r = self.call("mail_sender_unsubscribed", {"account": ME, "key": by["邮件退订"]["key"]})
        self.assertFalse(r.is_error, r.text)
        self.assertEqual([x["name"] for x in self.to_unsubscribe(ME)], ["早报"])
        self.assertTrue(self.call("mail_sender_reset", {"account": ME, "key": "from:nobody@x.example"}).is_error)

    def test_why_each_suggestion_and_where_to_block_a_sender(self):
        from test_window_clean import sender
        self.checked([sender("bank@x.example", "某银行", verdict="important", from_domain="x.example")])
        self.r.app.store.cache_set(ME, "subscriptions", {**self.r.app.store.cache_get(ME, "subscriptions")[0],
                                                         "model_note": "有 1 个发件人未能分析，它们的建议仅依据收信和阅读情况。",
                                                         "model_check": {"reason": "model_error", "problem_code": "hub_not_running",
                                                                         "unchecked": 1, "of": 3}})
        self.r.app.store.cache_set(ME, "sender_kinds", {"from:bank@x.example": "transactional"})
        s = self.data("clean", account=ME)["subscriptions"]
        x = s["senders"][0]
        self.assertEqual((x["group"], x["verdict"], x["kind"], x["domain"]), ("keep", "important", "transactional", "x.example"))
        self.assertIn("未能分析", s["model_note"])
        self.assertEqual(s["model_check"], {"reason": "model_error", "problem_code": "hub_not_running", "unchecked": 1, "of": 3})
        self.assertEqual(self.data("sender", account=ME, key=x["key"])["kind"], "transactional")
        gmail = next(p for p in self.data("providers")["providers"] if p["key"] == "gmail")
        self.assertTrue(gmail["block"]["steps"] and gmail["block"]["link"]["url"].startswith("https://"))
        self.assertIn("purge_days", gmail)

    def test_why_sorting_background_and_learning_are_not_going(self):
        app = self.r.app
        with mock.patch.object(app, "has_model", lambda: False):
            self.assertEqual(self.data("accounts")["model"]["status"], "none")
        note = {"status": "downloading", "text": "螺壳正在下载模型（40%）。", "short": "正在下载模型", "progress": 0.4}
        with mock.patch.object(app, "has_model", lambda: True), mock.patch.object(app, "model_note", lambda: note):
            self.assertEqual(self.data("accounts")["model"], {"status": "downloading", "progress": 0.4, "text": note["text"]})
        with mock.patch.object(app, "has_model", lambda: True), mock.patch.object(app, "model_note", lambda: None):
            self.assertIsNone(self.data("accounts")["model"])
        auto = {"enabled": True, "every": 30, "intervals": [30], "desktop": {"supported": True, "installed": False},
                "service": False, "last": {"at": "2026-11-15 08:00", "ok": False, "text": "连不上邮箱"}, "next_in_min": 12}
        mv = {"sites": [{"id": 3, "url": "https://blog.example", "title": "", "posts": 0, "problem": "打不开（404）",
                         "fetched_at": 1763164800}],
              "learning": {"enabled": True, "on": True, "stage": "sent", "running": False, "sent_done": 40, "sent_total": 120,
                           "problem": "模型没有回应", "problem_code": "model_timeout", "waiting": 2, "learnt": 5}}
        with mock.patch.object(app, "auto_view", lambda: auto), mock.patch.object(app.curator, "memory_view", lambda acct: mv):
            st = self.data("settings")
        self.assertEqual((st["auto"]["stalled"], st["auto"]["next_in_min"], st["auto"]["last"]["ok"]), ("removed", 12, False))
        site = st["more"]["sites"][0]
        self.assertEqual((site["problem"], site["error"]), (True, "打不开（404）"))
        self.assertTrue(site["checked_at"].startswith("2025-11-15") or site["checked_at"].startswith("2025-11-14"))
        self.assertEqual({k: st["more"]["learn"][k] for k in ("sent_done", "sent_total", "problem", "problem_code", "learnt")},
                         {"sent_done": 40, "sent_total": 120, "problem": "模型没有回应", "problem_code": "model_timeout", "learnt": 5})
        with mock.patch.object(app, "auto_view", lambda: {**auto, "desktop": {"supported": True, "installed": True, "stale": True}}):
            self.assertEqual(self.data("settings")["auto"]["stalled"], "moved")
        with mock.patch.object(app, "auto_view", lambda: {**auto, "service": True}):
            self.assertIsNone(self.data("settings")["auto"]["stalled"])          # this is the service: running

    def test_a_senders_history_is_counted_before_it_is_cleared(self):
        from test_window_clean import sender
        for n in (1, 2):                                                    # Gmail: All Mail has the inbox too
            self.r.srv._add("[Gmail]/All Mail", mail(200 + n, f"旧通知 {n}", "某平台 <notice@plat.example>", 40), {"\\Seen"})
        self.checked([sender("notice@plat.example", "某平台", count=2)])
        key = self.data("clean", account=ME)["subscriptions"]["senders"][0]["key"]
        x = self.data("sender", account=ME, key=key)
        self.assertEqual((x["name"], x["history_state"], x["history"]["count"]), ("某平台", "ready", 2))
        self.assertGreater(x["history"]["size"], 0)
        self.assertEqual(len([m for m in self.r.srv.folders["[Gmail]/All Mail"] if m["mid"] in ("<o201@x.example>", "<o202@x.example>")]), 2)
        self.assertEqual(self.data("sender", account=ME, key="from:x@y.example")["error"]["code"], "stale")

    def test_a_busy_mailbox_leaves_the_senders_page_without_the_count_for_now(self):
        """Counting a sender's mail is asked of the mailbox in the background: a busy mailbox (reading thousands of
        old mails) no longer holds the whole page; asking again does not ask the mailbox again (11-18)."""
        from test_window_clean import sender
        app = self.r.app
        self.checked([sender("notice@plat.example", "某平台", count=2)])
        key = self.data("clean", account=ME)["subscriptions"]["senders"][0]["key"]
        go, asked = threading.Event(), []
        real = app.sender_act

        def slow(body):
            if body.get("op") == "history_preview":
                asked.append(1)
                go.wait(10)
            return real(body)
        with mock.patch.object(app, "sender_act", slow), mock.patch.object(app, "HISTORY_WAIT", 0.2):
            x = self.data("sender", account=ME, key=key)
            self.assertEqual((x["name"], x["history_state"]), ("某平台", "pending"))
            self.assertNotIn("history", x)
            self.assertEqual(self.data("sender", account=ME, key=key)["history_state"], "pending")
            go.set()
            for _ in range(100):
                if app._histories[(ME, key)]["done"].is_set():
                    break
                time.sleep(0.05)
            x = self.data("sender", account=ME, key=key)
        self.assertEqual((x["history_state"], x["history"]["count"]), ("ready", 0))
        self.assertEqual(len(asked), 1)

        with mock.patch.object(app, "open_mailbox", side_effect=LoginRefused("授权码已失效")):
            app.sender_act({"source": ME, "key": key, "op": "keep"})        # anything done with the sender: asked again
            x = self.data("sender", account=ME, key=key)
        self.assertEqual((x["name"], x["history_state"], x["history_error_code"]), ("某平台", "error", "login_refused"))
        self.assertNotIn("history", x)

    def test_senders_to_unsubscribe_from_one_click_and_kept(self):
        from test_window_clean import SHOP, sender
        self.checked([SHOP, sender("one@x.example", "早报", method="one_click", unsub_http="https://x.example/u"),
                      sender("web@x.example", "周报", method="web", unsub_http="https://x.example/web"),
                      sender("bad@x.example", "冒充", method="one_click", unsub_http="https://bad.example/u",
                             verdict="never", suspicious=True)])
        by = {x["name"]: x for x in self.to_unsubscribe("all")}
        self.assertEqual(set(by), {"早报", "周报"})                      # not 无法退订, never the suspicious
        self.assertEqual((by["早报"]["one_click"], by["早报"]["per_week"]), (True, 0.5))
        self.assertEqual(by["周报"]["unsubscribe_url"], "https://x.example/web")
        self.assertTrue(self.call("mail_unsubscribe", {"account": ME, "key": by["周报"]["key"]}).is_error)   # not one click
        r = self.call("mail_sender_keep", {"account": ME, "key": by["周报"]["key"]})
        self.assertFalse(r.is_error, r.text)
        with mock.patch.object(subscriptions, "one_click", lambda url: (True, "")):
            r = self.call("mail_unsubscribe", {"account": ME, "key": by["早报"]["key"]})
        self.assertFalse(r.is_error, r.text)
        self.assertEqual(self.to_unsubscribe(ME), [])

    def test_several_unsubscribed_in_one_tap_and_a_refusal_goes_to_its_page(self):
        from test_window_clean import sender
        self.checked([sender("one@x.example", "早报", method="one_click", unsub_http="https://x.example/u"),
                      sender("two@x.example", "晚报", method="one_click", unsub_http="https://x.example/v"),
                      sender("web@x.example", "周报", method="web", unsub_http="https://x.example/web")])
        keys = {x["name"]: x["key"] for x in self.to_unsubscribe(ME)}
        r = self.call("mail_unsubscribe", {"account": ME, "keys": [keys["早报"], keys["周报"]]})
        self.assertTrue(r.is_error)                                          # 周报 takes no one click: none of them
        refused = lambda url: (url.endswith("/u"), "")                       # noqa: E731 -- 晚报's server says no
        with mock.patch.object(subscriptions, "one_click", refused):
            r = self.call("mail_unsubscribe", {"account": ME, "keys": [keys["早报"], keys["晚报"]]})
        self.assertFalse(r.is_error, r.text)
        self.assertEqual([x["name"] for x in r.structured["done"]], ["早报"])
        self.assertEqual(r.structured["web"], [{"key": keys["晚报"], "name": "晚报", "open_url": "https://x.example/v"}])
        self.assertIn("1 个需要在网页上退订", r.text)

    def test_a_number_setting_takes_its_text(self):
        self.assertFalse(self.call("mail_settings", {"key": "auto_every", "value": "60"}).is_error)
        self.assertEqual(self.r.app.cfg["auto"]["every"], 60)
        self.assertTrue(self.call("mail_settings", {"key": "auto_every", "value": "半小时"}).is_error)

    def test_old_mail_archived_by_group_or_kept(self):
        self.r.app.start_old_scan({"source": ME, "scope": "inbox"})
        self.settle()
        rows = self.r.app.store.old_rows(ME, "inbox")
        self.r.app.store.old_judge(ME, {x["message_id"]: "personal" for x in rows}, "t")
        a = self.data("clean", account=ME)["archive"]
        self.assertTrue(a["groups"])
        g = a["groups"][0]
        self.assertTrue(g["earliest"] <= g["latest"])
        r = self.call("mail_old_archive", {"account": ME, "keys": [g["key"]], "count": g["count"] + 1})
        self.assertTrue(r.is_error)                                          # not the count the user saw
        self.assertFalse(self.call("mail_old_keep", {"account": ME, "key": g["key"]}).is_error)
        self.assertNotIn(g["key"], [x["key"] for x in self.data("clean", account=ME)["archive"]["groups"]])
        self.assertFalse(self.call("mail_old_keep", {"account": ME, "key": g["key"], "on": False}).is_error)
        r = self.call("mail_old_archive", {"account": ME, "keys": [g["key"]], "count": g["count"]})
        self.assertFalse(r.is_error, r.text)
        self.settle()
        self.assertNotIn(g["key"], [x["key"] for x in self.data("clean", account=ME)["archive"]["groups"]])
        self.assertEqual(self.r.app.store.cache_get(ME, "keep:inbox")[0], [])          # archived, not kept

    def test_archived_mail_read_by_group_and_trashed_in_the_mode(self):
        all_mail = "[Gmail]/All Mail"
        for n in (60, 61, 62):
            self.r.srv._add(all_mail, mail(n, f"促销 {n}", "某店 <ad@store.example>", 200, "ad.store.example"), {"\\Seen"})
        r = self.call("mail_old_scan", {"account": ME, "scope": "archive"})
        self.assertFalse(r.is_error, r.text)
        self.settle()
        self.assertEqual(self.data("job", id=r.structured["job"])["status"], "done")
        o = self.data("old", account=ME, scope="archive")
        g = next(g for g in o["groups"] if g["address"] == "ad@store.example")
        self.assertEqual((g["action"], g["judged"], g["suggested"], g["count"]), ("trash", False, False, 3))   # 未分析
        mids = [x["message_id"] for x in self.r.app.store.old_rows(ME, "archive") if x["from_addr"] == "ad@store.example"]
        self.r.app.store.old_judge(ME, {m: "promotion" for m in mids}, "t")
        g = next(g for g in self.data("old", account=ME, scope="archive")["groups"] if g["address"] == "ad@store.example")
        self.assertEqual((g["kind"], g["judged"], g["suggested"]), ("promotion", True, True))    # 建议删除
        self.r.app.store.cache_set(ME, "judge:archive", {"on": True, "problem": "模型没有回应", "problem_code": "model_timeout"})
        j = self.data("old", account=ME, scope="archive")["judging"]
        self.assertEqual((j["problem"], j["problem_code"]), ("模型没有回应", "model_timeout"))
        args = {"account": ME, "scope": "archive", "keys": [g["key"]], "count": g["count"]}
        self.assertTrue(self.call("mail_old_archive", {**args, "scope": "inbox"}).is_error)     # not a group to archive
        self.assertTrue(self.call("mail_old_trash", {**args, "count": 1}).is_error)             # not what the user saw
        self.r.app.cfg["mode"]["write"] = False
        self.assertTrue(self.call("mail_old_trash", args).is_error)                              # 处理模式 first
        self.r.app.cfg["mode"]["write"] = True
        r = self.call("mail_old_trash", args)
        self.assertFalse(r.is_error, r.text)
        self.settle()
        j = self.data("job", id=r.structured["job"])
        self.assertEqual((j["kind"], j["status"], j["result"]["moved"]), ("clean", "done", 3))
        ours = lambda: [m for m in self.r.srv.folders[all_mail] if m["mid"] in mids]      # noqa: E731
        self.assertEqual(ours(), [])
        self.assertFalse(self.call("mail_undo_step", {"account": ME, "log_id": j["result"]["log_id"]}).is_error)
        self.assertEqual(len(ours()), 3)
        self.assertEqual(self.data("job", id="nope")["error"]["code"], "stale")

    def test_a_job_for_every_mailbox_and_one_that_cannot_start(self):
        two = "two@example.com"

        def start(body):
            if body["source"] == two:
                raise ValueError("邮箱的授权码丢了。")
            return {"id": "j-" + body["source"]}
        with mock.patch.object(self.r.app, "mail_accounts", lambda: [ME, two]), \
                mock.patch.object(self.r.app, "start_sync", start), mock.patch.object(self.r.app, "start_old_scan", start):
            r = self.call("mail_sync", {"account": "all"})
            self.assertEqual((r.structured["jobs"], r.structured["failed"][0]["account"]), (["j-" + ME], two))
            r = self.call("mail_old_scan", {"account": "all", "scope": "archive"})
            self.assertEqual(r.structured["job"], "j-" + ME)
            self.assertTrue(self.call("mail_sync", {"account": two}).is_error)

    def test_a_filter_preview_is_read_from_its_job(self):
        self.r.app.jobs["f1"] = {"id": "f1", "kind": "filter_preview", "account": ME, "status": "done", "stage": "",
                                 "done": 0, "total": 0, "error": None, "cancel": False,
                                 "result": {"rule": {"id": 3, "kind": "sender", "value": "a@x.example", "action": "ignore"},
                                            "checked": True, "advice": "过去一年会命中 4 封。",
                                            "ways": [{"kind": "sender", "value": "a@x.example", "count": 4, "judged": 4,
                                                      "risky": 1, "examples": ["您的验证码是 482913"], "label": "发件人"}]}}
        j = self.data("job", id="f1")
        self.assertEqual(j["result"]["ways"][0]["risky"], 1)
        self.assertNotIn("advice", j["result"])                              # numbers, no sentence put together
        self.assertNotIn("482913", json.dumps(j, ensure_ascii=False))

    def settle(self):
        import time
        for _ in range(500):
            if not any(j["status"] == "running" for j in list(self.r.app.jobs.values())):
                return
            time.sleep(0.02)

    # ---- data/settings, data/activity -------------------------------------------------------------------------
    def test_settings_as_values_and_changed_one_at_a_time(self):
        st = self.data("settings")
        self.assertEqual(st["accounts"][0]["id"], ME)
        self.assertEqual(st["rules"], [])
        self.assertIn("mark_read", st["more"])
        r = self.call("mail_rule_add", {"account": ME, "kind": "sender", "value": "news@weekly.example", "action": "archive"})
        self.assertFalse(r.is_error, r.text)
        rule = self.data("settings")["rules"][0]
        self.assertEqual((rule["kind"], rule["value"], rule["action"], rule["account"]),
                         ("sender", "news@weekly.example", "archive", ME))
        self.assertFalse(self.call("mail_rule_delete", {"account": ME, "id": rule["id"]}).is_error)
        self.assertEqual(self.data("settings")["rules"], [])
        was = self.data("settings")["more"]["mark_read"]
        self.assertFalse(self.call("mail_settings", {"key": "mark_read", "value": not was}).is_error)
        self.assertEqual(self.data("settings")["more"]["mark_read"], not was)
        self.assertTrue(self.call("mail_settings", {"key": "engine", "value": "x"}).is_error)   # not one of them
        self.assertFalse(self.call("mail_set_name", {"account": ME, "name": "工作"}).is_error)
        self.assertEqual(self.data("accounts")["accounts"][0]["name"], "工作")
        r = self.call("mail_settings", {"key": "mark_read", "value": True}, tap=None)
        self.assertEqual(r.structured["code"], "needs_user_action")

    def test_the_record_of_every_step_a_page_at_a_time(self):
        it = self.item("本周精选")
        with mock.patch.object(self.r.app, "_acted", lambda *a, **k: 0):     # the memory is shared by the tests
            self.call("mail_act", {"account": ME, "ids": [it["id"]], "op": "archive"})
        a = self.data("activity", account="all")
        step = a["items"][0]
        self.assertEqual((step["by"], step["op"], step["count"], step["undoable"]), ("user", "archive", 1, True))
        self.assertEqual(step["mail"]["subject"], "本周精选")
        self.assertEqual([(x["id"], x["subject"]) for x in step["mails"]], [(it["id"], "本周精选")])
        ids = [x["id"] for x in self.r.app.store.items(ME, None, None)]
        self.r.app.store.log(ME, "archive", ids, True, "多封", [{"kind": "noop"}])
        from luoke_mail import agent_data
        with mock.patch.object(agent_data, "MAILS_MOST", 3):                    # more than are listed
            big = self.data("activity", account=ME)["items"][0]
        self.assertEqual((len(big["mails"]), big["more"]), (3, len(ids) - 3))
        self.assertEqual(big["mails"], sorted(big["mails"], key=lambda x: x["date"] or "", reverse=True))
        for n in range(205):
            self.r.app.store.log(ME, "rule", [], True, f"第 {n} 步", [{"kind": "noop"}])
        a = self.data("activity", account=ME)
        self.assertEqual((len(a["items"]), a["more"]), (200, True))
        rest = self.data("activity", account=ME, cursor=a["cursor"])
        self.assertTrue(rest["items"] and all(x["log_id"] < int(a["cursor"]) for x in rest["items"]))

    # ---- data/mail --------------------------------------------------------------------------------------------
    def test_one_mail_with_its_text_and_what_can_be_done(self):
        it = self.item("答辩时间确认")
        m = self.data("mail", id=it["id"])
        self.assertEqual((m["account"], m["subject"], m["from"]["address"]), (ME, "答辩时间确认", it["from_addr"]))
        self.assertTrue(m["body_text"])
        self.assertEqual(m["ways"][0], "draft")
        self.assertIn("ask_text", m)
        self.assertTrue(m["to"])                                              # decoded (it was always [])
        code = self.data("mail", id=self.item("登录验证码")["id"])
        self.assertNotIn("482913", json.dumps(code, ensure_ascii=False))

    def test_every_appointment_of_a_mail_and_archived_mail_still_to_do(self):
        it = self.item("项目周会")
        ev = {"title": "去程", "date": "2020-01-01", "start": "08:00", "end": None, "location": None,
              "more": [{"title": "回程", "date": "2099-01-05", "start": "18:00", "end": None, "location": "机场"}]}
        st = self.r.app.store
        st.db.execute("UPDATE items SET event_json=? WHERE id=?", (json.dumps(ev, ensure_ascii=False), it["id"]))
        st.db.execute("UPDATE items SET place='archive' WHERE id=?", (self.item("合同扫描件")["id"],))
        st.db.commit()
        e = self.data("mail", id=it["id"])["events"]
        self.assertEqual([(x["index"], x["title"], x["past"], x["added"]) for x in e],
                         [(0, "去程", True, False), (1, "回程", False, False)])
        a = self.data("inbox", account=ME, list="archived")
        self.assertEqual(([x["subject"] for x in a["items"]], a["counts"]["archived"]), (["合同扫描件"], 1))
        self.assertNotIn("合同扫描件", [x["subject"] for x in self.data("inbox", account=ME, list="all")["items"]])

    # ---- actions ----------------------------------------------------------------------------------------------
    def test_acting_needs_the_tap_and_is_undone(self):
        it = self.item("本周精选")
        r = self.c.call_tool("mail_act", {"account": ME, "ids": [it["id"]], "op": "archive"})
        self.assertEqual(r.structured["code"], "needs_user_action")
        self.assertIn("本周精选", self.r.inbox())
        with mock.patch.object(self.r.app, "_acted", lambda *a, **k: 0):     # the memory is shared by the tests
            r = self.call("mail_act", {"account": ME, "ids": [it["id"]], "op": "archive"})
        self.assertFalse(r.is_error, r.text)
        self.assertNotIn("本周精选", self.r.inbox())
        log_id = r.structured["log_id"]
        r = self.call("mail_undo_step", {"account": ME, "log_id": log_id})
        self.assertFalse(r.is_error, r.text)
        self.assertIn("本周精选", self.r.inbox())

    def test_an_act_on_an_unknown_op_or_mailbox_is_refused(self):
        it = self.item("本周精选")
        self.assertTrue(self.call("mail_act", {"account": ME, "ids": [it["id"]], "op": "forward"}).is_error)
        self.assertTrue(self.call("mail_act", {"account": "x@example.com", "ids": [it["id"]], "op": "archive"}).is_error)
        self.assertTrue(self.call("mail_undo_step", {"account": ME, "log_id": 999999}).is_error)

    def test_an_appointment_goes_to_the_calendar(self):
        it = self.item("项目周会")
        with mock.patch.object(desktop, "open_url") as opened:
            r = self.call("mail_calendar", {"account": ME, "id": it["id"]})
        self.assertFalse(r.is_error, r.text)
        self.assertTrue(opened.call_args[0][0].endswith(".ics"))


if __name__ == "__main__":
    unittest.main()
