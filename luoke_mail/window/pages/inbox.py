"""收件箱 in the window: what the browser page's inbox shows (App.list_items), a screen at a time.

The lists: 需要你处理 (reply, schedule, star, and suspicious mail), 可以直接处理 (archive, delete), and what is due
soon on top. One mail opened shows its text; 改为… shows the choices in place.

A card's 在邮件助手里看 opens this page at that mail (`page/inbox?id=<item>`, protocol §4 pageArgs); on a core that
opens pages with args, 查看原文 and 返回列表 do the same, so the page shows what its args say. A card about several
opens it at a mailbox's list (`page/inbox?box=<address>&list=needs|suspicious`, luoke-mail#81): the page switches to
them once, and the user's own choices on the page stand while it stays open with those args."""
from __future__ import annotations

from ... import triage
from .. import kit
from .common import SCREEN, mailbox_row, more, running, screen

NEEDS = ("reply", "schedule", "follow_up")
LISTS = [("needs", "需要你处理"), ("direct", "可以直接处理"), ("suspicious", "判为可疑"), ("all", "全部")]


def _who(it: dict) -> str:
    return it.get("from_name") or it.get("from_addr") or ""


def _when(it: dict) -> str:
    return (it.get("date") or "")[5:16]


def _ops(p: kit.Page, src: str, it: dict, write: bool) -> list:
    """The buttons the browser page's card has for this suggestion (static/cards.js)."""
    one = {"source": src, "ids": [it["id"]]}
    if it.get("suspicious") or it.get("junk"):
        return [p.do("移到垃圾邮件", "/api/items/act", {**one, "op": "spam"})]
    a = it.get("action")
    if a == "reply":
        return [p.do("写草稿", "/api/items/draft", {"source": src, "id": it["id"]}, primary=True)]
    if a == "schedule":
        if it.get("past_event") or it.get("in_calendar"):
            return [p.do("归档", "/api/items/act", {**one, "op": "archive"}, primary=True)]
        events = it.get("events") or [{}]
        return [p.do("加入日程" if len(events) < 2 else f"加入日程：{(ev.get('date') or '')[5:]}", "calendar",
                     {"source": src, "id": it["id"], "index": n if len(events) > 1 else None}, primary=n == 0)
                for n, ev in enumerate(events[:3])] + [p.do("归档", "/api/items/act", {**one, "op": "archive"})]
    if a == "follow_up":
        return ([] if it.get("flagged") else [p.do("加星标", "/api/items/act", {**one, "op": "flag"}, primary=True)]) + \
            [p.do("完成并归档", "/api/items/act", {**one, "op": "complete"})]
    if a == "archive":
        return [p.do("归档", "/api/items/act", {**one, "op": "archive"}, primary=True)]
    if a == "ignore":
        return [p.do("删除", "/api/items/act", {**one, "op": "trash"}, primary=True)]
    return []


def _look(p: kit.Page, it: dict) -> str:
    return p.open("查看原文", {"id": it["id"]}) if p.can("pageArgs") else p.go("查看原文", {"open": it["id"]})


def _card(w, p: kit.Page, src: str, it: dict, view: dict, write: bool) -> str:
    tag = "可疑：请勿点击链接或转账" if it.get("suspicious") else \
        (triage.ACTIONS.get(it.get("action")) or "未分析") + ("（高优先级）" if it.get("priority") == "high" else "")
    lines = [p.text(it.get("subject") or "（无主题）", "h5"),
             p.text(" · ".join(x for x in (_who(it), _when(it), tag) if x), "caption")]
    if it.get("summary"):
        lines.append(p.text(it["summary"], "caption"))
    if it.get("overdue"):
        lines.append(p.text(f"截止 {it.get('due_date')}，已过", "caption"))
    elif it.get("due_date"):
        lines.append(p.text(f"截止 {it['due_date']}", "caption"))
    if it.get("past_event"):
        lines.append(p.text(f"日程已过（{it['past_event']}）", "caption"))
    elif it.get("in_calendar"):
        lines.append(p.text("已在日程中", "caption"))
    if it.get("draft_text") and view.get("draft") == it["id"]:
        lines.append(p.field(f"d{it['id']}", "草稿", it["draft_text"]))
        lines.append(p.row(p.do("存进草稿箱", "/api/items/draft/save",
                                {"source": src, "id": it["id"], "text": {"$input": f"fields/d{it['id']}"}},
                                inputs=["fields"], primary=True),
                           p.go("收起", {"draft": None})))
    elif it.get("draft_text"):
        lines.append(p.go("看草稿", {"draft": it["id"]}))
    if view.get("change") == it["id"]:
        others = [(a, label) for a, label in triage.ACTIONS.items() if a != it.get("action")]
        lines.append(p.choice(f"a{it['id']}", "改为", others, others[0][0]))
        rule = [("", "只改这一封")] + [(k, f"以后{label}的邮件都这样处理") for k, label in
                                       (("sender", "这个发件人"), ("domain", "这个域名"), ("list", "这个邮件列表"))
                                       if it.get({"sender": "from_addr", "domain": "from_domain", "list": "list_id"}[k])]
        lines.append(p.choice(f"r{it['id']}", "规则", rule, ""))
        lines.append(p.row(p.do("改好了", "/api/items/override",
                                {"source": src, "ids": [it["id"]], "action": {"$input": f"choices/a{it['id']}"},
                                 "remember": {"$input": f"choices/r{it['id']}"}}, inputs=["choices"], primary=True),
                           p.go("取消", {"change": None})))
    else:
        lines.append(p.row(*_ops(p, src, it, write),
                           _look(p, it),
                           p.go("改为…", {"change": it["id"]}),
                           p.do("跳过", "/api/items/act", {"source": src, "ids": [it["id"]], "op": "skip"}, quiet=True)))
    return p.card(*lines)


def _opened(w, p: kit.Page, src: str, it: dict, view: dict, write: bool) -> None:
    back = p.open("返回列表") if p.args.get("id") else p.go("返回列表", {"open": None})
    p.add(p.row(back, p.link("在 Gmail 中打开", it.get("open_url") or "")))
    p.add(p.text(it.get("subject") or "（无主题）", "h4"))
    p.add(p.text(" · ".join(x for x in (_who(it), it.get("from_addr") if it.get("from_name") else "", it.get("date")) if x),
                 "caption"))
    p.reserve = 20
    p.add(_card(w, p, src, {**it, "summary": it.get("summary")}, view, write))
    for t in p.long_text(it.get("body") or ""):
        if not p.room(2):
            p.add(p.text("（后面还有，在邮箱里看全文。）", "caption"))
            break
        p.add(t)


def _asked(w, p: kit.Page):
    """The mail the page was opened at (args `id`): {"id", "account"}, or None."""
    try:
        n = int(p.args.get("id") or "")
    except ValueError:
        return None
    got = w.app.store.get(n)
    return {"id": n, "account": (got or {}).get("account")}


def _take_list(w, p: kit.Page, view: dict) -> dict:
    """Opened at a mailbox's list (args `box`, `list`): switch to them the first time these args are read; read
    again with the same args (a press on the page, a refresh), the page shows what the user chose since."""
    args = {k: p.args.get(k) for k in ("box", "list") if p.args.get(k)}
    if not args or w.views.get("_inbox_args") == args:
        if not args:
            w.views.pop("_inbox_args", None)
        return view
    w.views["_inbox_args"] = args
    if args.get("box") in w.app.mail_accounts():
        w.views.setdefault("*", {})["account"] = args["box"]
    if args.get("list") in dict(LISTS):
        w.views.setdefault(p.page, {}).update({"list": args["list"], "at": 0, "open": None, "change": None})
    return w.view(p.page)


def build(w, p: kit.Page, view: dict) -> None:
    view = _take_list(w, p, view)
    mailbox_row(w, p)
    running(w, p)
    if not w.app.mail_accounts():
        p.add(p.text("还没有连接邮箱。", "h4"),
              p.text("在「设置」里添加邮箱以后，这里会列出要你处理的邮件。", "caption"))
        return
    src = w.source(p.page)
    asked = _asked(w, p)
    if asked:                                        # opened at one mail: the mailbox it is in
        src = asked["account"] if asked["account"] in w.app.mail_accounts() else src
    data = w.app.list_items(src)
    write = data.get("write")
    items = [e["item"] for e in data["entries"] if e["type"] == "item"] + \
        [m for e in data["entries"] if e["type"] == "group" for m in e["items"]]
    want = asked["id"] if asked else view.get("open")
    opened = next((it for it in items + data["due"] + data["archived"] if it["id"] == want), None)
    if opened:
        return _opened(w, p, src, opened, view, write)
    if asked:                                        # handled since the card was made, or no longer in the inbox
        p.add(p.row(p.text("这封邮件已经处理过了，或者不在收件箱里。", "caption"), p.open("返回列表")))
    c = data["counts"]
    tally = "、".join(f"{label} {c[a]}" for a, label in triage.ACTIONS.items() if c.get(a))
    p.add(p.row(p.text(tally or "收件箱里没有待处理的邮件", "caption"),
                p.do("同步新邮件", "/api/sync", {"source": src}, primary=True)))
    note = w.app.model_note() if hasattr(w.app, "model_note") else None
    if note:
        p.add(p.text(note.get("text") if isinstance(note, dict) else note, "caption"))
    if c.get("unanalyzed"):
        p.add(p.text(f"{c['unanalyzed']} 封还没分析", "caption"))
    p.reserve = 6
    if data["due"] and not view.get("at"):
        p.add(p.text("快到期", "h5"))
        for it in data["due"][:3]:
            p.add(_card(w, p, src, it, view, write))
    pick = view.get("list") or "needs"
    p.add(p.row(*[p.text(label, "h5") if key == pick else p.go(label, {"list": key, "at": 0}) for key, label in LISTS]))
    due = {it["id"] for it in data["due"]}
    rows = [it for it in items if it["id"] not in due and (
        pick == "all" or (pick == "needs" and (it.get("suspicious") or it.get("action") in NEEDS))
        or (pick == "suspicious" and it.get("suspicious"))
        or (pick == "direct" and not it.get("suspicious") and it.get("action") in ("archive", "ignore")))]
    rows.sort(key=lambda it: it.get("date") or "", reverse=True)
    if not rows:
        p.add(p.text("这里没有邮件。", "caption"))
    for it in screen(p, view, rows):
        if not p.room(16):
            break
        p.add(_card(w, p, src, it, view, write))
    p.reserve = 0
    more(p, view, len(rows))
    if data["archived"] and pick == "needs":
        p.add(p.text(f"你归档的邮件里还有 {len(data['archived'])} 封要处理", "caption"))
