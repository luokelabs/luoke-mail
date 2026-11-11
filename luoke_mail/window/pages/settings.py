"""设置 in the window: what the browser page's 设置 shows (App.state), plus the mail side of 记忆 -- 从邮件中了解你
and 你的网站和博客.

Adding a mailbox or changing its app password (static/setup.js) is a form here where the core draws a password field
(§4 secureField): what is typed reaches the tool only in the press of 保存并测试连接 and goes to the keychain; an older
core cannot take one, so 「添加邮箱…」 opens the browser page's 设置 there.

Not here, and why: choosing a model service (螺壳 runs the model:
only which model is shown); 外观 (the window follows 螺壳's own). What the browser page asks in a dialog -- 处理模式,
后台自动整理, 移除 a mailbox, 设为螺壳自己的邮箱, 全部清除 -- is asked with the same words in the page's 确认."""
from __future__ import annotations

import urllib.parse

from ... import background
from .. import kit

POWER = [("full", "全速", "模型连续运行，速度最快，风扇可能全速转。"),
         ("balanced", "均衡", "每次调用后休息同样长的时间，模型约一半时间在运行，速度约为全速的一半。"),
         ("quiet", "安静", "每次调用后休息三倍的时间，模型约四分之一时间在运行，速度约为全速的四分之一。")]
STAGE = {"sent_check": "正在查找你发出的邮件", "sent": "正在阅读你发出的邮件", "received": "正在了解你收到的往来邮件"}

MODE_ON = ("开启处理模式：开启后，可以一次处理多封邮件——删除或移到垃圾邮件（邮箱一般保留 30 天，可以恢复）；在订阅体检和清理里，"
           "清掉一个发件人的历史邮件。归档、标为已读、加标签、存草稿和退订，不开也能用。螺壳不会发信、转发或永久删除邮件。")
OWN_ON = ("把 {address} 设为螺壳自己的邮箱：这个邮箱的邮件分析后会直接处理，该归档的归档，该删的删除；可疑的和要你处理的留在收件箱。"
          "每封都列在「今日处理」里，可以撤销；删除的，在邮箱清空「已删除」之前都能撤销。只用于专给螺壳的邮箱，不要设在日常用的邮箱上。")
REMOVE = ("移除 {address}：螺壳不再读这个邮箱，并删掉本机存的授权码，以及它的分析结果、规则、清理和精选记录、操作记录。"
          "邮箱里的邮件和关于你的记忆不受影响。可以重新连接，但删掉的记录找不回来。")
FORGET = ("清除本机数据：删除邮箱授权码、模型 API Key、规则、分析结果和操作记录，并关闭后台自动整理。邮箱里的邮件不受影响。"
          "勾选了「同时删除邮件助手写进记忆的内容」的，也只删邮件助手自己记下和推测的；你确认过、改过或自己写的会保留。")


def _every(m: int) -> str:
    return f"每 {m // 60} 小时" if m >= 60 else f"每 {m} 分钟"


def _section(p: kit.Page, title: str) -> None:
    p.add(p.divider(), p.text(title, "h4"))


def _mailboxes(w, p: kit.Page, st: dict) -> None:
    _section(p, "邮箱")
    accounts = st.get("accounts") or []
    names = {x.get("key"): x.get("name") for x in st.get("providers") or []}
    for a in accounts:
        what = " · ".join(x for x in (f"{names[a['provider']]} · IMAP" if names.get(a.get("provider")) else "IMAP",
                                      "螺壳自己的邮箱" if a.get("own") else None,
                                      None if a.get("has_secret") else "授权码未保存") if x)
        own = p.do("改为我的邮箱", "/api/mail/own", {"source": a["key"], "own": False}, quiet=True) if a.get("own") else \
            p.do("设为螺壳自己的邮箱", "/api/mail/own", {"source": a["key"], "own": True}, quiet=True,
                 confirm=OWN_ON.format(address=a["address"]))
        last = len(accounts) == 1 and (st.get("auto") or {}).get("enabled")
        change = p.go("修改授权码…", {"add": a["key"], "kind": a.get("provider"), "typed": None}, quiet=True) \
            if p.can("secureField") else None
        p.add(p.card(p.text(a.get("address") or a["key"], "h5"), p.text(what, "caption"),
                     p.row(change, own, p.do("移除", "/api/mail/remove", {"key": a["key"]}, quiet=True,
                                     confirm=REMOVE.format(address=a["address"]) +
                                     ("这是唯一连接的邮箱，后台自动整理也将关闭。" if last else "")))))
    hint = "推荐给螺壳一个专用邮箱，把订阅、通知转发过去，由它自动整理。" \
        if accounts and not any(a.get("own") for a in accounts) else "每个邮箱的规则、清理和精选分开管理。"
    if p.can("secureField"):
        form = w.view(p.page)
        if form.get("add"):
            return _form(w, p, st, form)
        p.add(p.text(hint, "caption"),
              p.row(p.go("添加邮箱…" if accounts else "连接邮箱…", {"add": "new", "kind": None, "typed": None},
                         primary=not accounts, quiet=False)))
        return
    p.add(p.text(hint, "caption"),
          p.text("添加邮箱、修改授权码要在浏览器里填写，授权码只存在这台 Mac 的钥匙串里。", "caption"),
          p.row(p.do("添加邮箱…" if accounts else "连接邮箱…", "open_browser", {"page": "settings"}, primary=not accounts)))


GUESS = {"gmail.com": "gmail", "googlemail.com": "gmail", "qq.com": "qq", "foxmail.com": "qq", "163.com": "163",
         "126.com": "126"}                        # static/setup.js DOMAIN_GUESS


def _form(w, p: kit.Page, st: dict, form: dict) -> None:
    """连接邮箱 / 修改邮箱 (static/setup.js fillMail): the type, its steps for the app password, the address, the
    password (a password field: empty here, §4 secureField), and for 其他邮箱 the IMAP server."""
    accounts = {a["key"]: a for a in st.get("accounts") or []}
    editing = accounts.get(form["add"])
    address = form.get("typed") or (editing or {}).get("address") or ""
    kinds = {x["key"]: x for x in st.get("providers") or []}
    kind = form.get("kind") or GUESS.get(address.rsplit("@", 1)[-1].lower())
    prov = kinds.get(kind)
    p.add(p.text("修改邮箱" if editing else ("添加邮箱" if accounts else "连接邮箱"), "h5"),
          p.row(*[p.text(x["name"], "caption") if x["key"] == kind else p.go(x["name"], {"kind": x["key"]}, quiet=False)
                  for x in kinds.values()]))
    if not prov:
        p.add(p.text("先选择邮箱类型。", "caption"), p.row(p.go("取消", {"add": None, "kind": None, "typed": None})))
        return
    for n, step in enumerate(prov.get("steps") or [], 1):
        p.add(p.text(f"{n}. {step}", limit=600))
    links = [p.link(l["label"], _account_url(l, address)) for l in prov.get("links") or []]
    if any(links):
        p.add(p.row(*links))
    if prov.get("notes"):
        p.add(p.text(prov["notes"], "caption", limit=600))
    fields = ["address"]
    p.add(p.field("address", "邮箱地址", address),
          p.field("secret", prov.get("secret_label") or "授权码", secret=True))
    if prov.get("secret_hint"):
        p.add(p.text(prov["secret_hint"], "caption"))
    if kind == "custom":
        p.add(p.field("host", "IMAP 服务器", (editing or {}).get("host") or ""),
              p.field("port", "端口", str((editing or {}).get("port") or prov.get("port") or 993)))
        fields += ["host", "port"]
    args = {"key": editing["key"] if editing else None, "provider": kind, "secret": {"$input": "secrets/secret"},
            **{f: {"$input": f"fields/{f}"} for f in fields}}
    p.add(p.text("授权码只存在这台 Mac 的钥匙串里。", "caption"),
          p.row(p.do("保存并测试连接", "connect_mail", args, inputs=["fields", "secrets"], primary=True),
                p.go("取消", {"add": None, "kind": None, "typed": None})))
    trouble = prov.get("trouble") or []
    if trouble:
        p.add(p.text("连不上？", "h5"), *[p.text(t, "caption", limit=600) for t in trouble])


def _account_url(link: dict, address: str) -> str:
    """Google's pages for the account being connected, not the browser's first one (static/setup.js accountLinks)."""
    url = link.get("url") or ""
    if not link.get("account") or "@" not in address:
        return url
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode([*urllib.parse.parse_qsl(parts.query), ("authuser", address)])
    return urllib.parse.urlunsplit(parts._replace(query=query))


def _model(w, p: kit.Page, st: dict) -> None:
    _section(p, "本机模型")
    e = st.get("engine") or {}
    hm = st.get("hub_model") or {}
    if e.get("via") == "hub" or st.get("hub_only"):
        words = "螺壳" + (f" · {hm['name']}" if hm.get("name") else "") + (" · 就绪" if hm.get("ready") else "")
    elif e.get("model"):
        words = f"{e['model']} · {e.get('engine_name') or '本机模型服务'}"       # not its address: a detail
    else:
        words = "未选择"
    p.add(p.text(words))
    if st.get("hub_only"):
        p.add(p.text("由螺壳按这台 Mac 挑选；要换档，在螺壳的设置里换。", "caption"))
    note = w.app.model_note() if hasattr(w.app, "model_note") else None
    if note:
        p.add(p.text(note.get("text") if isinstance(note, dict) else note, "caption"))


def _mode(w, p: kit.Page, st: dict, has_mail: bool) -> None:
    _section(p, "处理模式")
    if not has_mail:
        p.add(p.text("未开启"), p.text("连接邮箱后可开启。", "caption"))
    elif (st.get("mode") or {}).get("write"):
        p.add(p.row(p.text("已开启：批量删除、移到垃圾邮件时，每次都需要你确认。"),
                    p.do("关闭", "/api/mode", {"write": False})))
    else:
        p.add(p.row(p.text("未开启：批量删除、移到垃圾邮件前需要开启。"),
                    p.do("开启处理模式", "/api/mode", {"write": True, "consent": True}, primary=True, confirm=MODE_ON)))


def _auto(w, p: kit.Page, st: dict, has_mail: bool) -> None:
    _section(p, "后台自动整理")
    a = st.get("auto") or {}
    d = a.get("desktop") or {}
    if not has_mail:
        p.add(p.text("连接邮箱后可用。", "caption"))
        return
    intervals = list(a.get("intervals") or background.INTERVALS)
    every = a.get("every") if a.get("every") in intervals else intervals[0]
    choices = [(str(m), _every(m)) for m in intervals]
    who = "螺壳" if d.get("in_luoke") else "螺壳邮件助手"
    if not a.get("enabled"):
        own = any(x.get("own") for x in st.get("accounts") or [])
        sure = ("开启后台自动整理：按选好的间隔同步并分析新邮件；有要回复、加日程或跟进的邮件时通知你，每晚 20:00 发一条「今天处理了 N 封」；"
                "标为已读，按你确认过的习惯归档或加标签，都能在「今日处理」里撤销。"
                + ("不会退订或发信。设为螺壳自己的邮箱里，建议删除的会删除，其他邮箱不会。" if own else "不会删除邮件、退订或发信。")
                + ("第一次通知时，请在系统提示里选「允许」。" if d.get("in_luoke") or d.get("supported") else ""))
        p.add(p.text("已关闭：开启后定时整理，有要你处理的邮件时发通知。"),
              p.choice("every", "整理间隔", choices, str(every)),
              p.row(p.do("开启", "/api/auto", {"enabled": True, "every": {"$input": "choices/every"}, "consent": True},
                         inputs=["choices"], primary=True, confirm=sure)))
        return
    stalled = d.get("supported") and not a.get("service") and (not d.get("installed") or d.get("stale"))
    if stalled:
        p.add(p.row(p.icon("warning"), p.text("未运行：" + ("程序的位置已变更。" if d.get("stale") else "登录项已被移除。")),
                    p.do("修复", "/api/auto", {"enabled": True, "every": every, "consent": True}, primary=True)))
        return
    n = st.get("notify") or {}
    if n.get("authorization") == "denied" or n.get("alert") == "disabled":
        p.add(p.row(p.icon("warning"),
                    p.text(f"macOS 没有允许{who}显示通知，提醒不会出现在你面前。请在「系统设置 → 通知 → {who}」里打开「允许通知」。"),
                    p.do("打开通知设置", "/api/auto/notify-settings", {}, primary=True)))
    elif n.get("authorization") == "not_determined":
        p.add(p.text(f"{who}还没有获得通知权限。点「发送测试通知」，在 macOS 的询问里选「允许」。", "caption"))
    last = a.get("last")
    if last:
        nxt = f"。下次约在 {a['next_in_min']} 分钟后" if a.get("next_in_min") is not None else ""
        said = f"上次 {(last.get('at') or '')[11:]}：{last.get('text') or ''}{nxt}"
    else:
        said = "尚未运行"
    p.add(p.text(f"已开启，{_every(every)}整理一次。"), p.text(said, "caption"),
          p.choice("every", "整理间隔", choices, str(every)),
          p.row(p.do("保存间隔", "/api/auto", {"enabled": True, "every": {"$input": "choices/every"}}, inputs=["choices"]),
                p.do("发送测试通知", "/api/auto/test-notify", {}),
                p.do("关闭", "/api/auto", {"enabled": False, "every": every}, quiet=True,
                     confirm="关闭后台自动整理：将不再定时整理和发送通知。")))


def _switches(w, p: kit.Page, st: dict) -> None:
    """已读, 往来邮件: one switch each, flipped by a button that says what it will do."""
    read = (st.get("flow") or {}).get("mark_read", True) is not False
    talk = bool((st.get("threads") or {}).get("on"))
    _section(p, "已读")
    p.add(p.row(p.text("分析过的邮件标为已读" + ("（已开启）" if read else "（已关闭）")),
                p.do("关闭" if read else "开启", "/api/flow", {"mark_read": not read})))
    _section(p, "往来邮件")
    p.add(p.row(p.text("提醒在等对方回复的邮件" + ("（已开启）" if talk else "（已关闭）")),
                p.do("关闭" if talk else "开启", "/api/threads", {"on": not talk})),
          p.text("会读「已发送」里最近一个月的邮件，只在本机分析。", "caption"))


def _power(w, p: kit.Page, st: dict) -> None:
    _section(p, "功耗")
    if (st.get("engine") or {}).get("via") == "hub" or st.get("hub_only"):
        p.add(p.text("由螺壳统一管理。"))
        return
    level = (st.get("power") or {}).get("level") or "full"
    level = level if level in {v for v, _, _ in POWER} else "full"
    p.add(p.row(*[p.text(f"{label}（当前）", "h5") if v == level else p.do(label, "/api/power", {"level": v}, quiet=True)
                  for v, label, _ in POWER]),
          p.text(next(t for v, _, t in POWER if v == level), "caption"))


def _profile(w, p: kit.Page, st: dict) -> None:
    _section(p, "分析")
    f, pr = st.get("fetch") or {}, st.get("profile") or {}
    p.add(p.text("同步范围：同步和整理只看这个范围内的邮件"),
          p.field("days", "最近几天（1–365）", str(f.get("days") or "")),
          p.field("limit", "每次最多几封（1–500）", str(f.get("limit") or "")),
          p.row(p.do("保存范围", "/api/profile", {"days": {"$input": "fields/days"}, "limit": {"$input": "fields/limit"}},
                     inputs=["fields"])),
          p.text("回复署名：写在回复草稿的末尾"),
          p.field("signature", "署名（例如：小周）", str(pr.get("signature") or "")),
          p.row(p.do("保存署名", "/api/profile", {"signature": {"$input": "fields/signature"}}, inputs=["fields"])))


def _memory(w, p: kit.Page, st: dict, src: str) -> None:
    """记忆: the counts (the whole of it is on the 记忆 page), 从邮件中了解你, and the user's own websites."""
    _section(p, "记忆")
    m = st.get("memory") or {}
    p.add(p.text(" · ".join(x for x in (f"关于你 {m.get('fact') or 0} 条", f"关注的话题 {m.get('interest') or 0} 个",
                                        f"不感兴趣 {m['dislike']} 个" if m.get("dislike") else None,
                                        f"{m['proposed']} 个问题想问你" if m.get("proposed") else None) if x), "caption"))
    mv = w.app.curator.memory_view(src)
    lv = mv.get("learning") or {}
    p.add(p.text("从邮件中了解你", "h5"))
    if not lv.get("enabled"):
        p.add(p.text("不用自己填写：根据你发出的邮件、收到的往来邮件和你的处理习惯，记下关于你的信息。", "caption"),
              p.row(p.do("开启", "/api/memory/learn", {"source": src, "on": True}, primary=True)))
    else:
        midway = lv.get("stage") == "sent" and (lv.get("sent_done") or 0) < (lv.get("sent_total") or 0)
        problem = "" if lv.get("running") else (lv.get("problem") or "")
        sent = f"（{lv.get('sent_done') or 0} / {lv.get('sent_total') or 0} 封）" if lv.get("stage") == "sent" else ""
        status = f"{STAGE.get(lv.get('stage'), '正在了解你')}{sent}" if lv.get("running") else \
            (f"了解你：已读你发出的邮件 {lv.get('sent_done') or 0} / {lv.get('sent_total') or 0} 封" if midway else
             f"已读完你发出的 {lv['sent_done']} 封邮件" if lv.get("sent_done") else "已开启")
        tail = "，已暂停" if not lv.get("on") else "" if problem else "，可以离开此页面" if lv.get("running") else \
            "，等待其他任务完成" if midway else "，下次空闲时继续" if lv.get("stage") else ""
        p.add(p.row(p.text(status + tail, "caption"),
                    p.do("暂停" if lv.get("on") else "继续", "/api/memory/learn", {"source": src, "on": not lv.get("on")},
                         quiet=True)))
        if problem:
            p.add(p.text(f"{problem}恢复后将自动继续。", "caption"))
    sites = mv.get("sites") or []
    p.add(p.text(f"你的网站和博客（{len(sites)}）" if sites else "你的网站和博客", "h5"),
          p.text("螺壳会读取这些网站上的文章，用来了解你关注的话题。只读取你填写的网址。", "caption"))
    for s in sites[:20]:
        if not p.room(8):
            break
        meta = " · ".join(x for x in (s.get("url") if s.get("title") else None,
                                      "最近一次无法打开" if s.get("problem") else f"{s.get('posts') or 0} 篇文章") if x)
        p.add(p.row(p.text(s.get("title") or s.get("url") or ""), p.text(meta, "caption"),
                    p.do("删除", "/api/memory/site", {"source": src, "op": "remove", "id": s["id"]}, quiet=True,
                         confirm=f"删除网站：将不再读取 {s.get('url')}。已经推测出的话题不受影响，可以在「记忆」里单独修改。")))
    p.add(p.field("site", "网站或博客的网址（例如：https://example.com）", ""),
          p.row(p.do("添加", "/api/memory/site", {"source": src, "op": "add", "url": {"$input": "fields/site"}},
                     inputs=["fields"])))


def _data(w, p: kit.Page, st: dict) -> None:
    _section(p, "本机数据")
    store = st.get("store") or {}
    p.add(p.text("账号、授权码和分析结果只保存在本机。"),
          p.text("注意：在此系统上，授权码以明文保存在本机文件中（仅你的账户可读）。" if store.get("plaintext")
                 else f"授权码保存在{store.get('label') or '钥匙串'}中，其他数据保存在本机螺壳的数据目录。", "caption"),
          p.checkbox("forget_memory", "同时删除邮件助手写进记忆的内容", False),
          p.text("不勾选，记忆留给其他螺壳工具用。勾选也只删邮件助手自己记下和推测的；你确认过、改过或自己写的会保留。", "caption"),
          p.row(p.do("全部清除", "/api/forget", {"memory": {"$input": "checks/forget_memory"}}, inputs=["checks"],
                     confirm=FORGET)))


def build(w, p: kit.Page, view: dict) -> None:
    st = w.app.state()
    has_mail = bool(w.app.mail_accounts())
    src = w.source(p.page)
    _mailboxes(w, p, st)
    _profile(w, p, st)
    _auto(w, p, st, has_mail)
    _switches(w, p, st)
    _mode(w, p, st, has_mail)
    _model(w, p, st)
    _power(w, p, st)
    _memory(w, p, st, src)
    _data(w, p, st)
