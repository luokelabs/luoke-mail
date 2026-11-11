"""今日处理 in the window: what the browser page's 今日处理 shows (static/review.js, view App.review.view), a screen
at a time -- each message sorted or handled on a day with its outcome, taken back (撤销), marked unread again, or
its suggestion changed (改为…) in place. The last 7 days can be switched to; a header filters to one group."""
from __future__ import annotations

from ... import triage
from .. import kit
from .common import mailbox_row, more, running, screen

RESULT = {
    "auto_archive": "已自动归档", "auto_trash": "已自动删除", "auto_read": "已自动标为已读", "auto_label": "已自动加标签",
    "auto_complete": "已自动归档", "kept": "已读，留在收件箱", "waiting": "等你处理", "suspicious": "可疑",
    "elsewhere": "你在邮箱中处理了", "unsorted": "未分析",
    "you_archive": "你已归档", "you_trash": "你已删除", "you_spam": "你已移到垃圾邮件", "you_complete": "你已完成并归档",
    "you_flag": "你已加星标", "you_read": "你已标为已读", "you_skip": "你已跳过", "you_skipped": "你已跳过", "you_done": "你已处理",
    "you_draft": "你已保存回复草稿",
}
# what each outcome is grouped under, in this order; the header counts the same groups, so the numbers add up
SECTIONS = [
    ("needs", "等你处理", "等你处理", lambda x: not x["older"] and x["result"] in ("waiting", "suspicious")),
    ("older", "以前的未读", "以前的未读", lambda x: x["older"]),
    ("kept", "已读，留在收件箱", "留在收件箱", lambda x: not x["older"] and x["result"] in ("kept", "unsorted")),
    ("auto", "已自动处理", "自动处理", lambda x: not x["older"] and x["result"].startswith("auto_")),
    ("you", "你处理的", "你处理的", lambda x: not x["older"] and (x["result"].startswith("you_") or x["result"] == "elsewhere")),
]
LATE = {"overdue": "已过期", "due": "快到期", "old": "很久以前收到"}


def _day_label(i: int, day: str) -> str:
    return "今天" if i == 0 else "昨天" if i == 1 else day[5:].replace("-", "/")


def _late(x: dict) -> str:
    if not x.get("late"):
        return ""
    if x["late"] == "overdue" and x.get("due_date"):
        return f"已过期（{x['due_date'][:10]}）"
    if x["late"] == "due" and x.get("due_date"):
        return f"快到期（{x['due_date'][:10]}）"
    if x["late"] == "old" and x.get("date"):
        return f"{x['date'][:10]} 收到"
    return LATE.get(x["late"], "")


def _row(p: kit.Page, src: str, day: str, x: dict, view: dict) -> str:
    what = RESULT.get(x["result"], "已处理") + (f"「{x['label']}」" if x.get("label") else "")    # never a code
    # what the card offered: suspicious mail goes to junk, not the trash (luoke-mail#13)
    suggested = "建议：移到垃圾邮件（可疑）" if x.get("suspicious") else \
        f"建议：{triage.ACTIONS[x['action']]}" if x.get("action") in triage.ACTIONS else ""
    lines = [p.text(x.get("subject") or "（无主题）", "h5"),
             p.row(p.icon("warning") if x["result"] == "suspicious" else None,
                   p.text(" · ".join(w for w in (what, _late(x)) if w), "caption"))]
    meta = " · ".join(w for w in (x.get("from"), suggested, x.get("summary")) if w)
    if meta:
        lines.append(p.text(meta, "caption"))
    one = {"source": src, "id": x["id"], "day": day}
    if x["state"] == "pending" and view.get("change") == x["id"] and view.get("change_from") == x.get("action"):
        others = [(a, label) for a, label in triage.ACTIONS.items() if a != x.get("action")]
        lines.append(p.choice(f"a{x['id']}", "改为", others, others[0][0]))
        lines.append(p.row(p.do("改好了", "/api/items/override",
                                {"source": src, "ids": [x["id"]], "action": {"$input": f"choices/a{x['id']}"}},
                                inputs=["choices"], primary=True),
                           p.go("取消", {"change": None, "change_from": None})))
        return p.card(*lines)
    acts = []
    if x["result"].startswith("auto_") and x.get("log_id"):
        acts.append(p.do("撤销", "/api/review/act", {**one, "op": "undo"}))
    if x["state"] == "pending" and x.get("read_by_us"):
        acts.append(p.do("标为未读", "/api/review/act", {**one, "op": "unread"}))
    if x["state"] == "pending":
        acts.append(p.go("改为…", {"change": x["id"], "change_from": x.get("action")}))
    if acts:
        lines.append(p.row(*acts))
    return p.card(*lines)


def build(w, p: kit.Page, view: dict) -> None:
    mailbox_row(w, p)
    running(w, p)
    if not w.app.mail_accounts():
        p.add(p.text("还没有连接邮箱。", "h4"),
              p.text("在「设置」里添加邮箱以后，这里会列出螺壳每天处理过的邮件。", "caption"))
        return
    src = w.source(p.page)
    v =w.app.review.view(src, view.get("day"))
    if v["day"] not in v["days"]:                    # a day chosen a week ago: back to today
        v = w.app.review.view(src)
    day = v["day"]
    if day == v["days"][0]:                          # looked at today's: the rail's badge clears (as the browser page)
        try:
            w.app.review.seen({"source": src})
        except ValueError:
            pass
    p.add(p.text("螺壳今天处理了哪些邮件。不对的改一下，它会记住。", "caption"))
    p.add(p.row(*[p.text(_day_label(i, d), "h5") if d == day else
                  p.go(_day_label(i, d), {"day": None if i == 0 else d, "only": None, "at": 0, "change": None})
                  for i, d in enumerate(v["days"])]))
    groups = [(key, title, short, [x for x in v["items"] if test(x)]) for key, title, short, test in SECTIONS]
    only = view.get("only")
    if only and not any(k == only and rows for k, _, _, rows in groups):
        only = None
    tiles = [p.text(f"全部 {len(v['items'])}", "caption") if not only else p.go(f"全部 {len(v['items'])}", {"only": None, "at": 0})]
    for key, _, short, rows in groups:
        if rows:
            label = f"{short} {len(rows)}"
            tiles.append(p.text(label, "caption") if only == key else p.go(label, {"only": key, "at": 0}))
    p.add(p.row(*tiles))
    if not v["items"]:
        p.add(p.text("这一天没有处理过的邮件。", "caption"))
        return
    flat = [(key, title, x, rows) for key, title, _, rows in groups if rows and (not only or only == key) for x in rows]
    p.reserve = 6
    shown = None
    for key, title, x, rows in screen(p, view, flat):
        if not p.room(18):
            break
        if key != shown:
            shown = key
            p.add(p.row(p.text(title, "h5"), p.text(str(len(rows)), "caption")))
            late = sum(1 for y in rows if y.get("late")) if key == "older" else 0
            if key == "older":
                p.add(p.text("以前一直没读的邮件，螺壳看过并标为已读。" + (f"其中 {late} 封还要你处理。" if late else ""), "caption"))
            if key == "needs" or late:
                p.add(p.text("请到「收件箱」处理。", "caption"))
        p.add(_row(p, src, day, x, view))
    p.reserve = 0
    more(p, view, len(flat))
