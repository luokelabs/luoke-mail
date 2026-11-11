"""清理 in the window: what the browser page's 清理 shows (static/clean.js, static/sweep.js) -- old mail in the inbox
and archived mail, grouped by source and by what is suggested for it, judged in the background.

What is ticked is kept in the page's view, not in checkboxes: the window's confirmation is written when the page is
drawn, and it has to say what will happen to how many messages, as the browser's dialog does (and `/api/old/apply`
takes the count the user confirmed). A checkbox's tick never reaches the page before the press, and a redraw (the
next screen, a pause) would drop it. So each source has 勾选 / 取消勾选 and each section 全选 / 全不选, the ticks
are the browser page's (suggested and not kept at an earlier clean-up, unless the user changed it), and they hold
across screens and sections, so 处理 carries out every ticked group, not only those on the screen."""
from __future__ import annotations

from typing import Dict, List, Optional

from ... import engines
from ...actions import human_size as human
from .. import kit
from .common import mailbox_row, running

SCOPES = [("inbox", "收件箱中的旧邮件"), ("archive", "已归档的邮件")]
SECTIONS = {
    "inbox": [("del", "建议删除"), ("todo", "待确认"), ("arch", "建议归档"), ("pending", "未分析")],
    "archive": [("del", "建议删除"), ("keep", "建议保留"), ("pending", "未分析")],
}
PER_SECTION = 6          # sources on one screen of a section: a source is 4 components, four sections fit in 200

MODE_TEXT = ("开启后，可以一次处理多封邮件：删除或移到垃圾邮件（邮箱一般保留 30 天，可以恢复）；在订阅体检和清理里，清掉一个"
             "发件人的历史邮件。归档、标为已读、加标签、存草稿和退订，不开也能用。螺壳不会发信、转发或永久删除邮件。")
NEEDS_MODE = "删除前需要先开启处理模式。"


# ---- shared with 订阅体检 (subs.py) ----------------------------------------------------------------------------
def can_write(w, src: str) -> bool:
    return src == "demo" or bool(w.app.cfg["mode"]["write"])


def need_mode(p: kit.Page) -> str:
    """The line a bulk deletion shows while 处理模式 is off, with the switch behind the browser page's explanation."""
    return p.row(p.text(NEEDS_MODE, "caption"),
                 p.do("开启处理模式", "/api/mode", {"write": True, "consent": True}, confirm=MODE_TEXT))


def finished(w, kinds: tuple, acct: str, view: dict, seen: str = "seen_job") -> Optional[dict]:
    """The last job of these kinds for this mailbox, once it is over and the user has not put it away: its result
    comes after the press that started it, so the page says it (and offers 撤销) when it is drawn next."""
    jobs = [j for j in list(w.app.jobs.values()) if j.get("kind") in kinds and j.get("account") == acct]
    last = jobs[-1] if jobs else None
    if not last or last["status"] == "running" or last["id"] == view.get(seen):
        return None
    return last


# ---- the groups --------------------------------------------------------------------------------------------
def _section(scope: str, g: dict) -> str:
    if g["kind"] == "pending":
        return "pending"
    if g["action"] == "trash" and g["suggested"]:
        return "del"
    if g["kind"] == "todo":
        return "todo"
    return "arch" if scope == "inbox" else "keep"


def _shown_kind(g: dict) -> str:
    return g.get("shown") or g["kind"]          # mail with a person judged a notice is listed under 和人的往来


def _kind_label(old: dict, k: str) -> str:
    return "未分析" if k == "pending" else "和人的往来" if k == "personal" else (old.get("kinds") or {}).get(k) or "其他"


def _count(rows: List[dict]) -> int:
    return sum(g["count"] for g in rows)


def _size(rows: List[dict]) -> int:
    return sum(g["size"] for g in rows)


def _tick(scope: str, src: str, key: str) -> str:
    return f"tick:{scope}:{src}:{key}"


def _picked(view: dict, scope: str, src: str, old: dict, g: dict) -> bool:
    t = view.get(_tick(scope, src, g["key"]))
    if t is not None:
        return bool(t)
    return bool(g["suggested"]) and g["key"] not in (old.get("keep") or [])


def _gmail_like(src: str, old: dict) -> bool:
    return src == "demo" or old.get("provider") == "gmail"


def _chosen(view: dict, scope: str, src: str, old: dict) -> dict:
    rows = [g for g in old["groups"] if _picked(view, scope, src, old, g)]
    trash = [g for g in rows if g["action"] == "trash"]
    kept = sum((g.get("digest") or {}).get("kept", 0) for g in trash)
    return {"rows": rows, "n": _count(rows), "archive": _count([g for g in rows if g["action"] == "archive"]),
            "trash": _count(trash) - kept, "kept": kept, "trash_bytes": _size(trash),
            "labelled": sum(g["count"] for g in trash if g.get("labels"))}


def _action_text(scope: str, c: dict) -> str:
    if scope == "archive":
        return f"删除（{c['n']} 封）" if c["n"] else "删除"
    return f"处理（{c['n']} 封）" if c["n"] else "处理"


def _confirm(scope: str, src: str, old: dict, c: dict) -> str:
    """The browser page's dialog (sweep.js apply), as one paragraph."""
    by: Dict[tuple, int] = {}
    for g in c["rows"]:
        k = (g["action"], _shown_kind(g))
        by[k] = by.get(k, 0) + g["count"]
    lines = [f"{'删除' if a == 'trash' else '归档'}：{_kind_label(old, k)} {n} 封" for (a, k), n in by.items()]
    head = "；".join(lines[:5]) + ("；等" if len(lines) > 5 else "") + "。"
    unjudged = sum(g["count"] for g in c["rows"] if g["kind"] == "pending")
    all_mail = "「所有邮件」" if _gmail_like(src, old) else "归档文件夹"
    parts = [head,
             f"其中 {unjudged} 封还没有分析，会直接归档。" if unjudged else "",
             f"归档的邮件将标为已读并移到{all_mail}。" if c["archive"] else "",
             (f"删除的邮件约 {human(c['trash_bytes'])}，" + ("30 天后由 Gmail 自动清空。" if _gmail_like(src, old)
                                                         else "由邮箱定期清空。")) if c["trash"] else "",
             f"其中 {c['kept']} 封含精选内容，将保留并加星标。" if c["kept"] else "",
             f"其中 {c['labelled']} 封带有你的标签，撤销时会一并恢复。" if c["labelled"] else "",
             "示例数据，不会更改邮箱。" if src == "demo" else "可在处理记录中撤销。"]
    return "".join(parts)


# ---- the parts of the page ---------------------------------------------------------------------------------
def _judge_bar(p: kit.Page, src: str, scope: str, old: dict) -> None:
    """Analysis in the background: how far it has got, 开始 / 继续 / 暂停 (sweep.js renderJudge)."""
    j = dict(old.get("judging") or {})
    problem = None if j.get("running") else j.get("problem")
    left = old["total"] - old["judged"]
    if not left and not problem:
        return
    if not j.get("on"):
        note = "。分析在后台进行，可以离开此页面。" if not old["judged"] else \
            "。当前一批分析完后暂停。" if j.get("running") else "。已暂停。"
        p.add(p.row(p.text(f"{left} 封未分析{note}", "caption"),
                    p.do("继续分析" if old["judged"] else "开始分析", "/api/old/judge",
                         {"source": src, "scope": scope, "on": True}, primary=True)))
        return
    count = f"正在分析 {old['judged']} / {old['total']}" if j.get("running") else f"已分析 {old['judged']} / {old['total']}"
    tail = "" if problem else "，可以离开此页面" if j.get("running") else "，等待其他任务完成"
    row = p.row(p.text(count + tail, "caption"),
                p.do("暂停", "/api/old/judge", {"source": src, "scope": scope, "on": False}, quiet=True))
    p.add(row)
    p.refresh(5, row)
    if problem:
        p.add(p.text(f"{problem}恢复后将自动继续。", "caption"))


def _reading(p: kit.Page, w, src: str, scope: str, old: dict, rows: List[dict]) -> None:
    """订阅精选 in 清理: newsletters read before they go, what is worth reading kept (sweep.js renderReading)."""
    rows = [g for g in rows if _shown_kind(g) == "newsletter"]       # the browser's note under 订阅内容
    if not rows:
        return
    d = {"queued": 0, "read": 0, "kept": 0}
    for g in rows:
        for k in d:
            d[k] += (g.get("digest") or {}).get(k, 0)
    if not d["queued"] and not d["read"]:
        n = _count(rows)
        factor = engines.POWER.get(w.app.cfg["power"]["level"], 0.0)          # 功耗 slows it
        minutes = max(1, round(n * 15 * (1 + factor) / 60))
        when = f"{round(minutes / 60)} 小时" if minutes >= 90 else f"{minutes} 分钟"
        p.add(p.row(p.text("删除之前，可以让本机模型先读一遍，把值得看的留下。", "caption"),
                    p.do("先读一遍", "/api/digest/read", {"source": src, "scope": scope, "keys": [g["key"] for g in rows]},
                         confirm=f"本机模型会读这 {n} 封邮件，把值得看的放进「精选」。阅读在后台进行，预计约 {when}，可以离开"
                                 "此页面。含精选内容的邮件在清理时会保留并加星标，其余照常处理。这些发件人之后的邮件会汇总到每周精选。")))
    elif d["queued"]:
        p.add(p.text(f"{'正在阅读' if old.get('reading_on', True) else '已暂停'}：已读 {d['read']} / {d['read'] + d['queued']} 封"
                     + (f"，{d['kept']} 封含精选内容" if d["kept"] else "") + "。可在「精选」中查看。", "caption"))
    else:
        p.add(p.text(f"已阅读 {d['read']} 封，其中 {d['kept']} 封含精选内容，处理时将保留并加星标。" if d["kept"]
                     else f"已阅读 {d['read']} 封，没有值得保留的内容。", "caption"))


def _source_line(old: dict, g: dict, section: str) -> str:
    who = g.get("name") or g.get("addr") or ""
    if g.get("labels"):
        who += "（" + "、".join(g["labels"]) + "）"
    if g.get("addr") and g.get("name") and g["name"] != g["addr"]:
        who += f" {g['addr']}"
    bits = [] if section == "pending" else [_kind_label(old, _shown_kind(g))]
    bits += [who, f"{g['count']} 封", f"{g['unread']} 未读" if g.get("unread") else human(g["size"]),
             (g.get("latest") or "")[:10]]
    if (g.get("digest") or {}).get("kept"):
        bits.append(f"含精选 {g['digest']['kept']}")
    line = " · ".join(b for b in bits if b)
    if g.get("subjects"):
        line += " — " + " / ".join(g["subjects"])
    return line


def _section_block(p: kit.Page, w, view: dict, src: str, scope: str, old: dict, sid: str, title: str) -> None:
    rows = [g for g in old["groups"] if _section(scope, g) == sid]
    if not rows:
        return
    order = list(dict.fromkeys(_shown_kind(g) for g in rows))       # the browser's kind blocks, in their order
    rows.sort(key=lambda g: order.index(_shown_kind(g)))
    all_on = all(_picked(view, scope, src, old, g) for g in rows)
    keys = {_tick(scope, src, g["key"]): not all_on for g in rows}
    if not p.room(6):
        raise kit.Full()
    p.add(p.row(p.text(f"{title}（{_count(rows)} 封，约 {human(_size(rows))}）", "h5"),
                p.go("全不选" if all_on else "全选", keys)))
    if sid == "todo":
        p.add(p.text("可能尚未处理完毕，例如未付的账单、未回复的问题。默认不勾选，勾选后将归档。", "caption"))
    _reading(p, w, src, scope, old, rows)
    at_key = f"at:{scope}:{sid}"
    at = max(0, min(int(view.get(at_key) or 0), max(len(rows) - 1, 0)))
    for g in rows[at:at + PER_SECTION]:
        if not p.room(4):
            raise kit.Full()
        on = _picked(view, scope, src, old, g)
        p.add(p.row(p.go("取消勾选" if on else "勾选", {_tick(scope, src, g["key"]): not on}, quiet=on),
                    p.text(_source_line(old, g, sid), limit=300)))
    if len(rows) > PER_SECTION and p.room(6):
        p.add(p.row(p.go("上一页", {at_key: max(at - PER_SECTION, 0)}) if at > 0 else None,
                    p.text(f"第 {at + 1}–{min(at + PER_SECTION, len(rows))} 个，共 {len(rows)} 个来源", "caption"),
                    p.go("下一页", {at_key: at + PER_SECTION}) if at + PER_SECTION < len(rows) else None))


def _actions(p: kit.Page, w, src: str, scope: str, old: dict, view: dict) -> None:
    """已选 N 封 and 处理 / 删除, with the browser dialog's words as the confirmation (sweep.js renderActions)."""
    c = _chosen(view, scope, src, old)
    detail = f"：归档 {c['archive']}，删除 {c['trash']}" if scope == "inbox" and c["n"] else \
        f"，约 {human(_size([g for g in c['rows'] if g['action'] == 'trash']))}" if c["n"] else ""
    needs = not can_write(w, src) and bool(c["n"] if scope == "archive" else c["trash"])
    if needs:
        p.add(p.text(f"已选 {c['n']} 封{detail}", "caption"), need_mode(p))
        return
    p.add(p.row(p.text(f"已选 {c['n']} 封{detail}", "caption"),
                p.do(_action_text(scope, c), "/api/old/apply",
                     {"source": src, "scope": scope, "keys": [g["key"] for g in c["rows"]], "count": c["n"]},
                     primary=True, confirm=_confirm(scope, src, old, c)) if c["n"] else None))


def _job_result(p: kit.Page, w, src: str, acct: str, view: dict) -> None:
    job = finished(w, ("clean", "old_scan"), acct, view)
    if not job:
        return
    r = job.get("result") or {}
    if job["status"] == "error":
        text = job.get("error") or "没有完成。"
    elif job["kind"] == "clean":
        text = r.get("message") or ("已停止。" if job["status"] == "cancelled" else "已处理。")
    else:
        return                                        # a reading that went through shows as the list itself
    undo = r.get("log_id") if job["kind"] == "clean" and r.get("ok", True) else None
    p.add(p.row(p.text(text, "caption"),
                p.do("撤销", "/api/undo", {"source": src, "log_id": undo}, quiet=True) if undo else None,
                p.go("知道了", {"seen_job": job["id"]})))


def build(w, p: kit.Page, view: dict) -> None:
    mailbox_row(w, p)
    running(w, p)
    if not w.app.mail_accounts():
        p.add(p.text("还没有连接邮箱。", "h4"),
              p.text("在「设置」里添加邮箱以后，这里会列出可以清理的旧邮件。", "caption"))
        return
    src = w.source(p.page)
    acct = w.app.account(src)
    scope = view.get("scope") if view.get("scope") in dict(SCOPES) else "inbox"
    p.add(p.row(*[p.text(label, "h5") if key == scope else p.go(label, {"scope": key}) for key, label in SCOPES]))
    old = w.app.old_view(acct, scope)
    gmail = _gmail_like(src, old)
    days = old.get("days") or w.app.cfg["fetch"]["days"]
    p.add(p.text((f"收件箱里超过 {days} 天的邮件。" if scope == "inbox" else
                  ("已归档的邮件。" if gmail else "归档文件夹里的邮件。")) + "加了星标或「收藏」「重要」这类标签的不会动。", "caption"))
    if src == "demo":
        p.add(p.text("示例数据，不会更改邮箱。", "caption"))
    _job_result(p, w, src, acct, view)
    scan = {"source": src, "scope": scope}
    if not old.get("scanned"):
        p.add(p.row(p.text("还没有读取这些邮件。", "caption"), p.do("读取邮件", "/api/old/scan", scan, primary=True)))
        return
    held = f"加了星标或「收藏」「重要」这类标签的 {old['flagged']} 封不在此列。" if old.get("flagged") else ""
    if old.get("stale"):
        p.add(p.row(p.text("收件箱里的旧邮件有了变化。", "caption"), p.do("重新读取", "/api/old/scan", scan, primary=True)))
    if not old["total"]:
        p.add(p.text("没有需要清理的邮件。" + held, "caption"),
              p.row(p.text(f"更新于 {old['at']}", "caption"), p.do("刷新", "/api/old/scan", scan, quiet=True)))
        return
    p.add(p.text(f"共 {old['total']} 封，约 {human(old['size'])}" + (f"，已分析 {old['judged']} 封" if old["judged"] else "")
                 + "。" + held, "caption"),
          p.row(p.text(f"更新于 {old['at']}", "caption"), p.do("刷新", "/api/old/scan", scan, quiet=True)))
    _judge_bar(p, src, scope, old)
    p.reserve = 8                                       # 已选 and 处理 come last, whatever the sections take
    try:
        for sid, title in SECTIONS[scope]:
            _section_block(p, w, view, src, scope, old, sid, title)
    except kit.Full:
        p.reserve = 0
        p.add(p.text("还有来源没有列出，处理上面勾选的以后会显示。", "caption"))
    p.reserve = 0
    _actions(p, w, src, scope, old, view)
