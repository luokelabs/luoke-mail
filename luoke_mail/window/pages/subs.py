"""订阅体检 in the window: what the browser page's 订阅体检 shows (static/subs.js) -- senders by how much they send and
how much of it is read, with a way out for each: unsubscribe, a rule for what comes next, 加入精选, 保留, and
clearing what is already in the mailbox.

The window opens only https pages and has no mail client to hand a message to: an unsubscribe page or link is a link
the agent opens (nothing is recorded then: the next check shows whether mail still comes), an unsubscribe by mail or
by reply says where to write. Suspicious senders are never offered an unsubscribe (it would confirm the address)."""
from __future__ import annotations

import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

from ...actions import ActionError, human_size as human
from ...mailbox import MailError
from .. import kit
from .clean import can_write, finished, need_mode
from .common import mailbox_row, per, running

SCREEN = 8              # senders on one screen: a card with its buttons is about 15 components (§4: 200)
DAYS = ["30", "60", "90", "180", "365"]
METHOD = {"one_click": "支持一键退订", "web": "通过网页退订", "mailto": "通过邮件退订",
          "body_link": "正文中有退订链接", "reply": "回复关键词退订"}
DONE = {"unsubscribed": "已退订", "keep": "已保留", "archive_rule": "以后都归档", "trash_rule": "以后都删除",
        "spam_rule": "以后都移到垃圾邮件", "junk_rule": "以后都移到垃圾邮件", "unsubscribe_opened": "已打开退订页面",
        "digest": "已加入精选"}
VERDICT = {"unsubscribe": "建议退订", "filter": "无法退订", "never": "可疑发件人", "keep": "常读", "important": "建议保留"}
RULE_STATES = ("archive_rule", "trash_rule", "spam_rule", "junk_rule")
LISTS = [("pending", "建议处理"), ("handled", "已处理"), ("regular", "建议保留")]

PREVIEW_FOR = 120       # seconds a 清理历史邮件 count is shown before the mailbox is asked again
_previews: Dict[Tuple[str, str], Tuple[float, dict]] = {}


def _quiet(s: dict) -> bool:
    return s.get("verdict") in ("keep", "important")      # read often, or worth keeping unread


def _label(s: dict) -> str:
    return s.get("name") or s.get("from_addr") or ""


def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return ""


def _by_mail(s: dict) -> Optional[str]:
    """How to unsubscribe when it takes a message the user sends (the window cannot open a mail client)."""
    if s.get("method") == "reply" and s.get("from_addr"):
        return f"退订方式：给 {s['from_addr']} 回复「{s.get('reply_keyword') or 'TD'}」。"
    if s.get("method") == "mailto" and s.get("unsub_mailto"):
        u = urllib.parse.urlparse(s["unsub_mailto"])
        subject = (urllib.parse.parse_qs(u.query).get("subject") or [""])[0]
        return f"退订方式：给 {urllib.parse.unquote(u.path)} 发一封邮件" + (f"，主题写「{subject}」。" if subject else "。")
    return None


def _web(s: dict) -> Optional[str]:
    """The unsubscribe page the sender gives (or the link in the mail's text), https only."""
    url = s.get("body_link") if s.get("method") == "body_link" else s.get("unsub_http")
    return url if str(url or "").startswith("https://") else None


def _unsubscribe(p: kit.Page, src: str, s: dict, primary: bool) -> List[str]:
    """The browser page's unsubscribe button, as far as the window can take it."""
    if s.get("suspicious") or s.get("verdict") == "never":
        return []
    m = s.get("method")
    if m == "one_click":
        return [p.do("一键退订", "/api/senders/act", {"source": src, "key": s["key"], "op": "unsubscribe"}, primary=primary)]
    if m in ("web", "body_link") and _web(s):
        label = "打开退订网页" if m == "web" else f"打开退订链接（{_host(_web(s))}）"
        return [b for b in [p.link(label, _web(s))] if b]
    return []


def _preview(w, src: str, s: dict) -> dict:
    """清理历史邮件: how many of this sender's messages there are (inbox and archive), as the browser page asks
    before its dialog. Asked once a short while, not at every drawing."""
    from ...server import post_routes            # noqa: PLC0415 -- server imports the window
    key = (src, s["key"])
    hit = _previews.get(key)
    if hit and time.time() - hit[0] < PREVIEW_FOR:
        return hit[1]
    try:
        out = post_routes(w.app, {"source": src, "key": s["key"], "op": "history_preview"})["/api/senders/act"]()
    except (MailError, ValueError, ActionError) as ex:
        return {"error": str(ex)}
    _previews[key] = (time.time(), out)
    return out


def _history(p: kit.Page, w, src: str, s: dict) -> List[str]:
    pv = _preview(w, src, s)
    if pv.get("error"):
        return [p.text(pv["error"], "caption"), p.go("收起", {"history": None})]
    n = pv.get("count") or 0
    if not n:
        return [p.row(p.text(f"邮箱中没有「{_label(s)}」的邮件。", "caption"), p.go("收起", {"history": None}))]
    when = f"邮箱会在 {pv['purge_days']} 天后自动清空。" if pv.get("purge_days") else "邮箱会定期清空。"
    say = f"将「{_label(s)}」的 {n} 封邮件（约 {human(pv.get('bytes') or 0)}，含已归档邮件）移到「已删除」。"
    out = [p.text(say, "caption")]
    if not can_write(w, src):
        return out + [need_mode(p), p.go("收起", {"history": None})]
    return out + [p.row(p.do(f"删除（{n} 封）", "/api/senders/act", {"source": src, "key": s["key"], "op": "trash_history"},
                             primary=True, confirm=f"{say}{when}在此之前可以在「处理记录」中撤销。"),
                        p.go("取消", {"history": None}))]


def _block(p: kit.Page, data: dict, s: dict) -> List[str]:
    """去邮箱里拉黑: the mailbox's own steps, and the address to block (the browser page's dialog)."""
    b = ((data.get("provider") or {}).get("block")) or {}
    out = [p.text(b.get("title") or "在邮箱中拉黑", "h5"),
           p.text("设好后，邮箱会直接拦下这个发件人之后的邮件。", "caption")]
    out += [p.text(f"{n}. {step}", "caption") for n, step in enumerate(b.get("steps") or [], 1)]
    addr = s.get("from_addr") or ""
    if addr:
        out.append(p.text(f"发件人地址：{addr}" + (f"，域名：{addr.split('@')[1]}" if "@" in addr else ""), "caption"))
    link = (b.get("link") or {})
    out.append(p.row(p.link(link.get("label") or "", link.get("url") or "") if link else None,
                     p.go("收起", {"block": None})))
    return out


def _card(p: kit.Page, w, src: str, data: dict, s: dict, view: dict) -> str:
    st = (s.get("decision") or {}).get("status")
    act = lambda label, op, **kw: p.do(label, "/api/senders/act", {"source": src, "key": s["key"], "op": op}, **kw)  # noqa: E731
    read = round(s["read"] / s["count"] * 100) if s.get("count") else 0
    tag = (DONE.get(st, "已处理") + (" · " + s["decision"]["at"][5:10].replace("-", "/") if s["decision"].get("at") else "")) \
        if st else VERDICT.get(s.get("verdict"), "")
    meta = [tag, s.get("from_addr"), f"{s.get('count', 0)} 封，读过 {read}%", f"约 {human(s.get('bytes') or 0)}",
            f"最近 {s['last_date'][:10]}" if s.get("last_date") else None,
            METHOD.get(s.get("method")) if s.get("method") else "无退订方式"]
    lines = [p.text(_label(s), "h5"), p.text(" · ".join(x for x in meta if x), "caption")]
    if not st and s.get("verdict") in ("never", "important") and s.get("why"):
        lines.append(p.text(s["why"], "caption"))
    if not st and s.get("method") == "body_link" and _web(s):
        lines.append(p.text("链接来自邮件正文，打开后请确认网址属于该发件人。", "caption"))
    if st == "unsubscribe_opened" and not s.get("still_sending"):
        lines.append(p.text("如果之后仍收到此发件人的邮件，下次体检时会提示你。", "caption"))
    if s.get("still_sending"):
        lines.append(p.text("退订后仍在发信。可以将之后的邮件删除，或在邮箱中拉黑。", "caption"))
    by_mail = _by_mail(s) if not s.get("suspicious") and s.get("verdict") != "never" and \
        (not st or st == "unsubscribe_opened") else None
    if by_mail:
        lines.append(p.text(by_mail, "caption"))

    more = view.get("more") == s["key"]
    buttons: List[Optional[str]] = []
    if not st:
        if s.get("verdict") == "never":
            buttons.append(act("以后都移到垃圾邮件", "spam_rule", primary=True))
        else:
            buttons += _unsubscribe(p, src, s, s.get("verdict") == "unsubscribe")
            buttons += [act("以后都删除", "trash_rule", primary=s.get("verdict") == "filter"), act("加入精选", "digest")]
            if not _quiet(s):
                buttons.append(act("保留", "keep"))
    else:
        if s.get("still_sending"):
            buttons.append(act("以后都删除", "trash_rule", primary=True))
        if st == "unsubscribe_opened" and not s.get("suspicious") and _web(s):
            buttons.append(p.link("再次打开退订网页", _web(s)))
        buttons.append(act("删除规则" if st in RULE_STATES else "移出精选" if st == "digest" else "移回建议处理", "reset",
                           quiet=True))
    buttons.append(p.go("收起" if more else "更多…", {"more": None if more else s["key"], "history": None, "block": None}))
    lines.append(p.row(*buttons))
    if more:
        lines.append(p.text("最近的邮件主题：" + " / ".join(x for x in (s.get("subjects") or []) if x), "caption"))
        lines.append(p.row(p.go("清理历史邮件", {"history": s["key"], "block": None}) if st != "keep" else None,
                           p.go("去邮箱里拉黑", {"block": s["key"], "history": None})))
        if view.get("history") == s["key"] and st != "keep":
            lines += _history(p, w, src, s)
        if view.get("block") == s["key"]:
            lines += _block(p, data, s)
    return p.card(*lines)


def build(w, p: kit.Page, view: dict) -> None:
    mailbox_row(w, p)
    running(w, p)
    if not w.app.mail_accounts():
        p.add(p.text("还没有连接邮箱。", "h4"),
              p.text("在「设置」里添加邮箱以后，可以在这里查看哪些订阅可以退订。", "caption"))
        return
    src = w.source(p.page)
    acct = w.app.account(src)
    data = w.app.subscriptions_view(acct)
    days = str(data.get("days") or 60)
    options = [(d, f"最近 {d} 天") for d in sorted(set(DAYS) | {days}, key=int)]
    p.add(p.row(p.choice("days", "体检范围", options, days if data.get("senders") is not None else "60"),
                p.do("开始体检" if data.get("senders") is None else "重新体检", "/api/subscriptions",
                     {"source": src, "days": {"$input": "choices/days"}}, inputs=["choices"], primary=True)))
    if src == "demo":
        p.add(p.text("示例数据，不会更改邮箱。", "caption"))
    job = finished(w, ("subscriptions",), acct, view)
    if job and job["status"] == "error":
        p.add(p.row(p.text(job.get("error") or "体检没有完成。", "caption"), p.go("知道了", {"seen_job": job["id"]})))
    rows = data.get("senders")
    if rows is None:
        p.add(p.text("还没体检过。", "h5"), p.text("设好天数，点「开始体检」。", "caption"))
        return
    pending = [s for s in rows if not s.get("decision") and not _quiet(s)]
    regular = [s for s in rows if not s.get("decision") and _quiet(s)]
    handled = [s for s in rows if s.get("decision")]
    size = sum(s.get("bytes") or 0 for s in pending)
    p.add(p.text((f"{data['at']} · " if data.get("at") else "") + f"最近 {data.get('days')} 天共 {data.get('scanned', 0)} 封邮件，"
                 f"来自 {len(rows)} 个发件人。" + (f"建议处理 {len(pending)} 个，约占 {human(size)}。" if pending else "")
                 + (f" {data['model_note']}" if data.get("model_note") else ""), "caption"))
    lists = {"pending": pending, "handled": handled, "regular": regular}
    pick = view.get("list") if view.get("list") in lists else "pending"
    p.add(p.row(*[p.text(f"{label}（{len(lists[k])}）", "h5") if k == pick else
                  p.go(f"{label}（{len(lists[k])}）", {"list": k, "at": 0, "more": None}) for k, label in LISTS]))
    shown = lists[pick]
    if not shown:
        p.add(p.text({"pending": "没有建议处理的发件人。", "handled": "还没有处理过的发件人。",
                      "regular": "没有建议保留的发件人。"}[pick], "caption"))
        return
    at = max(0, min(int(view.get("at") or 0), len(shown) - 1))
    p.reserve = 6
    n = per(p, SCREEN)
    for s in shown[at:at + n]:
        if not p.room(40 if view.get("more") == s["key"] else 18):
            break
        p.add(_card(p, w, src, data, s, view))
    p.reserve = 0
    if len(shown) > n:
        p.add(p.row(p.go("上一页", {"at": max(at - n, 0), "more": None}) if at > 0 else None,
                    p.text(f"第 {at + 1}–{min(at + n, len(shown))} 个，共 {len(shown)} 个发件人", "caption"),
                    p.go("下一页", {"at": at + n, "more": None}) if at + n < len(shown) else None))
