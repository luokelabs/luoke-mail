"""螺壳 (luoke-agent) and luoke-mail: the outbox, plans carried out on the user's tap, undo, and what is never
done from there -- walked through as the agent does (luoke-agent's tests/test_fake_tool.py), cards checked with
the protocol's own validator."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail.agent_mcp import ToolClient  # noqa: E402
import agent_a2ui as a2ui  # noqa: E402
from fakes import FakeChat  # noqa: E402
from test_background import new_job  # noqa: E402
from test_old_mail import ME, Rig, mail  # noqa: E402

SUGGEST = {
    "答辩时间确认": {"action": "reply", "priority": "high", "summary": "王老师问答辩时间，周五前回", "due_date": "2026-10-17"},
    "项目周会": {"action": "schedule", "priority": "medium", "summary": "周四下午三点的周会",
                 "event": {"title": "项目周会", "date": "2026-10-16", "start": "15:00", "end": "16:00"}},
    "合同扫描件": {"action": "follow_up", "priority": "medium", "summary": "律师发来合同扫描件"},
    "本周精选": {"action": "archive", "priority": "low", "summary": "周刊"},
    "双十一预告": {"action": "ignore", "priority": "low", "summary": "促销"},
    "账户已冻结": {"action": "ignore", "priority": "high", "summary": "冒充银行要求登录", "reason": "发件域名仿冒银行",
               "suspicious": True, "category": "phishing"},
    "登录验证码": {"action": "archive", "priority": "low", "summary": "您的验证码是 482913"},
}


def user_action(surface, component="ok"):
    return {"surfaceId": surface, "componentId": component, "at": "2026-10-15T09:00:00+08:00"}


def button(surface, event):
    comps = surface.components
    return next(cid for cid in a2ui._reachable(comps)
                if comps[cid].get("component") == "Button" and comps[cid]["action"]["event"]["name"] == event)


def drafts(body, *_):
    return 200, json.dumps({"body": "王老师好，周三下午可以。"}, ensure_ascii=False)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.r = r = Rig(self)
        for n, (subject, sender) in enumerate((("答辩时间确认", "王老师 <wang@school.example>"),
                                               ("项目周会", "项目组 <team@corp.example>"),
                                               ("合同扫描件", "李律师 <li@law.example>"),
                                               ("本周精选", "周刊 <news@weekly.example>"),
                                               ("双十一预告", "商城 <news@shop2.example>"),
                                               ("账户已冻结", "某银行 <sec@bank-verify.example>"),
                                               ("登录验证码", "某云 <noreply@cloud.example>")), 100):
            r.srv._add("INBOX", mail(n, subject, sender, 1), set())
        r.app.sync_mailbox(ME, new_job(), analyze=False)
        for it in r.app.store.unanalyzed(ME):
            s = SUGGEST.get(it["subject"])
            if s:
                r.app.store.set_suggestion(it["id"], {"category": "work", "reason": "", "reply_points": [], "event": None,
                                                      "suspicious": False, "due_date": None, **s}, "model", "m")
        self.chat = FakeChat(drafts).__enter__()
        self.addCleanup(self.chat.__exit__)
        r.app.cfg["engine"]["base_url"] = self.chat.base_url
        self.dir = tempfile.mkdtemp(prefix="lm", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "run", "agent.sock")
        r.app.agent.start(self.path)
        self.addCleanup(r.app.agent.close)
        self.c = ToolClient(self.path)

    def outbox(self):
        items = {}
        for it in self.c.outbox("luoke-mail"):
            self.assertEqual(a2ui.validate(it["card"]), [], it["id"])
            for k in ("id", "urgency", "why_now", "title", "summary"):
                self.assertTrue(it.get(k), k)
            items.setdefault(it["kind"], []).append(it)
        return items

    def test_the_outbox_says_what_needs_the_user_and_what_is_urgent(self):
        items = self.outbox()
        new, sus = items["new-mail"][0], items["suspicious"]
        self.assertEqual((new["urgency"], new["deadline"]), ("normal", "2026-10-17T23:59:00"))
        self.assertIn("3 封新邮件要处理", new["title"])
        self.assertEqual([s["urgency"] for s in sus], ["urgent"])
        s = a2ui.build(new["card"])
        picked = {k: v[0] for k, v in s.data["choices"].items()}
        self.assertEqual(sorted(picked.values()), ["draft", "flag", "flag", "suggested"])
        text = json.dumps(new["card"], ensure_ascii=False)
        self.assertNotIn("482913", text)                                  # never a code on a card
        self.assertIn("加入日程", text)                                    # the calendar, through the agent's open_url
        self.assertIn("agent.open_url", text)
        sus_card = json.dumps(sus[0]["card"], ensure_ascii=False)
        self.assertIn("移到垃圾邮件", sus_card)
        self.assertNotIn("退订", sus_card)
        self.assertTrue(self.r.app.agent.listening())                      # luoke-mail's own notes wait

    def test_carried_out_on_the_tap_then_undone(self):
        new = self.outbox()["new-mail"][0]
        s = a2ui.build(new["card"])
        follow = next(k for k, v in s.data["choices"].items() if v == ["flag"])
        s.data = a2ui.set_path(s.data, f"/choices/{follow}", ["keep"])      # the user changed one
        name, ctx = s.find_action(button(s, "mail_apply_plan"))
        r = self.c.call_tool(name, ctx, user_action=user_action(s.surface_id))
        self.assertFalse(r.is_error, r.text)
        self.assertTrue(r.done)
        self.assertIn("草稿", r.text)
        inbox = self.r.inbox()
        for gone in ("本周精选", "双十一预告", "登录验证码"):
            self.assertNotIn(gone, inbox)                                   # archived, trashed
        self.assertIn("答辩时间确认", inbox)                                 # a draft was written; the mail stays
        self.assertIn("账户已冻结", inbox)                                   # not part of this plan
        self.assertEqual(len(self.r.srv.folders["[Gmail]/Drafts"]), 1)
        self.assertNotIn("new-mail", self.outbox())       # done; the one left alone is not brought up again by itself
        res = a2ui.update(s, r.structured["card"])                       # the card becomes the result, with 撤销
        name, ctx = res.find_action(button(res, "mail_undo"))
        u = self.c.call_tool(name, ctx, user_action=user_action(s.surface_id, "undo"))
        self.assertFalse(u.is_error, u.text)
        self.assertTrue(u.text.startswith("已撤销：") and "回到收件箱" in u.text and "草稿删掉了" in u.text, u.text)
        self.assertIn("双十一预告", self.r.inbox())
        self.assertEqual(len(self.r.srv.folders["[Gmail]/Drafts"]), 0)
        again = self.c.call_tool(name, ctx, user_action=user_action(s.surface_id, "undo"))
        self.assertEqual(again.structured.get("code"), "stale")

    def test_nothing_changes_without_a_tap_processing_mode_or_the_cards_own_choices(self):
        for it in self.r.app.store.items(ME):                                 # two to the trash: that needs 处理模式
            if it["subject"] in ("本周精选", "双十一预告"):
                self.r.app.store.set_suggestion(it["id"], {"category": "promotion", "reason": "", "reply_points": [], "event": None,
                                                           "suspicious": False, "due_date": None, "action": "ignore",
                                                           "priority": "low", "summary": ""}, "model", "m")
        new = self.outbox()["new-mail"][0]
        s = a2ui.build(new["card"])
        name, ctx = s.find_action(button(s, "mail_apply_plan"))
        self.assertTrue(self.c.call_tool(name, ctx).is_error)               # no tap: refused by the server
        bad = dict(ctx, choices={**ctx["choices"], "rest": ["unsubscribe"]})
        self.assertEqual(self.c.call_tool(name, bad, user_action=user_action(s.surface_id)).structured.get("code"), "invalid")
        self.r.app.cfg["mode"]["write"] = False
        r = self.c.call_tool(name, ctx, user_action=user_action(s.surface_id))
        self.assertEqual(r.structured.get("code"), "needs_setup")          # several to the trash (ADR 0016)
        self.assertIn("处理模式", r.text)
        later = a2ui.update(s, r.structured["card"])                       # the same plan, with a note and 打开设置
        self.assertEqual(later.data["choices"], s.data["choices"])        # the user's choices stay
        button(later, "mail_apply_plan")
        button(later, "mail_open_settings")
        self.assertIn("双十一预告", self.r.inbox())                           # nothing moved
        self.r.app.cfg["mode"]["write"] = True
        self.assertFalse(self.c.call_tool(name, ctx, user_action=user_action(s.surface_id)).is_error)   # the same plan, later

    def test_left_alone_comes_back_only_with_new_mail(self):
        new = self.outbox()["new-mail"][0]
        s = a2ui.build(new["card"])
        for k in s.data["choices"]:
            s.data = a2ui.set_path(s.data, f"/choices/{k}", ["keep"])           # 先不动 for all of them
        name, ctx = s.find_action(button(s, "mail_apply_plan"))
        self.assertFalse(self.c.call_tool(name, ctx, user_action=user_action(s.surface_id)).is_error)
        self.assertNotIn("new-mail", self.outbox())                            # not asked again by itself
        self.r.srv._add("INBOX", mail(201, "新的通知", "某平台 <noreply@app.example>", 0), set())
        self.r.app.sync_mailbox(ME, new_job(), analyze=False)
        n = next(i for i in self.r.app.store.unanalyzed(ME) if i["subject"] == "新的通知")
        self.r.app.store.set_suggestion(n["id"], {"category": "notification", "reason": "", "reply_points": [], "event": None,
                                                  "suspicious": False, "due_date": None, "action": "archive",
                                                  "priority": "low", "summary": "通知"}, "model", "m")
        again = self.outbox()["new-mail"][0]                                   # new mail: said again, the others with it
        self.assertIn("答辩时间确认", json.dumps(again["card"], ensure_ascii=False))

    def test_mail_handled_meanwhile_is_left_alone_and_dismissed_is_not_offered_again(self):
        sus = self.outbox()["suspicious"][0]
        s = a2ui.build(sus["card"])
        name, ctx = s.find_action(button(s, "mail_apply_plan"))
        it = next(i for i in self.r.app.store.items(ME, "pending") if i["subject"] == "账户已冻结")
        self.r.app.actions("mailbox").execute([it["id"]], "spam")             # the user did it on the page
        r = self.c.call_tool(name, ctx, user_action=user_action(s.surface_id))
        self.assertIn("已经处理过", r.text)
        new = self.outbox()["new-mail"][0]
        self.c.call_tool("outbox_ack", {"id": new["id"], "state": "dismissed"})
        self.assertNotIn("new-mail", self.outbox())                            # not again for the same mail
        self.r.srv._add("INBOX", mail(200, "答辩材料", "王老师 <wang@school.example>", 0), set())   # new mail
        self.r.app.sync_mailbox(ME, new_job(), analyze=False)
        n = next(i for i in self.r.app.store.unanalyzed(ME) if i["subject"] == "答辩材料")
        self.r.app.store.set_suggestion(n["id"], {"category": "work", "reason": "", "reply_points": [], "event": None,
                                                  "suspicious": False, "due_date": None, "action": "reply",
                                                  "priority": "high", "summary": "答辩材料"}, "model", "m")
        self.assertIn("new-mail", self.outbox())                               # new mail: a new plan


class FirstReleaseTests(unittest.TestCase):
    """Off in the first release (top-level ADR 0008): no socket unless asked for, no capabilities declared."""

    def test_off_unless_asked_for_and_the_manifest_declares_nothing(self):
        from luoke_mail import agent
        from luoke_mail.agent_mcp import AGENT_PROTOCOL
        saved = os.environ.pop("LUOKE_MAIL_AGENT", None)
        self.addCleanup(lambda: saved is None or os.environ.__setitem__("LUOKE_MAIL_AGENT", saved))
        self.assertFalse(agent.enabled())
        os.environ["LUOKE_MAIL_AGENT"] = "1"
        self.assertTrue(agent.enabled())
        del os.environ["LUOKE_MAIL_AGENT"]
        here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packaging")
        with open(os.path.join(here, "accessory.json"), encoding="utf-8") as f:
            self.assertNotIn("capabilities", json.load(f))
        with open(os.path.join(here, "agent-capabilities.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["protocol"], AGENT_PROTOCOL)       # kept for when the agent ships


if __name__ == "__main__":
    unittest.main()
