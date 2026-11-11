"""螺壳 (luoke-agent) and luoke-mail: the outbox, plans carried out on the user's tap, undo, and what is never
done from there -- walked through as the agent does (luoke-agent's tests/test_fake_tool.py), cards checked with
the protocol's own validator."""
from __future__ import annotations

import contextlib
import email
import email.policy
import io
import json
import os
import plistlib
from unittest import mock
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import agent  # noqa: E402
from luoke_mail import agent_mcp  # noqa: E402
from luoke_mail.agent_mcp import ToolClient  # noqa: E402
from luoke_mail.window.pages import PAGES as WINDOW_PAGES  # noqa: E402
import agent_a2ui as a2ui  # noqa: E402
from fakes import FakeChat  # noqa: E402
from test_background import new_job  # noqa: E402
from test_old_mail import ME, Rig, mail  # noqa: E402

DUE = time.strftime("%Y-%m-%d", time.localtime(time.time() + 5 * 86400))   # high, and not past it: 「要你决定」
SUGGEST = {
    "答辩时间确认": {"action": "reply", "priority": "high", "summary": "王老师问答辩时间，周五前回", "due_date": DUE},
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


def said(comps, cid):
    """A button's words: its text, or the text beside its icon (protocol §4 图标按钮)."""
    child = comps.get(comps[cid].get("child"), {})
    if child.get("component") == "Row":
        return next((comps[k].get("text") for k in child.get("children", []) if comps.get(k, {}).get("component") == "Text"), None)
    return child.get("text")


def labelled(surface, words):
    """The button that says `words`."""
    comps = surface.components
    return next(cid for cid in a2ui._reachable(comps) if comps[cid].get("component") == "Button" and said(comps, cid) == words)


def drafts(body, *_):
    return 200, json.dumps({"body": "王老师好，周三下午可以。"}, ensure_ascii=False)


class TakenBackOnThePageTests(unittest.TestCase):
    def test_a_line_of_the_record_comes_back_on_each_kind_of_mailbox(self):
        for flavour, provider in (("gmail", "gmail"), ("qq", "qq"), ("netease", "163")):
            with self.subTest(flavour):
                r = Rig(self, flavour=flavour, provider=provider)
                r.srv._add("INBOX", mail(100, "答辩时间确认", "王老师 <wang@school.example>", 1), set())
                r.app.sync_mailbox(ME, new_job(), analyze=False)
                it = next(i for i in r.app.store.items(ME) if i["subject"] == "答辩时间确认")
                live = lambda: [str(email.message_from_bytes(m["raw"], policy=email.policy.default).get("Subject", ""))  # noqa: E731
                                for m in r.srv.folders["INBOX"] if "\\Deleted" not in m["flags"]]    # QQ keeps the moved original flagged
                log = r.app.actions(ME).execute([it["id"]], "trash")["log_id"]
                self.assertNotIn("答辩时间确认", live())
                said = r.app.agent.undo({"execution_id": f"log:{log}"}, {})
                self.assertTrue(said["done"])
                self.assertIn("答辩时间确认", live())
                self.assertEqual(r.app.store.get(it["id"])["state"], "pending")


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
            self.assertRegex(it["created"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", it["id"])    # protocol §3
            items.setdefault(it["kind"], []).append(it)
        return items

    def box(self):
        """The mailbox's card (「顺便看看」): 待处理, 可以整理, 判为可疑."""
        return self.outbox()["mailbox"][0]

    def press(self, card, words, **choices):
        """Tap the button that says `words` on this card, with these picks changed first; the tool's answer."""
        s = a2ui.build(card)
        for k, v in choices.items():
            s.data = a2ui.set_path(s.data, f"/choices/{k}", [v])
        b = labelled(s, words)
        name, ctx = s.find_action(b)
        return self.c.call_tool(name, ctx, user_action=user_action(s.surface_id, b))

    def test_the_outbox_says_what_needs_the_user_and_what_is_urgent(self):
        items = self.outbox()
        new, sus, box = items["new-mail"][0], items["suspicious"], items["mailbox"][0]
        self.assertEqual((new["urgency"], new["deadline"]), ("normal", f"{DUE}T23:59:00"))
        self.assertIn("1 封邮件要你处理", new["title"])                       # 「要你决定」: only what is high today
        self.assertEqual([s["urgency"] for s in sus], ["urgent"])
        s = a2ui.build(new["card"])
        self.assertEqual([v[0] for v in s.data["choices"].values()], ["draft"])
        labelled(s, "写草稿 1")                                              # the button says what it does
        self.assertIn(ME, [c.get("text") for c in s.components.values()])   # the mailbox's label after the heading
        self.assertEqual((box["urgency"], box["title"]), ("low", "2 封待处理，3 封可以整理"))
        b = a2ui.build(box["card"])
        self.assertEqual(sorted(v[0] for v in b.data["choices"].values()), ["flag", "flag"])   # what to tidy: no picker
        labelled(b, "加星标 2")
        labelled(b, "归档 2 · 删除 1")                                         # one decision, the button says what it does
        self.assertIn(ME, [c.get("text") for c in b.components.values()])
        text = json.dumps(new["card"], ensure_ascii=False) + json.dumps(box["card"], ensure_ascii=False)
        self.assertNotIn("482913", text)                                  # never a code on a card
        self.assertIn("加入日程", text)                                    # on the event's row, through the agent's open_url
        self.assertIn("agent.open_url", text)
        self.assertIn('"icon": "star"', text)                             # each choice with its icon
        sus_card = json.dumps(sus[0]["card"], ensure_ascii=False)
        self.assertIn("移到垃圾邮件", sus_card)
        self.assertIn('"destructive"', sus_card)
        self.assertNotIn("退订", sus_card)
        self.assertTrue(self.r.app.agent.listening())                      # luoke-mail's own notes wait

    def test_an_older_core_gets_the_same_cards_without_the_new_parts(self):
        with mock.patch.object(agent_mcp, "CLIENT_CAPABILITIES", {}):
            items = self.outbox()
        text = json.dumps([i["card"] for v in items.values() for i in v], ensure_ascii=False)
        for newer in ('"badge"', '"strong"', '"maxLines"', '"collapsed"', '"tooltip"', '"subtitle"', "agent.open_page"):
            self.assertNotIn(newer, text)
        self.assertIn(f"1 封要处理（{ME}）", text)
        self.assertIn("加入日程", text)                                    # under the card, as before
        s = a2ui.build(items["suspicious"][0]["card"])
        self.assertIn("这封邮件判为可疑", [c.get("text") for c in s.components.values()])

    def flag(self, n, subject, days_ago):
        self.r.srv._add("INBOX", mail(n, subject, f"某平台 <noreply{n}@site.example>", days_ago), set())
        self.r.app.sync_mailbox(ME, new_job(), analyze=False)
        it = next(i for i in self.r.app.store.unanalyzed(ME) if i["subject"] == subject)
        self.r.app.store.set_suggestion(it["id"], {"category": "phishing", "reason": "像是仿冒", "reply_points": [], "event": None,
                                                   "suspicious": True, "due_date": None, "action": "ignore",
                                                   "priority": "high", "summary": "可疑"}, "model", "m")

    def test_a_scam_just_received_is_one_tap_with_its_sender_and_time(self):
        sus = self.outbox()["suspicious"]
        self.assertEqual([s["urgency"] for s in sus], ["urgent"])
        self.assertIn("某银行", sus[0]["title"])                                   # told apart in a menu or a notice
        self.assertIn("账户已冻结", sus[0]["title"])
        it = next(i for i in self.r.app.store.items(ME, "pending") if i["subject"] == "账户已冻结")
        self.assertEqual(sus[0]["created"], it["date"].replace(" ", "T") + ":00")  # when it came, not when the card was made
        s = a2ui.build(sus[0]["card"])
        self.assertNotIn("choices", s.data)                                       # one decision: no picker to set first
        keep = labelled(s, "先不动")
        name, ctx = s.find_action(keep)
        r = self.c.call_tool(name, ctx, user_action=user_action(s.surface_id, keep))
        self.assertIn("都先不动", r.text)
        self.assertIn("账户已冻结", self.r.inbox())

    def test_the_other_button_moves_it_to_junk(self):
        s = a2ui.build(self.outbox()["suspicious"][0]["card"])
        spam = labelled(s, "移到垃圾邮件")
        name, ctx = s.find_action(spam)
        self.assertEqual(self.c.call_tool(name, ctx).code, "needs_user_action")   # the tap is the user's
        r = self.c.call_tool(name, ctx, user_action=user_action(s.surface_id, spam))
        self.assertIn("移到垃圾邮件", r.text)
        self.assertNotIn("账户已冻结", self.r.inbox())

    def test_a_past_deadline_says_so_and_a_notice_wanting_an_answer_is_not_drafted(self):
        store = self.r.app.store
        it = next(i for i in store.items(ME, "pending") if i["subject"] == "答辩时间确认")
        store.set_suggestion(it["id"], {**SUGGEST["答辩时间确认"], "category": "work", "reason": "", "reply_points": [],
                                        "event": None, "suspicious": False, "due_date": "2020-09-20"}, "model", "m")
        n = next(i for i in store.items(ME, "pending") if i["subject"] == "登录验证码")
        store.set_suggestion(n["id"], {"category": "notification", "reason": "", "reply_points": [], "event": None,
                                       "suspicious": False, "due_date": None, "action": "reply", "priority": "medium",
                                       "summary": "新设备登录提醒"}, "model", "m")
        items = self.outbox()
        self.assertNotIn("new-mail", items)                               # high, but past its date: not 「要你决定」
        card = items["mailbox"][0]["card"]
        s = a2ui.build(card)
        due = next(c for c in s.components.values() if c.get("text") == "09-20 已过")
        self.assertEqual((due["variant"], due["tone"]), ("badge", "danger"))
        picked = s.data["choices"]
        self.assertEqual(picked[f"m{it['id']}"], ["draft"])                       # a person asking: a draft
        self.assertEqual(picked[f"m{n['id']}"], ["flag"])                         # a notice: follow up, no draft

    def test_older_and_many_flagged_share_one_card_that_waits(self):
        for n in range(6):                                   # flagged anew, received days ago (a newer prompt, a re-sort)
            self.flag(300 + n, f"旧通知 {n}", 3 + n)
        for n in range(3):
            self.flag(400 + n, f"新通知 {n}", 0)
        sus = self.outbox()["suspicious"]
        self.assertEqual([s["urgency"] for s in sus], ["urgent"] * 3)          # the newest few, each its own card
        self.assertTrue(all("新通知" in s["summary"] for s in sus))
        box = self.box()                                     # the bank one (a day old) is pushed out too, with the old
        self.assertIn("7 封判为可疑", box["title"])
        s = a2ui.build(box["card"])
        flagged = [k for k in s.data["choices"] if k.startswith("s")]
        self.assertEqual(len(flagged), 7)
        self.assertEqual({s.data["choices"][k][0] for k in flagged}, {"keep"})
        self.assertIn("7 封，都先不动", json.dumps(box["card"], ensure_ascii=False))      # folded
        r = self.press(box["card"], "按选的处理")
        self.assertFalse(r.is_error, r.text)
        self.assertEqual(sum("旧通知" in x for x in self.r.inbox()), 6)     # nothing moved
        self.assertEqual(sum(1 for i in self.r.app.store.items(ME) if i["flagged"]), 0)   # nor the other parts
        self.assertEqual([s["urgency"] for s in self.outbox()["suspicious"]], ["urgent"] * 3)
        self.assertNotIn("判为可疑", self.box()["title"])                    # and not asked again today

    def test_many_old_ones_beyond_a_card_are_left_to_the_page(self):
        for n in range(12):
            self.flag(500 + n, f"旧通知 {n}", 3 + n)
        box = self.box()
        s = a2ui.build(box["card"])
        self.assertEqual(len([k for k in s.data["choices"] if k.startswith("s")]), 8)
        self.assertIn("12 封判为可疑", box["title"])                         # the bank one still has its own
        rest = labelled(s, "其余 4 封去邮件助手看")
        self.assertEqual(s.find_action(rest), ("agent.open_page", {"page": "inbox", "args": {"box": ME, "list": "suspicious"}}))

    def sorted_as(self, n, subject, sender, action, days_ago=0, **more):
        self.r.srv._add("INBOX", mail(n, subject, sender, days_ago), set())
        self.r.app.sync_mailbox(ME, new_job(), analyze=False)
        it = next(i for i in self.r.app.store.unanalyzed(ME) if i["subject"] == subject)
        self.r.app.store.set_suggestion(it["id"], {"category": "notification", "reason": "", "reply_points": [], "event": None,
                                                   "suspicious": False, "due_date": None, "action": action,
                                                   "priority": "low", "summary": subject, **more}, "model", "m")
        return it

    def test_the_tidy_card_keeps_its_mail_while_more_is_sorted(self):
        """More mail sorted to tidy every minute doesn't make a new card: the picks would be lost, and an old card
        tapped would be refused. The card does what it says; the newer ones are on the next card."""
        first = self.box()
        for n in range(3):
            self.sorted_as(600 + n, f"推送 {n}", f"推送{n} <p{n}@push.example>", "archive")
        again = self.box()
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["card"], first["card"])
        self.tidy()
        self.assertTrue(all(f"推送 {n}" in self.r.inbox() for n in range(3)))     # not on the card: not moved
        nxt = self.box()
        self.assertNotEqual(nxt["id"], first["id"])
        self.assertEqual(nxt["title"], "2 封待处理，3 封可以整理")

    def test_the_tidy_card_is_made_again_once_its_mail_is_handled_elsewhere(self):
        first = self.box()
        store = self.r.app.store
        ids = [i["id"] for i in store.items(ME, "pending") if i["subject"] in ("本周精选", "双十一预告")]
        self.r.app.actions(ME).execute(ids, "archive")                      # on the page
        self.sorted_as(610, "推送", "推送 <p@push.example>", "archive")
        nxt = self.box()
        self.assertNotEqual(nxt["id"], first["id"])
        self.assertIn("2 封可以整理", nxt["title"])
        self.assertEqual(store.plan_get(first["id"].split("/", 1)[1])["state"], "withdrawn")

    def test_the_card_says_how_many_more_wait_and_puts_the_same_notice_in_one_row(self):
        for n in range(4):                                                   # the same CI failure, four times
            self.sorted_as(700 + n, f"构建失败 #{n + 1}", "CI <noreply@build.example>", "reply", priority="high")
        for n in range(5):
            self.sorted_as(720 + n, f"问题 {n}", f"同事{n} <c{n}@corp.example>", "follow_up", priority="high")
        new = self.outbox()["new-mail"][0]
        self.assertEqual(new["title"], "10 封邮件要你处理")
        text = json.dumps(new["card"], ensure_ascii=False)
        self.assertIn("（共 4 封）", text)
        s = a2ui.build(new["card"])
        ci = next(k for k, e in self.r.app.store.plan_get(s.surface_id)["entries"].items() if len(e["ids"]) == 4)
        self.assertEqual(s.data["choices"][ci], ["flag"])                    # a notice: no draft, four times over
        self.assertNotIn("draft", self.r.app.store.plan_get(s.surface_id)["entries"][ci]["allowed"])
        self.assertEqual(len(s.data["choices"]), 7)
        self.assertIn("10 封要处理", text)
        ok = labelled(s, "加星标 9 · 写草稿 1")                               # what a tap does, counted once
        see = labelled(s, "查看全部 12 封")                                    # with the two not high on the mailbox's card
        self.assertEqual(s.find_action(see), ("agent.open_page", {"page": "inbox", "args": {"box": ME, "list": "needs"}}))
        self.assertNotIn("还有", text)
        self.assertNotIn("新邮件", new["title"] + new["why_now"])            # mostly a backlog
        name, ctx = s.find_action(ok)
        self.c.call_tool(name, ctx, user_action=user_action(s.surface_id, ok))
        self.assertEqual(sum(1 for i in self.r.app.store.items(ME) if i["subject"].startswith("构建失败") and i["flagged"]), 4)

    def test_only_a_machines_same_notice_shares_a_row(self):
        self.sorted_as(740, "[org/app] 安全警告：依赖有漏洞", "通知 <noreply@hub.example>", "follow_up", priority="medium")
        self.sorted_as(741, "[org/web] 请你审阅 PR", "通知 <noreply@hub.example>", "follow_up", priority="medium")
        self.sorted_as(742, "第3季度报告", "张经理 <zhang@corp.example>", "reply", priority="medium")
        self.sorted_as(743, "第4季度报告", "张经理 <zhang@corp.example>", "reply", priority="medium")
        text = json.dumps(self.box()["card"], ensure_ascii=False)
        self.assertNotIn("（共", text)
        for subject in ("安全警告", "请你审阅 PR", "第3季度报告", "第4季度报告"):
            self.assertIn(subject, text)

    def test_mail_changed_on_the_page_while_the_card_stays_is_left_alone(self):
        """The card keeps its mail while most of it waits (#68), so the user may have changed some on the page:
        a tap moves only what is still as it was."""
        store = self.r.app.store
        for n in range(17):
            self.sorted_as(800 + n, f"推送 {n}", f"推送{n} <p{n}@push.example>", "archive")
        t = a2ui.build(self.box()["card"])
        ok = labelled(t, "归档 19 · 删除 1")
        name, ctx = t.find_action(ok)
        it = lambda subject: next(i for i in store.items(ME) if i["subject"] == subject)   # noqa: E731
        store.set_fields(it("推送 0")["id"], action="ignore")               # 改为删除, on the page
        store.set_fields(it("推送 1")["id"], flagged=1)                     # starred on the page
        again = self.box()
        self.assertEqual(again["id"].split("/", 1)[1], t.surface_id)         # 18 of 20 still waiting: the same card
        r = self.c.call_tool(name, ctx, user_action=user_action(t.surface_id, ok))
        self.assertFalse(r.is_error, r.text)
        self.assertIn("2 封在邮件助手里改过，没再动", r.text)
        inbox = self.r.inbox()
        self.assertIn("推送 0", inbox)
        self.assertIn("推送 1", inbox)
        self.assertNotIn("推送 2", inbox)
        self.assertNotIn("双十一预告", inbox)
        self.assertIn("合同扫描件", inbox)                                   # another part of the card: its own tap

    def test_old_mail_no_longer_flagged_is_not_moved_to_junk_with_the_rest(self):
        for n in range(3):
            self.flag(900 + n, f"旧通知 {n}", 3 + n)
        card = self.box()["card"]
        store = self.r.app.store
        keys = [k for k in a2ui.build(card).data["choices"] if k.startswith("s")]
        store.set_fields(int(keys[0][1:]), suspicious=0)                    # 不是可疑, on the page
        r = self.press(card, "按选的处理", **{k: "spam" for k in keys})
        self.assertIn("1 封在邮件助手里改过，没再动", r.text)
        self.assertEqual(store.get(int(keys[0][1:]))["state"], "pending")
        self.assertEqual(sum("旧通知" in x for x in self.r.inbox()), 1)

    def test_a_card_stays_while_nine_tenths_of_its_mail_waits(self):
        store = self.r.app.store
        for n in range(7):
            self.sorted_as(950 + n, f"推送 {n}", f"推送{n} <p{n}@push.example>", "archive")
        first = self.box()                                                  # 10: 3 of the setUp, 7 here
        self.assertIn("10 封可以整理", first["title"])
        gone = [next(i for i in store.items(ME) if i["subject"] == f"推送 {n}")["id"] for n in range(2)]
        self.r.app.actions(ME).execute(gone[:1], "archive")                 # 9 of 10 still wait: the same card
        self.assertEqual(self.box()["id"], first["id"])
        self.r.app.actions(ME).execute(gone[1:], "archive")                 # 8 of 10: made again
        nxt = self.box()
        self.assertNotEqual(nxt["id"], first["id"])
        self.assertIn("8 封可以整理", nxt["title"])

    def test_newer_mail_waiting_for_the_next_tidy_card_is_said(self):
        first = self.box()
        self.sorted_as(970, "推送", "推送 <p@push.example>", "archive")
        again = self.box()
        self.assertEqual((again["id"], again["title"]), (first["id"], first["title"]))
        self.assertIn("之后又分拣好 1 封，下一张再说", again["summary"])

    def test_events_from_one_sender_are_told_apart_by_their_date(self):
        for n, day in enumerate(("2026-11-12", "2026-11-19")):
            self.sorted_as(980 + n, f"课程第 {n + 1} 讲", "课程 <class@school.example>", "schedule", priority="medium",
                           event={"title": f"第 {n + 1} 讲", "date": day, "start": "19:00", "end": "20:00"})
        s = a2ui.build(self.box()["card"])
        for when in ("11-12 19:00", "11-19 19:00"):                          # on its own row, 加入日程 in its tooltip
            self.assertEqual(s.components[labelled(s, when)]["tooltip"], "加入日程")
        with mock.patch.object(agent_mcp, "CLIENT_CAPABILITIES", {}):        # an older core: under the card
            text = json.dumps(self.box()["card"], ensure_ascii=False)
        self.assertIn("加入日程：11-12 19:00", text)
        self.assertIn("加入日程：11-19 19:00", text)

    def test_one_event_is_added_by_a_plain_button_and_the_heading_does_not_call_a_flag_a_scam(self):
        with mock.patch.object(agent_mcp, "CLIENT_CAPABILITIES", {}):
            labelled(a2ui.build(self.box()["card"]), "加入日程")              # the subject is on its row
            sus = json.dumps(self.outbox()["suspicious"][0]["card"], ensure_ascii=False)
        self.assertIn("这封邮件判为可疑", sus)
        self.assertNotIn("诈骗", sus)
        sus = json.dumps(self.outbox()["suspicious"][0]["card"], ensure_ascii=False)
        self.assertIn("可疑邮件", sus)
        self.assertNotIn("诈骗", sus)

    def test_older_flagged_mail_joining_the_tail_keeps_the_card(self):
        for n in range(12):
            self.flag(500 + n, f"旧通知 {n}", 3 + n)
        first = self.box()
        self.flag(530, "更旧的通知", 20)                                    # older than the 8 rows: left to the page
        self.assertTrue(any(i["subject"] == "更旧的通知" and i["suspicious"] for i in self.r.app.store.items(ME, "pending")))
        again = self.box()
        self.assertEqual(again["id"], first["id"])
        self.assertIn("13 封判为可疑", again["title"])

    def search(self, **args):
        return self.c.call_tool("mail_search", args)

    def test_search_finds_mail_by_subject_sender_and_body(self):
        hits = self.search(query="答辩").structured["hits"]
        self.assertEqual([h["title"] for h in hits], ["答辩时间确认"])
        self.assertTrue(hits[0]["strong"])
        self.assertEqual((hits[0]["source"], hits[0]["kind"]), (ME, "mail"))
        self.assertEqual(hits[0]["actions"][0]["name"], "mail_show")
        self.assertIn("mail_show", [t["name"] for t in self.c.list_tools()])
        self.assertEqual([h["title"] for h in self.search(query="wang@school").structured["hits"]], ["答辩时间确认"])
        body = self.search(query="正文 100").structured["hits"]
        self.assertEqual([h["title"] for h in body], ["答辩时间确认"])
        self.assertFalse(body[0]["strong"])
        self.assertIn("正文 100", body[0]["snippet"])

    def test_search_every_word_and_wildcards_are_plain(self):
        self.assertEqual(self.search(query="周会 项目").structured["hits"][0]["title"], "项目周会")
        self.assertEqual(self.search(query="周会 答辩").structured["hits"], [])
        self.assertEqual(self.search(query="%").structured["hits"], [])

    def test_search_with_only_a_time_and_nothing_at_all(self):
        self.assertEqual(len(self.search(since="2020-01-01", limit=3).structured["hits"]), 3)
        self.assertEqual(self.search(until="2020-01-01").structured["hits"], [])
        self.assertEqual(self.search().structured["hits"], [])

    def test_search_masks_codes_and_marks_suspicious(self):
        store = self.r.app.store
        it = next(i for i in store.items(ME) if i["subject"] == "登录验证码")
        store.set_fields(it["id"], body="您的验证码是 482913，五分钟内有效")
        self.assertNotIn("482913", json.dumps(self.search(query="五分钟").structured, ensure_ascii=False))
        self.assertIn("可疑", self.search(query="账户已冻结").structured["hits"][0]["line"])

    def test_search_skips_the_trash_and_reaches_older_mail(self):
        store = self.r.app.store
        it = next(i for i in store.items(ME) if i["subject"] == "双十一预告")
        store.set_state([it["id"]], "done", "trash")
        self.assertEqual(self.search(query="双十一预告").structured["hits"], [])
        store.old_sync(ME, "inbox", [{"message_id": "<old@x.example>", "uid": "9", "folder": "INBOX", "from_name": "老朋友",
                                      "from_addr": "old@x.example", "subject": "十年前的合影", "date": "2016-05-01 10:00"}])
        hit = self.search(query="合影").structured["hits"][0]
        self.assertEqual((hit["date"], hit["line"], hit["snippet"]), ("2016-05-01", "老朋友", ""))

    def test_reading_the_outbox_puts_nothing_in_the_calendar(self):
        # the button's Google link is made ahead; the appointment counts as added only once the user taps it
        self.assertIn("加入日程", json.dumps(self.box()["card"], ensure_ascii=False))
        self.assertFalse(self.r.app.store.cache_get(ME, "calendar_added")[0])

    def test_opening_a_hit_needs_the_tap(self):
        hit = self.search(query="答辩").structured["hits"][0]
        self.assertEqual(self.c.call_tool("mail_show", hit["actions"][0]["args"]).structured["code"], "needs_user_action")

    def hit(self, query):
        return self.search(query=query).structured["hits"][0]

    def test_reading_a_mail_gives_masked_text_marked_as_data(self):
        store = self.r.app.store
        it = next(i for i in store.items(ME) if i["subject"] == "登录验证码")
        store.set_fields(it["id"], body="您的验证码是 482913，请勿告诉他人")
        r = self.c.call_tool("mail_read", {"id": self.hit("登录验证码")["id"]})
        self.assertTrue(r.structured["untrusted"])
        self.assertIn("请勿告诉他人", r.structured["mail"]["body"])
        self.assertNotIn("482913", json.dumps(r.structured, ensure_ascii=False))

    def test_reading_old_mail_fetches_it_and_marks_nothing_read(self):
        store = self.r.app.store
        mid = "<o100@x.example>"
        m = next(x for x in self.r.srv.folders["INBOX"] if x["mid"] == mid)
        it = next(i for i in store.items(ME) if i["message_id"] == mid)
        with store.lock:
            store.db.execute("DELETE FROM items WHERE id=?", (it["id"],))
            store.db.commit()
        store.old_sync(ME, "inbox", [{"message_id": mid, "uid": str(m["uid"]), "folder": "INBOX", "subject": "老信",
                                      "from_addr": "wang@school.example", "date": "2016-05-01 10:00"}])
        before = set(m["flags"])
        r = self.c.call_tool("mail_read", {"id": f"{ME}|{mid}"})
        self.assertIn("正文 100", r.structured["mail"]["body"])
        self.assertEqual(m["flags"], before)

    def test_a_uid_holding_another_message_is_not_read_as_this_one(self):
        store = self.r.app.store
        store.old_sync(ME, "inbox", [{"message_id": "<gone@x.example>", "uid": str(self.r.srv.folders["INBOX"][0]["uid"]),
                                      "folder": "INBOX", "subject": "老信", "date": "2016-05-01 10:00"}])
        self.assertEqual(self.c.call_tool("mail_read", {"id": f"{ME}|<gone@x.example>"}).code, "stale")

    def test_the_conversation_gets_a_card_and_only_the_tap_acts(self):
        ids = [self.hit("答辩")["id"], self.hit("本周精选")["id"], self.hit("账户已冻结")["id"]]
        r = self.c.call_tool("mail_plan", {"ids": ids})
        self.assertEqual(a2ui.validate(r.card), [])
        self.assertEqual(self.r.inbox().count("本周精选"), 1)                # nothing has moved
        s = a2ui.build(r.card)
        self.assertEqual(sorted(v[0] for v in s.data["choices"].values()), ["archive", "draft", "keep"])   # a flag: 先不动
        self.assertIn("移到垃圾邮件", json.dumps(r.card, ensure_ascii=False))
        self.assertNotIn("退订", json.dumps(r.card, ensure_ascii=False))
        name, ctx = s.find_action(button(s, "mail_apply_plan"))
        self.assertEqual(self.c.call_tool(name, ctx).code, "needs_user_action")
        ctx["choices"] = {k: ["keep"] for k in ctx["choices"]}
        self.assertTrue(self.c.call_tool(name, ctx, user_action=user_action(s.surface_id)).done)

    def test_starred_mail_asked_about_in_the_conversation_is_still_acted_on(self):
        """A star already there when the plan was made is not a change on the page; one added after is."""
        store = self.r.app.store
        it = lambda subject: next(i for i in store.items(ME) if subject in (i["subject"] or ""))   # noqa: E731
        store.set_fields(it("本周精选")["id"], flagged=1)
        r = self.c.call_tool("mail_plan", {"ids": [self.hit("本周精选")["id"], self.hit("答辩")["id"]]})
        s = a2ui.build(r.card)
        name, ctx = s.find_action(button(s, "mail_apply_plan"))
        store.set_fields(it("答辩")["id"], flagged=1)                       # starred on the page after the plan
        ctx["choices"] = {k: ["archive"] for k in ctx["choices"]}
        r = self.c.call_tool(name, ctx, user_action=user_action(s.surface_id))
        self.assertFalse(r.is_error, r.text)
        self.assertIn("1 封在邮件助手里改过，没再动", r.text)
        inbox = self.r.inbox()
        self.assertNotIn("本周精选", inbox)
        self.assertIn("答辩时间确认", inbox)

    def test_asking_again_after_answering_gives_a_new_plan(self):
        ident = self.hit("答辩")["id"]
        first = self.c.call_tool("mail_plan", {"ids": [ident]}).structured["plan_id"]
        self.assertNotEqual(first, self.c.call_tool("mail_plan", {"ids": [ident]}).structured["plan_id"])

    def test_a_plan_only_for_mail_still_waiting(self):
        store = self.r.app.store
        it = next(i for i in store.items(ME) if i["subject"] == "本周精选")
        ident = self.hit("本周精选")["id"]
        store.set_state([it["id"]], "done", "archive")
        self.assertEqual(self.c.call_tool("mail_plan", {"ids": [ident]}).code, "stale")

    def habit(self, subject):
        store = self.r.app.store
        it = next(i for i in store.items(ME) if i["subject"] == subject)
        r = self.r.app.actions(ME).execute([it["id"]], "archive", by="rule")
        logs = store.cache_get(ME, "auto_logs")[0] or {}
        store.cache_set(ME, "auto_logs", {**logs, str(r["log_id"]): {"note_id": "n1", "sender": "from:x", "op": "archive", "label": None}})
        return it

    def test_auto_done_is_one_card_with_an_undo_per_batch(self):
        self.habit("本周精选")
        self.habit("双十一预告")
        got = self.outbox()["auto-done"]
        self.assertEqual(len(got), 1)
        self.assertIn("已自动处理 2 封", got[0]["title"])
        s = a2ui.build(got[0]["card"])
        undo = button(s, "mail_undo")
        name, ctx = s.find_action(undo)
        self.assertEqual(self.c.call_tool(name, ctx).code, "needs_user_action")
        r = self.c.call_tool(name, ctx, user_action=user_action(s.surface_id, undo))
        self.assertFalse(r.done)                                          # the other batch is still there
        self.assertEqual(a2ui.validate([{"version": "v0.9", "createSurface": {"surfaceId": s.surface_id, "catalogId": a2ui.AGENT_CATALOG}}] + r.card), [])
        self.assertIn("已自动处理 1 封", self.outbox()["auto-done"][0]["title"])
        self.assertEqual(self.c.call_tool(name, ctx, user_action=user_action(s.surface_id, undo)).code, "stale")

    def test_auto_done_put_down_stays_down_and_a_later_batch_replaces_a_shown_one(self):
        self.habit("本周精选")
        first = self.outbox()["auto-done"][0]
        self.c.call_tool("outbox_ack", {"id": first["id"], "state": "shown"})
        self.assertEqual(self.outbox()["auto-done"][0]["id"], first["id"])     # said: still there to undo
        self.habit("双十一预告")
        second = self.outbox()["auto-done"][0]
        self.assertIn("1 封", second["title"])
        self.assertNotEqual(second["id"], first["id"])
        self.c.call_tool("outbox_ack", {"id": second["id"], "state": "dismissed"})
        rest = self.outbox().get("auto-done", [])
        self.assertTrue(all("双十一" not in json.dumps(x["card"], ensure_ascii=False) for x in rest))

    def test_the_permission_is_reported_and_removing_needs_the_tap_and_the_page(self):
        self.assertEqual(self.c.read_json("permissions"), {"mailbox": "granted"})
        self.assertEqual(self.c.call_tool("mail_disconnect").code, "needs_user_action")
        r = self.c.call_tool("mail_disconnect", user_action=user_action("x"))
        self.assertIn("设置", r.text)
        self.assertEqual(self.r.app.mail_accounts(), [ME])

    # ---- the main window: the count and the base pages (protocol §1a) ----------------------------------
    def page(self, name):
        messages = self.c.read_json(f"page/{name}")
        self.assertEqual(a2ui.validate(messages, page=True), [], name)  # catalogue, 400 components (§4 bigPages), 2000 characters
        self.assertEqual(messages[0]["createSurface"]["surfaceId"], name)
        return a2ui.build(messages, page=True)

    @staticmethod
    def words(surface):
        return [c["text"] for c in surface.components.values() if c.get("component") == "Text"]

    def test_the_count_is_only_what_the_user_has_to_answer(self):
        self.assertEqual(self.c.read_json("summary"), {"count": 4, "text": "3 封要你处理，1 封可疑，3 封可以整理"})
        new = self.outbox()["new-mail"][0]
        keep = {k: "keep" for k in a2ui.build(new["card"]).data["choices"]}
        self.assertFalse(self.press(new["card"], "写草稿 1", **keep).is_error)
        box = self.box()
        keep = {k: "keep" for k in a2ui.build(box["card"]).data["choices"]}
        self.assertFalse(self.press(box["card"], "加星标 2", **keep).is_error)
        self.assertEqual(self.c.read_json("summary"), {"count": 1, "text": "1 封可疑，3 封可以整理"})   # left alone: not waiting

    def test_what_was_done_by_habit_is_in_the_line_and_not_in_the_count(self):
        self.habit("本周精选")
        got = self.c.read_json("summary")
        self.assertEqual(got["count"], 4)
        self.assertIn("今天自动处理了 1 封", got["text"])

    def test_no_mailbox_yet_is_said_once_and_leads_to_the_own_page(self):
        self.r.app.cfg["accounts"] = []
        self.assertEqual(self.c.read_json("summary"), {"count": 0, "text": "还没连邮箱"})
        self.assertEqual(self.c.read_json("permissions"), {"mailbox": "ask"})
        item = self.outbox()["needs-access"][0]
        self.assertEqual((item["id"], item["urgency"], item["permission"]), ("needs-access/mailbox", "low", "mailbox"))
        s = a2ui.build(item["card"])
        self.assertEqual(s.find_action(button(s, "agent.open_page"))[1], {"page": "settings"})   # the code is typed there
        with mock.patch.object(agent_mcp, "CLIENT_CAPABILITIES", {}):    # an older core: luoke-mail's own page
            s = a2ui.build(self.outbox()["needs-access"][0]["card"])
            self.assertEqual(s.find_action(button(s, "mail_open_page"))[1], {"page": "accounts"})
        self.assertNotIn("TextField", json.dumps(item["card"]))          # the code is typed on the own page only (§8)
        for name, _ in WINDOW_PAGES:
            if name != "settings":
                self.assertIn("还没有连接邮箱", " ".join(self.words(self.page(name))), name)
        self.c.call_tool("outbox_ack", {"id": item["id"], "state": "shown"})
        self.assertEqual(self.outbox()["needs-access"][0]["created"], item["created"])
        self.c.call_tool("outbox_ack", {"id": item["id"], "state": "dismissed"})
        self.assertEqual(self.outbox(), {})                              # put down: not said again
        self.assertIn("还没有连接邮箱", " ".join(self.words(self.page("inbox"))))    # the page still says so

    def test_a_connected_mailbox_is_never_asked_for(self):
        self.assertNotIn("needs-access", self.outbox())

    def record_line(self, subject="本周精选"):
        """A line of the record taken back the way a result card's 撤销 does (`log:<id>`)."""
        store = self.r.app.store
        it = next(i for i in store.items(ME) if i["subject"] == subject)
        log_id = self.r.app.actions(ME).execute([it["id"]], "archive")["log_id"]
        return None, "undo", "mail_undo", {"execution_id": f"log:{log_id}"}

    def test_a_line_of_the_record_is_taken_back_on_the_tap_and_only_once(self):
        p, undo, name, ctx = self.record_line()
        self.assertTrue(ctx["execution_id"].startswith("log:"))
        self.assertEqual(self.c.call_tool(name, ctx).code, "needs_user_action")
        self.assertNotIn("本周精选", self.r.inbox())
        r = self.c.call_tool(name, ctx, user_action=user_action("activity", undo))
        self.assertFalse(r.is_error, r.text)
        self.assertIn("本周精选", self.r.inbox())
        self.assertIn("（已撤销）", " ".join(self.words(self.page("activity"))))
        self.assertEqual(self.c.call_tool(name, ctx, user_action=user_action("activity", undo)).code, "stale")
        for bad in ("log:", "log:x", "log:99999"):
            self.assertEqual(self.c.call_tool(name, {"execution_id": bad}, user_action=user_action("activity")).code, "stale")

    def test_a_line_of_another_mailbox_is_not_taken_back(self):
        _, undo, name, ctx = self.record_line()
        self.r.app.mail_accounts = lambda: ["other@example.com"]
        self.assertEqual(self.c.call_tool(name, ctx, user_action=user_action("activity", undo)).code, "stale")

    def test_a_habits_work_taken_back_on_the_page_counts_against_the_habit(self):
        it = self.habit("本周精选")
        p = self.page("activity")
        comps = p.components
        undo = next(cid for cid in a2ui._reachable(comps) if comps[cid].get("component") == "Button")
        name, ctx = p.find_action(undo)
        self.assertFalse(self.c.call_tool(name, ctx, user_action=user_action("activity", undo)).is_error)
        self.assertIn(it["message_id"], self.r.app.store.cache_get(ME, "auto_undone")[0])      # not done to it again
        self.assertNotIn("auto-done", self.outbox())

    def test_a_plan_taken_back_line_by_line_can_still_be_undone_whole(self):
        s = a2ui.build(self.box()["card"])
        ok = labelled(s, "归档 2 · 删除 1")
        name, ctx = s.find_action(ok)
        r = self.c.call_tool(name, ctx, user_action=user_action(s.surface_id, ok))
        self.assertFalse(r.is_error, r.text)
        pid = ctx["plan_id"]
        first = self.r.app.store.plan_get(pid)["logs"][0]["log"]
        self.assertFalse(self.c.call_tool("mail_undo", {"execution_id": f"log:{first}"}, user_action=user_action("activity")).is_error)
        whole = self.c.call_tool("mail_undo", {"execution_id": pid}, user_action=user_action(s.surface_id, "undo"))
        self.assertFalse(whole.is_error, whole.text)
        self.assertEqual(self.r.app.store.plan_get(pid)["state"], "undone")
        self.assertEqual(self.c.call_tool("mail_undo", {"execution_id": pid}, user_action=user_action(s.surface_id, "undo")).code, "stale")

    def test_the_rules_page_lists_habits_and_rules(self):
        self.r.app.store.add_rule(ME, "sender", "news@weekly.example", "archive")
        habit = {"note_id": 7, "sender": "from:x", "category": "promotion", "action": "archive", "text": "某商城的促销都归档"}
        with mock.patch("luoke_mail.memory.auto_habits", lambda mem, acct: [habit]):
            p = self.page("rules")
        words = " ".join(self.words(p))
        self.assertIn("某商城的促销都归档", words)
        self.assertIn("news@weekly.example", words)

    def test_a_long_mailbox_stays_inside_a_pages_limits(self):
        r = self.r
        for n in range(200, 260):
            r.srv._add("INBOX", mail(n, f"第 {n} 封", f"人{n} <p{n}@corp.example>", 1), set())
            r.app.store.add_rule(ME, "sender", f"p{n}@corp.example", "archive")
        r.app.sync_mailbox(ME, new_job(), analyze=False)
        for it in r.app.store.unanalyzed(ME):
            if it["subject"].startswith("第 "):
                r.app.store.set_suggestion(it["id"], {"category": "work", "reason": "", "reply_points": [], "event": None, "suspicious": False,
                                                      "due_date": None, "action": "reply", "priority": "high", "summary": "问一件事"}, "model", "m")
                r.app.store.log(ME, "archive", [it["id"]], True, "已归档 1 封", [{"kind": "note"}])
        for name, _ in WINDOW_PAGES:                                     # each validated, so inside §4's limits
            self.page(name)
        with mock.patch.object(agent_mcp, "CLIENT_CAPABILITIES", {}):    # an older core: 200 a page
            for name, _ in WINDOW_PAGES:
                self.assertEqual(a2ui.validate(self.c.read_json(f"page/{name}")), [], name)
        self.assertIn("下一页", " ".join(self.words(self.page("inbox"))))
        self.assertIn("下一页", " ".join(self.words(self.page("activity"))))

    def test_a_code_in_a_mail_never_reaches_a_page(self):
        store = self.r.app.store
        it = next(i for i in store.items(ME) if i["subject"] == "答辩时间确认")
        store.set_fields(it["id"], summary="您的验证码是 482913")
        self.r.app.actions(ME).execute([next(i for i in store.items(ME) if i["subject"] == "本周精选")["id"]], "archive")
        for name, _ in WINDOW_PAGES:
            self.assertNotIn("482913", json.dumps(self.c.read_json(f"page/{name}"), ensure_ascii=False), name)
        self.assertNotIn("482913", json.dumps(self.c.read_json("summary"), ensure_ascii=False))

    def test_what_opens_something_says_it_is_on_the_mac(self):
        tools = {t["name"]: t for t in self.c.list_tools()}
        for name in ("mail_open", "mail_open_settings", "mail_open_page", "mail_open_calendar", "mail_show", "mail_disconnect"):
            self.assertEqual(tools[name]["_meta"], {"ai.luoke/where": "mac"}, name)
        self.assertNotIn("_meta", tools["mail_search"])

    def test_handing_an_event_to_the_calendar_needs_the_tap(self):
        """It writes a file and opens 「日历」: not a read, so not on the agent's word alone."""
        self.assertEqual(self.c.call_tool("mail_open_calendar", {"plan_id": "p", "key": "k"}).code, "needs_user_action")

    def test_what_the_capabilities_name_exists(self):
        here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packaging")
        with open(os.path.join(here, "accessory.json"), encoding="utf-8") as f:
            caps = json.load(f)["capabilities"]
        tools = {t["name"]: t for t in self.c.list_tools()}
        for a in caps["actions"]:
            self.assertFalse(tools[a["name"]]["annotations"]["readOnlyHint"], a["name"])
        writes = {a["name"] for a in caps["actions"]}
        self.assertEqual(writes, {n for n, t in tools.items() if not t["annotations"]["readOnlyHint"]})
        for name in (*caps["service"]["ask"].values(), caps["service"]["ui"]["open"]):
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"], name)
        pages = {p["id"] for p in caps["service"]["ui"]["pages"]}
        self.assertLessEqual(pages, set(agent.PAGES))                     # each opens somewhere on the own page
        for p in caps["permissions"]:
            self.assertIn(p["page"], pages)
            self.assertIn(p["remove"], writes)
        self.assertTrue(caps["service"]["summary"])
        for p in caps["service"]["pages"]:
            self.assertEqual(a2ui.validate(self.c.read_json(f"page/{p['id']}")), [], p["id"])
        resources = self.c.request("resources/list")["resources"]
        self.assertEqual({r["uri"] for r in resources if r["uri"].startswith("page/")}, {f"page/{p['id']}" for p in caps["service"]["pages"]})
        self.assertEqual({r["kind"]: r["urgency"] for r in caps["reports"]}["needs-access"], "low")
        self.assertTrue(caps["listing"]["tagline"] and caps["listing"]["category"])

    def test_carried_out_on_the_tap_then_undone(self):
        tidied = self.tidy()                                                 # what to tidy: its own tap
        self.assertNotIn("可以整理", self.box()["title"])
        new = self.outbox()["new-mail"][0]
        s = a2ui.build(new["card"])
        r = self.press(new["card"], "写草稿 1")
        self.assertFalse(r.is_error, r.text)
        self.assertTrue(r.done)
        self.assertIn("草稿", r.text)
        self.assertNotIn("联系方式", r.text)                               # the draft only has what the mail had
        inbox = self.r.inbox()
        for gone in ("本周精选", "双十一预告", "登录验证码"):
            self.assertNotIn(gone, inbox)                                   # archived, trashed
        self.assertIn("答辩时间确认", inbox)                                 # a draft was written; the mail stays
        self.assertIn("账户已冻结", inbox)                                   # not part of this plan
        self.assertEqual(len(self.r.srv.folders["[Gmail]/Drafts"]), 1)
        self.assertNotIn("new-mail", self.outbox())
        res = a2ui.update(s, r.structured["card"])                       # the card becomes the result, with 撤销
        name, ctx = res.find_action(button(res, "mail_undo"))
        u = self.c.call_tool(name, ctx, user_action=user_action(s.surface_id, "undo"))
        self.assertFalse(u.is_error, u.text)
        self.assertTrue(u.text.startswith("已撤销：") and "草稿删掉了" in u.text, u.text)
        self.assertEqual(len(self.r.srv.folders["[Gmail]/Drafts"]), 0)
        again = self.c.call_tool(name, ctx, user_action=user_action(s.surface_id, "undo"))
        self.assertEqual(again.structured.get("code"), "stale")
        box = self.box()                                                     # the rest that needs the user
        follow = next(k for k, e in self.r.app.store.plan_get(box["id"].split("/", 1)[1])["entries"].items()
                      if "合同扫描件" in (e.get("subject") or ""))
        r = self.press(box["card"], "加星标 2", **{follow: "keep"})          # the user changed one
        self.assertFalse(r.is_error, r.text)
        starred = [i["subject"] for i in self.r.app.store.items(ME) if i["flagged"]]
        self.assertEqual(starred, ["项目周会"])
        group = next(c for m in r.structured["card"] for c in (m.get("updateComponents") or {}).get("components", [])
                     if c["id"] == "root").get("group")
        self.assertIsNone(group)                                              # one left alone: not just one result
        b = a2ui.build(box["card"])
        u = self.c.call_tool("mail_undo", {"execution_id": b.surface_id}, user_action=user_action(b.surface_id, "undo"))
        self.assertIn("星标去掉了", u.text)
        undo = tidied.structured["card"]                                   # the tidy result has its own 撤销
        self.assertIn('"mail_undo"', json.dumps(undo))
        pid = next(m["updateComponents"]["surfaceId"] for m in undo if "updateComponents" in m)
        u = self.c.call_tool("mail_undo", {"execution_id": pid}, user_action=user_action(pid, "undo"))
        self.assertTrue("回到收件箱" in u.text, u.text)
        self.assertIn("双十一预告", self.r.inbox())

    def test_one_kind_of_result_joins_its_mailboxs_line(self):
        r = self.tidy()
        root = next(c for m in r.structured["card"] for c in (m.get("updateComponents") or {}).get("components", [])
                    if c["id"] == "root")
        self.assertIsNone(root.get("group"))                               # archived and deleted: two results
        s = a2ui.build(self.outbox()["suspicious"][0]["card"])
        spam = labelled(s, "移到垃圾邮件")
        name, ctx = s.find_action(spam)
        r = self.c.call_tool(name, ctx, user_action=user_action(s.surface_id, spam))
        root = next(c for m in r.structured["card"] for c in (m.get("updateComponents") or {}).get("components", [])
                    if c["id"] == "root")
        self.assertEqual(root["group"], {"key": f"{ME}/spam", "label": "可疑邮件", "text": "已移到垃圾邮件 {n} 封", "n": 1,
                                         "badge": ME})

    def tidy(self, words="归档 2 · 删除 1"):
        r = self.press(self.box()["card"], words)
        self.assertFalse(r.is_error, r.text)
        return r

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
        bad = dict(ctx, choices={**ctx["choices"], "tidy": ["suggested"]})  # a row that is not on this card
        self.assertEqual(self.c.call_tool(name, bad, user_action=user_action(s.surface_id)).structured.get("code"), "invalid")
        self.r.app.cfg["mode"]["write"] = False
        t = a2ui.build(self.box()["card"])
        self.assertIn("开启处理模式", json.dumps(t.messages(), ensure_ascii=False))     # said before the tap
        ok = labelled(t, "归档 1 · 删除 2")
        name, ctx = t.find_action(ok)
        for wrong in (dict(ctx, choices={"tidy": ["unsubscribe"]}), dict(ctx, part="nothing")):
            self.assertEqual(self.c.call_tool(name, wrong, user_action=user_action(t.surface_id)).structured.get("code"), "invalid")
        r = self.c.call_tool(name, ctx, user_action=user_action(t.surface_id, ok))
        self.assertEqual(r.structured.get("code"), "needs_setup")          # several to the trash (ADR 0016)
        self.assertIn("处理模式", r.text)
        later = a2ui.update(t, r.structured["card"])                       # the same plan, with 打开设置
        self.assertEqual(later.find_action(labelled(later, "归档 1 · 删除 2"))[1], ctx)
        self.assertEqual(later.find_action(labelled(later, "打开设置"))[1], {"page": "settings"})
        self.assertIn("双十一预告", self.r.inbox())                           # nothing moved
        self.r.app.cfg["mode"]["write"] = True
        self.assertFalse(self.c.call_tool(name, ctx, user_action=user_action(t.surface_id, ok)).is_error)   # the same plan, later
        self.assertNotIn("双十一预告", self.r.inbox())
        self.assertIn("合同扫描件", self.r.inbox())                           # 待处理 on the same card: not this tap

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
        self.assertNotIn("new-mail", self.outbox())                            # only more to tidy: not this card
        self.r.srv._add("INBOX", mail(202, "请确认报价", "张经理 <zhang@corp.example>", 0), set())
        self.r.app.sync_mailbox(ME, new_job(), analyze=False)
        n = next(i for i in self.r.app.store.unanalyzed(ME) if i["subject"] == "请确认报价")
        self.r.app.store.set_suggestion(n["id"], {"category": "work", "reason": "", "reply_points": [], "event": None,
                                                  "suspicious": False, "due_date": None, "action": "reply",
                                                  "priority": "high", "summary": "报价"}, "model", "m")
        again = self.outbox()["new-mail"][0]                                   # new mail for the user: said again, the others with it
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


class SwitchTests(unittest.TestCase):
    """On since the agent ships (beta 2): the manifest declares it, LUOKE_MAIL_AGENT=0 turns it off."""

    def test_on_unless_turned_off_and_the_manifest_declares_it(self):
        from luoke_mail import agent
        from luoke_mail.agent_mcp import AGENT_PROTOCOL
        saved = os.environ.pop("LUOKE_MAIL_AGENT", None)
        self.addCleanup(lambda: saved is None or os.environ.__setitem__("LUOKE_MAIL_AGENT", saved))
        self.assertTrue(agent.enabled())
        os.environ["LUOKE_MAIL_AGENT"] = "0"
        self.assertFalse(agent.enabled())
        here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packaging")
        with open(os.path.join(here, "accessory.json"), encoding="utf-8") as f:
            caps = json.load(f)["capabilities"]
        self.assertEqual(caps["protocol"], AGENT_PROTOCOL)
        self.assertEqual(os.path.expanduser(caps["socket"]), os.path.join(os.path.expanduser("~"), ".luoke-mail", "run", "agent.sock"))


class CallersTests(unittest.TestCase):
    """Who may connect (security review S2): in the app what looks like 螺壳's agent core, 螺壳.app's own Python
    started by 螺壳.app, signed as 螺壳 by this app's team."""

    def setUp(self):
        from luoke_mail import agent, desktop, memory
        self.agent, self.desktop = agent, desktop
        self.hub = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.hub, True)
        app = os.path.join(self.hub, "螺壳.app")
        for d in ("MacOS", "_CodeSignature", os.path.join("Resources", "python", "bin")):
            os.makedirs(os.path.join(app, "Contents", d))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": memory.HUB_ID, "CFBundleExecutable": "LuokeCore"}, f)
        for f in (("MacOS", "LuokeCore"), ("Resources", "python", "bin", "python3"), ("_CodeSignature", "CodeResources")):
            open(os.path.join(app, "Contents", *f), "w").close()
        self.main = os.path.realpath(os.path.join(app, "Contents", "MacOS", "LuokeCore"))
        self.core = os.path.realpath(os.path.join(app, "Contents", "Resources", "python", "bin", "python3"))
        self.procs = {10: (self.core, 9), 9: (self.main, 1), 20: (self.core, 30), 30: ("/bin/zsh", 1), 40: ("/usr/bin/python3", 9)}
        self.signs = []
        for name, fn in (("process_path", lambda pid: self.procs.get(pid, (None, None))[0]),
                         ("parent_pid", lambda pid: self.procs.get(pid, (None, None))[1]),
                         ("signed_as", lambda app, bid, team: self.signs.append((app, bid, team)) or self.signed),
                         ("team_of", lambda app: self.team), ("own_app", lambda: "/Applications/螺壳邮件助手.app")):
            saved = getattr(desktop, name)
            setattr(desktop, name, fn)
            self.addCleanup(setattr, desktop, name, saved)
        saved_hub = memory.hub_app
        memory.hub_app = lambda: app
        self.app = app
        self.addCleanup(setattr, memory, "hub_app", saved_hub)
        self.signed, self.team = True, "TEAM1"

    def test_only_the_agent_core_that_luoke_started(self):
        c = self.agent.Callers(checked=True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(c.allow(10))                   # 螺壳.app's Python, started by 螺壳.app
            self.assertFalse(c.allow(20))                  # the same Python, started from a shell
            self.assertFalse(c.allow(40))                  # another Python, started by 螺壳.app
            self.assertFalse(c.allow(9))                   # not the core
            self.assertFalse(c.allow(None))
        self.assertEqual(self.signs, [(self.app, "ai.luoke.core", "TEAM1")])
        c.allow(10)
        self.assertEqual(len(self.signs), 1)                   # checked once per 螺壳 version

    def test_inside_luoke_the_app_it_runs_in(self):
        """Part of 螺壳.app: the agent core is that app's, even when another 螺壳.app is found first."""
        from luoke_mail import memory
        memory.hub_app = lambda: "/Applications/螺壳.app"
        with mock.patch.object(self.desktop, "in_luoke", lambda: True), \
                mock.patch.object(self.desktop, "own_app", lambda: self.app), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(self.agent.Callers(checked=True).allow(10))

    def test_not_when_the_signature_is_not_luokes(self):
        self.signed = False
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self.agent.Callers(checked=True).allow(10))

    def test_a_failure_is_asked_again(self):
        self.signed = False
        c = self.agent.Callers(checked=True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(c.allow(10))
            self.signed = True
            self.assertTrue(c.allow(10))
        self.assertEqual(len(self.signs), 2)

    def test_an_app_without_a_team_lets_no_one_in(self):
        """An ad hoc build: on the bundle id alone, any app named ai.luoke.core would pass."""
        self.team = None
        c = self.agent.Callers(checked=True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(c.allow(10))
            self.team = "TEAM1"
            self.assertTrue(c.allow(10))                   # not kept: asked again
        self.assertEqual(self.signs, [(self.app, "ai.luoke.core", "TEAM1")])

    def _inside(self, bundle_id):
        """Mail in this 螺壳.app; `hub_app` finds another 螺壳 elsewhere."""
        from luoke_mail import memory
        os.makedirs(os.path.join(self.app, "Contents", "Resources", "mail"))
        open(os.path.join(self.app, "Contents", "Resources", "mail", "luoke-accessory.json"), "w").close()
        with open(os.path.join(self.app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleExecutable": "LuokeCore"}, f)
        self.desktop.own_app = lambda: self.app
        memory.hub_app = lambda: "/Applications/螺壳.app"

    def test_inside_luoke_the_core_is_the_one_in_this_app(self):
        """A second 螺壳 in the other Applications folder does not matter; the signature is still asked."""
        self._inside("ai.luoke.core")
        c = self.agent.Callers(checked=True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(c.allow(10))
            self.assertFalse(c.allow(20))
        self.assertEqual(self.signs, [(self.app, "ai.luoke.core", "TEAM1")])

    def test_inside_luoke_ad_hoc_still_lets_no_one_in(self):
        self._inside("ai.luoke.core")
        self.team = None
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self.agent.Callers(checked=True).allow(10))

    def test_the_development_build_skips_only_the_signature(self):
        self._inside("ai.luoke.core.dev")
        self.team = None
        c = self.agent.Callers(checked=True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(c.allow(10))
            self.assertFalse(c.allow(20))                  # the same Python, started from a shell
            self.assertFalse(c.allow(40))                  # another Python
        self.assertEqual(self.signs, [])

    def test_a_dev_bundle_id_outside_luoke_is_not_trusted(self):
        """A separate mail app never skips the signature, whatever the 主控 it finds calls itself."""
        with open(os.path.join(self.app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "ai.luoke.core.dev", "CFBundleExecutable": "LuokeCore"}, f)
        self.team = None
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self.agent.Callers(checked=True).allow(10))

    def test_from_a_source_tree_anyone_of_the_users(self):
        self.assertTrue(self.agent.Callers(checked=False).allow(20))

    def test_a_refused_process_gets_nothing(self):
        """Through the real socket: the server asks who connected (this test's own process) and answers only if
        allowed."""
        r = Rig(self)
        path = os.path.join(tempfile.mkdtemp(dir="/tmp"), "a.sock")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        asked = []
        callers = self.agent.Callers(checked=True)
        callers.allow = lambda pid: asked.append(pid) or pid == os.getpid()
        r.app.agent.start(path, callers)
        self.addCleanup(r.app.agent.close)
        self.assertTrue(ToolClient(path).list_tools())
        self.assertEqual(set(asked), {os.getpid()})
        callers.allow = lambda pid: False
        with self.assertRaises(Exception):
            ToolClient(path).list_tools()
        callers.allow = lambda pid: 1 / 0               # a check that fails turns the caller away, and the server lives on
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(Exception):
            ToolClient(path).list_tools()
        callers.allow = lambda pid: True
        self.assertTrue(ToolClient(path).list_tools())


if __name__ == "__main__":
    unittest.main()
