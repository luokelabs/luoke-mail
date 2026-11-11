"""精选 in the window: what the browser page's 精选 shows (Curator.view), a screen at a time.

Three shelves as on the browser page -- 精选 (the last seven days, then each earlier month), 收藏, 已归档 -- and the
精选来源 as a fourth list. 收藏 / 没兴趣 go to the memory (/api/digest/feedback); 归档 only puts a piece aside.
Opening a piece is recorded (/api/digest/open) on the user's tap; its links (https only) open through the window.
What a press just took off the list is said at the top with 撤销, as the browser card says it in place."""
from __future__ import annotations

import time

from .. import kit
from .common import mailbox_row, per, running

SCREEN = 8            # pieces on one screen: a card with its buttons is up to 16 components (§4: 200)
SHELVES = [("picks", "精选"), ("saved", "收藏"), ("archived", "已归档"), ("sources", "精选来源")]
FEW_TOPICS = 8


def _day(date: str) -> str:
    d = (date or "")[:10]
    try:
        return f"{int(d[5:7])}/{int(d[8:10])}"
    except ValueError:
        return ""


def _month(m: str) -> str:
    try:
        return f"{m[:4]} 年 {int(m[5:7])} 月"
    except ValueError:
        return m


def _taste(p: kit.Page, v: dict) -> None:
    """关注的话题 / 不感兴趣: what the picks go by, changed on the 记忆 page."""
    interests = [n.get("text") or "" for n in v.get("interests") or []]
    dislikes = [n.get("text") or "" for n in v.get("dislikes") or []]
    if interests:
        more = f" 等 {len(interests)} 个" if len(interests) > FEW_TOPICS else ""
        p.add(p.text("关注的话题：" + "、".join(interests[:FEW_TOPICS]) + more, "caption"))
    if dislikes:
        p.add(p.text("不感兴趣：" + "、".join(dislikes[:FEW_TOPICS]), "caption"))
    note = "按你的阅读和反馈归纳。" if interests else "暂时按热度挑选，读得多了会按你的口味挑。"
    where = "在「记忆」中修改。" if interests or dislikes else \
        ("在「设置」里添加你的网站或博客。" if not (v.get("profile") or {}).get("sites") else "在「记忆」中添加。")
    p.add(p.text(note + where, "caption"))


def _reading(p: kit.Page, src: str, v: dict) -> None:
    """Reading progress, with 暂停 / 继续 (as the browser page's bar)."""
    r = v.get("reading") or {}
    if not r.get("queued"):
        return
    problem = "" if r.get("running") else (r.get("problem") or "")
    done = (r.get("read") or 0) + (r.get("failed") or 0)
    total = (r.get("queued") or 0) + done
    tail = ("，读完当前这封后暂停" if r.get("running") else "，已暂停") if not r.get("on") else \
        ("" if problem else ("，可以离开此页面" if r.get("running") else "，等待其他任务完成"))
    p.add(p.row(p.icon("clock"), p.text(f"{'正在阅读' if r.get('on') else '已读'} {done} / {total} 封{tail}", "caption"),
                p.do("暂停" if r.get("on") else "继续", "/api/digest/reading", {"source": src, "on": not r.get("on")},
                     quiet=True)))
    if problem:
        p.add(p.text(f"{problem}恢复后将自动继续。", "caption"))


def _just_done(w, p: kit.Page, src: str, acct: str) -> None:
    """What the last press on this page took off the list or opened, with 撤销 / its links -- the line the browser
    card leaves in its place. Only for the press the page's note is about (a few seconds either side)."""
    note = w.notes.get(p.page)
    if not note or not note.get("at"):
        return
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(note["at"] - 5))
    store = w.app.store
    try:
        mem = w.app.memory_for(acct)
        said = mem.records(topic="digest-feedback", since=since, limit=1)
        opened = mem.records(topic="digest-open", since=since, limit=1)
    except Exception:  # noqa: BLE001 -- the memory unavailable: nothing more to say than the note
        said, opened = [], []
    for rec in said:
        it = store.digest_item(int(rec.get("ref") or 0))
        if it and it["account"] == acct and it.get("feedback") == "dismissed":
            p.add(p.row(p.text(f"已标记没兴趣：{it['title']}", "caption"),
                        p.do("撤销", "/api/digest/feedback", {"source": src, "id": it["id"], "value": None}, quiet=True)))
    for it in store.digest_archived(acct, limit=1):
        if (it.get("archived_at") or "") >= since:
            p.add(p.row(p.text(f"已归档：{it['title']}", "caption"),
                        p.do("撤销", "/api/digest/archive", {"source": src, "id": it["id"], "on": False}, quiet=True)))
    for rec in opened:
        it = store.digest_item(int(rec.get("ref") or 0))
        if it and it["account"] == acct:
            pub = w.app.curator.public_item(acct, it)
            p.add(p.row(p.text(f"已打开：{pub['title']}", "caption"), *_links(p, pub)))


def _links(p: kit.Page, it: dict) -> list:
    return [p.link(f"查看原文（{it.get('host') or '网页'}）", it.get("url") or ""),
            p.link("在 Gmail 中打开", it.get("mail_url") or "")]


def _has_link(it: dict) -> bool:
    return any(str(it.get(k) or "").startswith("https://") for k in ("url", "mail_url"))


def _card(p: kit.Page, src: str, it: dict, shelf: str) -> str:
    meta = " · ".join(x for x in (it.get("source"), _day(it.get("date") or ""),
                                  f"另有 {it['also']} 处提到" if it.get("also") else None) if x)
    tags = [t for t, on in (("推荐", (it.get("score") or 0) >= 3),
                            ("已收藏", it.get("feedback") == "useful" and shelf != "saved")) if on]
    lines = [p.text(meta, "caption") if meta else None,
             p.text((it.get("title") or "（无标题）") + ("（" + "，".join(tags) + "）" if tags else ""), "h5")]
    if it.get("summary"):
        lines.append(p.text(it["summary"], "body", 600))
    if it.get("pick_why") or it.get("why"):
        lines.append(p.text(it.get("pick_why") or it.get("why"), "caption"))
    one = {"source": src, "id": it["id"]}
    saved = it.get("feedback") == "useful"
    acts = []
    if _has_link(it):
        acts += _links(p, it) if it.get("opened") else [p.do("打开", "/api/digest/open", one, primary=True)]
    acts.append(p.do("取消收藏" if saved else "收藏", "/api/digest/feedback", {**one, "value": None if saved else "useful"},
                     quiet=True))
    if shelf == "archived":
        acts.append(p.do("移回精选", "/api/digest/archive", {**one, "on": False}, quiet=True))
    if shelf == "picks":
        acts += [p.do("归档", "/api/digest/archive", {**one, "on": True}, quiet=True),
                 p.do("没兴趣", "/api/digest/feedback", {**one, "value": "dismissed"}, quiet=True)]
    lines.append(p.row(*acts))
    return p.card(*lines)


def _rows(v: dict, shelf: str) -> list:
    """The pieces on this shelf, in order, each with the heading of the part it is in."""
    if shelf == "saved":
        return [("", "", it) for it in v.get("saved") or []]
    if shelf == "archived":
        return [("", "", it) for it in v.get("archived") or []]
    editing = (v.get("editing") or {}).get("running")
    wait = "正在挑选，暂按评分显示。" if editing else "尚未挑选，暂按评分显示。"
    out = [("最近 7 天", wait if v.get("week_pending") else "", it) for it in v.get("week") or []]
    for m in v.get("months") or []:
        out += [(f"以往精选 · {_month(m.get('month') or '')}", wait if m.get("pending") else "", it)
                for it in m.get("items") or []]
    return out


def _more(p: kit.Page, at: int, total: int) -> None:
    n = per(p, SCREEN)
    if total <= n:
        return
    p.add(p.row(p.go("上一页", {"at": max(at - n, 0)}) if at > 0 else None,
                p.text(f"第 {at + 1}–{min(at + n, total)} 条，共 {total} 条", "caption"),
                p.go("下一页", {"at": at + n}) if at + n < total else None))


def _sources(p: kit.Page, src: str, v: dict, at: int) -> None:
    sources = v.get("sources") or []
    if not sources:
        p.add(p.text("还没有精选来源。", "caption"),
              p.text("在「订阅体检」里把发件人加入精选，或在「清理」的「订阅内容」一栏选择「先读一遍」。", "caption"))
        return
    step = per(p, SCREEN * 2)
    for s in sources[at:at + step]:
        if not p.room(6):
            break
        name = s.get("name") or s.get("key") or ""
        key = str(s.get("key") or "")
        addr = key.split(":", 1)[1] if key.startswith(("list:", "from:")) else key
        p.add(p.row(p.text(name), p.text(addr, "caption") if addr != name else None,
                    p.do("移出", "/api/digest/source", {"source": src, "key": key, "on": False}, quiet=True)))
    p.reserve = 0
    total = len(sources)
    if total > step:
        p.add(p.row(p.go("上一页", {"at": max(at - step, 0)}) if at > 0 else None,
                    p.text(f"第 {at + 1}–{min(at + step, total)} 个，共 {total} 个", "caption"),
                    p.go("下一页", {"at": at + step}) if at + step < total else None))


def build(w, p: kit.Page, view: dict) -> None:
    mailbox_row(w, p)
    running(w, p)
    if not w.app.mail_accounts():
        p.add(p.text("还没有连接邮箱。", "h4"),
              p.text("在「设置」里添加邮箱以后，这里会列出订阅邮件中值得一读的内容。", "caption"))
        return
    src = w.source(p.page)
    acct = w.app.account(src)
    v = w.app.curator.view(acct)
    if src == "demo":
        p.add(p.text("示例数据，不会更改邮箱。", "caption"))
    _just_done(w, p, src, acct)
    _taste(p, v)
    _reading(p, src, v)
    shelf = view.get("shelf") if view.get("shelf") in dict(SHELVES) else "picks"
    counts = {"saved": len(v.get("saved") or []), "archived": len(v.get("archived") or []),
              "sources": len(v.get("sources") or [])}
    p.add(p.row(*[p.text(label + (f"（{counts[k]}）" if k in counts else ""), "h5") if k == shelf
                  else p.go(label + (f"（{counts[k]}）" if k in counts else ""), {"shelf": k, "at": 0})
                  for k, label in SHELVES]))
    at = max(0, int(view.get("at") or 0))
    p.reserve = 6
    if shelf == "sources":
        return _sources(p, src, v, at)
    rows = _rows(v, shelf)
    n = per(p, SCREEN)
    at = min(at, max(len(rows) - 1, 0)) // n * n
    if shelf == "saved":
        p.add(p.text("收藏的内容会一直保留在这里，不随时间移出。", "caption"))
        if not rows:
            p.add(p.text("还没有收藏的内容。", "caption"))
    elif shelf == "archived":
        p.add(p.text("已经看过、移出列表的内容。", "caption"))
        if not rows:
            p.add(p.text("没有已归档的内容。", "caption"))
    elif not v.get("sources") and not rows:
        p.add(p.text("还没有精选来源。", "caption"),
              p.text("在「订阅体检」里把发件人加入精选，或在「清理」的「订阅内容」一栏选择「先读一遍」。", "caption"))
    elif at == 0 and not v.get("week"):
        p.add(p.text("最近 7 天", "h5"),
              p.text("最近 7 天没有值得一看的内容。" if v.get("week_total") else "最近 7 天没有新的订阅邮件。", "caption"))
    part = None
    for head, wait, it in rows[at:at + n]:
        if not p.room(18):
            break
        if head and head != part:
            part = head
            p.add(p.text(head, "h5"), p.text(wait, "caption") if wait else None)
        p.add(_card(p, src, it, shelf))
    p.reserve = 0
    _more(p, at, len(rows))
