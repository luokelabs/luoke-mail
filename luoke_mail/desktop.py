"""Desktop integration: one instance per data folder, a page address that survives restarts, the login
item that keeps luoke-mail running in the background, the launcher app, and system notifications.

macOS:
- Login item = a per-user LaunchAgent (no admin rights) running `python3 -m luoke_mail --service`.
- Launcher = a small AppleScript app 「螺壳邮件助手」 in ~/Applications. Opening it opens the page; the
  service also asks it to post notifications, so they carry our name and clicking one opens the page
  (notifications posted with plain `osascript` belong to Script Editor, and clicking them opens that).
Elsewhere: notifications through notify-send when it exists; no login item yet (sorting then runs only
while the page's server is running).

Nothing here touches system or security settings; enabling and disabling are both one click in the page.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import plistlib
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .config import CONFIG_DIR, private_dir

APP_NAME = "螺壳邮件助手"
LABEL = "local.luoke-mail"
BUNDLE_ID = "local.luoke-mail.launcher"
PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_HOME = os.path.join(os.path.expanduser("~"), ".luoke-mail")


class DesktopError(Exception):
    pass


def _suffix(home: str) -> str:
    """A copy of the data folder (LUOKE_MAIL_HOME) gets its own login item and launcher, so an
    experiment never replaces the real ones."""
    if os.path.abspath(home) == os.path.abspath(DEFAULT_HOME):
        return ""
    return hashlib.sha1(os.path.abspath(home).encode()).hexdigest()[:8]


def _write_private(path: str, text: str) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ---- the page address: a token that survives restarts --------------------------------------------------
def load_token(home: str = CONFIG_DIR) -> str:
    """The token every /api request must carry. It is kept (0600) so an open tab keeps working when the
    background service restarts, and so the launcher can open the page."""
    path = os.path.join(home, "token")
    try:
        with open(path, encoding="utf-8") as f:
            tok = f.read().strip()
        if len(tok) >= 20:
            return tok
    except OSError:
        pass
    private_dir(home)
    tok = secrets.token_urlsafe(24)
    _write_private(path, tok)
    return tok


def page_url(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/?t={token}"


# ---- one instance per data folder ------------------------------------------------------------------------
class InstanceLock:
    """Held for the life of the server. Two servers on one data folder would sort the same mail twice."""

    def __init__(self, home: str = CONFIG_DIR, name: str = "server.lock"):
        """`name`: another lock in the same folder -- "idle.lock" is held by whoever does idle work right now (the
        app, or 螺壳主控's idle job in its own process), so the two never work at once."""
        self.home = home
        self.path = os.path.join(home, name)
        self.fh = None

    def acquire(self, wait: float | None = 0) -> bool:
        """wait=0: try once; wait=None: block until the other instance exits."""
        private_dir(self.home)
        fh = open(self.path, "a+")
        deadline = None if wait is None else time.time() + wait
        while True:
            if _try_lock(fh):
                self.fh = fh
                return True
            if deadline is not None and time.time() >= deadline:
                fh.close()
                return False
            time.sleep(0.25)

    def release(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None


def _try_lock(fh) -> bool:
    try:
        import fcntl
    except ImportError:                       # Windows: lock the first byte
        import msvcrt
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def write_server_info(port: int, service: bool, home: str = CONFIG_DIR) -> None:
    _write_private(os.path.join(home, "server.json"), json.dumps({"pid": os.getpid(), "port": port, "service": service}))


def read_server_info(home: str = CONFIG_DIR) -> dict | None:
    try:
        with open(os.path.join(home, "server.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ping(port: int, token: str, timeout: float = 1.5) -> dict | None:
    """Is a luoke-mail server answering on this port (with our token)?"""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", "/api/ping", headers={"X-Luoke-Token": token, "Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        body = r.read()
        c.close()
        return json.loads(body) if r.status == 200 else None
    except (OSError, ValueError):
        return None


def running(home: str = CONFIG_DIR) -> str | None:
    """The page address of the instance running on this data folder, if any."""
    info = read_server_info(home)
    token = load_token(home)
    if info and ping(info["port"], token):
        return page_url(info["port"], token)
    return None


def packaged() -> bool:
    """Running from 螺壳邮件助手.app (packaging/): the code sits in the app's own Python, inside a signed bundle
    that must not be written to -- no __pycache__ there."""
    return ".app/Contents/" in PKG_PARENT


def _child_env() -> dict:
    env = {**os.environ, "PYTHONPATH": PKG_PARENT, "LUOKE_MAIL_HOME": CONFIG_DIR, "PYTHONIOENCODING": "utf-8"}
    if packaged():
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def start_detached(argv: list[str]) -> subprocess.Popen:
    """A process that outlives the one starting it (the app's opener quits at once): its own session, no
    terminal; what it says on stderr goes to app.log in the data folder (stdout carries the page's token)."""
    os.makedirs(CONFIG_DIR, 0o700, exist_ok=True)
    log = os.path.join(CONFIG_DIR, "app.log")
    _rotate(log)                                    # it was never cut: 8.8 MB in a few days (luoke-mail#24)
    with open(log, "a", encoding="utf-8") as err:
        os.chmod(log, 0o600)
        return subprocess.Popen(argv, env={**_child_env(), STAMP_ENV: "1"}, cwd=CONFIG_DIR, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)


def alert(text: str) -> None:
    """A dialog, for the app (no terminal to print to). The text is passed as data, never as code."""
    print(f"[luoke-mail] {text}", file=sys.stderr)
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/osascript", "-e", "on run argv", "-e", f"display alert {_as_string(APP_NAME)} message (item 1 of argv) as warning",
                        "-e", "end run", text], capture_output=True, timeout=600)


def open_app(bundle_id: str) -> None:
    """Open another app the user installed, by its bundle id (e.g. 螺壳记忆, which then shows its own page)."""
    subprocess.run(["/usr/bin/open", "-b", bundle_id], capture_output=True, timeout=20)


def open_url(url: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/open", url], check=False)
    else:
        import webbrowser
        webbrowser.open(url)


def mark_service_started(home: str = CONFIG_DIR) -> None:
    """The service writes this before waiting for the lock: proof for the handoff that it really started."""
    _write_private(os.path.join(home, "service.pid"), str(os.getpid()))


def service_started_since(t: float, home: str = CONFIG_DIR) -> bool:
    path = os.path.join(home, "service.pid")
    try:
        if os.path.getmtime(path) < t:
            return False
        with open(path, encoding="utf-8") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


# ---- macOS login item ------------------------------------------------------------------------------------
class LoginItem:
    def __init__(self, home: str = CONFIG_DIR, agents_dir: str | None = None, run=subprocess.run):
        self.home = os.path.abspath(home)
        sfx = _suffix(self.home)
        self.label = LABEL + (f".{sfx}" if sfx else "")
        self.dir = agents_dir or os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")
        self.path = os.path.join(self.dir, self.label + ".plist")
        self.run = run

    @property
    def domain(self) -> str:
        return f"gui/{os.getuid()}"

    def plist(self, port: int) -> dict:
        log = os.path.join(self.home, "service.log")
        return {
            "Label": self.label,
            "ProgramArguments": [sys.executable, "-m", "luoke_mail", "--service", "--port", str(port)],
            "EnvironmentVariables": {"PYTHONPATH": PKG_PARENT, "LUOKE_MAIL_HOME": self.home,
                                     "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                                     **({"PYTHONDONTWRITEBYTECODE": "1"} if packaged() else {})},
            "WorkingDirectory": self.home,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},      # restart after a crash, not after a clean exit
            "ThrottleInterval": 30,
            "StandardOutPath": log, "StandardErrorPath": log,
            "AssociatedBundleIdentifiers": [BUNDLE_ID],
        }

    def read(self) -> dict | None:
        try:
            with open(self.path, "rb") as f:
                return plistlib.load(f)
        except (OSError, ValueError, plistlib.InvalidFileException):
            return None

    def status(self) -> dict:
        p = self.read()
        if not p:
            return {"installed": False, "stale": False}
        prog = p.get("ProgramArguments") or [""]
        env = p.get("EnvironmentVariables") or {}
        # it can't start any more (Python gone, folder moved) or it runs another copy of the code
        stale = not os.path.exists(prog[0]) or os.path.abspath(env.get("PYTHONPATH") or "") != PKG_PARENT
        return {"installed": True, "stale": stale}

    def _launchctl(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        r = self.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=20)
        if check and r.returncode != 0:
            print(f"[luoke-mail] launchctl {' '.join(args)}: {r.returncode} {r.stderr.strip()}", file=sys.stderr)
            raise DesktopError("无法添加登录项。请在「系统设置 → 通用 → 登录项与扩展」中确认是否允许。")
        return r

    def install(self, port: int) -> None:
        os.makedirs(self.dir, exist_ok=True)
        self._launchctl("bootout", f"{self.domain}/{self.label}")          # an older copy, if loaded
        with open(self.path, "wb") as f:
            plistlib.dump(self.plist(port), f)
        try:
            self._launchctl("bootstrap", self.domain, self.path, check=True)
        except DesktopError:
            os.remove(self.path)
            raise

    def uninstall(self) -> None:
        """Remove the file first: when the service itself asks, bootout ends this very process."""
        try:
            os.remove(self.path)
        except OSError:
            pass
        self._launchctl("bootout", f"{self.domain}/{self.label}")

    def kickstart(self) -> None:
        self._launchctl("kickstart", f"{self.domain}/{self.label}")


# ---- macOS launcher app ----------------------------------------------------------------------------------
def _as_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Launcher:
    def __init__(self, home: str = CONFIG_DIR, apps_dir: str | None = None, run=subprocess.run):
        self.home = os.path.abspath(home)
        sfx = _suffix(self.home)
        self.name = APP_NAME + (f"（{sfx}）" if sfx else "")
        self.bundle_id = BUNDLE_ID + (f".{sfx}" if sfx else "")
        # from 螺壳邮件助手.app, that name in ~/Applications is the app itself: the notifier lives in the data folder
        self.dir = apps_dir or (os.path.join(self.home, "notifier") if packaged() else os.path.join(os.path.expanduser("~"), "Applications"))
        self.path = os.path.join(self.dir, self.name + ".app")
        self.run = run

    def exists(self) -> bool:
        return os.path.isdir(self.path)

    def command(self) -> str:
        env = f"PYTHONPATH={shlex.quote(PKG_PARENT)} LUOKE_MAIL_HOME={shlex.quote(self.home)} PYTHONIOENCODING=utf-8"
        return f"{env} {shlex.quote(sys.executable)} -m luoke_mail"

    def script(self) -> str:
        """Opened by the user (or by clicking a notification): open the page. Started by the service with a
        notification waiting: post it under our name, then quit."""
        cmd = _as_string(self.command())
        return f"""property cmd : {cmd}

on run
	set msg to do shell script cmd & " --take-notification"
	if msg is "" then
		openPage()
	else
		set ps to paragraphs of msg
		if (count of ps) < 3 then set ps to ps & {{"", "", ""}}
		display notification (item 3 of ps) with title (item 1 of ps) subtitle (item 2 of ps)
		delay 1
	end if
end run

on reopen
	openPage()
end reopen

on openPage()
	try
		do shell script cmd & " --open"
	on error errMsg
		display alert {_as_string(APP_NAME)} message errMsg as warning
	end try
end openPage
"""

    def install(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        work = tempfile.mkdtemp(prefix="luoke-launcher-")
        try:
            src = os.path.join(work, "main.applescript")
            app = os.path.join(work, "app.app")
            with open(src, "w", encoding="utf-8") as f:
                f.write(self.script())
            for args in (["/usr/bin/osacompile", "-o", app, src], None, ["/usr/bin/codesign", "--force", "--sign", "-", app]):
                if args is None:
                    self._info_plist(app)
                    continue
                r = self.run(args, capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    print(f"[luoke-mail] {args[0]}: {r.stderr.strip()}", file=sys.stderr)
                    raise DesktopError("无法创建「螺壳邮件助手」App。")
            if os.path.exists(self.path):
                shutil.rmtree(self.path)
            shutil.move(app, self.path)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _info_plist(self, app: str) -> None:
        path = os.path.join(app, "Contents", "Info.plist")
        with open(path, "rb") as f:
            info = plistlib.load(f)
        info.update(CFBundleIdentifier=self.bundle_id, CFBundleName=self.name, CFBundleDisplayName=self.name,
                    LSUIElement=True)            # no Dock icon while it posts a notification
        for k in [k for k in info if k.startswith("NS") and k.endswith("UsageDescription")]:
            del info[k]                          # it asks for nothing
        with open(path, "wb") as f:
            plistlib.dump(info, f)

    def uninstall(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


# ---- notifications ---------------------------------------------------------------------------------------
def _one_line(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def take_notification(home: str = CONFIG_DIR, max_age: float = 600) -> str:
    """The launcher reads the waiting notification (title, subtitle, body: one per line) and it is gone."""
    path = os.path.join(home, "notification.txt")
    try:
        fresh = time.time() - os.path.getmtime(path) < max_age
        with open(path, encoding="utf-8") as f:
            text = f.read()
        os.remove(path)
    except OSError:
        return ""
    return text.strip("\n") if fresh else ""


class Desktop:
    """Everything the app needs from the operating system, in one place (tests swap in a fake)."""

    def __init__(self, home: str = CONFIG_DIR, agents_dir: str | None = None, apps_dir: str | None = None,
                 run=subprocess.run, platform: str = sys.platform):
        self.home = home
        self.platform = platform
        self.run = run
        self.login_item = LoginItem(home, agents_dir, run)
        self.launcher = Launcher(home, apps_dir, run)

    @property
    def supported(self) -> bool:
        """Can luoke-mail keep running after the terminal is closed and start at login? Only as 螺壳邮件助手.app,
        kept running by 螺壳主控 (AppDesktop). Run from source it never registers a login item of its own (the
        owner, 2026-10-16: no tool starts itself at login behind 螺壳主控's back): background sorting runs while
        the program is open, as on Windows and Linux."""
        return False

    def status(self) -> dict:
        return {"supported": False, "installed": False, "stale": False, "app_name": None}

    def enable(self, port: int) -> None:
        return

    def disable(self) -> None:
        """Switched off: a login item and launcher left by luoke-mail before 0.13.4 go too."""
        if self.platform != "darwin":
            return
        if self.login_item.status()["installed"]:
            self.login_item.uninstall()
        if self.launcher.exists():
            self.launcher.uninstall()

    def wait_for_service(self, since: float, timeout: float = 15) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if service_started_since(since, self.home):
                return True
            time.sleep(0.2)
        return False

    def kickstart(self) -> None:
        return

    # the launcher's exit codes (luoke-core b3c7131, V1 §8): 0 handed to the system, 3 not allowed, 4 refused by the
    # system, 5 no answer in 10 s
    LAUNCHER_RESULTS = {0: "posted", 3: "not allowed", 4: "failed", 5: "no answer"}

    def open_notification_settings(self) -> bool:
        """Open System Settings at this app's own notification page: once macOS has been told no, an app cannot ask
        again -- only the user can turn notifications back on there. The app's bundle id is read from the app the
        launcher is in. False outside the app."""
        launcher = os.environ.get("LUOKE_LAUNCHER")
        if self.platform != "darwin" or not launcher or not os.path.exists(launcher):
            return False
        import plistlib
        try:
            with open(os.path.join(os.path.dirname(os.path.dirname(launcher)), "Info.plist"), "rb") as f:
                bundle = plistlib.load(f).get("CFBundleIdentifier")
        except (OSError, ValueError):
            return False
        if not bundle:
            return False
        self.run(["/usr/bin/open", f"x-apple.systempreferences:com.apple.Notifications-Settings.extension?id={bundle}"],
                 capture_output=True, timeout=20)
        return True

    def notify_status(self) -> dict | None:
        """Whether macOS lets the app show notifications: {"authorization": not_determined / denied / authorized /
        provisional / ephemeral, "alert": enabled / disabled / not_supported}, asked of the app's launcher without
        notifying (`--notify-status`). None outside the app, with a launcher too old to say, or no answer."""
        launcher = os.environ.get("LUOKE_LAUNCHER")
        if self.platform != "darwin" or not launcher or not os.path.exists(launcher):
            return None
        try:
            r = self.run([launcher, "--notify-status"], capture_output=True, timeout=15)
            out = getattr(r, "stdout", b"") or b""
            out = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
            if getattr(r, "returncode", 1) != 0:
                return None
            got = json.loads(out.strip().splitlines()[-1])
            return {"authorization": str(got.get("authorization") or ""), "alert": str(got.get("alert") or "")}
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None

    def notify(self, title: str, subtitle: str, body: str) -> str:
        """Post a notification; returns how it went out. Mail text in it is passed as data, never as code. Each
        one is noted -- when, the title, how it went out and what came back -- so a notification that never showed
        can be traced (2026-10-20: none seen all afternoon)."""
        title, subtitle, body = _one_line(title, 40), _one_line(subtitle, 60), _one_line(body, 160)
        how, result = self._post(title, subtitle, body)
        self._note(title, subtitle, how, result)
        return how

    def _note(self, title: str, subtitle: str, how: str, result: str) -> None:
        print(f"[luoke-mail] notification ({how}, {result}): {title}：{subtitle}", file=sys.stderr)
        path = os.path.join(self.home, "run", "notifications.json")
        try:
            try:
                with open(path, encoding="utf-8") as f:
                    seen = json.load(f)
            except (OSError, ValueError):
                seen = []
            seen = (seen + [{"at": time.strftime("%Y-%m-%d %H:%M:%S"), "title": title, "subtitle": subtitle,
                             "how": how, "result": result}])[-100:]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _write_private(path, json.dumps(seen, ensure_ascii=False))
        except OSError as ex:
            print(f"[luoke-mail] notification not noted: {ex}", file=sys.stderr)

    def recent_notifications(self) -> list[dict]:
        try:
            with open(os.path.join(self.home, "run", "notifications.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _post(self, title: str, subtitle: str, body: str) -> tuple[str, str]:
        try:
            launcher = os.environ.get("LUOKE_LAUNCHER")
            if self.platform == "darwin" and launcher and os.path.exists(launcher):
                # 螺壳邮件助手.app's own launcher posts it under the app's name; clicking it opens the page
                r = self.run([launcher, "--notify", title, subtitle, body], capture_output=True, timeout=20)
                code = getattr(r, "returncode", 0)
                err = (getattr(r, "stderr", b"") or b"")
                err = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
                if err.strip():
                    print(f"[luoke-mail] launcher --notify: {err.strip()[:300]}", file=sys.stderr)
                return "app", self.LAUNCHER_RESULTS.get(code, f"exit {code}")
            if self.platform == "darwin":
                if self.launcher.exists():
                    _write_private(os.path.join(self.home, "notification.txt"), f"{title}\n{subtitle}\n{body}\n")
                    self.run(["/usr/bin/open", "-g", self.launcher.path], capture_output=True, timeout=20)
                    return "app", "posted"
                self.run(["/usr/bin/osascript", "-e", "on run argv",
                          "-e", "display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv)",
                          "-e", "end run", title, subtitle, body], capture_output=True, timeout=20)
                return "osascript", "posted"
            if shutil.which("notify-send"):
                self.run(["notify-send", "-a", APP_NAME, f"{title}：{subtitle}", body], capture_output=True, timeout=20)
                return "notify-send", "posted"
        except (OSError, subprocess.SubprocessError) as ex:
            print(f"[luoke-mail] notification failed: {ex}", file=sys.stderr)
            return "failed", str(ex)[:120]
        return "log", "no way to post"


LOG_MOST = 2_000_000
STAMP_ENV = "LUOKE_MAIL_STAMP"       # set for a process whose stderr is a log file (start_detached)


def _rotate(path: str, most: int = LOG_MOST) -> None:
    """Kept to `most` bytes, with one older file beside it."""
    try:
        if os.path.exists(path) and os.path.getsize(path) > most:
            os.replace(path, path + ".1")
    except OSError:
        pass


class Stamped(io.TextIOBase):
    """stderr going to a log file: each line starts with the time, and the same line again within a minute is
    counted rather than written (luoke-mail#24: 100,000 lines of one 「hub unreachable」 with no time on them)."""
    QUIET = 60

    def __init__(self, raw, clock=time.time):
        self.raw, self.clock = raw, clock
        self.part, self.last, self.last_at, self.repeats = "", None, float("-inf"), 0
        self.lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        with self.lock:
            self.part += s
            while "\n" in self.part:
                line, self.part = self.part.split("\n", 1)
                self._line(line)
        return len(s)

    def _line(self, line: str) -> None:
        now = self.clock()
        if line == self.last and now - self.last_at < self.QUIET:
            self.repeats += 1
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        if self.repeats:
            self.raw.write(f"{stamp} （上一行又出现 {self.repeats} 次）\n")
        self.raw.write(f"{stamp} {line}\n")
        self.raw.flush()
        self.last, self.last_at, self.repeats = line, now, 0

    def flush(self) -> None:
        self.raw.flush()

    def isatty(self) -> bool:
        return False


def stamp_stderr() -> None:
    """Put the time on what this process says on stderr (a log file, not a terminal)."""
    if not isinstance(sys.stderr, Stamped):
        sys.stderr = Stamped(sys.stderr)


def log_to_file(path: str, most: int = LOG_MOST) -> None:
    """The resident service (started by 螺壳主控, its stderr nowhere) writes what it says to a file in the data
    folder, kept to `most` bytes with one older file beside it. stdout is left alone: it carries the page's token."""
    _rotate(path, most)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(fd, 2)
        os.close(fd)
        sys.stderr = Stamped(os.fdopen(2, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False))
    except OSError:
        pass


# ---- inside 螺壳邮件助手.app, with 螺壳主控 ------------------------------------------------------------------------
def launcher_path() -> str | None:
    """The app's launcher (luoke-core's compiled one), when running inside 螺壳邮件助手.app: it keeps the app's
    process, runs our entry as its child, posts notifications under the app's name."""
    p = os.environ.get("LUOKE_LAUNCHER")
    return p if p and os.path.exists(p) else None


class AppDesktop(Desktop):
    """The same jobs from inside 螺壳邮件助手.app (luoke-core V1 §8): no login item of our own -- background
    sorting is a resident accessory, started at login and kept running by 螺壳主控 (`PUT /accessory/resident`),
    which starts the app with `--resident`; a resident instance that stops on purpose says so first
    (`POST /accessory/exiting`) so the hub does not start it again. Notifications go through the launcher."""

    def __init__(self, launcher: str, home: str = CONFIG_DIR, run=subprocess.run, hub_link=None,
                 agents_dir: str | None = None, apps_dir: str | None = None):
        super().__init__(home, agents_dir, run=run, platform="darwin")
        self.legacy_apps = apps_dir or os.path.join(os.path.expanduser("~"), "Applications")
        self.launcher_exe = launcher
        self.app = os.path.dirname(os.path.dirname(os.path.dirname(launcher)))     # <app>/Contents/MacOS/<exe>
        self.marker = os.path.join(home, "resident")
        self.hub = hub_link                 # () -> hub.Hub; set by the app (desktop knows nothing of the hub)

    @property
    def supported(self) -> bool:
        return True

    def status(self) -> dict:
        return {"supported": True, "installed": os.path.exists(self.marker), "stale": False, "app_name": APP_NAME}

    def _declare(self, on: bool) -> None:
        if self.hub is None:
            raise DesktopError("螺壳没有运行。")
        try:
            self.hub().call("PUT", "/accessory/resident", {"resident": on})
        except Exception as ex:            # engines.EngineError: not running, or it refused
            print(f"[luoke-mail] resident={on} not declared: {ex}", file=sys.stderr)
            raise DesktopError("螺壳没有运行，无法在登录时启动后台自动整理。") from ex

    def enable(self, port: int) -> None:
        self._declare(True)
        _write_private(self.marker, "1\n")
        self.kickstart()                     # the hub starts it at login; for now, start it here

    def disable(self) -> None:
        try:
            self._declare(False)
        except DesktopError:
            pass                             # not declared again at the next login anyway: the marker is gone
        try:
            os.remove(self.marker)
        except OSError:
            pass

    def kickstart(self) -> None:
        # -n: asked from inside the app itself (its opener is still running), plain `open` would only reopen it
        self.run(["/usr/bin/open", "-n", "-g", "-a", self.app, "--args", "--resident"], capture_output=True, timeout=20)

    def exiting(self) -> None:
        """The resident instance stops on purpose (background sorting switched off, data cleared)."""
        try:
            if self.hub is not None:
                self.hub().call("POST", "/accessory/exiting", {})
        except Exception as ex:
            print(f"[luoke-mail] exiting not told: {ex}", file=sys.stderr)

    def migrate(self) -> bool:
        """Before 螺壳主控, a LaunchAgent of our own kept luoke-mail running and an AppleScript launcher posted its
        notifications. The first time the app opens, both go (the old service with them); returns whether
        background sorting was on, to be handed to the hub."""
        old = self.login_item
        if not old.status()["installed"]:
            return False
        old.uninstall()
        legacy = Launcher(self.home, run=self.run)
        legacy.dir = self.legacy_apps
        legacy.path = os.path.join(legacy.dir, legacy.name + ".app")
        try:
            with open(os.path.join(legacy.path, "Contents", "Info.plist"), "rb") as f:
                if plistlib.load(f).get("CFBundleIdentifier") == legacy.bundle_id:     # the old launcher, not this app
                    legacy.uninstall()
        except (OSError, ValueError):
            pass
        return True


def for_this_app(home: str = CONFIG_DIR, hub_link=None) -> Desktop:
    """The desktop luoke-mail runs on: inside 螺壳邮件助手.app with 螺壳主控, or from a terminal (no login item: background
    sorting while it is open)."""
    launcher = launcher_path()
    return AppDesktop(launcher, home, hub_link=hub_link) if launcher and sys.platform == "darwin" else Desktop(home)
