"""What the service writes to its log leaves this Mac: 螺壳's diagnostics carry service.log, and users send us that file.
So it holds no mail text, subject, sender, address or label (docs/DESIGN.md, 安全边界). Only made-up mail here."""
import ast
import contextlib
import io
import json
import os
import pty
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import actions, desktop, engines, hub, mailbox, server, subscriptions, triage  # noqa: E402
from test_actions import ME, Rig, mail  # noqa: E402

SUBJECT = "下周裁员名单"
ADDRESS = "boss@corp.example"
LABEL = "私人-体检报告"
WORDS = (SUBJECT, ADDRESS, LABEL)


class Said:
    """Everything a piece of code prints, on either stream: in the app stdout goes to the log too (attach_body)."""

    def __enter__(self):
        self.out = io.StringIO()
        self.streams = contextlib.ExitStack()
        self.streams.enter_context(contextlib.redirect_stderr(self.out))
        self.streams.enter_context(contextlib.redirect_stdout(self.out))
        return self

    def __exit__(self, *exc):
        self.streams.close()

    @property
    def text(self):
        return self.out.getvalue()


class LogTestCase(unittest.TestCase):
    def assertNoMail(self, text):
        for word in WORDS:
            self.assertNotIn(word, text)


class Notifications(LogTestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        launcher = os.path.join(self.home, "launcher")
        open(launcher, "w").close()
        env = mock.patch.dict(os.environ, {"LUOKE_LAUNCHER": launcher})
        env.start()
        self.addCleanup(env.stop)
        self.noted = os.path.join(self.home, "run", "notifications.json")

    def desktop(self, run):
        return desktop.Desktop(self.home, tempfile.mkdtemp(), tempfile.mkdtemp(), run, platform="darwin")

    def test_a_notification_is_noted_without_its_words(self):
        ran = mock.Mock(returncode=3, stderr=f"could not post {SUBJECT}".encode())
        with Said() as said:
            self.desktop(lambda args, **k: ran).notify("螺壳邮件助手", f"1 件事快到期 · {ADDRESS}", f"明天：{SUBJECT}")
        self.assertIn("notification (app, not allowed)", said.text)
        self.assertNoMail(said.text)
        with open(self.noted, encoding="utf-8") as f:
            self.assertNoMail(f.read())

    def test_a_timeout_does_not_bring_the_command_line_along(self):
        """subprocess.TimeoutExpired says the command it ran, and the launcher is given the mail text as arguments."""
        def run(args, **k):
            raise subprocess.TimeoutExpired(args, 20)
        d = self.desktop(run)
        with Said() as said:
            d.notify("螺壳邮件助手", ADDRESS, SUBJECT)
        self.assertIn("notification (failed, TimeoutExpired)", said.text)
        self.assertNoMail(said.text)
        self.assertEqual(d.recent_notifications()[-1]["result"], "TimeoutExpired")
        with open(self.noted, encoding="utf-8") as f:
            self.assertNoMail(f.read())

    def test_what_an_older_version_noted_goes_at_the_next_notification(self):
        os.makedirs(os.path.dirname(self.noted))
        with open(self.noted, "w", encoding="utf-8") as f:
            json.dump([{"at": "2026-11-01 09:00:00", "title": "t", "subtitle": SUBJECT, "how": "app", "result": "posted"}], f)
        d = self.desktop(lambda args, **k: mock.Mock(returncode=0))
        with Said():
            d.notify("t", "s", "b")
        self.assertEqual(d.recent_notifications()[0], {"at": "2026-11-01 09:00:00", "how": "app", "result": "posted"})
        self.assertEqual(len(d.recent_notifications()), 2)


class OldLogs(LogTestCase):
    """Lines from before 0.19.34 hold mail text and would stay until the log is cut."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.log = os.path.join(self.home, "service.log")
        self.noted = os.path.join(self.home, "run", "notifications.json")
        self.mark = os.path.join(self.home, "run", "old-logs-cleared")
        os.makedirs(os.path.dirname(self.noted))
        for path in (self.log, self.log + ".1"):
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"hub refused: {SUBJECT}\n")
        with open(self.noted, "w", encoding="utf-8") as f:
            json.dump([{"at": "2026-11-01 09:00:00", "title": "t", "subtitle": SUBJECT, "how": "app", "result": "posted"}], f)

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_the_first_start_clears_them(self):
        desktop.clear_old_logs(self.home)
        self.assertEqual(self.read(self.log), "")
        self.assertFalse(os.path.exists(self.log + ".1"))
        self.assertEqual(json.loads(self.read(self.noted)), [{"at": "2026-11-01 09:00:00", "how": "app", "result": "posted"}])

    def test_only_once(self):
        desktop.clear_old_logs(self.home)
        with open(self.log, "a", encoding="utf-8") as f:
            f.write("kept\n")
        desktop.clear_old_logs(self.home)
        self.assertEqual(self.read(self.log), "kept\n")

    def test_the_file_is_emptied_in_place_because_launchd_holds_it_open(self):
        with open(self.log, "a", encoding="utf-8") as held:
            desktop.clear_old_logs(self.home)
            held.write("after\n")
        self.assertEqual(self.read(self.log), "after\n")

    def test_a_new_user_gets_no_empty_files_only_the_mark(self):
        home = tempfile.mkdtemp()
        desktop.clear_old_logs(home)
        self.assertEqual(os.listdir(home), ["run"])
        self.assertEqual(sorted(os.listdir(os.path.join(home, "run"))), ["old-app-log-cleared", "old-logs-cleared"])

    def test_the_app_log_too_even_after_the_others_were_marked(self):
        with open(self.mark, "w") as f:                         # 0.19.35 cleared the others, not this one
            f.write("2026-11-06")
        app = os.path.join(self.home, "app.log")
        for path in (app, app + ".1"):
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"Traceback: {SUBJECT}\n")
        desktop.clear_old_logs(self.home)
        self.assertEqual(self.read(app), "")
        self.assertFalse(os.path.exists(app + ".1"))
        self.assertEqual(self.read(self.log), f"hub refused: {SUBJECT}\n")      # marked already: left as it is
        with open(app, "a", encoding="utf-8") as f:
            f.write("kept\n")
        desktop.clear_old_logs(self.home)
        self.assertEqual(self.read(app), "kept\n")                              # once

    def test_a_noted_file_that_cannot_be_read_goes_with_its_words(self):
        with open(self.noted, "w", encoding="utf-8") as f:
            f.write(f"[{SUBJECT}")
        desktop.clear_old_logs(self.home)
        self.assertEqual(self.read(self.noted), "[]")

    def test_a_noted_file_of_the_wrong_shape_does_not_stop_the_start(self):
        with open(self.noted, "w", encoding="utf-8") as f:
            json.dump([SUBJECT], f)
        desktop.clear_old_logs(self.home)
        self.assertEqual(self.read(self.noted), "[]")

    def test_it_tries_again_after_a_failure(self):
        with mock.patch.object(os, "truncate", side_effect=PermissionError(SUBJECT)), Said() as said:
            desktop.clear_old_logs(self.home)
        self.assertIn("old logs not cleared: PermissionError", said.text)
        self.assertNoMail(said.text)
        self.assertFalse(os.path.exists(self.mark))
        desktop.clear_old_logs(self.home)
        self.assertEqual(self.read(self.log), "")

    def test_every_way_of_starting_that_writes_the_log_clears_it_first(self):
        script = textwrap.dedent("""
            import sys
            from luoke_mail import __main__ as entry, desktop, server
            server.serve = lambda **k: 0
            desktop.attach_body = lambda pid: None
            sys.argv = ["luoke-mail"] + sys.argv[1:]
            entry.main()
        """)
        for args in (["--service"], ["--app", "--resident"], ["--resident", "--parent-pid", "1"]):
            with self.subTest(args):
                self.setUp()
                out = subprocess.run([sys.executable, "-c", script, *args], cwd=ROOT, capture_output=True, text=True, timeout=60,
                                     env={**os.environ, "PYTHONPATH": ROOT, "LUOKE_MAIL_HOME": self.home})
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertNoMail(self.read(self.log))
                self.assertFalse(os.path.exists(self.log + ".1"))
                self.assertNoMail(self.read(self.noted))
                self.assertTrue(os.path.exists(self.mark))


class Mailboxes(LogTestCase):
    def test_a_refused_one_click_unsubscribe_does_not_name_the_sender(self):
        acts = actions.Actions(mock.Mock(), ME, "gmail", None)
        sub = {"key": f"from:{ADDRESS}", "name": SUBJECT, "from_addr": ADDRESS, "method": "one_click",
               "unsub_http": "https://corp.example/u/1"}
        with mock.patch.object(subscriptions, "one_click", lambda url: (False, "对方服务器返回 HTTP 405")), Said() as said:
            acts.unsubscribe(sub)
        self.assertIn("HTTP 405", said.text)
        self.assertNoMail(said.text)

    def test_a_label_that_could_not_be_restored_is_not_named(self):
        box = mock.Mock()
        box.copy.side_effect = mailbox.MailError(f"NO [TRYCREATE] {LABEL}")
        with Said() as said:
            actions.Actions._relabel(box, "[Gmail]/Trash", [("1", {"labels": [LABEL]})])
        self.assertIn("could not restore a label: MailError", said.text)
        self.assertNoMail(said.text)

    def test_what_an_imap_server_said_stays_out(self):
        for ex in (OSError(f"no route for {ADDRESS}"), Exception(f"AUTHENTICATIONFAILED for {ADDRESS}"),
                   Exception(f"BAD could not select {LABEL}")):
            with Said() as said:
                mailbox.friendly_error(ex, "custom")
            self.assertIn(type(ex).__name__, said.text)
            self.assertNoMail(said.text)
        with Said() as said:
            mailbox._refused("移动", [f"NO no such folder {LABEL}".encode()])
        self.assertIn("IMAP refused 移动", said.text)
        self.assertNoMail(said.text)

    def test_a_message_that_could_not_be_read_is_skipped_without_its_text(self):
        rig = Rig("gmail", "gmail", [(mail(1, "one"), False)])
        self.addCleanup(rig.close)
        with mock.patch.object(mailbox, "parse_message", side_effect=ValueError(f"bad header: {SUBJECT}")), Said() as said:
            with rig.open() as box:
                box.fetch_recent(limit=5, unseen_only=False, days=None)
        self.assertIn("could not be read", said.text)
        self.assertIn("ValueError", said.text)
        self.assertNoMail(said.text)


class Models(LogTestCase):
    """A model server's words can quote the prompt, and the prompt is the mail."""

    def ask(self):
        return engines._chat_json("http://127.0.0.1:1/v1", "m", [], {}, None, 10, 5, False)

    def test_an_http_error_is_its_code(self):
        def refused(*a, **k):
            raise urllib.error.HTTPError("http://x", 422, "no", {}, io.BytesIO(f'{{"input": "{SUBJECT}"}}'.encode()))
        with mock.patch.object(engines, "_request", refused), Said() as said, self.assertRaises(engines.EngineError):
            self.ask()
        self.assertIn("model server HTTP 422", said.text)
        self.assertNoMail(said.text)

    def test_an_answer_without_a_completion_is_not_copied(self):
        with mock.patch.object(engines, "_request", lambda *a, **k: {"error": SUBJECT}), Said() as said, \
                self.assertRaises(engines.EngineError):
            self.ask()
        self.assertIn("without a completion", said.text)
        self.assertNoMail(said.text)

    def test_the_memory_guard_still_reads_the_words_it_no_longer_logs(self):
        def full(*a, **k):
            body = b'{"error": {"message": "does not fit under the dynamic memory ceiling (66.61GB)."}}'
            raise urllib.error.HTTPError("http://x", 507, "no", {}, io.BytesIO(body))
        with mock.patch.object(engines, "_request", full), Said(), self.assertRaises(engines.MemoryFull):
            self.ask()

    def test_a_hub_refusal_is_its_code_and_the_page_keeps_the_words(self):
        with Said() as said:
            ex = hub._refusal("internal", {"message": SUBJECT})
        self.assertIn("hub refused: internal", said.text)
        self.assertNoMail(said.text)
        self.assertEqual(ex.detail, SUBJECT)

    def test_unusable_model_output_is_not_quoted(self):
        link = mock.MagicMock()
        link.limits.return_value = {}
        link.fit.return_value = 1000
        link.chat.return_value = (json.dumps({"action": SUBJECT}, ensure_ascii=False), {}, 0.1)
        with Said() as said, self.assertRaises(engines.EngineError):
            triage.analyze({"subject": "s", "body": "b", "from_addr": "a@x.example"}, link)
        self.assertIn("unusable model output twice", said.text)
        self.assertNoMail(said.text)


class Crashes(LogTestCase):
    def test_a_crash_is_its_type_and_where(self):
        try:
            raise KeyError(SUBJECT)
        except KeyError as ex:
            with Said() as said:
                desktop.log_crash(ex, "a page request")
        self.assertIn("a page request: KeyError at test_log_privacy.py:", said.text)
        self.assertIn("test_a_crash_is_its_type_and_where", said.text)
        self.assertNoMail(said.text)

    def test_no_exception_to_give_is_not_called_nonetype(self):
        with Said() as said:
            desktop.log_crash(None, "ignored")
        self.assertIn("[luoke-mail] ignored", said.text)
        self.assertNotIn("NoneType", said.text)

    def test_the_stream_going_away_does_not_count_as_a_terminal(self):
        with mock.patch.object(sys, "stderr", None):
            self.assertFalse(desktop._on_a_terminal())
        closed = io.StringIO()
        closed.close()
        with mock.patch.object(sys, "stderr", closed):
            self.assertFalse(desktop._on_a_terminal())

    def test_on_a_terminal_the_whole_traceback(self):
        """Nothing is kept there. The same crash in a log file is test_the_real_log_file."""
        script = textwrap.dedent(f"""
            from luoke_mail import desktop
            desktop.crashes_without_text()
            raise ValueError({SUBJECT!r})
        """)
        mine, theirs = pty.openpty()
        p = subprocess.Popen([sys.executable, "-c", script], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=theirs,
                             env={**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"})
        os.close(theirs)
        said = b""
        try:
            while True:
                part = os.read(mine, 4096)
                if not part:
                    break
                said += part
        except OSError:          # the other end closed
            pass
        os.close(mine)
        self.assertEqual(p.wait(timeout=60), 1)
        text = said.decode("utf-8")
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn(f"ValueError: {SUBJECT}", text)

    def test_a_pipe_is_not_a_terminal(self):
        """`2>&1 | tee file` keeps it."""
        script = textwrap.dedent(f"""
            from luoke_mail import desktop
            desktop.crashes_without_text()
            raise ValueError({SUBJECT!r})
        """)
        out = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=60,
                             env={**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"})
        self.assertIn("crashed: ValueError", out.stderr)
        self.assertNoMail(out.stderr)

    def test_the_page_server_reports_a_broken_request_the_same_way(self):
        page = server.PageServer.__new__(server.PageServer)
        try:
            raise RuntimeError(SUBJECT)
        except RuntimeError:
            with Said() as said:
                page.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("RuntimeError", said.text)
        self.assertNoMail(said.text)

    def test_the_real_log_file(self):
        """A process logging the way the service does: what Python itself prints when things crash goes the same way."""
        log = os.path.join(tempfile.mkdtemp(), "service.log")
        script = textwrap.dedent(f"""
            import sys, threading
            from luoke_mail import desktop
            desktop.log_to_file({log!r})
            print("started", file=sys.stderr)

            def boom():
                raise ValueError({SUBJECT!r})

            def leave():
                sys.exit(3)
            for target in (boom, leave):
                t = threading.Thread(target=target)
                t.start()
                t.join()
            raise RuntimeError({ADDRESS!r})
        """)
        out = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=60,
                             env={**os.environ, "PYTHONPATH": ROOT})
        self.assertEqual(out.returncode, 1, out.stderr)
        with open(log, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("started", text)
        self.assertIn("a thread crashed: ValueError", text)
        self.assertIn("crashed: RuntimeError", text)
        self.assertNotIn("SystemExit", text)
        self.assertNoMail(text)

    def test_an_idle_job_that_crashes_keeps_the_text_out_of_the_hubs_log(self):
        script = textwrap.dedent(f"""
            import sys
            from luoke_mail import __main__ as entry, server

            def boom(name=None):
                raise ValueError({SUBJECT!r})
            server.run_idle_job = boom
            sys.argv = ["luoke-mail", "--job"]
            entry.main()
        """)
        out = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=60,
                             env={**os.environ, "PYTHONPATH": ROOT})
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("crashed: ValueError", out.stderr)
        self.assertNoMail(out.stderr)


# What a line may carry, by what is written between the braces. Anything else is a new decision, made here:
# an exception's text, a server's reply, a name from the mail must not come in with it.
FACTS = {"op", "what", "run.stuck", "run.count()", "code", "uid", "i", "total", "how", "result", "on", "pid", "steps", "note_id", "p.returncode", "self.host",
         "memory.TOO_NEW", "__version__", "port", "port + 19", "app.port", "app.desktop.launcher.name", "e.code",
         "name or 'all'", "why", "forget_secrets()", "time.strftime('%Y-%m-%d %H:%M:%S')", "self.tool_server.name", "self.name"}
KNOWN = {
    ("__main__.py", "take_notification()"): "not a log line: the launcher reads it on stdout, to post the notification",
    ("server.py", "tail"): "service.log read back, into the stderr of the process that is starting it",
    ("actions.py", "detail"): "subscriptions.one_click: a fixed sentence with an HTTP code or an exception's type",
    ("desktop.py", "text"): "alert: fixed sentences only (server.py)",
    ("desktop.py", "' '.join(args)"): "launchctl's arguments, our own label and files",
    ("desktop.py", "args[0]"): "the path of a system tool",
    ("desktop.py", "r.returncode"): "launchctl's exit code",
    ("desktop.py", "r.stderr.strip()"): "launchctl and osacompile about our own files; no mail goes through them",
    ("desktop.py", "where"): "file names, lines and function names (log_crash)",
    ("agent_mcp.py", "self._which(method, params)"): "a copy from luoke-agent; says which tool, not its arguments",
    ("agent_mcp.py", "_where(e)"): "a copy from luoke-agent; file names, lines and function names",
    ("server.py", "url"): "terminal only, never the service: where the page is",
    ("server.py", "'，已在浏览器中打开' if open_browser else ''"): "terminal only",
}


def words(node):
    """What an argument puts in a line: what a literal interpolates, and anything that is no literal at all."""
    if isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                yield ast.unparse(part.value)
    elif isinstance(node, ast.BinOp):            # "..." + x, "%s" % x
        yield from words(node.left)
        yield from words(node.right)
    elif isinstance(node, ast.Tuple):            # "%s %s" % (x, y)
        for part in node.elts:
            yield from words(part)
    elif isinstance(node, ast.IfExp):            # x if ok else y
        yield from words(node.body)
        yield from words(node.orelse)
    elif not isinstance(node, ast.Constant):
        yield ast.unparse(node)


def writes(call):
    """A print, or a write straight to stderr or stdout (in the app stdout goes to the log too)."""
    f = call.func
    return (isinstance(f, ast.Name) and f.id == "print") or (
        isinstance(f, ast.Attribute) and f.attr == "write" and ast.unparse(f.value) in ("sys.stderr", "sys.stdout"))


def said_in(tree):
    """Each thing a module's log lines take from outside the line itself."""
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and writes(call):
            for arg in call.args:
                for value in words(arg):
                    yield call.lineno, value


class TheCheck(unittest.TestCase):
    """The list below only works if it sees the line."""

    def seen(self, code):
        return [value for _, value in said_in(ast.parse(code))]

    def test_each_way_of_writing_a_line(self):
        for code in ('print(f"failed: {ex}", file=sys.stderr)', "print(ex, file=sys.stderr)", 'print("failed:", ex)',
                     'print("failed: %s" % ex)', 'print("%s %s" % ("failed", ex))', 'print("failed: " + ex)',
                     'print(f"failed: {ex}" if ex else "")', 'sys.stderr.write(f"{ex}")', "sys.stderr.write(ex)", 'sys.stdout.write("failed: %s" % ex)'):
            with self.subTest(code):
                self.assertEqual(self.seen(code), ["ex"])

    def test_what_it_cannot_take_apart_counts_whole(self):
        self.assertEqual(self.seen('print("failed: {}".format(ex))'), ["'failed: {}'.format(ex)"])
        self.assertEqual(self.seen("print(str(ex))"), ["str(ex)"])

    def test_a_line_of_fixed_words_has_nothing_to_check(self):
        self.assertEqual(self.seen('print("started", file=sys.stderr)'), [])
        self.assertEqual(self.seen('sys.stderr.write("started")'), [])

    def test_another_files_write_is_not_a_log_line(self):
        self.assertEqual(self.seen("f.write(text)"), [])


class EveryLine(unittest.TestCase):
    def modules(self):
        folder = os.path.join(ROOT, "luoke_mail")
        for name in sorted(os.listdir(folder)):
            if name.endswith(".py"):
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    yield name, ast.parse(f.read())

    def test_a_log_line_carries_only_what_is_on_the_list(self):
        odd = []
        for name, tree in self.modules():
            for line, value in said_in(tree):
                fine = (value in FACTS or (name, value) in KNOWN or value.endswith(".__name__")
                        or value.startswith(("len(", "sorted(stuck")) or value == "len(rows) - len(ok)")
                if not fine:
                    odd.append(f"{name}:{line}: {value}")
        self.assertEqual(odd, [])

    def test_no_traceback_with_the_exceptions_text(self):
        """But one: log_crash's, on a terminal."""
        odd = [name for name, tree in self.modules() for node in ast.walk(tree)
               if isinstance(node, ast.Attribute) and node.attr in ("print_exc", "format_exc", "print_exception", "format_exception")]
        self.assertEqual(odd, ["desktop.py"])


if __name__ == "__main__":
    unittest.main()
