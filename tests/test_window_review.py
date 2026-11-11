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

    def page(self, name: str) -> "a2ui.Surface":
        messages = self.w.draw(name)
        self.assertEqual(a2ui.validate(messages), [], name)
        return a2ui.build(messages)

    def buttons(self, s) -> list:
        return [s.components[c["child"]].get("text") for c in s.components.values() if c.get("component") == "Button"]

    def item(self, subject: str) -> dict:
        return next(it for it in self.r.app.store.items(ME, None, None) if it["subject"] == subject)


class TodayTests(PagesRig):
    def test_the_day_lists_each_message_and_what_became_of_it(self):
        s = self.page("today")
        words = self.texts(s)
        for w in ("答辩时间确认", "等你处理", "可疑", "已读，留在收件箱", "今天"):
            self.assertIn(w, words)
        self.assertNotIn("482913", words)                                    # a code from mail is masked
        self.assertTrue(self.r.app.store.cache_get(ME, "review_seen")[0])     # looked at: the badge clears
        self.assertFalse(self.press("today", "昨天").is_error)
        self.assertIn("这一天没有处理过的邮件。", self.texts(self.page("today")))
        self.press("today", "今天")
        self.assertIn("答辩时间确认", self.texts(self.page("today")))

    def test_the_header_filters_to_one_group(self):
        s = self.page("today")
        label = next(b for b in self.buttons(s) if b.startswith("等你处理 "))
        self.press("today", label)
        words = self.texts(self.page("today"))
        self.assertIn("答辩时间确认", words)
        self.assertNotIn("本周精选", words)                                  # 留在收件箱 is another group
        self.press("today", next(b for b in self.buttons(self.page("today")) if b.startswith("全部 ")))
        self.assertIn("本周精选", self.texts(self.page("today")))

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

    def test_marked_unread_and_changed_in_place(self):
        it = self.item("合同扫描件")
        self.r.app.store.set_fields(it["id"], read_by_us="2026-11-09 09:00:00")
        r = self.press("today", "标为未读", on="合同扫描件")
        self.assertFalse(r.is_error, r.text)
        self.assertEqual(self.r.app.store.get(it["id"])["read_by_us"], "unread")
        self.assertNotIn("标为未读", [b for b in self.buttons(self.page("today"))])
        self.press("today", "改为…", on="合同扫描件")
        self.assertIn("改好了", self.buttons(self.page("today")))
        r = self.press("today", "改好了", on="合同扫描件", choices={f"a{it['id']}": ["archive"]})
        self.assertFalse(r.is_error, r.text)
        self.assertIn("已改为「归档」", r.text)
        self.assertEqual(self.r.app.store.get(it["id"])["action"], "archive")
        self.assertNotIn("改好了", self.buttons(self.page("today")))         # the picker closes once it is changed

    def test_unread_mail_from_before_has_its_own_section(self):
        self.r.app.store.set_fields(self.item("答辩时间确认")["id"], older=1)
        words = self.texts(self.page("today"))
        self.assertIn("以前的未读", words)
        self.assertIn("以前一直没读的邮件，螺壳看过并标为已读。", " ".join(words.split()))


class RulesTests(PagesRig):
    def test_a_rule_is_added_and_deleted_after_confirming_then_undone(self):
        r = self.press("rules", "添加", fields={"value": "news@weekly.example"},
                       choices={"kind": ["sender"], "action": ["archive"]})
        self.assertFalse(r.is_error, r.text)
        self.assertEqual([(x["kind"], x["value"]) for x in self.r.app.store.rules(ME)], [("sender", "news@weekly.example")])
        text = "发件人「news@weekly.example」→ 归档"
        self.assertIn(text, self.texts(self.page("rules")))
        self.press("rules", "删除", on=text)
        self.assertEqual(len(self.r.app.store.rules(ME)), 1)                 # not yet: the page asks first
        self.assertIn(f"删除规则：{text}？", self.texts(self.page("rules")))
        self.assertFalse(self.press("rules", "确认").is_error)
        self.assertEqual(self.r.app.store.rules(ME), [])
        self.assertFalse(self.press("rules", "撤销").is_error)
        self.assertEqual(len(self.r.app.store.rules(ME)), 1)

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
        self.assertIn("第 1–10 条，共 25 条", self.texts(s))
        self.press("rules", "下一页")
        self.assertIn("第 11–20 条，共 25 条", self.texts(self.page("rules")))


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
        self.assertIn("第 1–10 条，共 50 条", self.texts(s))
        self.press("activity", "下一页")
        self.assertIn("第 11–20 条，共 50 条", self.texts(self.page("activity")))


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
