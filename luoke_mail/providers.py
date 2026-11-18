"""Mailbox providers: IMAP endpoints, quirks and the setup guide shown in the wizard.

Quirks worth knowing before touching this file:
- NetEase (163/126) rejects SELECT with "Unsafe Login" unless the client sends an IMAP ID
  command (RFC 2971) after LOGIN. `needs_id` turns that on.
- QQ Mail answers unknown commands with an untagged "* BAD", which makes imaplib wait forever.
  Never send ID (or any extension) to QQ, and always connect with a timeout.
- Gmail has IMAP permanently on; plain passwords stopped working in 2025, an app password
  (requires 2-Step Verification) is the only non-OAuth way in.

The guide (`steps`, `trouble`, `refused`) is where closed-beta testers get stuck most (top-level archived ADR 0008); the same
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
                "在该发件人一行点击「今后一律删除」，再到「设置」的规则中点击「导出为 Gmail 过滤器」，将过滤器文件导入 Gmail。之后由 Gmail 自动删除其邮件。",
                "也可以在 Gmail 中打开该发件人的任意一封邮件，点击右上角「⋮」，选择「屏蔽“发件人”」。之后的邮件会直接进入垃圾邮件。",
            ],
            "link": {"label": "打开 Gmail 过滤器设置", "url": "https://mail.google.com/mail/u/0/#settings/filters"},
        },
        "name": "Gmail",
        # the authserv-id (a pattern) on the Authentication-Results a provider adds on receipt (RFC 8601): its own
        # verdict on the sender, which the sender cannot write
        "authserv": r"mx\.google\.com",
        "host": "imap.gmail.com",
        "port": 993,
        "needs_id": False,
        "domains": ["gmail.com", "googlemail.com"],
        "secret_label": "应用专用密码",
        "secret_hint": "16 位字母，可含空格；不是 Google 账号的登录密码",
        "steps": [
            "打开下方「两步验证」，确认已开启；未开启时按页面提示开启（需要手机）。",
            "打开下方「应用专用密码」（可能需要再次输入 Google 密码），在「应用名称」中填写「螺壳邮件助手」，点击「创建」。",
            "复制弹出的 16 位密码，粘贴到下方。密码仅显示一次，遗失后可重新创建。",
        ],
        "links": [
            {"label": "两步验证", "url": "https://myaccount.google.com/signinoptions/twosv", "account": True},
            {"label": "应用专用密码", "url": "https://myaccount.google.com/apppasswords", "account": True},
        ],
        "notes": "Gmail 的 IMAP 始终开启，无需另外设置。",
        "trouble": [
            "未找到「应用专用密码」：需要开启两步验证。如果两步验证只用安全密钥、账号加入了「高级保护计划」，或者是公司、学校的账号，Google 不提供应用专用密码，此类账号暂时无法连接。",
            "浏览器中登录了多个 Google 账号时：上方两个链接会打开所填地址对应的账号。创建前请核对页面右上角的头像，确认是该地址。",
            "修改 Google 密码后，原有的应用专用密码全部失效，需要重新创建，并在「设置 → 邮箱」中更新。",
            "提示 Google 拦截了登录：在浏览器中打开该 Gmail，按提示完成本人确认后重试。",
        ],
        "refused": "Gmail 拒绝登录：请填写应用专用密码，而不是 Google 账号的登录密码。修改过 Google 密码时，原有的应用专用密码已失效，需要重新创建。",
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
        "authserv": r"mx\.qq\.com",
        "host": "imap.qq.com",
        "port": 993,
        "needs_id": False,
        "domains": ["qq.com", "foxmail.com"],
        "secret_label": "授权码",
        "secret_hint": "16 位字母，不是 QQ 密码",
        "steps": [
            "在电脑浏览器登录 QQ 邮箱网页版，点击右上角头像旁的齿轮进入「设置」，选择「账号与安全」→「安全设置」（旧版界面在「设置」→「账户」）。",
            "找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务」，开启「IMAP/SMTP 服务」。已开启时，点击「生成授权码」。",
            "按页面提示验证身份（通常是用绑定的手机发送一条短信，发送后点击「我已发送」）。",
            "在同一页的「收取选项」里，「收取 ___ 的邮件」选「全部」，并勾选「收取“我的文件夹”」：未勾选时，邮件助手无法读取更早的邮件，也无法归档。",
            "复制页面显示的 16 位授权码，粘贴到下方。关闭页面后无法再次查看，遗失后可重新生成。",
        ],
        "links": [
            {"label": "打开 QQ 邮箱", "url": "https://mail.qq.com"},
            {"label": "QQ 邮箱官方说明", "url": "https://help.mail.qq.com/detail/0/1087"},
        ],
        "notes": "QQ 邮箱仅支持授权码连接，不支持 QQ 密码。",
        "trouble": [
            "开启服务前需要为 QQ 账号绑定手机。",
            "修改 QQ 密码或邮箱独立密码后，授权码会失效，需要重新生成，并在「设置 → 邮箱」中更新。",
            "邮箱地址需填写完整，例如 12345678@qq.com，而不是 QQ 号。foxmail.com 的地址也选择 QQ 邮箱。",
        ],
        "archive_hidden": "邮件未移动：QQ 邮箱未开放「我的文件夹」。请在网页版「设置 → 账号与安全 → 安全设置 → 收取选项」中勾选「收取“我的文件夹”」后再归档。",
        "refused": "QQ 邮箱拒绝登录：请确认已开启 IMAP/SMTP 服务，填写的是授权码，而不是 QQ 密码。修改过 QQ 密码时，原有的授权码已失效，需要重新生成。",
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
        "authserv": r"mx\d+|[a-z\d]+-mx-mta-g\d+-\d+",      # mx3 (older), gzga-mx-mta-g0-5 (2024 on)
        "host": "imap.163.com",
        "port": 993,
        "needs_id": True,
        "domains": ["163.com"],
        "secret_label": "授权码",
        "secret_hint": "16 位字母，不是邮箱登录密码",
        "steps": [
            "在电脑浏览器登录 163 邮箱网页版，点击顶部「设置」，选择「POP3/SMTP/IMAP」。",
            "在「IMAP/SMTP 服务」一行点击「开启」，在弹出的窗口中确认。同一页的「收取选项」默认为「收取最近30天邮件」，如需整理更早的邮件，请选择「收取全部邮件」。",
            "按提示验证：用绑定的手机扫码发送短信（或按页面内容手动发送），发送后点击「我已发送」。",
            "复制页面显示的授权码，粘贴到下方。授权码仅显示一次；需要新授权码时，在同一页的授权密码管理中新增。",
        ],
        "links": [
            {"label": "打开 163 邮箱", "url": "https://mail.163.com"},
        ],
        "notes": "网易邮箱仅支持授权码连接，不支持登录密码。",
        "trouble": [
            "授权码标有有效期时，到期后需要新增，并在「设置 → 邮箱」中更新。修改登录密码后同样需要更新。",
            "开启时提示绑定手机：按页面提示绑定后重新开启。",
        ],
        "refused": "网易邮箱拒绝登录：请确认已开启 IMAP/SMTP 服务，填写的是授权码，而不是登录密码。授权码过期或修改过密码时，需要新增授权码。",
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
        "authserv": r"mx\d+|[a-z\d]+-mx-mta-g\d+-\d+",      # mx3 (older), gzga-mx-mta-g0-5 (2024 on)
        "host": "imap.126.com",
        "port": 993,
        "needs_id": True,
        "domains": ["126.com"],
        "secret_label": "授权码",
        "secret_hint": "16 位字母，不是邮箱登录密码",
        "steps": [
            "在电脑浏览器登录 126 邮箱网页版，点击顶部「设置」，选择「POP3/SMTP/IMAP」。",
            "在「IMAP/SMTP 服务」一行点击「开启」，在弹出的窗口中确认。同一页的「收取选项」默认为「收取最近30天邮件」，如需整理更早的邮件，请选择「收取全部邮件」。",
            "按提示验证：用绑定的手机扫码发送短信（或按页面内容手动发送），发送后点击「我已发送」。",
            "复制页面显示的授权码，粘贴到下方。授权码仅显示一次；需要新授权码时，在同一页的授权密码管理中新增。",
        ],
        "links": [
            {"label": "打开 126 邮箱", "url": "https://mail.126.com"},
        ],
        "notes": "网易邮箱仅支持授权码连接，不支持登录密码。",
        "trouble": [
            "授权码标有有效期时，到期后需要新增，并在「设置 → 邮箱」中更新。修改登录密码后同样需要更新。",
            "开启时提示绑定手机：按页面提示绑定后重新开启。",
        ],
        "refused": "网易邮箱拒绝登录：请确认已开启 IMAP/SMTP 服务，填写的是授权码，而不是登录密码。授权码过期或修改过密码时，需要新增授权码。",
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
            "服务器地址和端口可在邮箱的帮助页面中查找，通常位于「客户端设置」「IMAP」一节。",
            "多数邮箱的第三方客户端需要使用单独生成的密码或授权码，不支持登录密码。",
        ],
        "refused": "登录失败：邮箱地址或密码不正确。多数邮箱需要填写单独生成的密码或授权码，而不是登录密码。",
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
