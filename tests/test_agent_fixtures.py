"""The scenario samples for luoke-agent (fixtures/agent/, protocol §11): recorded again and compared, so a change to
what the agent sees cannot land with the samples left behind, and read through as the agent reads them."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
import agent_a2ui as a2ui  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "record_agent_fixtures.py")
FIXTURES = os.path.join(ROOT, "fixtures", "agent")


def scenarios() -> list:
    return sorted(d for d in os.listdir(FIXTURES) if os.path.isdir(os.path.join(FIXTURES, d)))


def load(*path: str):
    with open(os.path.join(FIXTURES, *path), encoding="utf-8") as f:
        return json.load(f)


def uri(sc: str, name: str) -> str:
    return load(sc, name)["request"]["params"].get("uri", "")


class FixtureTests(unittest.TestCase):
    def test_recording_again_gives_what_is_in_the_repository(self):
        r = subprocess.run([sys.executable, "-B", SCRIPT, "--check"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout[-4000:])

    def test_each_scenario_reads_the_summary_the_permissions_and_every_page(self):
        with open(os.path.join(ROOT, "packaging", "accessory.json"), encoding="utf-8") as f:
            caps = json.load(f)["capabilities"]
        want = {"summary", "permissions", "luoke://luoke-mail/outbox"} | {f"page/{p['id']}" for p in caps["service"]["pages"]}
        self.assertGreaterEqual(len(scenarios()), 5)
        for sc in scenarios():
            steps = load(sc, "scenario.json")["steps"]
            self.assertLessEqual(want, {uri(sc, s["file"]) for s in steps}, sc)
            for s in steps:
                self.assertTrue(os.path.isfile(os.path.join(FIXTURES, sc, s["file"])), s["file"])

    def test_every_card_and_page_passes_the_agents_check(self):
        for sc in scenarios():
            for name in os.listdir(os.path.join(FIXTURES, sc)):
                reply = load(sc, name).get("reply") if name not in ("manifest.json", "scenario.json") else None
                result = (reply or {}).get("result") or {}
                for c in result.get("contents") or []:
                    text = c["text"]
                    if c["uri"].startswith("page/"):
                        self.assertEqual(a2ui.validate(text), [], f"{sc}/{name}")
                    elif c["uri"].endswith("/outbox"):
                        for it in text["items"]:
                            self.assertEqual(a2ui.validate(it["card"]), [], f"{sc}/{name}")

    def test_a_tap_carries_the_users_mark_and_nothing_else_does(self):
        taps = 0
        for sc in scenarios():
            for s in load(sc, "scenario.json")["steps"]:
                rec = load(sc, s["file"])
                params = rec["request"]["params"]
                mark = params["_meta"].get("ai.luoke/user-action")
                if rec["request"]["method"] == "tools/call" and params["name"] in ("mail_apply_plan", "mail_undo", "mail_do"):
                    self.assertEqual(sorted(mark), ["at", "componentId", "surfaceId"], s["file"])
                    self.assertFalse(rec["reply"]["result"]["isError"], s["file"])
                    taps += 1
                else:
                    self.assertIsNone(mark, s["file"])
        self.assertGreaterEqual(taps, 5)


if __name__ == "__main__":
    unittest.main()
