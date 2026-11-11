"""The person's memory lives in luoke-mem, shared by every luoke tool (see luoke-mem/DESIGN.md). This module
finds the package, decides which memory file luoke-mail uses, and moves the old 「关于你」 setting into it.

What luoke-mail writes there: the user's own words (关于你, 关注的话题, 不感兴趣, and their 收藏 / 没兴趣 on
订阅精选) as statements; what they did (opened an email, opened an article) as activities; guesses drawn from
those as proposals that wait for the user. Never what an email says about the user."""
from __future__ import annotations

import hashlib
import hmac
import inspect
import os
import re
import secrets
import subprocess
import sys
import time

from . import config

TOOL = "luoke-mail"


def _hub_copy() -> str | None:
    """In 螺壳邮件助手.app: where the luoke_mem 螺壳主控 carries lives (luoke-core c0c1c14: `GET /tasks` says so,
    {"luoke_mem": {"path", "version"}}), so every tool writes the memory with the same library. Asked anew each
    time the process starts (an upgraded hub moves it); None when there is no hub, it does not say, or it is not
    the app -- then the copy the app carries is used. Run from source, never: the sibling checkout is the one."""
    if not os.environ.get("LUOKE_LAUNCHER"):
        return None
    import json
    import socket
    from . import __version__
    named = os.environ.get("LUOKE_HUB_SOCKET")
    path = named or os.path.expanduser("~/Library/Application Support/luoke-core/run/hub.sock")
    if not os.path.exists(path):
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect(path)
            sock.sendall(f"GET /tasks HTTP/1.0\r\nHost: localhost\r\nX-Luoke-Accessory: {TOOL}/{__version__}\r\n\r\n".encode())
            raw = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw += chunk
        head, _, body = raw.partition(b"\r\n\r\n")
        if b" 200 " not in head.split(b"\r\n", 1)[0]:
            return None
        where = (json.loads(body.decode("utf-8")).get("luoke_mem") or {}).get("path")
    except (OSError, ValueError, AttributeError):
        return None
    return where if where and os.path.isdir(os.path.join(where, "luoke_mem")) else None


def _load():
    hub = _hub_copy()
    if hub:
        sys.path.insert(0, hub)
    try:
        import luoke_mem
        return luoke_mem
    except ImportError:
        pass
    # not installed: a checkout next to this one (luoke-mail/ and luoke-mem/ in the same folder)
    sibling = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "luoke-mem")
    if os.path.isdir(os.path.join(sibling, "luoke_mem")):
        sys.path.insert(0, sibling)
        import luoke_mem
        return luoke_mem
    raise ImportError("缺少 luoke-mem。请把 luoke-mem 放在 luoke-mail 旁边（同一个文件夹中），或运行 pip install luoke-mem。")


luoke_mem = _load()
# 0.8: secrets apart from sensitive context (top-level ADR 0004); 0.9: a rewritten note marked by its new words;
# 0.15: what the user says about understandings is the memory's, not forgotten with the mail assistant (ADR 0009)
# -- the forget dialogs promise it; 0.15.4: what is forgotten is gone from the file too, and the names users see
NEEDED = (0, 23, 0)
if (tuple(int(x) for x in re.findall(r"\d+", getattr(luoke_mem, "__version__", "0"))[:3]) < NEEDED
        or not hasattr(luoke_mem, "sensitive_kinds") or "sensitive" not in inspect.signature(luoke_mem.Memory.revise).parameters):
    raise ImportError(f"luoke-mem 版本太旧（{getattr(luoke_mem, '__version__', '?')}），需要 {'.'.join(map(str, NEEDED))} 或更新的版本。"
                      "请更新放在 luoke-mail 旁边的 luoke-mem。")
Memory = luoke_mem.Memory
TooNew = luoke_mem.TooNew                       # the memory file was written by a newer luoke-mem than the one here
TOO_NEW = "你的记忆是更新版本的螺壳记忆写的，这一版螺壳邮件助手读不了。请更新螺壳邮件助手。"
TOO_NEW_IN_LUOKE = "你的记忆是更新版本的螺壳记忆写的，这一版螺壳读不了。请更新螺壳。"
sensitive = luoke_mem.sensitive                 # either kind below: the same lists for every luoke tool


def mask(text: str | None) -> str:
    """What is written to the memory, credentials hidden: a password, a code, a key or token, a full card or ID
    number is replaced where it stands (「验证码 〔验证码〕」), the rest kept as it is (top-level ADR 0013: a
    credential is a security matter; nothing else is held back on the user's own machine). The memory masks
    again when it writes; this is so the mail assistant's own words never carry one either."""
    return luoke_mem.mask_secrets(text or "")


def marks(*texts: str | None) -> list[str]:
    """The kinds of sensitive context in what is about to be written: it is kept, marked with them, for the memory
    to weigh (what it infers about health, legal matters or beliefs waits for the user's word; ADR 0013)."""
    found: set[str] = set()
    for t in texts:
        found.update(luoke_mem.sensitive_kinds(t or ""))
    return sorted(found)


# ---- which mailbox a record comes from (top-level ADR 0010) ----------------------------------------------------
def account_key(acct: str) -> str:
    """The mailbox a record comes from, as it is written into the record's data: an opaque key, not the address
    (2026-10-16). The same mailbox removed and added again gets the same key -- it rests on a random
    value kept in the data folder (`account-salt`); after the data folder is deleted, a mailbox gets a new one."""
    if acct == "demo":
        return "demo"
    return "mbx-" + hmac.new(_salt(), acct.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def sourced(acct: str, data: dict | None = None) -> dict:
    """A record's data with the mailbox it comes from. The memory itself does not read it; with it, what one
    mailbox contributed can be told apart, and forgotten (`forget_records(match={"account": ...})`)."""
    return {**(data or {}), "account": account_key(acct)}


def happened(mem, acct: str, topic: str, text: str, *, sender: str | None, by: str = "user", when: str | None = None,
             data: dict | None = None, ref: str | None = None, speaker: str = "user", kind: str = "activity") -> int:
    """What happened, for the memory to learn from (top-level ADR 0015: the tools record, the memory concludes):
    which mailbox, which sender (list:/from:), who did it -- "user", "rule", or "suggestion" (the user accepting
    one) -- and when it really happened (`when`, not when it is written down). Credentials masked, sensitive
    context marked."""
    words = mask(text)
    return mem.record(speaker, kind, words, topic=topic, ref=ref, happened_at=(when or "")[:19] or None,
                      sensitive=marks(words), data=sourced(acct, {**(data or {}), "sender": sender, "by": by}))


AUTO_ACTIONS = ("archive", "read", "label")   # what may be done unasked, each undone in one click (ADR 0016)


def auto_habits(mem, acct: str) -> list[dict]:
    """The handling habits the user confirmed for this mailbox that the mail assistant carries out by itself:
    [{"note_id", "sender", "category", "action", "label", "text"}] (a filter suggestion is shown on 规则, not done). The memory marks which (luoke-mem 0.18: `auto` on a
    habit that is sure, archive or read, with `data` {account, sender, category, action, name}); with an older
    memory there are none. A habit is one sender's one kind of mail (2026-10-19: archiving a shop's
    promotions says nothing of its receipts or security alerts), so one without a category is not done unasked.
    Trash, spam and unsubscribing never are -- they stay suggestions the user clicks."""
    try:
        notes = mem.notes(kinds=["preference"], confidence=["sure"])
    except TypeError:
        return []
    key, out = account_key(acct), []
    for n in notes:
        d = n.get("data") or {}
        if (n.get("auto") and d.get("account") == key and d.get("action") in AUTO_ACTIONS and d.get("sender")
                and d.get("category") and (d["action"] != "label" or d.get("label"))):
            out.append({"note_id": n["id"], "sender": d["sender"], "category": d["category"], "action": d["action"],
                        "label": d.get("label"), "text": n.get("text") or ""})
    return out



def from_account(record: dict, acct: str | None) -> bool:
    """Whether a kept record came from this mailbox (`acct` None: from none, e.g. the user's website). Records
    from before 0.13.6 carry no mailbox -- "source unknown" -- and are not guessed."""
    have = (record.get("data") or {}).get("account")
    return have == (account_key(acct) if acct else None)


_salts: dict[str, bytes] = {}


def _salt() -> bytes:
    path = os.path.join(config.CONFIG_DIR, "account-salt")
    if path not in _salts:
        config.private_dir()
        try:                                       # the app and 螺壳主控's jobs may both start first: one writes it
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        for _ in range(50):
            with open(path, encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                break
            time.sleep(0.02)                       # the other one is still writing it
        _salts[path] = value.encode("utf-8")
    return _salts[path]


def whole_memory_ui():
    """luoke-mem's own page for the whole memory (0.6: `luoke_mem.ui.ensure(path) -> url`), or None with an
    older luoke-mem. Everything the user keeps, from every tool, is seen and managed there (top-level ADR 0002)."""
    try:
        from luoke_mem import ui
    except ImportError:
        return None
    return ui if hasattr(ui, "ensure") else None


MEMORY_APP_ID = "ai.luoke.memory"
MEMORY_URL = "luoke://memory"
HUB_APPS = ("螺壳.app", "螺壳主控.app")
HUB_ID = "ai.luoke.core"
HUB_DEV_ID = "ai.luoke.core.dev"     # 螺壳（开发）: build_app.sh's default, ad hoc


def hub_roots() -> tuple[str, ...]:
    """Where to look for 主控. `LUOKE_HUB_APPS` (folders, os.pathsep apart) names others: tests never look at the
    real /Applications (tests/isolate.py)."""
    named = os.environ.get("LUOKE_HUB_APPS")
    if named is not None:
        return tuple(p for p in named.split(os.pathsep) if p)
    return "/Applications", os.path.join(os.path.expanduser("~"), "Applications")


def hub_app() -> str | None:
    """Where 主控 is installed: dragged into /Applications (luoke-core#30, 10-24) or in ~/Applications,
    under its new name 「螺壳」 or the old one, and it is the one with 主控's bundle id."""
    import plistlib
    for root in hub_roots():
        for name in HUB_APPS:
            app = os.path.join(root, name)
            try:
                with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
                    if plistlib.load(f).get("CFBundleIdentifier") == HUB_ID:
                        return app
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
    return None


def memory_entry() -> str | None:
    """How to open 螺壳记忆, or None when there is none. From beta1 it is part of 螺壳主控, opened with
    luoke://memory (top-level ADR 0003, amended 2026-10-16; luoke-core's page): there when 主控 carries it. With
    a 主控 from before, the separate 螺壳记忆.app, by its bundle id."""
    hub = hub_app()
    if hub and os.path.isfile(os.path.join(hub, "Contents", "Resources", "memory", "luoke-accessory.json")):
        return MEMORY_URL
    return MEMORY_APP_ID if memory_app() else None


def memory_app() -> str | None:
    """The separate 螺壳记忆.app of before beta1 (bundle id ai.luoke.memory), or None: its usual place first, then
    Spotlight (off on some Macs: then only the usual place counts). Copies in a build folder (*.noindex) are not it."""
    usual = os.path.join(os.path.expanduser("~"), "Applications", "螺壳记忆.app")
    found = [usual] if os.path.isdir(usual) else []
    if not found:
        try:
            out = subprocess.run(["/usr/bin/mdfind", f"kMDItemCFBundleIdentifier == '{MEMORY_APP_ID}'"],
                                 capture_output=True, text=True, timeout=5).stdout
            found = [p for p in out.splitlines() if p.endswith(".app") and ".noindex/" not in p]
        except (OSError, subprocess.SubprocessError):
            found = []
    return found[0] if found else None


DEFAULT_HOME = os.path.join(os.path.expanduser("~"), ".luoke-mail")


def path(demo: bool = False) -> str:
    if demo:                                   # the demo inbox teaches nothing to the real memory
        return os.path.join(config.CONFIG_DIR, "demo-mem.db")
    if os.environ.get("LUOKE_HOME"):
        return os.path.join(os.environ["LUOKE_HOME"], "mem.db")
    # a copy of the data folder (experiments, tests) keeps its own memory, out of the real one; the login item
    # names the real folder explicitly, and still shares the memory with every luoke tool
    if os.path.abspath(config.CONFIG_DIR) != os.path.abspath(DEFAULT_HOME):
        return os.path.join(config.CONFIG_DIR, "mem.db")
    return luoke_mem.default_path()


def open_memory(demo: bool = False) -> Memory:
    config.private_dir()
    return Memory(path(demo), tool=TOOL)


def migrate_profile(cfg: dict, mem: Memory) -> bool:
    """Up to 0.7 「关于你」 was one text field in the settings; it is the user's own words about themselves."""
    text = (cfg["profile"].get("about_me") or "").strip()
    if not text:
        return False
    if not any(n["text"] == text for n in mem.notes(kinds=["fact"])):
        mem.state("fact", text, record_text=f"在设置中填写：{text}", sensitive=marks(text))
    cfg["profile"]["about_me"] = ""
    return True


def about(mem: Memory, limit: int = 600) -> str:
    """The block the triage and drafting prompts get: who the user is, and the habits they confirmed."""
    return mem.context(["fact", "preference"], limit=limit)


def seed_demo(mem: Memory) -> None:
    """The demo memory starts with a little of everything, so every part of the page can be tried."""
    if mem.records(limit=1):
        return
    at = "2026-10-01 09:00:00"
    mem.state("fact", "在一家软件公司做产品经理，负责 B 端客户", now=at)
    mem.state("interest", "本地大模型和推理优化", now=at)
    mem.state("interest", "AI 编程工具", now=at)
    mem.state("dislike", "购物促销和优惠券", now=at)
    seen = [mem.record("user", "activity", f"打开过：{s}", topic="mail-read", happened_at=d, now=at) for s, d in (
        ("开源周报：本月最值得关注的 10 个仓库", "2026-09-14 08:00:00"),
        ("开源社区治理：维护者如何分配精力", "2026-09-21 08:00:00"),
        ("一个开源项目的十年", "2026-09-28 08:00:00"))]
    nid = mem.propose("interest", "开源社区和项目维护", seen, why="多次打开过这类主题的邮件", now=at)
    if nid and hasattr(luoke_mem, "WholeMemory"):        # what the memory would ask, for the 记忆 page to show
        whole = luoke_mem.WholeMemory(mem.path, regrade=False)
        try:
            whole.ask("question", "你关注「开源社区和项目维护」吗？", "你多次打开过这类主题的订阅邮件", about_note=nid,
                      about_kind="interest", answer={"type": "choice", "options": ["对", "不对", "以后再说"]}, now=at)
        finally:
            whole.close()
