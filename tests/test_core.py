import imaplib
import json
import os
import ssl
import sys
import tempfile
import time
import unittest
from unittest import mock
from email.message import EmailMessage
from email.utils import formatdate

_tmp = tempfile.mkdtemp(prefix="luoke-test-")
os.environ["LUOKE_MAIL_HOME"] = _tmp          # must be set before importing luoke_mail.config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import isolate  # noqa: F401,E402 -- first: never the user's real data (tests/isolate.py)
from luoke_mail import engines, mailbox, providers, samples, triage  # noqa: E402
from luoke_mail.credstore import FileStore  # noqa: E402
from fakes import GOOD, FakeChat, FakeIMAP  # noqa: E402


def make_mail(subject="测试", body="正文", html=None, charset="utf-8", attach=False, sender="李婷 <liting@corp.example>",
              days_ago=0):
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "me@example.com"
    m["Subject"] = subject
    # relative to today: syncs read "the last N days", so a fixed date would age out of the tests
    m["Date"] = formatdate(time.time() - days_ago * 86400 - 3600, localtime=True)
    m["Message-ID"] = "<abc123@corp.example>"
    if html is not None and body is None:
        m.set_content(html, subtype="html", charset=charset)
    else:
        m.set_content(body, charset=charset)
        if html is not None:
            m.add_alternative(html, subtype="html")
    if attach:
        m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="评审材料.pdf")
    return m.as_bytes()


class ParseTests(unittest.TestCase):
    def test_the_mailboxs_own_verdict_on_the_sender(self):
        """Gmail adds Authentication-Results on receipt, on top; a sender can write one into the message, below it."""
        def raw(*results, sender="American Express <accounts@welcome.bank.example>"):
            head = "".join(f"Authentication-Results: {r}\r\n" for r in results)
            return head.encode() + make_mail("Your decision is ready", "View our decision online.", sender=sender)
        gm = "mx.google.com"
        ok = "mx.google.com; dkim=pass header.i=@welcome.bank.example; spf=pass; dmarc=pass (p=REJECT) header.from=welcome.bank.example"
        self.assertEqual(mailbox.parse_message(raw(ok), gm)["auth"], "pass")
        bad = "mx.google.com; dkim=none; spf=softfail; dmarc=fail (p=REJECT) header.from=welcome.bank.example"
        self.assertEqual(mailbox.parse_message(raw(bad, ok), gm)["auth"], "fail")      # the forged one below is not read
        self.assertIsNone(mailbox.parse_message(raw("evil.example; dmarc=pass header.from=welcome.bank.example"), gm)["auth"])
        self.assertIsNone(mailbox.parse_message(raw(ok), None)["auth"])               # a provider not relied on
        self.assertIsNone(mailbox.parse_message(raw(ok, sender="x <a@other.example>"), gm)["auth"])   # not this From
        self.assertIsNone(mailbox.parse_message(raw(ok.replace("dmarc=pass", "dmarc=bestguesspass")), gm)["auth"])
        line = lambda auth: [x for x in triage.build_messages({"from_addr": "a@welcome.bank.example", "auth": auth})[1]  # noqa: E731
                             ["content"].splitlines() if x.startswith("发件域名验证")]
        self.assertIn("确实来自 welcome.bank.example", line("pass")[0])
        self.assertIn("没有通过", line("fail")[0])
        self.assertEqual(line(None), [])
        # without a verdict the prompt is what it was: the meaning travels with the verdict, not in the system prompt
        # (0.13.0 had it there, and the 9B base took any foreign sender domain for a sign of phishing)
        self.assertNotIn("发件域名", triage.SYSTEM_PROMPT)
        self.assertIn("官方域名", line("pass")[0])

    def test_plain_with_attachment(self):
        r = mailbox.parse_message(make_mail("会议邀请", "周三下午三点开会", attach=True))
        self.assertEqual(r["subject"], "会议邀请")
        self.assertEqual(r["from_name"], "李婷")
        self.assertEqual(r["from_addr"], "liting@corp.example")
        self.assertIn("周三下午三点", r["body"])
        self.assertEqual(r["attachments"], ["评审材料.pdf"])
        self.assertEqual(r["message_id"], "<abc123@corp.example>")

    def test_an_unclosed_head_does_not_empty_the_body(self):
        """luoke-mail#28: a <head> never closed skipped everything after it -- the model got 「（正文为空）」."""
        text = mailbox.html_to_text("<html><head><title>促销</title><style>p{color:red}</style>"
                                    "<body><p>请在 10 月 30 日前点击确认。</p></body></html>")
        self.assertIn("请在 10 月 30 日前点击确认。", text)
        self.assertNotIn("color", text)

    def test_preheader_padding_is_cleared(self):
        """luoke-mail#28: &#8199; and &shy; in the usual padding were left -- 600 invisible characters up front."""
        pad = "\u034f\u200c\u00a0\u2007\ufeff\u00ad" * 300
        self.assertEqual(mailbox.clean_body(pad + "您的订单已发货"), "您的订单已发货")
        self.assertEqual(mailbox.clean_body("co\u00adop 1\u20071"), "coop 1 1")

    def test_long_tracking_links_give_way_to_the_text(self):
        """luoke-mail#28: buttons written as 「text <a tracking link of hundreds of characters>」 filled the 6,000
        characters and the deadline after them was cut off. A long link is told by its host; short ones stay."""
        track = "https://click.mail.shop.example/ls/click?upn=" + "x7Qa" * 60
        body = "".join(f"商品 {i} ¥99 <{track}{i}>\n" for i in range(40)) + "请在 10 月 30 日前上传退货凭证。"
        out = mailbox.clean_body(body)
        self.assertIn("请在 10 月 30 日前上传退货凭证。", out)
        self.assertIn("https://click.mail.shop.example/…", out)          # still a link to the eye, not a mark
        self.assertNotIn("〔链接", out)
        self.assertNotIn("upn=", out)
        self.assertIn("http://icbc-verify.example/login", mailbox.clean_body("请登录 http://icbc-verify.example/login 验证"))

    def test_the_part_that_has_the_message_is_read(self):
        """luoke-mail#28: the plain-text part was used whenever there was one -- also when it was HTML source, or a
        line saying to read the HTML version."""
        html = "<html><body><p>您的订单 4471 已发货，预计周四送达。</p>" + "<p>商品清单</p>" * 30 + "</body></html>"
        self.assertIn("预计周四送达", mailbox.pick_body("您的客户端不支持 HTML，请查看网页版。", html))
        self.assertIn("预计周四送达", mailbox.pick_body(html, None))                  # HTML source in the plain part
        self.assertNotIn("<p>", mailbox.pick_body(html, None))
        same = "<html><body><p>周五开会，带上电脑。</p><p>-- 王磊</p></body></html>"           # its own HTML version
        self.assertEqual(mailbox.pick_body("周五开会，带上电脑。\n-- 王磊", same), "周五开会，带上电脑。\n-- 王磊")

    def test_html_only_gb18030(self):
        html = "<html><head><style>p{color:red}</style></head><body><p>您的账单</p><p>应还 3,280 元</p><script>x()</script></body></html>"
        r = mailbox.parse_message(make_mail("账单", None, html=html, charset="gb18030"))
        self.assertIn("您的账单", r["body"])
        self.assertIn("应还 3,280 元", r["body"])
        self.assertNotIn("color:red", r["body"])
        self.assertNotIn("x()", r["body"])

    def test_quoted_history_removed(self):
        body = "已经解决了，不用管了。\n\n在 2026年10月11日 21:03，王磊 写道：\n> 测试环境一直报 403\n> 急"
        r = mailbox.parse_message(make_mail("Re: 测试环境", body))
        self.assertIn("已经解决了", r["body"])
        self.assertNotIn("403", r["body"])
        body_en = "Sounds good, see you then.\n\nOn Mon, Oct 12, 2026 at 9:00 AM Daniel <d@x.example> wrote:\n> Can we meet?"
        self.assertNotIn("Can we meet", mailbox.clean_body(body_en))

    def test_body_truncated(self):
        self.assertLess(len(mailbox.clean_body("字" * 20000)), mailbox.BODY_CHAR_LIMIT + 50)


class JunkRuleTests(unittest.TestCase):
    """A rule 「以后都移到垃圾邮件」 is the user not wanting the mail, not a scam (prompt review, 10-22): the
    model is asked for ignore only, nothing makes it suspicious, and the page carries it out as 移到垃圾邮件."""

    def test_junk_rule_asks_for_ignore_not_suspicious(self):
        from luoke_mail import pipeline
        rule = {"id": 3, "kind": "list", "value": "promo.shop.example", "action": "spam"}
        text = triage.rule_text(rule)
        self.assertIn("action 设为 ignore", text)
        self.assertNotIn("suspicious", text)
        self.assertTrue(pipeline.follows_rule({"action": "ignore", "suspicious": False}, rule))
        s = pipeline.rule_suggestion({"category": "promotion", "subject": "促销"}, rule)
        self.assertEqual((s["action"], s["category"], s["suspicious"]), ("ignore", "promotion", False))


class DraftTests(unittest.TestCase):
    class Link:
        def __init__(self, answers):
            self.answers, self.seen = list(answers), []

        def fit(self, task, build, n):
            return n

        def chat(self, task, messages, schema, max_tokens=None):
            self.seen.append(messages)
            return self.answers.pop(0), None, None

        def again(self, task, messages, content, note):
            return messages + [{"role": "assistant", "content": content}, {"role": "user", "content": note}]

    def test_no_signature_is_not_a_name_and_a_bad_answer_is_asked_again(self):
        import datetime as dt
        link = self.Link(["不是 JSON", '{"body": "好的，周五前发你。"}'])
        body = triage.draft_reply({"subject": "Q3", "body": "能发我数据吗？"}, ["周五前发"], link, today=dt.date(2026, 10, 22))
        self.assertEqual(body, "好的，周五前发你。")
        system = link.seen[0][0]["content"]
        self.assertIn("今天是 2026-10-22", system)
        self.assertIn("结尾不要署名", system)
        self.assertNotIn("（不署名）", system)
        self.assertEqual(len(link.seen), 2)                       # asked once more
        with self.assertRaises(engines.EngineError):
            triage.draft_reply({"subject": "Q3", "body": "x"}, [], self.Link(["坏", "也坏"]))
        named = self.Link(['{"body": "好"}'])
        triage.draft_reply({"subject": "Q3", "body": "x"}, [], named, signature="小周")
        self.assertIn("结尾署名「小周」", named.seen[0][0]["content"])


class UnsubscribeFinderTests(unittest.TestCase):
    def mk(self, html=None, text=None):
        m = EmailMessage()
        m["Subject"], m["From"] = "x", "shop@x.example"
        if text:
            m.set_content(text)
        if html:
            (m.add_alternative if text else m.set_content)(html, subtype="html")
        return m.as_bytes()

    def test_cases(self):
        f = mailbox.find_unsubscribe
        self.assertEqual(f(self.mk(html='<a href="https://s.example/u?i=1">Unsubscribe</a><a href="https://s.example">Home</a>')),
                         {"kind": "link", "url": "https://s.example/u?i=1"})
        self.assertEqual(f(self.mk(html='<a href="https://x.example/prefs">点此退订</a>'))["url"], "https://x.example/prefs")
        self.assertEqual(f(self.mk(text="如需退订请访问 https://m.example/td?x=1 。"))["url"], "https://m.example/td?x=1")
        self.assertEqual(f(self.mk(text="限时优惠。退订请回复 TD。")), {"kind": "reply", "keyword": "TD"})
        self.assertIsNone(f(self.mk(text="欢迎访问 https://x.example 看看新品")))


class ImapTests(unittest.TestCase):
    def msgs(self):
        return [(make_mail("旧邮件", "已读的"), True), (make_mail("新邮件一", "未读 A"), False), (make_mail("新邮件二", "未读 B"), False)]

    def test_readonly_fetch_unseen_newest_first(self):
        with FakeIMAP(self.msgs()) as srv:
            with mailbox.Mailbox("custom", "me@example.com", "secret", host="127.0.0.1", port=srv.port, ssl=False, timeout=5) as mb:
                self.assertEqual(mb.stats(), {"total": 3, "unseen": 2})
                got = mb.fetch_recent(limit=10, unseen_only=True, days=None)
        self.assertEqual([m["subject"] for m in got], ["新邮件二", "新邮件一"])
        self.assertIn("EXAMINE", srv.commands)          # read-only open
        self.assertNotIn("SELECT", srv.commands)
        self.assertNotIn("ID", srv.commands)            # never sent to non-NetEase servers (QQ hangs on it)
        self.assertEqual(srv.violations, [])

    def test_header_scan_reports_sizes(self):
        with FakeIMAP(self.msgs()) as srv:
            with mailbox.Mailbox("custom", "me@example.com", "secret", host="127.0.0.1", port=srv.port, ssl=False, timeout=5) as mb:
                hs = mb.fetch_headers(days=3650)
        self.assertEqual(len(hs), 3)
        self.assertTrue(all(h["size"] > 100 for h in hs))

    def test_netease_requires_id(self):
        with FakeIMAP(self.msgs(), flavour="netease") as srv:
            with mailbox.Mailbox("163", "me@example.com", "secret", host="127.0.0.1", port=srv.port, ssl=False, timeout=5) as mb:
                self.assertEqual(mb.stats()["total"], 3)
            self.assertIn("ID", srv.commands)

    def test_unsafe_login_message(self):
        with FakeIMAP(self.msgs(), flavour="netease") as srv:
            with self.assertRaises(mailbox.MailError) as ctx:
                mailbox.Mailbox("custom", "me@example.com", "secret", host="127.0.0.1", port=srv.port, ssl=False, timeout=5).open()
        self.assertIn("网易邮箱拒绝登录", str(ctx.exception))

    def test_wrong_password_message(self):
        with FakeIMAP(self.msgs()) as srv:
            with self.assertRaises(mailbox.MailError) as ctx:
                mailbox.Mailbox("qq", "me@example.com", "nope", host="127.0.0.1", port=srv.port, ssl=False, timeout=5).open()
        self.assertIn("授权码", str(ctx.exception))

    def test_gmail_app_password_spaces_stripped(self):
        mb = mailbox.Mailbox("gmail", "me@gmail.com", "abcd efgh ijkl mnop")
        self.assertEqual(mb.secret, "abcdefghijklmnop")
        self.assertEqual(mailbox.Mailbox("qq", "me@qq.com", "abcd\u3000efgh\u00a0ijkl mnop\n").secret, "abcdefghijklmnop")
        self.assertEqual(mailbox.Mailbox("custom", "me@example.com", "my pass", host="h").secret, "my pass")

    def test_each_provider_says_what_to_check_when_login_is_refused(self):
        for provider, says in (("gmail", "应用专用密码"), ("qq", "不是 QQ 密码"), ("163", "开启 IMAP/SMTP 服务"),
                               ("custom", "单独生成的密码")):
            with FakeIMAP(self.msgs()) as srv:
                with self.assertRaises(mailbox.MailError) as ctx:
                    mailbox.Mailbox(provider, "me@example.com", "nope", host="127.0.0.1", port=srv.port, ssl=False,
                                    timeout=5).open()
            self.assertIn(says, str(ctx.exception), provider)
        gmail = mailbox.friendly_error(imaplib.IMAP4.error(
            "[ALERT] Application-specific password required: https://support.google.com/accounts/answer/185833 (Failure)"), "gmail")
        self.assertIn("不是 Google 账号的登录密码", str(gmail))
        web = mailbox.friendly_error(imaplib.IMAP4.error("[WEBALERT https://accounts.google.com/signin/continue?x] Web login required."), "gmail")
        self.assertIn("确认是你本人", str(web))
        for p in providers.PROVIDERS.values():
            self.assertTrue(p["steps"] and p["trouble"] and p["refused"])


class TlsCompatTests(unittest.TestCase):
    """luoke-mail#40: imap.139.com offers RSA key exchange only; Python's default ciphers have none of it."""

    HANDSHAKE = "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] sslv3 alert handshake failure (_ssl.c:1000)"

    def setUp(self):
        mailbox._compat_hosts.clear()
        self.addCleanup(mailbox._compat_hosts.clear)

    def connect(self, outcomes, host="imap.139.example"):
        seen = []

        def fake(host, port, timeout=None, ssl_context=None):
            seen.append(ssl_context)
            out = outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        with mock.patch.object(imaplib, "IMAP4_SSL", fake):
            conn = mailbox.Mailbox("custom", "me@example.com", "pw", host=host, port=993)._connect()
        return conn, seen

    def test_a_failed_handshake_is_tried_once_more_with_rsa_and_remembered(self):
        if mailbox.compat_context() is None:
            self.skipTest("this TLS library takes no cipher list with a security level")
        conn, seen = self.connect([ssl.SSLError(1, self.HANDSHAKE), "conn"])
        self.assertEqual(conn, "conn")
        self.assertIsNone(seen[0])                                  # the default first
        ctx = seen[1]
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)        # certificates checked as before
        self.assertTrue(ctx.check_hostname)
        names = [c["name"] for c in ctx.get_ciphers()]
        self.assertIn("AES256-GCM-SHA384", names)                   # what 139 settles on
        self.assertFalse(any("SHA" == n.rsplit("-", 1)[-1] for n in names))   # no SHA-1 suites came with it
        _, seen = self.connect(["again"])                           # the same host goes straight to it
        self.assertIsNotNone(seen[0])
        _, seen = self.connect(["other"], host="imap.other.example")
        self.assertIsNone(seen[0])                                  # other hosts keep the default

    def test_other_tls_errors_are_not_retried(self):
        for err in (ssl.SSLCertVerificationError(1, "certificate verify failed"), ssl.SSLError(1, "wrong version number")):
            with self.assertRaises(ssl.SSLError):
                self.connect([err, "never"])
        self.assertFalse(mailbox._compat_hosts)

    def test_a_handshake_that_still_fails_says_so(self):
        msg = str(mailbox.friendly_error(ssl.SSLError(1, self.HANDSHAKE), "custom"))
        self.assertIn("加密连接", msg)
        self.assertNotIn("服务器地址。", msg.split("：")[0])


class RouteTests(unittest.TestCase):
    def test_no_route_is_named_twice(self):
        """A dict literal keeps the last of two equal keys: 0.10 to 0.13 had /api/profile twice, and the settings'
        sync range and signature were never saved."""
        import ast
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "luoke_mail", "server.py")
        for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if isinstance(node, ast.Dict):
                keys = [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                self.assertEqual(len(keys), len(set(keys)), [k for k in keys if keys.count(k) > 1])


class IsolationTests(unittest.TestCase):
    """Tests never reach the user's real data: every module imports tests/isolate.py before luoke_mail."""

    def test_every_test_module_isolates_itself_first_and_the_data_folder_is_not_the_owners(self):
        import glob
        import re
        from luoke_mail import config
        self.assertNotEqual(os.path.realpath(config.CONFIG_DIR), os.path.realpath(config.DEFAULT_DIR))
        here = os.path.dirname(os.path.abspath(__file__))
        first = re.compile(r"^(from|import) (luoke_mail|fakes|test_\w+|run_tasks|task_sets|agent_a2ui)\b")
        for path in glob.glob(os.path.join(here, "test_*.py")):
            lines = open(path, encoding="utf-8").read().split("\n")
            at = next((n for n, l in enumerate(lines) if l.startswith("import isolate")), None)
            uses = next((n for n, l in enumerate(lines) if first.match(l)), None)
            self.assertIsNotNone(at, os.path.basename(path))
            self.assertTrue(uses is None or at < uses, os.path.basename(path))


class SecureDeleteTests(unittest.TestCase):
    def test_a_removed_mailboxs_mail_is_gone_from_the_file(self):
        """「移除邮箱」 says its records are deleted: the text must not stay in the database file's free pages."""
        from luoke_mail.store import Store
        path = os.path.join(tempfile.mkdtemp(), "s.db")
        st = Store(path)
        secret = "螺壳删除测试-7f3a9c"
        st.upsert_message("gone@example.com", {"message_id": "<x@y>", "uid": "1", "subject": secret, "body": secret * 50,
                                               "from_addr": "a@b.example"})
        st.upsert_message("kept@example.com", {"message_id": "<k@y>", "uid": "2", "subject": "留着", "body": "留着",
                                               "from_addr": "a@b.example"})
        st.clear_account("gone@example.com")
        st.close()
        for suffix in ("", "-journal", "-wal"):
            if os.path.exists(path + suffix):
                with open(path + suffix, "rb") as f:
                    self.assertNotIn(secret.encode(), f.read(), suffix or "db")

    def test_an_older_file_is_scrubbed_once(self):
        import sqlite3
        from luoke_mail import store
        path = os.path.join(tempfile.mkdtemp(), "s.db")
        db = sqlite3.connect(path)                                   # 0.13.2: no secure_delete, a deleted row
        db.executescript(store.SCHEMA)
        db.execute("INSERT INTO items (account, message_id, body) VALUES ('a', '<m>', ?)", ("旧的删除-2b8d" * 40,))
        db.commit()
        db.execute("DELETE FROM items")
        db.commit()
        db.close()
        st = store.Store(path)
        self.assertEqual(st.db.execute("PRAGMA user_version").fetchone()[0], 1)
        st.close()
        with open(path, "rb") as f:
            self.assertNotIn("旧的删除-2b8d".encode(), f.read())


class UninstallTests(unittest.TestCase):
    def test_the_keychain_items_go_before_the_app_is_uninstalled(self):
        """螺壳主控's 「卸载…」 runs `--forget-secrets` before deleting the app and ~/.luoke-mail."""
        from luoke_mail import config, server
        from luoke_mail.credstore import FileStore
        old = config.CONFIG_PATH
        config.CONFIG_PATH = os.path.join(tempfile.mkdtemp(), "config.json")
        self.addCleanup(setattr, config, "CONFIG_PATH", old)
        cfg = config.load()
        cfg["accounts"] = [{"provider": "gmail", "address": "a@example.com"}, {"provider": "qq", "address": "b@qq.com"}]
        cfg["engine"]["base_url"] = "http://127.0.0.1:8000/v1"
        config.save(cfg)
        store = FileStore(os.path.join(tempfile.mkdtemp(), "c.json"))
        for name, account in (("imap", "a@example.com"), ("imap", "b@qq.com"), ("engine", "http://127.0.0.1:8000/v1")):
            store.set(name, account, "secret")
        store.set("imap", "someone-else@example.com", "kept")          # not luoke-mail's settings: not touched
        self.assertEqual(server.forget_secrets(store), 3)
        self.assertIsNone(store.get("imap", "a@example.com"))
        self.assertIsNone(store.get("engine", "http://127.0.0.1:8000/v1"))
        self.assertEqual(store.get("imap", "someone-else@example.com"), "kept")


class StoreMigrationTests(unittest.TestCase):
    def test_an_older_database_gets_the_senders_verdict_column(self):
        import sqlite3
        from luoke_mail import store
        path = os.path.join(tempfile.mkdtemp(), "s.db")
        db = sqlite3.connect(path)
        db.executescript(store.SCHEMA.replace(" auth TEXT,", ""))                # as 0.12 made it
        db.close()
        Store = store.Store
        st = Store(path)
        self.addCleanup(st.close)
        self.assertIn("auth", [r[1] for r in st.db.execute("PRAGMA table_info(items)")])


class TriageTests(unittest.TestCase):
    def test_parse_variants(self):
        self.assertEqual(triage.parse_output(GOOD)["action"], "reply")
        fenced = "```json\n" + GOOD + "\n```"
        self.assertEqual(triage.parse_output(fenced)["due_date"], json.loads(GOOD)["due_date"])
        noisy = "<think>想一想</think>好的，结果如下：" + GOOD + " 以上。"
        self.assertEqual(triage.parse_output(noisy)["priority"], "high")

    def test_normalisation(self):
        r = triage.parse_output('{"action":"Follow-Up","category":"weird","priority":"urgent","deadline":"下周三",'
                                '"summary":"x","reason":"y","reply_points":["a","b","c","d"],"suspicious":0}')
        self.assertEqual(r["action"], "follow_up")
        self.assertEqual(r["category"], "other")
        self.assertEqual(r["priority"], "medium")
        self.assertIsNone(r["due_date"])
        self.assertEqual(len(r["reply_points"]), 3)

    def test_event_only_for_schedule(self):
        from fakes import suggestion
        r = triage.parse_output(suggestion("schedule", due_date=None, event_title="评审会", event_date="2026-10-14",
                                           event_start="15:00", event_end="16:30", event_location="3 楼"))
        self.assertEqual(r["event"], {"title": "评审会", "date": "2026-10-14", "start": "15:00", "end": "16:30", "location": "3 楼"})
        r = triage.parse_output(suggestion("archive", due_date=None, event_date="2026-11-01"))
        self.assertIsNone(r["event"])                    # a date on a non-calendar mail is not an event
        r = triage.parse_output(suggestion("reply", suspicious=True))
        self.assertEqual((r["action"], r["category"]), ("ignore", "phishing"))

    def test_bad_action_rejected(self):
        with self.assertRaises(ValueError):
            triage.parse_output('{"action":"forward","category":"work"}')

    def test_prompt_marks_email_as_data(self):
        msgs = triage.build_messages(samples.build()[0]["mail"], today=samples.TODAY)
        self.assertIn("不可信", msgs[0]["content"])
        self.assertIn("2026-10-12（星期一）", msgs[1]["content"])
        self.assertIn("=== 邮件开始 ===", msgs[1]["content"])

    def test_cc_only(self):
        # luoke-mail#36 / #37: only the cc'd mail's prompt gets the line; high is capped whatever the model says
        cc = next(it["mail"] for it in samples.build() if it["tag"] == "cc-only")
        said = lambda mail: "只在抄送里" in triage.build_messages(mail, today=samples.TODAY, user_address=samples.ME)[1]["content"]  # noqa: E731
        self.assertTrue(said(cc))
        self.assertTrue(said({**cc, "cc": [samples.ME.upper()]}))
        self.assertFalse(said({**cc, "to": [samples.ME]}))                 # on To as well: a direct recipient
        self.assertFalse(said({**cc, "to": []}))                           # nobody on To: tells nothing
        self.assertFalse(any(said(it["mail"]) for it in samples.build() if it["tag"] != "cc-only"))
        self.assertNotIn("只在抄送里", triage.build_messages(cc, today=samples.TODAY)[1]["content"])
        self.assertEqual(triage.settle_cc({"priority": "high"}, cc, samples.ME)["priority"], "medium")
        self.assertEqual(triage.settle_cc({"priority": "low"}, cc, samples.ME)["priority"], "low")
        self.assertEqual(triage.settle_cc({"priority": "high"}, {**cc, "to": [samples.ME]}, samples.ME)["priority"], "high")

    def test_engine_fallbacks_and_retry(self):
        calls = []

        def reply(body):
            calls.append(body)
            if "chat_template_kwargs" in body:
                return 400, "unknown field"
            if len(calls) == 2:
                return 200, "我觉得应该回复。"         # not JSON -> analyze() retries
            return 200, GOOD

        with FakeChat(reply) as srv:
            r = triage.analyze(samples.build()[0]["mail"], engines.Direct(srv.base_url, "test-model"), today=samples.TODAY)
        self.assertEqual(r["action"], "reply")
        self.assertTrue(r["retried"])
        self.assertEqual(calls[1]["response_format"]["type"], "json_schema")

    def test_probe_and_scan(self):
        from unittest import mock
        with FakeChat(lambda b: (200, GOOD), models=("Qwen3.5-9B-Q4_K_M", "embeddinggemma-300m")) as srv, \
                mock.patch.object(engines, "KNOWN_ENGINES", []):        # never knock on this machine's own services
            p = engines.probe(srv.base_url)
            self.assertTrue(p["ok"])
            r = engines.scan([{"name": "test", "base_url": srv.base_url}])
        eng = [e for e in r["engines"] if e["base_url"] == srv.base_url][0]
        self.assertEqual(eng["models"], ["Qwen3.5-9B-Q4_K_M"])          # embedding model filtered out
        nine = [x for x in r["recommendations"] if "9B" in x["title"]][0]
        self.assertTrue(nine["installed"])

    def test_below_16_gb_nothing_is_recommended(self):
        from unittest import mock
        with FakeChat(lambda b: (200, GOOD), models=("Qwen3.5-4B-MLX-4bit", "gemma-4-e4b-it-4bit")) as srv, \
                mock.patch.object(engines, "KNOWN_ENGINES", []), mock.patch.object(engines, "system_memory_gb", lambda: 8.0):
            r = engines.scan([{"name": "test", "base_url": srv.base_url}])
        self.assertTrue(r["too_small"])
        self.assertIsNone(r["suggested"])                    # the small models fail the hard criteria
        self.assertFalse(any(x["fits"] for x in r["recommendations"]))


class SampleTests(unittest.TestCase):
    def test_samples_are_consistent(self):
        items = samples.build()
        self.assertGreaterEqual(len(items), 25)
        for it in items:
            e = it["expect"]
            self.assertTrue(e["action"] <= set(triage.ACTIONS), it["tag"])
            self.assertTrue(e["category"] <= set(triage.CATEGORIES), it["tag"])
            if e["suspicious"]:
                self.assertEqual(e["action"], {"ignore"}, it["tag"])
        self.assertEqual(len({it["tag"] for it in items}), len(items))


class StoreTests(unittest.TestCase):
    def test_file_store_permissions(self):
        path = os.path.join(_tmp, "creds.json")
        s = FileStore(path)
        s.set("imap", "a@b.c", "s3cret")
        self.assertEqual(s.get("imap", "a@b.c"), "s3cret")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        s.delete("imap", "a@b.c")
        self.assertIsNone(s.get("imap", "a@b.c"))


class SentDateTests(unittest.TestCase):
    """10-23: old unread mail is sorted weeks later. An invitation of 8/3 for 「8月8日」, sorted on 10-22,
    came out as next August: the dates were counted from the day of sorting, not the day it was sent."""

    LETTER = {"date": "2026-08-03 09:00", "subject": "夏季交流会邀请", "body": "8月8日下午两点在本校礼堂举行。"}

    def test_the_prompt_gives_the_day_it_was_sent(self):
        import datetime as dt
        text = triage.build_messages(self.LETTER, today=dt.date(2026, 10, 22))[1]["content"]
        self.assertIn("今天是 2026-10-22", text)
        self.assertIn("这封邮件寄出于 2026-08-03", text)
        self.assertIn("按寄出日期换算", text)
        # mail sent today gets the prompt as before (the repeated date moved small models' answers, luoke-core 10-23)
        same = triage.build_messages({**self.LETTER, "date": "2026-10-22 08:00"}, today=dt.date(2026, 10, 22))[1]["content"]
        self.assertNotIn("寄出于", same)

    def test_a_date_a_year_ahead_with_no_year_written_is_last_years(self):
        out = triage.settle_dates({"due_date": "2027-08-07", "event": {"date": "2027-08-08", "start": "14:00"}}, self.LETTER)
        self.assertEqual(out["due_date"], "2026-08-07")
        self.assertEqual(out["event"]["date"], "2026-08-08")

    def test_written_years_next_year_and_near_dates_stay(self):
        near = {"due_date": "2026-09-01", "event": None}
        self.assertEqual(triage.settle_dates(dict(near), self.LETTER), near)
        written = {**self.LETTER, "body": "2027年8月8日举行。"}
        self.assertEqual(triage.settle_dates({"due_date": None, "event": {"date": "2027-08-08"}}, written)["event"]["date"],
                         "2027-08-08")
        said = {**self.LETTER, "body": "明年8月8日举行。"}
        self.assertEqual(triage.settle_dates({"due_date": "2027-08-08", "event": None}, said)["due_date"], "2027-08-08")
        self.assertEqual(triage.settle_dates({"due_date": "2027-08-08", "event": None}, {"subject": "x"})["due_date"],
                         "2027-08-08")                                             # no sent date: nothing to count from

    def test_mail_sorted_before_is_set_right_once(self):
        from luoke_mail.store import Store
        st = Store(os.path.join(tempfile.mkdtemp(), "s.db"))
        iid, _ = st.upsert_message("a@b.example", {"message_id": "<inv@x>", "uid": "1", "from_addr": "x@school.example",
                                                   **self.LETTER})
        st.set_suggestion(iid, {"action": "schedule", "priority": "medium", "category": "event", "summary": "", "reason": "",
                                "due_date": None, "reply_points": [], "suspicious": False,
                                "event": {"title": "交流会", "date": "2027-08-08", "start": "14:00", "end": None,
                                          "location": None}}, "model")
        self.assertEqual(st.settle_dates(triage.settle_dates), 1)
        self.assertEqual(st.settle_dates(triage.settle_dates), 0)
        row = st.db.execute("SELECT event_json FROM items WHERE id=?", (iid,)).fetchone()
        self.assertEqual(json.loads(row["event_json"])["date"], "2026-08-08")
        st.close()


if __name__ == "__main__":
    unittest.main()
