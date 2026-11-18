"""The window's pages (window.Window draws them). Each builder is fn(window, page, view): it reads the same view the
browser page reads (server.get_routes) and adds components to the page; buttons through page.do / page.go.

Pages are in the order of packaging/accessory.json `service.pages`: the four tabs (设计规范 §18.2: 今日、收件箱、精选、
清理), then the pages only a button goes to (protocol §1a `hidden`): 处理记录, with the icon the window draws at the
top right of the app's page; the old 设置 as `legacy-settings` (11-13: 螺壳's own 设置 is luoke-agent's, built from
data/settings), kept for adding and removing a mailbox until those are tools; 规则 from it; 订阅体检 and 清理旧邮件
(the whole of each) from 清理; and one piece of 精选 from its line."""
from __future__ import annotations

from . import activity, clean, digest, inbox, piece, rules, settings, subs, sweep, today

PAGES = [("today", "今日"), ("inbox", "收件箱"), ("digest", "精选"), ("clean", "清理"),
         ("legacy-settings", "邮箱和设置"), ("activity", "处理记录"), ("rules", "规则"), ("subs", "订阅体检"),
         ("sweep", "清理旧邮件"), ("piece", "精选内容")]
# hidden pages (§1a) -> the icon that opens them, or None: only a button on another page does
HIDDEN = {"legacy-settings": None, "activity": "history", "rules": None, "subs": None, "sweep": None, "piece": None}
BUILDERS = {"today": today.build, "inbox": inbox.build, "digest": digest.build, "clean": clean.build,
            "legacy-settings": settings.build, "activity": activity.build, "rules": rules.build, "subs": subs.build,
            "sweep": sweep.build, "piece": piece.build}


def manifest_pages() -> list:
    """`service.pages` as packaging/accessory.json has it (a test keeps the two the same)."""
    out = []
    for pid, title in PAGES:
        entry = {"id": pid, "title": title}
        if pid in HIDDEN:
            entry["hidden"] = True
            if HIDDEN[pid]:
                entry["icon"] = HIDDEN[pid]
        out.append(entry)
    return out
