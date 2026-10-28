"""Rules belong to one inbox; exporting them as a Gmail filter file."""
import os
import sqlite3
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import gmailfilters, server  # noqa: E402
from luoke_mail.credstore import FileStore  # noqa: E402
from luoke_mail.store import Store  # noqa: E402

ME = "me@example.com"
APPS = "{http://schemas.google.com/apps/2006}"
ATOM = "{http://www.w3.org/2005/Atom}"


def props(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    return [{p.get("name"): p.get("value") for p in e.iter(APPS + "property")} for e in root.iter(ATOM + "entry")]


class RuleScopeTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "s.db")

    def test_v02_rules_are_migrated_and_sorted(self):
        db = sqlite3.connect(self.path)
        db.executescript("""CREATE TABLE rules (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, value TEXT NOT NULL,
                            action TEXT NOT NULL, label TEXT, created_at TEXT, hits INTEGER DEFAULT 0, UNIQUE(kind, value));
                            INSERT INTO rules (kind, value, action, label, hits) VALUES
                              ('list', 'promo.mall.example', 'archive', '归档', 3),
                              ('sender', 'info@kfc.example', 'ignore', '忽略', 1);""")
        db.commit()
        db.close()
        st = Store(self.path)
        self.addCleanup(st.close)
        st.adopt_rules(None, "demo", {"promo.mall.example"})
        self.assertEqual([r["value"] for r in st.rules("demo")], ["promo.mall.example"])
        self.assertEqual(st.rules("demo")[0]["hits"], 3)
        st.adopt_rules(ME, "demo", {"promo.mall.example"})           # once a mailbox is connected
        self.assertEqual([r["value"] for r in st.rules(ME)], ["info@kfc.example"])

    def test_demo_rules_never_touch_the_mailbox(self):
        st = Store(self.path)
        self.addCleanup(st.close)
        st.add_rule("demo", "sender", "no-reply@accounts.google.com", "ignore", "忽略")
        item = {"from_addr": "no-reply@accounts.google.com", "from_domain": "accounts.google.com"}
        self.assertIsNone(st.match_rule({**item, "account": ME}))
        self.assertEqual(st.match_rule({**item, "account": "demo"})["action"], "ignore")

    def test_changing_the_action_invalidates_the_export_and_undo_is_exact(self):
        st = Store(self.path)
        self.addCleanup(st.close)
        rid = st.add_rule(ME, "domain", "shop.example", "archive", "归档")
        st.mark_exported([rid])
        before = st.rule(rid)
        self.assertEqual(st.add_rule(ME, "domain", "shop.example", "archive", "归档"), rid)
        self.assertIsNotNone(st.rule(rid)["exported_at"])           # same action: still exported
        st.add_rule(ME, "domain", "shop.example", "ignore", "忽略")
        self.assertIsNone(st.rule(rid)["exported_at"])              # the Gmail filter no longer matches
        st.restore_rule(before, ME)
        self.assertEqual(st.rule(rid), before)


class GmailFilterFileTests(unittest.TestCase):
    def test_xml_matches_gmails_format(self):
        rules = [{"id": 1, "kind": "sender", "value": "notice@forum.example", "action": "ignore"},
                 {"id": 2, "kind": "list", "value": "promo.mall.example", "action": "archive"},
                 {"id": 3, "kind": "domain", "value": "bad&co.example", "action": "spam"},
                 {"id": 4, "kind": "sender", "value": "boss@corp.example", "action": "reply"}]
        ok, skipped = gmailfilters.split(rules)
        self.assertEqual([r["id"] for r in ok], [1, 2, 3])
        self.assertEqual(skipped[0]["rule"]["id"], 4)
        xml = gmailfilters.build_xml(ok, ME, at=0)
        entries = props(xml)
        self.assertEqual(entries[0], {"from": "notice@forum.example", "shouldMarkAsRead": "true", "shouldTrash": "true"})
        self.assertEqual(entries[1], {"hasTheWord": "list:(promo.mall.example)", "shouldArchive": "true", "shouldMarkAsRead": "true"})
        self.assertEqual(entries[2], {"from": "bad&co.example", "shouldTrash": "true"})     # escaped, and spam -> trash
        self.assertTrue(gmailfilters.filename(0).endswith(".xml"))


class ExportTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.mkdtemp()
        self.app = server.App(secret_store=FileStore(os.path.join(home, "c.json")), state_store=Store(os.path.join(home, "s.db")))
        self.addCleanup(self.app.store.close)

    def test_export_only_new_rules_and_explain_deletes(self):
        a = self.app
        r1 = a.add_rule({"source": "demo", "kind": "sender", "value": "notice@forum.example", "action": "ignore"})
        a.add_rule({"source": "demo", "kind": "sender", "value": "boss@corp.example", "action": "reply"})
        out = a.export_gmail_filters({"source": "demo"})
        self.assertEqual(out["count"], 1)
        self.assertEqual(props(out["xml"])[0]["from"], "notice@forum.example")
        self.assertEqual(len(out["skipped"]), 1)
        self.assertIsNone(out["settings_url"])                    # demo: nothing to open
        with self.assertRaises(ValueError):                       # nothing new; importing twice would duplicate
            a.export_gmail_filters({"source": "demo"})
        again = a.export_gmail_filters({"source": "demo", "include_exported": True})
        self.assertEqual(again["count"], 1)
        one = a.export_gmail_filters({"source": "demo", "ids": [r1["id"]]})
        self.assertEqual(one["count"], 1)
        d = a.delete_rule({"source": "demo", "id": r1["id"]})
        self.assertIn("Gmail", d["message"])                      # the imported filter is still in Gmail

    def test_only_gmail_can_import(self):
        a = self.app
        a.cfg["accounts"] = [{"provider": "qq", "address": "me@qq.example", "host": None, "port": 993}]
        a.secrets.set(server.MAIL_SECRET, "me@qq.example", "x")
        a.add_rule({"source": "mailbox", "kind": "sender", "value": "ad@shop.example", "action": "ignore"})
        with self.assertRaises(ValueError):
            a.export_gmail_filters({"source": "mailbox"})
        self.assertFalse(a.rules_view("mailbox")["gmail"])
        self.assertEqual(a.rules_view("demo")["rules"], [])       # the mailbox rule stays in the mailbox


if __name__ == "__main__":
    unittest.main()
