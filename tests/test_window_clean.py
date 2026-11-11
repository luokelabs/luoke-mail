"""清理 and 订阅体检 in 螺壳's window: drawn from the browser page's views, valid A2UI under the 200
limit, acting only through the routes the browser page uses, irreversible steps behind 确认 and 处理模式."""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
import agent_a2ui as a2ui  # noqa: E402
from luoke_mail.window import kit, pages  # noqa: E402
from luoke_mail.window.pages import clean, subs  # noqa: E402
from test_old_mail import ME  # noqa: E402
from test_window import WindowRig  # noqa: E402


class Rig(WindowRig):
    def setUp(self):
        for pid, fn in (("clean", clean.build), ("subs", subs.build)):
            pages.BUILDERS[pid] = fn
            self.addCleanup(pages.BUILDERS.pop, pid, None)
        super().setUp()
        self.w = self.r.app.agent.window
        self.assertIs(self.w.builders, pages.BUILDERS)
        self.addCleanup(self.settle)
        subs._previews.clear()

    def page(self, name: str) -> "a2ui.Surface":
        """Drawn directly (the agent serves only the pages registered when it started)."""
        messages = self.w.draw(name)
        self.assertEqual(a2ui.validate(messages), [], name)
        comps = messages[1]["updateComponents"]["components"]
        self.assertLessEqual(len(comps), kit.MAX_COMPONENTS + 1, name)       # + the root
        return a2ui.build(messages)

    def buttons(self, s) -> list:
        return [s.components[c["child"]].get("text") for c in s.components.values() if c.get("component") == "Button"]

    @staticmethod
    def in_row(s, words: str, about: str) -> str:
        """The button that says `words` beside a text that has `about` in it (a source in 清理)."""
        comps = s.components
        for c in comps.values():
            if c.get("component") != "Row":
                continue
            kids = [comps[k] for k in c["children"] if k in comps]
            if any(about in (k.get("text") or "") for k in kids):
                return next(k["id"] for k in kids if k.get("component") == "Button" and comps[k["child"]].get("text") == words)
        raise AssertionError(f"没有「{about}」那一行")

    def tap(self, name: str, words: str, about: str):
        """Press the button `words` in the row about `about`, on the page as it is drawn now."""
        from test_agent import user_action
        s = self.page(name)
        bid = self.in_row(s, words, about)
        event, ctx = s.find_action(bid)
        ua = user_action(f"luoke-mail/page/{name}", bid) if event == "mail_do" else None
        return self.c.call_tool(event, ctx, user_action=ua)

    def ok(self, r):
        self.assertFalse(r.is_error, r.text)
        return r

    def settle(self):
        """Every job started by a press has finished (its result is drawn on the next page)."""
        for _ in range(500):
            if not any(j["status"] == "running" for j in list(self.r.app.jobs.values())):
                return
            time.sleep(0.02)
        raise AssertionError("a job did not finish")


class CleanTests(Rig):
    def read(self, scope_words: str = ""):
        if scope_words:
            self.ok(self.press("clean", scope_words))
        self.assertIn("还没有读取这些邮件", self.texts(self.page("clean")))
        self.ok(self.press("clean", "读取邮件"))
        self.settle()

    def judge(self):
        rows = self.r.app.store.old_rows(ME, "inbox")
        self.r.app.store.old_judge(ME, {x["message_id"]: ("promotion" if "推送" in x["subject"] else "personal")
                                        for x in rows}, "test")

    def test_reading_lists_the_sources_and_nothing_unjudged_is_ticked(self):
        self.read()
        s = self.page("clean")
        words = self.texts(s)
        self.assertIn("未分析", words)
        self.assertIn("某某商城", words)
        self.assertIn("已选 0 封", words)
        self.assertNotIn("合同扫描件", words)                          # starred: never part of a clean-up
        self.assertNotIn("处理（", " ".join(self.buttons(s)))

    def test_the_suggested_are_ticked_and_carried_out_after_confirming_then_undone(self):
        self.read()
        self.judge()
        s = self.page("clean")
        words = self.texts(s)
        self.assertIn("建议删除（3 封", words)
        self.assertIn("已选 4 封：归档 1，删除 3", words)
        r = self.ok(self.press("clean", "处理（4 封）"))
        self.assertIn("确认", r.text)
        self.assertIn("旧推送一", self.r.inbox())                        # nothing moved before 确认
        words = self.texts(self.page("clean"))
        self.assertIn("删除：广告促销 3 封", words)                         # the browser dialog's words
        self.assertIn("可在处理记录中撤销", words)
        self.ok(self.press("clean", "确认"))
        self.settle()
        self.assertNotIn("旧推送一", self.r.inbox())
        self.assertNotIn("去年的聚餐", self.r.inbox())
        self.assertIn("合同扫描件（留着）", self.r.inbox())
        s = self.page("clean")
        self.assertIn("撤销", self.buttons(s))
        self.ok(self.press("clean", "撤销"))
        self.assertIn("旧推送一", self.r.inbox())

    def test_unticking_holds_across_redraws_and_changes_what_is_carried_out(self):
        self.read()
        self.judge()
        s = self.page("clean")
        self.ok(self.tap("clean", "取消勾选", "某某商城"))
        self.ok(self.press("clean", "刷新"))                              # a redraw keeps it
        s = self.page("clean")
        self.assertIn("已选 1 封：归档 1，删除 0", self.texts(s))
        self.assertIn("勾选", [self.s_text(s, self.in_row(s, "勾选", "某某商城"))])
        self.ok(self.press("clean", "全选"))                               # the section's 全选 ticks it again
        self.assertIn("已选 4 封", self.texts(self.page("clean")))

    @staticmethod
    def s_text(s, bid):
        return s.components[s.components[bid]["child"]]["text"]

    def test_the_trash_needs_processing_mode_and_turning_it_on_needs_confirming(self):
        self.r.app.cfg["mode"]["write"] = False
        self.read()
        self.judge()
        s = self.page("clean")
        self.assertIn("删除前需要先开启处理模式", self.texts(s))
        self.assertNotIn("处理（4 封）", self.buttons(s))
        self.ok(self.press("clean", "开启处理模式"))
        self.assertFalse(self.r.app.cfg["mode"]["write"])
        self.assertIn("螺壳不会发信", self.texts(self.page("clean")))
        self.ok(self.press("clean", "确认"))
        self.assertTrue(self.r.app.cfg["mode"]["write"])
        self.assertIn("处理（4 封）", self.buttons(self.page("clean")))

    def test_archiving_alone_needs_no_mode(self):
        self.r.app.cfg["mode"]["write"] = False
        self.read()
        self.judge()
        s = self.page("clean")
        self.ok(self.tap("clean", "取消勾选", "某某商城"))
        self.assertIn("处理（1 封）", self.buttons(self.page("clean")))

    def test_analysis_pauses_and_resumes(self):
        self.read()
        self.assertIn("已分析 0 / 4", self.texts(self.page("clean")))
        self.ok(self.press("clean", "暂停"))
        self.assertIn("4 封未分析", self.texts(self.page("clean")))
        self.ok(self.press("clean", "开始分析"))
        self.assertTrue(self.r.app.judging(ME, "inbox"))

    def test_newsletters_are_read_first_after_confirming(self):
        self.read()
        rows = self.r.app.store.old_rows(ME, "inbox")
        self.r.app.store.old_judge(ME, {x["message_id"]: "newsletter" for x in rows if "推送" in x["subject"]}, "t")
        self.ok(self.press("clean", "暂停"))                     # the analysis is not what this is about
        r = self.ok(self.press("clean", "先读一遍"))
        self.assertIn("确认", r.text)
        self.assertIn("本机模型会读这 3 封邮件", self.texts(self.page("clean")))
        self.ok(self.press("clean", "确认"))
        self.assertIn("list:promo.shop.example", {x["key"] for x in self.r.app.store.digest_sources(ME)})
        self.settle()
        self.assertRegex(self.texts(self.page("clean")), "正在阅读|已暂停|已阅读")

    def test_archived_mail_is_its_own_tab(self):
        self.read("已归档的邮件")
        s = self.page("clean")
        self.assertIn("已归档的邮件", self.texts(s))
        self.assertIn("收件箱中的旧邮件", self.buttons(s))

    def test_the_demo_and_a_long_archive_stay_within_the_page(self):
        self.w.source = lambda page: "demo"
        self.read("已归档的邮件")
        old = self.r.app.old_view("demo", "archive")
        self.r.app.store.old_judge("demo", {r["message_id"]: "promotion" for r in
                                            self.r.app.store.old_rows("demo", "archive")[::2]}, "t")
        s = self.page("clean")
        self.assertIn("示例数据，不会更改邮箱。", self.texts(s))
        self.assertGreater(len(old["groups"]), 0)
        self.read("收件箱中的旧邮件")
        self.page("clean")

    def test_no_mailbox(self):
        self.r.app.cfg["accounts"] = []
        for pid in ("clean", "subs"):
            self.assertIn("还没有连接邮箱", self.texts(self.page(pid)))


SHOP = {"key": "list:promo.shop.example", "name": "某某商城", "from_addr": "news@shop.example", "list_id": "promo.shop.example",
        "count": 4, "read": 0, "last_date": "2026-11-01 10:00", "bytes": 40000, "subjects": ["双十一预热", "旧推送一"],
        "method": None, "suspicious": False, "verdict": "filter", "why": None}


def sender(key, name, **kw):
    return {**SHOP, "key": f"from:{key}", "name": name, "from_addr": key, "list_id": None, "verdict": "unsubscribe", **kw}


class SubsTests(Rig):
    def checked(self, senders):
        self.r.app.store.cache_set(ME, "subscriptions", {"days": 60, "scanned": 50, "senders": senders,
                                                         "at": "2026-11-09 09:00", "model_note": None})

    def test_a_check_starts_from_the_page_and_lists_the_senders(self):
        self.assertIn("还没体检过", self.texts(self.page("subs")))
        self.ok(self.press("subs", "开始体检", choices={"days": ["365"]}))
        self.settle()
        self.assertEqual(self.r.app.subscriptions_view(ME)["days"], 365)
        s = self.page("subs")
        self.assertIn("个发件人", self.texts(s))
        self.assertIn("重新体检", self.buttons(s))

    def test_a_rule_for_a_sender_moves_it_to_handled_and_is_undone(self):
        self.checked([SHOP])
        r = self.ok(self.press("subs", "以后都删除", on="某某商城"))
        self.assertIn("已创建规则", r.text)
        s = self.page("subs")
        self.assertIn("没有建议处理的发件人", self.texts(s))
        self.ok(self.press("subs", "已处理（1）"))
        self.assertIn("删除规则", self.buttons(self.page("subs")))
        self.ok(self.press("subs", "撤销"))
        self.assertIsNone(self.r.app.subscriptions_view(ME)["senders"][0]["decision"])

    def test_keep_and_digest(self):
        self.checked([SHOP, sender("a@news.example", "早报")])
        self.ok(self.press("subs", "保留", on="某某商城"))
        self.ok(self.press("subs", "加入精选", on="早报"))
        self.assertIn("from:a@news.example", {x["key"] for x in self.r.app.store.digest_sources(ME)})
        self.ok(self.press("subs", "已处理（2）"))
        self.ok(self.press("subs", "移出精选", on="早报"))
        self.assertNotIn("from:a@news.example", {x["key"] for x in self.r.app.store.digest_sources(ME)})

    def test_ways_to_unsubscribe_and_never_for_the_suspicious(self):
        self.checked([sender("one@x.example", "一键", method="one_click", unsub_http="https://x.example/u"),
                      sender("web@x.example", "网页", method="web", unsub_http="https://x.example/web"),
                      sender("plain@x.example", "明文", method="web", unsub_http="http://x.example/web"),
                      sender("body@x.example", "正文", method="body_link", body_link="https://y.example/stop"),
                      sender("mail@x.example", "写信", method="mailto", unsub_mailto="mailto:off@x.example?subject=unsubscribe"),
                      sender("td@x.example", "回复", method="reply", reply_keyword="TD"),
                      sender("bad@x.example", "冒充", method="one_click", unsub_http="https://bad.example/u",
                             verdict="never", suspicious=True)])
        s = self.page("subs")
        words, labels = self.texts(s), self.buttons(s)
        self.assertIn("一键退订", labels)
        self.assertIn("打开退订网页", labels)
        self.assertIn("打开退订链接（y.example）", labels)
        self.assertEqual(labels.count("打开退订网页"), 1)                       # not the http one
        self.assertIn("给 off@x.example 发一封邮件，主题写「unsubscribe」", words)
        self.assertIn("给 td@x.example 回复「TD」", words)
        self.assertIn("以后都移到垃圾邮件", labels)
        bad = [c for c in s.components.values() if c.get("component") == "Button"
               and c["action"]["event"]["name"] == "agent.open_url"]
        self.assertTrue(all("bad.example" not in c["action"]["event"]["context"]["url"] for c in bad))
        self.assertEqual(labels.count("一键退订"), 1)                          # not for 冒充

    def test_clearing_a_senders_history_counts_first_and_needs_confirming(self):
        self.checked([SHOP])
        self.ok(self.press("subs", "更多…", on="某某商城"))
        self.ok(self.press("subs", "清理历史邮件", on="某某商城"))
        s = self.page("subs")
        label = next(x for x in self.buttons(s) if x.startswith("删除（"))
        self.assertIn("含已归档邮件", self.texts(s))
        r = self.ok(self.press("subs", label, on="某某商城"))
        self.assertIn("确认", r.text)
        self.assertIn("旧推送一", self.r.inbox())
        self.ok(self.press("subs", "确认"))
        self.assertNotIn("旧推送一", self.r.inbox())
        self.assertIn("撤销", self.buttons(self.page("subs")))

    def test_clearing_history_needs_processing_mode(self):
        self.r.app.cfg["mode"]["write"] = False
        self.checked([SHOP])
        self.ok(self.press("subs", "更多…", on="某某商城"))
        self.ok(self.press("subs", "清理历史邮件", on="某某商城"))
        s = self.page("subs")
        self.assertIn("开启处理模式", self.buttons(s))
        self.assertFalse(any(x.startswith("删除（") for x in self.buttons(s)))

    def test_block_guide(self):
        self.checked([SHOP])
        self.ok(self.press("subs", "更多…", on="某某商城"))
        self.ok(self.press("subs", "去邮箱里拉黑", on="某某商城"))
        s = self.page("subs")
        self.assertIn("news@shop.example", self.texts(s))
        self.assertIn("打开 Gmail 过滤器设置", self.buttons(s))

    def test_many_senders_page_within_the_limit(self):
        self.checked([sender(f"s{i}@x.example", f"发件人{i}", method="one_click", unsub_http="https://x.example/u")
                      for i in range(40)])
        s = self.page("subs")
        self.assertIn("共 40 个发件人", self.texts(s))
        self.ok(self.press("subs", "下一页"))
        self.assertIn("发件人8", self.texts(self.page("subs")))

    def test_the_demo(self):
        self.w.source = lambda page: "demo"
        self.ok(self.press("subs", "开始体检"))
        self.settle()
        s = self.page("subs")
        self.assertIn("示例数据，不会更改邮箱。", self.texts(s))
        for prefix in ("已处理（", "建议保留（", "建议处理（"):
            label = next(x for x in self.buttons(self.page("subs")) if x.startswith(prefix))
            self.ok(self.press("subs", label))
        if "一键退订" in self.buttons(self.page("subs")):
            self.assertIn("（示例）", self.ok(self.press("subs", "一键退订")).text)

if __name__ == "__main__":
    unittest.main()
