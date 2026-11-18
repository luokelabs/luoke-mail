"""精选 and 设置 in 螺壳's window: the same views and routes as the browser page, a change only on the
user's tap, what the browser page asks in a dialog asked with 确认 on the page."""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
import agent_a2ui as a2ui  # noqa: E402
from luoke_mail import agent_mcp  # noqa: E402
from test_agent import labelled, said, user_action  # noqa: E402
from test_old_mail import ME  # noqa: E402
from test_window import WindowRig  # noqa: E402
from luoke_mail.window import pages  # noqa: E402
from luoke_mail.window.pages import digest, settings  # noqa: E402


class PageRig(WindowRig):
    """The window's own pages drawn directly (the agent serves only the resources registered at its start)."""

    def setUp(self):
        super().setUp()
        self.w = self.r.app.agent.window

    def draw(self, name: str) -> "a2ui.Surface":
        messages = self.w.draw(name)
        self.assertEqual(a2ui.validate(messages), [], name)
        return a2ui.build(messages)

    def words(self, name: str) -> str:
        return self.texts(self.draw(name))

    def buttons(self, name: str) -> list:
        s = self.draw(name)
        return [said(s.components, cid) for cid, c in s.components.items() if c.get("component") == "Button"]

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
    def buttons_of(self, s) -> list:
        return [said(s.components, cid) for cid, c in s.components.items() if c.get("component") == "Button"]

    def setUp(self):
        super().setUp()
        _pieces(self.r.app, ["本地模型的新进展", "邮件协议小史"])

    def test_the_picks_are_listed_and_more_like_this_keeps_one(self):
        """设计规范 §18.2: no topics, a piece is its title and a sentence; 多来点这类 keeps it under 收藏 too."""
        words = self.words("digest")
        self.assertIn("本地模型的新进展", words)
        self.assertIn("本地模型的新进展的摘要", words)
        self.assertIn("最近 7 天", words)
        self.assertNotIn("关注的话题", words)
        self.assertNotIn("收藏", self.buttons("digest"))                      # nothing saved yet: not offered
        self.tap("digest", "多来点这类", on="本地模型的新进展")
        self.assertNotIn("收藏（1）", self.buttons("digest"))                 # no counts (§18.1)
        self.tap("digest", "收藏")
        s = self.draw("digest")
        self.assertIn("本地模型的新进展（推荐）", self.texts(s))
        self.assertNotIn("邮件协议小史", self.texts(s))
        self.tap("digest", "取消收藏", on="本地模型的新进展（推荐）")
        self.assertIn("还没有收藏的内容。", self.words("digest"))
        self.tap("digest", "精选")
        self.assertIn("邮件协议小史", self.words("digest"))

    def test_less_like_this_leaves_the_list_and_can_be_taken_back(self):
        self.tap("digest", "少来点这类", on="本地模型的新进展")
        words = self.words("digest")
        self.assertIn("已记下，少来点这类：本地模型的新进展", words)
        self.assertRaises(AssertionError, self.in_card, self.draw("digest"), "少来点这类", "本地模型的新进展")
        self.tap("digest", "撤销")
        self.assertIn("本地模型的新进展的摘要", self.words("digest"))

    def test_a_tap_on_the_line_opens_the_piece_and_its_site_is_a_button_there(self):
        """The line opens the piece's own page (§4 rowOpen to agent.open_page, never straight to a website): the
        site is on its button, seen before going (设计规范 §18.1). Opening it is recorded once."""
        s = WindowRig.page(self, "digest")
        bid = self.line_opens(s, "本地模型的新进展")
        event, ctx = s.find_action(bid)
        self.assertEqual(event, "agent.open_page")
        self.assertEqual(ctx["page"], "piece")
        mem = self.r.app.memory_for(ME)
        before = len(mem.records(topic="digest-open"))
        self.opened["piece"] = ctx["args"]
        for _ in range(2):                                                    # read again: still once
            s = WindowRig.page(self, "piece")
        self.assertEqual(len(mem.records(topic="digest-open")), before + 1)       # the memory is shared by the tests
        self.assertIn("本地模型的新进展的摘要", self.texts(s))
        site = s.components[labelled(s, "在浏览器打开（news.example）")]
        self.assertEqual(site["action"]["event"]["context"]["url"], "https://news.example/a")
        self.assertFalse(self.press("piece", "多来点这类").is_error)
        self.assertIn("不用多来了", self.buttons_of(WindowRig.page(self, "piece")))
        self.assertIsNone(self.press("piece", "返回精选"))

    def test_the_piece_page_without_one(self):
        self.assertIn("从「精选」里点一条进来。", self.texts(WindowRig.page(self, "piece")))
        self.opened["piece"] = {"id": "999999"}
        self.assertIn("这条内容不在了。", self.texts(WindowRig.page(self, "piece")))

    def test_an_older_core_records_the_opening_on_the_line(self):
        mem = self.r.app.memory_for(ME)
        before = len(mem.records(topic="digest-open"))
        s = self.draw("digest")
        bid = self.in_card(s, "打开", "本地模型的新进展")
        event, ctx = s.find_action(bid)
        self.assertEqual(self.c.call_tool(event, ctx).structured["code"], "needs_user_action")
        self.tap("digest", "打开", on="本地模型的新进展")
        s = self.draw("digest")
        links = [c["action"]["event"]["context"].get("url") for c in s.components.values()
                 if c.get("component") == "Button" and c["action"]["event"]["name"] == "agent.open_url"]
        self.assertIn("https://news.example/a", links)
        self.assertIn("已打开：本地模型的新进展", self.texts(s))
        self.assertEqual(len(mem.records(topic="digest-open")), before + 1)

    def test_only_https_links_go_on_the_page(self):
        _pieces(self.r.app, ["只有明文链接"], url="http://plain.example/x")
        self.tap("digest", "打开", on="只有明文链接")                          # still opens in Gmail
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
        self.tap("digest", "精选来源")
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
        s = self.draw("legacy-settings")
        words = self.texts(s)
        self.assertIn(ME, words)
        self.assertIn("test-model", words)
        self.assertNotIn("secret", str(s.components))
        self.assertIn("添加邮箱…", self.buttons("legacy-settings"))
        self.assertNotIn("更换模型", self.buttons("legacy-settings"))

    def test_processing_mode_needs_the_explanation_confirmed_to_turn_on_not_off(self):
        self.tap_in_row("legacy-settings", "关闭", "已开启：批量删除、移到垃圾邮件时，每次都需要你确认。")
        self.assertFalse(self.r.app.cfg["mode"]["write"])
        r = self.tap("legacy-settings", "开启处理模式")
        self.assertIn("确认", r.text)
        self.assertFalse(self.r.app.cfg["mode"]["write"])
        self.assertIn("可以一次处理多封邮件", self.words("legacy-settings"))
        self.tap("legacy-settings", "确认")
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
        self.tap("legacy-settings", "设为螺壳自己的邮箱", on=ME)
        self.assertFalse(self.r.app.own_mailbox(ME))
        self.tap("legacy-settings", "取消")
        self.assertNotIn("这个邮箱的邮件分析后会直接处理", self.words("legacy-settings"))
        self.tap("legacy-settings", "设为螺壳自己的邮箱", on=ME)
        self.tap("legacy-settings", "确认")
        self.assertTrue(self.r.app.own_mailbox(ME))
        self.tap("legacy-settings", "改为我的邮箱", on=ME)
        self.assertFalse(self.r.app.own_mailbox(ME))
        self.tap("legacy-settings", "移除", on=ME)
        self.assertEqual(self.r.app.mail_accounts(), [ME])
        self.assertIn("删掉的记录找不回来", self.words("legacy-settings"))
        self.tap("legacy-settings", "确认")
        self.assertEqual(self.r.app.mail_accounts(), [])
        words = self.words("legacy-settings")                       # nothing connected now: still a page
        self.assertIn(f"已移除 {ME}", words)
        self.assertIn("连接邮箱…", self.buttons("legacy-settings"))
        self.assertIn("连接邮箱后可开启。", words)

    def test_switches_power_and_what_a_sync_reads(self):
        self.tap_in_row("legacy-settings", "关闭", "分析过的邮件标为已读（已开启）")
        self.assertFalse(self.r.app.cfg["flow"]["mark_read"])
        on = self.r.app.cfg["threads"]["on"]
        self.tap_in_row("legacy-settings", "关闭" if on else "开启", f"提醒在等对方回复的邮件（{'已开启' if on else '已关闭'}）")
        self.assertEqual(self.r.app.cfg["threads"]["on"], not on)
        self.tap("legacy-settings", "安静")
        self.assertEqual(self.r.app.cfg["power"]["level"], "quiet")
        self.assertIn("安静（当前）", self.words("legacy-settings"))
        self.tap("legacy-settings", "保存范围", fields={"days": "14", "limit": "50"})
        self.assertEqual((self.r.app.cfg["fetch"]["days"], self.r.app.cfg["fetch"]["limit"]), (14, 50))
        self.tap("legacy-settings", "保存署名", fields={"signature": "小周"})
        self.assertEqual(self.r.app.cfg["profile"]["signature"], "小周")

    def test_background_sorting_is_turned_on_only_after_the_explanation(self):
        self.r.app.cfg["auto"]["enabled"] = False
        s = self.draw("legacy-settings")
        self.assertIn("已关闭：开启后定时整理，有要你处理的邮件时发通知。", self.texts(s))
        every = str(self.r.app.auto_view()["intervals"][-1])
        r = self.tap("legacy-settings", "开启", choices={"every": [every]})
        self.assertIn("确认", r.text)
        self.assertFalse(self.r.app.cfg["auto"]["enabled"])
        self.tap("legacy-settings", "确认")
        self.assertTrue(self.r.app.cfg["auto"]["enabled"])
        self.assertIn("发送测试通知", self.buttons("legacy-settings"))
        self.tap("legacy-settings", "发送测试通知")
        self.assertEqual(str(self.r.app.cfg["auto"]["every"]), every)
        self._close_auto()
        self.assertFalse(self.r.app.cfg["auto"]["enabled"])

    def _close_auto(self):
        s = self.draw("legacy-settings")
        bid = next(cid for cid, c in s.components.items() if c.get("component") == "Button"
                   and s.components[c["child"]].get("text") == "关闭"
                   and self.w._find("legacy-settings", cid).get("op") == "/api/auto")
        event, ctx = s.find_action(bid)
        r = self.c.call_tool(event, ctx, user_action=user_action("luoke-mail/page/legacy-settings", bid))
        self.assertIn("确认", r.text)
        self.assertTrue(self.r.app.cfg["auto"]["enabled"])
        return self.tap("legacy-settings", "确认")

    def test_learning_about_the_user_and_their_websites(self):
        self.r.app.profiler.fetch = lambda url: {"title": "我的博客", "posts": [{"title": "一篇"}]}
        self.r.app.idle_step = lambda **_: None
        self.tap("legacy-settings", "添加", fields={"site": "https://blog.example"})
        words = self.words("legacy-settings")
        self.assertIn("已读取「我的博客」，找到 1 篇文章。", words)
        self.assertIn("我的博客", words)
        self.tap("legacy-settings", "删除")
        self.assertEqual(len(self.r.app.profiler.sites(ME)), 1)              # not before 确认
        self.tap("legacy-settings", "确认")
        self.assertEqual(self.r.app.profiler.sites(ME), [])
        on = self.r.app.learner.view(ME)["on"]
        self.tap("legacy-settings", "暂停" if on else "开启")
        self.assertEqual(self.r.app.learner.view(ME)["on"], not on)

    def test_clearing_everything_is_confirmed_and_takes_the_memory_box(self):
        seen = []
        self.r.app.forget = lambda b=None: seen.append(b) or {"ok": True, "exiting": False}
        self.tap("legacy-settings", "全部清除", checks={"forget_memory": True})
        self.assertEqual(seen, [])
        self.tap("legacy-settings", "确认")
        self.assertEqual(seen, [{"memory": True}])

    def test_a_change_needs_the_tap(self):
        s = self.draw("legacy-settings")
        event, ctx = s.find_action(labelled(s, "全部清除"))
        self.assertEqual(self.c.call_tool(event, ctx).structured["code"], "needs_user_action")



class ReworkTests(PageRig):
    """设计规范 §18.2 on the pages: 设置 is 账号, 规则, 提醒 with the rest folded; a mailbox goes by its name; the
    icons added 11-13 only on a core that says it draws them (§4 lucide2)."""

    def test_settings_are_account_rules_and_reminders_and_the_rest_folded(self):
        s = WindowRig.page(self, "legacy-settings")
        words = self.texts(s)
        for w in ("账号", "规则", "提醒"):
            self.assertIn(w, words)
        self.assertLess(words.index("账号"), words.index("规则"))
        self.assertLess(words.index("规则"), words.index("提醒"))
        folded = [c for c in s.components.values() if c.get("collapsed")]
        self.assertEqual(len(folded), 1)
        self.assertTrue(folded[0]["collapsed"].startswith("更多设置"))
        self.assertIsNone(self.press("legacy-settings", "管理规则"))
        self.assertIn("rules", self.opened)

    def test_a_mailbox_is_named_and_shown_by_its_name(self):
        s = WindowRig.page(self, "legacy-settings")
        field = next(c for c in s.components.values() if c.get("component") == "TextField" and c.get("label") == "显示名")
        self.assertEqual(said(s.components, field["submit"]), "保存名字")       # Return saves it (§4 fieldSubmit)
        self.assertEqual(s.data["fields"]["name0"], ME.split("@")[0])         # unnamed: before the @
        r = self.press("legacy-settings", "保存名字", fields={"name0": " 工作 "})
        self.assertFalse(r.is_error, r.text)
        self.assertEqual(self.r.app.mailbox_name(ME), "工作")
        self.assertEqual(self.r.app.cfg["accounts"][0]["name"], "工作")
        self.press("legacy-settings", "保存名字", fields={"name0": ""})
        self.assertNotIn("name", self.r.app.cfg["accounts"][0])

    def test_with_two_mailboxes_each_goes_by_its_name_its_address_on_hover(self):
        self.r.app.cfg["accounts"][0]["name"] = "工作"
        with mock.patch.object(self.r.app, "mail_accounts", lambda: [ME, "other@example.com"]):
            s = WindowRig.page(self, "activity")
        names = {said(s.components, cid): c.get("tooltip") for cid, c in s.components.items()
                 if c.get("component") == "Button" and c["action"]["event"]["name"] == "mail_view"}
        self.assertEqual(names.get("工作"), ME)
        self.assertEqual(names.get("other"), "other@example.com")

    def test_the_icons_added_later_only_on_a_core_that_has_them(self):
        it = next(x for x in self.r.app.store.items(ME, None, None) if x["subject"] == "本周精选")
        self.r.app.items_act(ME, [it["id"]], "archive")
        full = frozenset(agent_mcp.CLIENT_CAPABILITIES["experimental"]["ai.luoke/a2ui"]["features"])

        def icons(features):
            messages = self.w.draw("activity", features=features)
            self.assertEqual(a2ui.validate(messages, page=True), [])
            return {c.get("name") for c in a2ui.build(messages, page=True).components.values()
                    if c.get("component") == "Icon"}
        self.assertIn("undo-2", icons(full))
        self.assertNotIn("undo-2", icons(full - {"lucide2"}))                 # its words only, not a page undrawn

if __name__ == "__main__":
    unittest.main()
