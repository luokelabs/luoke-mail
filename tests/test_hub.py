"""Asking for a model through 螺壳主控 (luoke-core's task layer) instead of a model service directly: the
contract's request (who asks, which task, how urgent, how many items), the limits read from `GET /tasks`
(characters, batch size), every reason code told in a sentence, waiting out a busy hub, and the switch staying
with the model service until the user moves to the hub."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import __version__, engines, hub, learn, samples, triage  # noqa: E402
from fakes import GOOD, FakeHub  # noqa: E402
from luoke_mail import config, server  # noqa: E402
from luoke_mail.credstore import FileStore  # noqa: E402
from luoke_mail.store import Store  # noqa: E402
from test_background import FakeDesktop, new_job, private_config  # noqa: E402
from test_old_mail import ME, Rig  # noqa: E402

MAIL = samples.build()[0]["mail"]


def old_rows(n: int) -> list[dict]:
    return [{"message_id": f"<m{i}@x>", "from_name": "Shop", "from_addr": "news@shop.example", "date": "2025-01-01",
             "subject": f"Sale number {i}", "labels": [], "category": None} for i in range(n)]


def old_reply(task, body, req):
    return json.dumps({"items": [{"n": n, "kind": "promotion"} for n in range(1, body["luoke_items"] + 1)]})


def via_hub(app, path):
    app.cfg["engine"] = {"via": "hub", "hub_socket": path}


def no_real_hub(test):
    """A hub running on this machine (a released one, a lab build, LUOKE_HUB_SOCKET) must not be found here."""
    for name in ("DEFAULT_SOCKET", "LAB_SOCKET"):
        test.addCleanup(setattr, hub, name, getattr(hub, name))
        setattr(hub, name, "/tmp/no-such-luoke-hub.sock")
    env = os.environ.pop("LUOKE_HUB_SOCKET", None)
    if env:
        test.addCleanup(os.environ.__setitem__, "LUOKE_HUB_SOCKET", env)


class HubTests(unittest.TestCase):
    def setUp(self):
        hub._cache.clear()
        engines._later["until"] = 0.0
        self.addCleanup(engines._later.update, until=0.0)
        no_real_hub(self)

    def test_a_request_names_the_task_says_who_asks_and_leaves_the_model_to_the_hub(self):
        with FakeHub(lambda t, b, r: GOOD) as h:
            link = hub.Hub(h.path, "user-waiting")
            r = triage.analyze(MAIL, link, today=samples.TODAY)
        self.assertEqual(r["action"], "reply")
        req = h.requests[0]
        self.assertEqual((req["task"], req["priority"], req["accessory"]), ("triage", "user-waiting", f"luoke-mail/{__version__}"))
        self.assertEqual(sorted(req["body"]), ["luoke_items", "messages", "response_format", "stream"])  # no model, no max_tokens
        self.assertEqual(req["body"]["response_format"]["type"], "json_schema")
        self.assertEqual(link.name, "omlx-0.7.0+gemma-4-e4b-4bit")          # what answered is recorded

    def test_the_recipe_can_leave_out_the_verdict_on_the_sender(self):
        """A base that misreads the mailbox's verdict on the sender (the 9B on 16 GB, 2026-10-17) is not told it:
        its recipe says sender_verdict false, and the request is what it was before 0.13."""
        verified = {**MAIL, "from_addr": "ccsvc@message.cmbchina.com", "auth": "pass"}
        for verdict, told in ((None, True), (True, True), (False, False)):
            params = {"max_chars": 32000, "batch": 1, **({} if verdict is None else {"sender_verdict": verdict})}
            with FakeHub(lambda t, b, r: GOOD, tasks={"triage": {"status": "available", "params": params}}) as h:
                triage.analyze(verified, hub.Hub(h.path), today=samples.TODAY)
                asked = h.requests[0]["body"]["messages"][1]["content"]
            self.assertEqual("发件域名验证" in asked, told, verdict)
            if not told:
                self.assertEqual(h.requests[0]["body"]["messages"],
                                 triage.build_messages({**verified, "auth": None}, today=samples.TODAY))

    def test_a_long_mail_is_cut_to_what_one_request_may_carry(self):
        long = {**MAIL, "body": "这是一封很长的邮件。" * 2000}
        with FakeHub(lambda t, b, r: GOOD, max_chars=4000) as h:
            triage.analyze(long, hub.Hub(h.path), today=samples.TODAY)
            sent = h.requests[0]["body"]["messages"]
            self.assertLessEqual(sum(len(m["content"]) for m in sent), 4000)
            self.assertIn("…", sent[-1]["content"])
            self.assertEqual(engines.Direct("http://x", "m").fit("triage", lambda n: [{"content": "x" * n}], 99999), 99999)

    def test_batches_follow_the_hubs_batch_size_and_character_limit(self):
        with FakeHub(old_reply, tasks={"cleanup": {"status": "available", "params": {"max_chars": 32000, "batch": 10}}}) as h:
            kinds, count = triage.classify_old(old_rows(25), hub.Hub(h.path))
        self.assertEqual((count, len(kinds), h.requests[0]["body"]["luoke_items"]), (10, 10, 10))
        hub._cache.clear()
        with FakeHub(old_reply, tasks={"cleanup": {"status": "available", "params": {"max_chars": 1500, "batch": 25}}}) as h:
            kinds, count = triage.classify_old(old_rows(25), hub.Hub(h.path))
        self.assertLess(count, 25)
        self.assertEqual(len(kinds), count)
        self.assertLessEqual(sum(len(m["content"]) for m in h.requests[0]["body"]["messages"]), 1500)

    def test_limits_are_read_again_when_the_hub_says_its_tasks_changed(self):
        with FakeHub(old_reply, tasks={"cleanup": {"status": "available", "params": {"max_chars": 32000, "batch": 25}}}) as h:
            link = hub.Hub(h.path)
            self.assertEqual(link.limits("cleanup")["batch"], 25)
            h.tasks["cleanup"]["params"]["batch"] = 5                       # the recipe changed in the hub
            self.assertEqual(link.limits("cleanup")["batch"], 25)           # nothing said yet: the list read before
            h.version = "2"
            triage.classify_old(old_rows(3), link)                          # an answer says the list is a new one
            self.assertEqual(link.limits("cleanup")["batch"], 5)

    def test_an_unusable_answer_is_asked_again_within_the_limit(self):
        answers = iter(["不是 JSON " * 400, GOOD])
        with FakeHub(lambda t, b, r: next(answers), max_chars=4000) as h:
            r = triage.analyze(MAIL, hub.Hub(h.path), today=samples.TODAY)
        self.assertTrue(r["retried"])
        self.assertLessEqual(sum(len(m["content"]) for m in h.requests[1]["body"]["messages"]), 4000)

    def test_every_refusal_is_told_in_a_sentence(self):
        cases = {
            "task_unavailable": (hub.Refused, "螺壳暂时无法处理这项任务"),
            "unsupported": (hub.Refused, "不支持这项功能"),
            "engine_failed": (hub.Refused, "模型出错"),
            "needs_user": (engines.MemoryFull, "正在占用内存：LM Studio"),
            "busy": (engines.Busy, "模型正忙"),
            "paused": (engines.Busy, "已暂停后台任务"),
        }
        for code, (kind, words) in cases.items():
            with self.subTest(code=code):
                out = {"refuse": code, "retry_after": 5, "occupants": [{"pid": 1, "name": "LM Studio", "rss_gb": 20}]}
                with FakeHub(lambda t, b, r, o=out: o) as h:
                    with self.assertRaises(kind) as cm:
                        hub.Hub(h.path, "user-waiting").chat("triage", [{"role": "user", "content": "x"}], {})
                self.assertIn(words, str(cm.exception))
                self.assertNotIn(code, str(cm.exception))                     # no codes on the page

    def test_an_error_after_part_of_the_answer_throws_the_part_away(self):
        with FakeHub(lambda t, b, r: {"fail": "interrupted", "retry_after": 3}) as h:
            with self.assertRaises(engines.Busy) as cm:
                hub.Hub(h.path).chat("triage", [{"role": "user", "content": "x"}], {})
        self.assertEqual(cm.exception.retry_after, 3)
        self.assertGreater(engines.wait_for(), 0)                             # idle work holds back meanwhile

    def test_background_work_that_must_finish_waits_out_a_busy_hub(self):
        answers = iter([{"refuse": "busy", "retry_after": 1}, GOOD])
        with FakeHub(lambda t, b, r: next(answers)) as h:
            t0 = time.monotonic()
            content, _, _ = hub.Hub(h.path, patient=lambda: False).chat("triage", [{"role": "user", "content": "x"}], {})
        self.assertEqual(json.loads(content)["action"], "reply")
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)
        with FakeHub(lambda t, b, r: {"refuse": "busy", "retry_after": 1}) as h:   # cancelled: stops waiting
            with self.assertRaises(engines.Busy):
                hub.Hub(h.path, patient=lambda: True).chat("triage", [{"role": "user", "content": "x"}], {})

    def test_the_hubs_pace_is_waited_out_in_place_not_a_reason_to_start_over(self):
        """功耗 on the hub: a background request too soon after the last is refused with power_cooldown. That is
        waited out where it is, so a step of several requests carries on; a cancelled job stops waiting at once."""
        n = []

        def reply(task, body, req):
            n.append(task)
            return {"refuse": "busy", "retry_after": 1, "reason": "power_cooldown"} if len(n) % 2 else GOOD
        with FakeHub(reply) as h:
            content, _, _ = hub.Hub(h.path, "background").chat("triage", [{"role": "user", "content": "x"}], {})
            self.assertEqual((content, len(n)), (GOOD, 2))
            self.assertEqual(engines._later["until"], 0.0)                 # idle work in general is not held back
            t0 = time.monotonic()
            with self.assertRaises(engines.Busy) as ctx:
                hub.Hub(h.path, "background", stop=lambda: True).chat("triage", [{"role": "user", "content": "x"}], {})
            self.assertLess(time.monotonic() - t0, 1)
            self.assertEqual(ctx.exception.reason, "power_cooldown")

    def test_waiting_out_the_pace_is_shown_on_the_job_line(self):
        """luoke-mail#25: six minutes of 「正在整理你的资料」 while the hub held background requests back."""
        n, seen = [], []
        job = {"stage": "正在整理你的资料"}
        show = __import__("luoke_mail.server", fromlist=["App"]).App._progress(job)

        def reply(task, body, req):
            n.append(task)
            return {"refuse": "busy", "retry_after": 1, "reason": "power_cooldown"} if len(n) == 1 else GOOD
        with FakeHub(reply) as h:
            link = hub.Hub(h.path, "background", progress=lambda s, secs=None: (show(s, secs), seen.append(job["stage"])))
            link.chat("triage", [{"role": "user", "content": "x"}], {})
        self.assertIn("螺壳在控制后台功耗，约 1 秒后继续…", seen)
        self.assertEqual(job["stage"], "正在整理你的资料")                   # back to what it was doing

    def test_loading_the_model_is_shown_on_the_job_line(self):
        job, seen = {"stage": "正在分析：Q3 预算评审"}, []
        show = __import__("luoke_mail.server", fromlist=["App"]).App._progress(job)
        with FakeHub(lambda t, b, r: GOOD) as h:
            h.loading = True
            link = hub.Hub(h.path, "user-waiting", progress=lambda s: (show(s), seen.append((s, job["stage"]))))
            link.chat("triage", [{"role": "user", "content": "x"}], {})
        self.assertEqual(seen, [("loading", "正在加载模型…"), ("running", "正在分析：Q3 预算评审")])

    def test_a_named_socket_is_that_one_or_none_never_the_hub_in_daily_use(self):
        daily = os.path.join(__import__("tempfile").mkdtemp(), "hub.sock")
        open(daily, "w").close()                                        # a hub at the usual place
        hub.DEFAULT_SOCKET = daily                                      # (no_real_hub restores it)
        self.assertEqual(hub.socket_path(), daily)
        self.assertIsNone(hub.socket_path("/tmp/no-such-luoke-hub.sock"))
        os.environ["LUOKE_HUB_SOCKET"] = "/tmp/no-such-luoke-hub.sock"
        self.addCleanup(os.environ.pop, "LUOKE_HUB_SOCKET", None)
        self.assertIsNone(hub.socket_path())

    def test_an_unreachable_hub_is_not_asked_again_at_once(self):
        """luoke-mail#24: every page poll asked a hub that was not running, and logged a line each time."""
        import io
        path = os.path.join(__import__("tempfile").mkdtemp(), "hub.sock")
        link = hub.Hub(path)
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            for _ in range(5):
                self.assertFalse(link.ready("triage"))
        self.assertEqual(err.getvalue().count("hub unreachable"), 1)
        with mock.patch("sys.stderr", err), self.assertRaises(engines.EngineError):
            link.tasks(fresh=True)                                      # asked for fresh: asks
        self.assertEqual(err.getvalue().count("hub unreachable"), 2)

    def test_no_hub_running(self):
        link = hub.Hub("/tmp/no-such-luoke-hub.sock")
        with self.assertRaises(engines.EngineError) as cm:
            link.chat("triage", [{"role": "user", "content": "x"}], {})
        self.assertEqual(str(cm.exception), hub.NOT_RUNNING)
        self.assertFalse(link.ready("triage"))

    def test_learning_from_sent_mail_carries_only_what_fits_and_says_how_many(self):
        mails = [{"uid": str(i), "subject": f"周报 {i}", "body": "本周完成了很多工作。" * 80, "to": ["a@corp.example"],
                  "from_addr": samples.ME, "date": "2026-10-01"} for i in range(8)]
        with FakeHub(lambda t, b, r: json.dumps({"facts": []}), max_chars=4000) as h:
            facts, count = learn.from_sent(mails, samples.ME, hub.Hub(h.path))
        self.assertLess(count, 8)
        self.assertEqual(h.requests[0]["body"]["luoke_items"], count)


class AppViaHubTests(unittest.TestCase):
    """The app with the switch on 螺壳主控."""

    def setUp(self):
        hub._cache.clear()
        engines._later["until"] = 0.0
        self.addCleanup(engines._later.update, until=0.0)
        no_real_hub(self)

    def test_the_model_service_stays_until_the_owner_switches(self):
        r = Rig(self)
        self.assertIsInstance(r.app.link(), engines.Direct)
        via_hub(r.app, "/tmp/x.sock")
        self.assertIsInstance(r.app.link(), hub.Hub)
        self.assertTrue(r.app.has_model())

    def test_switching_to_the_hub_and_back_keeps_the_models_service_choice(self):
        r = Rig(self)
        r.app.cfg["engine"]["hub_socket"] = "/tmp/no-such-luoke-hub.sock"
        with self.assertRaises(ValueError):                              # no hub running: nothing to switch to
            r.app.save_engine({"via": "hub"})
        with FakeHub(lambda t, b, req: GOOD, tasks={"triage": {"status": "available", "params": {"max_chars": 6000, "batch": 1}},
                                                     "draft": {"status": "needs_download", "params": {"max_chars": 6000, "batch": 1}}}) as h:
            r.app.cfg["engine"]["hub_socket"] = h.path
            view = r.app.hub_view()
            self.assertEqual((view["ok"], view["status"]), (True, "available"))
            self.assertNotIn("model", view)                               # the combination is not read for meaning
            self.assertIn("draft", view["unavailable"])
            self.assertIn("cleanup", view["unavailable"])                 # not in the recipe at all
            e = r.app.save_engine({"via": "hub"})
            self.assertEqual((e["via"], e["model"]), ("hub", "test-model"))
            t = r.app.test_engine()
            self.assertEqual(t["result"]["action"], "reply")
            self.assertEqual(h.requests[-1]["priority"], "user-waiting")
        e = r.app.save_engine({"base_url": "http://127.0.0.1:9/v1", "model": "test-model"})
        self.assertNotIn("via", e)                                        # back to the model service
        self.assertIsInstance(r.app.link(), engines.Direct)

    def test_the_model_is_kept_loaded_while_the_page_is_open(self):
        r = Rig(self)
        with FakeHub(lambda t, b, req: GOOD) as h:
            r.app.activity(seen=True)                                   # direct: nothing to keep warm
            via_hub(r.app, h.path)
            r.app.activity(seen=False)                                  # a hidden page: no
            time.sleep(0.3)
            self.assertEqual(h.warmups, [])
            r.app.activity(seen=True)
            r.app.activity(seen=True)                                   # every few seconds: asked once
            time.sleep(0.5)
            self.assertEqual(h.warmups, [{"task": "luoke-mail.triage"}])
            self.assertEqual(r.app._warmth.lease, "lease-1")
            r.app._warmth.next = float("-inf")                          # halfway through the lease: renewed
            r.app.activity(seen=True)
            time.sleep(0.5)
            self.assertEqual(h.warmups[-1], {"task": "luoke-mail.triage", "lease_id": "lease-1"})

    def test_a_sync_the_user_watches_is_user_waiting_otherwise_background_on_battery_too(self):
        r = Rig(self)
        with FakeHub(lambda t, b, req: GOOD) as h:
            via_hub(r.app, h.path)
            r.app.activity(seen=True)                                   # the page is open and visible
            r.app.sync_mailbox(ME, new_job(), user=True)
            self.assertEqual({q["priority"] for q in h.requests}, {"user-waiting"})
            r.app._seen = float("-inf")                                  # the user left the page
            r.srv._add("INBOX", r.srv.folders["INBOX"][0]["raw"].replace(b"Message-ID: <", b"Message-ID: <again-"), set())
            n = len(h.requests)
            r.app.sync_mailbox(ME, new_job(), user=True)
            self.assertTrue(h.requests[n:])
            self.assertEqual({(q["priority"], q["battery"]) for q in h.requests[n:]}, {("background", True)})

    def test_background_sorting_leaves_mail_waiting_while_the_hub_pauses_background_work(self):
        r = Rig(self)
        with FakeHub(lambda t, b, req: {"refuse": "paused", "retry_after": 600}) as h:
            via_hub(r.app, h.path)
            s = r.app.sync_mailbox(ME, new_job())
        self.assertEqual((s["errors"], s["by_model"]), (0, 0))
        self.assertEqual(s["waiting"], len(r.app.store.unanalyzed(ME)))
        self.assertFalse(any(it.get("error") for it in r.app.store.unanalyzed(ME)))
        self.assertEqual({q["priority"] for q in h.requests}, {"background"})
        self.assertEqual(len(h.requests), 1)                            # paused: not asked again for every message

    def test_idle_work_skips_tasks_the_hub_does_not_offer(self):
        r = Rig(self)
        with FakeHub(lambda t, b, req: GOOD, tasks={"triage": {"status": "available", "params": {"max_chars": 6000, "batch": 1}}}) as h:
            via_hub(r.app, h.path)
            r.app.store.cache_set(ME, "judge:inbox", {"on": True})
            self.assertFalse(r.app.link().ready("cleanup"))
            self.assertIsNone(r.app.curator.next_account())
            self.assertIsNone(r.app.learner.next_account())
            self.assertTrue(r.app.model_up())
        self.assertFalse(r.app.model_up())                              # the hub went away



class PackagedAppTests(unittest.TestCase):
    """螺壳邮件助手.app comes with 螺壳主控 and only uses it (owner 2026-10-15): no model to pick; until the hub can
    sort, the mail waits, the page says why, and the sorting starts once it can."""

    def setUp(self):
        hub._cache.clear()
        no_real_hub(self)

    def test_the_app_has_no_model_to_pick(self):
        private_config(self)
        home = __import__("tempfile").mkdtemp()
        app = server.App(secret_store=FileStore(os.path.join(home, "c.json")), state_store=Store(os.path.join(home, "s.db")),
                         desktop_=FakeDesktop(), hub_only=True)
        self.addCleanup(app.store.close)
        self.assertEqual(app.cfg["engine"]["via"], "hub")
        self.assertEqual(config.load()["engine"]["via"], "hub")
        self.assertTrue(app.has_model() and app.state()["hub_only"])
        with self.assertRaises(ValueError):
            app.save_engine({"base_url": "http://127.0.0.1:9/v1", "model": "test-model"})

    def test_settings_name_the_hubs_model_and_whether_it_is_ready(self):
        """luoke-mail#6 (brand spec): 「螺壳主控 · 模型名」 and a ready mark -- the name the hub gives for people
        (`model_name`, luoke-core 5e67d4b); the combo is opaque and never taken apart (V1 §8)."""
        r = Rig(self)
        with FakeHub(lambda t, b, req: GOOD, combo="omlx-0.7.0+gemma-4-26b-8bit") as h:
            via_hub(r.app, h.path)
            self.assertEqual(r.app.state()["hub_model"], {"name": "", "ready": True})          # an older hub: no name
            h.model_name = "gemma-4-26b-a4b-it-8bit"
            hub._cache.clear()
            self.assertEqual(r.app.state()["hub_model"], {"name": "gemma-4-26b-a4b-it-8bit", "ready": True})
        hub._cache.clear()
        self.assertIsNone(r.app.state()["hub_model"])                          # the hub gone: nothing claimed

    def test_the_mail_waits_for_the_download_the_page_says_so_and_sorting_starts_when_ready(self):
        r = Rig(self)
        params = {"max_chars": 6000, "batch": 1}
        with FakeHub(lambda t, b, req: GOOD, tasks={"triage": {"status": "downloading", "reason": "正在下载",
                                                               "progress": 0.45, "params": params}}) as h:
            via_hub(r.app, h.path)
            t0 = time.monotonic()
            s = r.app.sync_mailbox(ME, new_job(), user=True)
            self.assertLess(time.monotonic() - t0, 10)                  # not each message waiting on the download
            self.assertEqual([q for q in h.requests if q["task"] == "triage"], [])
            self.assertTrue(s["waiting"])
            self.assertEqual(s["waiting"], len(r.app.store.unanalyzed(ME)))
            a = r.app.activity(seen=True)
            self.assertEqual(a["model"]["status"], "downloading")
            self.assertIn("45%", a["model"]["text"])
            self.assertIn("45%", a["model"]["short"])
            self.assertEqual(a["waiting"][ME], s["waiting"])
            h.tasks["triage"] = {"status": "available", "params": params}
            r.app._model_note = (float("-inf"), r.app._model_note[1])   # the page asks again a few seconds later
            self.assertIsNone(r.app.activity(seen=True)["model"])       # ready: the page starts the sync
            s = r.app.sync_mailbox(ME, new_job(), user=True)
            self.assertEqual((s["waiting"], bool(s["by_model"])), (0, True))
        r.app.cfg["engine"]["hub_socket"] = "/tmp/no-such-luoke-hub.sock"
        r.app._model_note = (float("-inf"), None)
        self.assertEqual(r.app.model_note()["status"], "absent")
        r.app.cfg["engine"] = {"base_url": "http://127.0.0.1:9/v1", "model": "test-model"}
        self.assertIsNone(r.app.model_note())                           # a model service the user picked: nothing to say


class HubMemoryTests(unittest.TestCase):
    """The app writes the memory with the library 螺壳主控 carries (luoke-core c0c1c14), else its own copy."""

    def test_where_the_hubs_luoke_mem_is(self):
        import tempfile
        from luoke_mail import memory as mem_mod
        lib = tempfile.mkdtemp()
        os.makedirs(os.path.join(lib, "luoke_mem"))
        with FakeHub(lambda *a: "{}") as h, mock.patch.dict(os.environ, {"LUOKE_HUB_SOCKET": h.path}):
            with mock.patch.dict(os.environ, {"LUOKE_LAUNCHER": ""}):
                self.assertIsNone(mem_mod._hub_copy())                         # from source: never
            with mock.patch.dict(os.environ, {"LUOKE_LAUNCHER": "/x/launcher"}):
                self.assertIsNone(mem_mod._hub_copy())                         # a hub without the memory: our own
                h.luoke_mem = {"path": lib, "version": "0.23.1"}
                self.assertEqual(mem_mod._hub_copy(), lib)
                h.luoke_mem = {"path": "/no/such/place", "version": "0.23.1"}
                self.assertIsNone(mem_mod._hub_copy())                         # moved away: our own


if __name__ == "__main__":
    unittest.main()
