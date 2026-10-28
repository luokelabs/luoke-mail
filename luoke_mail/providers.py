"""Mailbox providers: IMAP endpoints, quirks and the setup guide shown in the wizard.

Quirks worth knowing before touching this file:
- NetEase (163/126) rejects SELECT with "Unsafe Login" unless the client sends an IMAP ID
  command (RFC 2971) after LOGIN. `needs_id` turns that on.
- QQ Mail answers unknown commands with an untagged "* BAD", which makes imaplib wait forever.
  Never send ID (or any extension) to QQ, and always connect with a timeout.
- Gmail has IMAP permanently on; plain passwords stopped working in 2025, an app password
  (requires 2-Step Verification) is the only non-OAuth way in.

The guide (`steps`, `trouble`, `refused`) is where closed-beta testers get stuck most (top-level ADR 0008); the same
walk-through, longer, is docs/community/授权码引导.md -- change both together. Checked against the providers' help pages
and current tutorials on 2026-10-15; the providers move their menus, so testers' reports win.
"""
from __future__ import annotations

import urllib.parse

PROVIDERS = {
    "gmail": {
        "purge_days": 30,
        "block": {
            "title": "在 Gmail 中屏蔽或过滤",
            "steps": [
                "在此卡片上点击「以后都移到垃圾箱」，再点击「导出到 Gmail…」，将过滤器文件导入 Gmail。之后由 Gmail 自动将其邮件移到垃圾箱。",
                "也可以在 Gmail 中打开该发件人的任意一封邮件，点击右上角「⋮」，选择「屏蔽“发件人”」。之后的邮件会直接进入垃圾邮件。",
            ],
            "link": {"label": "打开 Gmail 过滤器设置", "url": "https://mail.google.com/mail/u/0/#settings/filters"},
        },
        "name": "Gmail",
        # the authserv-id Gmail puts on the Authentication-Results it adds on receipt (RFC 8601): its own verdict
        # on the sender, which the sender cannot write. Other providers: not relied on yet
        "authserv": "mx.google.com",
        "host": "imap.gmail.com",
        "port": 993,
        "needs_id": False,
        "domains": ["gmail.com", "googlemail.com"],
        "secret_label": "应用专用密码",
        "secret_hint": "16 位字母，带不带空格都可以；不是 Google 账号的登录密码",
        "steps": [
            "打开下方「两步验证」，确认已开启。没开的话按页面提示开启（需要手机）。",
            "打开下方「应用专用密码」（可能要再输一次 Google 密码），在「应用名称」里填「螺壳邮件助手」，点击「创建」。",
            "复制弹出的 16 位密码，粘贴到下方。密码只显示这一次，忘了可以再创建一个。",
        ],
        "links": [
            {"label": "两步验证", "url": "https://myaccount.google.com/signinoptions/twosv", "account": True},
            {"label": "应用专用密码", "url": "https://myaccount.google.com/apppasswords", "account": True},
        ],
        "notes": "Gmail 的 IMAP 一直开着，不用另外设置。",
        "trouble": [
            "找不到「应用专用密码」：先开启两步验证。如果两步验证只用安全密钥、账号加入了「高级保护计划」，或者是公司、学校的账号，Google 不提供应用专用密码，这样的账号暂时无法连接。",
            "浏览器里登录了多个 Google 账号：上面两个链接会打开你填写的这个地址的账号。创建前看一眼页面右上角的头像，确认是这个地址。",
            "改过 Google 密码后，原来的应用专用密码全部失效，要重新创建一个，在「设置 → 邮箱」里换上。",
            "提示 Google 拦下了登录：在浏览器里打开这个 Gmail，按提示确认是你本人，再回来重试。",
        ],
        "refused": "Gmail 拒绝登录：请填写应用专用密码，不是 Google 账号的登录密码。改过 Google 密码的话，原来的应用专用密码已经失效，要重新创建。",
    },
    "qq": {
        "purge_days": 30,
        "block": {
            "title": "在 QQ 邮箱中拉黑",
            "steps": [
                "登录 QQ 邮箱网页版，进入「设置」→「反垃圾」→「黑名单」。",
                "点击「设置邮件地址黑名单」，粘贴地址后点击「添加到黑名单」。之后的来信会被拒收。",
                "如果对方使用多个地址发信，可以通过「设置域名黑名单」拉黑整个域名。",
            ],
            "link": {"label": "打开 QQ 邮箱", "url": "https://mail.qq.com"},
        },
        "name": "QQ 邮箱",
        "host": "imap.qq.com",
        "port": 993,
        "needs_id": False,
        "domains": ["qq.com", "foxmail.com"],
        "secret_label": "授权码",
        "secret_hint": "16 位字母，不是 QQ 密码",
        "steps": [
            "在电脑浏览器登录 QQ 邮箱网页版，点击右上角头像旁的齿轮进入「设置」，选择「账号与安全」→「安全设置」（旧版界面在「设置」→「账户」）。",
            "找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务」，开启「IMAP/SMTP 服务」。已经开着的，点击「生成授权码」。",
            "按页面提示验证身份（通常是用绑定的手机发一条短信，发完点击「我已发送」）。",
            "复制页面显示的 16 位授权码，粘贴到下方。关掉页面就看不到了，忘了可以再生成一个。",
        ],
        "links": [
            {"label": "打开 QQ 邮箱", "url": "https://mail.qq.com"},
            {"label": "QQ 邮箱官方说明", "url": "https://help.mail.qq.com/detail/0/1087"},
        ],
        "notes": "QQ 邮箱只能用授权码连接，QQ 密码不行。",
        "trouble": [
            "开启服务时要先给 QQ 账号绑定手机。",
            "改过 QQ 密码或邮箱独立密码后，授权码会失效，要重新生成一个，在「设置 → 邮箱」里换上。",
            "邮箱地址要填完整，例如 12345678@qq.com，不是只填 QQ 号。foxmail.com 的地址也选 QQ 邮箱。",
        ],
        "refused": "QQ 邮箱拒绝登录：请确认已开启 IMAP/SMTP 服务，填写的是授权码，不是 QQ 密码。改过 QQ 密码的话，原来的授权码已经失效，要重新生成。",
    },
    "163": {
        "purge_days": 7,
        "block": {
            "title": "在网易邮箱中拉黑",
            "steps": [
                "登录网页版邮箱，进入「设置」→「反垃圾/黑白名单」（部分版本位于「邮箱安全设置」中）。",
                "在「黑名单」中点击「添加黑名单」，粘贴地址或域名。之后的来信会被拒收。",
            ],
            "link": {"label": "打开 163 邮箱", "url": "https://mail.163.com"},
        },
        "name": "网易 163 邮箱",
        "host": "imap.163.com",
        "port": 993,
        "needs_id": True,
        "domains": ["163.com"],
        "secret_label": "授权码",
        "secret_hint": "16 位字母，不是邮箱登录密码",
        "steps": [
            "在电脑浏览器登录 163 邮箱网页版，点击顶部「设置」，选择「POP3/SMTP/IMAP」。",
            "在「IMAP/SMTP 服务」一行点击「开启」，在弹出的窗口里确认。",
            "按提示验证：用绑定的手机扫码发送短信（或照页面上的内容手动发送），发完点击「我已发送」。",
            "复制页面显示的授权码，粘贴到下方。授权码只显示一次；以后需要新的，在同一页的授权密码管理里新增一个。",
        ],
        "links": [
            {"label": "打开 163 邮箱", "url": "https://mail.163.com"},
        ],
        "notes": "网易邮箱只能用授权码连接，登录密码不行。",
        "trouble": [
            "授权码如果标了有效期，到期后要新增一个，在「设置 → 邮箱」里换上。改过登录密码后也要换。",
            "开启时提示先绑定手机：按页面提示绑定后再开启。",
        ],
        "refused": "网易邮箱拒绝登录：请确认已开启 IMAP/SMTP 服务，填写的是授权码，不是登录密码。授权码过期或改过密码的话，要新增一个。",
    },
    "126": {
        "purge_days": 7,
        "block": {
            "title": "在网易邮箱中拉黑",
            "steps": [
                "登录网页版邮箱，进入「设置」→「反垃圾/黑白名单」（部分版本位于「邮箱安全设置」中）。",
                "在「黑名单」中点击「添加黑名单」，粘贴地址或域名。之后的来信会被拒收。",
            ],
            "link": {"label": "打开 126 邮箱", "url": "https://mail.126.com"},
        },
        "name": "网易 126 邮箱",
        "host": "imap.126.com",
        "port": 993,
        "needs_id": True,
        "domains": ["126.com"],
        "secret_label": "授权码",
        "secret_hint": "16 位字母，不是邮箱登录密码",
        "steps": [
            "在电脑浏览器登录 126 邮箱网页版，点击顶部「设置」，选择「POP3/SMTP/IMAP」。",
            "在「IMAP/SMTP 服务」一行点击「开启」，在弹出的窗口里确认。",
            "按提示验证：用绑定的手机扫码发送短信（或照页面上的内容手动发送），发完点击「我已发送」。",
            "复制页面显示的授权码，粘贴到下方。授权码只显示一次；以后需要新的，在同一页的授权密码管理里新增一个。",
        ],
        "links": [
            {"label": "打开 126 邮箱", "url": "https://mail.126.com"},
        ],
        "notes": "网易邮箱只能用授权码连接，登录密码不行。",
        "trouble": [
            "授权码如果标了有效期，到期后要新增一个，在「设置 → 邮箱」里换上。改过登录密码后也要换。",
            "开启时提示先绑定手机：按页面提示绑定后再开启。",
        ],
        "refused": "网易邮箱拒绝登录：请确认已开启 IMAP/SMTP 服务，填写的是授权码，不是登录密码。授权码过期或改过密码的话，要新增一个。",
    },
    "custom": {
        "purge_days": None,
        "block": {
            "title": "在邮箱中拉黑",
            "steps": [
                "大多数邮箱的网页版设置中都有「黑名单」「拒收」或「过滤规则」，通常位于「反垃圾」一栏。",
                "将发件地址加入黑名单后，之后的来信会被拒收或移到垃圾邮件。",
            ],
            "link": None,
        },
        "name": "其他邮箱（手动填写 IMAP）",
        "host": "",
        "port": 993,
        "needs_id": False,
        "domains": [],
        "secret_label": "密码或授权码",
        "secret_hint": "多数邮箱需要在网页版设置中单独生成",
        "steps": [
            "在邮箱网页版设置中开启 IMAP，并生成第三方客户端密码或授权码。",
            "在下方填写 IMAP 服务器地址和端口（通常为 993）。",
        ],
        "links": [],
        "notes": "未经测试的邮箱可能存在兼容性问题。",
        "trouble": [
            "服务器地址和端口在邮箱的帮助页面里找，通常写在「客户端设置」「IMAP」一节。",
            "多数邮箱的第三方客户端要用单独生成的密码或授权码，登录密码不行。",
        ],
        "refused": "登录失败：邮箱地址或密码不正确。多数邮箱要填单独生成的密码或授权码，而不是登录密码。",
    },
}


def guess_provider(address: str) -> str | None:
    domain = address.rsplit("@", 1)[-1].strip().lower()
    for key, p in PROVIDERS.items():
        if domain in p["domains"]:
            return key
    return None


def gmail_url(address: str | None, fragment: str) -> str:
    """A page in Gmail on the web, in the account luoke is connected to. /mail/u/0/ is only the browser's first
    signed-in Google account; `authuser` names the account (an address in the path stopped working in 2026)."""
    who = "?authuser=" + urllib.parse.quote(address, safe="@") if address else "0/"
    return f"https://mail.google.com/mail/u/{who}#{fragment}"


def gmail_message_url(address: str | None, message_id: str, gm_msgid: str | None = None) -> str:
    """The message itself when Gmail's own id is known (read over IMAP), otherwise a search by its Message-ID."""
    if gm_msgid:
        return gmail_url(address, "all/" + format(int(gm_msgid), "x"))
    return gmail_url(address, "search/" + urllib.parse.quote("rfc822msgid:" + message_id.strip("<>"), safe=""))


def public_list() -> list[dict]:
    """Provider info for the UI (no behaviour flags)."""
    fields = ("name", "host", "port", "secret_label", "secret_hint", "steps", "links", "notes", "trouble", "purge_days", "block")
    return [dict({k: p[k] for k in fields}, key=key) for key, p in PROVIDERS.items()]
