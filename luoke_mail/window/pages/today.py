"""今日 in the window (设计规范 §18.2): what the mail assistant did for the user today on top, a line a message with
its 撤销; then the few it could not decide on (at most three, §18.1), each with its ways to go as one group of
choices, the suggestion picked already, and one button for them; last, one small line for the mail that waits for a
reply, which goes to the inbox. No group headings with counts of what waits (§18.1).

Drawn from the browser page's 今日处理 (App.review.view). Earlier days are in 处理记录."""
from __future__ import annotations

from .. import kit
from .common import mailbox_row, more, running, screen

RESULT = {
    "auto_archive": "已自动归档", "auto_trash": "已自动删除", "auto_read": "已自动标为已读", "auto_label": "已自动加标签",
    "auto_complete": "已自动归档", "kept": "已读，留在收件箱",
}
DECIDE_AT_MOST = 3          # what the user decides on a day (§18.1)
STAY = ("skip", "留在收件箱")
# the ways a mail can go, by what it was sorted as: the first is the suggestion (picked already)
WAYS = {
    "suspicious": [("spam", "移到垃圾邮件"), STAY],
    "schedule": [("calendar", "加入日程"), ("archive", "归档"), STAY],
    "follow_up": [("flag", "加星标"), ("archive", "归档"), STAY],
    "archive": [("archive", "归档"), STAY],
    "ignore": [("trash", "删除"), ("archive", "归档"), STAY],
}
ICONS = {"spam": "shield-alert", "skip": "circle-minus", "calendar": "calendar-plus", "archive": "archive",
         "flag": "star", "trash": "trash-2"}


def _done(x: dict) -> bool:
    """Done for the user today: by a habit or a rule, or sorted, marked read and left where it was."""
    return not x["older"] and (x["result"].startswith("auto_") or x["result"] == "kept")


def _undo(p: kit.Page, src: str, day: str, x: dict):
    """Taking it back: what a habit did is undone; mail marked read goes back to unread."""
    one = {"source": src, "id": x["id"], "day": day}
    if x["result"].startswith("auto_") and x.get("log_id"):
        return p.do("撤销", "/api/review/act", {**one, "op": "undo"}, icon="undo-2")
    if x["state"] == "pending" and x.get("read_by_us"):
        return p.do("撤销", "/api/review/act", {**one, "op": "unread"}, icon="undo-2", tip="标为未读")
    return None


def _done_line(p: kit.Page, src: str, day: str, x: dict) -> str:
    what = RESULT.get(x["result"], "已处理") + (f"「{x['label']}」" if x.get("label") else "")
    return p.item([p.text(x.get("subject") or "（无主题）", "strong"),
                   p.text(" · ".join(w for w in (what, x.get("from")) if w), "caption")],
                  [_undo(p, src, day, x)])


def _ways(x: dict) -> list:
    if x.get("suspicious"):
        return WAYS["suspicious"]
    return WAYS.get(x.get("action") or "", [])


def _to_decide(items: list) -> list:
    """What waits for the user's word today, and the older mail still waiting: suspicious first, then what is late.
    Mail that waits for a reply is not decided here: it is answered in the inbox."""
    rows = [x for x in items if x["state"] == "pending" and x["result"] in ("waiting", "suspicious")
            and (x["result"] == "suspicious" or x.get("action") != "reply") and _ways(x)]
    late = {"overdue": 0, "due": 1, "old": 2}
    return sorted(rows, key=lambda x: (x["result"] != "suspicious", late.get(x.get("late") or "", 3)))


def _decide(w, p: kit.Page, src: str, rows: list) -> None:
    shown = rows[:DECIDE_AT_MOST]
    lines, picks = [], {}
    for x in shown:
        ways = _ways(x)
        about = " · ".join(v for v in (x.get("from"), _late(x), x.get("summary")) if v)
        key = f"a{x['id']}"
        lines.append(p.item([p.text(x.get("subject") or "（无主题）", "strong"), p.text(about, "caption") if about else None],
                            [p.choice(key, "怎么处理", ways, ways[0][0], icons=ICONS)]))
        picks[str(x["id"])] = {"$input": f"choices/{key}"}
    rest = len(rows) - len(shown)
    p.add(p.column(p.text("要你定的", "h5"), p.list(lines),
                   p.row(p.do("按选择处理", "decide", {"source": src, "picks": picks}, inputs=["choices"], primary=True),
                         p.text(f"另有 {rest} 封在收件箱里。", "caption") if rest else None,
                         p.open("去收件箱", {"box": w.app.account(src), "list": "needs"}, page="inbox") if rest else None)))


def _late(x: dict) -> str:
    if x.get("late") == "overdue":
        return f"已过期（{x['due_date'][:10]}）" if x.get("due_date") else "已过期"
    if x.get("late") == "due":
        return f"快到期（{x['due_date'][:10]}）" if x.get("due_date") else "快到期"
    return ""


def _replies(w, p: kit.Page, src: str) -> None:
    """One small line for the mail that waits for a reply, all of this mailbox's: it goes to the inbox."""
    acct = w.app.account(src)
    try:
        n = sum(1 for it in w.app.agent.waiting(acct)["needs"] if it.get("action") == "reply")
    except Exception:  # noqa: BLE001 -- the line is a nicety
        return
    if n:
        p.add(p.row(p.text(f"还有 {n} 封等你回复。", "caption"),
                    p.open("去收件箱", {"box": acct, "list": "reply"}, page="inbox")))


def build(w, p: kit.Page, view: dict) -> None:
    mailbox_row(w, p)
    running(w, p)
    if not w.app.mail_accounts():
        p.add(p.text("还没有连接邮箱。", "h4"),
              p.text("在「设置」里添加邮箱以后，这里会列出螺壳每天替你处理的邮件。", "caption"))
        return
    src = w.source(p.page)
    v = w.app.review.view(src)
    day = v["day"]
    try:
        w.app.review.seen({"source": src})          # looked at today's: the rail's badge clears (as the browser page)
    except ValueError:
        pass
    p.sections = True
    done = [x for x in v["items"] if _done(x)]
    older = [x for x in v["items"] if x["older"] and x["result"] not in ("waiting", "suspicious")]
    p.reserve = 30                                    # 要你定的 and the line for replies come after
    if done or older:
        lines = []
        for x in screen(p, view, done):
            if not p.room(10):
                break
            lines.append(_done_line(p, src, day, x))
        p.add(p.column(p.text(f"今天替你处理了 {len(done)} 封" if done else "今天替你处理的", "h5"),
                       p.list(lines) if lines else None,
                       p.text(f"以前一直没读的 {len(older)} 封，螺壳看过，标为已读。", "caption") if older else None))
        more(p, view, len(done))
    p.reserve = 0
    decide = _to_decide(v["items"])
    if decide:
        _decide(w, p, src, decide)
    if not done and not older and not decide:
        p.add(p.text("今天还没有新邮件要处理。", "caption"))
    _replies(w, p, src)
