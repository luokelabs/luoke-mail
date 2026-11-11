"""The mail assistant's pages in 螺壳's window: drawn from the same views as the browser page, checked
with the protocol's validator, and acted on only through the buttons a page drew -- the agent passes which button
was pressed, never what it does."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
import agent_a2ui as a2ui  # noqa: E402
import test_agent  # noqa: E402 -- its setUp; not its tests, which run there
from test_agent import labelled, user_action  # noqa: E402
from test_old_mail import ME  # noqa: E402


class WindowRig(unittest.TestCase):
    def setUp(self):
        test_agent.AgentTests.setUp(self)
        self.opened = {}                          # page -> the args it was opened with (agent.open_page, §4 pageArgs)

    def page(self, name: str) -> "a2ui.Surface":
        messages = self.c.read_json(a2ui.page_uri(name, self.opened.get(name)))
        self.assertEqual(a2ui.validate(messages, page=True), [], name)
        return a2ui.build(messages, page=True)

    def texts(self, s) -> str:
        return " ".join(c.get("text", "") for c in s.components.values() if c.get("component") == "Text")

    @staticmethod
    def in_card(s, words: str, subject: str) -> str:
        """The button that says `words` in the card that shows `subject`."""
        comps = s.components

        def under(cid):
            c = comps.get(cid) or {}
            kids = c.get("children") or ([c["child"]] if c.get("child") else [])
            return [cid] + [x for k in kids for x in under(k)]
        for cid, c in comps.items():
            if c.get("component") == "Card":
                inside = under(cid)
                if any(comps[x].get("text") == subject for x in inside if x in comps):
                    return next(x for x in inside if comps.get(x, {}).get("component") == "Button"
                                and comps.get(comps[x]["child"], {}).get("text") == words)
        raise AssertionError(f"没有「{subject}」的卡片")

    def press(self, name: str, words: str, *, on: str = "", tap: bool = True, **inputs):
        """Press the button that says `words` on page `name` (in the card showing `on`), as the agent does: what
        the button's context says, the page's inputs as the user left them (or as given), and the tap for a change."""
        s = self.page(name)
        bid = self.in_card(s, words, on) if on else labelled(s, words)
        event, ctx = s.find_action(bid)
        for kind, values in inputs.items():
            ctx[kind] = {**ctx.get(kind, {}), **values}
        if event == "agent.open_page":            # the window opens the page; args as the press resolved them
            self.opened[ctx["page"]] = ctx.get("args") or {}
            return None
        ua = user_action(f"luoke-mail/page/{name}", bid) if tap and event == "mail_do" else None
        return self.c.call_tool(event, ctx, user_action=ua)


class InboxTests(WindowRig):
    def test_the_inbox_lists_what_needs_the_user_and_acts_on_a_press(self):
        s = self.page("inbox")
        words = self.texts(s)
        self.assertIn("答辩时间确认", words)
        self.assertNotIn("本周精选", words)                                  # 可以直接处理 is another list
        self.assertNotIn("482913", json.dumps(s.components, ensure_ascii=False))
        r = self.press("inbox", "可以直接处理")
        self.assertFalse(r.is_error, r.text)
        self.assertIn("本周精选", self.texts(self.page("inbox")))
        r = self.press("inbox", "归档", on="本周精选")
        self.assertFalse(r.is_error, r.text)
        self.assertNotIn("本周精选", self.r.inbox())
        s = self.page("inbox")
        self.assertIn("撤销", [c.get("text") for c in s.components.values()])
        self.assertFalse(self.press("inbox", "撤销").is_error)
        self.assertIn("本周精选", self.r.inbox())

    def test_a_change_needs_the_tap_on_that_very_button(self):
        s = self.page("inbox")
        bid = labelled(s, "写草稿")
        event, ctx = s.find_action(bid)
        self.assertEqual(self.c.call_tool(event, ctx).structured["code"], "needs_user_action")
        other = labelled(s, "跳过")
        r = self.c.call_tool(event, ctx, user_action=user_action("luoke-mail/page/inbox", other))
        self.assertEqual(r.structured["code"], "invalid")                    # the press was on another button
        r = self.c.call_tool(event, {**ctx, "button": "g999-b1"}, user_action=user_action("luoke-mail/page/inbox", "g999-b1"))
        self.assertEqual(r.structured["code"], "stale")                      # never drawn
        self.assertEqual(self.c.call_tool("mail_do", {"page": "inbox", "button": bid, "op": "/api/forget"},
                                          user_action=user_action("luoke-mail/page/inbox", bid)).text,
                         self.c.call_tool(event, ctx, user_action=user_action("luoke-mail/page/inbox", bid)).text)

    def test_a_mail_opens_and_its_suggestion_changes_in_place(self):
        self.press("inbox", "查看原文")
        s = self.page("inbox")
        self.assertIn("返回列表", [c.get("text") for c in s.components.values()])
        self.press("inbox", "返回列表")
        self.press("inbox", "改为…")
        r = self.press("inbox", "改好了", choices={})
        self.assertFalse(r.is_error, r.text)
        self.assertIn("已改为", r.text)

    def test_every_page_is_valid_with_no_mailbox_and_with_several(self):
        from luoke_mail.window.pages import PAGES
        for pid, _ in PAGES:
            self.page(pid)
