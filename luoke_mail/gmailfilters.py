"""Local rules -> a Gmail filter file (mailFilters.xml) the user imports in Gmail's settings.

A local rule only acts when luoke-mail syncs, and only after the user approves the suggestion. A
Gmail filter runs on Google's side for every new message, even when this tool is closed. Importing a
file needs no OAuth app, no Google review and gives nobody else a token: the user picks the file in
Gmail (设置 → 过滤器和屏蔽的地址 → 导入过滤器) and Gmail lists the filters before creating them.

Gmail filters cannot send mail to spam, so "移到垃圾邮件" becomes "删除它" (moved to the trash).
Importing the same file twice creates duplicate filters, so the store remembers what was exported.
"""
from __future__ import annotations

import time
from xml.sax.saxutils import quoteattr

from .store import RULE_KINDS

# rule action -> Gmail filter actions (the property names Gmail itself uses in exported files)
FILTER_ACTIONS = {
    "archive": (("shouldArchive", "true"), ("shouldMarkAsRead", "true")),
    "ignore": (("shouldMarkAsRead", "true"), ("shouldTrash", "true")),
    "spam": (("shouldTrash", "true"),),
    "follow_up": (("shouldStar", "true"),),
}
FILTER_LABELS = {"archive": "跳过收件箱并标为已读", "ignore": "删除（移到垃圾箱）", "spam": "删除（移到垃圾箱）",
                 "follow_up": "加星标"}
NOT_EXPORTABLE = {"reply": "需要你亲自回复", "schedule": "需要你确认日程"}


def criteria(rule: dict) -> tuple[str, str]:
    """Gmail search criteria for a rule. `from:example.com` matches the domain and its subdomains,
    like the local domain rule; `list:` matches the List-Id header."""
    if rule["kind"] == "list":
        return "hasTheWord", f"list:({rule['value']})"
    return "from", rule["value"]


def describe(rule: dict) -> str:
    return f"{RULE_KINDS.get(rule['kind'], rule['kind'])}「{rule['value']}」→ {FILTER_LABELS.get(rule['action'], '无法导出')}"


def split(rules: list[dict]) -> tuple[list[dict], list[dict]]:
    """(exportable rules, [{'rule', 'reason'}] for the ones a filter can't express)."""
    ok, skipped = [], []
    for r in rules:
        if r["action"] in FILTER_ACTIONS:
            ok.append(r)
        else:
            skipped.append({"rule": r, "reason": NOT_EXPORTABLE.get(r["action"], "过滤器不支持此操作")})
    return ok, skipped


def build_xml(rules: list[dict], author_email: str, at: float | None = None) -> str:
    at = time.time() if at is None else at
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
    base = int(at)
    out = ["<?xml version='1.0' encoding='UTF-8'?>",
           "<feed xmlns='http://www.w3.org/2005/Atom' xmlns:apps='http://schemas.google.com/apps/2006'>",
           "  <title>Mail Filters</title>",
           f"  <id>tag:mail.google.com,2008:filters:{base}</id>",
           f"  <updated>{stamp}</updated>",
           f"  <author><name>luoke-mail</name><email>{_text(author_email)}</email></author>"]
    for r in rules:
        name, value = criteria(r)
        props = [(name, value), *FILTER_ACTIONS[r["action"]]]
        out += ["  <entry>",
                "    <category term='filter'></category>",
                "    <title>Mail Filter</title>",
                f"    <id>tag:mail.google.com,2008:filter:{base}{r['id']}</id>",
                f"    <updated>{stamp}</updated>",
                "    <content></content>",
                *(f"    <apps:property name='{k}' value={quoteattr(v)}/>" for k, v in props),
                "  </entry>"]
    out.append("</feed>")
    return "\n".join(out) + "\n"


def filename(at: float | None = None) -> str:
    return time.strftime("luoke-gmail-filters-%Y%m%d-%H%M.xml", time.localtime(at))


def _text(s: str) -> str:
    return quoteattr(s or "")[1:-1]
