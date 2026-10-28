"""Sync: new mail -> local store -> the model, one message at a time. Every message is read by the model.

A personal rule doesn't replace the model: the model is told the rule and normally follows it, but may
differ when the message clearly needs the user (a bill about to fall due, a security warning, someone
asking them directly) or looks suspicious -- the card then says so. Bulk mail is judged per message too
(the same bank sends statements and promotions); same source + same suggestion is grouped only on screen.
"""
from __future__ import annotations

from typing import Callable

from . import engines
from .store import Store


def rule_suggestion(item: dict, rule: dict) -> dict:
    """When the user has just made a rule (or changed a suggestion with "以后都这样"), it applies to the pending
    list at once -- their explicit decision. Mail that arrives later is read by the model with the rule as context."""
    from .store import RULE_KINDS
    from .triage import RULE_ACTIONS
    spam = rule["action"] == "spam"
    action = "ignore" if spam else rule["action"]      # junk: not a scam, just never wanted (moved to junk on the page)
    return {"action": action, "priority": "low" if action in ("archive", "ignore") else "medium",
            "category": item.get("category"), "summary": item.get("summary") or item.get("subject"),
            "suspicious": bool(item.get("suspicious")), "due_date": item.get("due_date"), "event": item.get("event"), "reply_points": item.get("reply_points"),
            "reason": f"规则：{RULE_KINDS.get(rule['kind'], rule['kind'])}「{rule['value']}」的邮件"
                      f"{RULE_ACTIONS.get(rule['action'], rule['action'])}。"}


def follows_rule(res: dict, rule: dict) -> bool:
    if rule["action"] == "spam":
        return res.get("action") == "ignore"
    return res.get("action") == rule["action"] and not res.get("suspicious")


def sync(store: Store, account: str, mails: list[dict], analyze: Callable[[dict, dict | None], dict] | None,
         progress: Callable[[str, int, int], None] = lambda *a: None, cancelled: Callable[[], bool] = lambda: False,
         link: engines.Link | None = None, older: bool = False) -> dict:
    """`analyze(item, rule)` returns a suggestion; `rule` is the user's rule for this sender, if any.
    `analyze=None` when the model service isn't running: everything waits (unanalyzed) for the next sync, as does
    the rest when 螺壳主控 says not now (background work paused). `link.name` is recorded as what answered.
    `decided` lists the items that got a suggestion now."""
    stats = {"fetched": len(mails), "new": 0, "by_rule": 0, "rule_differs": 0, "by_model": 0, "errors": 0, "waiting": 0}
    decided: list[int] = []
    for m in mails:
        stats["new"] += store.upsert_message(account, m)[1]

    todo = store.unanalyzed(account, older)                 # `older`: the unread mail from before the range
    for done, it in enumerate(todo, 1):
        progress(it["subject"], done, len(todo))
        if cancelled():
            break
        if analyze is None:
            stats["waiting"] += 1
            continue
        rule = store.match_rule(it)
        try:
            res = analyze(it, rule)
        except engines.Busy:
            stats["waiting"] += len(todo) - done + 1
            break
        except Exception as e:  # noqa: BLE001 - keep going, show the error on the card
            store.set_error(it["id"], str(e))
            stats["errors"] += 1
            continue
        if rule:
            follows = follows_rule(res, rule)
            store.set_suggestion(it["id"], res, f"{'rule' if follows else 'rule-differs'}:{rule['id']}", link and link.name)
            stats["by_rule" if follows else "rule_differs"] += 1
        else:
            store.set_suggestion(it["id"], res, "model", link and link.name)
            stats["by_model"] += 1
        decided.append(it["id"])
    return {**stats, "decided": decided}
