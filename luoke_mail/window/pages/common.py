"""What every window page shares: the mailbox switch, the job in progress, screens of a long list."""
from __future__ import annotations

from .. import kit

SCREEN = 10           # mail on one screen of a list: a card with its buttons is about 15 components (§4: 200)


def per(p: kit.Page, n: int) -> int:
    """How many on one screen: twice as many where a page takes 400 components (§4 bigPages)."""
    return n * 2 if p.can("bigPages") else n


def mailbox_row(w, p: kit.Page) -> None:
    """Which mailbox the page shows, when there are several: the chosen one as text, the others as buttons."""
    accts = w.app.mail_accounts()
    if len(accts) < 2:
        return
    now = w.source(p.page)
    p.add(p.row(*[p.text(a, "caption") if a == now else p.go(a, {"account": a}) for a in accts]))


def running(w, p: kit.Page) -> None:
    """A job in progress (同步, 清理, 读订阅…), as on every browser page: what it is doing, 停止; the page is read
    again every few seconds while it runs (§4 pageRefresh), or has 刷新 (kit.Page.refresh)."""
    job = w.app.running_job()
    if not job:
        return
    pub = w.app.public_job(job)
    done, total = pub.get("done") or 0, pub.get("total") or 0
    words = (pub.get("stage") or "正在处理") + (f"（{done}/{total}）" if total else "")
    row = p.row(p.icon("clock"), p.text(words, "caption"),
                p.do("停止", "/api/jobs/cancel", {"id": job["id"]}, quiet=True) if job.get("kind") != "auto" else None)
    p.add(row)
    p.refresh(3, row)


def screen(p: kit.Page, view: dict, rows: list, key: str = "at") -> list:
    """The rows on this screen of a list, and 上一页 / 下一页 after them (added by `more`)."""
    at = max(0, min(int(view.get(key) or 0), max(len(rows) - 1, 0)))
    return rows[at:at + per(p, SCREEN)]


def more(p: kit.Page, view: dict, total: int, key: str = "at") -> None:
    at, n = int(view.get(key) or 0), per(p, SCREEN)
    if total <= n:
        return
    p.add(p.row(p.go("上一页", {key: max(at - n, 0)}) if at > 0 else None,
                p.text(f"第 {at + 1}–{min(at + n, total)} 封，共 {total} 封", "caption"),
                p.go("下一页", {key: at + n}) if at + n < total else None))
