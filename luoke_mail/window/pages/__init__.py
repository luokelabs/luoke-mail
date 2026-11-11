"""The window's pages (window.Window draws them). Each builder is fn(window, page, view): it reads the same view the
browser page reads (server.get_routes) and adds components to the page; buttons through page.do / page.go.

Pages are in the order of the window's tabs (packaging/accessory.json `service.pages`)."""
from __future__ import annotations

from . import activity, clean, digest, inbox, rules, settings, subs, today

PAGES = [("today", "今日处理"), ("inbox", "收件箱"), ("clean", "清理"), ("digest", "精选"), ("subs", "订阅体检"),
         ("rules", "规则"), ("activity", "处理记录"), ("settings", "设置")]
BUILDERS = {"today": today.build, "inbox": inbox.build, "clean": clean.build, "digest": digest.build,
            "subs": subs.build, "rules": rules.build, "activity": activity.build, "settings": settings.build}

