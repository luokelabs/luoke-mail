"""精选 and 设置 in 螺壳's window: the same views and routes as the browser page, a change only on the
user's tap, what the browser page asks in a dialog asked with 确认 on the page."""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
import agent_a2ui as a2ui  # noqa: E402
from test_agent import labelled, user_action  # noqa: E402
from test_old_mail import ME  # noqa: E402
from test_window import WindowRig  # noqa: E402
from luoke_mail.window import pages  # noqa: E402
from luoke_mail.window.pages import digest, settings  # noqa: E402


class PageRig(WindowRig):
    """The window's own pages drawn directly (the agent serves only the resources registered at its start)."""

    def setUp(self):
        super().setUp()
        for pid, fn in (("digest", digest.build), ("settings", settings.build)):
            pages.BUILDERS[pid] = fn
            self.addCleanup(pages.BUILDERS.pop, pid, None)
        self.w = self.r.app.agent.window

    def draw(self, name: str) -> "a2ui.Surface":
        messages = self.w.draw(name)
        self.assertEqual(a2ui.validate(messages), [], name)
        return a2ui.build(messages)

    def words(self, name: str) -> str:
        return self.texts(self.draw(name))

    def buttons(self, name: str) -> list:
        s = self.draw(name)
        return [s.components[c["child"]].get("text") for c in s.components.values() if c.get("component") == "Button"]

    def tap(self, name: str, words: str, *, on: str = "", tap: bool = True, **inputs):
        s = self.draw(name)
        bid = self.in_card(s, words, on) if on else labelled(s, words)
        event, ctx = s.find_action(bid)
        for kind, values in inputs.items():
            ctx[kind] = {**ctx.get(kind, {}), **values}
        ua = user_action(f"luoke-mail/page/{name}", bid) if tap and event == "mail_do" else None
        r = self.c.call_tool(event, ctx, user_action=ua)
        self.assertFalse(r.is_error, r.text)
        return r


def _pieces(app, titles, url="https://news.example/a"):
    """Newsletter pieces read this week, from one source the user added to 精选."""
    store = app.store
    store.digest_add_source(ME, "list:weekly.news.example", "周刊", "user")
    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for title in titles:
        mid = f"<{abs(hash(title))}@news.example>"
        store.digest_queue(ME, [{"message_id": mid, "source_key": "list:weekly.news.example", "name": "周刊",
                                 "subject": title, "date": today}], "weekly")
        store.digest_read(ME, mid, [{"title": title, "summary": f"{title}的摘要", "url": url, "host": "news.example",
                                     "topics": ["技术"], "lasting": True, "score": 3, "why": "值得一读"}], "m")


class DigestTests(PageRig):
    def setUp(self):
        super().setUp()
        _pieces(self.r.app, ["本地模型的新进展", "邮件协议小史"])

    def test_the_picks_are_listed_and_saved_or_set_aside(self):
        words = self.words("digest")
        self.assertIn("本地模型的新进展", words)
        self.assertIn("最近 7 天", words)
        self.tap("digest", "收藏", on="本地模型的新进展（推荐）")
        self.tap("digest", "收藏（1）")
        s = self.draw("digest")
        self.assertIn("本地模型的新进展（推荐）", self.texts(s))
        self.assertNotIn("邮件协议小史", self.texts(s))
        self.tap("digest", "取消收藏", on="本地模型的新进展（推荐）")
        self.assertIn("还没有收藏的内容。", self.words("digest"))
        self.tap("digest", "精选")
        self.tap("digest", "归档", on="邮件协议小史（推荐）")
        words = self.words("digest")
        self.assertIn("已归档：邮件协议小史", words)                       # said at the top, with 撤销
        self.assertNotIn("邮件协议小史（推荐）", words)
        self.tap("digest", "已归档（1）")
        self.tap("digest", "移回精选", on="邮件协议小史（推荐）")
        self.tap("digest", "精选")
        self.assertIn("邮件协议小史（推荐）", self.words("digest"))

    def test_not_interested_leaves_the_list_and_can_be_taken_back(self):
        self.tap("digest", "没兴趣", on="本地模型的新进展（推荐）")
        words = self.words("digest")
        self.assertIn("已标记没兴趣：本地模型的新进展", words)
        self.assertNotIn("本地模型的新进展（推荐）", words)
        self.tap("digest", "撤销")
        self.assertIn("本地模型的新进展（推荐）", self.words("digest"))

    def test_opening_a_piece_is_recorded_on_the_tap_and_then_its_link_opens(self):
        mem = self.r.app.memory_for(ME)
        before = len(mem.records(topic="digest-open"))
        s = self.draw("digest")
        bid = self.in_card(s, "打开", "本地模型的新进展（推荐）")
        event, ctx = s.find_action(bid)
        self.assertEqual(self.c.call_tool(event, ctx).structured["code"], "needs_user_action")
        self.tap("digest", "打开", on="本地模型的新进展（推荐）")
        s = self.draw("digest")
        links = [c["action"]["event"]["context"].get("url") for c in s.components.values()
                 if c.get("component") == "Button" and c["action"]["event"]["name"] == "agent.open_url"]
        self.assertIn("https://news.example/a", links)
        self.assertIn("已打开：本地模型的新进展", self.texts(s))
        self.assertEqual(len(mem.records(topic="digest-open")), before + 1)       # the memory is shared by the tests

    def test_only_https_links_go_on_the_page(self):
        _pieces(self.r.app, ["只有明文链接"], url="http://plain.example/x")
        self.tap("digest", "打开", on="只有明文链接（推荐）")                  # still opens in Gmail
        s = self.draw("digest")
        links = [c["action"]["event"]["context"].get("url") or "" for c in s.components.values()
                 if c.get("component") == "Button" and c["action"]["event"]["name"] == "agent.open_url"]
        self.assertTrue(links)
        self.assertTrue(all(u.startswith("https://") for u in links), links)
        self.assertNotIn("http://plain.example/x", links)

    def test_reading_pauses_and_sources_can_be_taken_out(self):
        self.r.app.store.digest_queue(ME, [{"message_id": "<q@news.example>", "source_key": "list:weekly.news.example",
                                            "name": "周刊", "subject": "待读", "date": "2026-11-01 08:00:00"}], "weekly")
        self.assertIn("已读 2 / 3 封", self.words("digest").replace("正在阅读", "已读"))
        self.tap("digest", "暂停")
        self.assertIn("已暂停", self.words("digest"))
        self.tap("digest", "精选来源（1）")
        self.assertIn("周刊", self.words("digest"))
        self.tap("digest", "移出")
        self.assertEqual(self.r.app.store.digest_sources(ME), [])

    def test_a_long_shelf_is_shown_a_screen_at_a_time(self):
        _pieces(self.r.app, [f"第 {n} 条" for n in range(30)])
        s = self.draw("digest")
        self.assertLessEqual(len(s.components), 200)
        self.assertIn("下一页", self.buttons("digest"))
        self.tap("digest", "下一页")
        self.assertIn("上一页", self.buttons("digest"))


class DigestEmptyTests(PageRig):
    def test_nothing_read_yet_no_mailbox_and_the_demo(self):
        self.assertIn("还没有精选来源。", self.words("digest"))
        saved = self.w.source
        self.w.source = lambda page: "demo"
        self.addCleanup(setattr, self.w, "source", saved)
        self.assertIn("示例数据，不会更改邮箱。", self.words("digest"))
        self.w.source = saved
        self.r.app.cfg["accounts"] = []
        self.assertIn("还没有连接邮箱。", self.words("digest"))


class SettingsTests(PageRig):
    def test_the_page_shows_the_mailbox_and_the_model_but_never_a_password_field(self):
        s = self.draw("settings")
        words = self.texts(s)
        self.assertIn(ME, words)
        self.assertIn("test-model", words)
        self.assertNotIn("secret", str(s.components))
        self.assertIn("添加邮箱…", self.buttons("settings"))
        self.assertNotIn("更换模型", self.buttons("settings"))

    def test_processing_mode_needs_the_explanation_confirmed_to_turn_on_not_off(self):
        self.tap_in_row("settings", "关闭", "已开启：批量删除、移到垃圾邮件时，每次都需要你确认。")
        self.assertFalse(self.r.app.cfg["mode"]["write"])
        r = self.tap("settings", "开启处理模式")
        self.assertIn("确认", r.text)
        self.assertFalse(self.r.app.cfg["mode"]["write"])
        self.assertIn("可以一次处理多封邮件", self.words("settings"))
        self.tap("settings", "确认")
        self.assertTrue(self.r.app.cfg["mode"]["write"])

    def tap_in_row(self, name: str, words: str, beside: str):
        """The button that says `words` in the row that says `beside`."""
        s = self.draw(name)
        comps = s.components
        for c in comps.values():
            kids = c.get("children") or []
            if c.get("component") == "Row" and any(comps.get(k, {}).get("text") == beside for k in kids):
                bid = next(k for k in kids if comps[k].get("component") == "Button"
                           and comps[comps[k]["child"]].get("text") == words)
                event, ctx = s.find_action(bid)
                r = self.c.call_tool(event, ctx, user_action=user_action(f"luoke-mail/page/{name}", bid))
                self.assertFalse(r.is_error, r.text)
                return r
        raise AssertionError(f"没有「{beside}」")

    def test_removing_a_mailbox_and_its_own_mailbox_switch_are_confirmed(self):
        self.tap("settings", "设为螺壳自己的邮箱", on=ME)
        self.assertFalse(self.r.app.own_mailbox(ME))
        self.tap("settings", "取消")
        self.assertNotIn("这个邮箱的邮件分析后会直接处理", self.words("settings"))
        self.tap("settings", "设为螺壳自己的邮箱", on=ME)
        self.tap("settings", "确认")
        self.assertTrue(self.r.app.own_mailbox(ME))
        self.tap("settings", "改为我的邮箱", on=ME)
        self.assertFalse(self.r.app.own_mailbox(ME))
        self.tap("settings", "移除", on=ME)
        self.assertEqual(self.r.app.mail_accounts(), [ME])
        self.assertIn("删掉的记录找不回来", self.words("settings"))
        self.tap("settings", "确认")
        self.assertEqual(self.r.app.mail_accounts(), [])
        words = self.words("settings")                       # nothing connected now: still a page
        self.assertIn(f"已移除 {ME}", words)
        self.assertIn("连接邮箱…", self.buttons("settings"))
        self.assertIn("连接邮箱后可开启。", words)

    def test_switches_power_and_what_a_sync_reads(self):
        self.tap_in_row("settings", "关闭", "分析过的邮件标为已读（已开启）")
        self.assertFalse(self.r.app.cfg["flow"]["mark_read"])
        on = self.r.app.cfg["threads"]["on"]
        self.tap_in_row("settings", "关闭" if on else "开启", f"提醒在等对方回复的邮件（{'已开启' if on else '已关闭'}）")
        self.assertEqual(self.r.app.cfg["threads"]["on"], not on)
        self.tap("settings", "安静")
        self.assertEqual(self.r.app.cfg["power"]["level"], "quiet")
        self.assertIn("安静（当前）", self.words("settings"))
        self.tap("settings", "保存范围", fields={"days": "14", "limit": "50"})
        self.assertEqual((self.r.app.cfg["fetch"]["days"], self.r.app.cfg["fetch"]["limit"]), (14, 50))
        self.tap("settings", "保存署名", fields={"signature": "小周"})
        self.assertEqual(self.r.app.cfg["profile"]["signature"], "小周")

    def test_background_sorting_is_turned_on_only_after_the_explanation(self):
        self.r.app.cfg["auto"]["enabled"] = False
        s = self.draw("settings")
        self.assertIn("已关闭：开启后定时整理，有要你处理的邮件时发通知。", self.texts(s))
        every = str(self.r.app.auto_view()["intervals"][-1])
        r = self.tap("settings", "开启", choices={"every": [every]})
        self.assertIn("确认", r.text)
        self.assertFalse(self.r.app.cfg["auto"]["enabled"])
        self.tap("settings", "确认")
        self.assertTrue(self.r.app.cfg["auto"]["enabled"])
        self.assertIn("发送测试通知", self.buttons("settings"))
        self.tap("settings", "发送测试通知")
        self.assertEqual(str(self.r.app.cfg["auto"]["every"]), every)
        self._close_auto()
        self.assertFalse(self.r.app.cfg["auto"]["enabled"])

    def _close_auto(self):
        s = self.draw("settings")
        bid = next(cid for cid, c in s.components.items() if c.get("component") == "Button"
                   and s.components[c["child"]].get("text") == "关闭"
                   and self.w._find("settings", cid).get("op") == "/api/auto")
        event, ctx = s.find_action(bid)
        r = self.c.call_tool(event, ctx, user_action=user_action("luoke-mail/page/settings", bid))
        self.assertIn("确认", r.text)
        self.assertTrue(self.r.app.cfg["auto"]["enabled"])
        return self.tap("settings", "确认")

    def test_learning_about_the_user_and_their_websites(self):
        self.r.app.profiler.fetch = lambda url: {"title": "我的博客", "posts": [{"title": "一篇"}]}
        self.r.app.idle_step = lambda **_: None
        self.tap("settings", "添加", fields={"site": "https://blog.example"})
        words = self.words("settings")
        self.assertIn("已读取「我的博客」，找到 1 篇文章。", words)
        self.assertIn("我的博客", words)
        self.tap("settings", "删除")
        self.assertEqual(len(self.r.app.profiler.sites(ME)), 1)              # not before 确认
        self.tap("settings", "确认")
        self.assertEqual(self.r.app.profiler.sites(ME), [])
        on = self.r.app.learner.view(ME)["on"]
        self.tap("settings", "暂停" if on else "开启")
        self.assertEqual(self.r.app.learner.view(ME)["on"], not on)

    def test_clearing_everything_is_confirmed_and_takes_the_memory_box(self):
        seen = []
        self.r.app.forget = lambda b=None: seen.append(b) or {"ok": True, "exiting": False}
        self.tap("settings", "全部清除", checks={"forget_memory": True})
        self.assertEqual(seen, [])
        self.tap("settings", "确认")
        self.assertEqual(seen, [{"memory": True}])

    def test_a_change_needs_the_tap(self):
        s = self.draw("settings")
        event, ctx = s.find_action(labelled(s, "全部清除"))
        self.assertEqual(self.c.call_tool(event, ctx).structured["code"], "needs_user_action")


if __name__ == "__main__":
    unittest.main()
