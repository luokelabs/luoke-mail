"""The evaluation sets for the tasks other than sorting (bench/task_sets.py) and their scoring (bench/run_tasks.py):
well-formed, every answer one the app can give, and the hard criteria catching what they are for."""
from __future__ import annotations

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "bench")]

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import hub, learn, threads, triage  # noqa: E402
from fakes import FakeHub  # noqa: E402
import run_tasks  # noqa: E402
import task_sets as S  # noqa: E402


class SetTests(unittest.TestCase):
    def test_answers_are_ones_the_app_can_give(self):
        for scope, items in S.CLEANUP.items():
            for row, ok, hard in items:
                self.assertTrue(set(ok) <= set(triage.old_kinds(scope)), (scope, row["subject"], ok))
                if hard:                                   # something to keep: no trash answer is acceptable
                    self.assertFalse(set(ok) & set(S.TRASH), row["subject"])
        for s, ok, hard in S.SENDERS:
            self.assertTrue(set(ok) <= set(triage.SENDER_KINDS), s["from_addr"])
            if hard:
                self.assertFalse(set(ok) & set(run_tasks.UNSUB), s["from_addr"])
        for case in S.LEARN_SENT:
            for topics, _ in case["expect"]:
                self.assertTrue(set(topics) <= set(learn.FACT_TOPICS), topics)
        for msgs, ok, hard in S.THREAD:
            self.assertTrue(set(ok) <= set(threads.STATES), ok)
            self.assertFalse(hard and len(ok) > 1, msgs[-1]["subject"])     # hard only when there is one fair reading
            self.assertEqual([m["date"] for m in msgs], sorted(m["date"] for m in msgs))
        for case in S.LEARN_ORGS:
            for pos, topics, _ in case["expect"]:
                self.assertTrue(1 <= pos <= len(case["senders"]) and set(topics) <= set(learn.LIFE_TOPICS), (pos, topics))

    def test_sizes_and_fiction(self):
        self.assertGreaterEqual(sum(len(v) for v in S.CLEANUP.values()), 30)
        self.assertGreaterEqual(len(S.SENDERS), 20)
        self.assertGreaterEqual(len(S.THREAD), 15)
        text = json.dumps([S.CLEANUP, S.SENDERS, S.LEARN_SENT, S.LEARN_ORGS, S.THREAD], ensure_ascii=False)
        for addr in __import__("re").findall(r"[\w.+-]+@([\w.-]+)", text):
            self.assertTrue(addr.endswith(".example"), addr)    # made-up senders only


class ManifestTests(unittest.TestCase):
    def test_the_app_manifest_says_this_version_and_these_tasks(self):
        from luoke_mail import __version__, engines
        with open(os.path.join(os.path.dirname(HERE), "packaging", "accessory.json"), encoding="utf-8") as f:
            m = json.load(f)
        self.assertEqual(m["version"], __version__)
        self.assertEqual([t["name"] for t in m["tasks"]], [f"luoke-mail.{t}" for t in engines.TASKS])
        from luoke_mail import server
        self.assertEqual([(j["name"], j["command"]) for j in m["jobs"]],
                         [(n, ["-m", "luoke_mail", "--job", n]) for n in server.IDLE_JOBS])
        self.assertEqual(sorted(k for ks in server.IDLE_JOBS.values() for k in ks), sorted(server.App.IDLE_KINDS))
        self.assertEqual(m["commands"]["uninstall"], ["-m", "luoke_mail", "--forget-secrets"])
        with open(os.path.join(os.path.dirname(HERE), "pyproject.toml"), encoding="utf-8") as f:
            self.assertIn(f'version = "{__version__}"', f.read())


class ScoringTests(unittest.TestCase):
    def setUp(self):
        hub._cache.clear()

    def test_a_model_that_trashes_everything_fails_cleanup_and_one_that_keeps_banks_passes_senders(self):
        def reply(task, body, req):
            n = body["luoke_items"]
            kind = {"cleanup": "promotion", "senders": "transactional"}[task]
            return json.dumps({"items": [{"n": i, "kind": kind} for i in range(1, n + 1)]})
        with FakeHub(reply) as h:
            link = hub.Hub(h.path, "user-waiting")
            _, cleanup = run_tasks.run_cleanup(link)
            _, senders = run_tasks.run_senders(link)
        self.assertEqual(cleanup["hard_failures"], cleanup["hard_n"] - 1)  # all but the one kept by its 收藏 label
        self.assertEqual(senders["hard_failures"], 0)
        self.assertEqual(senders["unsub_found"], 0)               # safe, and useless: the soft score says so

    def test_a_third_partys_claim_taken_as_the_users_is_a_hard_failure(self):
        def reply(task, body, req):
            return json.dumps({"facts": [{"topic": "职位", "who": "", "text": "职位：产品经理", "n": [1]},
                                         {"topic": "其他", "who": "", "text": "私人银行客户", "n": [2]}]})
        with FakeHub(reply) as h:
            items, score = run_tasks.run_learn(hub.Hub(h.path, "user-waiting"), "learn-sent")
        self.assertEqual(score["hard_failures"], 1)
        self.assertEqual(items[1]["hard_failures"], ["私人银行客户"])


    def test_a_model_that_says_you_to_everything_nags_and_one_that_says_done_misses(self):
        for state, nag, miss in (("you", 5, 0), ("done", 0, 5)):
            with FakeHub(lambda task, body, req, s=state: json.dumps({"state": s, "since": "", "what": "x"})) as h:
                _, score = run_tasks.run_thread(hub.Hub(h.path, "user-waiting"))
            self.assertEqual((score["done_as_you"], score["missed_you"]), (nag, miss), state)
            self.assertEqual(score["hard_failures"], nag + miss)


if __name__ == "__main__":
    unittest.main()
