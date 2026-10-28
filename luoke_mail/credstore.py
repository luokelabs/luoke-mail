"""Where secrets (mail app passwords, local API keys) live.

macOS: the login Keychain via /usr/bin/security. The secret is written through `security -i`
on stdin so it never appears in argv (visible to `ps`).
Elsewhere (until native backends land): a 0600 JSON file in the config dir, clearly flagged as
plaintext in the UI.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from .config import CONFIG_DIR

SERVICE_PREFIX = "luoke-mail"
NOT_FOUND = 44                  # `security find-generic-password`: errSecItemNotFound


class SecretUnavailable(RuntimeError):
    """The keychain could not be read just now (locked, no session to ask in) -- not "there is no secret". Never
    a reason to tell the user to enter it again, nor to store it again."""


KEYCHAIN_LOCKED = "钥匙串暂时读不到（Mac 锁定时会这样），解锁后会自动继续。"


class KeychainStore:
    kind = "keychain"
    label = "macOS 钥匙串"
    plaintext = False

    def __init__(self, binary: str = "/usr/bin/security"):
        self.bin = binary

    @staticmethod
    def _quote(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def set(self, name: str, account: str, secret: str) -> None:
        if "\n" in secret or "\r" in secret:
            raise ValueError("secret must be a single line")
        cmd = "add-generic-password -U -s {} -a {} -w {}\n".format(
            self._quote(f"{SERVICE_PREFIX}:{name}"), self._quote(account), self._quote(secret))
        p = subprocess.run([self.bin, "-i"], input=cmd, capture_output=True, text=True, timeout=20)
        if p.returncode != 0 or "error" in (p.stderr or "").lower():
            raise RuntimeError(f"写入钥匙串失败：{(p.stderr or p.stdout).strip()[:200]}")

    def get(self, name: str, account: str) -> str | None:
        """The secret, None when there is none -- or SecretUnavailable when the keychain cannot be read now."""
        p = subprocess.run([self.bin, "find-generic-password", "-s", f"{SERVICE_PREFIX}:{name}", "-a", account, "-w"],
                           capture_output=True, text=True, timeout=20)
        if p.returncode == 0:
            return p.stdout.rstrip("\n")
        if p.returncode == NOT_FOUND:
            return None
        print(f"[luoke-mail] keychain not readable now ({p.returncode}): {(p.stderr or '').strip()[:160]}", file=sys.stderr)
        raise SecretUnavailable(KEYCHAIN_LOCKED)

    def delete(self, name: str, account: str) -> None:
        subprocess.run([self.bin, "delete-generic-password", "-s", f"{SERVICE_PREFIX}:{name}", "-a", account],
                       capture_output=True, text=True, timeout=20)


class FileStore:
    kind = "file"
    label = "本机文件（明文，仅本人可读）"
    plaintext = True

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(CONFIG_DIR, "credentials.json")

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(self.path, 0o600)

    def set(self, name: str, account: str, secret: str) -> None:
        data = self._load()
        data[f"{name}|{account}"] = secret
        self._save(data)

    def get(self, name: str, account: str) -> str | None:
        return self._load().get(f"{name}|{account}")

    def delete(self, name: str, account: str) -> None:
        data = self._load()
        if data.pop(f"{name}|{account}", None) is not None:
            self._save(data)


def default_store():
    if sys.platform == "darwin" and shutil.which("security"):
        return KeychainStore()
    return FileStore()
