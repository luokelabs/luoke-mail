"""What 螺壳's window draws since protocol §4's 2026-11-10 additions, and what an older core gets instead:
a page opened at one mail (pageArgs), 400 components (bigPages), a page read again while a job runs (pageRefresh),
the app password typed on the page (secureField), the Gmail filter file handed to a save dialog (saveFile)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
import agent_a2ui as a2ui  # noqa: E402
from test_agent import labelled  # noqa: E402
from test_window import WindowRig  # noqa: E402
from luoke_mail import agent_mcp, server  # noqa: E402
from test_old_mail import ME  # noqa: E402

OLD_CORE = mock.patch.object(agent_mcp, "CLIENT_CAPABILITIES", {})      # a core that lists none of §4's additions


def events(s, name: str) -> list:
    return [c["action"]["event"].get("context") for c in s.components.values()
            if c.get("component") == "Button" and c["action"]["event"]["name"] == name]


class OpenAtOneMailTests(WindowRig):
    def card(self, kind: str) -> "a2ui.Surface":
        it = next(i for i in self.c.outbox("luoke-mail") if i["kind"] == kind)
        return a2ui.build(it["card"])

    def test_the_card_opens_the_inbox_at_its_mail_and_back_to_the_list(self):
        s = self.card("suspicious")
        ctx = events(s, "agent.open_page")[0]
        self.assertEqual(ctx["page"], "inbox")
        self.opened["inbox"] = ctx["args"]
        words = self.texts(self.page("inbox"))
        self.assertIn("账户已冻结", words)
        self.assertIn("返回列表", [c.get("text") for c in self.page("inbox").components.values()])
        self.assertIsNone(self.press("inbox", "返回列表"))                       # agent.open_page, no args
        self.assertEqual(self.opened["inbox"], {})
        self.assertIn("答辩时间确认", self.texts(self.page("inbox")))

    def test_a_card_about_one_mail_opens_at_it_and_one_about_several_at_the_list(self):
        ctx = events(self.card("new-mail"), "agent.open_page")[0]
        self.assertEqual(ctx, {"page": "inbox", "args": {"box": ME, "list": "needs"}})   # more wait: that list
        self.opened["inbox"] = ctx["args"]
        self.assertIn("合同扫描件", self.texts(self.page("inbox")))
        store = self.r.app.store
        for it in store.items(self.r.app.account(None)):
            if it["subject"] != "答辩时间确认" and it.get("action") in ("reply", "schedule", "follow_up"):
                store.set_fields(it["id"], state="done")
        ctx = events(self.card("new-mail"), "agent.open_page")[0]
        self.opened["inbox"] = ctx["args"]
        self.assertIn("写草稿", [c.get("text") for c in self.page("inbox").components.values()])
        self.assertIn("返回列表", [c.get("text") for c in self.page("inbox").components.values()])

    def test_a_mailboxs_list_is_opened_once_and_the_users_choice_stands(self):
        box = next(i for i in self.c.outbox("luoke-mail") if i["kind"] == "mailbox")
        ctx = events(a2ui.build(box["card"]), "agent.open_page")[0]
        self.assertEqual(ctx["args"], {"box": ME, "list": "needs"})
        self.opened["inbox"] = {"box": ME, "list": "suspicious"}
        words = self.texts(self.page("inbox"))
        self.assertIn("账户已冻结", words)
        self.assertNotIn("合同扫描件", words)                              # only what looks suspicious
        self.press("inbox", "全部")                                         # the user's own choice, same args
        self.assertIn("合同扫描件", self.texts(self.page("inbox")))

    def test_view_the_text_opens_the_page_at_that_mail(self):
        self.press("inbox", "查看原文")
        self.assertTrue(self.opened["inbox"].get("id"))
        self.assertIn("返回列表", [c.get("text") for c in self.page("inbox").components.values()])

    def test_a_mail_handled_since_says_so(self):
        self.opened["inbox"] = {"id": "999999"}
        self.assertIn("这封邮件已经处理过了", self.texts(self.page("inbox")))
        self.opened["inbox"] = {"id": "not a number"}
        self.assertIn("答辩时间确认", self.texts(self.page("inbox")))

    def test_an_older_core_has_the_browser_page_opened(self):
        with OLD_CORE:
            s = self.card("suspicious")
            self.assertEqual(events(s, "agent.open_page"), [])
            self.assertTrue(events(s, "mail_open"))
            self.assertFalse(events(self.page("inbox"), "agent.open_page"))


class BigPageTests(WindowRig):
    def test_a_page_takes_400_components_on_a_core_that_draws_them(self):
        w = self.r.app.agent.window
        for features, most in ((frozenset({"bigPages"}), 400), (frozenset(), 200)):
            messages = w.draw("activity", None, features)
            self.assertEqual(a2ui.validate(messages, page=True), [])
            p = next(iter(w._drawn["activity"].values().__reversed__()))
            self.assertEqual(p.max, most)


class RefreshTests(WindowRig):
    def running(self):
        job = {"id": "j1", "kind": "sync", "status": "running", "account": "x", "done": 1, "total": 4,
               "stage": "正在读取邮件", "cancel": False}
        self.r.app.jobs["j1"] = job
        self.addCleanup(self.r.app.jobs.pop, "j1", None)

    def root(self, s) -> dict:
        return s.components["root"]

    def test_a_page_with_a_job_running_is_read_again_without_a_button(self):
        self.running()
        s = self.page("activity")
        self.assertEqual(self.root(s).get("refresh"), 3)
        self.assertNotIn("刷新", [c.get("text") for c in s.components.values()])

    def test_a_page_with_inputs_or_an_older_core_has_the_button(self):
        self.running()
        s = self.page("rules")                                         # the 添加规则 field: not read again
        self.assertNotIn("refresh", self.root(s))
        self.assertIn("刷新", [c.get("text") for c in s.components.values()])
        with OLD_CORE:
            s = self.page("activity")
            self.assertNotIn("refresh", self.root(s))
            self.assertIn("刷新", [c.get("text") for c in s.components.values()])

    def test_nothing_running_nothing_to_read_again(self):
        self.assertNotIn("refresh", self.root(self.page("activity")))


class AddMailboxTests(WindowRig):
    SECRET = "pw-not-on-any-page-1234"

    def test_the_app_password_goes_to_the_keychain_and_nowhere_else(self):
        self.press("settings", "添加邮箱…")
        self.press("settings", "其他邮箱（手动填写 IMAP）")
        s = self.page("settings")
        secret = next(c for c in s.components.values() if c.get("variant") == "obscured")
        self.assertEqual(s.data["secrets"]["secret"], "")                # never a value from the tool
        self.assertEqual(secret["value"], {"path": "/secrets/secret"})
        r = self.press("settings", "保存并测试连接", fields={"address": "lucy@corp.example", "host": "127.0.0.1",
                                                             "port": "993"}, secrets={"secret": self.SECRET})
        self.assertFalse(r.is_error, r.text)
        self.assertIn("连接成功", r.text)
        self.assertEqual(self.r.app.secrets.get(server.MAIL_SECRET, "lucy@corp.example"), self.SECRET)
        self.assertIn("lucy@corp.example", self.r.app.mail_accounts())
        w = self.r.app.agent.window
        seen = json.dumps([r.text, r.structured, w.views, w.notes, w.confirming, self.c.read_json("page/settings")],
                          ensure_ascii=False, default=str)
        self.assertNotIn(self.SECRET, seen)

    def test_a_refused_password_says_so_and_the_form_stays(self):
        self.press("settings", "添加邮箱…")
        self.press("settings", "Gmail")
        r = self.press("settings", "保存并测试连接", fields={"address": "lucy@gmail.example"})
        self.assertTrue(r.is_error)
        self.assertIn("授权码", r.text)
        words = self.texts(self.page("settings"))
        self.assertIn("连不上？", words)
        self.assertIn("lucy@gmail.example", json.dumps(self.page("settings").data, ensure_ascii=False))

    def test_an_older_core_opens_the_browser_page(self):
        with OLD_CORE:
            s = self.page("settings")
            self.assertFalse([c for c in s.components.values() if c.get("variant") == "obscured"])
            bid = labelled(s, "添加邮箱…")
            self.assertEqual(s.find_action(bid)[0], "mail_do")
            self.assertNotIn("修改授权码…", [c.get("text") for c in s.components.values()])


class GmailFilterTests(WindowRig):
    def setUp(self):
        super().setUp()
        self.r.app.store.add_rule(self.r.app.account(None), "sender", "news@shop2.example", "archive")

    def export(self):
        r = self.press("rules", "导出到 Gmail（1 条）")
        self.assertIn("确认", r.text)
        self.assertIn("导入过滤器", self.texts(self.page("rules")))
        r = self.press("rules", "确认")
        self.assertFalse(r.is_error, r.text)
        return r

    def test_the_file_goes_to_the_save_dialog(self):
        r = self.export()
        f = r.structured["file"]
        self.assertEqual(f["mediaType"], "application/xml")
        self.assertTrue(f["name"].endswith(".xml"))
        self.assertIn("news@shop2.example", f["text"])
        self.assertIn("重新导出全部", [c.get("text") for c in self.page("rules").components.values()])

    def test_an_older_core_has_it_put_in_downloads(self):
        with OLD_CORE:
            r = self.export()
        self.assertNotIn("file", r.structured or {})
        self.assertIn("已存到「下载」", r.text)
        folder = os.environ["LUOKE_DOWNLOADS"]
        saved = [n for n in os.listdir(folder) if n.endswith(".xml")]
        self.assertTrue(saved)
        with open(os.path.join(folder, saved[-1]), encoding="utf-8") as f:
            self.assertIn("news@shop2.example", f.read())


if __name__ == "__main__":
    unittest.main()
