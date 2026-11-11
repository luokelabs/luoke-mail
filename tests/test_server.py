import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http.server import ThreadingHTTPServer  # noqa: E402

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import engines, samples, server  # noqa: E402
from luoke_mail.credstore import FileStore  # noqa: E402
from luoke_mail.store import Store  # noqa: E402
from fakes import GOOD, FakeChat, FakeIMAP, suggestion  # noqa: E402


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        home = tempfile.mkdtemp()
        cls.app = server.App(secret_store=FileStore(os.path.join(home, "c.json")), state_store=Store(os.path.join(home, "s.db")))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cls.app))
        cls.app.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.store.close()

    def req(self, method, path, body=None, token=True, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=20)
        headers = {"Host": host or f"127.0.0.1:{self.app.port}"}
        if token:
            headers["X-Luoke-Token"] = self.app.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def wait_job(self, job):
        for _ in range(200):
            _, job = self.req("GET", f"/api/jobs/{job['id']}")
            if job["status"] != "running":
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    # ---- security --------------------------------------------------------------------------
    def test_requires_token(self):
        self.assertEqual(self.req("GET", "/api/state", token=False)[0], 401)
        self.assertEqual(self.req("POST", "/api/forget", {}, token=False)[0], 401)
        self.assertEqual(self.req("GET", "/api/state")[0], 200)

    def test_rejects_foreign_host(self):
        self.assertEqual(self.req("GET", "/api/state", host="evil.example:80")[0], 403)
        self.assertEqual(self.req("GET", "/", host="evil.example")[0], 403)

    def test_static_and_traversal(self):
        status, body = self.req("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn("螺壳邮件助手".encode(), body)
        self.assertEqual(self.req("GET", "/../server.py", token=False)[0], 404)
        self.assertEqual(self.req("GET", "/%2e%2e/server.py", token=False)[0], 404)

    def test_state_has_no_secrets(self):
        self.req("POST", "/api/mail", {"provider": "qq", "address": "me@qq.com", "secret": "AUTHCODE1234"})
        status, st = self.req("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(st["mail"]["has_secret"])
        self.assertNotIn("AUTHCODE1234", json.dumps(st))

    # ---- setup -----------------------------------------------------------------------------
    def test_saved_key_used_without_retyping(self):
        with FakeChat(lambda b: (200, GOOD)) as chat:
            self.app.secrets.set("engine", chat.base_url, "k-1")
            _, eng = self.req("POST", "/api/engine", {"base_url": chat.base_url, "model": "test-model"})
            self.assertTrue(eng["has_api_key"])
            self.assertEqual(self.app.engine_key(), "k-1")
            self.app.secrets.delete("engine", chat.base_url)

    def test_scan_uses_typed_keys(self):
        with FakeChat(lambda b: (200, GOOD), models=("Qwen3.8-27B-oQ4e-mtp",)) as chat:
            with mock.patch.object(engines, "KNOWN_ENGINES", []):       # never knock on this machine's own services
                status, r = self.req("POST", "/api/engines/scan", {"custom": chat.base_url, "keys": {chat.base_url: "k-123"}})
            self.assertEqual(status, 200)
            eng = [e for e in r["engines"] if e["base_url"] == chat.base_url][0]
            self.assertEqual(eng["models"], ["Qwen3.8-27B-oQ4e-mtp"])
            self.assertNotIn("k-123", json.dumps(r))

    def test_engine_test_uses_newest_inbox_mail(self):
        from luoke_mail.mailbox import Mailbox, MailError
        from test_core import make_mail
        msgs = [(make_mail("旧的那封", "旧"), True), (make_mail("最新的一封", "请明天回复我"), False)]
        self.req("POST", "/api/mail", {"provider": "custom", "address": "me@example.com", "secret": "secret", "host": "127.0.0.1"})
        with FakeIMAP(msgs) as imap, FakeChat(lambda b: (200, GOOD)) as chat:
            self.app.open_mailbox = lambda *_: Mailbox("custom", "me@example.com", "secret", host="127.0.0.1", port=imap.port, ssl=False, timeout=5)
            self.req("POST", "/api/engine", {"base_url": chat.base_url, "model": "test-model"})
            status, r = self.req("POST", "/api/engine/test", {})
            self.assertEqual(status, 200, r)
            self.assertEqual((r["source"], r["mail"]["subject"]), ("inbox", "最新的一封"))

            def broken(*_):
                raise MailError("登录失败：测试")
            self.app.open_mailbox = broken
            _, r = self.req("POST", "/api/engine/test", {})
            self.assertEqual(r["source"], "demo")
            self.assertIn("改用示例邮件", r["warning"])
        del self.app.open_mailbox

    # ---- workbench (demo inbox) ---------------------------------------------------------------
    def test_demo_workbench_end_to_end(self):
        def reply(body):
            text = body["messages"][1]["content"]
            if "draft" in json.dumps(body.get("response_format", {})) or '"body"' in json.dumps(body.get("response_format", {})):
                return 200, json.dumps({"body": "好的，今天下班前发您。也可以找 helper@else.example。"}, ensure_ascii=False)
            if "AI Weekly" in text or "某某商城" in text:
                return 200, suggestion("ignore", category="promotion", priority="low", due_date=None, reply_points=[])
            return 200, GOOD
        with FakeChat(reply) as chat:
            self.req("POST", "/api/engine", {"base_url": chat.base_url, "model": "test-model"})
            status, job = self.req("POST", "/api/sync", {"source": "demo"})
            self.assertEqual(status, 200, job)
            job = self.wait_job(job)
            self.assertEqual(job["status"], "done", job)
            self.assertEqual(job["result"]["by_model"], len(samples.demo_inbox()))     # every message read
            _, view = self.req("GET", "/api/items?source=demo")
            groups = [e for e in view["entries"] if e["type"] == "group"]
            self.assertEqual(sorted(g["count"] for g in groups), [3, 3])

            # act on a group, then undo it (demo needs no write mode and touches no mailbox)
            ids = [i["id"] for i in groups[0]["items"]]
            status, res = self.req("POST", "/api/items/act", {"source": "demo", "ids": ids, "op": "trash"})
            self.assertEqual(status, 200, res)
            _, after = self.req("GET", "/api/items?source=demo")
            self.assertEqual(after["pending"], view["pending"] - 3)
            self.req("POST", "/api/undo", {"source": "demo", "log_id": res["log_id"]})
            _, back = self.req("GET", "/api/items?source=demo")
            self.assertEqual(back["pending"], view["pending"])

            # change a suggestion and remember it as a rule
            one = next(e["item"] for e in view["entries"] if e["type"] == "item")
            status, r = self.req("POST", "/api/items/override", {"source": "demo", "ids": [one["id"]], "action": "archive",
                                                                  "remember": "sender"})
            self.assertEqual(status, 200, r)
            _, rules = self.req("GET", "/api/rules?source=demo")
            self.assertTrue(any(x["value"] == one["from_addr"] for x in rules["rules"]))
            self.req("POST", "/api/undo", {"source": "demo", "log_id": r["log_id"]})          # undo restores both
            _, rules = self.req("GET", "/api/rules?source=demo")
            self.assertFalse(any(x["value"] == one["from_addr"] for x in rules["rules"]))
            _, again = self.req("GET", "/api/items?source=demo")
            restored = next(e["item"] for e in again["entries"] if e["type"] == "item" and e["item"]["id"] == one["id"])
            self.assertEqual(restored["action"], one["action"])

            # draft text for a reply item
            status, d = self.req("POST", "/api/items/draft", {"source": "demo", "id": one["id"]})
            self.assertEqual(status, 200, d)
            self.assertIn("下班前", d["text"])
            self.assertIn("helper@else.example", d["note"])                     # not in the mail: checked before sending
            _, view = self.req("GET", "/api/items?source=demo")
            drafted = next(e["item"] for e in view["entries"] if e["type"] == "item" and e["item"]["id"] == one["id"])
            self.assertEqual(drafted["draft_note"], d["note"])                   # and still there on 查看草稿

    def test_write_mode_gates_bulk_trash_only(self):
        self.req("POST", "/api/mail", {"provider": "qq", "address": "me@qq.com", "secret": "AUTHCODE1234"})
        self.req("POST", "/api/mode", {"write": False})
        status, r = self.req("POST", "/api/items/act", {"source": "me@qq.com", "ids": [1, 2], "op": "trash"})
        self.assertEqual(status, 400)
        self.assertIn("处理模式", r["error"])
        status, r = self.req("POST", "/api/items/act", {"source": "me@qq.com", "ids": [1], "op": "archive"})
        self.assertNotIn("处理模式", r.get("error") or "")                      # archiving: the click is enough
        self.assertEqual(self.req("POST", "/api/mode", {"write": True})[0], 400)        # consent required
        self.assertEqual(self.req("POST", "/api/mode", {"write": True, "consent": True})[0], 200)
        self.req("POST", "/api/mode", {"write": False})

    def test_subscriptions_persist_and_undo(self):
        status, job = self.req("POST", "/api/subscriptions", {"source": "demo", "days": 60})
        self.assertEqual(status, 200, job)
        job = self.wait_job(job)
        self.assertEqual(job["status"], "done", job)
        # a refresh shows the stored result without re-scanning
        _, view = self.req("GET", "/api/subscriptions?source=demo")
        rows = {r["key"]: r for r in view["senders"]}
        self.assertEqual(rows["list:promo.mall.example"]["verdict"], "unsubscribe")

        # one-click unsubscribe: plain-language message, sender moves to "handled" and stays there
        status, r = self.req("POST", "/api/senders/act", {"source": "demo", "key": "list:weekly.aiweekly.example", "op": "unsubscribe"})
        self.assertEqual(status, 200, r)
        self.assertTrue(r["message"].startswith("已退订「AI Weekly」"))
        self.assertNotIn("HTTP", r["message"])
        self.wait_job(self.req("POST", "/api/subscriptions", {"source": "demo", "days": 60})[1])          # re-scan
        _, view = self.req("GET", "/api/subscriptions?source=demo")
        rows = {r["key"]: r for r in view["senders"]}
        self.assertEqual(rows["list:weekly.aiweekly.example"]["decision"]["status"], "unsubscribed")

        # "以后都归档" creates a rule and is undoable; undoing a rule needs no write mode
        _, r = self.req("POST", "/api/senders/act", {"source": "demo", "key": "list:promo.mall.example", "op": "archive_rule"})
        _, rules = self.req("GET", "/api/rules?source=demo")
        self.assertTrue(any(x["value"] == "promo.mall.example" for x in rules["rules"]))
        _, u = self.req("POST", "/api/undo", {"source": "demo", "log_id": r["log_id"]})
        self.assertEqual(u["message"], "已撤销")
        _, rules = self.req("GET", "/api/rules?source=demo")
        self.assertFalse(any(x["value"] == "promo.mall.example" for x in rules["rules"]))
        _, view = self.req("GET", "/api/subscriptions?source=demo")
        self.assertIsNone({r["key"]: r for r in view["senders"]}["list:promo.mall.example"]["decision"])

        # suspicious senders are never unsubscribed
        status, r = self.req("POST", "/api/senders/act", {"source": "demo", "key": "from:security@icbc-verify.example", "op": "unsubscribe"})
        self.assertEqual(status, 400)

    def test_storage_options_for_senders_without_unsubscribe(self):
        job = self.wait_job(self.req("POST", "/api/subscriptions", {"source": "demo", "days": 60})[1])
        self.assertEqual(job["status"], "done", job)
        _, view = self.req("GET", "/api/subscriptions?source=demo")
        self.assertEqual(view["provider"]["purge_days"], 30)
        self.assertTrue(view["provider"]["block"]["steps"])
        forum = "from:notice@forum.example"
        # trash rule + undo
        _, r = self.req("POST", "/api/senders/act", {"source": "demo", "key": forum, "op": "trash_rule"})
        _, rules = self.req("GET", "/api/rules?source=demo")
        self.assertTrue(any(x["value"] == "notice@forum.example" and x["action"] == "ignore" for x in rules["rules"]))
        self.req("POST", "/api/undo", {"source": "demo", "log_id": r["log_id"]})
        _, rules = self.req("GET", "/api/rules?source=demo")
        self.assertFalse(any(x["value"] == "notice@forum.example" for x in rules["rules"]))
        # history preview + cleanup
        _, p = self.req("POST", "/api/senders/act", {"source": "demo", "key": forum, "op": "history_preview"})
        self.assertEqual((p["count"], p["purge_days"]), (25, 30))
        status, r = self.req("POST", "/api/senders/act", {"source": "demo", "key": forum, "op": "trash_history"})
        self.assertEqual(status, 200, r)
        self.assertIn("「已删除」", r["message"])
        # suspicious sender: spam rule allowed, unsubscribe refused
        _, r = self.req("POST", "/api/senders/act", {"source": "demo", "key": "from:security@icbc-verify.example", "op": "spam_rule"})
        self.assertIn("垃圾邮件", r["message"])
        self.req("POST", "/api/undo", {"source": "demo", "log_id": r["log_id"]})

    def test_rule_add_and_delete_are_undoable(self):
        _, r = self.req("POST", "/api/rules", {"source": "demo", "kind": "domain", "value": "undo-me.example", "action": "archive"})
        self.req("POST", "/api/undo", {"source": "demo", "log_id": r["log_id"]})
        _, rules = self.req("GET", "/api/rules?source=demo")
        self.assertFalse(any(x["value"] == "undo-me.example" for x in rules["rules"]))
        _, r = self.req("POST", "/api/rules", {"source": "demo", "kind": "domain", "value": "keep-me.example", "action": "archive"})
        _, d = self.req("POST", "/api/rules/delete", {"source": "demo", "id": r["id"]})
        self.req("POST", "/api/undo", {"source": "demo", "log_id": d["log_id"]})
        _, rules = self.req("GET", "/api/rules?source=demo")
        self.assertTrue(any(x["value"] == "keep-me.example" for x in rules["rules"]))


if __name__ == "__main__":
    unittest.main()
