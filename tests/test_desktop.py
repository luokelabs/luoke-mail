"""Desktop integration in temporary folders: the page token, one instance per data folder, the login item
(launchctl is faked), the launcher app (really compiled on macOS, never opened) and notifications."""
import http.client
import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import desktop, server  # noqa: E402
from luoke_mail.credstore import FileStore  # noqa: E402
from luoke_mail.store import Store  # noqa: E402


def free_port() -> int:
    """A port nothing listens on now: tests name their port (never the default one, where the assistant in daily use
    runs), and two test runs side by side must not pick the same."""
    import socket as sk
    with sk.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeRun:
    """Records commands instead of running them; `fail` makes the named tool exit with an error."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        code = 1 if any(f in " ".join(args) for f in self.fail) else 0
        return subprocess.CompletedProcess(args, code, "", "boom" if code else "")


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class TokenAndInstanceTests(unittest.TestCase):
    def test_token_survives_restarts_and_is_private(self):
        home = os.path.join(tempfile.mkdtemp(), "data")
        t1 = desktop.load_token(home)
        self.assertEqual(desktop.load_token(home), t1)
        self.assertEqual(mode(os.path.join(home, "token")), 0o600)
        self.assertEqual(mode(home), 0o700)                    # mail text lives here too
        self.assertEqual(desktop.page_url(8765, t1), f"http://127.0.0.1:8765/?t={t1}")

    def test_one_instance_per_data_folder(self):
        home = tempfile.mkdtemp()
        first, second = desktop.InstanceLock(home), desktop.InstanceLock(home)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        self.assertFalse(second.acquire(wait=0.3))
        threading.Timer(0.3, first.release).start()            # the service waits for the terminal server to let go
        self.assertTrue(second.acquire(wait=5))
        second.release()

    def test_ping_finds_the_running_server(self):
        home = tempfile.mkdtemp()
        app = server.App(secret_store=FileStore(os.path.join(home, "c.json")), state_store=Store(os.path.join(home, "s.db")))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
        app.port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            p = desktop.ping(app.port, app.token)
            self.assertEqual((p["pid"], p["service"]), (os.getpid(), False))
            self.assertIsNone(desktop.ping(app.port, "wrong-token"))
            c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=5)
            c.request("GET", "/api/activity", headers={"X-Luoke-Token": app.token, "Host": f"127.0.0.1:{app.port}"})
            act = json.loads(c.getresponse().read())
            self.assertIsNone(act["job"])
            self.assertFalse(act["auto"]["enabled"])
            c.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            app.store.close()
        self.assertIsNone(desktop.ping(app.port, app.token))

    def test_service_start_marker(self):
        home = tempfile.mkdtemp()
        t0 = time.time() - 1
        self.assertFalse(desktop.service_started_since(t0, home))
        desktop.mark_service_started(home)
        self.assertTrue(desktop.service_started_since(t0, home))
        self.assertFalse(desktop.service_started_since(time.time() + 5, home))   # an old marker doesn't count


class LoginItemTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.agents = tempfile.mkdtemp()

    def test_install_writes_a_user_launch_agent(self):
        run = FakeRun()
        li = desktop.LoginItem(self.home, self.agents, run)
        self.assertTrue(li.label.startswith("local.luoke-mail."))   # a copied data folder never replaces the real one
        li.install(8765)
        with open(li.path, "rb") as f:
            p = plistlib.load(f)
        self.assertEqual(p["ProgramArguments"], [sys.executable, "-m", "luoke_mail", "--service", "--port", "8765"])
        self.assertEqual(p["EnvironmentVariables"]["PYTHONPATH"], ROOT)
        self.assertEqual(p["EnvironmentVariables"]["LUOKE_MAIL_HOME"], os.path.abspath(self.home))
        self.assertEqual(p["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue(p["StandardOutPath"].startswith(os.path.abspath(self.home)))
        self.assertEqual([c[1] for c in run.calls], ["bootout", "bootstrap"])
        self.assertEqual(run.calls[1][2:], [f"gui/{os.getuid()}", li.path])
        self.assertEqual(li.status(), {"installed": True, "stale": False})

        p["EnvironmentVariables"]["PYTHONPATH"] = "/old/place"          # the folder was moved
        with open(li.path, "wb") as f:
            plistlib.dump(p, f)
        self.assertTrue(li.status()["stale"])

        li.uninstall()
        self.assertFalse(os.path.exists(li.path))
        self.assertEqual(run.calls[-1][1], "bootout")
        self.assertEqual(li.status(), {"installed": False, "stale": False})

    def test_failed_bootstrap_leaves_nothing_behind(self):
        li = desktop.LoginItem(self.home, self.agents, FakeRun(fail=["bootstrap"]))
        with self.assertRaises(desktop.DesktopError):
            li.install(8765)
        self.assertFalse(os.path.exists(li.path))

    def test_default_folder_uses_the_plain_names(self):
        li = desktop.LoginItem(desktop.DEFAULT_HOME, self.agents, FakeRun())
        self.assertEqual(li.label, "local.luoke-mail")
        self.assertEqual(desktop.Launcher(desktop.DEFAULT_HOME, self.agents).name, "螺壳邮件助手")


class LogTests(unittest.TestCase):
    """luoke-mail#24: app.log had 105,612 lines in a few days, nearly all one hub error, none with a time."""

    def test_lines_get_the_time_and_a_repeat_is_counted_not_written(self):
        import io
        t = [1_000_000.0]
        out = io.StringIO()
        log = desktop.Stamped(out, clock=lambda: t[0])
        for _ in range(500):
            log.write("[luoke-mail] hub unreachable: ConnectionRefusedError\n")
            t[0] += 0.05
        log.write("Traceback (most recent call last):\n  File \"x\"")
        log.write(", line 1\n")
        t[0] += 120
        log.write("[luoke-mail] hub unreachable: ConnectionRefusedError\n")       # a minute on: written again
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 5, lines)
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[luoke-mail\] hub unreachable")
        self.assertIn("又出现 499 次", lines[1])
        self.assertTrue(lines[3].endswith('File "x", line 1'))
        self.assertIn("hub unreachable", lines[4])

    def test_a_big_log_is_set_aside_at_start(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "app.log")
        with open(path, "w") as f:
            f.write("x" * 100)
        desktop._rotate(path, most=50)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(path + ".1"))


class LauncherAndNotificationTests(unittest.TestCase):
    def setUp(self):
        self.home = os.path.join(tempfile.mkdtemp(), 'my "mail" data')   # quotes and spaces must survive two languages
        os.makedirs(self.home)
        self.apps = tempfile.mkdtemp()

    def test_script_quotes_paths(self):
        s = desktop.Launcher(self.home, self.apps).script()
        self.assertIn('my \\"mail\\" data', s)
        self.assertIn("--take-notification", s)
        self.assertIn("on reopen", s)

    @unittest.skipUnless(sys.platform == "darwin" and os.path.exists("/usr/bin/osacompile"), "macOS only")
    def test_builds_a_signed_background_app(self):
        la = desktop.Launcher(self.home, self.apps)
        la.install()
        with open(os.path.join(la.path, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
        self.assertTrue(info["CFBundleIdentifier"].startswith("local.luoke-mail.launcher."))
        self.assertTrue(info["LSUIElement"])
        self.assertFalse([k for k in info if k.endswith("UsageDescription")])
        self.assertEqual(subprocess.run(["/usr/bin/codesign", "--verify", la.path], capture_output=True).returncode, 0)
        la.uninstall()
        self.assertFalse(la.exists())

    def test_notification_goes_through_the_launcher(self):
        run = FakeRun()
        d = desktop.Desktop(self.home, tempfile.mkdtemp(), self.apps, run, platform="darwin")
        os.makedirs(d.launcher.path)
        self.assertEqual(d.notify("螺壳邮件助手", "1 封新邮件需要你处理", "回复：王总「预算\n评审」"), "app")
        self.assertEqual(run.calls[-1], ["/usr/bin/open", "-g", d.launcher.path])
        self.assertEqual(mode(os.path.join(self.home, "notification.txt")), 0o600)
        self.assertEqual(desktop.take_notification(self.home), "螺壳邮件助手\n1 封新邮件需要你处理\n回复：王总「预算 评审」")
        self.assertEqual(desktop.take_notification(self.home), "")          # read once

        d.notify("t", "s", "b")
        old = time.time() - 3600
        os.utime(os.path.join(self.home, "notification.txt"), (old, old))
        self.assertEqual(desktop.take_notification(self.home), "")          # stale: the click came much later

    def test_from_the_packaged_app_its_own_launcher_posts_the_notification(self):
        run = FakeRun()
        d = desktop.Desktop(self.home, tempfile.mkdtemp(), self.apps, run, platform="darwin")
        launcher = os.path.join(tempfile.mkdtemp(), "luoke-mail")
        open(launcher, "w").close()
        old = os.environ.get("LUOKE_LAUNCHER")
        os.environ["LUOKE_LAUNCHER"] = launcher                  # set by 螺壳邮件助手.app's launcher for its child
        self.addCleanup(lambda: os.environ.__setitem__("LUOKE_LAUNCHER", old) if old else os.environ.pop("LUOKE_LAUNCHER", None))
        self.assertEqual(d.notify("螺壳邮件助手", "1 封新邮件需要你处理", 'x" & do shell script "rm'), "app")
        self.assertEqual(run.calls[-1], [launcher, "--notify", "螺壳邮件助手", "1 封新邮件需要你处理", 'x" & do shell script "rm'])

    def test_opening_the_app_starts_the_assistant_and_gives_its_page(self):
        env = {**os.environ, "LUOKE_MAIL_HOME": self.home, "LUOKE_HOME": os.path.join(self.home, "mem"),
               "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"}
        port = free_port()
        out = subprocess.run([sys.executable, "-m", "luoke_mail", "--app", "--no-browser", "--port", str(port)], capture_output=True,
                             text=True, env=env, timeout=60, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        url = out.stdout.strip()
        self.assertTrue(url.startswith(f"http://127.0.0.1:{port}/"), out.stdout)
        port, token = int(url.split(":")[2].split("/")[0]), url.split("t=")[1]
        info = desktop.ping(port, token)
        self.addCleanup(lambda: os.kill(info["pid"], 15))         # the assistant runs on its own; stop it here
        self.assertTrue(info and info["ok"])
        again = subprocess.run([sys.executable, "-m", "luoke_mail", "--app", "--reopen", "--no-browser"], capture_output=True,
                               text=True, env=env, timeout=60, encoding="utf-8")
        self.assertEqual(again.stdout.strip(), url)                # opened again: the same one, not a second

    def test_started_by_the_hub_at_login_into_a_fresh_data_folder(self):
        home = os.path.join(self.home, "not-yet")
        env = {**os.environ, "LUOKE_MAIL_HOME": home, "LUOKE_HOME": os.path.join(self.home, "mem"),
               "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"}
        port = free_port()
        p = subprocess.Popen([sys.executable, "-m", "luoke_mail", "--app", "--resident", "--port", str(port)], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(lambda: (p.poll() is None and p.terminate(), p.wait(), p.stderr.close()))
        for _ in range(60):
            info = desktop.read_server_info(home)
            if info or p.poll() is not None:
                break
            time.sleep(0.5)
        self.assertIsNone(p.poll(), p.stderr.read().decode() if p.poll() is not None else "")
        self.assertEqual((info["port"], info["service"]), (port, True))
        self.assertEqual(stat.S_IMODE(os.stat(home).st_mode), 0o700)

    def test_the_apps_log_is_kept_by_the_service_not_emptied_on_start(self):
        from luoke_mail import server
        log = os.path.join(self.home, "service.log")
        launcher = os.path.join(tempfile.mkdtemp(), "launcher")
        open(launcher, "w").close()
        for env, left in (({"LUOKE_LAUNCHER": launcher}, 2000), ({"LUOKE_LAUNCHER": ""}, 0)):
            with open(log, "w") as f:
                f.write("x" * 2000)
            with mock.patch.dict(os.environ, env):
                server._trim_log(log, limit=1000)
            self.assertEqual(os.path.getsize(log), left)       # in the app: desktop.log_to_file keeps it small

    def test_a_second_background_instance_steps_aside_instead_of_waiting(self):
        home = os.path.join(self.home, "data")
        env = {**os.environ, "LUOKE_MAIL_HOME": home, "LUOKE_HOME": os.path.join(self.home, "mem"),
               "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"}
        argv = [sys.executable, "-m", "luoke_mail", "--app", "--resident"]
        env["LUOKE_MAIL_PORT"] = str(free_port())                       # as an app started by macOS gets it
        first = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (first.terminate(), first.wait()))
        for _ in range(60):
            if desktop.read_server_info(home):
                break
            time.sleep(0.5)
        second = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30, encoding="utf-8")
        self.assertEqual(second.returncode, 0)
        with open(os.path.join(home, "service.log"), encoding="utf-8") as f:   # the resident one logs to a file
            log = f.read()
        self.assertIn("已在运行", log)
        self.assertIn("resident", log)
        self.assertIsNone(first.poll())                                   # the first one goes on

    def test_the_first_open_starts_one_background_instance_not_two(self):
        from fakes import FakeHub
        h = FakeHub(lambda t, b, r: "{}").__enter__()
        self.addCleanup(h.__exit__)
        d, run, app = self.app_desktop(h.path)
        os.makedirs(d.login_item.dir, exist_ok=True)
        with open(d.login_item.path, "wb") as f:
            plistlib.dump(d.login_item.plist(8765), f)
        answers = iter([None] * 3 + ["http://127.0.0.1:8765/?t=x"] * 5)
        saved = desktop.for_this_app, desktop.running
        desktop.for_this_app, desktop.running = (lambda **k: d), (lambda *a: next(answers))
        self.addCleanup(lambda: (setattr(desktop, "for_this_app", saved[0]), setattr(desktop, "running", saved[1])))
        self.assertEqual(server.open_or_start(open_browser=False), 0)
        self.assertEqual(len([c for c in run.calls if c[:2] == ["/usr/bin/open", "-n"]]), 1)
        self.assertEqual(h.calls, [("resident", {"resident": True})])

    def test_a_port_named_is_that_one_or_none(self):
        import socket as sk
        taken = sk.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        self.addCleanup(taken.close)
        env = {**os.environ, "LUOKE_MAIL_HOME": os.path.join(self.home, "data"), "LUOKE_HOME": os.path.join(self.home, "mem"),
               "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"}
        out = subprocess.run([sys.executable, "-m", "luoke_mail", "--no-browser", "--port", str(port)], capture_output=True,
                             text=True, env=env, timeout=60, encoding="utf-8")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(f"端口 {port} 已被占用", out.stderr)

    def test_a_memory_written_by_a_newer_luoke_mem_is_said_not_crashed_on(self):
        import sqlite3
        os.makedirs(os.path.join(self.home, "mem"))
        db = sqlite3.connect(os.path.join(self.home, "mem", "mem.db"))
        db.execute("PRAGMA user_version = 5002")
        db.close()
        env = {**os.environ, "LUOKE_MAIL_HOME": self.home, "LUOKE_HOME": os.path.join(self.home, "mem"),
               "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"}
        out = subprocess.run([sys.executable, "-m", "luoke_mail", "--no-browser", "--port", str(free_port())], capture_output=True,
                             text=True, env=env, timeout=60, encoding="utf-8")
        self.assertEqual(out.returncode, server.TOO_NEW_EXIT)
        self.assertIn("请更新螺壳邮件助手", out.stderr)
        self.assertNotIn("Traceback", out.stderr)

    def app_desktop(self, hub_path):
        from luoke_mail import hub
        app = os.path.join(tempfile.mkdtemp(), "螺壳邮件助手.app")
        os.makedirs(os.path.join(app, "Contents", "MacOS"))
        exe = os.path.join(app, "Contents", "MacOS", "luoke-mail")
        open(exe, "w").close()
        run = FakeRun()
        d = desktop.AppDesktop(exe, self.home, run, hub_link=lambda: hub.Hub(hub_path), agents_dir=tempfile.mkdtemp(),
                               apps_dir=self.apps)
        return d, run, app

    def test_inside_the_app_background_sorting_is_the_hubs_resident_accessory(self):
        from fakes import FakeHub
        with FakeHub(lambda t, b, r: "{}") as h:
            d, run, app = self.app_desktop(h.path)
            self.assertEqual(d.status()["installed"], False)
            d.enable(8765)
            self.assertEqual(h.calls, [("resident", {"resident": True})])
            self.assertEqual(run.calls[-1], ["/usr/bin/open", "-n", "-g", "-a", app, "--args", "--resident"])   # now, not only at login
            self.assertTrue(d.status()["installed"])
            self.assertFalse([c for c in run.calls if "launchctl" in c[0]])              # no login item of our own
            d.exiting()
            d.disable()
            self.assertEqual(h.calls[1:], [("exiting", {}), ("resident", {"resident": False})])
            self.assertFalse(d.status()["installed"])
        d, run, app = self.app_desktop("/tmp/no-such-luoke-hub.sock")
        with self.assertRaises(desktop.DesktopError):                                    # no hub: it says so
            d.enable(8765)
        self.assertFalse(d.status()["installed"])

    def test_the_first_time_the_app_opens_the_old_login_item_and_launcher_go(self):
        d, run, app = self.app_desktop("/tmp/no-such-luoke-hub.sock")
        self.assertFalse(d.migrate())                                                    # nothing from before
        os.makedirs(d.login_item.dir, exist_ok=True)
        with open(d.login_item.path, "wb") as f:
            plistlib.dump(d.login_item.plist(8765), f)
        old = desktop.Launcher(self.home, self.apps, run)
        os.makedirs(os.path.join(old.path, "Contents"))
        with open(os.path.join(old.path, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": old.bundle_id}, f)
        self.assertTrue(d.migrate())
        self.assertFalse(os.path.exists(d.login_item.path))
        self.assertTrue(any(c[:2] == ["/bin/launchctl", "bootout"] for c in run.calls))
        self.assertFalse(os.path.exists(old.path))

    def test_inside_the_app_notifications_go_through_its_launcher(self):
        d, run, app = self.app_desktop("/tmp/no-such-luoke-hub.sock")
        old = os.environ.get("LUOKE_LAUNCHER")
        os.environ["LUOKE_LAUNCHER"] = d.launcher_exe
        self.addCleanup(lambda: os.environ.__setitem__("LUOKE_LAUNCHER", old) if old else os.environ.pop("LUOKE_LAUNCHER", None))
        self.assertIsInstance(desktop.for_this_app(self.home), desktop.AppDesktop)
        d.notify("螺壳邮件助手", "后台自动整理已开启", "每小时整理一次")
        self.assertEqual(run.calls[-1][:2], [d.launcher_exe, "--notify"])

    def test_keychain_says_found_not_found_or_cannot_read_now(self):
        from luoke_mail import credstore
        fake = os.path.join(tempfile.mkdtemp(), "security")
        for code, out in ((0, "pw"), (44, None), (36, "locked")):
            with open(fake, "w") as f:
                f.write(f"#!/bin/sh\necho pw\nexit {code}\n")
            os.chmod(fake, 0o755)
            store = credstore.KeychainStore(fake)
            if out == "locked":
                with self.assertRaises(credstore.SecretUnavailable):
                    store.get("imap", "me@example.com")
            else:
                self.assertEqual(store.get("imap", "me@example.com"), out)

    def test_a_copy_of_the_data_folder_has_its_own_keychain_entries(self):
        """A development 螺壳 or an experiment (LUOKE_MAIL_HOME elsewhere) never overwrites or deletes the real
        folder's app passwords: entries are keyed by address, so they get the folder's tag."""
        from luoke_mail import config, credstore
        self.assertEqual(credstore.service("imap", config.DEFAULT_DIR), "luoke-mail:imap")        # as it always was
        self.assertEqual(credstore.service("imap", config.DEFAULT_DIR + "/"), "luoke-mail:imap")
        copy = credstore.service("imap", "/tmp/elsewhere")
        self.assertRegex(copy, r"^luoke-mail:imap:[0-9a-f]{8}$")
        self.assertNotEqual(copy, credstore.service("imap", "/tmp/another"))
        fake = os.path.join(tempfile.mkdtemp(), "security")
        said = fake + ".args"
        with open(fake, "w") as f:
            f.write(f'#!/bin/sh\necho "$@" >> {said}\nexit 0\n')
        os.chmod(fake, 0o755)
        credstore.KeychainStore(fake, "/tmp/elsewhere").delete("imap", "me@example.com")
        credstore.KeychainStore(fake).get("imap", "me@example.com")        # the tests' data folder is a copy too
        with open(said) as f:
            lines = f.read().splitlines()
        self.assertIn(f"-s {copy} -a me@example.com", lines[0])
        self.assertIn(f"-s {credstore.service('imap', config.CONFIG_DIR)} -a", lines[1])
        self.assertNotIn("-s luoke-mail:imap -a", "\n".join(lines))

    def test_each_notification_is_noted_with_what_came_back(self):
        """2026-10-20: none seen all afternoon and nothing to tell why -- each one is noted: how it went out, and
        what the app's launcher said (a newer launcher exits 3 when macOS has not allowed notifications)."""
        launcher = os.path.join(tempfile.mkdtemp(), "launcher")
        open(launcher, "w").close()
        codes = iter([0, 3])

        class Ran:
            def __init__(self, code):
                self.returncode, self.stderr = code, b"not authorized" if code else b""
        run = lambda args, **k: Ran(next(codes))  # noqa: E731
        d = desktop.Desktop(self.home, tempfile.mkdtemp(), self.apps, run, platform="darwin")
        with mock.patch.dict(os.environ, {"LUOKE_LAUNCHER": launcher}):
            d.notify("螺壳邮件助手", "1 封新邮件需要你处理", "回复：王总")
            d.notify("螺壳邮件助手", "测试通知", "x")
        seen = d.recent_notifications()
        self.assertEqual([(n["how"], n["result"]) for n in seen], [("app", "posted"), ("app", "not allowed")])
        self.assertEqual(mode(os.path.join(self.home, "run", "notifications.json")), 0o600)

    def test_notification_settings_open_at_the_apps_own_page(self):
        import plistlib
        app = os.path.join(tempfile.mkdtemp(), "X.app", "Contents")
        os.makedirs(os.path.join(app, "MacOS"))
        launcher = os.path.join(app, "MacOS", "luoke-mail")
        open(launcher, "w").close()
        with open(os.path.join(app, "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "ai.luoke.mail"}, f)
        calls = []
        d = desktop.Desktop(self.home, tempfile.mkdtemp(), self.apps, lambda args, **k: calls.append(args), platform="darwin")
        self.assertFalse(d.open_notification_settings())                     # not in the app
        with mock.patch.dict(os.environ, {"LUOKE_LAUNCHER": launcher}):
            self.assertTrue(d.open_notification_settings())
        self.assertEqual(calls, [["/usr/bin/open",
                                  "x-apple.systempreferences:com.apple.Notifications-Settings.extension?id=ai.luoke.mail"]])

    def test_whether_macos_allows_notifications_is_asked_of_the_launcher(self):
        launcher = os.path.join(tempfile.mkdtemp(), "launcher")
        open(launcher, "w").close()
        answers = iter([(0, b'{"authorization": "denied", "alert": "disabled"}\n'), (64, b""), (0, b"not json")])

        class Ran:
            def __init__(self, code, out):
                self.returncode, self.stdout = code, out
        calls = []

        def run(args, **k):
            calls.append(args)
            return Ran(*next(answers))
        d = desktop.Desktop(self.home, tempfile.mkdtemp(), self.apps, run, platform="darwin")
        self.assertIsNone(d.notify_status())                                  # not in the app: nothing to ask
        with mock.patch.dict(os.environ, {"LUOKE_LAUNCHER": launcher}):
            self.assertEqual(d.notify_status(), {"authorization": "denied", "alert": "disabled"})
            self.assertEqual(calls[-1], [launcher, "--notify-status"])
            self.assertIsNone(d.notify_status())                              # an older launcher: can't tell
            self.assertIsNone(d.notify_status())

    def test_notification_without_launcher_passes_text_as_data(self):
        run = FakeRun()
        d = desktop.Desktop(self.home, tempfile.mkdtemp(), self.apps, run, platform="darwin")
        d.notify("螺壳邮件助手", "1 封", 'x" & do shell script "rm')
        args = run.calls[-1]
        self.assertEqual(args[0], "/usr/bin/osascript")
        self.assertEqual(args[-1], 'x" & do shell script "rm')             # an argument, never part of the script
        self.assertNotIn("rm", " ".join(args[:-3]))

    def test_unsupported_platform_does_nothing_to_the_system(self):
        run = FakeRun()
        d = desktop.Desktop(self.home, tempfile.mkdtemp(), self.apps, run, platform="linux")
        self.assertFalse(d.supported)
        d.enable(8765)
        d.disable()
        self.assertEqual(run.calls, [])
        self.assertEqual(d.status()["supported"], False)

    def test_take_notification_command(self):
        with open(os.path.join(self.home, "notification.txt"), "w", encoding="utf-8") as f:
            f.write("螺壳邮件助手\n标题\n正文\n")
        env = {**os.environ, "LUOKE_MAIL_HOME": self.home, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"}
        out = subprocess.run([sys.executable, "-m", "luoke_mail", "--take-notification"], capture_output=True, text=True,
                             env=env, timeout=30, encoding="utf-8")
        self.assertEqual(out.stdout, "螺壳邮件助手\n标题\n正文\n")
        self.assertFalse(os.path.exists(os.path.join(self.home, "notification.txt")))


if __name__ == "__main__":
    unittest.main()
