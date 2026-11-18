"""清理 in the window (设计规范 §18.2, 订阅体检 folded in): two groups -- 「可以退订」 (senders the subscription check
suggests unsubscribing from) and 「可以归档」 (old mail in the inbox, by source, judged as fine to archive). Each lists
what it is about, sender by sender, with that sender's own buttons (退订, 留着别再问), then one button that does the
whole group, asked first (§4 confirm). Nothing about when it was last tidied; with nothing to clear, one sentence.

The whole of each -- every sender of the check, deleting, archived mail, reading newsletters first -- is on the
pages a button below goes to: 订阅体检 (subs.py) and 清理旧邮件 (sweep.py)."""
from __future__ import annotations

from typing import List

from ...actions import human_size as human
from .. import kit
from .common import mailbox_row, running
from .subs import _host, _web
from .sweep import _job_result

SHOWN = 8               # senders listed in a group; the rest are on the page a button goes to


def _unsubscribable(data: dict) -> List[dict]:
    """What the check suggests unsubscribing from and the user has not decided on: never a suspicious sender (an
    unsubscribe would tell it the address is read)."""
    return [s for s in data.get("senders") or [] if not s.get("decision") and s.get("verdict") == "unsubscribe"
            and not s.get("suspicious")]


def _unsubscribe_button(p: kit.Page, src: str, s: dict):
    """A sender's 退订: done here when the sender takes a one-click unsubscribe, its page (https) otherwise."""
    if s.get("method") == "one_click":
        return p.do("退订", "/api/senders/act", {"source": src, "key": s["key"], "op": "unsubscribe"}, icon="mail-x")
    url = _web(s)
    return p.link(f"退订（{_host(url)}）" if s.get("method") == "body_link" else "退订", url or "") if url else None


def _unsubscribe_group(w, p: kit.Page, src: str) -> bool:
    data = w.app.subscriptions_view(w.app.account(src))
    if data.get("senders") is None:
        p.add(p.column(p.text("可以退订", "h5"),
                       p.row(p.text("还没检查过你的订阅。", "caption"),
                             p.do("检查订阅", "/api/subscriptions", {"source": src, "days": "60"}, quiet=True))))
        return True
    rows = _unsubscribable(data)
    if not rows:
        return False
    lines = []
    for s in rows[:SHOWN]:
        read = round(s["read"] / s["count"] * 100) if s.get("count") else 0
        how = "" if s.get("method") == "one_click" or _web(s) else "要发邮件退订，在订阅体检里看怎么做"
        lines.append(p.item([p.text(s.get("name") or s.get("from_addr") or "", "strong"),
                             p.text(" · ".join(x for x in (f"{s.get('count', 0)} 封，读过 {read}%", how) if x), "caption")],
                            [_unsubscribe_button(p, src, s),
                             p.do("留着别再问", "/api/senders/act", {"source": src, "key": s["key"], "op": "keep"}, icon="bell-off")]))
    one_click = [s for s in rows if s.get("method") == "one_click"]
    names = "、".join((s.get("name") or s.get("from_addr") or "") for s in one_click[:5]) + ("等" if len(one_click) > 5 else "")
    whole = p.do(f"全部退订（{len(one_click)} 个）", "unsubscribe_all", {"source": src, "keys": [s["key"] for s in one_click]},
                 primary=True, sure=f"退订这 {len(one_click)} 个发件人？",
                 confirm=f"{names}。退订以后，它们不再给你发这类邮件；退订没法撤销，要再订得去对方那里。") \
        if one_click else None
    p.add(p.column(p.text("可以退订", "h5"), p.list(lines),
                   p.text(f"还有 {len(rows) - SHOWN} 个，在订阅体检里。", "caption") if len(rows) > SHOWN else None,
                   p.row(whole) if whole else None))
    return True


def _archivable(old: dict) -> List[dict]:
    """Old mail in the inbox the model judged fine to archive, by source, not kept at an earlier clean-up and not
    「留着别再问」."""
    keep = set(old.get("keep") or [])
    return [g for g in old.get("groups") or [] if g["kind"] != "pending" and g["action"] == "archive"
            and g["suggested"] and g["key"] not in keep]


def _archive_group(w, p: kit.Page, src: str) -> bool:
    acct = w.app.account(src)
    old = w.app.old_view(acct, "inbox")
    scan = {"source": src, "scope": "inbox"}
    if not old.get("scanned"):
        p.add(p.column(p.text("可以归档", "h5"),
                       p.row(p.text("还没看过收件箱里的旧邮件。", "caption"),
                             p.do("看一遍", "/api/old/scan", scan, quiet=True))))
        return True
    rows = _archivable(old)
    j = old.get("judging") or {}
    left = old["total"] - old["judged"]
    progress = None
    if left and j.get("on"):
        progress = p.row(p.icon("clock"), p.text(f"还在看其余 {left} 封。", "caption"))
        p.refresh(5, progress)
    elif left:
        progress = p.row(p.text(f"还有 {left} 封没看。", "caption"),
                         p.do("接着看", "/api/old/judge", {**scan, "on": True}, quiet=True))
    if not rows:
        if progress:
            p.add(p.column(p.text("可以归档", "h5"), progress))
            return True
        return False
    lines = []
    for g in rows[:SHOWN]:
        who = g.get("name") or g.get("addr") or ""
        lines.append(p.item([p.text(who, "strong"),
                             p.text(" · ".join(x for x in (f"{g['count']} 封", (g.get("latest") or "")[:10]) if x), "caption")],
                            [p.do("归档", "/api/old/apply", {**scan, "keys": [g["key"]], "count": g["count"], "only": True},
                                  icon="archive"),
                             p.do("留着别再问", "/api/old/keep", {**scan, "key": g["key"]}, icon="bell-off")]))
    n = sum(g["count"] for g in rows)
    gmail = src == "demo" or old.get("provider") == "gmail"
    where = "「所有邮件」" if gmail else "归档文件夹"
    whole = p.do(f"全部归档（{n} 封）", "/api/old/apply", {**scan, "keys": [g["key"] for g in rows], "count": n, "only": True},
                 primary=True, sure=f"把这 {n} 封旧邮件归档？",
                 confirm=f"来自 {len(rows)} 个来源，约 {human(sum(g['size'] for g in rows))}。归档的邮件标为已读、移到{where}，"
                         "不会删除；可以在处理记录里撤销。")
    p.add(p.column(p.text("可以归档", "h5"), p.list(lines),
                   p.text(f"还有 {len(rows) - SHOWN} 个来源，在清理旧邮件里。", "caption") if len(rows) > SHOWN else None,
                   progress, p.row(whole)))
    return True


def build(w, p: kit.Page, view: dict) -> None:
    mailbox_row(w, p)
    running(w, p)
    if not w.app.mail_accounts():
        p.add(p.text("还没有连接邮箱。", "h4"),
              p.text("在「设置」里添加邮箱以后，这里会列出可以退订的订阅和可以归档的旧邮件。", "caption"))
        return
    src = w.source(p.page)
    p.sections = True
    if src == "demo":
        p.add(p.text("示例数据，不会更改邮箱。", "caption"))
    _job_result(p, w, src, w.app.account(src), view)
    p.reserve = 4
    shown = _unsubscribe_group(w, p, src)
    shown = _archive_group(w, p, src) or shown
    p.reserve = 0
    if not shown:
        p.add(p.text("现在没有要清理的。", "caption"))
    p.add(p.row(p.open("订阅体检", page="subs"), p.open("清理旧邮件", page="sweep")))
