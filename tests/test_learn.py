"""了解你 and 功耗. Who said it decides what an inference may do: the user's own sent mail takes effect when two
messages agree (the newest value wins on one-value topics); organisations' mail and the user's habits only ever
are proposed for the memory to grade; credentials are never kept; on by default, can be paused (ADR 0013).
功耗 makes every model call wait a multiple of the previous call's time."""
import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock
from email.message import EmailMessage
from email.utils import formatdate

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import engines, learn, learner, memory  # noqa: E402
from fakes import FakeChat  # noqa: E402
from test_digest import DigestRig  # noqa: E402
from test_old_mail import ME, mail  # noqa: E402
from test_background import triage_reply  # noqa: E402

SENT_FOLDER = {"gmail": "[Gmail]/Sent Mail", "qq": "Sent Messages", "netease": "&XfJT0ZAB-"}


def sent(n, to, subject, body, days_ago):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = f"小周 <{ME}>", to, subject
    m["Date"] = formatdate(time.time() - days_ago * 86400, localtime=True)
    m["Message-ID"] = f"<s{n}@example.com>"
    m.set_content(body)
    return m.as_bytes()


def model(body):
    system, user = body["messages"][0]["content"], body["messages"][-1]["content"]
    if system.startswith("下面是用户自己发出的邮件"):
        blocks = re.split(r"=== 邮件 (\d+) ===", user)[1:]
        mails = {int(blocks[i]): blocks[i + 1] for i in range(0, len(blocks), 2)}
        facts = []
        for title in ("产品经理", "产品负责人"):
            ns = [n for n, t in mails.items() if title in t]
            if ns:
                facts.append({"topic": "职位", "who": "", "text": f"职位：{title}", "n": ns})
        ns = [n for n, t in mails.items() if "王磊" in t]
        if ns:
            facts.append({"topic": "同事", "who": "王磊", "text": "同事：王磊（wanglei@corp.example）", "n": ns})
        ns = [n for n, t in mails.items() if "体检" in t]
        if ns:
            facts.append({"topic": "其他", "who": "", "text": "最近在医院做体检", "n": ns})       # sensitive: dropped
        return 200, json.dumps({"facts": facts}, ensure_ascii=False)
    if system.startswith("下面是用户收到的、来自一些机构的邮件"):
        senders = re.findall(r"^\[(\d+)\] (\S+)", user, re.M)
        return 200, json.dumps({"facts": [{"topic": "会员" if "航空" in name else "账户", "who": name,
                                           "text": f"是{name}的会员" if "航空" in name else f"在{name}有账户", "n": [int(n)]}
                                          for n, name in senders]}, ensure_ascii=False)
    return triage_reply(body)


class LearnRig(DigestRig):
    def __init__(self, test, **kw):
        super().__init__(test, **kw)
        self.chat.__exit__()
        self.chat = FakeChat(model).__enter__()
        test.addCleanup(self.chat.__exit__)
        self.app.cfg["engine"]["base_url"] = self.chat.base_url
        self.folder = SENT_FOLDER[self.srv.flavour]

    def send(self, *raws):
        for raw in raws:
            self.srv._add(self.folder, raw, {"\\Seen"})

    def run(self, acct=ME):
        for _ in range(20):
            if not self.app.learner._next(acct, self.app.learner.state(acct)):
                return
            self.app.learner.slice(acct, {"cancel": False})
            problem = self.app.learner.state(acct).get("problem")
            assert not problem, problem
        raise AssertionError("learning made no progress")

    def on(self, source="mailbox"):
        self.app.idle_step = lambda **k: None
        self.app.learner.set_on({"source": source, "on": True, "consent": True})


class EngineTests(unittest.TestCase):
    def test_credentials_are_masked_and_the_rest_kept_whatever_the_model_says(self):
        for text, secret in (("银行卡号 4111 1111 1111 1111", "4111 1111"), ("验证码 482913", "482913")):
            self.assertNotIn(secret, learn.mask(text), text)                    # only that string is hidden (ADR 0013)
            self.assertTrue(learn.mask(text).startswith(text[:3]))
        self.assertIn("health", memory.marks("最近在医院复查"))                    # kept, marked
        for text in ("职位：产品负责人", "同事：王磊（wanglei@corp.example）", "邮编 00427 的房价"):
            self.assertEqual(learn.mask(text), text)
        out = learn.parse_facts(json.dumps({"facts": [
            {"topic": "职位", "who": "", "text": "职位：产品经理", "n": [1, 9]},
            {"topic": "同事", "who": "王磊", "text": "同事：王磊", "n": [2]},
            {"topic": "爱好", "who": "", "text": "喜欢跑步", "n": [1]},            # not a topic we keep
            {"topic": "其他", "who": "", "text": "在医院复查", "n": [1]},          # sensitive context: kept (marked later)
            {"topic": "其他", "who": "", "text": "身份证号 110101199001011234", "n": [2]}]}), 2)
        self.assertTrue(all("110101199001011234" not in o["text"] for o in out))   # the ID number masked, not the fact dropped
        out = [o for o in out if "身份证" not in o["text"]]
        self.assertEqual(out, [{"topic": "职位", "text": "职位：产品经理", "n": [1]}, {"topic": "同事:王磊", "text": "同事：王磊", "n": [2]},
                               {"topic": "其他:在医院复查", "text": "在医院复查", "n": [1]}])   # 其他: one key per fact

    def test_organisations_come_as_whole_sentences_and_services_apart_from_accounts(self):
        out = learn.parse_facts(json.dumps({"facts": [
            {"topic": "服务", "who": "shop.example", "text": "在 shop.example 买东西", "n": [1]},
            {"topic": "其他", "who": "shop.example", "text": "shop.example", "n": [1]},        # the name alone says nothing
            {"topic": "账户", "who": "招商银行", "text": "持有信用卡", "n": [2]}]}), 2, learn.LIFE_TOPICS)
        self.assertEqual(out, [{"topic": "服务:shop.example", "text": "在 shop.example 买东西", "n": [1]},
                               {"topic": "账户:招商银行", "text": "招商银行：持有信用卡", "n": [2]}])
        self.assertEqual(learner.Learner.ABOUT["服务"], "service")
        self.assertIn("服务", learn.RECEIVED_PROMPT)
        # a fact that names its organisation another way gets no prefix; the name is kept whole (10-22)
        out = learn.parse_facts(json.dumps({"facts": [
            {"topic": "账户", "who": "Example Cloud Services Co., Ltd.", "text": "在示例云有账户", "n": [1]}]}), 1, learn.LIFE_TOPICS)
        self.assertEqual(out, [{"topic": "账户:Example Cloud Services Co., Ltd.", "text": "在示例云有账户", "n": [1]}])

    def test_only_the_users_own_words_go_to_the_model(self):
        m = {"subject": "Re: 预算", "to": ["boss@corp.example"], "date": "2026-10-01 10:00", "body": "好的。\n\n小周\n产品经理"}
        text = learn.sent_messages([m], ME)[1]["content"]
        self.assertIn("收件人：boss@corp.example", text)
        self.assertIn("产品经理", text)
        long = {**m, "body": "开头" + "中" * 2000 + "签名：小周 产品经理"}
        self.assertTrue(learn.own_text(long).endswith("签名：小周 产品经理"))
        self.assertLessEqual(len(learn.own_text(long)), learn.SENT_CHARS + 5)


class SentTests(unittest.TestCase):
    def check(self, flavour, provider):
        r = LearnRig(self, flavour=flavour, provider=provider)
        r.send(sent(1, "boss@corp.example", "周报", "王总，本周周报如下。\n小周 产品负责人", 3),
               sent(2, "wanglei@corp.example", "排期", "王磊，下午碰一下排期。\n小周 产品负责人", 10),
               sent(3, "boss@corp.example", "去年的周报", "王总，周报。\n小周 产品经理", 400),
               sent(4, "hr@corp.example", "入职材料", "附件是材料。\n小周 产品经理", 420),
               sent(5, "clinic@x.example", "体检预约", "下周去做体检。", 5))
        self.assertEqual(r.app.learner.next_account(), ME)                       # on from the start (ADR 0013)
        r.on()
        r.run()
        mem = r.app.mem
        facts = {n["text"]: n for n in mem.notes(kinds=["fact"])}
        self.assertEqual(sorted(facts), ["职位：产品负责人"])                     # newest value; 王磊 and 体检 once only
        n = mem.note(facts["职位：产品负责人"]["id"])
        self.assertEqual((n["origin"], n["status"], n["decided_at"]), ("inferred", "active", None))
        self.assertEqual({(c["speaker"], c["topic"]) for c in n["cites"]}, {("user", "sent-mail")})
        # one statement per message and thing said, keyed by the thing, at the time it was sent (ADR 0012 amended)
        stated = [x for x in mem.records(topic="sent-mail") if x["text"] == "职位：产品负责人"]
        self.assertEqual(len(stated), 2)
        self.assertEqual({(x["data"]["subject"], x["data"]["by"]) for x in stated}, {("职位", "user")})
        self.assertTrue(all(x["ref"].endswith("#职位") and x["happened_at"] for x in stated))
        self.assertTrue(all("产品负责人" in x["data"]["text"] for x in stated))      # what the user wrote, in the record
        self.assertNotIn("产品经理", json.dumps(mem.notes(status=[]), ensure_ascii=False))
        self.assertIn("职位：产品负责人", memory.about(mem))
        self.assertEqual(r.app.learner.view(ME)["sent_done"], 5)
        self.assertEqual(r.srv.violations, [])
        self.assertNotIn("STORE", " ".join(r.srv.commands))
        return r

    def test_only_the_users_own_words_reach_the_model_not_what_they_forwarded(self):
        fwd = ("看看这个，你感兴趣吗？\n\n---------- Forwarded message ----------\nFrom: 某某基金 <vip@fund.example>\n"
               "尊敬的周晓女士：您是我行的私人银行客户。")
        self.assertEqual(learn.own_text({"body": fwd}), "看看这个，你感兴趣吗？")
        self.assertEqual(learn.own_text({"body": "Begin forwarded message:\n\nFrom: x\n你是 VIP"}), "（转发，没有附言）")
        quoted = "好的，周五前发您。\n\n周晓\n\n在 2025年10月1日 王总 写道：\n请把方案再改一版"
        self.assertNotIn("方案再改", learn.own_text({"body": quoted}))
        self.assertIn("周晓", learn.own_text({"body": quoted}))

    def test_health_is_kept_marked_and_waits_for_the_users_word(self):
        import sqlite3
        r = LearnRig(self)
        r.send(sent(1, "clinic@x.example", "体检预约", "下周去做体检。", 5), sent(2, "clinic@x.example", "体检改期", "体检改到周五。", 3))
        r.on()
        r.run()
        self.assertFalse(any("体检" in n["text"] for n in r.app.mem.notes(kinds=["fact"])))   # health waits for the user
        self.assertNotIn("体检", memory.about(r.app.mem))                                     # so not in prompts yet
        db = sqlite3.connect(r.app.mem.path)
        self.addCleanup(db.close)
        row = db.execute("SELECT status, sensitive FROM notes WHERE text LIKE '%体检%'").fetchone()
        self.assertEqual(row[0], "proposed")                       # from the user's own mail, still only a guess
        self.assertIn("health", row[1])

    def test_gmail(self):
        r = self.check("gmail", "gmail")
        r.send(sent(6, "wanglei@corp.example", "Re: 排期", "王磊，没问题。", 1))     # new mail later: 王磊 twice now
        r.app.store.cache_update(ME, "learn", sent_checked_at=0)
        r.run()
        page = r.app.curator.memory_view("mailbox")
        colleague = next(n for n in page["fact"] if n["text"].startswith("同事"))
        self.assertEqual((colleague["origin_text"], colleague["learnt"]), ("根据你发出的邮件推断", True))
        r.app.curator.memory_change({"source": "mailbox", "op": "reject", "id": colleague["id"]})   # 不对: stays out
        r.app.store.cache_update(ME, "learn", sent_checked_at=0)
        r.send(sent(7, "wanglei@corp.example", "周末", "王磊，周末愉快。", 0), sent(8, "wanglei@corp.example", "周一", "王磊，早。", 0))
        r.run()
        self.assertFalse(any(n["text"].startswith("同事") for n in r.app.mem.notes(kinds=["fact"])))

    def test_qq(self):
        self.check("qq", "qq")

    def test_netease(self):
        self.check("netease", "163")


class EventTests(unittest.TestCase):
    """Top-level ADR 0012 amended (2026-10-17): what happens is recorded as it happens -- who did it, to which
    sender, when -- for the memory to conclude from; always, whether 了解你 is on or paused (ADR 0013)."""

    def test_what_the_user_does_and_what_came_are_recorded(self):
        r = LearnRig(self)
        for i in range(4):                                                         # a newsletter, months back, one opened
            r.srv._add("INBOX", mail(600 + i, f"周刊 {i}", "某周刊 <news@weekly.example>", 35 + 30 * i), {"\\Seen"} if i == 0 else set())
        r.app.sync_mailbox(ME, {"cancel": False})
        item = next(i for i in r.app.store.items(ME, "pending") if i["action"] != "archive")
        r.app.cfg["learn"]["on"] = False
        r.app.override({"source": "mailbox", "ids": [item["id"]], "action": "archive"})
        self.assertTrue(r.app.mem.records(topic="mail-override"))               # recorded with 了解你 paused too
        r.on()
        r.app.override({"source": "mailbox", "ids": [item["id"]], "action": "follow_up"})
        o = r.app.mem.records(topic="mail-override")[0]["data"]
        self.assertEqual((o["from"], o["to"], o["by"]), ("archive", "keep", "user"))     # the memory's words for it
        self.assertEqual(o["suggestion"], {"from": "archive", "to": "follow_up"})
        r.app.override({"source": "mailbox", "ids": [item["id"]], "action": "ignore"})
        o = r.app.mem.records(topic="mail-override")[0]["data"]
        self.assertEqual((o["from"], o["to"]), ("keep", "trash"))                         # 「移到垃圾箱」 is trash
        self.assertTrue(o["sender"].startswith(("from:", "list:")) and o["account"].startswith("mbx-"))
        r.app.cfg["mode"]["write"] = True
        r.app.items_act(ME, [item["id"]], "archive")
        a = r.app.mem.records(topic="mail-acted")[0]["data"]
        self.assertEqual((a["action"], a["count"], a["by"]), ("archive", 1, "user"))
        r.app.profiler.run(ME, {"cancel": False})                                 # how newsletters were read: weekly
        reads = [x for x in r.app.mem.records(topic="mail-read") if x["ref"].startswith("from:news@weekly.example")]
        self.assertGreaterEqual(len(reads), 3)                                     # one per finished month
        d = reads[0]["data"]
        self.assertEqual(set(d) >= {"month", "count", "opened", "sender", "by", "account"}, True)
        self.assertEqual(reads[0]["happened_at"][:7], d["month"])                  # when it happened, not when written
        opened = [x for x in reads if x["data"]["opened"]]
        self.assertTrue(opened and opened[0]["data"]["mails"][0]["text"])           # what it said: the memory reads it
        before = len(r.app.mem.records(topic="mail-read"))
        r.app.profiler.run(ME, {"cancel": False})
        self.assertEqual(len(r.app.mem.records(topic="mail-read")), before)        # the same months: not again


class ReceivedTests(unittest.TestCase):
    def test_organisations_become_guesses_a_few_at_a_time(self):
        r = LearnRig(self)
        r.send(sent(1, "friend@mail.example", "周末", "周末见。", 2))
        for k in range(7):                                                         # 7 organisations, months apart
            for i in range(3):
                r.srv._add("INBOX", mail(300 + 10 * k + i, f"账单 {i}", f"机构{k} <bill@org{k}.example>", 20 + 40 * i), {"\\Seen"})
        for i in range(3):                                                         # a person the user writes to
            r.srv._add("INBOX", mail(400 + i, f"聊天 {i}", "朋友 <friend@mail.example>", 20 + 40 * i), {"\\Seen"})
        for i in range(3):                                                         # never opened
            r.srv._add("INBOX", mail(410 + i, f"推广 {i}", "某店 <ad@shop2.example>", 20 + 40 * i), set())
        r.on()
        r.run()
        mem = r.app.mem
        # organisations' mail is graded by the memory on its evidence (ADR 0015): three mails each is thin, so
        # nothing takes effect; what was proposed is kept, as a guess
        self.assertEqual(mem.notes(kinds=["fact"]), [])
        seen = mem.notes(kinds=["fact"], status=["proposed", "observed"])
        self.assertTrue({f"在机构{k}有账户" for k in range(7)} <= {n["text"] for n in seen})
        # one organisation is one fact: the memory's topic is 「机构:<name>」 (top-level ADR 0015 amended, 10-22)
        self.assertTrue({f"机构:机构{k}" for k in range(7)} <= {n.get("topic") for n in seen})
        n = mem.note(seen[0]["id"])
        self.assertEqual({(c["speaker"], c["topic"]) for c in n["cites"]}, {("third_party", "received-mail")})
        # how strong the evidence is travels with each record, counts only (top-level ADR 0011)
        data = next(x for x in mem.records(topic="received-mail") if "机构0" in x["text"])["data"]
        self.assertEqual((data["count"], data["months"], data["opened"]), (3, 3, 3))
        self.assertLess(data["first"], data["last"])
        self.assertTrue(data["account"].startswith("mbx-"))
        prompt = next(b["messages"][1]["content"] for b in r.chat.requests if b["messages"][0]["content"].startswith("下面是用户收到"))
        self.assertNotIn("friend@mail.example", prompt)
        self.assertNotIn("shop2", prompt)
        self.assertEqual(r.app.learner.view(ME)["waiting"], 0)                    # nothing left waiting to be asked


    def test_a_plain_fact_with_strong_evidence_is_taken_down_without_asking(self):
        """ADR 0015: a fact from organisations' mail, seen in many opened mails over months, takes effect at once,
        whatever its kind (money included, ADR 0013); a thin one stays a guess."""
        r = LearnRig(self)
        for i in range(7):                                                         # strong: 7 mails, 4 months, opened
            r.srv._add("INBOX", mail(500 + i, f"会员通知 {i}", "某航空 <club@air.example>", 10 + 30 * i), {"\\Seen"})
            r.srv._add("INBOX", mail(520 + i, f"账单 {i}", "某银行 <card@bank.example>", 10 + 30 * i), {"\\Seen"})
        for i in range(3):                                                         # weak for now: 3 mails
            r.srv._add("INBOX", mail(540 + i, f"订单 {i}", "某书店 <shop@books.example>", 10 + 40 * i), {"\\Seen"})
        r.on()
        r.run()
        mem = r.app.mem
        facts = {n["text"]: n["status"] for n in mem.notes(kinds=["fact"], status=["active", "proposed", "observed"])}
        self.assertEqual(facts.get("是某航空的会员"), "active")                    # strong evidence: in effect, not asked
        self.assertEqual(facts.get("在某银行有账户"), "active")                    # money is no longer kept aside (ADR 0013)
        self.assertNotEqual(facts.get("在某书店有账户"), "active")                 # weak for now: a guess
        for i in range(3, 7):                                                      # the bookshop keeps writing
            r.srv._add("INBOX", mail(540 + i, f"订单 {i}", "某书店 <shop@books.example>", 5 + 40 * i), {"\\Seen"})
        r.app.store.cache_update(ME, "learn", received_at=0)                      # the next look at received mail
        r.run()
        # the same airline filed under 其他 by the model: it takes the sort of the fact already known (2026-10-17)
        key = next(c for c in r.app.store.learn_candidates(ME, "received") if c["text"] == "是某航空的会员")["evidence"]
        r.app.store.learn_add(ME, "received", "其他:某航空", "某航空：有乘机记录", key)
        r.app.store.learn_set(ME, "received", "其他:某航空", "某航空：有乘机记录".lower(), state="waiting")
        r.app.learner.refill(ME)
        # in effect, never waiting as a question: on its own (luoke-mem before 0.25) or merged into the airline's one
        # fact (0.25: one organisation, one fact -- top-level ADR 0015 amended)
        air = [n for n in mem.notes(kinds=["fact"], status=["active", "proposed", "observed"])
               if n.get("topic") in ("机构:某航空", "其他:某航空", "会员:某航空")]
        self.assertTrue(air and all(n["status"] == "active" for n in air), air)

    def test_a_refused_request_does_not_start_the_organisations_over(self):
        """螺壳主控 under 均衡 refuses a background request that comes too soon after the last (power cooldown).
        Each batch looked at stays looked at: one instance asked about the same 10 organisations ~3000
        times in a day (2026-10-15) because nothing was kept until the whole list was done."""
        r = LearnRig(self)
        r.send(sent(1, "friend@mail.example", "周末", "周末见。", 2))
        for k in range(15):                                                        # two batches of organisations
            for i in range(3):
                r.srv._add("INBOX", mail(300 + 10 * k + i, f"账单 {i}", f"机构{k} <bill@org{k}.example>", 20 + 40 * i), {"\\Seen"})
        r.on()
        real, asked = engines.Direct.chat, []

        def chat(link, task, *a, **kw):
            if task == "learn-orgs":
                asked.append(a[0][1]["content"])
                if len(asked) % 2 == 0:
                    raise engines.Busy("功耗间歇", retry_after=1, code="busy")
            return real(link, task, *a, **kw)
        with mock.patch.object(engines.Direct, "chat", chat):
            r.run()
        self.assertEqual(len(asked), 3)                         # batch 1, batch 2 refused, batch 2 again
        self.assertEqual(asked[1], asked[2])
        self.assertNotEqual(asked[0], asked[2])
        self.assertTrue(r.app.learner.state(ME).get("received_at"))


class CleanUpTests(unittest.TestCase):
    """The owner, 2026-10-16: a clean-up must not take what the memory could learn with it -- 了解你 never reads the
    trash, and the provider empties it. Read first (先学后清), and keep the clean-up itself as a record."""

    def rig(self):
        r = LearnRig(self)
        for i in range(3):          # an organisation, months apart, read, older than the sorting window
            r.srv._add("INBOX", mail(500 + i, f"保单通知 {i}", "某保险 <notice@insure.example>", 40 + 40 * i), {"\\Seen"})
        for i in range(3):          # a shop's promotions
            r.srv._add("INBOX", mail(510 + i, f"大促 {i}", "某商城 <promo@shop9.example>", 40 + 30 * i), set())
        return r

    def clean(self, r):
        from test_old_mail import wait
        assert wait(r.app, r.app.start_old_scan({"source": "mailbox", "scope": "inbox"}))["status"] == "done"
        r.app.store.old_judge(ME, {**{f"<o{500 + i}@x.example>": "notification" for i in range(3)},
                                   **{f"<o{510 + i}@x.example>": "promotion" for i in range(3)}}, "m")
        keys = [g["key"] for g in r.app.old_view(ME, "inbox")["groups"] if g["suggested"]]
        job = wait(r.app, r.app.start_old_apply({"source": "mailbox", "scope": "inbox", "keys": keys}))
        assert job["status"] == "done", job
        return job["result"]

    def asked(self, r):
        return [b["messages"][1]["content"] for b in r.chat.requests if b["messages"][0]["content"].startswith("下面是用户收到")]

    def test_what_goes_to_the_trash_is_read_first_and_the_clean_up_is_recorded(self):
        r = self.rig()
        r.on()
        r.app.store.cache_update(ME, "learn", received_at=time.time())          # 了解你 looked at received mail lately
        self.clean(r)
        self.assertNotIn("保单通知 0", r.srv.subjects("INBOX"))                   # cleared to the trash
        self.assertTrue(any("某保险" in a for a in self.asked(r)))               # but read first
        self.assertFalse(any("某商城" in a for a in self.asked(r)))              # (a shop's promotions are not an organisation)
        self.assertIn("在某保险有账户", [c["text"] for c in r.app.store.learn_candidates(ME, state="written")]
                      + [n["text"] for n in r.app.mem.notes(kinds=["fact"], status=["proposed"])])
        cleared = {x["ref"]: x for x in r.app.mem.records(topic="mail-cleared")}
        self.assertEqual(set(cleared), {"from:notice@insure.example", "from:promo@shop9.example"})
        shop = cleared["from:promo@shop9.example"]
        self.assertEqual((shop["text"], shop["kind"], shop["speaker"]), ("清掉了「某商城」的 3 封广告促销", "receipt", "user"))
        self.assertEqual({k: shop["data"][k] for k in ("count", "action", "by", "category")},
                         {"count": 3, "action": "trash", "by": "suggestion", "category": "广告促销"})
        self.assertNotIn("大促", json.dumps(list(cleared.values()), ensure_ascii=False))   # no subjects

    def test_with_learning_paused_nothing_is_read_but_the_clean_up_is_recorded(self):
        """了解你 paused: the model does not read the organisations first; what the user cleared is recorded anyway
        (ADR 0013: records are always written; the switch is only about reading mail to learn)."""
        r = self.rig()
        r.app.cfg["learn"]["on"] = False
        self.clean(r)
        self.assertEqual(self.asked(r), [])
        self.assertTrue(r.app.mem.records(topic="mail-cleared"))


class PersonSwitchTests(unittest.TestCase):
    """了解你 is about the person: one switch for every connected mailbox (2026-10-14)."""

    def test_one_switch_for_every_mailbox_and_those_added_later(self):
        r = LearnRig(self)
        r.on()
        r.app.cfg["accounts"].append({"provider": "gmail", "address": "other@example.com", "host": None, "port": 993})
        self.assertEqual(r.app.learner._next("other@example.com", r.app.learner.state("other@example.com")), "sent_check")
        self.assertTrue(r.app.learner.view("other@example.com")["enabled"])
        r.app.learner.set_on({"source": "mailbox", "on": False})          # paused: for all of them
        self.assertIsNone(r.app.learner._next("other@example.com", {}))
        self.assertFalse(r.app.learner.switch(samples_demo()).get("on"))   # the demo keeps its own

    def test_switched_on_in_one_mailbox_before_is_on_for_all(self):
        r = LearnRig(self)
        r.app.cfg["learn"] = {"on": False, "enabled_at": None}
        r.app.store.cache_update(ME, "learn", on=True, enabled_at=1000.0)  # 0.11: per mailbox
        r.app.cfg["accounts"].append({"provider": "gmail", "address": "other@example.com", "host": None, "port": 993})
        r.app.learner.migrate()
        self.assertEqual(r.app.cfg["learn"], {"on": True, "enabled_at": 1000.0})
        self.assertEqual(r.app.learner._next("other@example.com", {}), "sent_check")

    def test_organisations_are_looked_at_again_only_when_new(self):
        r = LearnRig(self)
        r.send(sent(1, "friend@mail.example", "周末", "周末见。", 2))
        for k in range(2):
            for i in range(3):
                r.srv._add("INBOX", mail(300 + 10 * k + i, f"账单 {i}", f"机构{k} <bill@org{k}.example>", 20 + 40 * i), {"\\Seen"})
        r.on()
        r.run()
        asked = lambda: [b for b in r.chat.requests if b["messages"][0]["content"].startswith("下面是用户收到")]  # noqa: E731
        first = len(asked())
        self.assertEqual(first, 1)
        r.app.store.cache_update(ME, "learn", received_at=0)                # two weeks later
        r.run()
        self.assertEqual(len(asked()), first)                               # nothing new: not asked again
        for i in range(3):
            r.srv._add("INBOX", mail(330 + i, f"账单 {i}", "机构9 <bill@org9.example>", 20 + 40 * i), {"\\Seen"})
        r.app.store.cache_update(ME, "learn", received_at=0)
        r.run()
        last = asked()[-1]["messages"][1]["content"]
        self.assertIn("org9", last)
        self.assertNotIn("org0", last)


def samples_demo():
    return "demo"


class HabitTests(unittest.TestCase):
    def test_habits_are_the_memorys_to_work_out_from_what_the_user_changed(self):
        """luoke-mem 0.18 works habits out of mail-override itself: 了解你 no longer guesses them."""
        r = LearnRig(self)
        r.srv._add("INBOX", mail(500, "双十一促销", "某某商城 <news@shop.example>", 1), set())
        r.srv._add("INBOX", mail(501, "会员日促销", "某某商城 <news@shop.example>", 2), set())
        r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        r.on()
        ids = [it["id"] for it in r.app.store.items(ME) if "促销" in (it["subject"] or "")]
        for i in ids:                                                              # changed twice, no rule made
            r.app.override({"source": "mailbox", "ids": [i], "action": "archive"})
        changed = r.app.mem.records(topic="mail-override")                       # what the memory learns from
        self.assertEqual([(c["data"]["to"], c["data"]["name"]) for c in changed], [("archive", "某某商城")] * 2)
        r.run()
        self.assertEqual(r.app.store.learn_candidates(ME, "habit"), [])
        self.assertFalse(any(n.get("tool") == memory.TOOL for n in r.app.mem.notes(kinds=["preference"], status=["active", "proposed", "observed"])))


class ElsewhereTests(unittest.TestCase):
    """The owner, 2026-10-19: what the user does to their mail themselves -- here or in Gmail, on the phone -- is
    what habits are learnt from, one sender's one kind of mail at a time; what the mail assistant did is not."""

    def sync(self, r):
        r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})

    def acted(self, r):
        return sorted((x["data"]["action"], x["data"]["category"], x["data"]["where"], x["data"]["count"])
                      for x in r.app.mem.records(topic="mail-acted"))

    def test_archived_deleted_or_read_in_gmail_is_the_users_doing(self):
        r = LearnRig(self)
        for n, subject in enumerate(["春节促销", "会员日促销", "周末促销"]):
            r.srv._add("INBOX", mail(510 + n, subject, "某某商城 <news@shop.example>", 1 + n), set())
        r.on()
        r.app.cfg["flow"]["mark_read"] = False                           # sorted mail left unread: read in Gmail counts
        self.sync(r)
        self.assertEqual(self.acted(r), [])                              # the first look only notes what is unread
        r.srv._gmail_move("INBOX", r.srv.find("INBOX", "春节"), "[Gmail]/All Mail", False)
        r.srv._gmail_move("INBOX", r.srv.find("INBOX", "会员日"), "[Gmail]/Trash", False)
        r.srv.find("INBOX", "周末")["flags"].add("\\Seen")
        self.sync(r)
        self.assertEqual(self.acted(r), [("archive", "promotion", "elsewhere", 1), ("read", "promotion", "elsewhere", 1),
                                         ("trash", "promotion", "elsewhere", 1)])
        rec = r.app.mem.records(topic="mail-acted")[0]
        self.assertTrue(memory.from_account(rec, ME) and rec["data"]["by"] == "user" and rec["data"]["name"] == "某某商城")
        self.sync(r)
        self.assertEqual(len(self.acted(r)), 3)                          # once each

    def test_sorted_mail_is_marked_read_and_that_is_not_the_user_reading_it(self):
        """ADR 0016 (2026-10-19): what the model sorted is marked read; the user reviews it in 今日处理."""
        r = LearnRig(self)
        r.srv._add("INBOX", mail(540, "年中促销", "某某商城 <news@shop.example>", 1), set())
        r.on()
        self.sync(r)
        self.assertIn("\\Seen", r.srv.find("INBOX", "年中")["flags"])
        it = next(i for i in r.app.store.items(ME) if "年中" in (i["subject"] or ""))
        self.assertTrue(it["read_by_us"] and it["state"] == "pending")         # still on the list, to be reviewed
        self.sync(r)
        self.assertEqual(self.acted(r), [])                                     # never the user's doing
        self.assertIn(it["message_id"], r.app.store.read_by_us(ME))

    def test_what_the_mail_assistant_did_is_not_taken_for_the_users(self):
        r = LearnRig(self)
        r.srv._add("INBOX", mail(520, "清仓促销", "某某商城 <news@shop.example>", 1), set())
        r.on()
        self.sync(r)
        r.app.cfg["mode"]["write"] = True
        it = next(i for i in r.app.store.items(ME) if "清仓" in (i["subject"] or ""))
        r.app.items_act("mailbox", [it["id"]], "archive")                # clicked here: the user's, "here"
        self.sync(r)
        self.assertEqual(self.acted(r), [("archive", "promotion", "here", 1)])

    def test_recorded_with_learning_paused_too(self):
        r = LearnRig(self)
        r.app.cfg["learn"]["on"] = False
        r.srv._add("INBOX", mail(530, "限时促销", "某某商城 <news@shop.example>", 1), set())
        self.sync(r)
        r.srv._gmail_move("INBOX", r.srv.find("INBOX", "限时"), "[Gmail]/All Mail", False)
        self.sync(r)
        self.assertEqual(self.acted(r), [("archive", "promotion", "elsewhere", 1)])


class SwitchTests(unittest.TestCase):
    def test_on_by_default_pause_and_demo(self):
        r = LearnRig(self)
        r.app.idle_step = lambda **k: None
        self.assertEqual(r.app.learner.next_account(), ME)                          # on from the start (ADR 0013)
        r.app.learner.set_on({"source": "mailbox", "on": False})
        self.assertIsNone(r.app.learner.next_account())
        r.app.learner.set_on({"source": "mailbox", "on": True})                     # 继续
        self.assertEqual(r.app.learner.next_account(), ME)
        r.app.learner.set_on({"source": "mailbox", "on": False})
        r.on("demo")
        r.run("demo")
        self.assertIn("职位：产品经理", [n["text"] for n in r.app.demo_mem.notes(kinds=["fact"])])
        self.assertEqual(r.app.mem.notes(status=[]), [])                           # the demo taught the real memory nothing


class PowerTests(unittest.TestCase):
    def tearDown(self):
        engines.set_power("full")

    def test_every_call_waits_a_multiple_of_the_last(self):
        def slow(body):
            time.sleep(0.3)
            return 200, json.dumps({"ok": True})
        with FakeChat(slow) as chat:
            call = lambda: engines.chat_json(chat.base_url, "m", [{"role": "user", "content": "x"}], {"type": "object"})  # noqa: E731
            engines.set_power("balanced")
            call()
            t = time.monotonic()
            call()
            self.assertGreaterEqual(time.monotonic() - t, 0.55)                    # waited ~0.3 s, then ran ~0.3 s
            engines.set_power("full")                                              # back to full: no waiting
            t = time.monotonic()
            call()
            self.assertLess(time.monotonic() - t, 0.5)

    def test_the_setting(self):
        r = DigestRig(self)
        self.assertEqual(r.app.state()["power"], {"level": "full", "factor": 0.0})
        self.assertEqual(r.app.save_power({"level": "quiet"}), {"level": "quiet", "factor": 3.0})
        self.assertEqual(engines._pace["factor"], 3.0)
        with self.assertRaises(ValueError):
            r.app.save_power({"level": "turbo"})


if __name__ == "__main__":
    unittest.main()


GUARD_400 = {"error": {"message": "oMLX prefill memory guard rejected this prompt: Prefill would require ~75.99 GB peak "
                                  "but dynamic ceiling is 71.33 GB."}}
FIT_507 = {"error": {"message": "Model 'big' (72.65GB) does not fit under the dynamic memory ceiling (66.61GB)."}}


class ModelServiceTests(unittest.TestCase):
    """What oMLX actually answered on a full machine (2026-10-06): the page must say memory, not "can't connect"."""

    def tearDown(self):
        engines._pace.update(full_until=0.0, full_wait=0.0)                    # the back-off is global

    def call(self, reply):
        with FakeChat(reply) as chat:
            engines.chat_json(chat.base_url, "m", [{"role": "user", "content": "x"}], {"type": "object"})
            return chat

    def test_memory_guard_is_memory_full_and_not_retried_simpler(self):
        sent_ = []
        with self.assertRaises(engines.MemoryFull) as ctx:
            self.call(lambda b: (sent_.append(b), (400, GUARD_400["error"]))[1])
        self.assertTrue(ctx.exception.prompt_too_big)
        self.assertIn("内存不足", str(ctx.exception))
        self.assertEqual(len(sent_), 1)                                            # a simpler request would not fit either
        with self.assertRaises(engines.MemoryFull) as ctx:
            self.call(lambda b: (507, FIT_507["error"]))
        self.assertFalse(ctx.exception.prompt_too_big)

    def test_an_answer_without_a_completion_is_not_a_connection_problem(self):
        orig = engines._request
        engines._request = lambda *a, **k: {"error": "something went wrong"}           # 200, but no completion
        try:
            with self.assertRaises(engines.EngineError) as ctx:
                engines.chat_json("http://127.0.0.1:9/v1", "m", [{"role": "user", "content": "x"}], {"type": "object"})
        finally:
            engines._request = orig
        self.assertNotIn("无法连接", str(ctx.exception))

    def test_a_newsletter_too_long_for_the_memory_left_is_read_shorter(self):
        from luoke_mail import digest
        mail_ = {"subject": "早报", "from_addr": "a@b.example", "text": "本地大模型新进展。" * 1000,
                 "links": [{"n": 1, "text": "x", "url": "https://b.example/1", "host": "b.example"}]}

        def guard(body):
            if len(body["messages"][1]["content"]) > 6000:
                return 400, GUARD_400["error"]
            return 200, json.dumps({"items": [{"title": "本地大模型新进展", "score": 3, "lasting": True, "link": 1, "topics": [],
                                               "summary": "摘要", "why": "相关"}]}, ensure_ascii=False)
        with FakeChat(guard) as chat:
            items = digest.extract(mail_, "", [], [], __import__("datetime").date(2026, 10, 6), engines.Direct(chat.base_url, "m"))
        self.assertEqual([i["title"] for i in items], ["本地大模型新进展"])


class TurnTests(unittest.TestCase):
    def test_idle_work_takes_turns_and_the_users_request_goes_first(self):
        r = LearnRig(self)
        started = []
        r.app._judge_next = lambda: (ME, "archive")
        r.app.curator.next_account = lambda: ME
        r.app.learner.next_account = lambda: ME
        r.app.profiler.next_account = lambda: None
        r.app._start_job = lambda kind, fn, **extra: started.append(kind)
        for _ in range(4):
            r.app.idle_step()
        self.assertEqual(started, ["judge", "read", "learn", "judge"])            # a long queue cannot starve the others
        r.app.idle_step(prefer="learn")
        self.assertEqual(started[-1], "learn")


    def test_the_users_job_starts_at_once_and_waits_for_idle_work_to_finish_its_step(self):
        import threading
        import time as time_
        r = LearnRig(self)
        release, ran = threading.Event(), []

        def idle(job):                                       # a model call that takes a while
            release.wait(10)
            return {}
        r.app._start_job("read", idle, account=ME)
        t0 = time_.monotonic()
        job = r.app._start_job("sync", lambda job: ran.append(job["stage"]) or {})
        self.assertLess(time_.monotonic() - t0, 1)                                 # the page gets an answer at once
        self.assertEqual(job["stage"], r.app.WAITING)
        self.assertEqual(r.app.running_job()["id"], job["id"])                     # 进行中 shows the user's job
        with self.assertRaises(ValueError):
            r.app._start_job("learn", lambda job: {}, account=ME)                 # no idle work behind it
        self.assertEqual(ran, [])
        release.set()
        for _ in range(100):
            if r.app.jobs[job["id"]]["status"] != "running":
                break
            time_.sleep(0.05)
        self.assertEqual((r.app.jobs[job["id"]]["status"], ran), ("done", ["准备中"]))


class BackoffTests(unittest.TestCase):
    def tearDown(self):
        engines._pace.update(full_until=0.0, full_wait=0.0)

    def test_when_the_model_does_not_fit_idle_work_waits_longer_each_time(self):
        with FakeChat(lambda b: (507, FIT_507["error"])) as chat:
            for want in (120, 240, 480):
                with self.assertRaises(engines.MemoryFull):
                    engines.chat_json(chat.base_url, "m", [{"role": "user", "content": "x"}], {"type": "object"})
                self.assertAlmostEqual(engines.full_for(), want, delta=2)
        r = LearnRig(self)
        started = []
        r.app._judge_next = lambda: (ME, "archive")
        r.app._start_job = lambda kind, fn, **extra: started.append(kind)
        self.assertFalse(r.app.idle_pending())
        r.app.idle_step()
        self.assertEqual(started, [])                                              # nobody asks the model to load again
        r.app.idle_step(prefer="judge")                                            # unless the user just asked
        self.assertEqual(started, ["judge"])
        with FakeChat(lambda b: (200, json.dumps({"ok": 1}))) as chat:
            engines._pace["full_until"] = 0.0
            engines.chat_json(chat.base_url, "m", [{"role": "user", "content": "x"}], {"type": "object"})
        self.assertEqual(engines._pace["full_wait"], 0.0)                         # it fits again: back to normal
