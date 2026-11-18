"""今日处理, 规则 and 处理记录 as pages in 螺壳's window: drawn from the same views as the browser
page, valid for the protocol, and acted on only through the buttons the page drew."""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
import agent_a2ui as a2ui  # noqa: E402
from fakes import FakeChat  # noqa: E402
from test_background import triage_reply  # noqa: E402
from test_old_mail import ME, mail  # noqa: E402
from test_agent import said  # noqa: E402
from test_window import WindowRig  # noqa: E402
from luoke_mail import memory  # noqa: E402
from luoke_mail.window.pages import activity, rules, today  # noqa: E402

HABIT = [{"note_id": 7, "sender": "list:promo.shop.example", "category": "promotion", "action": "archive",
          "text": "某某商城的推广邮件都归档"}]


class PagesRig(WindowRig):
    def setUp(self):
        super().setUp()
        w = self.w = self.r.app.agent.window
        w.builders = {**w.builders, "today": today.build, "rules": rules.build, "activity": activity.build}

    def buttons(self, s) -> list:
        return [said(s.components, cid) for cid, c in s.components.items() if c.get("component") == "Button"]

    def item(self, subject: str) -> dict:
        return next(it for it in self.r.app.store.items(ME, None, None) if it["subject"] == subject)


class TodayTests(PagesRig):
    """设计规范 §18.2: what was done for the user on top, then the few to decide, then a line for the replies."""

    def test_what_was_done_comes_first_then_what_to_decide_then_the_replies(self):
        s = self.page("today")
        words = self.texts(s)
        for w in ("今天替你处理了 3 封", "已读，留在收件箱", "要你定的", "账户已冻结", "合同扫描件", "还有 1 封等你回复。"):
            self.assertIn(w, words)
        self.assertLess(words.index("今天替你处理了"), words.index("要你定的"))
        self.assertLess(words.index("要你定的"), words.index("等你回复"))
        self.assertNotIn("等你处理", words)                                  # no heading of what waits (§18.1)
        self.assertNotIn("答辩时间确认", words)                              # a reply is answered in the inbox
        self.assertNotIn("482913", words)                                    # a code from mail is masked
        self.assertTrue(self.r.app.store.cache_get(ME, "review_seen")[0])     # looked at: the badge clears
        pickers = [c for c in s.components.values() if c.get("component") == "ChoicePicker"]
        self.assertEqual([o["label"] for o in pickers[0]["options"]], ["移到垃圾邮件", "留在收件箱"])   # suspicious first
        for c in pickers:                                                    # the suggestion, picked already
            self.assertEqual(s.data["choices"][c["value"]["path"].split("/")[-1]], [c["options"][0]["value"]])
        self.assertIsNone(self.press("today", "去收件箱"))
        self.assertEqual(self.opened["inbox"], {"box": ME, "list": "reply"})

    def test_the_choices_are_carried_out_together_and_taken_back_together(self):
        contract, meeting = self.item("合同扫描件"), self.item("项目周会")
        r = self.press("today", "按选择处理", choices={f"a{contract['id']}": ["archive"], f"a{meeting['id']}": ["skip"]})
        self.assertFalse(r.is_error, r.text)
        self.assertIn("处理好了 3 封", r.text)
        inbox = self.r.inbox()
        self.assertNotIn("合同扫描件", inbox)
        self.assertNotIn("账户已冻结", inbox)                                # its suggestion: 移到垃圾邮件
        self.assertIn("项目周会", inbox)                                     # 留在收件箱
        self.assertNotIn("要你定的", self.texts(self.page("today")))
        self.assertFalse(self.press("today", "撤销").is_error)
        inbox = self.r.inbox()
        self.assertIn("合同扫描件", inbox)
        self.assertIn("账户已冻结", inbox)

    def test_at_most_three_to_decide_a_day(self):
        with mock.patch.object(today, "DECIDE_AT_MOST", 2):
            words = self.texts(self.page("today"))
        self.assertIn("另有 1 封在收件箱里。", words)
        self.assertNotIn("项目周会", words)                                  # suspicious first, then what is late

    def test_a_habits_work_is_undone_from_the_page(self):
        self.r.srv._add("INBOX", mail(90, "周末促销", "某某商城 <news@shop.example>", 0, "promo.shop.example"), set())
        with mock.patch.object(memory, "auto_habits", lambda mem, acct: HABIT), FakeChat(triage_reply) as chat:
            self.r.app.cfg["engine"]["base_url"] = chat.base_url
            self.r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
            self.assertNotIn("周末促销", self.r.inbox())
            self.assertIn("已自动归档", self.texts(self.page("today")))
            r = self.press("today", "撤销", on="周末促销")
            self.assertFalse(r.is_error, r.text)
            self.assertIn("周末促销", self.r.inbox())
            self.assertIn("已撤销，邮件回到收件箱", self.texts(self.page("today")))

    def test_marked_read_is_taken_back_to_unread(self):
        it = self.item("本周精选")
        self.r.app.store.set_fields(it["id"], read_by_us="2026-11-09 09:00:00")
        r = self.press("today", "撤销", on="本周精选")
        self.assertFalse(r.is_error, r.text)
        self.assertEqual(self.r.app.store.get(it["id"])["read_by_us"], "unread")

    def test_unread_mail_from_before_is_one_line(self):
        self.r.app.store.set_fields(self.item("本周精选")["id"], older=1)
        words = self.texts(self.page("today"))
        self.assertIn("以前一直没读的 1 封，螺壳看过，标为已读。", words)
        self.assertIn("今天替你处理了 2 封", words)


class RulesTests(PagesRig):
    def test_a_rule_is_added_and_deleted_after_confirming_then_undone(self):
        r = self.press("rules", "添加", fields={"value": "news@weekly.example"},
                       choices={"kind": ["sender"], "action": ["archive"]})
        self.assertFalse(r.is_error, r.text)
        self.assertEqual([(x["kind"], x["value"]) for x in self.r.app.store.rules(ME)], [("sender", "news@weekly.example")])
        text = "发件人「news@weekly.example」→ 归档"
        self.assertIn(text, self.texts(self.page("rules")))
        s = self.page("rules")
        delete = s.components[self.in_card(s, "删除", text)]
        self.assertEqual(delete["confirm"]["text"], f"删除规则：{text}？")      # the window asks first (§4 confirm)
        self.assertFalse(self.press("rules", "删除", on=text).is_error)
        self.assertEqual(self.r.app.store.rules(ME), [])
        self.assertFalse(self.press("rules", "撤销").is_error)
        self.assertEqual(len(self.r.app.store.rules(ME)), 1)

    def test_a_list_rule_is_named_by_who_sends_the_list(self):
        """The audit 11-11: a list rule showed its List-Id, a run of hex. It shows the list's sender; a list with
        no mail here keeps its id."""
        for value in ("promo.shop.example", "0f3a9c.list-id.example"):
            self.r.app.store.add_rule(ME, "list", value, "archive")
        words = self.texts(self.page("rules"))
        self.assertIn("邮件列表「某某商城」→ 归档", words)
        self.assertNotIn("「promo.shop.example」", words)
        self.assertIn("邮件列表「0f3a9c.list-id.example」→ 归档", words)

    def test_an_incomplete_rule_is_refused(self):
        r = self.press("rules", "添加", fields={"value": " "}, choices={"kind": ["sender"], "action": ["archive"]})
        self.assertTrue(r.is_error)
        self.assertEqual(self.r.app.store.rules(ME), [])

    def test_the_habits_followed_are_listed(self):
        with mock.patch.object(memory, "auto_habits", lambda mem, acct: HABIT):
            words = self.texts(self.page("rules"))
        self.assertIn("按习惯自动做的", words)
        self.assertIn("某某商城的推广邮件都归档", words)

    def test_a_gmail_filter_is_tried_on_the_past_year(self):
        self.r.app.add_rule({"source": ME, "kind": "sender", "value": "news@weekly.example", "action": "archive"})
        words = self.texts(self.page("rules"))
        self.assertIn("导出为 Gmail 过滤器", words)
        r = self.press("rules", "预演 Gmail 过滤器")
        self.assertFalse(r.is_error, r.text)
        until = time.time() + 20
        while any(j["status"] == "running" for j in self.r.app.jobs.values()) and time.time() < until:
            time.sleep(0.1)
        self.assertIn("过去一年", self.texts(self.page("rules")))

    def test_many_rules_a_screen_at_a_time(self):
        for n in range(25):
            self.r.app.add_rule({"source": ME, "kind": "domain", "value": f"d{n}.example", "action": "ignore"})
        s = self.page("rules")
        self.assertIn("第 1–20 条，共 25 条", self.texts(s))                 # §4 bigPages: twice as many
        self.press("rules", "下一页")
        self.assertIn("第 21–25 条，共 25 条", self.texts(self.page("rules")))


class ActivityTests(PagesRig):
    def test_each_step_is_listed_and_undone_from_the_page(self):
        it = self.item("本周精选")
        self.r.app.items_act(ME, [it["id"]], "archive")
        self.assertNotIn("本周精选", self.r.inbox())
        s = self.page("activity")
        self.assertIn("本周精选", self.texts(s))
        r = self.press("activity", "撤销", on=next(c["text"] for c in s.components.values()
                                                   if c.get("component") == "Text" and "本周精选" in c.get("text", "")))
        self.assertFalse(r.is_error, r.text)
        self.assertIn("本周精选", self.r.inbox())
        self.assertIn("（已撤销）", self.texts(self.page("activity")))

    def test_a_long_record_a_screen_at_a_time(self):
        for n in range(60):
            self.r.app.store.log(ME, "rule", [], True, f"第 {n} 步", [{"kind": "noop"}])
        s = self.page("activity")
        self.assertIn("第 1–20 条，共 50 条", self.texts(s))
        self.press("activity", "下一页")
        self.assertIn("第 21–40 条，共 50 条", self.texts(self.page("activity")))


class EmptyTests(PagesRig):
    def test_no_mailbox_the_sample_and_an_empty_one(self):
        self.r.app.store.log(ME, "rule", [], True, "x")              # something on 处理记录 before it is emptied
        for pid in ("today", "rules", "activity"):
            self.page(pid)
        with mock.patch.object(self.w, "source", lambda page: "demo"):
            for pid in ("today", "rules", "activity"):
                self.page(pid)
        with mock.patch.object(self.r.app, "mail_accounts", lambda: []):
            for pid in ("today", "rules", "activity"):
                self.assertIn("还没有连接邮箱。", self.texts(self.page(pid)))


if __name__ == "__main__":
    unittest.main()
