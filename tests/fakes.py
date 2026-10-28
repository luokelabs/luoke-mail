r"""Test doubles: an IMAP server with folders and write operations, and an OpenAI-compatible chat server.

FakeIMAP flavours:
- "gmail":   MOVE, UIDPLUS, SPECIAL-USE, X-GM-EXT-1; MOVE to All Mail = archive. Like the real Gmail
             (observed 2026-10-06), STORE -X-GM-LABELS (\Inbox) is answered OK and changes nothing.
- "qq":      no MOVE, no UIDPLUS; any extension command is recorded as a violation (real QQ hangs).
- "netease": UTF-7 Chinese folder names without special-use flags; requires ID before SELECT.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import socketserver
from email.utils import parsedate_to_datetime
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os

# Tests never reach this machine's own services -- the 螺壳主控 and model services in daily
# use: a hub socket that is never there, unless one is named on purpose.
os.environ.setdefault("LUOKE_HUB_SOCKET", "/nonexistent/luoke-hub-for-tests.sock")

FLAVOURS = {
    "gmail": {"caps": "IMAP4rev1 MOVE UIDPLUS SPECIAL-USE X-GM-EXT-1 ID",
              "folders": [("INBOX", ""), ("[Gmail]/All Mail", "\\All"), ("[Gmail]/Trash", "\\Trash"),
                          ("[Gmail]/Spam", "\\Junk"), ("[Gmail]/Drafts", "\\Drafts"), ("[Gmail]/Sent Mail", "\\Sent")]},
    "qq": {"caps": "IMAP4rev1",
           "folders": [("INBOX", ""), ("Deleted Messages", ""), ("Junk", ""), ("Drafts", ""), ("Sent Messages", "")]},
    "netease": {"caps": "IMAP4rev1 ID UIDPLUS",           # 已删除, 垃圾邮件, 草稿箱, 已发送
                "folders": [("INBOX", ""), ("&XfJSIJZk-", ""), ("&V4NXPpCuTvY-", ""), ("&g0l6P3ux-", ""), ("&XfJT0ZAB-", "")]},
}
def _msg_date(m: dict) -> dt.date | None:
    hit = re.search(rb"^Date:\s*(.+?)\r?$", m["raw"], re.M | re.I)
    try:
        return parsedate_to_datetime(hit.group(1).decode()).date() if hit else None
    except (TypeError, ValueError):
        return None


TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\([^)]*\)|\[[^\]]*\]|\{\d+\}|\S+')


def _unquote(t: str) -> str:
    return t[1:-1].replace('\\"', '"').replace("\\\\", "\\") if t.startswith('"') else t


class FakeIMAP:
    def __init__(self, messages: list[tuple[bytes, bool]], user="me@example.com", password="secret",
                 flavour: str = "qq", require_id: bool | None = None, ignore_moves: bool = False):
        f = FLAVOURS[flavour]
        self.flavour, self.user, self.password = flavour, user, password
        self.caps = f["caps"]
        self.require_id = flavour == "netease" if require_id is None else require_id
        self.special = {name: flag for name, flag in f["folders"]}
        self.folders: dict[str, list[dict]] = {name: [] for name, _ in f["folders"]}
        self.uidnext: dict[str, int] = {name: 1 for name in self.folders}
        self.commands: list[str] = []
        self.violations: list[str] = []
        self.ignore_moves = ignore_moves          # answer OK to MOVE / COPY and do nothing (a lying server)
        self.drop_after_move = 0                  # carry out the next N MOVE / COPY, then drop the connection unanswered
        self.drop_at_move = 0                     # let N-1 MOVE / COPY through, carry out the Nth and drop unanswered
        self.drop_body_at: set[int] = set()       # the Nth whole-message fetch (counting from 1) drops the connection
        self.body_fetches = 0
        self.label_stores: list[str] = []
        for raw, seen in messages:
            self._add("INBOX", raw, {"\\Seen"} if seen else set())
            if flavour == "gmail":
                self._add("[Gmail]/All Mail", raw, {"\\Seen"} if seen else set())
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def send(self, line: str):
                self.wfile.write(line.encode() + b"\r\n")

            def handle(self):
                state = {"auth": False, "id": False, "sel": None}
                self.send(f"* OK [CAPABILITY {outer.caps}] fake ready")
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode().rstrip("\r\n")
                    m = re.search(r"\{(\d+)\}$", line)
                    literal = None
                    if m:                                   # APPEND literal
                        self.send("+ go ahead")
                        literal = self.rfile.read(int(m.group(1)))
                        self.rfile.readline()
                    try:
                        outer._dispatch(self, line, state, literal)
                    except _Bye:
                        return

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]

    # ---- store helpers -------------------------------------------------------------------
    def _add(self, folder: str, raw: bytes, flags: set) -> int:
        self.folders.setdefault(folder, [])            # Gmail: copying to a label creates its folder view
        uid = self.uidnext.setdefault(folder, 1)
        self.uidnext[folder] += 1
        mid = re.search(rb"^Message-ID:\s*(\S+)", raw, re.M | re.I)
        self.folders[folder].append({"uid": uid, "raw": raw, "flags": set(flags),
                                     "mid": mid.group(1).decode() if mid else None})
        return uid

    @staticmethod
    def gm_msgid(m: dict) -> int:
        """Gmail's own id: one per message, whichever folder (label) it is seen in."""
        return 1_600_000_000_000_000_000 + int(hashlib.md5((m["mid"] or str(m["uid"])).encode()).hexdigest()[:12], 16)

    def subjects(self, folder: str) -> list[str]:
        import email
        import email.policy
        return [str(email.message_from_bytes(m["raw"], policy=email.policy.default).get("Subject", "")).strip()
                for m in self.folders[folder]]

    def find(self, folder: str, subject_part: str) -> dict | None:
        return next((m for m, s in zip(self.folders[folder], self.subjects(folder)) if subject_part in s), None)

    @staticmethod
    def _uids(spec: str) -> list[int]:
        out = []
        for part in spec.split(","):
            if ":" in part:
                a, b = part.split(":")
                out += list(range(int(a), int(b) + 1))
            else:
                out.append(int(part))
        return out

    def _msgs(self, folder, spec):
        want = set(self._uids(spec))
        return [m for m in self.folders[folder] if m["uid"] in want]

    # ---- protocol --------------------------------------------------------------------------
    def _dispatch(self, h, line: str, st: dict, literal: bytes | None):
        toks = TOKEN.findall(line)
        tag, cmd, args = toks[0], toks[1].upper(), toks[2:]
        name = cmd if cmd != "UID" else "UID " + args[0].upper()
        self.commands.append(name)
        caps = self.caps.split()
        if name in ("UID MOVE",) and "MOVE" not in caps or name == "UID EXPUNGE" and "UIDPLUS" not in caps or \
                name == "ID" and "ID" not in caps:
            self.violations.append(name)
            h.send(f"{tag} BAD Command!")
            return
        if cmd == "CAPABILITY":
            h.send(f"* CAPABILITY {self.caps}")
            return h.send(f"{tag} OK done")
        if cmd == "LOGIN":
            if (_unquote(args[0]), _unquote(args[1])) == (self.user, self.password):
                st["auth"] = True
                return h.send(f"{tag} OK LOGIN completed")
            return h.send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        if cmd == "LOGOUT":
            h.send("* BYE")
            h.send(f"{tag} OK LOGOUT completed")
            raise _Bye()
        if not st["auth"]:
            return h.send(f"{tag} BAD not logged in")
        if cmd == "ID":
            st["id"] = True
            h.send("* ID NIL")
            return h.send(f"{tag} OK ID completed")
        if cmd == "LIST":
            for fname in self.folders:
                flag = self.special.get(fname, "")
                h.send(f'* LIST (\\HasNoChildren{" " + flag if flag else ""}) "/" "{fname}"')
            return h.send(f"{tag} OK LIST completed")
        if cmd == "CREATE":
            fname = _unquote(args[0])
            self.folders.setdefault(fname, [])
            self.uidnext.setdefault(fname, 1)
            return h.send(f"{tag} OK CREATE completed")
        if cmd in ("SELECT", "EXAMINE"):
            fname = _unquote(args[0])
            if self.require_id and not st["id"]:
                return h.send(f"{tag} NO SELECT Unsafe Login. Please contact kefu@188.com for help")
            if fname not in self.folders:
                return h.send(f"{tag} NO no such folder")
            st["sel"], st["ro"] = fname, cmd == "EXAMINE"
            h.send(f"* {len(self.folders[fname])} EXISTS")
            h.send("* OK [UIDVALIDITY 7] ok")
            return h.send(f"{tag} OK [{'READ-ONLY' if cmd == 'EXAMINE' else 'READ-WRITE'}] {cmd} completed")
        if cmd == "APPEND":
            fname = _unquote(args[0])
            flags = set(args[1].strip("()").split()) if len(args) > 1 and args[1].startswith("(") else set()
            uid = self._add(fname, literal or b"", flags)
            return h.send(f"{tag} OK [APPENDUID 7 {uid}] APPEND completed")
        if cmd != "UID":
            return h.send(f"{tag} BAD unknown command")
        sub, rest = args[0].upper(), args[1:]
        folder = st["sel"]
        msgs = self.folders[folder]
        if sub == "SEARCH":
            text = " ".join(rest).upper()
            if "UNDELETED" in text:
                msgs = [m for m in msgs if "\\Deleted" not in m["flags"]]
            up = [t.upper() for t in rest]
            for key in ("SINCE", "BEFORE"):                   # by the Date header (good enough for a fake)
                if key in up:
                    day = dt.datetime.strptime(rest[up.index(key) + 1], "%d-%b-%Y").date()
                    msgs = [m for m in msgs if _msg_date(m) and (_msg_date(m) >= day if key == "SINCE" else _msg_date(m) < day)]
            if "X-GM-RAW" in up:                              # the bits of Gmail search the app uses
                q = _unquote(rest[up.index("X-GM-RAW") + 1])
                if "-in:inbox" in q:
                    msgs = [m for m in msgs if not self.find_mid("INBOX", m["mid"])]
                cat = re.search(r"category:(\w+)", q)
                if cat:
                    msgs = [m for m in msgs if m.get("category") == cat.group(1)]
            if "UID" in up:                                   # UID <set> criterion
                want_uids = set(self._uids(rest[up.index("UID") + 1]))
                msgs = [m for m in msgs if m["uid"] in want_uids]
            if "HEADER" in up or "FROM" in up:                 # HEADER <field> <value> | FROM <value>
                at = up.index("HEADER") if "HEADER" in up else up.index("FROM")
                field = _unquote(rest[at + 1]) if up[at] == "HEADER" else "From"
                value = _unquote(rest[at + 2] if up[at] == "HEADER" else rest[at + 1])
                want = value.lower()

                def header(m):
                    import email
                    import email.policy
                    v = str(email.message_from_bytes(m["raw"], policy=email.policy.default).get(field, "")).lower()
                    return v if field.lower() != "list-id" else v.strip("<>").split("<")[-1].rstrip(">")
                # like a real server: mail flagged \Deleted is found unless the search says UNDELETED (it was
                # filtered above then) -- a fake that always hid it covered up an undo that deleted both copies
                hits = [m["uid"] for m in msgs if
                        ((field.lower() == "message-id" and m["mid"] == value) or
                         (field.lower() != "message-id" and want in header(m)))]
                # what the real ones did (issue #9, 2026-10-21): QQ never finds a Message-ID by HEADER; 163 finds
                # one only when the HEADER key stands alone -- with UNDELETED or anything else, nothing
                if self.flavour == "qq" and field.lower() == "message-id":
                    hits = []
                if self.flavour == "netease" and field.lower() == "message-id" and len(rest) > 3:
                    hits = []
            elif "UNSEEN" in text:
                hits = [m["uid"] for m in msgs if "\\Seen" not in m["flags"]]
            else:
                hits = [m["uid"] for m in msgs]
            h.send("* SEARCH " + " ".join(map(str, hits)))
            return h.send(f"{tag} OK SEARCH completed")
        if sub == "FETCH":
            what = " ".join(rest[1:]).upper()
            if "BODY.PEEK[]" in what:                         # a network that stalls mid-way (luoke-mail#19)
                self.body_fetches += 1
                if self.body_fetches in self.drop_body_at:
                    raise _Bye()
            assert not re.search(r"BODY\[|\bRFC822\b(?!\.)", what.replace("BODY.PEEK[", "")), "must not set \\Seen while reading"
            gmail = "X-GM-EXT-1" in self.caps
            for m in self._msgs(folder, rest[0]):
                flags = " ".join(sorted(m["flags"]))
                extra = ""
                if "X-GM-LABELS" in what and gmail:
                    extra += " X-GM-LABELS (" + " ".join(f'"{x}"' for x in m.get("labels", [])) + ")"
                if "X-GM-MSGID" in what and gmail:
                    extra += f" X-GM-MSGID {self.gm_msgid(m)}"
                if "BODY.PEEK" not in what:
                    h.send(f"* {m['uid']} FETCH (UID {m['uid']}{extra})")
                    continue
                h.wfile.write(f"* {m['uid']} FETCH (UID {m['uid']} RFC822.SIZE {len(m['raw'])} FLAGS ({flags}){extra} BODY[] {{{len(m['raw'])}}}\r\n".encode()
                              + m["raw"] + b")\r\n")
            return h.send(f"{tag} OK FETCH completed")
        if st.get("ro"):
            return h.send(f"{tag} NO mailbox is read-only")
        if sub == "STORE":
            op, flagset = rest[1].upper(), set(rest[2].strip("()").split())
            for m in self._msgs(folder, rest[0]):
                if op.startswith("+FLAGS"):
                    m["flags"] |= flagset
                elif op.startswith("-FLAGS"):
                    m["flags"] -= flagset
                elif "X-GM-LABELS" in op:
                    self.label_stores.append(line)
                    names = [_unquote(x) for x in re.findall(r'"(?:[^"\\]|\\.)*"|\S+', rest[2].strip("()"))]
                    if any(n.lower() == "\\inbox" for n in names):
                        continue                             # what real Gmail did with \Inbox: OK, nothing changes
                    have = m.setdefault("labels", [])
                    for n in names:
                        if op.startswith("+") and n not in have:
                            have.append(n)
                        elif op.startswith("-") and n in have:
                            have.remove(n)
            return h.send(f"{tag} OK STORE completed")
        if sub in ("MOVE", "COPY"):
            dest = _unquote(rest[1])
            if self.ignore_moves:
                return h.send(f"{tag} OK {sub} completed")
            pairs = []
            for m in self._msgs(folder, rest[0]):
                if self.flavour == "gmail":
                    pairs.append((m["uid"], self._gmail_move(folder, m, dest, copy=sub == "COPY")))
                else:
                    pairs.append((m["uid"], self._add(dest, m["raw"], m["flags"])))
                    if sub == "MOVE":
                        msgs.remove(m)
            if self.drop_after_move > 0:                      # done on the server, but the client never hears back
                self.drop_after_move -= 1
                raise _Bye()
            if self.drop_at_move > 0:
                self.drop_at_move -= 1
                if self.drop_at_move == 0:
                    raise _Bye()
            if "UIDPLUS" in caps and pairs:                   # RFC 4315 / 6851: where each message went
                h.send(f"* OK [COPYUID 7 {','.join(str(a) for a, _ in pairs)} {','.join(str(b) for _, b in pairs)}] done")
            return h.send(f"{tag} OK {sub} completed")
        if sub == "EXPUNGE":
            for m in self._msgs(folder, rest[0]):
                if "\\Deleted" in m["flags"]:
                    msgs.remove(m)
            return h.send(f"{tag} OK EXPUNGE completed")
        return h.send(f"{tag} BAD unsupported")

    def _gmail_move(self, src: str, msg: dict, dest: str, copy: bool) -> int:
        """Gmail folders are labels: Trash/Spam strip every label; INBOX implies All Mail."""
        ALL, INBOX, TRASHY = "[Gmail]/All Mail", "INBOX", ("[Gmail]/Trash", "[Gmail]/Spam")
        mid = msg["mid"]

        def drop(folder):
            self.folders[folder] = [m for m in self.folders[folder] if m["mid"] != mid]

        def ensure(folder):
            have = self.find_mid(folder, mid)
            return have["uid"] if have else self._add(folder, msg["raw"], msg["flags"])
        if dest in TRASHY:
            if not copy:
                for f in (INBOX, ALL, src):
                    drop(f)
            return ensure(dest)
        if dest == INBOX:
            if src in TRASHY and not copy:
                drop(src)
            ensure(ALL)
            return ensure(INBOX)
        if dest == ALL:
            if not copy and src in TRASHY + (INBOX,):
                drop(src)
            return ensure(ALL)
        if not copy:
            drop(src)
        return ensure(dest)

    def find_mid(self, folder, mid):
        return next((m for m in self.folders[folder] if m["mid"] == mid), None)

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class _Bye(Exception):
    pass


class FakeChat:
    """OpenAI-compatible /v1 server. `reply(body) -> (status, content)` decides each answer."""

    def __init__(self, reply, models=("test-model",)):
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.endswith("/models"):
                    return self._json(200, {"data": [{"id": m} for m in models]})
                self._json(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                status, content = reply(body)
                if status != 200:
                    return self._json(status, {"error": content})
                self._json(200, {"choices": [{"message": {"content": content}}],
                                 "usage": {"prompt_tokens": 100, "completion_tokens": 40}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def suggestion(action="reply", **kw) -> str:
    base = {"category": "work", "priority": "high", "action": action, "summary": "同事要 Q3 数据", "reason": "明早汇报要用",
            "due_date": (dt.date.today() + dt.timedelta(days=30)).isoformat(), "event_title": None,   # not 快到期 "event_date": None, "event_start": None, "event_end": None,
            "event_location": None, "reply_points": ["今天下班前发"], "suspicious": False}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


GOOD = suggestion()


# ---- 螺壳主控's task layer (luoke-core V1 §2, contract 1) ---------------------------------------------------
HUB_TASKS = {"triage": 1, "draft": 1, "senders": 10, "cleanup": 25, "digest-read": 1, "digest-pick": 30,
             "learn-sent": 8, "learn-orgs": 10, "thread": 1}


class FakeHub:
    """The hub as luoke-mail sees it, over a Unix socket: `GET /tasks` and streamed generation with the
    contract's checks (who asks, the task is theirs, stream, items within the batch, characters within the
    limit). `reply(task, body, headers)` decides each answer: the content, or {"refuse": code, "retry_after": s,
    ...} before the stream starts, or {"fail": code} as an `luoke.error` event after part of an answer.
    `tasks` maps short task names to {"status", "params"} (default: every luoke-mail task available)."""

    STATUS = {"invalid_request": 400, "unknown_accessory": 403, "task_unavailable": 404, "unsupported": 404,
              "input_too_long": 413, "busy": 503, "paused": 503, "not_ready": 503, "needs_user": 503, "engine_failed": 503}

    def __init__(self, reply, tasks: dict | None = None, max_chars: int = 32000, combo: str = "omlx-0.7.0+gemma-4-e4b-4bit"):
        import os
        import tempfile
        self.requests: list[dict] = []
        self.warmups: list[dict] = []
        self.calls: list[tuple] = []          # resident / exiting
        self.version = "1"                    # X-Luoke-Tasks-Version: bump it when changing `tasks`
        self.luoke_mem = None                 # {"path", "version"}: the luoke_mem a hub with the memory carries
        self.model_name = None                # per task, for people to read (luoke-core 5e67d4b); older hubs: none
        self.loading = False                  # report loading the model before the answer
        self.tasks = tasks if tasks is not None else {
            t: {"status": "available", "params": {"max_chars": max_chars, "batch": b}} for t, b in HUB_TASKS.items()}
        self.path = os.path.join(tempfile.mkdtemp(prefix="hub", dir="/tmp"), "hub.sock")
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def address_string(self):
                return "unix"

            def _json(self, status, obj):
                data = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Luoke-Contract", "1")
                self.send_header("X-Luoke-Tasks-Version", outer.version)
                self.end_headers()
                self.wfile.write(data)

            def _refuse(self, code, message="", retry_after=None, **extra):
                self._json(outer.STATUS[code], {"code": code, "message": message, "retry_after": retry_after, **extra})

            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path != "/accessory/resident":
                    return self._json(404, {"code": "not_found"})
                outer.calls.append(("resident", body))
                self._json(200, {})

            def do_GET(self):
                if self.path != "/tasks":
                    return self._json(404, {"code": "not_found"})
                acc = self.headers.get("X-Luoke-Accessory") or ""
                if not acc.startswith("luoke-mail/"):
                    return self._refuse("unknown_accessory")
                self._json(200, {"contract": 1, "hub_version": "test", "tier": "128", "tasks_version": outer.version,
                                 "background": {"state": "running", "reasons": []},
                                 "tasks": {f"luoke-mail.{t}": {"kind": "generate", "main": t == "triage", "reason": None,
                                                               "progress": None, "slow": None, "combo": combo,
                                                               **({"model_name": outer.model_name} if outer.model_name else {}), **v}
                                           for t, v in outer.tasks.items()},
                                 **({"luoke_mem": outer.luoke_mem} if outer.luoke_mem else {})})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/accessory/exiting":
                    outer.calls.append(("exiting", body))
                    return self._json(200, {})
                if self.path == "/warmup":
                    outer.warmups.append(body)
                    return self._json(200, {"accepted": True, "lease_id": body.get("lease_id") or "lease-1", "expires_in": 60})
                m = re.match(r"^/tasks/luoke-mail\.([a-z-]+)/v1/chat/completions$", self.path)
                task = m.group(1) if m else None
                req = {"task": task, "priority": self.headers.get("X-Luoke-Priority"),
                       "battery": self.headers.get("X-Luoke-Allow-Battery") == "1",
                       "accessory": self.headers.get("X-Luoke-Accessory"), "body": body}
                outer.requests.append(req)
                if not (req["accessory"] or "").startswith("luoke-mail/"):
                    return self._refuse("unknown_accessory")
                if task not in outer.tasks:
                    return self._refuse("task_unavailable", f"没有任务 luoke-mail.{task}")
                if outer.tasks[task]["status"] != "available":
                    return self._refuse("task_unavailable", outer.tasks[task].get("reason") or outer.tasks[task]["status"])
                if body.get("stream") is not True or req["priority"] not in ("user-waiting", "background"):
                    return self._refuse("invalid_request")
                params = outer.tasks[task]["params"]
                chars = sum(len(x.get("content") or "") for x in body.get("messages") or [])
                if chars > params["max_chars"]:
                    return self._refuse("input_too_long", f"输入 {chars} 字符")
                if body.get("luoke_items", 1) > params["batch"]:
                    return self._refuse("invalid_request", "条目数超过批大小")
                out = reply(task, body, req)
                if isinstance(out, dict) and "refuse" in out:
                    extra = {k: v for k, v in out.items() if k not in ("refuse", "retry_after", "message")}
                    return self._refuse(out["refuse"], out.get("message", ""), out.get("retry_after"), **extra)
                self.send_response(200)
                self.send_header("X-Luoke-Tasks-Version", outer.version)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                w = lambda s: self.wfile.write(s.encode("utf-8"))  # noqa: E731
                if outer.loading:
                    w('event: luoke.progress\ndata: {"stage": "loading", "message": "", "eta": 5}\n\n')
                w('event: luoke.progress\ndata: {"stage": "running", "message": "", "eta": null}\n\n')
                if isinstance(out, dict) and "fail" in out:
                    w('data: ' + json.dumps({"model": combo, "choices": [{"delta": {"content": '{"act'}}]}) + "\n\n")
                    w("event: luoke.error\ndata: " + json.dumps({"code": out["fail"], "message": "x", "retry_after": out.get("retry_after")}) + "\n\n")
                    return
                half = len(out) // 2
                for piece in (out[:half], out[half:]):
                    w("data: " + json.dumps({"model": combo, "choices": [{"delta": {"content": piece}}]}, ensure_ascii=False) + "\n\n")
                w("data: " + json.dumps({"model": combo, "choices": [], "usage": {"prompt_tokens": chars // 2, "completion_tokens": 40}}) + "\n\n")
                w("data: [DONE]\n\n")

        class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = S(self.path, H)

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        import os
        import shutil
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(os.path.dirname(self.path), ignore_errors=True)
