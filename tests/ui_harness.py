"""Look at the real page without touching real data: a fake Gmail, a fake model and a fake desktop
(no keychain, no login item, notifications printed to the terminal), in a temporary data folder.

    python3 tests/ui_harness.py [off|on] [--port 8799] [--hub SOCKET]

It also serves luoke-mail's side of the tool protocol on <data folder>/run/agent.sock, for 螺壳 (luoke-agent).
It reaches nothing in daily use: the port is exactly the one given
(taken: an error), the model page lists only the fake model service, and 螺壳主控 is there only when `--hub` names
a hub started for testing (luoke-core's `tools/dev_hub.sh small mail`).

"on" starts as if background sorting were on and running as the login item; its first run comes ~8 s
after start. `--packaged` is the app on a new Mac: nothing set up, 螺壳主控 only (a fake one, in this process) and
still downloading the model for about a minute; any mailbox address connects to the fake Gmail. The page address is printed; it is also written to <data folder>/url.txt.
"""
import argparse
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
home = tempfile.mkdtemp(prefix="luoke-ui-")
os.environ["LUOKE_MAIL_HOME"] = home
sys.path[:0] = [os.path.dirname(HERE), HERE]

from http.server import ThreadingHTTPServer  # noqa: E402

from luoke_mail import background, config, engines, profile, server  # noqa: E402
from luoke_mail.credstore import FileStore  # noqa: E402
from luoke_mail.mailbox import Mailbox  # noqa: E402
from fakes import HUB_TASKS, FakeChat, FakeHub, FakeIMAP  # noqa: E402
from test_background import FakeDesktop  # noqa: E402
from test_old_mail import mail  # noqa: E402
from test_archive import ARCHIVED, FINANCE, judge  # noqa: E402
from test_digest import model, newsletter, site_get  # noqa: E402
from test_learn import model as learn_model, sent  # noqa: E402

ME = "me@example.com"


class PrintingDesktop(FakeDesktop):
    def notify(self, title, subtitle, body):
        print(f"[通知] {subtitle}｜{body}", flush=True)
        return super().notify(title, subtitle, body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", choices=("off", "on"), default="off")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--two", action="store_true", help="a second connected mailbox (another fake Gmail)")
    ap.add_argument("--hub", default="/nonexistent/luoke-hub-for-tests.sock", help="a hub started for testing")
    ap.add_argument("--packaged", action="store_true", help="the app's first run, the model still downloading")
    a = ap.parse_args()
    if a.hub != ap.get_default("hub") and not os.path.exists(a.hub):
        ap.error(f"{a.hub} 不存在")
    os.environ["LUOKE_HUB_SOCKET"] = a.hub
    engines.KNOWN_ENGINES = []                     # never knock on this machine's own model services
    background.FIRST_RUN_DELAY = 8
    pool = ["本地大模型部署指南", "融资快讯", "用 Rust 写命令行工具", "本地推理提速一倍的方法", "活动报名：开发者大会",
            "AI 编程助手的三个月体会", "本地模型的量化格式怎么选"]
    old = [(newsletter(10 + i, "技术周刊 <weekly@news.example>", [pool[(i + k) % len(pool)] for k in range(3)], 35 + 7 * i,
                       "weekly.news.example"), i % 3 == 0) for i in range(12)]
    old += [(mail(30 + i, f"您的账单已出（{i + 1} 月）", "某银行 <bill@bank.example>", 60 + 30 * i, "bill.bank.example"), True) for i in range(5)]
    old += [(mail(40, "去年的聚餐照片", "张伟 <zhangwei@mail.example>", 300), True),
            (mail(41, "合同扫描件", "李律师 <lawyer@firm.example>", 200), True)]
    imap = FakeIMAP([(mail(1, "Q3 预算评审", "王总 <boss@corp.example>", 1), False),
                     (mail(2, "双十一促销", "某某商城 <news@shop.example>", 2), False),
                     (mail(3, "周五团建报名", "HR <hr@corp.example>", 1), False),
                     (mail(4, "您的账户已冻结，请立即验证", "某银行 <sec@bank-verify.example>", 1), False),
                     (mail(5, "项目周会邀请", "项目组 <team@corp.example>", 1), False)] + old, flavour="gmail").__enter__()
    for m in imap.folders["INBOX"]:
        if b"lawyer@" in m["raw"]:
            m["flags"].add("\\Flagged")
    # archived mail (not in the inbox): 清理 → 已归档的邮件
    for rep_ in range(4):
        for i, (subject, sender, cat, labels, starred) in enumerate(ARCHIVED):
            imap._add("[Gmail]/All Mail", mail(200 + 10 * rep_ + i, subject, sender, 120 + 40 * rep_ + i),
                      {"\\Seen"} | ({"\\Flagged"} if starred and not rep_ else set()))
            imap.folders["[Gmail]/All Mail"][-1].update(category=cat, labels=labels)
    imap.folders[FINANCE] = []
    # archived on the phone yesterday, before a sync saw it in the inbox: 「你归档的邮件里还有要处理的」
    imap._add("[Gmail]/All Mail", mail(70, "项目周会改到周四下午", "项目组 <team@corp.example>", 1), {"\\Seen"})
    # what the user sent: 了解你 reads it
    for n, (to, subject, body, days) in enumerate([
            ("boss@corp.example", "本周周报", "王总，本周周报如下。\n小周 产品负责人", 3),
            ("wanglei@corp.example", "排期", "王磊，下午碰一下排期。\n小周 产品负责人", 8),
            ("wanglei@corp.example", "Re: 需求评审", "王磊，纪要没问题。\n小周 产品负责人", 15),
            ("boss@corp.example", "Re: Q3 预算评审", "王总，预算表已更新。\n小周 产品负责人", 20)]):
        imap._add("[Gmail]/Sent Mail", sent(900 + n, to, subject, body, days), {"\\Seen"})
    # more archived mail, so judging takes several batches (and 暂停 can be tried in the middle)
    for k, sender in enumerate(["某论坛 <notice@forum.example>", "某平台 <no-reply@verify.example>", "某商城 <promo@mall.example>"]):
        for i in range(60):
            imap._add("[Gmail]/All Mail", mail(500 + 60 * k + i, f"通知 {k}-{i}", sender, 90 + 3 * i), {"\\Seen"})

    def slow(body):                      # slow enough to watch the progress bar (and to press 暂停 in the middle)
        system = body["messages"][0]["content"]
        time.sleep(6 if system.startswith("你在帮用户清理") else 4 if system.startswith("你在替用户读订阅邮件") else 0.8)
        if system.startswith(("下面是用户自己发出的邮件", "下面是用户收到的")):
            return learn_model(body)         # 了解你
        if not system.startswith("你在帮用户清理"):
            return model(body)               # triage, newsletters (精选), drafting interests
        status, out = judge(body)                    # old mail: plus newsletters, so every section shows up
        lines = [ln for ln in body["messages"][1]["content"].splitlines() if ln[:1].isdigit()]
        items = json.loads(out)["items"]
        for it, ln in zip(items, lines):
            it["kind"] = "newsletter" if "Weekly" in ln else it["kind"]
        return status, json.dumps({"items": items}, ensure_ascii=False)
    chat = FakeChat(slow).__enter__()
    desk = PrintingDesktop()
    fake_hub = None
    if a.packaged:                       # 螺壳主控 downloading the model: about a minute, then every task available
        fake_hub = FakeHub(lambda task, body, req: slow(body)[1], tasks={
            t: {"status": "downloading", "reason": "正在下载", "progress": 0.0, "params": {"max_chars": 32000, "batch": b}}
            for t, b in HUB_TASKS.items()}).__enter__()
        os.environ["LUOKE_HUB_SOCKET"] = fake_hub.path

        def download():
            for step in range(1, 31):
                time.sleep(2)
                for v in fake_hub.tasks.values():
                    v.update(progress=step / 30) if step < 30 else v.update(status="available", reason=None, progress=None)
                fake_hub.version = str(step)
        threading.Thread(target=download, daemon=True).start()
    app = server.App(secret_store=FileStore(os.path.join(home, "c.json")), desktop_=desk, service=a.mode == "on",
                     hub_only=a.packaged)
    if not a.packaged:
        app.cfg["accounts"] = [{"provider": "gmail", "address": ME, "host": None, "port": 993}]
        app.secrets.set(server.MAIL_SECRET, ME, "secret")
    servers = {ME: imap}
    if a.two:                            # a second mailbox: a few mails of its own
        other = "work@example.org"
        servers[other] = FakeIMAP([(mail(60, "合同修订意见", "法务 <legal@corp.example>", 1), False),
                                   (mail(61, "季度复盘会议", "王总 <boss@corp.example>", 2), False)], user=other, flavour="gmail").__enter__()
        app.cfg["accounts"].append({"provider": "gmail", "address": other, "host": None, "port": 993})
        app.secrets.set(server.MAIL_SECRET, other, "secret")
    app.open_mailbox = lambda acct=None: Mailbox("gmail", acct if acct in servers else ME, "secret", host="127.0.0.1",
                                                 port=servers.get(acct, imap).port, ssl=False, timeout=5)
    app.profiler.fetch = lambda url: profile.fetch_site(url, get=site_get)     # no network: https://lab.example is a fake site
    app.cfg["engine"] = ({"via": "hub", "hub_socket": fake_hub.path} if a.packaged else
                         {"base_url": chat.base_url, "model": "test-model", "engine_name": "Fake", "has_api_key": False})
    if a.mode == "on":
        app.cfg["auto"] = {"enabled": True, "every": 30}
        desk.installed = True
    config.save(app.cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), server.make_handler(app))
    app.port = a.port
    app.on_exit = lambda: threading.Thread(target=httpd.shutdown, daemon=True).start()
    app.auto.tick = 2
    app.auto.start()
    app.agent.start()                              # 螺壳 (luoke-agent) can ask this fake luoke-mail: <home>/run/agent.sock
    url = f"http://127.0.0.1:{a.port}/?t={app.token}"
    with open(os.path.join(home, "url.txt"), "w") as f:
        f.write(url)
    print(f"数据文件夹：{home}\n页面：{url}\n给螺壳的 socket：{os.path.join(home, 'run', 'agent.sock')}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    print("退出：", app.exit_reason, flush=True)


if __name__ == "__main__":
    main()
