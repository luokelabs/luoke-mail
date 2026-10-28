"""Non-secret settings under ~/.luoke-mail (override with LUOKE_MAIL_HOME). Mail state lives in store.py."""
from __future__ import annotations

import json
import os
import shutil
import sys

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".luoke-mail")
CONFIG_DIR = os.environ.get("LUOKE_MAIL_HOME") or DEFAULT_DIR
if "unittest" in sys.modules and os.path.realpath(CONFIG_DIR) == os.path.realpath(DEFAULT_DIR):
    # a test must never read or write the owner's data (tests/isolate.py sets a temporary folder first)
    raise RuntimeError("tests must not use ~/.luoke-mail: import tests/isolate.py before luoke_mail")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    # the connected mailboxes, each {"provider", "address", "host", "port"}; the first is shown first. The data
    # of each lives under its lowercased address (the store, the keychain), so mailboxes never mix.
    "accounts": [],
    "engine": {"base_url": None, "model": None, "engine_name": None, "has_api_key": False},
    "profile": {"about_me": "", "language": "中文", "signature": ""},
    # the inbox is the unit: the last `days` days are sorted mail by mail (read or not); older mail is
    # cleared in bulk with 清理旧邮件. `limit` caps how many new mails one sync reads.
    "fetch": {"limit": 100, "days": 30},
    "mode": {"write": False},
    "auto": {"enabled": False, "every": 30},       # background sorting (minutes between runs), see background.py
    "power": {"level": "full"},                    # 功耗: full / balanced / quiet, see engines.set_power
    "learn": {"on": True, "enabled_at": None},     # 从邮件中了解你: on by default (ADR 0013), the person's switch (learner.py)
    # whose turn it is in a conversation (threads.py): reads the sent folder too; on by default, can be turned off
    "threads": {"on": True},
    # what was sorted is marked read (ADR 0016, 2026-10-19): the unread count in the mail app is what
    # the user has yet to look at, here or in 今日处理. Can be turned off in 设置
    "flow": {"mark_read": True},
}


def private_dir(path: str = CONFIG_DIR) -> str:
    """The data folder holds mail text and the page token: readable by this user only (macOS home
    folders are readable by the whole "staff" group)."""
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


VERSION = 3


def _migrate(saved: dict) -> None:
    """v1 (<= 0.4.1) only looked at new mail: unread, last 7 days. The whole inbox counts now.
    v2 (<= 0.9) had one mailbox ("mail"); now there can be several ("accounts")."""
    if saved.get("version", 1) < 2:
        f = saved.get("fetch") or {}
        f.pop("unseen_only", None)
        if f.get("days") == 7:            # the old default, never changed by the user
            f["days"] = 30
    if saved.get("version", 1) < 3:
        m = saved.pop("mail", None) or {}
        if m.get("address") and not saved.get("accounts"):
            saved["accounts"] = [{k: m.get(k) for k in ("provider", "address", "host", "port")}]


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        _migrate(saved)
        for section, values in saved.items():
            if isinstance(values, dict) and isinstance(cfg.get(section), dict):
                cfg[section].update(values)
        cfg["accounts"] = [a for a in saved.get("accounts") or [] if isinstance(a, dict) and a.get("address")]
    except (OSError, ValueError):
        pass
    cfg["fetch"].pop("unseen_only", None)
    return cfg


def save(cfg: dict) -> None:
    cfg = {**cfg, "version": VERSION}
    private_dir()
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def remove_legacy() -> None:
    """v0.1 kept per-run JSON snapshots; v0.2 keeps one state database instead."""
    shutil.rmtree(os.path.join(CONFIG_DIR, "runs"), ignore_errors=True)
