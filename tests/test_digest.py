"""订阅精选 and the memory: newsletters are read piece by piece against what the user said they care about;
what holds a lasting pick is kept (starred) when 清理 trashes the rest; 有用 / 没兴趣 and interests live in
luoke-mem as the user's own words, guesses wait for a yes, and the demo never teaches the real memory."""
import datetime as dt
import json
import os
import plistlib
import shutil
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from email.message import EmailMessage
from email.utils import formatdate

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import actions, config, digest, editor, engines, memory, profile, samples, server  # noqa: E402
from luoke_mail.credstore import FileStore  # noqa: E402
from luoke_mail.store import Store  # noqa: E402
from fakes import FakeChat  # noqa: E402
from test_background import FakeDesktop, private_config, triage_reply  # noqa: E402
from test_old_mail import ME, Rig, wait  # noqa: E402

AI = "AI Weekly <digest@aiweekly.example>"
GYM = "云端健身 <news@gymcloud.example>"


def newsletter(n, sender, pieces, days_ago, list_id="weekly.aiweekly.example"):
    m = EmailMessage()
    m["From"] = sender
    m["To"] = ME
    m["Subject"] = f"Weekly #{n}"
    m["Date"] = formatdate(dt.datetime.now().timestamp() - days_ago * 86400 - 3600, localtime=True)
    m["Message-ID"] = f"<nl{n}@aiweekly.example>"
    m["List-Id"] = f"<{list_id}>"
    m["List-Unsubscribe"] = "<https://aiweekly.example/unsubscribe>"
    html = ["<p><a href='https://aiweekly.example/web'>在浏览器中查看</a></p>"]
    html += [f"<h2><a href='https://aiweekly.example/{i}'>{t}</a></h2><p>{t}的详细介绍。</p>" for i, t in enumerate(pieces)]
    html.append("<p><a href='https://aiweekly.example/unsubscribe'>退订</a></p>")
    m.set_content("".join(html), subtype="html")
    return m.as_bytes()


def model(body):
    """A fake local model for every prompt this feature sends."""
    system, user = body["messages"][0]["content"], body["messages"][-1]["content"]
    if system.startswith("你在替用户读订阅邮件"):
        links = re.findall(r"^〔(\d+)〕(.+)（[^（]+）$", body["messages"][1]["content"], re.M)
        items = [{"title": t, "score": 3 if "本地" in t else 0, "lasting": "本地" in t, "link": int(n), "topics": ["AI"],
                  "summary": f"{t}。" if "本地" in t else "", "why": "和本地大模型有关" if "本地" in t else ""} for n, t in links
                 if "试用" not in t]
        return 200, json.dumps({"items": items}, ensure_ascii=False)
    if system.startswith("你是用户的订阅编辑"):            # the editor: what is about local models first
        k = int(re.search(r"最多 (\d+) 条", system).group(1))
        lines = re.findall(r"^\[(\d+)\] (.+)$", user, re.M)
        ranked = sorted(lines, key=lambda x: "本地" not in x[1])[:k]
        return 200, json.dumps({"picks": [{"n": int(n), "why": "和本地大模型有关" if "本地" in t else "值得一读"} for n, t in ranked]},
                               ensure_ascii=False)
    if system.startswith("根据下面关于用户的资料"):           # 关注的话题 from the user's own things
        lines = re.findall(r"^\[(\d+)\] (.+)$", user, re.M)
        local = [int(n) for n, t in lines if "本地" in t or "Weekly" in t]
        gym = [int(n) for n, t in lines if "健身" in t]
        existing = re.search(r"^已有的话题：(.*)$", user, re.M)
        out = {"interests": [{"text": "本地大模型", "evidence": local[:4], "why": "网站和阅读记录"}]
               if not (existing and "本地大模型" in existing.group(1)) else [],
               "dislikes": [{"text": "健身团课促销", "evidence": gym[:3], "why": "收到很多从不打开"}] if len(gym) >= 2 else []}
        return 200, json.dumps(out, ensure_ascii=False)
    if "kind" in system and "清理" in system:
        lines = [ln for ln in user.splitlines() if ln[:1].isdigit()]
        return 200, json.dumps({"items": [{"n": i, "kind": "newsletter" if "Weekly" in ln else "other"}
                                          for i, ln in enumerate(lines, 1)]}, ensure_ascii=False)
    return triage_reply(body)


class DigestRig(Rig):
    def __init__(self, test, **kw):
        super().__init__(test, **kw)
        home = tempfile.mkdtemp()
        self.app.mem = memory.Memory(os.path.join(home, "mem.db"), tool=memory.TOOL)
        self.app.demo_mem = memory.Memory(os.path.join(home, "demo-mem.db"), tool=memory.TOOL)
        test.addCleanup(self.app.mem.close)
        test.addCleanup(self.app.demo_mem.close)
        self.chat = FakeChat(model).__enter__()
        test.addCleanup(self.chat.__exit__)
        self.app.cfg["engine"]["base_url"] = self.chat.base_url

    def archive(self, *mails):
        for raw in mails:
            self.srv._add(self.archive_folder(), raw, {"\\Seen"})

    def archive_folder(self):
        return "[Gmail]/All Mail" if self.srv.flavour == "gmail" else "Archive"

    def read_all(self, acct=ME):
        busy = self.app.running_job()                 # read_from_clean may have started a reading slice already
        if busy:
            wait(self.app, busy)
        for _ in range(20):
            if not self.app.store.digest_mail_counts(acct).get("queued"):
                return
            self.app.curator.read_slice(acct, {"cancel": False})
            problem = (self.app.store.cache_get(acct, "read")[0] or {}).get("problem")
            assert not problem, problem
        raise AssertionError("reading made no progress")

    def clean(self, scope):
        assert wait(self.app, self.app.start_old_scan({"source": "mailbox", "scope": scope}))["status"] == "done"
        self.app.store.cache_set(ME, f"judge:{scope}", {"on": True})
        self.app._judge_slice(ME, scope, {"cancel": False})
        return self.app.old_view(ME, scope)

    def prompts(self, start):
        return [b["messages"][1]["content"] for b in self.chat.requests if b["messages"][0]["content"].startswith(start)]


class ReadingTests(unittest.TestCase):
    def test_newsletter_text_numbers_links_and_drops_the_chrome(self):
        r = digest.read(newsletter(1, AI, ["本地推理提速", "融资快讯"], 3))
        self.assertIn("本地推理提速〔1〕", r["text"])
        self.assertEqual([(l["n"], l["host"]) for l in r["links"]], [(1, "aiweekly.example"), (2, "aiweekly.example")])
        self.assertFalse(any("unsubscribe" in l["url"] or l["url"].endswith("/web") for l in r["links"]))
        plain = EmailMessage()
        plain["Subject"], plain["Message-ID"] = "plain", "<p@x>"
        plain.set_content("本周推荐：一篇长文 https://blog.example/post\n退订 https://x.example/unsubscribe")
        r = digest.read(plain.as_bytes())
        self.assertIn("〔1〕", r["text"])
        self.assertEqual([l["url"] for l in r["links"]], ["https://blog.example/post"])

    def test_parse_clamps_and_maps_links(self):
        links = [{"n": 1, "url": "https://a.example/x", "host": "a.example", "text": "x"}]
        out = digest.parse(json.dumps({"items": [{"title": "A", "score": 9, "lasting": True, "link": 1, "topics": ["t"], "summary": "s", "why": "w"},
                                                 {"title": "B", "score": "x", "lasting": False, "link": 7, "topics": "t", "summary": "", "why": "w"},
                                                 {"title": "", "score": 3}]}), links)
        self.assertEqual([(i["title"], i["score"], i["url"], i["why"]) for i in out],
                         [("A", 3, "https://a.example/x", "w"), ("B", 0, None, "")])
        with self.assertRaises(ValueError):
            digest.parse("都挺好", links)

    def test_thousands_of_pieces_stay_quick(self):
        """A real backlog gave 13,468 pieces, 5,748 of them scored 2+: the page took 73 s (2026-10-08)."""
        import hashlib
        import time as _t
        items = [{"title": hashlib.sha1(str(i).encode()).hexdigest()[:16], "score": 2 + i % 2, "lasting": True,
                  "feedback": None, "date": f"2025-{i % 12 + 1:02d}-01"} for i in range(6000)]
        story = {"title": "本地大模型推理提速一倍", "score": 3, "lasting": True, "feedback": None, "date": "2026-01-01"}
        items += [story] + [{**story, "date": "2024-01-01"}] * 3                  # the same story three more times
        t0 = _t.time()
        out = digest.picks(items, lasting_only=True, limit=60)
        self.assertLess(_t.time() - t0, 1.0)
        self.assertEqual(len(out), 60)
        self.assertEqual(out[0]["title"], story["title"])
        self.assertEqual(out[0]["also"], 3)

    def test_picks_best_first_one_story_once(self):
        it = lambda t, s, lasting=True, fb=None, d="2026-10-01": {"title": t, "score": s, "lasting": lasting, "feedback": fb, "date": d}  # noqa: E731
        items = [it("本地推理提速一倍的方法", 2), it("本地推理提速一倍的方法（转载）", 3, d="2026-10-02"), it("融资快讯", 3, lasting=False),
                 it("无关", 1), it("不想看", 3, fb="dismissed"), it("我点过有用", 0, fb="useful")]
        got = digest.picks(items)
        self.assertEqual([(i["title"], i["also"]) for i in got],
                         [("我点过有用", 0), ("本地推理提速一倍的方法（转载）", 1), ("融资快讯", 0)])
        self.assertNotIn("融资快讯", [i["title"] for i in digest.picks(items, lasting_only=True)])


class BacklogTests(unittest.TestCase):
    def setUp(self):
        self.r = DigestRig(self)
        self.r.archive(newsletter(1, AI, ["本地大模型部署指南", "融资快讯"], 90),
                       newsletter(2, AI, ["融资快讯", "活动报名"], 60),
                       newsletter(3, AI, ["本地大模型部署指南（续）"], 40))

    def key(self, view):
        return next(g["key"] for g in view["groups"] if g["kind"] == "newsletter")

    def test_read_before_clean_keeps_and_stars_what_holds_a_pick(self):
        r = self.r
        v = r.clean("archive")
        key = self.key(v)
        v = r.app.curator.read_from_clean({"source": "mailbox", "scope": "archive", "keys": [key]})
        self.assertEqual(next(g for g in v["groups"] if g["key"] == key)["digest"], {"queued": 3, "read": 0, "kept": 0})
        self.assertEqual([s["key"] for s in r.app.store.digest_sources(ME)], ["list:weekly.aiweekly.example"])
        r.read_all()
        g = next(g for g in r.app.old_view(ME, "archive")["groups"] if g["key"] == key)
        self.assertEqual(g["digest"], {"queued": 0, "read": 3, "kept": 2})
        view = r.app.curator.view(ME)
        earlier = [i for m in view["months"] for i in m["items"]]
        self.assertEqual([i["title"] for i in earlier], ["本地大模型部署指南（续）", "本地大模型部署指南"])   # month by month
        self.assertTrue(all(m["pending"] for m in view["months"]))                      # the editor has not chosen yet
        self.assertTrue(earlier[0]["mail_url"].startswith("https://mail.google.com/"))
        self.assertEqual(earlier[0]["url"], "https://aiweekly.example/0")
        # 清理: the one without a pick goes to the trash, the two with one stay where they are, starred
        res = wait(r.app, r.app.start_old_apply({"source": "mailbox", "scope": "archive", "keys": [key]}))["result"]
        self.assertIn("2 封含精选内容，已保留并加星标", res["message"])
        self.assertEqual(r.srv.subjects("[Gmail]/Trash"), ["Weekly #2"])
        starred = sorted(m["mid"] for m in r.srv.folders["[Gmail]/All Mail"] if "\\Flagged" in m["flags"])
        self.assertEqual(starred, ["<nl1@aiweekly.example>", "<nl3@aiweekly.example>"])
        self.assertEqual(r.app.old_view(ME, "archive")["flagged"], 2)
        # undo: back from the trash, stars off
        r.app.actions("mailbox", write=False).undo(res["log_id"])
        self.assertEqual(r.srv.subjects("[Gmail]/Trash"), [])
        self.assertFalse(any("\\Flagged" in m["flags"] for m in r.srv.folders["[Gmail]/All Mail"]))
        self.assertEqual(r.app.old_view(ME, "archive")["flagged"], 0)

    def test_the_reader_reads_by_message_id_and_never_marks_read(self):
        r = self.r
        key = self.key(r.clean("archive"))
        r.app.idle_step = lambda **k: None                                          # read below, after the UIDs went stale
        r.app.curator.read_from_clean({"source": "mailbox", "scope": "archive", "keys": [key]})
        r.app.store.db.execute("UPDATE digest_mail SET uid='999'")              # stale UIDs: found by Message-ID
        r.read_all()
        self.assertEqual(r.app.store.digest_mail_counts(ME), {"read": 3})
        self.assertNotIn("STORE", " ".join(r.srv.commands))

    def test_interests_and_feedback_reach_the_model(self):
        r = self.r
        r.app.mem.state("interest", "本地大模型")
        r.app.mem.state("dislike", "融资新闻")
        key = self.key(r.clean("archive"))
        r.app.curator.read_from_clean({"source": "mailbox", "scope": "archive", "keys": [key]})
        r.read_all()
        first = r.prompts("你在替用户读订阅邮件")[0]
        self.assertIn("关注的话题：\n- 本地大模型", first)
        self.assertIn("不感兴趣：\n- 融资新闻", first)
        item = r.app.curator.view(ME)["months"][0]["items"][0]
        r.app.curator.feedback({"source": "mailbox", "id": item["id"], "value": "useful"})
        rec = r.app.mem.records(topic="digest-feedback")[0]
        self.assertEqual((rec["speaker"], rec["kind"], rec["data"]["value"]), ("user", "statement", "useful"))
        self.assertEqual(r.app.curator.taste(ME)[1], [item["title"]])
        r.app.curator.opened({"source": "mailbox", "id": item["id"]})
        self.assertEqual(r.app.mem.records(topic="digest-open")[0]["kind"], "activity")     # attention, not a view
        r.app.curator.feedback({"source": "mailbox", "id": item["id"], "value": None})    # taken back
        self.assertEqual(r.app.curator.taste(ME)[1], [])
        self.assertEqual(len(r.app.mem.records(topic="digest-feedback")), 2)              # both kept, the newest counts

    def test_model_off_waits_and_reading_yields(self):
        r = self.r
        key = self.key(r.clean("archive"))
        r.app.cfg["engine"]["base_url"] = "http://127.0.0.1:9/v1"
        r.app.curator.read_from_clean({"source": "mailbox", "scope": "archive", "keys": [key]})
        r.app.curator.read_slice(ME, {"cancel": False})
        self.assertEqual(r.app.store.digest_mail_counts(ME), {"queued": 3})
        self.assertTrue(r.app.curator.view(ME)["reading"]["problem"])
        self.assertIsNone(r.app.curator.next_account())                                    # tries again a minute later
        r.app.profiler.run(ME, {"cancel": False})                                            # the weekly records: no model
        r.app.cfg["learn"]["on"] = False
        self.assertFalse(r.app.idle_pending())


class OtherMailboxTests(unittest.TestCase):
    """Old inbox newsletters on QQ (no MOVE, no extensions) and NetEase (UTF-7 folders, ID first): the one with a
    pick is starred in place, the rest go to the trash, and undo takes the star off and brings the rest back."""

    def check(self, flavour, provider, trash):
        r = DigestRig(self, flavour=flavour, provider=provider)
        for n, pieces, days in ((31, ["本地大模型部署指南"], 60), (32, ["融资快讯"], 70)):
            r.srv._add("INBOX", newsletter(n, AI, pieces, days), {"\\Seen"})
        v = r.clean("inbox")
        key = next(g["key"] for g in v["groups"] if g["kind"] == "newsletter")
        r.app.curator.read_from_clean({"source": "mailbox", "scope": "inbox", "keys": [key]})
        r.read_all()
        res = wait(r.app, r.app.start_old_apply({"source": "mailbox", "scope": "inbox", "keys": [key]}))["result"]
        self.assertIn("1 封含精选内容", res["message"])
        self.assertEqual(r.srv.subjects(trash), ["Weekly #32"])
        kept = next(m for m in r.srv.folders["INBOX"] if m["mid"] == "<nl31@aiweekly.example>")
        self.assertIn("\\Flagged", kept["flags"])
        r.app.actions("mailbox", write=False).undo(res["log_id"])
        self.assertNotIn("\\Flagged", kept["flags"])
        self.assertIn("Weekly #32", r.srv.subjects("INBOX"))
        self.assertEqual(r.srv.violations, [])

    def test_qq(self):
        self.check("qq", "qq", "Deleted Messages")

    def test_netease(self):
        self.check("netease", "163", "&XfJSIJZk-")


class WeeklyTests(unittest.TestCase):
    def test_new_mail_from_a_source_lands_in_this_weeks_digest_and_one_note(self):
        r = DigestRig(self)
        r.srv._add("INBOX", newsletter(9, AI, ["本地推理新技巧", "融资快讯"], 0), set())
        r.app.store.digest_add_source(ME, "list:weekly.aiweekly.example", "AI Weekly", "user")
        r.app.sync_mailbox(ME, {"cancel": False, "stage": "", "done": 0, "total": 0})
        triage_prompt = next(p for p in r.prompts("你是一个帮用户整理收件箱的助手") if "Weekly #9" in p)
        self.assertIn("已加入用户的订阅精选", triage_prompt)
        self.assertEqual(r.app.store.digest_mail_counts(ME), {"queued": 1})
        r.read_all()
        self.assertEqual([i["title"] for i in r.app.curator.view(ME)["week"]], ["本地推理新技巧"])
        r.app.curator.now = lambda acct: dt.datetime.combine(dt.date.today(), dt.time(23))
        self.assertEqual(r.app.curator.weekly_note(ME), ("本周订阅精选：1 条", "「本地推理新技巧」"))
        self.assertIsNone(r.app.curator.weekly_note(ME))                                   # once a week

    def test_questions_the_memory_has_are_mentioned_once_a_week_and_answered_on_the_memory_page(self):
        r = DigestRig(self)
        rid = r.app.mem.record("user", "activity", "把两封「某银行」的邮件从「归档」改为「待跟进」", topic="mail-override", ref="1")
        nid = r.app.mem.propose("preference", "某银行的邮件要跟进", [rid])
        whole = memory.luoke_mem.WholeMemory(r.app.mem.path, regrade=False)
        self.addCleanup(whole.close)
        whole.ask("question", "「某银行」的邮件都要跟进吗？", "你改过两次", about_note=nid, about_kind="preference",
                  answer={"type": "choice", "options": ["对", "不对", "以后再说"]})
        r.app.curator.now = lambda acct: dt.datetime.combine(dt.date.today(), dt.time(23))
        self.assertEqual(r.app.curator.weekly_note(ME), ("有 1 个问题想问你", "在「记忆」中回答。"))
        self.assertIsNone(r.app.curator.weekly_note(ME))
        self.assertEqual(r.app.activity()["guesses"], 1)                                   # the 记忆 badge
        ask = r.app.curator.memory_view("mailbox")["asks"][0]
        page = r.app.curator.memory_change({"source": "mailbox", "op": "reply", "id": ask["id"], "choice": "yes"})
        self.assertEqual(page["asks"], [])
        self.assertEqual([n["text"] for n in page["preference"]], ["某银行的邮件要跟进"])          # the memory settled it
        with self.assertRaises(ValueError):
            r.app.curator.memory_change({"source": "mailbox", "op": "reply", "id": ask["id"], "choice": "yes"})

    def test_the_answers_are_the_ones_the_memory_offers(self):
        r = DigestRig(self)

        class Mem:
            def __init__(self, asks):
                self._asks = asks

            def asks(self, tool):
                return self._asks
        four = {"id": 1, "kind": "question", "about_note": 3, "text": "t",
                "answer": {"type": "choice", "options": ["对", "不对", "以前是，现在不是", "以后再说"]}}
        odd = {**four, "id": 2, "answer": {"type": "choice", "options": ["是", "否"]}}
        got = r.app.curator.asks(Mem([four, odd]))
        self.assertEqual([o["choice"] for o in got[0]["options"]], ["yes", "no", "past", "later"])   # luoke-mem 0.20
        self.assertEqual([o["choice"] for o in got[1]["options"]], ["yes", "no", "later"])          # words not known


class MemoryTests(unittest.TestCase):
    def test_memory_page_changes_and_forgetting(self):
        r = DigestRig(self)
        page = r.app.curator.memory_change({"source": "mailbox", "op": "add", "kind": "fact", "text": "我是产品经理"})
        nid = page["fact"][0]["id"]
        page = r.app.curator.memory_change({"source": "mailbox", "op": "revise", "id": nid, "text": "我是产品负责人"})
        self.assertEqual([n["text"] for n in page["fact"]], ["我是产品负责人"])
        with self.assertRaises(ValueError):
            r.app.curator.memory_change({"source": "mailbox", "op": "confirm", "id": nid})  # superseded already
        page = r.app.curator.memory_change({"source": "mailbox", "op": "forget", "id": page["fact"][0]["id"]})
        self.assertEqual(page["fact"], [])
        self.assertEqual(r.app.mem.records(), [])                                          # the evidence went too

    def test_the_memory_is_the_users_only_the_mail_assistants_part_is_forgotten_here(self):
        r = DigestRig(self)
        luoke_ime = memory.Memory(r.app.mem.path, tool="luoke-ime")             # another tool, the same file
        self.addCleanup(luoke_ime.close)
        typed = luoke_ime.record("user", "statement", "上屏：我最近在研究本地大模型", topic="ime-commit")
        ime_interest = luoke_ime.conclude("interest", "本地大模型", [typed])
        luoke_ime.confirm(ime_interest)                    # in effect: the 记忆 page lists it (guesses only on luoke-mem's)
        r.app.curator.memory_change({"source": "mailbox", "op": "add", "kind": "fact", "text": "我是产品经理"})
        wrote = r.app.mem.record("user", "statement", "发出的邮件：本周周报", topic="sent-mail")
        r.app.mem.conclude("fact", "职位：产品负责人", [wrote])                     # learnt from the user's mail
        seen = r.app.mem.record("user", "activity", "打开过：本地大模型周报", topic="mail-read")
        r.app.mem.add_cites(ime_interest, [seen])                               # both tools rest on it
        view = r.app.curator.memory_view("mailbox")
        note = next(n for n in view["interest"] if n["text"] == "本地大模型")
        self.assertEqual(note["origin_text"], "来自螺壳输入法，已确认")
        self.assertEqual([c["text"] for c in note["cites"]], ["打开过：本地大模型周报"])   # only the mail assistant's text
        self.assertEqual(note["others"], [{"source": "螺壳输入法", "count": 1}])
        with self.assertRaises(ValueError):                                     # the whole memory: not from here
            r.app.curator.memory_change({"source": "mailbox", "op": "forget_all"})
        page = r.app.curator.memory_change({"source": "mailbox", "op": "forget_mine"})
        self.assertEqual([n["text"] for n in page["fact"]], ["我是产品经理"])   # what the user said stays (ADR 0009);
        self.assertEqual([n["text"] for n in page["interest"] + page["proposed"]], ["本地大模型"])  # what was learnt from mail went; the
        self.assertTrue(luoke_ime.records(topic="ime-commit"))                  # input method's stays
        wrote = r.app.mem.record("user", "statement", "发出的邮件：本周周报", topic="sent-mail")
        r.app.mem.conclude("fact", "职位：产品负责人", [wrote])
        r.app.forget({})                                                        # 全部清除, not ticked: memory kept
        self.assertEqual(len(r.app.mem.notes(kinds=["fact"], status=["active", "proposed", "observed"])), 2)
        r.app.forget({"memory": True})                                          # ticked: the mail assistant's part
        self.assertEqual([n["text"] for n in r.app.mem.notes(kinds=["fact"])], ["我是产品经理"])
        self.assertEqual([n["text"] for n in r.app.mem.notes(kinds=["interest"], status=["active", "proposed", "observed"])], ["本地大模型"])

    def test_what_the_user_said_is_theirs_whichever_page_it_was_said_on(self):
        """ADR 0009: the user's words about understandings belong to the memory (luoke-mem), even when said on the
        mail assistant's page; the page still calls them 「你填写的」 and forgetting the mail assistant keeps them."""
        r = DigestRig(self)
        mem_own = memory.Memory(r.app.mem.path, tool="luoke-mem")              # what luoke-mem keeps in its own name
        self.addCleanup(mem_own.close)
        mem_own.state("fact", "我住在杭州")
        learnt = mem_own.record("user", "statement", "上屏：周末去爬山", topic="ime-commit")
        mem_own.confirm(mem_own.conclude("interest", "户外徒步", [learnt]))
        page = r.app.curator.memory_view("mailbox")
        said = next(n for n in page["fact"] if n["text"] == "我住在杭州")
        self.assertEqual((said["origin_text"], said["others"]), ("你填写的", []))
        self.assertEqual(next(n for n in page["interest"] if n["text"] == "户外徒步")["origin_text"], "来自螺壳记忆，已确认")
        page = r.app.curator.memory_change({"source": "mailbox", "op": "forget_mine"})
        self.assertEqual([n["text"] for n in page["fact"]], ["我住在杭州"])

    def test_the_whole_memory_opens_in_luoke_mems_own_page(self):
        import types
        from unittest import mock
        r = DigestRig(self)
        real = memory.luoke_mem.__dict__.pop("ui", None)                       # as with luoke-mem before 0.6
        self.addCleanup(lambda: real and setattr(memory.luoke_mem, "ui", real))
        with mock.patch.dict(sys.modules, {"luoke_mem.ui": None}):
            self.assertFalse(r.app.curator.memory_view("mailbox")["whole"])      # no entry
        asked = []

        def ensure(path=None):
            asked.append(path)
            if len(asked) > 1:
                raise RuntimeError("did not start in time")
            return "http://127.0.0.1:9/?t=x"
        fake = types.ModuleType("luoke_mem.ui")
        fake.ensure = ensure
        with mock.patch.dict(sys.modules, {"luoke_mem.ui": fake}):
            self.assertTrue(r.app.curator.memory_view("mailbox")["whole"])
            self.assertFalse(r.app.curator.memory_view("demo")["whole"])        # the demo's memory: not there
            self.assertEqual(r.app.curator.open_whole({"source": "mailbox"}), {"url": "http://127.0.0.1:9/?t=x"})
            self.assertEqual(asked, [r.app.mem.path])                           # the file luoke-mail uses (a copy here)
            with self.assertRaises(ValueError):                                 # did not come up: said in plain words
                r.app.curator.open_whole({"source": "mailbox"})
            with self.assertRaises(ValueError):
                r.app.curator.open_whole({"source": "demo"})
        memory.luoke_mem.__dict__.pop("ui", None)                                # the fake must not stay behind

    def test_the_app_opens_the_memory_the_user_installed(self):
        """In 螺壳邮件助手.app on the user's own memory, 「查看整份记忆」 is 螺壳记忆 itself, as from the input method
        and the agent -- not the page of the library the app carries (boundary audit, 2026-10-16). From beta1 it is
        part of 螺壳主控 (luoke://memory, ADR 0003 amended); with a 主控 from before, the separate app (open -b)."""
        from unittest import mock
        r = DigestRig(self)
        r.app.hub_only = True
        home = tempfile.mkdtemp()
        opened, urls = [], []
        system, mine = os.path.join(home, "sys-Applications"), os.path.join(home, "Applications")
        with mock.patch.dict(os.environ, {"HOME": home}), \
                mock.patch.object(memory, "hub_roots", lambda: (system, mine)), \
                mock.patch.object(memory.luoke_mem, "default_path", lambda: r.app.mem.path), \
                mock.patch.object(memory, "memory_app", lambda: None), \
                mock.patch("luoke_mail.desktop.open_app", opened.append), \
                mock.patch("luoke_mail.desktop.open_url", urls.append):
            self.assertFalse(r.app.curator.memory_view("mailbox")["whole"])          # neither: no entry
            with mock.patch.object(memory, "memory_app", lambda: "/Users/x/Applications/螺壳记忆.app"):
                self.assertTrue(r.app.curator.memory_view("mailbox")["whole"])
                self.assertEqual(r.app.curator.open_whole({"source": "mailbox"}), {"opened": True})
                self.assertEqual(opened, ["ai.luoke.memory"])                        # a 主控 from before beta1
            # 主控: renamed 螺壳 (10-23), dragged into /Applications (luoke-core#30, 10-24); known by its bundle id
            def install(root, name, bundle="ai.luoke.core"):
                app = os.path.join(root, name)
                os.makedirs(os.path.join(app, "Contents", "Resources", "memory"))
                with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
                    plistlib.dump({"CFBundleIdentifier": bundle}, f)
                open(os.path.join(app, "Contents", "Resources", "memory", "luoke-accessory.json"), "w").close()
                return app
            for root in (system, mine):
                for name in ("螺壳.app", "螺壳主控.app"):
                    app = install(root, name)
                    self.assertEqual(memory.hub_app(), app)
                    self.assertTrue(r.app.curator.memory_view("mailbox")["whole"], app)
                    self.assertEqual(r.app.curator.open_whole({"source": "mailbox"}), {"opened": True})
                    self.assertEqual(urls[-1], "luoke://memory")
                    shutil.rmtree(app)
            install(system, "螺壳.app", bundle="com.example.other")                 # another app of that name
            self.assertIsNone(memory.hub_app())
            self.assertFalse(r.app.curator.memory_view("demo")["whole"])

    def test_a_code_in_a_title_is_masked_and_the_rest_kept(self):
        """ADR 0013: credentials are a security matter -- hidden where they stand; nothing else is held back."""
        r = DigestRig(self)
        add_pieces(r.app, 1, "2026-05", local=1)
        it = r.app.store.digest_candidates(ME)[0]
        r.app.store.digest_set(it["id"], title="您的验证码是 482913，请勿告诉他人")
        r.app.curator.feedback({"source": "mailbox", "id": it["id"], "value": "useful"})
        r.app.curator.opened({"source": "mailbox", "id": it["id"]})
        recs = r.app.mem.records()
        self.assertTrue(recs and all("请勿告诉他人" in x["text"] for x in recs))
        self.assertFalse(any("482913" in x["text"] + json.dumps(x["data"], ensure_ascii=False) for x in recs))

    def test_a_card_number_the_user_types_is_masked(self):
        r = DigestRig(self)
        page = r.app.curator.memory_change({"source": "mailbox", "op": "add", "kind": "fact", "text": "我的银行卡号是 4111 1111 1111 1111"})
        self.assertTrue(page["fact"] and "4111 1111" not in page["fact"][0]["text"] and "银行卡号" in page["fact"][0]["text"])
        page = r.app.curator.memory_change({"source": "mailbox", "op": "add", "kind": "fact", "text": "我在一家软件公司做产品"})
        self.assertIn("我在一家软件公司做产品", [n["text"] for n in page["fact"]])
        self.assertEqual(page["message"], "")

    def test_a_note_rewritten_with_health_words_is_marked_and_still_shown_here(self):
        """ADR 0013: no category is hidden from a tool on the user's own machine; it is only marked."""
        r = DigestRig(self)
        page = r.app.curator.memory_change({"source": "mailbox", "op": "add", "kind": "fact", "text": "我每周跑步三次"})
        page = r.app.curator.memory_change({"source": "mailbox", "op": "revise", "id": page["fact"][0]["id"],
                                            "text": "我每周跑步三次，膝盖受过伤在做康复治疗"})
        self.assertEqual([n["text"] for n in page["fact"]], ["我每周跑步三次，膝盖受过伤在做康复治疗"])
        self.assertEqual(page["message"], "")
        db = sqlite3.connect(r.app.mem.path)
        self.addCleanup(db.close)
        row = db.execute("SELECT status, sensitive FROM notes WHERE text LIKE '%康复%'").fetchone()
        self.assertEqual(row[0], "active")                         # the user's own words, only marked
        self.assertIn("health", row[1])
        page = r.app.curator.memory_change({"source": "mailbox", "op": "add", "kind": "fact", "text": "我的验证码是 482913"})
        self.assertFalse(any("482913" in n["text"] for n in page["fact"]))      # masked, not refused

    def test_other_tools_kinds_are_shown_here_too(self):
        """ADR 0013: the 记忆 page shows every kind, the input method's 常用说法 included."""
        r = DigestRig(self)
        luoke_ime = memory.Memory(r.app.mem.path, tool="luoke-ime")             # another tool, the same file
        self.addCleanup(luoke_ime.close)
        luoke_ime.state("term", "螺壳")
        self.assertEqual([n["text"] for n in r.app.curator.memory_view("mailbox")["term"]], ["螺壳"])

    def test_demo_has_its_own_memory(self):
        r = DigestRig(self)
        memory.seed_demo(r.app.demo_mem)
        demo = r.app.curator.memory_view("demo")
        self.assertTrue(demo["interest"])
        self.assertEqual([(a["text"], [o["label"] for o in a["options"]]) for a in demo["asks"]],
                         [("你关注「开源社区和项目维护」吗？", ["对", "不对", "以后再说"])])
        r.app.curator.memory_change({"source": "demo", "op": "add", "kind": "interest", "text": "只在示例里"})
        self.assertEqual(r.app.mem.notes(status=[]), [])
        page = r.app.curator.memory_change({"source": "demo", "op": "forget_mine"})    # 清除示例记忆: all of it,
        self.assertNotIn("只在示例里", [n["text"] for n in page["interest"]])         # what was added there too
        self.assertTrue(page["interest"] and page["asks"])                            # and the sample again

    def test_which_memory_file(self):
        old, home = config.CONFIG_DIR, os.environ.pop("LUOKE_HOME", None)
        self.addCleanup(setattr, config, "CONFIG_DIR", old)
        if home:
            self.addCleanup(os.environ.__setitem__, "LUOKE_HOME", home)
        config.CONFIG_DIR = memory.DEFAULT_HOME            # the real folder, named by the login item: shared memory
        self.assertEqual(memory.path(), memory.luoke_mem.default_path())
        self.assertEqual(memory.path(demo=True), os.path.join(memory.DEFAULT_HOME, "demo-mem.db"))
        config.CONFIG_DIR = "/tmp/a-copy"                   # a copy: its own memory
        self.assertEqual(memory.path(), "/tmp/a-copy/mem.db")

    def test_about_me_moves_into_memory_and_reaches_triage(self):
        private_config(self)
        with open(config.CONFIG_PATH, "w") as f:
            json.dump({"profile": {"about_me": "我负责 B 端客户", "signature": "小周"}}, f)
        home = tempfile.mkdtemp()
        mem = memory.Memory(os.path.join(home, "mem.db"), tool=memory.TOOL)
        self.addCleanup(mem.close)
        app = server.App(secret_store=FileStore(os.path.join(home, "c.json")), state_store=Store(os.path.join(home, "s.db")),
                         desktop_=FakeDesktop(), memory_=mem, demo_memory_=memory.Memory(os.path.join(home, "d.db")))
        self.addCleanup(app.store.close)
        self.assertEqual([n["text"] for n in mem.notes(kinds=["fact"])], ["我负责 B 端客户"])
        self.assertEqual(config.load()["profile"]["about_me"], "")
        said = mem.note(mem.notes()[0]["id"])
        self.assertEqual((said["origin"], said["tool"]), ("user", "luoke-mem"))      # the user's words: the memory's
        prompt = server.triage.build_messages({"subject": "x"}, memory.about(mem))[1]["content"]
        self.assertIn("关于用户：\n- 我负责 B 端客户", prompt)


class SubscriptionTests(unittest.TestCase):
    def test_join_the_digest_from_subscription_health_and_undo(self):
        r = DigestRig(self)
        res = wait(r.app, r.app.start_subscriptions({"source": "demo", "days": 60}))["result"]
        sub = next(s for s in res["senders"] if s["from_addr"] == "digest@aiweekly.example")
        out = r.app.sender_act({"source": "demo", "key": sub["key"], "op": "digest"})
        self.assertIn("加入精选来源", out["message"])
        self.assertEqual([s["key"] for s in r.app.store.digest_sources("demo")], [sub["key"]])
        r.app.actions("demo", write=False).undo(out["log_id"])
        self.assertEqual(r.app.store.digest_sources("demo"), [])


class DemoTests(unittest.TestCase):
    def test_demo_newsletters_are_read_and_picked(self):
        r = DigestRig(self)
        assert wait(r.app, r.app.start_old_scan({"source": "demo", "scope": "archive"}))["status"] == "done"
        r.app.store.cache_set("demo", "judge:archive", {"on": True})
        r.app.store.old_judge("demo", {row["message_id"]: "newsletter" for row in r.app.store.old_rows("demo", "archive")
                                       if row["from_addr"] == "noreply@medium.example"}, "test")
        key = next(g["key"] for g in r.app.old_view("demo", "archive")["groups"] if g["kind"] == "newsletter")
        r.app.curator.read_from_clean({"source": "demo", "scope": "archive", "keys": [key]})
        r.read_all("demo")
        titles = [i["title"] for m in r.app.curator.view("demo")["months"] for i in m["items"]]
        self.assertIn("在本地部署大模型：从零到可用的完整指南", titles)
        self.assertEqual(r.app.mem.records(), [])                                          # nothing reached the real memory


def add_pieces(app, n, month, local=0, start=0, score=2):
    """`n` candidates in `month` (YYYY-MM), the first `local` of them about local models."""
    for i in range(n):
        words = "".join(chr(0x4e00 + ((start + i) * 131 + j * 977) % 20000) for j in range(8))   # one story each
        title = ("本地" if i < local else "其他") + words
        app.store.db.execute(
            "INSERT INTO digest_items (account, message_id, title, summary, url, host, topics_json, lasting, score, why, date,"
            " source, run, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ME, f"<p{month}-{start + i}@x>", title, "", None, None, "[]", 1, score, "", f"{month}-{10 + i % 15:02d} 08:00", "AI Weekly",
             "backlog", "2026-10-01 08:00:00"))
    app.store.db.commit()


class EditorAnswerTests(unittest.TestCase):
    """At most k: fewer is the editor leaving out what the user is not interested in, not a failed answer."""

    class Link:
        def __init__(self, answer):
            self.answer = answer

        def chat(self, task, messages, schema, items=None, max_tokens=None):
            return self.answer, None, None

    def test_fewer_picks_are_kept_none_fall_back(self):
        from luoke_mail import editor
        cands = [{"title": f"第 {i} 条", "score": 3 - i % 3, "date": "2026-10-01"} for i in range(8)]
        got = editor.choose(cands, 5, "", [], [], self.Link('{"picks": [{"n": 2, "why": "有分量"}]}'))
        self.assertEqual([(it["title"], why) for it, why in got], [("第 1 条", "有分量")])
        self.assertEqual(len(editor.choose(cands, 5, "", [], [], self.Link('{"picks": []}'))), 5)     # the reader's ranking


class EditorTests(unittest.TestCase):
    """The editor chooses the few worth reading: about WEEK_QUOTA for the last seven days, MONTH_QUOTA a month."""

    def test_rounds_of_batches_end_with_the_quota(self):
        with FakeChat(model) as chat:
            cands = [{"id": i, "title": f"本地大模型 {i}" if i % 10 == 0 else f"其他 {i}", "summary": "", "score": 2, "date": "2026-05-01"}
                     for i in range(100)]
            got = editor.edit(cands, editor.MONTH_QUOTA, "", [], [], engines.Direct(chat.base_url, "m"))
            self.assertEqual(len(got), editor.MONTH_QUOTA)
            self.assertEqual({it["id"] for it, _ in got if "本地" in it["title"]}, set(range(0, 100, 10)))  # none lost to a strong batch
            calls = [b for b in chat.requests if b["messages"][0]["content"].startswith("你是用户的订阅编辑")]
            self.assertTrue(all(len(re.findall(r"^\[\d+\]", b["messages"][1]["content"], re.M)) <= editor.BATCH for b in calls))

    def test_an_unusable_answer_falls_back_to_the_readers_ranking(self):
        with FakeChat(lambda body: (200, "不是 JSON")) as chat:
            cands = [{"id": i, "title": f"t{i}", "score": 3 if i == 7 else 2, "date": "2026-05-01"} for i in range(12)]
            got = editor.choose(cands, 3, "", [], [], engines.Direct(chat.base_url, "m"))
        self.assertEqual(len(got), 3)
        self.assertEqual(got[0][0]["id"], 7)

    def test_months_are_chosen_once_read_and_only_picks_are_kept(self):
        r = DigestRig(self)
        add_pieces(r.app, 25, "2026-05", local=4)
        add_pieces(r.app, 3, "2026-04", local=1, start=100)
        self.assertEqual(r.app.curator.edit_pending(ME), [("month", "2026-05"), ("month", "2026-04")])
        kept = r.app.curator.kept_mids(ME)
        self.assertEqual(len(kept), 28)                                         # nothing judged yet: all kept for now
        r.app.curator.edit_slice(ME, {"cancel": False})
        self.assertEqual(r.app.curator.edit_pending(ME), [])
        may = next(m for m in r.app.curator.view(ME)["months"] if m["month"] == "2026-05")
        self.assertFalse(may["pending"])
        self.assertEqual(len(may["items"]), editor.MONTH_QUOTA)
        self.assertEqual(sum(it["title"].startswith("本地") for it in may["items"]), 4)       # none of them lost
        self.assertEqual(may["items"][0]["pick_why"] or "", "和本地大模型有关" if "本地" in may["items"][0]["title"] else "值得一读")
        self.assertEqual(len(r.app.curator.kept_mids(ME)), editor.MONTH_QUOTA + 3)   # the rest of May can go
        # a newsletter still to be read in a month: that month waits for it
        r.app.store.digest_queue(ME, [{"message_id": "<late@x>", "source_key": "k", "name": "n", "folder": "INBOX", "uid": "1",
                                       "subject": "s", "date": "2026-05-30 08:00"}], "backlog")
        add_pieces(r.app, 1, "2026-05", local=1, start=50)
        self.assertEqual(r.app.curator.edit_pending(ME), [])
        self.assertEqual(r.app.curator.edit_pending(ME, waiting=True), [("month", "2026-05")])
        self.assertTrue(next(m for m in r.app.curator.view(ME)["months"] if m["month"] == "2026-05")["pending"])

    def test_choosing_stops_between_batches_when_asked(self):
        with FakeChat(model) as chat:
            cands = [{"id": i, "title": f"其他 {i}", "score": 2, "date": "2026-05-01"} for i in range(100)]
            asked = []
            got = editor.edit(cands, editor.MONTH_QUOTA, "", [], [], engines.Direct(chat.base_url, "m"), stop=lambda: asked.append(1) or len(asked) > 2)
            self.assertIsNone(got)
            self.assertEqual(len([b for b in chat.requests if b["messages"][0]["content"].startswith("你是用户的订阅编辑")]), 2)

    def test_saving_keeps_a_piece_and_archiving_puts_it_aside(self):
        r = DigestRig(self)
        add_pieces(r.app, 3, "2026-05", local=3)
        r.app.curator.edit_slice(ME, {"cancel": False})
        may = lambda: next(m for m in r.app.curator.view(ME)["months"] if m["month"] == "2026-05")["items"]  # noqa: E731
        first, second = may()[0], may()[1]
        r.app.curator.feedback({"source": "mailbox", "id": first["id"], "value": "useful"})        # 收藏
        self.assertTrue(r.app.mem.records(topic="digest-feedback")[0]["text"].startswith("收藏了「"))
        self.assertEqual([i["id"] for i in r.app.curator.view(ME)["saved"]], [first["id"]])
        records = len(r.app.mem.records())
        out = r.app.curator.archive({"source": "mailbox", "id": second["id"], "on": True})        # 归档
        self.assertTrue(out["archived"])
        view = r.app.curator.view(ME)
        self.assertNotIn(second["id"], [i["id"] for i in may()])                                  # off the list
        self.assertEqual([i["id"] for i in view["archived"]], [second["id"]])
        self.assertEqual(len(r.app.mem.records()), records)                                       # not a judgement
        self.assertEqual(r.app.curator.edit_pending(ME), [])                                      # nothing chosen again
        self.assertIn(f"<p2026-05-{second['id'] - 1}@x>", r.app.curator.kept_mids(ME))           # still held in 清理
        r.app.curator.archive({"source": "mailbox", "id": second["id"], "on": False})             # 移回精选
        self.assertIn(second["id"], [i["id"] for i in may()])
        r.app.curator.feedback({"source": "mailbox", "id": first["id"], "value": None})
        self.assertTrue(r.app.mem.records(topic="digest-feedback")[0]["text"].startswith("取消收藏「"))
        self.assertEqual(r.app.curator.view(ME)["saved"], [])

    def test_a_08_database_gains_the_pick_columns(self):
        import sqlite3
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE digest_items (id INTEGER PRIMARY KEY, account TEXT NOT NULL, message_id TEXT NOT NULL, title TEXT,"
                   " summary TEXT, url TEXT, host TEXT, topics_json TEXT, lasting INTEGER, score INTEGER, why TEXT, date TEXT,"
                   " source TEXT, run TEXT, feedback TEXT, opened INTEGER DEFAULT 0, created_at TEXT)")
        db.execute("INSERT INTO digest_items (account, message_id, title, lasting, score, date) VALUES ('a', '<m>', 't', 1, 2, '2026-05-01')")
        db.commit()
        db.close()
        st = Store(path)
        self.addCleanup(st.close)
        self.assertEqual([(i["title"], i["pick_month"], i["archived_at"]) for i in st.digest_candidates("a", lasting=True)],
                         [("t", 0, None)])

    def test_this_weeks_picks_and_choosing_again_when_interests_change(self):
        r = DigestRig(self)
        add_pieces(r.app, 30, dt.date.today().strftime("%Y-%m"), local=3)
        r.app.store.db.execute("UPDATE digest_items SET date=?", (dt.date.today().isoformat() + " 08:00",))
        r.app.store.db.commit()
        self.assertEqual(r.app.curator.edit_pending(ME), [("week", None)])
        r.app.curator.edit_slice(ME, {"cancel": False})
        view = r.app.curator.view(ME)
        self.assertEqual((len(view["week"]), view["week_pending"]), (editor.WEEK_QUOTA, False))
        r.app.curator.memory_change({"source": "mailbox", "op": "add", "kind": "interest", "text": "AI 编程工具"})
        self.assertEqual(r.app.curator.edit_pending(ME), [("week", None)])          # chosen again with the new interest
        self.assertIn("AI 编程工具", r.app.curator.taste(ME)[0])


SITE = (b'<html><head><title>\xe5\xb0\x8f\xe5\x91\xa8\xe7\x9a\x84\xe5\xae\x9e\xe9\xaa\x8c\xe5\xae\xa4</title>'
        b'<meta name="description" content="notes"><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head>'
        b'<body><a href="/about">About this lab</a></body></html>')
FEED = ('<?xml version="1.0"?><rss><channel><item><title>本地大模型推理提速</title><link>https://lab.example/posts/1</link>'
        '<description>&lt;p&gt;一些笔记&lt;/p&gt;</description><pubDate>Tue, 14 Oct 2025 08:00:00 +0900</pubDate></item>'
        '<item><title>三值量化实测</title><link>https://lab.example/posts/2</link></item></channel></rss>').encode()


def site_get(url):
    if url.endswith("/feed.xml"):
        return FEED, "utf-8"
    if url.startswith("https://lab.example"):
        return SITE, "utf-8"
    raise OSError("unreachable")


class ProfileTests(unittest.TestCase):
    """The user's own things -- their site, labels, the threads they started -- kept as records for the memory,
    which works out 关注的话题 from them (luoke-mem 0.19)."""

    def rig(self, mail=True):
        r = DigestRig(self)
        r.app.idle_step = lambda **k: None                                     # run by hand below
        r.app.profiler.fetch = lambda url: profile.fetch_site(url, get=site_get)
        if mail:
            for i in range(5):
                r.srv._add("INBOX", newsletter(20 + i, AI, ["本地大模型"], 5 + i), {"\\Seen"})
            for n, sender, lid in ((40, GYM, "news.gymcloud.example"), (50, "健身优选 <promo@fit.example>", "promo.fit.example")):
                for i in range(6):
                    r.srv._add("INBOX", newsletter(n + i, sender, ["团课"], 3 + i, list_id=lid), set())
        return r

    def test_reading_a_site_feed_or_front_page(self):
        info = profile.fetch_site("https://lab.example/", get=site_get)
        self.assertEqual((info["title"], info["about"]), ("小周的实验室", "notes"))
        self.assertEqual([(p["title"], p["summary"]) for p in info["posts"]], [("本地大模型推理提速", "一些笔记"), ("三值量化实测", "")])
        bare = profile.fetch_site("https://lab.example/", get=lambda u: (SITE.replace(b"/feed.xml", b"/none"), "utf-8")
                                  if u == "https://lab.example/" else (b"<html>404</html>", "utf-8"))
        self.assertEqual([p["title"] for p in bare["posts"]], ["About this lab"])            # no feed: the links on the page
        with self.assertRaises(ValueError):
            profile.fetch_site("file:///etc/passwd", get=site_get)

    def learning(self, r, on=True):
        r.app.cfg["learn"].update(on=on, enabled_at=1 if on else None)

    def sent(self, r, subjects):
        for n, subject in enumerate(subjects):
            m = EmailMessage()
            m["From"], m["To"], m["Subject"] = ME, "a@corp.example", subject
            m["Date"], m["Message-ID"] = formatdate(localtime=True), f"<s{n}@x>"
            m.set_content("x")
            r.srv._add("[Gmail]/Sent Mail", m.as_bytes(), {"\\Seen"})

    def test_the_users_own_things_are_kept_as_records_and_nothing_is_guessed_here(self):
        """The memory works out the topics (luoke-mem 0.19): the mail assistant only writes down the user's site,
        labels and the threads they started, once each, with no model call."""
        r = self.rig()
        self.learning(r)
        self.sent(r, ["本地大模型分享会安排", "Re: 本地大模型分享会安排", "回复：周报"])
        r.app.store.old_sync(ME, "archive", [{"uid": 1, "message_id": "<l1@x>", "subject": "信用卡账单", "labels": ["财务"],
                                              "from_addr": "bank@x.example", "date": "2026-09-01"}])
        page = r.app.profiler.change_site({"source": "mailbox", "op": "add", "url": "lab.example"})
        self.assertEqual([(s["url"], s["title"], s["posts"]) for s in page["sites"]], [("https://lab.example", "小周的实验室", 2)])
        self.assertEqual(page["message"], "已读取「小周的实验室」，找到 2 篇文章。")
        self.assertEqual(r.app.profiler.due(ME), "first")
        made = r.app.profiler.run(ME, {"cancel": False})["made"]
        site = [x["text"] for x in r.app.mem.records(topic="own-site")]
        self.assertIn("你的网站上的文章：「本地大模型推理提速」｜一些笔记", site)
        sent = r.app.mem.records(topic="sent-mail")
        self.assertEqual([x["text"] for x in sent], ["你发起的邮件：「本地大模型分享会安排」"])     # not the replies
        self.assertTrue(memory.from_account(sent[0], ME) and sent[0]["data"]["by"] == "user")
        label = r.app.mem.records(topic="own-label")
        self.assertEqual([(x["text"], x["data"]["count"]) for x in label], [("标签「财务」：信用卡账单", 1)])
        self.assertEqual(label[0]["happened_at"][:10], "2026-09-01")         # its newest mail, not when written
        post = next(x for x in r.app.mem.records(topic="own-site") if "推理提速" in x["text"])
        self.assertEqual(post["happened_at"][:10], "2025-10-14")             # the feed's date
        self.assertEqual(r.prompts("根据下面关于用户的资料"), [])                                   # no model call
        self.assertEqual(r.app.mem.notes(kinds=["interest", "dislike"], status=["active", "proposed", "observed"]), [])
        self.assertIsNone(r.app.profiler.due(ME))
        r.app.store.cache_update(ME, "profile", at=time.time() - 8 * 86400)
        self.assertEqual(r.app.profiler.due(ME), "weekly")
        self.assertEqual(r.app.profiler.run(ME, {"cancel": False})["made"], 0)                  # written once
        self.assertGreaterEqual(made, 3)

    def test_records_written_with_the_wrong_time_are_dated_again(self):
        """0.14.0 and before dated labels by when they were written; written once more, the new way, and the old
        record's time put right where the memory allows it."""
        r = self.rig(mail=False)
        self.learning(r)
        r.app.store.old_sync(ME, "archive", [{"uid": 1, "message_id": "<l1@x>", "subject": "学费", "labels": ["学业"],
                                              "from_addr": "u@x.example", "date": "2016-05-02 10:00"}])
        old = r.app.mem.record("user", "statement", "你在邮箱里建的标签「学业」", topic="own-label", ref="学业",
                               data=memory.sourced(ME, {"by": "user"}))
        r.app.store.cache_update(ME, "profile", at=time.time())                # run before, the old way
        self.assertEqual(r.app.profiler.due(ME), "again")
        moved = []
        r.app.mem.redate = lambda rid, when: moved.append((rid, when))         # what luoke-mem would offer
        r.app.profiler.run(ME, {"cancel": False})
        self.assertEqual(moved, [(old, "2016-05-02 10:00")])
        self.assertIsNone(r.app.profiler.due(ME))

    def test_written_with_learning_paused_too(self):
        """ADR 0013: the user's own things are recorded whether 了解你 is on or paused."""
        r = self.rig()
        self.learning(r, on=False)
        self.sent(r, ["本地大模型分享会安排"])
        self.assertEqual(r.app.profiler.due(ME), "first")
        r.app.profiler.run(ME, {"cancel": False})
        self.assertEqual([x["text"] for x in r.app.mem.records(topic="sent-mail")], ["你发起的邮件：「本地大模型分享会安排」"])

    def test_the_picks_follow_what_the_memory_works_out(self):
        r = self.rig(mail=False)
        self.assertFalse(r.app.curator.follow_taste(ME))                          # the first look: where things stand
        r.app.store.cache_set(ME, "editor", {**r.app.store.cache_get(ME, "editor")[0], "week": "1:1", "months": {"2026-05": 3}})
        self.assertFalse(r.app.curator.follow_taste(ME))
        r.app.mem.state("interest", "本地大模型")
        self.assertIn("本地大模型", r.app.curator.taste(ME)[0])
        self.assertTrue(r.app.curator.follow_taste(ME))
        st = r.app.store.cache_get(ME, "editor")[0]
        self.assertNotIn("week", st)
        self.assertEqual(st["months"], {})                                       # chosen again

    def test_sites_can_be_wrong_repeated_or_removed(self):
        r = self.rig(mail=False)
        for url, word in (("ftp://lab.example", "http"), ("https://gone.example", "无法打开")):
            with self.assertRaises(ValueError) as e:
                r.app.profiler.change_site({"source": "mailbox", "op": "add", "url": url})
            self.assertIn(word, str(e.exception))
        r.app.profiler.change_site({"source": "mailbox", "op": "add", "url": "https://lab.example"})
        with self.assertRaises(ValueError):
            r.app.profiler.change_site({"source": "mailbox", "op": "add", "url": "https://lab.example/"})
        site = r.app.profiler.sites(ME)[0]
        page = r.app.profiler.change_site({"source": "mailbox", "op": "remove", "id": site["id"]})
        self.assertEqual(page["sites"], [])
        self.assertEqual(r.app.mem.notes(kinds=["fact"]), [])

    def test_demo_guesses_stay_in_the_demo(self):
        r = self.rig(mail=False)
        r.app.store.digest_add_source("demo", "list:digest.medium.example", "Medium", "user")
        r.app.profiler.run("demo", {"cancel": False})
        self.assertEqual(r.app.mem.records(), [])


if __name__ == "__main__":
    unittest.main()


class PauseTests(unittest.TestCase):
    """暂停 while a slice is in the middle of a model call: the current item finishes, nothing after it starts,
    and the pause survives the slice (the background loop must not pick the work up again)."""

    def blocking(self, reply):
        import threading
        self.entered, self.release = threading.Event(), threading.Event()

        def model_(body):
            self.entered.set()
            self.release.wait(10)
            return reply(body)
        return model_

    def run_paused(self, app, start, pause):
        import threading
        t = threading.Thread(target=start)
        t.start()
        self.assertTrue(self.entered.wait(10))
        pause()
        self.release.set()
        t.join(20)
        self.assertFalse(t.is_alive())

    def test_pausing_reading(self):
        r = DigestRig(self)
        r.archive(*(newsletter(40 + i, AI, ["本地大模型部署指南"], 40 + i) for i in range(4)))
        key = next(g["key"] for g in r.clean("archive")["groups"] if g["kind"] == "newsletter")
        r.app.idle_step = lambda **k: None
        r.app.curator.read_from_clean({"source": "mailbox", "scope": "archive", "keys": [key]})
        with FakeChat(self.blocking(model)) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            job = {"cancel": False, "kind": "read", "account": ME, "status": "running"}
            r.app.jobs["slice"] = job
            self.run_paused(r.app, lambda: r.app.curator.read_slice(ME, job),
                            lambda: r.app.curator.set_reading({"source": "mailbox", "on": False}))
        self.assertEqual(r.app.store.digest_mail_counts(ME), {"read": 1, "queued": 3})
        self.assertFalse(r.app.curator.view(ME)["reading"]["on"])
        self.assertFalse(r.app.old_view(ME, "archive")["reading_on"])                     # 清理 says so too
        del r.app.idle_step
        self.assertIsNone(r.app.curator.next_account())
        r.app.curator.set_reading({"source": "mailbox", "on": True})                     # 继续
        self.assertTrue(r.app.curator.view(ME)["reading"]["on"])

    def test_pausing_judging(self):
        from test_archive import ArchiveRig, judge
        r = ArchiveRig(self)
        r.scan()
        r.app.store.cache_set(ME, "judge:archive", {"on": True})
        with FakeChat(self.blocking(judge)) as chat:
            r.app.cfg["engine"]["base_url"] = chat.base_url
            job = {"cancel": False, "kind": "judge", "account": ME, "scope": "archive", "status": "running"}
            r.app.jobs["slice"] = job
            self.run_paused(r.app, lambda: r.app._judge_slice(ME, "archive", job),
                            lambda: r.app.set_judging({"source": "mailbox", "scope": "archive", "on": False}))
        v = r.view()
        self.assertFalse(v["judging"]["on"])
        r.app.store.cache_update(ME, "profile", at=time.time(), records_v=2)   # the weekly records: written already
        r.app.cfg["learn"]["on"] = False                                     # 了解你 paused: only the work tested here
        self.assertFalse(r.app.idle_pending())
