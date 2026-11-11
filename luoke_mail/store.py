"""Local inbox state (SQLite): every message seen, its suggestion, what the user did, rules, action log.

One store backs every feature -- incremental sync, grouping, rules, undo, subscription health -- so
they stay consistent with each other. See docs/DESIGN.md.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time

from .config import CONFIG_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  account TEXT NOT NULL,
  message_id TEXT NOT NULL,
  uid TEXT,
  from_name TEXT, from_addr TEXT, from_domain TEXT, reply_to TEXT, auth TEXT,
  place TEXT NOT NULL DEFAULT 'inbox',     -- 'archive': archived by the user in the last days, no inbox UID
  list_id TEXT, unsub_http TEXT, unsub_mailto TEXT, unsub_one_click INTEGER DEFAULT 0, bulk INTEGER DEFAULT 0,
  subject TEXT, date TEXT, to_json TEXT, cc_json TEXT, attachments_json TEXT, body TEXT,
  group_key TEXT,
  action TEXT, priority TEXT, category TEXT, summary TEXT, reason TEXT, due_date TEXT,
  event_json TEXT, reply_points_json TEXT, suspicious INTEGER DEFAULT 0,
  suggested_by TEXT, model TEXT, seconds REAL, error TEXT,
  draft_text TEXT, flagged INTEGER DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'pending',
  done_action TEXT, done_at TEXT,
  fetched_at TEXT, analyzed_at TEXT, read_by_us TEXT, older INTEGER DEFAULT 0, images INTEGER DEFAULT 0,
  UNIQUE(account, message_id)
);
CREATE INDEX IF NOT EXISTS items_state ON items(account, state);
CREATE INDEX IF NOT EXISTS items_group ON items(account, group_key);
CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY, account TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, value TEXT NOT NULL,
  action TEXT NOT NULL, label TEXT, created_at TEXT, hits INTEGER DEFAULT 0, exported_at TEXT,
  UNIQUE(account, kind, value)
);
CREATE TABLE IF NOT EXISTS senders (
  account TEXT NOT NULL, key TEXT NOT NULL, name TEXT, status TEXT NOT NULL, at TEXT, detail TEXT,
  PRIMARY KEY (account, key)
);
CREATE TABLE IF NOT EXISTS cache (
  account TEXT NOT NULL, name TEXT NOT NULL, value TEXT, at TEXT, PRIMARY KEY (account, name)
);
CREATE TABLE IF NOT EXISTS old_mail (
  account TEXT NOT NULL, message_id TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'archive', uid TEXT, folder TEXT,
  from_name TEXT, from_addr TEXT, list_id TEXT, bulk INTEGER DEFAULT 0, subject TEXT, date TEXT, size INTEGER DEFAULT 0,
  seen INTEGER DEFAULT 0, flagged INTEGER DEFAULT 0, category TEXT, labels_json TEXT,
  kind TEXT, judged_at TEXT, model TEXT, state TEXT NOT NULL DEFAULT 'here',
  PRIMARY KEY (account, message_id)
);
CREATE TABLE IF NOT EXISTS log (
  id INTEGER PRIMARY KEY, ts TEXT, account TEXT, action TEXT, item_ids TEXT, ok INTEGER,
  detail TEXT, undo_json TEXT, undone INTEGER DEFAULT 0
);
-- 0.18: undo steps of a long operation, written batch by batch as it goes (清理 of tens of thousands, luoke-mail#32)
CREATE TABLE IF NOT EXISTS log_steps (
  log_id INTEGER NOT NULL, batch INTEGER NOT NULL, steps TEXT NOT NULL, PRIMARY KEY (log_id, batch)
);
CREATE TABLE IF NOT EXISTS digest_sources (
  account TEXT NOT NULL, key TEXT NOT NULL, name TEXT, origin TEXT, added_at TEXT, PRIMARY KEY (account, key)
);
CREATE TABLE IF NOT EXISTS digest_mail (
  account TEXT NOT NULL, message_id TEXT NOT NULL, source_key TEXT, name TEXT, folder TEXT, uid TEXT, subject TEXT,
  date TEXT, run TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', queued_at TEXT, read_at TEXT, model TEXT, error TEXT,
  PRIMARY KEY (account, message_id)
);
CREATE TABLE IF NOT EXISTS digest_items (
  id INTEGER PRIMARY KEY, account TEXT NOT NULL, message_id TEXT NOT NULL, title TEXT, summary TEXT, url TEXT, host TEXT,
  topics_json TEXT, lasting INTEGER, score INTEGER, why TEXT, date TEXT, source TEXT, run TEXT, feedback TEXT,
  opened INTEGER DEFAULT 0, created_at TEXT, pick_week INTEGER DEFAULT 0, pick_month INTEGER DEFAULT 0, pick_why TEXT,
  archived_at TEXT
);
CREATE INDEX IF NOT EXISTS digest_items_date ON digest_items(account, date);
CREATE TABLE IF NOT EXISTS gmail_ids (
  account TEXT NOT NULL, message_id TEXT NOT NULL, gm_msgid TEXT NOT NULL, PRIMARY KEY (account, message_id)
);
CREATE TABLE IF NOT EXISTS learn_candidates (
  account TEXT NOT NULL, source TEXT NOT NULL, topic TEXT NOT NULL, norm TEXT NOT NULL, text TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'pending', note_id INTEGER, updated_at TEXT,
  PRIMARY KEY (account, source, topic, norm)
);
CREATE TABLE IF NOT EXISTS agent_plans (
  plan_id TEXT PRIMARY KEY, account TEXT NOT NULL, kind TEXT NOT NULL, entries_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open', logs_json TEXT, created_at TEXT, shown_at TEXT, updated_at TEXT
);
"""

MESSAGE_FIELDS = ("uid", "from_name", "from_addr", "from_domain", "reply_to", "list_id", "unsub_http",
                  "unsub_mailto", "unsub_one_click", "bulk", "subject", "date", "body", "group_key", "auth", "images")
RULE_KINDS = {"sender": "发件人", "domain": "域名", "list": "邮件列表"}
SUGGESTION_FIELDS = ("action", "priority", "category", "summary", "reason", "due_date", "event_json", "reply_points_json",
                     "suspicious", "suggested_by", "model", "seconds", "analyzed_at")


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(CONFIG_DIR, "luoke.db")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.lock = threading.RLock()
        # the app and 螺壳主控's idle job (its own process) share the file: wait for the other's write, don't fail
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        # what is deleted (a mailbox removed, its mail and log) is overwritten, not left in the file's free pages
        # (top-level boundary audit, 2026-10-16: "removed" has to mean gone)
        self.db.execute("PRAGMA secure_delete=ON")
        with self.lock:
            self._migrate_rules()
            self._migrate_old_mail()
            self.db.executescript(SCHEMA)
            self._migrate_digest()
            self._migrate_items()
            self.db.commit()
            self._scrub_once()
            self._reread_verdicts_once()

    def _scrub_once(self) -> None:
        """Before 0.13.3 deletes left their text in free pages: the first start with secure_delete rebuilds the file
        once (VACUUM), so what was removed earlier is gone too. Marked with the file's user_version."""
        if self.db.execute("PRAGMA user_version").fetchone()[0] >= 1:
            return
        try:
            self.db.execute("VACUUM")
        except sqlite3.OperationalError as ex:            # another process has it open (螺壳主控's idle job): next start
            print(f"[luoke-mail] store: rebuilding the file postponed: {type(ex).__name__}", file=sys.stderr)
            return
        self.db.execute("PRAGMA user_version=1")

    def _reread_verdicts_once(self) -> None:
        """Before 0.19.10 a Gmail verdict on a subdomain sender (header.from names the organisation) and NetEase's
        own delivery were kept as "none": waiting mail has its verdict read again (App._recheck_senders). user_version 2."""
        if self.db.execute("PRAGMA user_version").fetchone()[0] != 1:
            return
        self.db.execute("UPDATE items SET auth=NULL WHERE auth='none' AND place='inbox' AND state='pending'")
        self.db.execute("PRAGMA user_version=2")
        self.db.commit()

    def _migrate_rules(self) -> None:
        """v0.2 rules had no account, so rules made on the demo inbox also matched the real mailbox.
        Old rules get account '' until App.adopt_rules() sorts them into demo / the real account."""
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(rules)")]
        if not cols or "account" in cols:
            return
        self.db.execute("ALTER TABLE rules RENAME TO rules_v02")
        self.db.executescript(SCHEMA)
        self.db.execute("""INSERT INTO rules (id, account, kind, value, action, label, created_at, hits)
                           SELECT id, '', kind, value, action, label, created_at, hits FROM rules_v02""")
        self.db.execute("DROP TABLE rules_v02")

    def _migrate_old_mail(self) -> None:
        """0.6.0 kept only archived mail, in a table called `archive`; inbox mail older than the sorting
        window is judged the same way now. Rows (and verdicts) carry over."""
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "cache" in tables:            # 0.5.0 kept the scanned old inbox mail in the cache; it lives in old_mail now
            self.db.execute("DELETE FROM cache WHERE name IN ('old_mail', 'old_keep', 'demo_archived')")
        if "archive" in tables and "old_mail" not in tables:
            self.db.execute("ALTER TABLE archive RENAME TO old_mail")
            self.db.execute("ALTER TABLE old_mail ADD COLUMN scope TEXT NOT NULL DEFAULT 'archive'")
            self.db.execute("UPDATE old_mail SET state='here' WHERE state='archived'")
            for was, now_ in (("archive_scan", "scan:archive"), ("judge", "judge:archive"), ("archive_keep", "keep:archive")):
                self.db.execute("UPDATE OR IGNORE cache SET name=? WHERE name=?", (now_, was))

    def _migrate_digest(self) -> None:
        """0.8 kept every piece scored 2+ as a pick; 0.9 has an editor choose the few (see editor.py), and the user
        can put a piece aside once read (归档)."""
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(digest_items)")]
        for col, kind in (("pick_week", "INTEGER DEFAULT 0"), ("pick_month", "INTEGER DEFAULT 0"), ("pick_why", "TEXT"),
                          ("archived_at", "TEXT")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE digest_items ADD COLUMN {col} {kind}")

    def _migrate_items(self) -> None:
        """0.13: the mailbox's own verdict on the sender (`auth`, see mailbox.sender_check); mail the user archived
        in the last days is sorted too (`place` 'archive', see App._recent_archived)."""
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(items)")]
        if "auth" not in cols:
            self.db.execute("ALTER TABLE items ADD COLUMN auth TEXT")
        if "place" not in cols:
            self.db.execute("ALTER TABLE items ADD COLUMN place TEXT NOT NULL DEFAULT 'inbox'")
        if "read_by_us" not in cols:        # 0.15: marked read by the mail assistant once sorted (ADR 0016), not by the user
            self.db.execute("ALTER TABLE items ADD COLUMN read_by_us TEXT")
        if "older" not in cols:             # 0.16: unread mail from before the range, sorted as idle work (App._older_slice)
            self.db.execute("ALTER TABLE items ADD COLUMN older INTEGER DEFAULT 0")
        if "images" not in cols:            # 0.19: pictures in the body, whose words the model can't read (#42)
            self.db.execute("ALTER TABLE items ADD COLUMN images INTEGER DEFAULT 0")

    # ---- items -----------------------------------------------------------------------------
    def settle_dates(self, settle) -> int:
        """Run `settle(result, mail)` (triage.settle_dates) over every item's due date and event, saving what it
        moved. Mail sorted before 0.17.10 had its dates counted from the day it was sorted, not sent. Returns the count."""
        moved = 0
        with self.lock:
            rows = self.db.execute("SELECT id, date, subject, body, due_date, event_json FROM items "
                                   "WHERE due_date IS NOT NULL OR event_json IS NOT NULL").fetchall()
            for r in rows:
                event = json.loads(r["event_json"]) if r["event_json"] else None
                was = {"due_date": r["due_date"], "event": event}
                now = settle({"due_date": r["due_date"], "event": dict(event) if event else None},
                             {"date": r["date"], "subject": r["subject"], "body": r["body"]})
                if now != was:
                    self.db.execute("UPDATE items SET due_date=?, event_json=? WHERE id=?",
                                    (now["due_date"], json.dumps(now["event"], ensure_ascii=False) if now["event"] else None, r["id"]))
                    moved += 1
            if moved:
                self.db.commit()
        return moved

    def upsert_message(self, account: str, m: dict) -> tuple[int, bool]:
        """Insert a fetched message, or refresh its location/headers. Returns (id, is_new)."""
        mid = m.get("message_id") or f"<uid-{m.get('uid')}@{account}>"
        with self.lock:
            self.db.execute("UPDATE OR IGNORE items SET message_id=? WHERE account=? AND message_id=?",
                            (mid, account, f"<uid-{m.get('uid')}@{account}>"))     # read before messages had their own identity
            row = self.db.execute("SELECT id, place FROM items WHERE account=? AND message_id=?", (account, mid)).fetchone()
            vals = {k: m.get(k) for k in MESSAGE_FIELDS}
            vals["place"] = m.get("place") or "inbox"          # seen in the inbox now: it is an inbox item
            vals.update(to_json=json.dumps(m.get("to") or []), cc_json=json.dumps(m.get("cc") or []),
                        attachments_json=json.dumps(m.get("attachments") or []))
            if row:
                if row["place"] == "archive" and vals["place"] == "inbox":     # the user moved it back: an inbox item again
                    vals.update(state="pending", done_action=None, done_at=None)
                sets = ", ".join(f"{k}=?" for k in vals)
                self.db.execute(f"UPDATE items SET {sets} WHERE id=?", (*vals.values(), row["id"]))
                self.db.commit()
                return row["id"], False
            vals["older"] = int(bool(m.get("older")))          # set once, when it comes in
            cols = ", ".join(("account", "message_id", "fetched_at", *vals))
            marks = ", ".join("?" * (3 + len(vals)))
            cur = self.db.execute(f"INSERT INTO items ({cols}) VALUES ({marks})", (account, mid, now(), *vals.values()))
            self.db.commit()
            return cur.lastrowid, True

    def set_suggestion(self, item_id: int, s: dict, source: str, model: str | None = None) -> None:
        with self.lock:
            self.db.execute(
                """UPDATE items SET action=?, priority=?, category=?, summary=?, reason=?, due_date=?, event_json=?,
                   reply_points_json=?, suspicious=?, suggested_by=?, model=?, seconds=?, error=NULL, analyzed_at=?
                   WHERE id=?""",
                (s["action"], s.get("priority"), s.get("category"), s.get("summary"), s.get("reason"), s.get("due_date"),
                 json.dumps(s.get("event")) if s.get("event") else None, json.dumps(s.get("reply_points") or [], ensure_ascii=False),
                 int(bool(s.get("suspicious"))), source, model, s.get("seconds"), now(), item_id))
            self.db.commit()

    def set_error(self, item_id: int, err: str) -> None:
        with self.lock:
            self.db.execute("UPDATE items SET error=? WHERE id=?", (err[:300], item_id))
            self.db.commit()

    def unread_sorted(self, account: str) -> list[dict]:
        """Inbox mail sorted by the model and not yet marked read by the mail assistant."""
        with self.lock:
            rows = self.db.execute("SELECT * FROM items WHERE account=? AND place='inbox' AND state='pending' AND uid IS NOT NULL"
                                   " AND analyzed_at IS NOT NULL AND error IS NULL AND read_by_us IS NULL", (account,)).fetchall()
        return [self._item(r) for r in rows]

    def read_by_us(self, account: str) -> set[str]:
        """Message-IDs the mail assistant marked read and that are still so: their \\Seen says nothing of the user.
        One marked unread again ("unread") and then opened, the user opened."""
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT message_id FROM items WHERE account=? AND read_by_us IS NOT NULL"
                                                  " AND read_by_us<>'unread'", (account,))}

    def set_fields(self, item_id: int, **fields) -> None:
        with self.lock:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), item_id))
            self.db.commit()

    def set_state(self, ids: list[int], state: str, done_action: str | None = None) -> None:
        with self.lock:
            self.db.executemany("UPDATE items SET state=?, done_action=?, done_at=? WHERE id=?",
                                [(state, done_action, now() if state != "pending" else None, i) for i in ids])
            self.db.commit()

    def mark_missing(self, account: str, present_uids: set[str]) -> list[int]:
        """Pending items no longer in the inbox were handled elsewhere (phone, webmail): drop them. Returns their ids."""
        with self.lock:
            rows = self.db.execute("SELECT id, uid FROM items WHERE account=? AND state='pending'", (account,)).fetchall()
            gone = [r["id"] for r in rows if r["uid"] and r["uid"] not in present_uids]
            self.db.executemany("UPDATE items SET state='done', done_action='gone', done_at=? WHERE id=?", [(now(), i) for i in gone])
            self.db.commit()
        return gone

    # what moves mail out of the inbox; "gone" = it left the inbox on its own (handled on the phone)
    LEFT_INBOX = ("archive", "complete", "trash", "spam", "gone")

    def handled_but_present(self, account: str, present_uids: set[str]) -> list[dict]:
        """Items recorded as out of the inbox whose inbox UID is still there: the move didn't happen, or the
        user moved the mail back. The caller confirms by Message-ID before reopening them."""
        marks = ",".join("?" * len(self.LEFT_INBOX))
        with self.lock:
            rows = self.db.execute(f"SELECT id, uid, message_id FROM items WHERE account=? AND state='done' "
                                   f"AND done_action IN ({marks})", (account, *self.LEFT_INBOX)).fetchall()
        return [dict(r) for r in rows if r["uid"] and r["uid"] in present_uids]

    def mark_done_by_mid(self, account: str, message_ids: list[str], action: str) -> None:
        """Mail moved by 清理旧邮件 may also be on the list: it is handled now."""
        with self.lock:
            self.db.executemany("UPDATE items SET state='done', done_action=?, done_at=? WHERE account=? AND message_id=? "
                                "AND state='pending'", [(action, now(), account, m) for m in message_ids if m])
            self.db.commit()

    def reopen_by_mid(self, account: str, message_ids: list[str]) -> None:
        """Undoing 清理: mail it marked handled on the list is pending again."""
        with self.lock:
            self.db.executemany("UPDATE items SET state='pending', done_action=NULL, done_at=NULL WHERE account=? AND message_id=? "
                                "AND state='done' AND done_action IN ('archive', 'trash')", [(account, m) for m in message_ids if m])
            self.db.commit()

    def known_mids(self, account: str) -> set[str]:
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT message_id FROM items WHERE account=?", (account,))}

    def settle_archived(self, account: str, needs: tuple[str, ...], older_than: str) -> None:
        """Archived mail, once sorted: what needs nothing from the user (or looks suspicious -- the user put it away)
        is done; what is older than the window is done too. Only what needs the user stays on the page."""
        marks = ",".join("?" * len(needs))
        with self.lock:
            self.db.execute(f"UPDATE items SET state='done', done_action='quiet', done_at=? WHERE account=? AND place='archive' "
                            f"AND state='pending' AND action IS NOT NULL AND (action NOT IN ({marks}) OR suspicious=1)",
                            (now(), account, *needs))
            self.db.execute("UPDATE items SET state='done', done_action='expired', done_at=? WHERE account=? AND place='archive' "
                            "AND state='pending' AND date < ?", (now(), account, older_than))
            self.db.commit()

    def sender_seen(self, account: str, key: str, message_id: str | None) -> bool:
        """Other mail from this sender in this mailbox, never flagged: in the inbox and sorted, or among the older and
        archived mail read for 清理. `key` (`triage.seen_as`) is a registered domain -- mail from it or a host
        under it -- or a whole address, for a domain anyone can send from."""
        if not key or "." not in key:
            return False
        esc = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if "@" in key:
            match, args = "lower(from_addr) LIKE ? ESCAPE '\\'", (esc,)
        else:
            match, args = "(lower(from_addr) LIKE ? ESCAPE '\\' OR lower(from_addr) LIKE ? ESCAPE '\\')", (f"%@{esc}", f"%.{esc}")
        flagged = "SELECT message_id FROM items WHERE account=? AND suspicious=1 AND message_id IS NOT NULL"
        with self.lock:
            row = self.db.execute(
                f"SELECT 1 FROM items WHERE account=? AND message_id IS NOT ? AND suspicious=0 AND action IS NOT NULL AND {match} "
                f"UNION ALL SELECT 1 FROM old_mail WHERE account=? AND message_id IS NOT ? AND {match} "
                f"AND message_id NOT IN ({flagged}) LIMIT 1",          # read for 清理 once, flagged in the inbox before
                (account, message_id, *args, account, message_id, *args, account)).fetchone()
        return row is not None

    def auth_unchecked(self, account: str) -> list[dict]:
        """Inbox mail waiting for the user that was sorted without the provider's verdict on the sender (sorted
        before 0.13, or the verdict was not read): id, uid, message_id, suspicious."""
        with self.lock:
            rows = self.db.execute("SELECT id, uid, message_id, suspicious FROM items WHERE account=? AND place='inbox' "
                                   "AND state='pending' AND action IS NOT NULL AND auth IS NULL AND uid IS NOT NULL "
                                   "ORDER BY date DESC", (account,)).fetchall()
        return [dict(r) for r in rows]

    def set_auth(self, verdicts: dict[int, str], again: list[int]) -> None:
        """Store the verdicts ("none": the provider gave none); `again`: sort these once more (their suggestion
        goes, the next sync asks the model)."""
        with self.lock:
            self.db.executemany("UPDATE items SET auth=? WHERE id=?", [(v, i) for i, v in verdicts.items()])
            self.db.executemany("UPDATE items SET action=NULL, suggested_by=NULL, error=NULL WHERE id=? AND state='pending'",
                                [(i,) for i in again])
            self.db.commit()

    def known_uids(self, account: str) -> set[str]:
        """Inbox UIDs already in the local list: a sync reads only the bodies of the others."""
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT uid FROM items WHERE account=? AND uid IS NOT NULL", (account,))}

    # ---- Gmail's own id of a message: "在 Gmail 中打开" opens the message itself ----------------------------
    def gmail_ids_set(self, account: str, ids: dict[str, str]) -> None:
        """{Message-ID: Gmail id}; "" for mail no longer where it was looked for (not looked for again)."""
        with self.lock:
            self.db.executemany("INSERT INTO gmail_ids (account, message_id, gm_msgid) VALUES (?,?,?) ON CONFLICT(account, message_id)"
                                " DO UPDATE SET gm_msgid=excluded.gm_msgid WHERE excluded.gm_msgid<>''",
                                [(account, m, g or "") for m, g in ids.items() if m])
            self.db.commit()

    def gmail_id(self, account: str, message_id: str | None) -> str | None:
        with self.lock:
            r = self.db.execute("SELECT gm_msgid FROM gmail_ids WHERE account=? AND message_id=?", (account, message_id)).fetchone()
        return (r[0] or None) if r else None

    def gmail_ids_missing(self, account: str, inbox: set[str]) -> tuple[dict[str, dict[str, str]], list[str]]:
        """Mail with a 在 Gmail 中打开 link whose Gmail id was never looked for: ({folder: {uid: Message-ID}} -- on
        the list and still in the inbox (`inbox` UIDs), then what 精选 read, newest first; [Message-ID] -- 精选 mail
        read from the inbox that has left it since (archived): its inbox UID is no use any more). 清理 shows no
        links; its scan reads the ids anyway, at no cost."""
        out: dict[str, dict[str, str]] = {}
        moved: list[str] = []
        with self.lock:
            rows = self.db.execute(
                "SELECT 'INBOX', uid, message_id, 0, date FROM items WHERE account=? AND uid IS NOT NULL"
                " UNION ALL SELECT folder, uid, message_id, 1, date FROM digest_mail WHERE account=? AND uid IS NOT NULL",
                (account, account)).fetchall()
            known = {r[0] for r in self.db.execute("SELECT message_id FROM gmail_ids WHERE account=?", (account,))}
        for folder, uid, mid, digest, date in rows:
            if mid in known or not folder:
                continue
            if folder != "INBOX" or uid in inbox:
                out.setdefault(folder, {})[str(uid)] = mid
            elif digest:
                moved.append((date or "", mid))
        return out, [m for _, m in sorted(moved, reverse=True)]

    def get(self, item_id: int) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return self._item(row) if row else None

    def items(self, account: str, state: str | None = "pending", place: str | None = "inbox") -> list[dict]:
        """`place`: "inbox" (the list), "archive" (archived mail that still needs the user), None (both)."""
        q, args = "SELECT * FROM items WHERE account=?", [account]
        if state:
            q += " AND state=?"
            args.append(state)
        if place:
            q += " AND place=?"
            args.append(place)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY date DESC", args).fetchall()
        return [self._item(r) for r in rows]

    def unnamed(self, account: str, present: set[str]) -> list[dict]:
        """Mail in the inbox read before a message with no Date or Message-ID was given them (#41)."""
        with self.lock:
            rows = self.db.execute("SELECT id, uid, message_id, date FROM items WHERE account=? AND place='inbox' "
                                   "AND (date IS NULL OR message_id LIKE '<uid-%')", (account,)).fetchall()
        return [dict(r) for r in rows if r["uid"] in present]

    def renamed(self, item_id: int, message_id: str, date: str | None) -> None:
        """Its identity and date, worked out now. Still waiting for the user, it is sorted again: it was sorted not
        knowing when it was sent, and old bills were taken for fraud."""
        with self.lock:
            self.db.execute("UPDATE OR IGNORE items SET message_id=?, date=COALESCE(date, ?) WHERE id=?", (message_id, date, item_id))
            if date:
                self.db.execute("UPDATE items SET action=NULL, suggested_by=NULL, error=NULL, suspicious=0 "
                                "WHERE id=? AND state='pending' AND analyzed_at IS NOT NULL", (item_id,))
            self.db.commit()

    def mail_by_id(self, account: str, message_id: str) -> dict | None:
        """One message kept here: the sorted one with its body, or an old one with only its headers."""
        with self.lock:
            row = self.db.execute("SELECT * FROM items WHERE account=? AND message_id=?", (account, message_id)).fetchone()
            old = None if row else self.db.execute("SELECT * FROM old_mail WHERE account=? AND message_id=? AND state='here'",
                                                   (account, message_id)).fetchone()
        return self._item(row) if row else dict(old) if old else None

    def frequent_senders(self, account: str, since: str, least: int) -> list[str]:
        """Senders of at least `least` messages since then, not known to be bulk mail."""
        with self.lock:
            rows = self.db.execute("SELECT from_addr FROM items WHERE account=? AND date >= ? AND group_key IS NULL "
                                   "AND from_addr != '' GROUP BY from_addr HAVING COUNT(*) >= ?", (account, since, least)).fetchall()
        return [r[0] for r in rows]

    def group_known_senders(self, account: str) -> None:
        """New mail from a sender grouped before joins the group (it would otherwise wait for five more)."""
        with self.lock:
            self.db.execute("UPDATE items SET bulk=1, group_key='from:' || from_addr WHERE account=? AND group_key IS NULL "
                            "AND from_addr != '' AND from_addr IN (SELECT from_addr FROM items WHERE account=? "
                            "AND group_key = 'from:' || from_addr)", (account, account))
            self.db.commit()

    def group_sender(self, account: str, address: str) -> None:
        with self.lock:
            self.db.execute("UPDATE items SET bulk=1, group_key=? WHERE account=? AND from_addr=? AND group_key IS NULL",
                            ("from:" + address, account, address))
            self.db.commit()

    def search(self, account: str, words: list[str], since: str = "", until: str = "", limit: int = 20) -> list[dict]:
        """Mail kept here that has every word, newest first: recent mail by subject, sender and body, older mail
        by subject and sender (only its headers are here). Not what went to the trash or junk. `since` and `until`
        are in the stored form, "2026-10-03" or "2026-10-03 09:00"."""
        def has(fields: tuple[str, ...]) -> tuple[str, list[str]]:
            sql, args = "", []
            for w in words:
                like = "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                sql += " AND (" + " OR ".join(f"{f} LIKE ? ESCAPE '\\'" for f in fields) + ")"
                args += [like] * len(fields)
            if since:
                sql, args = sql + " AND date >= ?", args + [since]
            if until:
                sql, args = sql + " AND date <= ?", args + [until + " 99" if len(until) == 10 else until]
            return sql, args
        recent, a = has(("subject", "from_name", "from_addr", "body"))
        older, b = has(("subject", "from_name", "from_addr"))
        with self.lock:
            found = self.db.execute("SELECT message_id, from_name, from_addr, subject, date, body, summary, suspicious "
                                    "FROM items WHERE account=? AND COALESCE(done_action, '') NOT IN ('trash', 'spam')"
                                    + recent + " ORDER BY date DESC LIMIT ?", [account, *a, limit]).fetchall()
            old = self.db.execute("SELECT message_id, from_name, from_addr, subject, date FROM old_mail "
                                  "WHERE account=? AND state='here'" + older + " ORDER BY date DESC LIMIT ?",
                                  [account, *b, limit]).fetchall()
        out = [dict(r) for r in found]
        known = {r["message_id"] for r in out}
        out += [dict(r) for r in old if r["message_id"] not in known]
        return sorted(out, key=lambda r: r["date"] or "", reverse=True)[:limit]

    def count_unanalyzed(self, account: str) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM items WHERE account=? AND state='pending' AND action IS NULL"
                                   " AND older=0", (account,)).fetchone()[0]

    def unanalyzed(self, account: str, older: bool = False) -> list[dict]:
        """Waiting for the model: the range's mail (a sync), or the unread mail from before it (idle work)."""
        with self.lock:
            rows = self.db.execute("SELECT * FROM items WHERE account=? AND state='pending' AND action IS NULL "
                                   "AND older=? ORDER BY date DESC", (account, int(older))).fetchall()
        return [self._item(r) for r in rows]

    def group_precedent(self, account: str, group_key: str, exclude: list[int]) -> dict | None:
        """Latest model/rule judgement for this group from an earlier sync (for consistency)."""
        marks = ",".join("?" * len(exclude)) or "NULL"
        with self.lock:
            row = self.db.execute(
                f"""SELECT * FROM items WHERE account=? AND group_key=? AND action IS NOT NULL
                    AND id NOT IN ({marks}) ORDER BY analyzed_at DESC LIMIT 1""",
                (account, group_key, *exclude)).fetchone()
        return self._item(row) if row else None

    def suspicious_senders(self, account: str) -> set[str]:
        with self.lock:
            rows = self.db.execute("SELECT DISTINCT from_addr FROM items WHERE account=? AND suspicious=1", (account,)).fetchall()
        return {(r[0] or "").lower() for r in rows}

    @staticmethod
    def _item(row: sqlite3.Row) -> dict:
        d = dict(row)
        for k, default in (("to_json", []), ("cc_json", []), ("attachments_json", []), ("reply_points_json", []), ("event_json", None)):
            raw = d.pop(k)
            d[k[:-5]] = json.loads(raw) if raw else default
        d["suspicious"] = bool(d["suspicious"])
        d["bulk"] = bool(d["bulk"])
        d["flagged"] = bool(d["flagged"])
        return d

    def snapshot(self, ids: list[int]) -> list[dict]:
        """Suggestion fields of these items, so a rule or a correction can be undone exactly."""
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self.lock:
            rows = self.db.execute(f"SELECT id, state, {', '.join(SUGGESTION_FIELDS)} FROM items WHERE id IN ({marks})", ids).fetchall()
        return [dict(r) for r in rows]

    def restore(self, snap: list[dict]) -> None:
        with self.lock:
            for r in snap:
                fields = {k: r[k] for k in SUGGESTION_FIELDS}
                sets = ", ".join(f"{k}=?" for k in fields)
                self.db.execute(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), r["id"]))
            self.db.commit()

    # ---- senders (subscription decisions) and small caches --------------------------------
    def set_sender(self, account: str, key: str, status: str | None, name: str | None = None, detail: str | None = None) -> None:
        with self.lock:
            if status is None:
                self.db.execute("DELETE FROM senders WHERE account=? AND key=?", (account, key))
            else:
                self.db.execute("""INSERT INTO senders (account, key, name, status, at, detail) VALUES (?,?,?,?,?,?)
                                   ON CONFLICT(account, key) DO UPDATE SET name=excluded.name, status=excluded.status,
                                   at=excluded.at, detail=excluded.detail""", (account, key, name, status, now(), detail))
            self.db.commit()

    def sender(self, account: str, key: str) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM senders WHERE account=? AND key=?", (account, key)).fetchone()
        return dict(r) if r else None

    def senders(self, account: str) -> dict[str, dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM senders WHERE account=?", (account,)).fetchall()
        return {r["key"]: dict(r) for r in rows}

    def cache_set(self, account: str, name: str, value) -> None:
        with self.lock:
            self.db.execute("""INSERT INTO cache (account, name, value, at) VALUES (?,?,?,?)
                               ON CONFLICT(account, name) DO UPDATE SET value=excluded.value, at=excluded.at""",
                            (account, name, json.dumps(value, ensure_ascii=False), now()))
            self.db.commit()

    def cache_update(self, account: str, name: str, **fields) -> dict:
        """Merge fields into a cached dict in one step: a worker reporting its progress must not undo what the
        user set meanwhile (暂停 while a model call is under way)."""
        with self.lock:
            value = {**(self.cache_get(account, name)[0] or {}), **fields}
            self.cache_set(account, name, value)
            return value

    def cache_get(self, account: str, name: str):
        with self.lock:
            r = self.db.execute("SELECT value, at FROM cache WHERE account=? AND name=?", (account, name)).fetchone()
        return (json.loads(r["value"]), r["at"]) if r else (None, None)

    def has_items(self, account: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM items WHERE account=? LIMIT 1", (account,)).fetchone() is not None

    # ---- rules (per account: rules made on the demo inbox never touch the real mailbox) ---------
    def add_rule(self, account: str, kind: str, value: str, action: str, label: str | None = None) -> int:
        """Add a rule, or change the action of an existing one (its Gmail export is then out of date)."""
        if kind not in RULE_KINDS:
            raise ValueError("未知的规则类型")
        value = value.strip().lower()
        with self.lock:
            self.db.execute("""INSERT INTO rules (account, kind, value, action, label, created_at) VALUES (?, ?, ?, ?, ?, ?)
                               ON CONFLICT(account, kind, value) DO UPDATE SET action=excluded.action, label=excluded.label,
                               exported_at=CASE WHEN rules.action=excluded.action THEN rules.exported_at ELSE NULL END""",
                            (account, kind, value, action, label, now()))
            self.db.commit()
            return self.db.execute("SELECT id FROM rules WHERE account=? AND kind=? AND value=?",
                                   (account, kind, value)).fetchone()[0]

    def rule(self, rule_id: int) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        return dict(r) if r else None

    def rules(self, account: str) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM rules WHERE account=? ORDER BY created_at DESC, id DESC",
                                                     (account,)).fetchall()]

    def restore_rule(self, rule: dict, account: str) -> None:
        """Put a rule back exactly as it was (undo): same id, hits and export date."""
        with self.lock:
            self.db.execute("""INSERT OR REPLACE INTO rules (id, account, kind, value, action, label, created_at, hits, exported_at)
                               VALUES (?,?,?,?,?,?,?,?,?)""",
                            (rule["id"], rule.get("account") or account, rule["kind"], rule["value"], rule["action"],
                             rule.get("label"), rule.get("created_at") or now(), rule.get("hits") or 0, rule.get("exported_at")))
            self.db.commit()

    def delete_rule(self, rule_id: int) -> None:
        with self.lock:
            self.db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
            self.db.commit()

    def mark_exported(self, rule_ids: list[int]) -> str:
        at = now()
        with self.lock:
            self.db.executemany("UPDATE rules SET exported_at=? WHERE id=?", [(at, i) for i in rule_ids])
            self.db.commit()
        return at

    def adopt_rules(self, account: str | None, demo: str, demo_values: set[str]) -> None:
        """Sort rules migrated from v0.2 (account ''): demo senders go to the demo inbox, the rest to
        the connected mailbox (they wait until one is connected)."""
        with self.lock:
            marks = ",".join("?" * len(demo_values)) or "NULL"
            self.db.execute(f"UPDATE OR IGNORE rules SET account=? WHERE account='' AND value IN ({marks})", (demo, *demo_values))
            if account:
                self.db.execute("UPDATE OR IGNORE rules SET account=? WHERE account=''", (account,))
            self.db.commit()

    def match_rule(self, item: dict) -> dict | None:
        addr = (item.get("from_addr") or "").lower()
        domain = (item.get("from_domain") or "").lower()
        list_id = (item.get("list_id") or "").lower()
        for r in self.rules(item["account"]):
            v = r["value"]
            if (r["kind"] == "sender" and addr == v) or (r["kind"] == "list" and list_id and list_id == v) or \
               (r["kind"] == "domain" and domain and (domain == v or domain.endswith("." + v))):
                with self.lock:
                    self.db.execute("UPDATE rules SET hits=hits+1 WHERE id=?", (r["id"],))
                    self.db.commit()
                return r
        return None

    # ---- old mail (清理): inbox mail older than the sorting window ("inbox") and archived mail ("archive"),
    # one row per message, the model's verdict kept across scans ------------------------------------------------
    OLD_FIELDS = ("uid", "folder", "from_name", "from_addr", "list_id", "bulk", "subject", "date", "size", "seen",
                  "flagged", "category")

    def old_sync(self, account: str, scope: str, rows: list[dict], partial: bool = False) -> None:
        """Make one scope match a fresh scan: new mail is added unjudged, known mail keeps its verdict (headers,
        flags and location refreshed), mail no longer there is dropped. `partial`: the server left some out, so
        what isn't in the scan may still be there, and nothing is dropped."""
        with self.lock:
            seen = set()
            for r in rows:
                mid = r.get("message_id") or f"<uid-{r.get('uid')}@{account}>"
                self.db.execute("UPDATE OR IGNORE old_mail SET message_id=? WHERE account=? AND message_id=?",
                                (mid, account, f"<uid-{r.get('uid')}@{account}>"))    # keeps its verdict
                seen.add(mid)
                vals = {k: r.get(k) for k in self.OLD_FIELDS}
                vals["bulk"], vals["seen"], vals["flagged"] = int(bool(vals["bulk"])), int(bool(vals["seen"])), int(bool(vals["flagged"]))
                vals["labels_json"] = json.dumps(r.get("labels") or [], ensure_ascii=False)
                sets = ", ".join(f"{k}=excluded.{k}" for k in vals)
                self.db.execute(f"""INSERT INTO old_mail (account, message_id, scope, {', '.join(vals)})
                                    VALUES (?, ?, ?, {', '.join('?' * len(vals))})
                                    ON CONFLICT(account, message_id) DO UPDATE SET {sets}, scope=excluded.scope, state='here'""",
                                (account, mid, scope, *vals.values()))
            stale = [r[0] for r in self.db.execute("SELECT message_id FROM old_mail WHERE account=? AND scope=?", (account, scope))
                     if r[0] not in seen and not partial]
            self.db.executemany("DELETE FROM old_mail WHERE account=? AND message_id=?", [(account, m) for m in stale])
            self.db.commit()

    # old inbox mail on the list already (sorted from before the range): 收件箱 and 今日处理 have it, not 清理
    NOT_LISTED = " AND message_id NOT IN (SELECT message_id FROM items WHERE account=old_mail.account)"

    def old_read(self, account: str, scope: str) -> int:
        """How many of the scope's old messages the last reading found and are still where they were -- including
        those also listed in the inbox (the older unread, sorted there): what the sync's count counts too."""
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM old_mail WHERE account=? AND scope=? AND state='here'",
                                   (account, scope)).fetchone()[0]

    def old_rows(self, account: str, scope: str, state: str | None = "here") -> list[dict]:
        q, args = "SELECT * FROM old_mail WHERE account=? AND scope=?" + (self.NOT_LISTED if scope == "inbox" else ""), [account, scope]
        if state:
            q += " AND state=?"
            args.append(state)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["labels"] = json.loads(d.pop("labels_json") or "[]")
            d["bulk"], d["seen"], d["flagged"] = bool(d["bulk"]), bool(d["seen"]), bool(d["flagged"])
            out.append(d)
        return out

    def old_unjudged(self, account: str, scope: str, held=None) -> int:
        """Not starred, not judged yet -- and, given `held(labels)`, not kept by one of the user's labels either."""
        with self.lock:
            rows = self.db.execute("SELECT labels_json FROM old_mail WHERE account=? AND scope=? AND state='here' "
                                   "AND kind IS NULL AND flagged=0" + (self.NOT_LISTED if scope == "inbox" else ""),
                                   (account, scope)).fetchall()
        if held is None:
            return len(rows)
        return sum(1 for (labels,) in rows if not held({"labels": json.loads(labels or "[]")}))

    def old_judge(self, account: str, kinds: dict[str, str], model: str | None) -> None:
        with self.lock:
            self.db.executemany("UPDATE old_mail SET kind=?, judged_at=?, model=? WHERE account=? AND message_id=?",
                                [(k, now(), model, account, mid) for mid, k in kinds.items()])
            self.db.commit()

    def old_flag(self, account: str, message_ids: list[str], flagged: bool) -> None:
        with self.lock:
            self.db.executemany("UPDATE old_mail SET flagged=? WHERE account=? AND message_id=?",
                                [(int(flagged), account, m) for m in message_ids])
            self.db.commit()

    def old_set_state(self, account: str, message_ids: list[str], state: str) -> None:
        """'here' (where it was scanned), 'moved' (archived out of the inbox) or 'trashed'."""
        with self.lock:
            self.db.executemany("UPDATE old_mail SET state=? WHERE account=? AND message_id=?", [(state, account, m) for m in message_ids])
            self.db.commit()

    # ---- 订阅精选: sources, the mail the model reads, the pieces it found --------------------------------------
    def digest_sources(self, account: str) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM digest_sources WHERE account=? ORDER BY added_at", (account,))]

    def digest_add_source(self, account: str, key: str, name: str | None, origin: str) -> bool:
        with self.lock:
            cur = self.db.execute("INSERT OR IGNORE INTO digest_sources (account, key, name, origin, added_at) VALUES (?,?,?,?,?)",
                                  (account, key, name, origin, now()))
            self.db.commit()
            return cur.rowcount > 0

    def digest_remove_source(self, account: str, key: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM digest_sources WHERE account=? AND key=?", (account, key))
            self.db.execute("DELETE FROM digest_mail WHERE account=? AND source_key=? AND state='queued'", (account, key))
            self.db.commit()

    def digest_queue(self, account: str, rows: list[dict], run: str) -> int:
        """Mail for the model to read. Mail already read (or queued) is not read again."""
        with self.lock:
            n = 0
            for r in rows:
                cur = self.db.execute(
                    """INSERT OR IGNORE INTO digest_mail (account, message_id, source_key, name, folder, uid, subject, date, run, queued_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (account, r["message_id"], r.get("source_key"), r.get("name"), r.get("folder"), r.get("uid"),
                     r.get("subject"), r.get("date"), run, now()))
                n += cur.rowcount
            self.db.commit()
            return n

    def digest_next(self, account: str, limit: int = 5) -> list[dict]:
        """This week's mail first (it is what the weekly digest needs), then the newest of the backlog."""
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM digest_mail WHERE account=? AND state='queued' ORDER BY run='weekly' DESC, date DESC LIMIT ?",
                (account, limit))]

    def digest_read(self, account: str, message_id: str, items: list[dict] | None, model: str | None, error: str | None = None) -> None:
        with self.lock:
            m = self.db.execute("SELECT * FROM digest_mail WHERE account=? AND message_id=?", (account, message_id)).fetchone()
            if not m or m["state"] != "queued":           # read once: a second reader's result is dropped
                return
            for it in items or []:
                self.db.execute(
                    """INSERT INTO digest_items (account, message_id, title, summary, url, host, topics_json, lasting, score, why,
                       date, source, run, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (account, message_id, it["title"], it["summary"], it.get("url"), it.get("host"),
                     json.dumps(it.get("topics") or [], ensure_ascii=False), int(bool(it["lasting"])), it["score"], it.get("why"),
                     m["date"], m["name"], m["run"], now()))
            self.db.execute("UPDATE digest_mail SET state=?, read_at=?, model=?, error=? WHERE account=? AND message_id=?",
                            ("failed" if items is None else "read", now(), model, error, account, message_id))
            self.db.commit()

    def digest_mail_counts(self, account: str, message_ids: list[str] | None = None) -> dict[str, int]:
        q, args = "SELECT state, COUNT(*) FROM digest_mail WHERE account=?", [account]
        if message_ids is not None:
            q += f" AND message_id IN ({','.join('?' * len(message_ids))})"
            args += message_ids
        with self.lock:
            return dict(self.db.execute(q + " GROUP BY state", args).fetchall())

    def digest_unread_dates(self, account: str) -> set[str]:
        """Dates (YYYY-MM-DD) of mail still waiting to be read: the editor leaves those periods until they are read."""
        with self.lock:
            return {r[0][:10] for r in self.db.execute(
                "SELECT DISTINCT substr(date, 1, 10) FROM digest_mail WHERE account=? AND state='queued' AND date IS NOT NULL",
                (account,))}

    def digest_states(self, account: str) -> dict[str, str]:
        with self.lock:
            return dict(self.db.execute("SELECT message_id, state FROM digest_mail WHERE account=?", (account,)).fetchall())

    def digest_items(self, account: str, since: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM digest_items WHERE account=?", [account]
        if since:
            q += " AND date>=?"
            args.append(since)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY date DESC, id", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["topics"] = json.loads(d.pop("topics_json") or "[]")
            d["lasting"] = bool(d["lasting"])
            out.append(d)
        return out

    def digest_candidates(self, account: str, since: str | None = None, before: str | None = None,
                          lasting: bool = False) -> list[dict]:
        """Only what could be shown: saved (收藏), or scored 2+ and not marked 没兴趣 (and lasting, for older mail)."""
        q = ("SELECT * FROM digest_items WHERE account=? AND (feedback='useful' OR (score>=2 AND "
             "(feedback IS NULL OR feedback<>'dismissed')" + (" AND lasting=1" if lasting else "") + "))")
        args: list = [account]
        if since:
            q += " AND date>=?"
            args.append(since)
        if before:
            q += " AND date<?"
            args.append(before)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["topics"] = json.loads(d.pop("topics_json") or "[]")
            d["lasting"] = bool(d["lasting"])
            out.append(d)
        return out

    def digest_set_picks(self, account: str, column: str, ids_scope: list[int], picked: dict[int, str]) -> None:
        """One editor result: among `ids_scope`, exactly the ids in `picked` (with why) are chosen."""
        assert column in ("pick_week", "pick_month")
        with self.lock:
            self.db.executemany(f"UPDATE digest_items SET {column}=0 WHERE account=? AND id=?", [(account, i) for i in ids_scope])
            self.db.executemany(f"UPDATE digest_items SET {column}=1, pick_why=COALESCE(NULLIF(?, ''), pick_why) WHERE account=? AND id=?",
                                [(why, account, i) for i, why in picked.items()])
            self.db.commit()

    def digest_picked(self, account: str, column: str, since: str | None = None, before: str | None = None) -> list[dict]:
        assert column in ("pick_week", "pick_month")
        q, args = f"SELECT * FROM digest_items WHERE account=? AND {column}=1", [account]
        if since:
            q += " AND date>=?"
            args.append(since)
        if before:
            q += " AND date<?"
            args.append(before)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY date DESC", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["topics"] = json.loads(d.pop("topics_json") or "[]")
            d["lasting"] = bool(d["lasting"])
            out.append(d)
        return out

    def digest_months(self, account: str, before: str) -> dict[str, int]:
        """Months with something an editor could choose from (lasting, 2+), and how many candidates each has."""
        with self.lock:
            return dict(self.db.execute(
                "SELECT substr(date, 1, 7), COUNT(*) FROM digest_items WHERE account=? AND date<? AND lasting=1 AND "
                "(feedback='useful' OR (score>=2 AND (feedback IS NULL OR feedback<>'dismissed'))) GROUP BY 1",
                (account, before)).fetchall())

    def digest_counts(self, account: str, since: str) -> tuple[int, int]:
        """(pieces from mail since `since`, pieces from older mail)."""
        with self.lock:
            r = self.db.execute("SELECT SUM(date>=?), SUM(date<? OR date IS NULL) FROM digest_items WHERE account=?",
                                (since, since, account)).fetchone()
        return int(r[0] or 0), int(r[1] or 0)

    def digest_saved(self, account: str, limit: int = 300) -> list[dict]:
        """收藏: what the user saved, newest first, however old."""
        return self._digest_rows("SELECT * FROM digest_items WHERE account=? AND feedback='useful' ORDER BY date DESC, id DESC LIMIT ?",
                                 (account, limit))

    def digest_archived(self, account: str, limit: int = 300) -> list[dict]:
        """归档: what the user read and put aside, the most recently put aside first."""
        return self._digest_rows("SELECT * FROM digest_items WHERE account=? AND archived_at IS NOT NULL"
                                 " ORDER BY archived_at DESC, id DESC LIMIT ?", (account, limit))

    def _digest_rows(self, q: str, args: tuple) -> list[dict]:
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["topics"] = json.loads(d.pop("topics_json") or "[]")
            d["lasting"] = bool(d["lasting"])
            out.append(d)
        return out

    def digest_sender(self, account: str, message_id: str) -> str | None:
        """The sender key (list:/from:) of the newsletter a 精选 piece came from."""
        with self.lock:
            r = self.db.execute("SELECT source_key FROM digest_mail WHERE account=? AND message_id=?", (account, message_id)).fetchone()
        return r[0] if r else None

    def digest_item(self, item_id: int) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM digest_items WHERE id=?", (item_id,)).fetchone()
        return dict(r) if r else None

    def digest_set(self, item_id: int, **fields) -> None:
        with self.lock:
            self.db.execute(f"UPDATE digest_items SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), item_id))
            self.db.commit()

    # ---- 了解你: what the mail suggests about the user, before it is written to the memory ---------------------
    def learn_add(self, account: str, source: str, topic: str, text: str, evidence: list[dict]) -> dict:
        """The same fact seen again only gains evidence (one entry per message or sender)."""
        norm = "".join(text.lower().split())
        with self.lock:
            row = self.db.execute("SELECT * FROM learn_candidates WHERE account=? AND source=? AND topic=? AND norm=?",
                                  (account, source, topic, norm)).fetchone()
            ev = json.loads(row["evidence_json"]) if row else []
            refs = {e["ref"] for e in ev}
            ev += [e for e in evidence if e["ref"] not in refs]
            self.db.execute("""INSERT INTO learn_candidates (account, source, topic, norm, text, evidence_json, updated_at)
                               VALUES (?,?,?,?,?,?,?) ON CONFLICT(account, source, topic, norm)
                               DO UPDATE SET evidence_json=excluded.evidence_json, updated_at=excluded.updated_at""",
                            (account, source, topic, norm, text, json.dumps(ev, ensure_ascii=False), now()))
            self.db.commit()
        return self.learn_get(account, source, topic, norm)

    def learn_get(self, account: str, source: str, topic: str, norm: str) -> dict:
        with self.lock:
            r = self.db.execute("SELECT * FROM learn_candidates WHERE account=? AND source=? AND topic=? AND norm=?",
                                (account, source, topic, norm)).fetchone()
        return self._candidate(r) if r else None

    @staticmethod
    def _candidate(r) -> dict:
        d = dict(r)
        d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
        return d

    # ---- plans offered through 螺壳 (luoke-agent): what each card proposes, and what was done ------------------
    def plan_get(self, plan_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM agent_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["entries"], d["logs"] = json.loads(d.pop("entries_json")), json.loads(d.pop("logs_json") or "[]")
        return d

    def plan_offer(self, plan_id: str, account: str, kind: str, entries: dict) -> dict:
        """The plan for this set of messages: made once, the same one again while the set stays the same."""
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO agent_plans (plan_id, account, kind, entries_json, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (plan_id, account, kind, json.dumps(entries, ensure_ascii=False), now(), now()))
        return self.plan_get(plan_id)

    def plan_set(self, plan_id: str, **fields) -> None:
        if "logs" in fields:
            fields["logs_json"] = json.dumps(fields.pop("logs"))
        fields["updated_at"] = now()
        with self.lock, self.db:
            self.db.execute(f"UPDATE agent_plans SET {', '.join(f'{k}=?' for k in fields)} WHERE plan_id=?",
                            (*fields.values(), plan_id))

    def plans_open(self, account: str, kind: str) -> list[str]:
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT plan_id FROM agent_plans WHERE account=? AND kind=? AND state='open'",
                                                  (account, kind))]

    def learn_candidates(self, account: str, source: str | None = None, state: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM learn_candidates WHERE account=?", [account]
        for col, val in (("source", source), ("state", state)):
            if val:
                q += f" AND {col}=?"
                args.append(val)
        with self.lock:
            return [self._candidate(r) for r in self.db.execute(q + " ORDER BY updated_at", args)]

    def learn_set(self, account: str, source: str, topic: str, norm: str, **fields) -> None:
        with self.lock:
            self.db.execute(f"UPDATE learn_candidates SET {', '.join(f'{k}=?' for k in fields)} "
                            "WHERE account=? AND source=? AND topic=? AND norm=?", (*fields.values(), account, source, topic, norm))
            self.db.commit()

    # ---- action log --------------------------------------------------------------------------
    def log(self, account: str, action: str, item_ids: list[int], ok: bool, detail: str, undo: list | None = None) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO log (ts, account, action, item_ids, ok, detail, undo_json) VALUES (?,?,?,?,?,?,?)",
                                  (now(), account, action, json.dumps(item_ids), int(ok), detail,
                                   json.dumps(undo, ensure_ascii=False) if undo else None))
            self.db.commit()
            return cur.lastrowid

    def log_batch(self, log_id: int, batch: int, steps: list) -> None:
        """One batch's undo steps of a long operation, saved before and after the batch is carried out (a step for
        a message that did not move finds nothing to take back). Read with the entry's own (log_entry)."""
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO log_steps (log_id, batch, steps) VALUES (?,?,?)",
                            (log_id, batch, json.dumps(steps, ensure_ascii=False)))
            self.db.commit()

    def log_settle(self, log_id: int, ok: bool, detail: str) -> None:
        with self.lock:
            self.db.execute("UPDATE log SET ok=?, detail=? WHERE id=?", (int(ok), detail, log_id))
            self.db.commit()

    def log_entries(self, account: str, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT log.*, EXISTS(SELECT 1 FROM log_steps s WHERE s.log_id=log.id) AS batched "
                                   "FROM log WHERE account=? ORDER BY id DESC LIMIT ?", (account, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["item_ids"] = json.loads(d["item_ids"] or "[]")
            d["undoable"] = bool(d.pop("undo_json") or d.pop("batched")) and not d["undone"]
            d.pop("batched", None)
            out.append(d)
        # one message: which one, so 「已改为「回复」」 says of what (luoke-mail#27)
        one = {d["item_ids"][0] for d in out if len(d["item_ids"]) == 1}
        if one:
            marks = ",".join("?" * len(one))
            with self.lock:
                found = {r["id"]: r for r in self.db.execute(
                    f"SELECT id, subject, from_name, from_addr FROM items WHERE id IN ({marks})", list(one))}
            for d in out:
                r = found.get(d["item_ids"][0]) if len(d["item_ids"]) == 1 else None
                if r:
                    d["mail"] = {"subject": r["subject"] or "（无主题）", "from": r["from_name"] or r["from_addr"] or ""}
        return out

    def log_entry(self, log_id: int) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM log WHERE id=?", (log_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["item_ids"] = json.loads(d["item_ids"] or "[]")
        undo = json.loads(d.pop("undo_json")) if d["undo_json"] else []
        with self.lock:
            for b in self.db.execute("SELECT steps FROM log_steps WHERE log_id=? ORDER BY batch", (log_id,)):
                undo += json.loads(b["steps"])
        d["undo"] = undo or None
        return d

    def mark_undone(self, log_id: int) -> None:
        with self.lock:
            self.db.execute("UPDATE log SET undone=1 WHERE id=?", (log_id,))
            self.db.commit()

    # ---- housekeeping ------------------------------------------------------------------------
    def close(self) -> None:
        with self.lock:
            self.db.close()

    KEPT_ON_REPLAY = ("read", "digest_note", "learn")    # the demo inbox replays; what it has read and learnt stays

    def clear_account(self, account: str, keep_digest: bool = False) -> None:
        """Everything of one account; `keep_digest` (the demo replaying its inbox) keeps 订阅精选 and 了解你."""
        tables = ("items", "log", "senders", "old_mail", "gmail_ids") + (
            () if keep_digest else ("cache", "digest_sources", "digest_mail", "digest_items", "learn_candidates",
                                    "rules", "agent_plans"))       # a mailbox removed: its rules don't come back with it
        with self.lock:
            self.db.execute("DELETE FROM log_steps WHERE log_id IN (SELECT id FROM log WHERE account=?)", (account,))
            for table in tables:
                self.db.execute(f"DELETE FROM {table} WHERE account=?", (account,))
            if keep_digest:
                self.db.execute(f"DELETE FROM cache WHERE account=? AND name NOT IN ({','.join('?' * len(self.KEPT_ON_REPLAY))})",
                                (account, *self.KEPT_ON_REPLAY))
            self.db.commit()
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")      # (a rollback journal today; if it ever is WAL)

    def wipe(self) -> None:
        with self.lock:
            self.db.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass
        self.__init__(self.path)
