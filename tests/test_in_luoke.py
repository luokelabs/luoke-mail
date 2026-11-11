"""Inside 螺壳.app: the body starts this code with its own Python. The pipe to the body, the desktop that goes with it, and
the real process: `python -m luoke_mail --resident --parent-pid`, its answers and how it stops."""
import json
import os
import plistlib
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import desktop, memory, server  # noqa: E402


def pipes(test):
    """A link and the body's end of it."""
    r1, w1 = os.pipe()
    r2, w2 = os.pipe()
    files = [os.fdopen(r1, "r", encoding="utf-8"), os.fdopen(w2, "w", encoding="utf-8"), os.fdopen(w1, "w", encoding="utf-8"),
             os.fdopen(r2, "r", encoding="utf-8")]
    test.addCleanup(lambda: [_close(f) for f in files[1:]])
    return desktop.BodyLink(files[0], files[1]).start(), files[2], files[3]


def _close(f):
    try:
        f.close()
    except (OSError, ValueError):
        pass


class Body(threading.Thread):
    """Answers each request the way the body does."""

    def __init__(self, out, inp, answer):
        super().__init__(daemon=True)
        self.out, self.inp, self.answer, self.seen = out, inp, answer, []
        self.start()

    def run(self):
        for line in self.inp:
            msg = json.loads(line)
            self.seen.append(msg)
            self.out.write(json.dumps({"id": msg["id"], **self.answer}) + "\n")
            self.out.flush()


class LinkTests(unittest.TestCase):
    def test_each_request_gets_its_own_answer(self):
        link, to_link, from_link = pipes(self)
        body = Body(to_link, from_link, {"result": "posted"})
        self.assertEqual(link.ask({"op": "notify"})["result"], "posted")
        self.assertEqual(link.ask({"op": "notify"})["id"], 2)
        self.assertEqual([m["op"] for m in body.seen], ["notify", "notify"])

    def test_no_answer_in_time_is_none(self):
        link, _, _keep = pipes(self)
        self.assertIsNone(link.ask({"op": "notify"}, timeout=0.05))
        self.assertEqual(link.waiting, {})

    def test_when_the_body_is_gone_nothing_waits(self):
        link, to_link, _keep = pipes(self)
        to_link.close()
        t0 = time.monotonic()
        while not link.closed and time.monotonic() - t0 < 5:
            time.sleep(0.01)
        self.assertIsNone(link.ask({"op": "notify"}, timeout=30))
        self.assertLess(time.monotonic() - t0, 5)

    def test_a_request_waiting_when_the_body_goes_comes_back(self):
        link, to_link, _keep = pipes(self)
        threading.Timer(0.1, to_link.close).start()
        t0 = time.monotonic()
        self.assertIsNone(link.ask({"op": "notify"}, timeout=30))
        self.assertLess(time.monotonic() - t0, 5)

    def test_the_body_opens_the_page(self):
        link, to_link, _keep = pipes(self)
        opened = threading.Event()
        link.on_open = opened.set
        to_link.write('{"op": "open"}\n')
        to_link.flush()
        self.assertTrue(opened.wait(5))

    def test_opening_the_page_never_holds_up_the_pipe(self):
        link, to_link, from_link = pipes(self)
        Body(to_link, from_link, {"result": "posted"})
        release = threading.Event()
        self.addCleanup(release.set)
        link.on_open = release.wait            # `open` can take its time
        to_link.write('{"op": "open"}\n')
        to_link.flush()
        self.assertEqual(link.ask({"op": "notify"}, timeout=5)["result"], "posted")

    def test_opening_the_page_failing_leaves_the_pipe_up(self):
        link, to_link, from_link = pipes(self)
        Body(to_link, from_link, {"result": "posted"})
        failed = threading.Event()

        def fail():
            failed.set()
            raise OSError("no more processes")
        link.on_open = fail
        with mock.patch.object(threading, "excepthook"):
            to_link.write('{"op": "open"}\n')
            to_link.flush()
            self.assertTrue(failed.wait(5))
            self.assertEqual(link.ask({"op": "notify"}, timeout=5)["result"], "posted")

    def test_a_line_it_cant_use_leaves_the_pipe_up(self):
        link, to_link, from_link = pipes(self)
        Body(to_link, from_link, {"result": "posted"})
        to_link.write('{"id": [1]}\n')
        to_link.flush()
        self.assertEqual(link.ask({"op": "notify"}, timeout=5)["result"], "posted")

    def test_a_line_that_is_not_json_is_passed_over(self):
        link, to_link, _keep = pipes(self)
        opened = threading.Event()
        link.on_open = opened.set
        to_link.write('not json\n[1]\n{"op": "open"}\n')
        to_link.flush()
        self.assertTrue(opened.wait(5))


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.calls = []

    def run_(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def desktop(self, link=None):
        return desktop.BodyDesktop(self.home, link, self.run_, agents_dir=tempfile.mkdtemp(), apps_dir=tempfile.mkdtemp())

    def test_background_sorting_is_only_the_setting(self):
        d = self.desktop()
        self.assertTrue(d.supported)
        self.assertEqual(d.status(), {"supported": True, "installed": False, "stale": False, "app_name": None, "in_luoke": True})
        d.enable(8765)
        d.kickstart()
        d.disable()
        self.assertEqual(self.calls, [])      # no second 螺壳 opened, no launchctl

    def test_a_notification_goes_to_the_body_and_what_came_back_is_noted(self):
        link, to_link, from_link = pipes(self)
        body = Body(to_link, from_link, {"result": "not allowed"})
        d = self.desktop(link)
        self.assertEqual(d.notify("t", "s", "b"), "app")
        self.assertEqual(body.seen[0]["title"], "t")
        self.assertEqual(body.seen[0]["op"], "notify")
        self.assertEqual(d.recent_notifications()[-1]["result"], "not allowed")
        self.assertEqual(self.calls, [])      # osascript never

    def test_a_notification_with_no_body_to_post_it_is_only_noted(self):
        d = self.desktop()
        self.assertEqual(d.notify("t", "s", "b"), "log")
        self.assertEqual(self.calls, [])

    def test_whether_macos_allows_notifications_is_asked_of_the_body(self):
        link, to_link, from_link = pipes(self)
        Body(to_link, from_link, {"authorization": "denied", "alert": "disabled"})
        self.assertEqual(self.desktop(link).notify_status(), {"authorization": "denied", "alert": "disabled"})
        self.assertIsNone(self.desktop().notify_status())

    def test_notification_settings_open_at_luokes_page(self):
        app = os.path.join(self.home, "螺壳.app")
        os.makedirs(os.path.join(app, "Contents"))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "ai.luoke.core"}, f)
        with mock.patch.object(desktop, "own_app", return_value=app):
            self.assertTrue(self.desktop().open_notification_settings())
        self.assertIn("id=ai.luoke.core", self.calls[-1][1])

    def test_where_the_code_is_decides_the_desktop_even_with_a_launcher_named(self):
        app = os.path.join(self.home, "螺壳.app")
        os.makedirs(os.path.join(app, "Contents", "Resources", "mail"))
        with mock.patch.object(desktop, "own_app", return_value=app):
            self.assertFalse(desktop.in_luoke())
            open(os.path.join(app, "Contents", "Resources", "mail", "luoke-accessory.json"), "w").close()
            self.assertTrue(desktop.in_luoke())
            with mock.patch.dict(os.environ, {"LUOKE_LAUNCHER": sys.executable}):
                self.assertIsInstance(desktop.for_this_app(self.home), desktop.BodyDesktop)

    def test_outside_luoke_nothing_changes(self):
        with mock.patch.dict(os.environ, {"LUOKE_LAUNCHER": sys.executable}):
            self.assertIsInstance(desktop.for_this_app(self.home), desktop.AppDesktop)
        with mock.patch.dict(os.environ):
            os.environ.pop("LUOKE_LAUNCHER", None)
            self.assertIs(type(desktop.for_this_app(self.home)), desktop.Desktop)


def env_for(home):
    return {"PATH": "/usr/bin:/bin", "HOME": home, "LUOKE_MAIL_HOME": os.path.join(home, "m"), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": ROOT, "LUOKE_HUB_SOCKET": "/nonexistent/hub.sock", "LUOKE_HUB_APPS": "/nonexistent"}


class Started:
    """The real entry, started the way the body starts it: pipes on stdin and stdout, in a data folder of its own."""

    def __init__(self, home, parent=None):
        self.env = env_for(home)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.p = subprocess.Popen([sys.executable, "-B", "-m", "luoke_mail", "--resident", "--parent-pid", str(parent or os.getpid()),
                                   "--port", str(self.port)], env=self.env, cwd="/", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8")

    def line(self, wait=30):
        ready, _, _ = select.select([self.p.stdout], [], [], wait)
        return json.loads(self.p.stdout.readline()) if ready else None

    def stop(self):
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait()
        _close(self.p.stdin)
        _close(self.p.stdout)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="lm")

    def test_ready_then_sigterm_ends_it_cleanly(self):
        m = Started(self.home)
        self.addCleanup(m.stop)
        self.assertEqual(m.line(), {"op": "ready"})
        sock = os.path.join(self.home, "m", "run", "agent.sock")
        self.assertTrue(os.path.exists(sock))
        self.assertEqual(desktop.read_server_info(os.path.join(self.home, "m"))["port"], m.port)
        m.p.send_signal(signal.SIGTERM)
        self.assertEqual(m.p.wait(15), 0)
        self.assertFalse(os.path.exists(sock))

    def test_nothing_it_prints_reaches_the_pipe(self):
        m = Started(self.home)
        self.addCleanup(m.stop)
        self.assertEqual(m.line(), {"op": "ready"})
        m.p.send_signal(signal.SIGTERM)
        m.p.wait(15)
        self.assertEqual(m.p.stdout.read(), "")
        with open(os.path.join(self.home, "m", "service.log"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn("started by 螺壳", log)
        self.assertNotIn(desktop.load_token(os.path.join(self.home, "m")), log)

    def test_text_that_isnt_valid_unicode_still_reaches_the_body(self):
        # a subject with bytes that wouldn't decode comes this far as lone surrogates
        code = ("import os\nfrom luoke_mail import desktop\nlink = desktop.attach_body(os.getppid())\n"
                "link.tell({'title': 'a\\udc80b'})\nlink.tell({'op': 'done'})\n")
        out = subprocess.run([sys.executable, "-B", "-c", code], env=env_for(self.home), cwd="/", input="", capture_output=True,
                             text=True, encoding="utf-8", timeout=30).stdout.splitlines()
        self.assertEqual([json.loads(x) for x in out], [{"title": "a?b"}, {"op": "done"}])

    def test_a_memory_file_too_new_is_told_once_and_not_started_again(self):
        # exit 0 is the one exit the body doesn't start again from; a dialog, there is no terminal
        said = []
        link = mock.Mock(tell=said.append)
        with mock.patch.object(server, "App", side_effect=memory.TooNew("newer")), \
                mock.patch.object(desktop, "alert", side_effect=lambda text: said.append(text)):
            self.assertEqual(server.serve(open_browser=False, service=True, body=link), 0)
        self.assertEqual(said, [{"op": "not-starting"}, memory.TOO_NEW_IN_LUOKE])      # the body knows before the dialog comes up
        self.assertIn("请更新螺壳。", memory.TOO_NEW_IN_LUOKE)
        lock = desktop.InstanceLock()
        self.assertTrue(lock.acquire())
        lock.release()

    def test_a_second_one_on_the_same_data_says_so_and_leaves(self):
        m = Started(self.home)
        self.addCleanup(m.stop)
        self.assertEqual(m.line(), {"op": "ready"})
        other = Started(self.home)
        self.addCleanup(other.stop)
        self.assertEqual(other.p.wait(30), 3)

    def test_it_ends_when_the_body_is_gone(self):
        # a parent that isn't this test: killed, the process has to notice by itself
        parent = subprocess.Popen([sys.executable, "-c", (
            "import os, subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-B', '-m', 'luoke_mail', '--resident', '--parent-pid', str(os.getpid()), '--port', sys.argv[1]],"
            " cwd='/', stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)\n"
            "print(p.pid, flush=True)\n"
            "p.stdout.readline()\n"
            "print('ready', flush=True)\n"
            "time.sleep(600)\n"), "0"], env=env_for(self.home), stdout=subprocess.PIPE, text=True)
        self.addCleanup(parent.stdout.close)
        self.addCleanup(parent.kill)
        child = int(parent.stdout.readline())
        self.addCleanup(lambda: self._kill(child))
        self.assertEqual(parent.stdout.readline().strip(), "ready")
        parent.kill()
        parent.wait()
        end = time.monotonic() + 20
        while time.monotonic() < end and self._alive(child):
            time.sleep(0.2)
        self.assertFalse(self._alive(child))

    @staticmethod
    def _alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    @staticmethod
    def _kill(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
