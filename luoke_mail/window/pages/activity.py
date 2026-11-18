"""处理记录 in the window: the browser page's 操作记录 (static/rules.js showLog, view Store.log_entries) -- every
step the mail assistant took on the mailbox, newest first, each undoable one with 撤销, a screen at a time."""
from __future__ import annotations

from .. import kit
from .common import SCREEN, mailbox_row, per, running, screen


def pager(p: kit.Page, view: dict, total: int, unit: str = "条", key: str = "at") -> None:
    """上一页 / 下一页 for a list that is not of mail (common.more counts 封)."""
    at, n = int(view.get(key) or 0), per(p, SCREEN)
    if total <= n:
        return
    p.add(p.row(p.go("上一页", {key: max(at - n, 0)}) if at > 0 else None,
                p.text(f"第 {at + 1}–{min(at + n, total)} {unit}，共 {total} {unit}", "caption"),
                p.go("下一页", {key: at + n}) if at + n < total else None))


def _entry(p: kit.Page, src: str, e: dict) -> str:
    """One step a line, its 撤销 on the right (设计规范 §18.2)."""
    mail = e.get("mail") or {}
    return p.item([p.text((e.get("detail") or "") + ("（已撤销）" if e.get("undone") else ""), "strong"),
                   p.text(" · ".join(x for x in ((e.get("ts") or "")[5:16], mail.get("from"), mail.get("subject")) if x),
                          "caption")],
                  [p.do("撤销", "/api/undo", {"source": src, "log_id": e["id"]}, icon="undo-2") if e.get("undoable") else None])


def build(w, p: kit.Page, view: dict) -> None:
    mailbox_row(w, p)
    running(w, p)
    if not w.app.mail_accounts():
        p.add(p.text("还没有连接邮箱。", "h4"),
              p.text("在「设置」里添加邮箱以后，螺壳对邮箱做过的每一步都会记在这里。", "caption"))
        return
    src = w.source(p.page)
    entries = w.app.store.log_entries(w.app.account(src))
    p.add(p.text("螺壳对邮箱做过的每一步，退订以外都能撤销。", "caption"))
    if not entries:
        p.add(p.text("暂无处理记录。", "caption"))
        return
    p.reserve = 6
    lines = []
    for e in screen(p, view, entries):
        if not p.room(8):
            break
        lines.append(_entry(p, src, e))
    p.add(p.list(lines))
    p.reserve = 0
    pager(p, view, len(entries))
