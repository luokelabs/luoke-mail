"""IMAP access: read new mail, read headers for subscription stats, and the few write operations the
workbench can perform (move to archive / trash / junk, flags, append a draft).

Safety rules baked in here (see docs/DESIGN.md):
- Reading never changes anything: EXAMINE + BODY.PEEK.
- Writes happen only through the explicit methods below; nothing ever sends mail, permanently
  deletes, or EXPUNGEs a whole folder (only UID EXPUNGE of the exact UIDs we just flagged).
- Extension commands (MOVE, UID EXPUNGE, ID) are sent only when the server advertises them --
  QQ Mail hangs on commands it doesn't know.
"""
from __future__ import annotations

import base64
import datetime as dt
import email
import email.policy
import hashlib
import imaplib
import re
import socket
import ssl
import sys
import time
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from . import __version__
from .providers import PROVIDERS

# NetEase needs the ID command; imaplib doesn't know it, so register it (allowed after LOGIN).
imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))

MAX_FETCH_BYTES = 1_048_576   # partial fetch: enough for the text parts, skips most attachment bytes
BODY_CHAR_LIMIT = 6000        # keeps prefill bounded on small machines
HEADER_FIELDS = "FROM DATE SUBJECT LIST-ID LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST PRECEDENCE MESSAGE-ID"


class MailError(Exception):
    """User-facing error with a friendly Chinese message."""


class KeychainLocked(MailError):
    """The mailbox's password is there, but the keychain could not be read just now: try again later, quietly."""


def _refused(what: str, data) -> MailError:
    print(f"[luoke-mail] IMAP refused {what}", file=sys.stderr)      # not its reply: servers quote names and addresses
    return MailError(f"邮箱未能完成{what}，请稍后重试。")


# luoke-mail#40 (10-28): imap.139.com speaks TLS 1.2 with RSA key exchange only, and Python's default ciphers
# (3.10 on) are all forward-secret -- the handshake fails. Such a server is asked again, once, with Python's own
# list plus RSA+AESGCM: same security level, same certificate checks, only that host loses forward secrecy.
COMPAT_CIPHERS = "@SECLEVEL=2:ECDH+AESGCM:ECDH+CHACHA20:ECDH+AES:DHE+AES:RSA+AESGCM:!aNULL:!eNULL:!aDSS:!SHA1:!AESCCM"
_compat_hosts: set[str] = set()                 # hosts that needed it, this process: straight to it next time


def handshake_failed(exc: Exception) -> bool:
    return (isinstance(exc, ssl.SSLError) and not isinstance(exc, ssl.SSLCertVerificationError)
            and "handshake_failure" in f"{getattr(exc, 'reason', '')} {exc}".lower().replace(" ", "_"))


def compat_context() -> ssl.SSLContext | None:
    """The default context with RSA key exchange allowed; None where the TLS library takes no such list (the
    LibreSSL of macOS's own Python, whose defaults allow RSA already)."""
    ctx = ssl.create_default_context()
    try:
        ctx.set_ciphers(COMPAT_CIPHERS)
    except ssl.SSLError:
        return None
    return ctx


def friendly_error(exc: Exception, provider: str) -> MailError:
    msg = str(exc)
    low = msg.lower()
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return MailError("连接邮箱服务器超时，请检查网络后重试。")
    if handshake_failed(exc):
        print(f"[luoke-mail] TLS handshake failed: {type(exc).__name__}", file=sys.stderr)
        return MailError("无法和邮箱服务器建立加密连接：服务器的加密方式不受支持。请确认服务器地址和端口，或换用邮箱提供的其他 IMAP 地址。")
    if isinstance(exc, (socket.gaierror, ConnectionRefusedError, OSError)) and not isinstance(exc, imaplib.IMAP4.error):
        print(f"[luoke-mail] IMAP connection failed: {type(exc).__name__}", file=sys.stderr)
        return MailError("无法连接邮箱服务器，请检查网络或服务器地址。")
    if "unsafe login" in low:
        return MailError("网易邮箱拒绝登录。请确认已在网页版设置中开启 IMAP，并使用授权码登录。")
    if "webalert" in low or "web browser" in low:            # Gmail: a sign-in it found unusual, to confirm on the web
        return MailError("Google 拦下了这次登录：请在浏览器里打开这个 Gmail，按提示确认是你本人，再回来重试。")
    if any(k in low for k in ("authenticationfailed", "invalid credentials", "login fail", "authenticate failed",
                              "password error", "login error", "user name or password", "application-specific password")):
        print(f"[luoke-mail] IMAP login refused: {type(exc).__name__}", file=sys.stderr)
        return MailError(PROVIDERS.get(provider, PROVIDERS["custom"])["refused"])
    if "imap" in low and ("disabled" in low or "not enabled" in low or "service is not open" in low):
        return MailError("此邮箱未开启 IMAP 服务，请先在网页版设置中开启。")
    print(f"[luoke-mail] IMAP error: {type(exc).__name__}", file=sys.stderr)
    return MailError("邮箱暂时无响应，请稍后重试。如果问题持续，请检查网络，或在邮箱网页版中确认 IMAP 已开启。")


# ---- modified UTF-7 for mailbox names (RFC 3501 §5.1.3): "已删除" <-> "&XfJSIJZk-" ----------------
def utf7_decode(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        if s[i] == "&":
            j = s.index("-", i)
            if j == i + 1:
                out.append("&")
            else:
                b64 = s[i + 1:j].replace(",", "/")
                out.append(base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-16-be"))
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def utf7_encode(s: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append("&" + b + "-")
            buf.clear()
    for c in s:
        if 0x20 <= ord(c) <= 0x7E:
            flush()
            out.append("&-" if c == "&" else c)
        else:
            buf.append(c)
    flush()
    return "".join(out)


def quote_mailbox(name: str) -> str:
    enc = utf7_encode(name)
    return '"' + enc.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_quoted(value: str | None) -> str | None:
    """A search value as an IMAP quoted string (RFC 3501): backslash and double quote escaped. None for one that
    can't be searched for safely: empty (FROM "" matches every message) or with a line break or NUL in it (it
    would end the command and start another). The values come from mail headers, which the sender writes."""
    value = str(value or "")
    if not value.strip() or any(c in value for c in "\r\n\0") or not value.isascii():
        return None
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def same_mid(found: str | None, wanted: str | None) -> bool:
    return bool(found and wanted) and found.strip() == wanted.strip()


# a provider delivering mail itself, as the top Received header it adds says: (pattern, the sender's main domain it
# must come from, or None for any). NetEase passes its own notices inside its network (`trans-mta`, a private
# address) with no Authentication-Results; a user's mail to another may go that way too, so only netease.com counts.
PLATFORM_139 = (r"\(RichMail\).*with ODA", None)
PLATFORM_NETEASE = (r"\[(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.[\d.]+\]\) by [\w.-]*trans-mta[\w.-]* \(Coremail\)", "netease.com")


def derived_mid(raw: bytes) -> str:
    """The identity of a message with no Message-ID (139's mail nearly never has one): a hash
    of its header block. A message's bytes never change, so it stays the same when the message moves and its UID
    changes -- finding, undoing and not doing a thing twice work as they do for any other message."""
    block = re.split(rb"\r?\n\r?\n", raw, 1)[0].strip()
    return f"<nomid-{hashlib.sha256(block).hexdigest()[:32]}@luoke.local>"


def real_mid(message_id: str | None) -> bool:
    """A Message-ID the message itself has: one the mail assistant made up means nothing to anyone else."""
    return bool(message_id) and not message_id.startswith(("<nomid-", "<uid-"))


def _fetched(data) -> list[tuple[str, bytes]]:
    """A FETCH answer as (everything but the literal, the literal) per message: imaplib splits a response around
    its literal, and a server may put FLAGS or INTERNALDATE on either side of it."""
    out: list[tuple[str, bytes]] = []
    for part in data or []:
        if isinstance(part, tuple) and len(part) > 1:
            out.append((part[0].decode("utf-8", "replace"), part[1]))
        elif isinstance(part, bytes) and out:
            out[-1] = (out[-1][0] + " " + part.decode("utf-8", "replace"), out[-1][1])
    return out


def internal_date(meta: str) -> str | None:
    """When the server received the message, in the form dates are kept: what a message with no Date header has."""
    meta = re.sub(r'(INTERNALDATE ")\s*(\d)-', r"\g<1>0\2-", " ".join(meta.split()))    # 139 writes "5-Nov-2025"
    parsed = imaplib.Internaldate2tuple(meta.encode())
    return time.strftime("%Y-%m-%d %H:%M", parsed) if parsed else None


ROLE_LABEL = {"archive": "归档", "trash": "已删除", "junk": "垃圾邮件", "drafts": "草稿箱", "sent": "已发送"}
SPECIAL_USE = {"archive": ("\\all", "\\archive"), "trash": ("\\trash",), "junk": ("\\junk",), "drafts": ("\\drafts",),
               "sent": ("\\sent",)}
SPECIAL_NAMES = {
    "archive": ("archive", "archives", "归档", "已归档", "邮件归档", "all mail", "所有邮件"),
    "trash": ("trash", "deleted messages", "deleted items", "deleted", "已删除", "垃圾箱", "废纸篓", "已删除邮件"),
    "junk": ("junk", "spam", "junk e-mail", "junk email", "垃圾邮件", "广告邮件"),
    "drafts": ("drafts", "draft", "草稿箱", "草稿"),
    "sent": ("sent", "sent messages", "sent mail", "sent items", "已发送", "已发送邮件", "发件箱"),
}
_LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$')


def parse_list_line(line: bytes | str) -> dict | None:
    s = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
    m = _LIST_RE.match(s.strip())
    if not m:
        return None
    raw = m.group("name").strip()
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    delim = m.group("delim").strip('"') if m.group("delim") != "NIL" else ""
    return {"name": utf7_decode(raw), "flags": [f.lower() for f in m.group("flags").split()], "delim": delim}


def pick_folder(folders: list[dict], role: str) -> str | None:
    for f in folders:
        if any(flag in f["flags"] for flag in SPECIAL_USE[role]):
            return f["name"]
    names = SPECIAL_NAMES[role]
    leaf = lambda f: (f["name"].split(f["delim"])[-1] if f["delim"] else f["name"]).strip().lower()     # noqa: E731
    hits = [f for f in folders if leaf(f) in names and "\\noselect" not in f["flags"]]
    return min(hits, key=lambda f: names.index(leaf(f)))["name"] if hits else None      # the likeliest name first


def _guarded(fn):
    """Mailbox methods speak MailError (a sentence for the user), whatever imaplib or the socket raised: a dropped
    connection in the middle of a move must reach the caller as a mail problem, not as a crash."""
    import functools

    @functools.wraps(fn)
    def wrapper(self, *args, **kw):
        return self._guard(fn, self, *args, **kw)
    return wrapper


class Mailbox:
    REFETCH = (50, 10)        # batch sizes for what a server left out of a reply (_fetch)
    def __init__(self, provider: str, address: str, secret: str, host: str | None = None,
                 port: int | None = None, ssl: bool = True, timeout: float = 30):
        p = PROVIDERS.get(provider, PROVIDERS["custom"])
        self.provider = provider
        self.address = address
        # app passwords and authorization codes are letters: Gmail shows its own in groups of four, and a copy
        # from a web page can carry a full-width or non-breaking space
        self.secret = "".join(secret.split()) if provider != "custom" else secret
        self.host = host or p["host"]
        host = (self.host or "").lower()
        self.platform = PLATFORM_139 if host.endswith(".139.com") else \
            PLATFORM_NETEASE if host.endswith((".163.com", ".126.com")) else None
        self.port = int(port or p["port"])
        self.needs_id = p["needs_id"]
        self.ssl = ssl
        # 139 can take more than 30 seconds over a FETCH of headers on a slow day, and a timeout fails the whole round
        self.timeout = max(timeout, 90) if self.platform is PLATFORM_139 else timeout
        self.conn: imaplib.IMAP4 | None = None
        self.selected: tuple[str, bool] | None = None
        self._scanned: dict[str, dict[str, list[str]]] = {}     # folder -> Message-ID -> UIDs (_by_message_id)
        self.unreturned: set[str] = set()                       # asked for, and the server never gave them (_fetch)
        self._folders: list[dict] | None = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- session ---------------------------------------------------------------------------
    def open(self, readonly: bool = True) -> None:
        if not self.host:
            raise MailError("请填写 IMAP 服务器地址。")
        try:
            self.conn = self._connect()
            self.conn.login(self.address, self.secret)
            typ, caps = self.conn.capability()        # servers often advertise MOVE/UIDPLUS only after login
            if typ == "OK" and caps and caps[-1]:
                self.conn.capabilities = tuple(caps[-1].decode().upper().split())
            if self.needs_id:
                self.conn._simple_command("ID", f'("name" "luoke-mail" "version" "{__version__}" "vendor" "luoke")')
            self.select("INBOX", readonly=readonly)
        except Exception as e:  # noqa: BLE001 - mapped to a friendly message
            self.close()                              # whatever failed, the half-open connection goes
            if isinstance(e, MailError):
                raise
            raise friendly_error(e, self.provider) from e

    def _connect(self):
        if not self.ssl:
            return imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
        ctx = compat_context() if self.host in _compat_hosts else None
        try:
            return imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout, ssl_context=ctx)
        except ssl.SSLError as e:
            if ctx is not None or not handshake_failed(e):
                raise
            ctx = compat_context()
            if ctx is None:
                raise
            conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout, ssl_context=ctx)
            print(f"[luoke-mail] {self.host}: TLS without forward secrecy (the server offers RSA key exchange only)",
                  file=sys.stderr)
            _compat_hosts.add(self.host)
            return conn

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:  # noqa: BLE001 - a dropped connection: logout fails before closing the socket
                try:
                    self.conn.shutdown()
                except Exception:  # noqa: BLE001
                    pass
            self.conn = None

    def has(self, capability: str) -> bool:
        return capability.upper() in {c.upper() for c in (self.conn.capabilities or ())}

    @_guarded
    def select(self, folder: str, readonly: bool) -> None:
        if self.selected == (folder, readonly):
            return
        typ, data = self.conn.select(quote_mailbox(folder) if folder != "INBOX" else "INBOX", readonly=readonly)
        if typ != "OK":
            raise imaplib.IMAP4.error((data[0] or b"").decode(errors="replace"))
        self.selected = (folder, readonly)

    def _guard(self, fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except MailError:
            raise
        except Exception as e:  # noqa: BLE001
            raise friendly_error(e, self.provider) from e

    # ---- reading ---------------------------------------------------------------------------
    def _uid_search(self, criteria: list[str]) -> list[bytes]:
        typ, data = self.conn.uid("SEARCH", *criteria)
        if typ != "OK":
            raise MailError("邮箱搜索失败。")
        return (data[0] or b"").split()

    def stats(self) -> dict:
        def run():
            self.select("INBOX", readonly=True)
            return {"total": len(self._uid_search(["ALL"])), "unseen": len(self._uid_search(["UNSEEN"]))}
        return self._guard(run)

    def recent_uids(self, days: int | None, unseen_only: bool = False) -> list[str]:
        """Inbox UIDs of the last `days` days (read or not, unless asked), newest first."""
        def run():
            self.select("INBOX", readonly=True)
            criteria = ["UNDELETED"] + (["UNSEEN"] if unseen_only else [])
            if days:
                criteria += ["SINCE", _imap_date(days)]
            return [u.decode() for u in sorted(self._uid_search(criteria), key=int)[::-1]]
        return self._guard(run)

    def folder_uids(self, folder: str, days: int) -> list[str]:
        """UIDs in a folder from the last `days` days, newest first (read-only)."""
        def run():
            self.select(folder, readonly=True)
            return [u.decode() for u in sorted(self._uid_search(["UNDELETED", "SINCE", _imap_date(days)]), key=int)[::-1]]
        return self._guard(run)

    def count_before(self, days: int) -> int:
        """How much older mail still sits in the inbox (for 清理旧邮件)."""
        def run():
            self.select("INBOX", readonly=True)
            return len(self._uid_search(["UNDELETED", "BEFORE", _imap_date(days)]))
        return self._guard(run)

    def wrote_to(self, addresses: list[str]) -> set[str]:
        """Which of these the user has ever sent mail to. Without a sent folder to look in: all of them, so nobody
        the user may know is taken for a mailing."""
        def run():
            try:
                self.select(self.folder_for("sent"), readonly=True)
            except MailError:
                return set(addresses)
            return {a for a in addresses if imap_quoted(a) and self._uid_search(["TO", imap_quoted(a)])}
        return self._guard(run)

    def fetch_recent(self, limit: int = 20, unseen_only: bool = True, days: int | None = 7) -> list[dict]:
        return self.fetch_messages(self.recent_uids(days, unseen_only)[:int(limit)])

    def fetch_messages(self, uids: list[str], folder: str = "INBOX") -> list[dict]:
        """Full messages (the body capped) for these UIDs, without marking anything read. On Gmail also the
        message's own id there (`gm_msgid`), which opens it in Gmail directly."""
        def run():
            self.select(folder, readonly=True)
            gm = " X-GM-MSGID" if self.has("X-GM-EXT-1") else ""
            out = []
            for uid in uids:
                typ, data = self.conn.uid("FETCH", uid, f"(FLAGS INTERNALDATE{gm} BODY.PEEK[]<0.{MAX_FETCH_BYTES}>)")
                got = _fetched(data)
                if got and got[0][1]:
                    text, raw = got[0]
                    meta = text.encode("utf-8", "replace")
                    authserv = PROVIDERS.get(self.provider, {}).get("authserv")
                    try:
                        msg = parse_message(raw, authserv, self.platform)
                    except Exception as ex:  # noqa: BLE001 -- one malformed message must not stop the others (#29)
                        print(f"[luoke-mail] skipped a message that could not be read (UID {uid}): {type(ex).__name__}", file=sys.stderr)
                        continue
                    if authserv and not msg["auth"]:
                        msg["auth"] = "none"                 # looked: the provider gave no verdict (never asked again)
                    msg["uid"] = uid
                    msg["date"] = msg["date"] or internal_date(text)
                    msg["flagged"] = b"\\Flagged" in meta
                    hit = re.search(rb"X-GM-MSGID (\d+)", meta)
                    msg["gm_msgid"] = hit.group(1).decode() if hit else None
                    out.append(msg)
            return out
        return self._guard(run)

    def gmail_ids(self, folder: str, uids: list[str]) -> dict[str, str]:
        """{uid: Gmail's own id} for these UIDs (Gmail only; others get {}). A UID that is no longer in the
        folder is left out (UIDs are never reused)."""
        if not uids or not self.has("X-GM-EXT-1"):
            return {}

        def run():
            self.select(folder, readonly=True)
            out = {}
            for i in range(0, len(uids), 500):
                typ, data = self.conn.uid("FETCH", ",".join(uids[i:i + 500]), "(X-GM-MSGID)")
                for part in data or []:
                    meta = part[0] if isinstance(part, tuple) else part
                    if not isinstance(meta, bytes):
                        continue
                    uid, gm = re.search(rb"UID (\d+)", meta), re.search(rb"X-GM-MSGID (\d+)", meta)
                    if uid and gm:
                        out[uid.group(1).decode()] = gm.group(1).decode()
            return out
        return self._guard(run)

    def inbox_uids(self) -> set[str]:
        """UIDs currently in the inbox, so the local list can drop mail handled elsewhere (and take back
        mail recorded as handled that is still there). Mail flagged for deletion has left already."""
        def run():
            self.select("INBOX", readonly=True)
            return {u.decode() for u in self._uid_search(["UNDELETED"])}
        return self._guard(run)

    def unseen_uids(self) -> set[str]:
        """UIDs in the inbox not yet read -- to notice what the user read elsewhere (the phone, Gmail's page)."""
        def run():
            self.select("INBOX", readonly=True)
            return {u.decode() for u in self._uid_search(["UNSEEN", "UNDELETED"])}
        return self._guard(run)

    def everywhere_headers(self, days: int, limit: int = 3000, trash: bool = True) -> list[dict]:
        """Headers of the last `days` days of mail wherever it is now -- the inbox, the archive (Gmail's All Mail
        holds both; read once each) and, with `trash`, the trash: what was archived, by the user or by a rule, or deleted outside
        the mail assistant is learnt from too (top-level ADR 0012 amended: every message is learnt from, whatever
        happens to it after). Once each, by Message-ID. Never the spam folder."""
        arch = pick_folder(self.folders(), "archive")
        folders = ["INBOX"] + ([arch] if arch else [])         # Gmail's All Mail repeats the inbox: deduplicated below
        if trash:
            bin_ = pick_folder(self.folders(), "trash")
            folders += [bin_] if bin_ else []
        seen, out = set(), []
        for folder in folders:
            for h in self.fetch_headers(days=days, limit=limit, folder=folder):
                mid = h.get("message_id") or f"{folder}:{h.get('uid')}"
                if mid not in seen:
                    seen.add(mid)
                    out.append({**h, "folder": folder})       # where to read its text (Learner._reading)
        return out

    def _fetch(self, uids: list, what: str) -> list:
        """A FETCH of headers, however few the server brings back. 139 answers OK to a batch of 200 with 63-96 of
        them and a different number each time, all of them at 100, so what is missing is asked for again in smaller
        batches; what still doesn't come is in `unreturned`, because it is not mail that has gone."""
        uids = [u.decode() if isinstance(u, bytes) else u for u in uids]
        data = list(self.conn.uid("FETCH", ",".join(uids), what)[1] or [])

        def missing() -> list[str]:
            parts = _fetched(data)
            got = {m.group(1) for meta, _ in parts for m in [re.search(r"UID (\d+)", meta)] if m}
            return [] if len(parts) >= len(uids) or (parts and not got) else [u for u in uids if u not in got]
        for size in self.REFETCH:
            if len(uids) > size:
                left = missing()
                for i in range(0, len(left), size):
                    data += self.conn.uid("FETCH", ",".join(left[i:i + size]), what)[1] or []
        left = missing()
        if left:
            self.unreturned.update(left)
            print(f"[mailbox] {len(left)} of {len(uids)} messages not returned by the server", file=sys.stderr)
        return data

    def fetch_headers(self, days: int = 60, limit: int = 3000, before: bool = False, progress=None,
                      folder: str = "INBOX", recipients: bool = False) -> list[dict]:
        """Headers + flags of a folder's mail (no bodies), the inbox unless told: the last `days` days for
        subscription stats, or with `before` everything older, for 清理旧邮件. `progress(done, total)` after
        each batch. With `recipients`, To and Cc as well (`recipients`: who the user wrote to, from Sent)."""
        fields = HEADER_FIELDS + (" TO CC" if recipients else "")
        def run():
            self.select(folder, readonly=True)
            uids = sorted(self._uid_search(["UNDELETED", "BEFORE" if before else "SINCE", _imap_date(days)]), key=int)[-int(limit):]
            out = []
            for i in range(0, len(uids), 200):
                if progress:
                    progress(i, len(uids))
                data = self._fetch(uids[i:i + 200], f"(FLAGS RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS ({fields})])")
                for meta, header in _fetched(data):
                    try:
                        msg = email.message_from_bytes(header, policy=email.policy.default)
                        info = header_info(msg)
                        if recipients:
                            info["recipients"] = recipients_of(msg)
                    except Exception:  # noqa: BLE001 -- one malformed message must not stop the scan (#29)
                        continue
                    info["date"] = info["date"] or internal_date(meta)
                    info["seen"] = "\\Seen" in meta
                    info["flagged"] = "\\Flagged" in meta
                    m = re.search(r"UID (\d+)", meta)
                    info["uid"] = m.group(1) if m else None
                    size = re.search(r"RFC822\.SIZE (\d+)", meta)
                    info["size"] = int(size.group(1)) if size else 0
                    out.append(info)
            return self._named(out)
        return self._guard(run)

    GMAIL_ARCHIVED = "-in:inbox -in:sent -in:drafts -in:chats"
    GMAIL_CATEGORIES = ("promotions", "social", "forums", "updates")

    def old_headers(self, scope: str, days: int, progress=None) -> tuple[str | None, list[dict]]:
        """Old mail for 清理 -- headers, size, flags, and on Gmail its category and the user's own labels (no
        bodies). scope "inbox": inbox mail older than `days`. scope "archive": archived mail -- on Gmail All Mail
        minus inbox, sent, drafts and chats (Gmail's own search), elsewhere the archive folder (None if none)."""
        def run():
            gmail = self.has("X-GM-EXT-1")
            if scope == "inbox":
                folder, where = "INBOX", "in:inbox"
                self.select(folder, readonly=True)
                uids = self._uid_search(["UNDELETED", "BEFORE", _imap_date(days)])
            else:
                try:
                    folder = self.folder_for("archive")
                except MailError:
                    return None, []
                where = self.GMAIL_ARCHIVED
                self.select(folder, readonly=True)
                uids = self._uid_search(["X-GM-RAW", imap_quoted(where)] if gmail else ["UNDELETED"])
            cats: dict[str, str] = {}
            if gmail and uids:
                for name in self.GMAIL_CATEGORIES:
                    for u in self._uid_search(["X-GM-RAW", imap_quoted(f"{where} category:{name}")]):
                        cats[u.decode()] = name
            what = "FLAGS RFC822.SIZE INTERNALDATE" + (" X-GM-LABELS X-GM-MSGID" if gmail else "") + f" BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})]"
            out = []
            for i in range(0, len(uids), 500):
                if progress:
                    progress(i, len(uids))
                data = self._fetch(uids[i:i + 500], f"({what})")
                for meta, header in _fetched(data):
                    try:
                        info = header_info(email.message_from_bytes(header, policy=email.policy.default))
                    except Exception:  # noqa: BLE001 -- one malformed message must not stop the scan (#29)
                        continue
                    info["date"] = info["date"] or internal_date(meta)
                    uid = re.search(r"UID (\d+)", meta)
                    size = re.search(r"RFC822\.SIZE (\d+)", meta)
                    gm = re.search(r"X-GM-MSGID (\d+)", meta)
                    info.update(uid=uid.group(1) if uid else None, size=int(size.group(1)) if size else 0, folder=folder,
                                gm_msgid=gm.group(1) if gm else None,
                                seen="\\Seen" in meta, flagged="\\Flagged" in meta, labels=gmail_labels(meta),
                                category=cats.get(uid.group(1)) if uid else None)
                    out.append(info)
            return folder, self._named(out)
        return self._guard(run)

    def archived_recent(self, days: int) -> tuple[str | None, list[tuple[str, str]]]:
        """Mail archived from the last `days` days, newest first, as [(UID, Message-ID)] -- headers only. Gmail for
        now: All Mail minus inbox, sent, drafts and chats (Gmail's own search). Other mailboxes: (None, [])."""
        def run():
            if not self.has("X-GM-EXT-1"):
                return None, []
            try:
                folder = self.folder_for("archive")
            except MailError:
                return None, []
            self.select(folder, readonly=True)
            uids = self._uid_search(["SINCE", _imap_date(days), "X-GM-RAW", imap_quoted(self.GMAIL_ARCHIVED)])
            out = []
            for i in range(0, len(uids), 500):
                data = self._fetch(uids[i:i + 500], "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                for part in data:
                    if isinstance(part, tuple) and len(part) > 1:
                        uid = re.search(r"UID (\d+)", part[0].decode("utf-8", "replace"))
                        mid = hdr(email.message_from_bytes(part[1], policy=email.policy.default), "Message-ID")
                        if uid:
                            out.append((uid.group(1), mid))
            return folder, sorted(out, key=lambda x: int(x[0]), reverse=True)
        return self._guard(run)

    # ---- folders ---------------------------------------------------------------------------
    @_guarded
    def folders(self) -> list[dict]:
        if self._folders is None:
            typ, data = self.conn.list()
            self._folders = [f for f in (parse_list_line(x) for x in (data or []) if x) if f]
        return self._folders

    @_guarded
    def folder_for(self, role: str, create: bool = False) -> str:
        name = pick_folder(self.folders(), role)
        if name:
            return name
        if create and role == "archive":
            self.conn.create(quote_mailbox("Archive"))
            self._folders = None
            name = pick_folder(self.folders(), role)
            if name:                                   # listed, so mail moved there can be found again
                self.select(name, readonly=True)
                return name
            # QQ keeps a client's folders under 「我的文件夹」, and until that is opened to IMAP they exist and can't
            # be seen: mail moved there couldn't be brought back. Nothing is moved.
            raise MailError(PROVIDERS.get(self.provider, {}).get("archive_hidden") or "邮箱没有开放新建的归档文件夹，邮件没有移动。")
        raise MailError(f"邮箱中没有{ROLE_LABEL[role]}文件夹。")

    def search_sender(self, list_id: str | None, from_addr: str | None) -> list[str]:
        """Inbox UIDs from one mailing list (preferred) or one sender address -- exactly that one."""
        def run():
            self.select("INBOX", readonly=True)
            return [m["uid"] for m in self._sender_hits(list_id, from_addr, "FROM LIST-ID")]
        return self._guard(run)

    def _sender_hits(self, list_id: str | None, from_addr: str | None, fields: str) -> list[dict]:
        """Messages in the selected folder from this list (or, with no list, this address), with their headers
        (`fields`) and flags. SEARCH only narrows it down -- it matches parts of a header, and the value is the
        sender's own -- so each hit's own List-Id / From must be exactly the one asked for."""
        q = imap_quoted(list_id) if list_id else imap_quoted(from_addr)
        if not q:
            return []
        crit = ["HEADER", "List-Id", q] if list_id else ["FROM", q]
        uids = [u.decode() for u in self._uid_search(crit)]
        out = []
        gm = " X-GM-LABELS" if self.has("X-GM-EXT-1") else ""
        for i in range(0, len(uids), 200):
            data = self._fetch(uids[i:i + 200], f"(FLAGS RFC822.SIZE{gm} BODY.PEEK[HEADER.FIELDS ({fields})])")
            for part in data or []:
                if isinstance(part, tuple) and len(part) > 1:
                    meta = part[0].decode("utf-8", "replace")
                    uid = re.search(r"UID (\d+)", meta)
                    size = re.search(r"RFC822\.SIZE (\d+)", meta)
                    if not uid:
                        continue
                    try:
                        info = header_info(email.message_from_bytes(part[1], policy=email.policy.default))
                    except Exception:  # noqa: BLE001 -- one malformed message must not stop the search (#29)
                        continue
                    if (info["list_id"] != list_id.strip().lower()) if list_id else (info["from_addr"] != from_addr.strip().lower()):
                        continue
                    out.append({**info, "uid": uid.group(1), "size": int(size.group(1)) if size else 0,
                                "message_id": info["message_id"] or None, "seen": "\\Seen" in meta,
                                "flagged": "\\Flagged" in meta, "labels": gmail_labels(meta), "inbox": _in_inbox(meta)})
        return self._named(out)

    def state_of(self, uids: list[str]) -> dict[str, dict]:
        """In the selected folder, what each UID holds now: its Message-ID, starred or not, its Gmail labels and
        whether it is (back) in the inbox -- what a bulk move checks right before it moves (the user may have
        starred or filed it on the phone since the scan)."""
        gm = " X-GM-LABELS" if self.has("X-GM-EXT-1") else ""
        out: dict[str, dict] = {}
        for i in range(0, len(uids), 200):
            data = self._fetch(uids[i:i + 200], f"(FLAGS{gm} BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            for part in data or []:
                if isinstance(part, tuple) and len(part) > 1:
                    meta = part[0].decode("utf-8", "replace")
                    m = re.search(r"UID (\d+)", meta)
                    mid = re.search(rb"Message-ID:\s*(\S+)", part[1], re.I)
                    if m:
                        out[m.group(1)] = {"message_id": mid.group(1).decode("utf-8", "replace") if mid else None,
                                           "flagged": "\\Flagged" in meta, "labels": gmail_labels(meta),
                                           "inbox": _in_inbox(meta)}
        names = self._derived([u for u, s in out.items() if not s["message_id"]])
        for u, name in names.items():
            out[u]["message_id"] = name
        return out

    def sender_messages(self, folder: str, list_id: str | None, from_addr: str | None) -> list[dict]:
        """One list / sender in `folder` (read-only): [{uid, message_id, size, seen}] and the headers (from, date,
        subject...) -- what 了解你 reads before the mail goes to the trash."""
        def run():
            self.select(folder, readonly=True)
            return self._sender_hits(list_id, from_addr, HEADER_FIELDS)
        return self._guard(run)

    def fetch_raw(self, uid: str, folder: str = "INBOX") -> bytes | None:
        def run():
            self.select(folder, readonly=True)
            typ, data = self.conn.uid("FETCH", uid, f"(BODY.PEEK[]<0.{MAX_FETCH_BYTES}>)")
            return next((part[1] for part in data if isinstance(part, tuple) and len(part) > 1), None)
        return self._guard(run)

    def locate(self, folder: str, message_id: str) -> str | None:
        """Read-only: the UID of a message in a folder, by its Message-ID."""
        def run():
            self.select(folder, readonly=True)
            hits = self._by_message_id(message_id)
            return hits[-1] if hits else None
        return self._guard(run)

    SCAN_NEWEST = 500       # what a search that cannot find Message-IDs falls back to: the folder's newest messages

    def _by_message_id(self, message_id: str | None, uids: list[str] | tuple = ()) -> list[str]:
        """UIDs in the selected folder whose Message-ID is exactly this one -- not counting mail flagged \\Deleted:
        that has left (a server without UIDPLUS keeps a moved original flagged until it is expunged, and undo took
        it for "the move was cut off" and deleted the only copy left). Each candidate's own Message-ID and flags are
        read and checked, whatever found it.

        Found three ways, because SEARCH HEADER Message-ID cannot be trusted everywhere (issue #9, 2026-10-21): 163
        finds it only when the HEADER key stands alone (with UNDELETED, nothing), QQ never finds it.
        1. `uids` -- where the message was last seen (the inbox UID from a sync, the UID COPYUID gave): read it.
        2. SEARCH HEADER Message-ID, the key on its own (SEARCH matches part of a header: checked as above).
        3. Not Gmail: the folder's newest SCAN_NEWEST messages, read once per folder per connection -- a message
           just moved or copied is among them (new UIDs are the highest)."""
        if not message_id:
            return []
        hits = self._with_mid([str(u) for u in uids if u], message_id) if uids else []
        if hits:
            return hits
        q = imap_quoted(message_id) if real_mid(message_id) else None
        if q:
            hits = self._with_mid([u.decode() for u in self._uid_search(["HEADER", "Message-ID", q])], message_id)
            if hits or self.provider == "gmail":
                return hits
        folder = self.selected[0] if self.selected else ""
        if folder not in self._scanned:
            newest = [u.decode() for u in self._uid_search(["ALL"])][-self.SCAN_NEWEST:]
            seen: dict[str, list[str]] = {}
            for u, (mid, flags) in self._mids_flags(newest).items():
                if "\\Deleted" not in flags:
                    seen.setdefault(mid.strip().lower(), []).append(u)
            self._scanned[folder] = seen
        return sorted(self._scanned[folder].get(message_id.strip().lower(), []), key=int)

    def _with_mid(self, uids: list[str], message_id: str) -> list[str]:
        """Those of `uids` whose Message-ID is exactly this one and that are not flagged \\Deleted, oldest first."""
        if not uids:
            return []
        got = self._mids_flags(uids)
        return sorted((u for u, (mid, flags) in got.items() if same_mid(mid, message_id) and "\\Deleted" not in flags), key=int)

    def _mids_flags(self, uids: list[str]) -> dict[str, tuple[str, str]]:
        """UID -> (Message-ID, flags) for messages in the selected folder; headers only, nothing marked read. The
        flags may come before or after the header literal, so both are read."""
        out: dict[str, tuple[str, str]] = {}
        for i in range(0, len(uids), 200):
            data = self._fetch(uids[i:i + 200], "(FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            uid = None
            for part in data or []:
                meta = part[0] if isinstance(part, tuple) else part
                meta = meta.decode("utf-8", "replace") if isinstance(meta, bytes) else str(meta or "")
                m = re.search(r"UID (\d+)", meta)
                if m:
                    uid = m.group(1)
                if not uid:
                    continue
                mid, flags = out.get(uid, ("", ""))
                f = re.search(r"FLAGS \(([^)]*)\)", meta)
                if f:
                    flags += " " + f.group(1)
                if isinstance(part, tuple) and len(part) > 1:
                    hit = re.search(rb"Message-ID:\s*(\S+)", part[1], re.I)
                    if hit:
                        mid = hit.group(1).decode("utf-8", "replace")
                out[uid] = (mid, flags)
        names = self._derived([u for u, v in out.items() if not v[0]])
        return {u: (v[0] or names.get(u, ""), v[1]) for u, v in out.items() if v[0] or u in names}

    def sender_checks(self, uids: list[str]) -> dict[str, tuple[str, str | None]]:
        """Inbox UID -> (Message-ID, the provider's verdict on the From domain) for mail read before that verdict
        was kept (sender_check). Headers only; nothing is marked read."""
        authserv, platform = PROVIDERS.get(self.provider, {}).get("authserv"), self.platform
        if not ((authserv or platform) and uids):
            return {}

        def run():
            self.select("INBOX", readonly=True)
            out = {}
            for i in range(0, len(uids), 200):
                data = self._fetch(uids[i:i + 200], "(BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID AUTHENTICATION-RESULTS RECEIVED)])")
                for part in data or []:
                    if isinstance(part, tuple) and len(part) > 1:
                        uid = re.search(r"UID (\d+)", part[0].decode("utf-8", "replace"))
                        if uid:
                            try:
                                msg = email.message_from_bytes(part[1], policy=email.policy.default)
                                info = header_info(msg)
                                out[uid.group(1)] = (info["message_id"], _delivered_by_platform(msg, platform, info["from_domain"])
                                                     or sender_check(msg, authserv, info["from_domain"]))
                            except Exception:  # noqa: BLE001 -- one malformed message must not stop the others (#29)
                                continue
            return out
        return self._guard(run)

    @_guarded
    def _headers(self, uids: list[str], what: str = "") -> list[tuple[str, bytes]]:
        out = []
        for i in range(0, len(uids), 200):
            data = self._fetch(uids[i:i + 200], f"({what}BODY.PEEK[HEADER])")
            out += _fetched(data)
        return out

    def _derived(self, uids: list[str]) -> dict[str, str]:
        """UID -> the identity of each of these messages with no Message-ID, in the selected folder."""
        out = {}
        for meta, header in self._headers(uids) if uids else []:
            m = re.search(r"UID (\d+)", meta)
            if m:
                out[m.group(1)] = derived_mid(header)
        return out

    def _named(self, rows: list[dict]) -> list[dict]:
        """Rows from a header scan of the selected folder, each with its identity."""
        names = self._derived([r["uid"] for r in rows if r.get("uid") and not r.get("message_id")])
        for r in rows:
            r["message_id"] = r.get("message_id") or names.get(r.get("uid"))
        return rows

    def identities(self, uids: list[str]) -> dict[str, dict]:
        """Inbox UID -> {message_id, date}: what mail read before these were worked out for itself gets now."""
        def run():
            self.select("INBOX", readonly=True)
            out = {}
            for meta, header in self._headers(uids, "INTERNALDATE "):
                m = re.search(r"UID (\d+)", meta)
                msg = email.message_from_bytes(header, policy=email.policy.default)
                if m:
                    out[m.group(1)] = {"message_id": hdr(msg, "Message-ID").strip() or derived_mid(header),
                                       "date": header_info(msg)["date"] or internal_date(meta)}
            return out
        return self._guard(run)

    def message_ids(self, uids: list[str]) -> dict[str, str]:
        out = {}
        for i in range(0, len(uids), 200):
            data = self._fetch(uids[i:i + 200], "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            for part in data or []:
                if isinstance(part, tuple) and len(part) > 1:
                    m = re.search(r"UID (\d+)", part[0].decode("utf-8", "replace"))
                    mid = re.search(rb"Message-ID:\s*(\S+)", part[1], re.I)
                    if m and mid:
                        out[m.group(1)] = mid.group(1).decode("utf-8", "replace")
        return {**self._derived([u for u in uids if u not in out]), **out}

    # ---- writing (only called from actions.py after the user confirms) ----------------------
    @_guarded
    def find_uid(self, folder: str, message_id: str, uid: str | None = None) -> str | None:
        """`uid`: where it was last seen in this folder (read first, see _by_message_id)."""
        self.select(folder, readonly=False)
        hits = self._by_message_id(message_id, [uid] if uid else ())
        return hits[-1] if hits else None

    @_guarded
    def store_labels(self, folder: str, uids: list[str], label: str, add: bool = True) -> None:
        """Gmail: give these messages a label of the user's, or take it off (never \\Inbox: Gmail answers OK and
        changes nothing -- archiving and unarchiving move between folders instead)."""
        if not label.strip() or label.strip().lower() in ("\\inbox", "inbox") or any(c in label for c in "\r\n\0"):
            raise MailError("无法使用这个标签。")
        name = quote_mailbox(label)                    # label names travel like folder names (modified UTF-7)
        self.select(folder, readonly=False)
        typ, data = self.conn.uid("STORE", ",".join(uids), "+X-GM-LABELS" if add else "-X-GM-LABELS", f"({name})")
        if typ != "OK":
            raise _refused("加标签", data)

    @_guarded
    def store_flags(self, folder: str, uids: list[str], flags: str, add: bool = True) -> None:
        self._scanned.clear()
        self.select(folder, readonly=False)
        typ, data = self.conn.uid("STORE", ",".join(uids), "+FLAGS.SILENT" if add else "-FLAGS.SILENT", flags)
        if typ != "OK":
            raise _refused("标记", data)

    def _copyuid(self) -> dict[str, str]:
        """Source UID -> UID in the destination, from the COPYUID a UIDPLUS server reports (empty if none):
        undo then finds moved mail without searching for each message."""
        typ, data = self.conn.response("COPYUID")
        out = {}
        for d in data or []:
            parts = (d.decode() if isinstance(d, bytes) else str(d or "")).split()
            if len(parts) == 3:
                out.update(zip(_uid_list(parts[1]), _uid_list(parts[2])))
        return out

    @_guarded
    def move(self, src: str, uids: list[str], dest: str) -> dict[str, str]:
        """Move without ever expunging anything we didn't flag ourselves. Returns source -> destination UIDs
        when the server says (COPYUID)."""
        self._scanned.clear()
        self.select(src, readonly=False)
        uid_set = ",".join(uids)
        self.conn.untagged_responses.pop("COPYUID", None)
        if self.has("MOVE"):
            typ, data = self.conn.uid("MOVE", uid_set, quote_mailbox(dest))
            if typ != "OK":
                raise _refused("移动", data)
            return self._copyuid()
        mids = self.message_ids(uids)
        # A move cut off between the copy and the clean-up left a copy in `dest`: use it, don't copy again
        # (doing the same move twice must not leave the mail there twice).
        self.select(dest, readonly=True)
        there = {u for u in uids if mids.get(u) and self._by_message_id(mids[u])}
        todo = [u for u in uids if u not in there]
        mapping = {}
        if todo:
            self.select(src, readonly=False)
            self.conn.untagged_responses.pop("COPYUID", None)
            typ, data = self.conn.uid("COPY", ",".join(todo), quote_mailbox(dest))
            if typ != "OK":
                raise _refused("移动", data)
            mapping = self._copyuid()
        # The original is only flagged \Deleted once its copy is confirmed in `dest`: a server that answers
        # OK to COPY and copies nothing would otherwise lose the mail when the client cleans up.
        self.select(dest, readonly=True)
        self._scanned.pop(dest, None)                  # the copies are new there: look again
        copied = [u for u in uids if mids.get(u) and self._by_message_id(mids[u], [mapping[u]] if u in mapping else ())]
        if not copied:
            return {}
        self.store_flags(src, copied, "(\\Deleted)")
        if self.has("UIDPLUS"):
            self.conn.uid("EXPUNGE", ",".join(copied))
        return {u: mapping[u] for u in copied if u in mapping}
        # Without UIDPLUS the originals stay flagged \Deleted; the user's mail client removes them.

    @_guarded
    def remaining(self, folder: str, uids: list[str]) -> set[str]:
        """Which of these messages are still in `folder` (mail flagged for deletion has left). A server can
        answer OK to a move and change nothing -- Gmail did exactly that for label stores -- so every move
        out of the inbox is checked with this before it is reported as done."""
        if not uids:
            return set()
        self.select(folder, readonly=False)
        return {u.decode() for u in self._uid_search(["UID", ",".join(uids), "UNDELETED"])} & set(uids)

    @_guarded
    def copy(self, src: str, uids: list[str], dest: str) -> dict[str, str]:
        self._scanned.clear()
        self.select(src, readonly=False)
        self.conn.untagged_responses.pop("COPYUID", None)
        typ, data = self.conn.uid("COPY", ",".join(uids), quote_mailbox(dest))
        if typ != "OK":
            raise _refused("移动", data)
        return self._copyuid()

    @_guarded
    def append(self, folder: str, raw: bytes, flags: str = "(\\Draft)") -> None:
        self._scanned.clear()
        typ, data = self.conn.append(quote_mailbox(folder), flags, imaplib.Time2Internaldate(dt.datetime.now().astimezone()), raw)
        if typ != "OK":
            raise _refused("保存草稿", data)

    @_guarded
    def delete_exact(self, folder: str, uid: str) -> None:
        """Remove one message we created ourselves -- a draft, or the copy a cut-off move left behind while the
        original is still where it was. Never used on the user's mail."""
        self._scanned.clear()
        self.store_flags(folder, [uid], "(\\Deleted)")
        if self.has("UIDPLUS"):
            self.conn.uid("EXPUNGE", uid)


_LABELS = re.compile(r'X-GM-LABELS \(((?:"(?:[^"\\]|\\.)*"|[^()"]+)*)\)')
_LABEL_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([^\s"]+)')


def _in_inbox(meta: str) -> bool:
    """Gmail: the message carries the Inbox label (seen from All Mail, it is also in the inbox)."""
    m = _LABELS.search(meta)
    return bool(m and re.search(r'(^|[\s"])\\{1,2}Inbox("|\s|$)', m.group(1)))


def gmail_labels(meta: str) -> list[str]:
    """The user's own Gmail labels from a FETCH response (system ones like \\Important left out), decoded."""
    m = _LABELS.search(meta)
    if not m:
        return []
    out = []
    for quoted, atom in _LABEL_TOKEN.findall(m.group(1)):
        name = re.sub(r"\\(.)", r"\1", quoted) if quoted else atom
        if name and not name.startswith("\\"):
            out.append(utf7_decode(name))
    return out


def _imap_date(days: int) -> str:
    return (dt.date.today() - dt.timedelta(days=int(days))).strftime("%d-%b-%Y")


def _uid_list(spec: str) -> list[str]:
    """'3,5:7' -> ['3', '5', '6', '7'] (a UID set in the order the server listed it)."""
    out = []
    for part in spec.split(","):
        if ":" in part:
            a, b = (int(x) for x in part.split(":"))
            out += [str(u) for u in (range(a, b + 1) if a <= b else range(a, b - 1, -1))]
        elif part:
            out.append(part)
    return out


# ---- parsing ---------------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "section"}
    SKIP = {"script", "style", "head", "title"}

    VOID = {"img", "br", "hr", "meta", "link", "input", "area", "base", "col", "wbr"}
    UNSEEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])", re.I)
    # what a start tag closes when it is left open, as a browser reads it: an unclosed <p> or <td> in a hidden
    # element must not hide the rest of the mail
    ENDS_P = {"p", "div", "table", "ul", "ol", "dl", "h1", "h2", "h3", "h4", "h5", "h6", "section", "blockquote",
              "pre", "form", "address", "article", "aside", "header", "footer", "nav", "figure"}
    IMPLIED = {"p": ENDS_P, "li": {"li"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"}, "td": {"td", "th", "tr"},
               "th": {"td", "th", "tr"}, "tr": {"tr"}, "option": {"option"}}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open: list[str] = []         # elements open now
        self.hidden_at = 0                # how many were open with the one the reader doesn't see (#42: filler,
        self.unseen: list[str] = []       # hidden links), and what is written there
        self.images = 0

    @property
    def hidden(self) -> bool:
        return bool(self.hidden_at)

    @property
    def skip(self) -> bool:
        return any(t in self.SKIP for t in self.open)

    def _image(self, attrs: dict) -> None:
        if str(attrs.get("width")) in ("0", "1") or str(attrs.get("height")) in ("0", "1"):
            return                        # a tracking pixel
        self.images += 1
        if attrs.get("alt"):
            self.parts.append(attrs["alt"] + " ")

    def _close(self) -> None:
        self.open.pop()
        if len(self.open) < self.hidden_at:
            self.hidden_at = 0

    def _implied_end(self, tag: str) -> None:
        """Close what `tag` ends, with whatever is still open inside it, not reaching out of a table or cell."""
        for i in range(len(self.open) - 1, -1, -1):
            t = self.open[i]
            if tag in self.IMPLIED.get(t, ()):
                while len(self.open) > i:
                    self._close()
                return
            if t in ("table",) or t in ("td", "th") and tag not in ("td", "th", "tr"):
                return

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in self.VOID:
            self._implied_end(tag)
            if tag == "body":             # a <head> never closed: the body is text all the same (luoke-mail#28)
                self.open = [t for t in self.open if t not in self.SKIP]
                self.hidden_at = self.hidden_at if self.hidden_at <= len(self.open) else 0
            self.open.append(tag)
            if not self.hidden and ("hidden" in a or self.UNSEEN.search(a.get("style") or "")):
                self.hidden_at = len(self.open)
        if self.hidden:
            return
        if tag == "img":
            self._image(a)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        hidden = self.hidden
        if tag in self.open:
            while self.open and self.open[-1] != tag:
                self._close()
            self._close()
        if not hidden and tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        (self.unseen if self.hidden else self.parts).append(data)


UNSEEN_NOTE = "[以下文字在邮件里设为不显示，读邮件的人看不到]"
UNSEEN_MIN = 20                   # shorter, once repeats are folded: filler (「隐藏文字隐藏文字…」), not something said


def _unseen(parts: list[str]) -> str:
    """What a mail hides from its reader, apart from the text, when there is something to it: words written to an AI
    assistant are hidden too, and leaving them out hid exactly what makes such a mail suspicious."""
    text = re.sub(r"(.{2,20}?)\1{2,}", r"\1…", " ".join(" ".join(parts).split()))
    return text[:300] if len(text) >= UNSEEN_MIN else ""


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - malformed HTML: fall back to tag stripping
        return re.sub(r"<[^>]+>", " ", html)
    hidden = _unseen(p.unseen)
    return "".join(p.parts) + (f"\n\n{UNSEEN_NOTE}\n{hidden}" if hidden else "")


def image_count(html: str | None) -> int:
    """Pictures a reader sees in this HTML, tracking pixels aside: their words are out of the model's reach."""
    p = _TextExtractor()
    try:
        p.feed(html or "")
        p.close()
    except Exception:  # noqa: BLE001 - malformed HTML: as if there were none
        pass
    return p.images


_QUOTE_HEADERS = [
    re.compile(r"^On .{5,200} wrote:\s*$", re.M),
    re.compile(r"^在 .{3,200}写道[：:]\s*$", re.M),
    re.compile(r"^-{2,}\s*(Original Message|原始邮件|Forwarded message|转发的邮件)\s*-{2,}", re.M | re.I),
    re.compile(r"^(From|发件人)[：:].+\n(Sent|Date|发送时间|时间)[：:]", re.M),
]


# luoke-mail#28 (10-23): in 38 of 250 real messages the text was cut at BODY_CHAR_LIMIT, 64 % of it links --
# every button of a plain-text part written as "text <a tracking link of hundreds of characters>". A long link is
# told by its host alone; short ones (a sender's own page, a phishing address) stay whole, as the model judges by them.
# Written as a link still ("https://host/…"): a page of 「〔链接 host〕」 marks made gemma (128 GB) certain a plain shop
# notice was phishing, while the same mail with links in this form was not (luoke-core's probe, 10-26).
LINK_KEEP = 60
_LINK = re.compile(r"<?(https?://[^\s<>\"']+)>?")


def _short_link(m: re.Match) -> str:
    url = m.group(1)
    if len(url) <= LINK_KEEP:
        return m.group(0)
    return re.sub(r"^(https?://[^/?#]*).*$", r"\1/…", url)


def pick_body(plain: str | None, html: str | None) -> str:
    """The part to read: the plain-text part, unless it is really HTML source, or only a line saying to read the
    HTML version while the HTML part has the message (luoke-mail#28)."""
    p = (plain or "").strip()
    if not p:
        return html_to_text(html or "")
    if re.search(r"<(html|body)\b", p, re.I) or len(re.findall(r"<(?:div|table|td|tr|p|br|span|a)\b", p, re.I)) >= 10:
        return html_to_text(html or plain)
    if html and len(p) < 200:
        text = html_to_text(html).strip()
        if len(text) > max(3 * len(p), 100):        # the HTML version says much more (and says the rest anyway)
            return text
    return plain


def clean_body(text: str, limit: int = BODY_CHAR_LIMIT) -> str:
    """Drop quoted history and noise; keep the newest part of a thread."""
    text = text.replace("\r\n", "\n").replace(" ", " ")
    # preheader padding: zero-width marks, soft hyphens; a figure space is a space (luoke-mail#28: &#8199; and
    # &shy; were left, 600 invisible characters ahead of the text)
    text = re.sub("[\u034f\u200b-\u200f\u2060\ufeff\u00ad]+", "", text).replace("\u2007", " ")
    # what the mail hides (html_to_text puts it last) is kept whatever is cut from what it shows: a quoted
    # history or a long body took it away with them, and with it what makes a mail suspicious
    seen, hidden = _hidden_apart(text)
    cut = len(seen)
    for pat in _QUOTE_HEADERS:
        m = pat.search(seen)
        if m and m.start() > 40:
            cut = min(cut, m.start())
    text = seen[:cut] + ("\n\n" + hidden if hidden and cut < len(seen) else hidden)
    lines = [ln.rstrip() for ln in text.split("\n") if not ln.lstrip().startswith(">")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    text = re.sub(r"[ \t]{2,}", " ", _LINK.sub(_short_link, text)).strip()
    seen, hidden = _hidden_apart(text)
    if len(seen) > limit:
        text = seen[:limit] + "\n…（正文过长，已截断）" + ("\n\n" + hidden if hidden else "")
    return text


def _hidden_apart(text: str) -> tuple[str, str]:
    """The text a reader sees, and the note on what is hidden with what follows it ("" when nothing is)."""
    i = text.find(UNSEEN_NOTE)
    return (text, "") if i < 0 else (text[:i], text[i:])


# Mail that says gb2312 or gbk often carries characters only gb18030 has (「廳」 in a shop's name). get_content()
# does not fail on them, it puts U+FFFD in their place, so the retries below never ran. gb18030 reads all of gb2312
# and gbk the same way.
GB_FAMILY = {"gb2312", "gb_2312-80", "euc-cn", "gbk", "x-gbk", "cp936", "ms936"}


def _decode_part(part) -> str:
    if (part.get_content_charset() or "").lower() in GB_FAMILY:
        return (part.get_payload(decode=True) or b"").decode("gb18030", errors="replace")
    try:
        return part.get_content()
    except Exception:  # noqa: BLE001 - bad charset declarations are common in the wild
        payload = part.get_payload(decode=True) or b""
        for cs in (part.get_content_charset(), "utf-8", "gb18030", "latin-1"):
            if not cs:
                continue
            try:
                return payload.decode(cs)
            except (LookupError, UnicodeDecodeError):
                continue
        return payload.decode("utf-8", errors="replace")


UNSUB_WORDS = re.compile(r"(unsubscribe|unsub|opt[\s_-]?out|email[\s_-]?preferences|manage[\s_-]?(your[\s_-]?)?subscriptions?|"
                         r"退订|取消订阅|退阅|不再接收|取消接收|拒收)", re.I)
REPLY_UNSUB = re.compile(r"(?:回复|回)\s*[「“\"']?\s*(TD|T|N|退订)\s*[」”\"']?\s*(?:即可)?\s*退订|退订请回复\s*[「“\"']?\s*(TD|T|N)", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


class _LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href, self._text = None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href.strip(), " ".join("".join(self._text).split())))
            self._href = None


def find_unsubscribe(raw: bytes) -> dict | None:
    """An unsubscribe link or a "reply TD" instruction in the body, for mail without List-Unsubscribe.
    Returns {"kind": "link", "url"} or {"kind": "reply", "keyword"}; https links win over http."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    found, texts = [], []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/html":
            html = _decode_part(part)
            c = _LinkCollector()
            try:
                c.feed(html)
            except Exception:  # noqa: BLE001
                pass
            found += [href for href, text in c.links if href.lower().startswith(("http://", "https://"))
                      and (UNSUB_WORDS.search(text) or UNSUB_WORDS.search(href))]
            texts.append(html_to_text(html))
        elif ctype == "text/plain":
            text = _decode_part(part)
            texts.append(text)
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if UNSUB_WORDS.search(line):
                    found += URL_RE.findall(" ".join(lines[i:i + 2]))
    found = [u.rstrip(".,;，。") for u in found]
    best = next((u for u in found if u.lower().startswith("https://")), None) or (found[0] if found else None)
    if best:
        return {"kind": "link", "url": best}
    m = REPLY_UNSUB.search(" ".join(texts))
    if m:
        return {"kind": "reply", "keyword": (m.group(1) or m.group(2) or "TD").upper()}
    return None


NOREPLY = re.compile(r"(^|[._-])(no-?reply|do-?not-?reply|notifications?|newsletter|news|promo|marketing|mailer|updates?)([._@-]|$)", re.I)


# the legally required mark on Chinese advertising mail: [AD] in front, or (AD) at the end as some banks write it
AD_SUBJECT = re.compile(r"^\s*[\[［(（]\s*AD\s*[\]］)）]|[\[［(（]\s*AD\s*[\]］)）]\s*$", re.I)


def hdr(msg, name: str) -> str:
    """A header's text on one line. The strict parser refuses some real mail -- a display name whose encoded word
    decodes to a line break (「优惠中心\\n客服」) raises ValueError -- and one such message failed every sync and every
    清理 scan after it (luoke-mail#29): then the raw header is decoded leniently. Line breaks never survive (they
    could also forge lines in a prompt)."""
    try:
        v = msg.get(name, "")
        v = "" if v is None else str(v)
    except (ValueError, TypeError, AttributeError, IndexError, LookupError):
        raw = next((val for key, val in msg.raw_items() if key.lower() == name.lower()), "")
        try:
            v = str(make_header(decode_header(str(raw))))
        except (ValueError, TypeError, LookupError, UnicodeError):
            v = str(raw)
    return " ".join(v.split())


def header_info(msg) -> dict:
    """Sender / list / unsubscribe facts from headers; shared by full parses and header-only scans."""
    name, addr = parseaddr(hdr(msg, "From"))
    addr = addr.lower()
    list_id = hdr(msg, "List-Id")
    m = re.search(r"<([^>]+)>", list_id)
    list_id = (m.group(1) if m else list_id).strip().lower() or None
    unsub = hdr(msg, "List-Unsubscribe")
    targets = re.findall(r"<([^>]+)>", unsub) or [u.strip() for u in unsub.split(",") if u.strip()]
    http = next((t for t in targets if t.lower().startswith("https://")), None) or \
        next((t for t in targets if t.lower().startswith("http://")), None)
    mailto = next((t for t in targets if t.lower().startswith("mailto:")), None)
    one_click = "one-click" in hdr(msg, "List-Unsubscribe-Post").lower() and bool(http and http.lower().startswith("https://"))
    precedence = hdr(msg, "Precedence").lower()
    bulk = bool(list_id or targets or precedence in ("bulk", "list", "junk") or NOREPLY.search(addr.split("@")[0] + "@")
                or AD_SUBJECT.search(hdr(msg, "Subject")))
    date_iso = None
    try:
        d = parsedate_to_datetime(hdr(msg, "Date"))
        date_iso = d.astimezone().strftime("%Y-%m-%d %H:%M") if d else None
    except (TypeError, ValueError):
        pass
    return {
        "message_id": hdr(msg, "Message-ID").strip(),
        "subject": hdr(msg, "Subject").strip() or "（无主题）",
        "from_name": name, "from_addr": addr, "from_domain": addr.rsplit("@", 1)[-1] if "@" in addr else "",
        "date": date_iso, "list_id": list_id, "unsub_http": http, "unsub_mailto": mailto,
        "unsub_one_click": one_click, "bulk": bulk,
    }


def recipients_of(msg) -> list[dict]:
    """Who a message went to: [{"name", "addr" (lower case), "as": "to" | "cc"}], each address once (To first)."""
    out, seen = [], set()
    for field, role in (("To", "to"), ("Cc", "cc")):
        for name, addr in getaddresses([hdr(msg, field)]):
            addr = (addr or "").strip().lower()
            if "@" in addr and addr not in seen:
                seen.add(addr)
                out.append({"name": " ".join((name or "").split()), "addr": addr, "as": role})
    return out[:30]


def group_key(info: dict) -> str | None:
    """Bulk mail from the same list or sender is judged once and shown as one group."""
    if not info.get("bulk"):
        return None
    return ("list:" + info["list_id"]) if info.get("list_id") else ("from:" + (info.get("from_addr") or ""))


def _delivered_by_platform(msg, platform: tuple | None, from_domain: str) -> str | None:
    """139 delivers its partners' advertising itself, NetEase its own notices: the top Received header says so, and
    no outside mail gets it (PLATFORM_139, PLATFORM_NETEASE)."""
    if not platform:
        return None
    pattern, own = platform
    top = (msg.get_all("Received") or [""])[0]
    if own and main_domain(from_domain or "") != own:
        return None
    return "platform" if re.search(pattern, " ".join(str(top).split())) else None


_TWO_PART_TLDS = ("com.cn", "net.cn", "org.cn", "co.uk", "co.jp", "com.hk", "com.tw", "com.au")


def main_domain(domain: str) -> str:
    """example.com for mail.example.com: enough to tell whether a signature is the sender's own organisation's."""
    parts = domain.lower().rstrip(".").split(".")
    return ".".join(parts[-3:] if ".".join(parts[-2:]) in _TWO_PART_TLDS else parts[-2:])


def _dmarc(m: re.Match, h: str, from_domain: str) -> str | None:
    frm = re.search(r"header\.from=([\w.-]+)", h[m.end():], re.I)
    f, d = (frm.group(1).lower().rstrip(".") if frm else ""), from_domain.lower()
    if frm and d != f and not d.endswith("." + f):        # Gmail names the organisation: wirex.example for mail.wirex.example
        return None
    if m.group(1).lower() == "fail":
        # the domain's owner asks nothing of a failure under p=none, and ordinary list mail fails that way
        policy = re.search(r"\(([^)]*)\)", h[m.end():m.end() + 80])
        told = re.findall(r"\b(?:p|dis)=(\w+)", policy.group(1), re.I) if policy else []
        return "fail" if not told or any(x.lower() in ("quarantine", "reject") for x in told) else None
    return {"pass": "pass"}.get(m.group(1).lower())


def sender_check(msg, authserv: str | None, from_domain: str) -> str | None:
    """The receiving provider's own verdict on the From domain: "pass" (the mail really comes from that domain --
    which says nothing about whether the domain is the organisation it names), "fail", or None (not there, not this
    provider's, or no clear verdict). Only the topmost Authentication-Results counts, and only with the provider's
    own id: the provider adds it on receipt, above anything the sender wrote into the message.

    DMARC where there is one. NetEase and QQ give only DKIM and SPF (#41): a DKIM signature of the sender's own
    organisation is a pass, both failing is a fail, the rest says nothing."""
    tops = msg.get_all("Authentication-Results") or []
    if not (authserv and from_domain and tops):
        return None
    h = " ".join(str(tops[0]).split())
    if not re.fullmatch(authserv, h.split(";", 1)[0].strip().lower()):
        return None
    m = re.search(r"\bdmarc=([a-z]+)", h, re.I)
    if m:
        return _dmarc(m, h, from_domain)
    dkim, spf = (re.search(rf"\b{k}=([a-z]+)", h, re.I) for k in ("dkim", "spf"))
    signer = re.search(r"header\.(?:d|i)=@?([\w.-]+)", h, re.I)
    if dkim and dkim.group(1).lower() == "pass" and signer and main_domain(signer.group(1)) == main_domain(from_domain):
        return "pass"
    if dkim and spf and dkim.group(1).lower() == "fail" and spf.group(1).lower() == "fail":
        return "fail"
    return None


def parse_message(raw: bytes, authserv: str | None = None, platform: tuple | None = None) -> dict:
    """`authserv`: the provider's id on the Authentication-Results it adds (providers.PROVIDERS[...]["authserv"]).
    `platform`: how its top Received header reads when the provider delivered the mail itself (PLATFORM_139 …)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    plain, html, attachments = None, None, []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = part.get_content_disposition()
        ctype = part.get_content_type()
        if disp == "attachment" or (part.get_filename() and disp != "inline"):
            attachments.append(part.get_filename() or ctype)
            continue
        if ctype == "text/plain" and plain is None:
            plain = _decode_part(part)
        elif ctype == "text/html" and html is None:
            html = _decode_part(part)
    body = pick_body(plain, html)
    info = header_info(msg)
    _, reply_to = parseaddr(hdr(msg, "Reply-To"))
    info.update({
        "to": [a for _, a in getaddresses([hdr(msg, "To")]) if a][:10],
        "cc": [a for _, a in getaddresses([hdr(msg, "Cc")]) if a][:10],
        "reply_to": reply_to or None,
        "references": hdr(msg, "References").strip(),
        "attachments": attachments[:20],
        "body": clean_body(body or ""),
        "auth": _delivered_by_platform(msg, platform, info["from_domain"]) or sender_check(msg, authserv, info["from_domain"]),
    })
    info["message_id"] = info["message_id"] or derived_mid(raw)
    info["images"] = image_count(html)
    info["group_key"] = group_key(info)
    return info
