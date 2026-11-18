"""One piece of 精选, a page a line of 精选 opens (protocol §1a hidden, `page/piece?id=<piece>`): its title, where it
is from, the sentence and why it was picked; then 在浏览器打开 with the site on the button (the user sees where it goes
before going, 设计规范 §18.1 先给明细), 在 Gmail 中打开, and 多来点这类 / 少来点这类.

Opened from a line on the user's tap (§4 rowOpen), so drawing it records that the piece was opened, once
(Curator.opened: an activity, kept in the memory)."""
from __future__ import annotations

from .. import kit
from .common import mailbox_row
from .digest import _day


def build(w, p: kit.Page, view: dict) -> None:
    mailbox_row(w, p)
    if not w.app.mail_accounts():
        p.add(p.text("还没有连接邮箱。", "h4"))
        return
    back = p.open("返回精选", page="digest")
    try:
        n = int(p.args.get("id") or "")
    except ValueError:
        p.add(p.text("从「精选」里点一条进来。", "caption"), p.row(back))
        return
    src = w.source(p.page)
    acct = w.app.account(src)
    it = w.app.store.digest_item(n)
    if not it or it["account"] != acct:
        p.add(p.text("这条内容不在了。", "caption"), p.row(back))
        return
    w.app.curator.opened({"source": src, "id": n})
    it = w.app.curator.public_item(acct, w.app.store.digest_item(n))
    one = {"source": src, "id": n}
    saved = it.get("feedback") == "useful"
    p.add(p.row(back))
    p.add(p.text(it.get("title") or "（无标题）", "h4"),
          p.text(" · ".join(x for x in (it.get("source"), _day(it.get("date") or "")) if x), "caption"))
    if it.get("summary"):
        p.add(p.text(it["summary"], "body", 1000))
    if it.get("pick_why") or it.get("why"):
        p.add(p.text(it.get("pick_why") or it.get("why"), "caption"))
    p.add(p.row(p.link(f"在浏览器打开（{it.get('host') or '网页'}）", it.get("url") or ""),
                p.link("在 Gmail 中打开", it.get("mail_url") or ""),
                p.do("不用多来了" if saved else "多来点这类", "/api/digest/feedback",
                     {**one, "value": None if saved else "useful"}, icon="thumbs-up"),
                p.do("少来点这类", "/api/digest/feedback", {**one, "value": "dismissed"}, icon="thumbs-down")))
