"""The raw data and the actions 螺壳's window builds the mail assistant's pages from (top-level ADR 0002: the pages
are luoke-agent's; the mail assistant gives what there is and what can be done). The shapes are docs/agent-data.md.

Each `data/<name>` resource is read-only JSON: no A2UI, no sentences put together here. What the model wrote (a
summary, what the sender wants) is given as it is under a key ending in `_text`. Text that came from mail has its
credentials masked (memory.mask), as everywhere it leaves this app. `account=all` merges the mailboxes.

The actions are tools; each that changes something needs the user's tap (protocol §5 `ai.luoke/user-action`)."""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Callable, Dict, List, Optional

from . import config, desktop, memory, providers, samples, triage
from .agent_mcp import ToolError
from .mailbox import MailError, parse_message, real_mid
from .actions import ActionError
from .store import Store

LIST_MOST = 200
BODY_MOST = 8000
SENDERS_MOST = 5
MAILS_MOST = 20                 # the mail of one step, listed (「今日」's handled rows open to it)
# 今日's mail to decide on (设计规范 §18.2): the ways it can go, by what it was sorted as; the first is the suggestion
WAYS = {"suspicious": ["spam", "skip"], "schedule": ["calendar", "archive", "skip"],
        "follow_up": ["flag", "archive", "skip"], "archive": ["archive", "skip"], "ignore": ["trash", "archive", "skip"]}
ACT_OPS = ("archive", "trash", "spam", "flag", "read", "unread", "skip", "complete")


# ---- shared ---------------------------------------------------------------------------------------------------
def accounts_of(app, args: dict) -> List[str]:
    """The mailboxes asked about: one (`account`), all of them (`all`), or the first."""
    want = str(args.get("account") or "").strip().lower()
    accts = app.mail_accounts()
    if want == "all":
        return accts
    if want == "demo":
        return ["demo"]
    if not want:
        return accts[:1]
    if want not in accts:
        raise ToolError("invalid", "未连接该邮箱。")
    return [want]


def _who(it: dict) -> dict:
    return {"name": memory.mask(it.get("from_name")) or None, "address": it.get("from_addr") or None}


def _open_url(app, acct: str, it: dict) -> Optional[str]:
    """The mail in Gmail (the account it is in), when it is Gmail; else None."""
    mid = it.get("message_id") or ""
    if acct == "demo" or app.provider(acct) != "gmail" or not real_mid(mid):
        return None
    return providers.gmail_message_url(app.address(acct), mid, app.store.gmail_id(acct, mid))


# ---- data/accounts ----------------------------------------------------------------------------------------------
def accounts(app, args: dict) -> dict:
    out = []
    for m in app.cfg["accounts"]:
        acct = m["address"].lower()
        status, _ = app.store.cache_get(acct, "mail_status")
        _, synced = app.store.cache_get(acct, "synced_ok")
        if not app._has_secret(m["address"]):
            state, error, code = "needs_auth", None, "missing_secret"
        elif status and not status.get("ok"):
            state, error = ("needs_auth" if status.get("auth") else "error"), status.get("error")
            code = status.get("code") or ("login_refused" if status.get("auth") else "mail_error")
        else:
            state, error, code = "ok", None, None
        out.append({"id": acct, "address": m["address"], "name": m.get("name") or None, "provider": m.get("provider"),
                    "own": bool(m.get("own")), "last_sync": synced, "status": state, "error": error,
                    "error_code": code})
    job = app.running_job()
    pub = app.public_job(job) if job else None
    return {"accounts": out, "demo": not app.mail_accounts() or app.store.has_items("demo"), "model": _model(app),
            "job": {"id": job["id"], "kind": job.get("kind"), "account": job.get("account"), "stage": pub.get("stage"),
                    "done": pub.get("done") or 0, "total": pub.get("total") or 0} if job else None}


def _model(app) -> Optional[dict]:
    """Why mail cannot be sorted now, or None when it can: no model chosen (`none`), or 螺壳 not ready (its status,
    as App.model_note: `absent`, `error`, `downloading`, `needs_download`, `paused`, `unsupported`)."""
    if not app.has_model():
        return {"status": "none", "progress": None, "text": None}
    try:
        n = app.model_note()
    except Exception:  # noqa: BLE001 -- 螺壳 asked just now and did not answer: said as such
        return {"status": "error", "progress": None, "text": None}
    return {"status": n["status"], "progress": n.get("progress"), "text": n.get("text")} if n else None


# ---- data/today -------------------------------------------------------------------------------------------------
def _by(app, acct: str, e: dict, plans: set) -> str:
    """Who took a step: a habit, a rule, 螺壳's own mailbox, marking read after sorting, a plan on a card, the user."""
    if e["action"] == "seen":
        return "read"
    if e["action"] == "auto":
        how = (app.store.cache_get(acct, "auto_logs")[0] or {}).get(str(e["id"])) or {}
        return "own" if how.get("own") else "habit" if how.get("note_id") else "rule"
    return "plan" if e["id"] in plans else "user"


def _op(app, acct: str, e: dict) -> str:
    if e["action"] == "seen":
        return "read"
    if e["action"] == "auto":
        how = (app.store.cache_get(acct, "auto_logs")[0] or {}).get(str(e["id"])) or {}
        return how.get("op") or "archive"
    return e["action"]


def _touched(app, acct: str, e: dict) -> tuple:
    """The senders (at most SENDERS_MOST) and the categories of the mail a step touched, and the mail itself: newest
    first, at most MAILS_MOST, with how many more. Each opens with data/mail (kept here after it is moved)."""
    senders: Dict[str, dict] = {}
    cats: List[str] = []
    mails = []
    for i in dict.fromkeys(e["item_ids"]):           # each mail once
        it = app.store.get(i)
        if not it:
            continue
        key = (it.get("from_addr") or "").lower()
        if key and key not in senders and len(senders) < SENDERS_MOST:
            senders[key] = _who(it)
        if it.get("category") and it["category"] not in cats:
            cats.append(it["category"])
        mails.append({"id": it["id"], "account": acct, "from": _who(it), "subject": memory.mask(it.get("subject")),
                      "date": it.get("date")})
    mails.sort(key=lambda m: m["date"] or "", reverse=True)
    return list(senders.values()), cats, mails[:MAILS_MOST], max(0, len(mails) - MAILS_MOST)


def _step(app, acct: str, e: dict, plans: set) -> dict:
    senders, cats, mails, more = _touched(app, acct, e)
    how = (app.store.cache_get(acct, "auto_logs")[0] or {}).get(str(e["id"])) or {} if e["action"] == "auto" else {}
    return {"log_id": e["id"], "account": acct, "at": e.get("ts"), "by": _by(app, acct, e, plans), "op": _op(app, acct, e),
            "count": len(e["item_ids"]), "senders": senders, "categories": cats, "mails": mails, "more": more,
            "label": how.get("label"),
            "ok": bool(e.get("ok", True)), "undoable": bool(e["undoable"]), "undone": bool(e.get("undone"))}


def _decide_item(acct: str, x: dict, it: dict) -> dict:
    ways = WAYS["suspicious"] if x.get("suspicious") else WAYS.get(x.get("action") or "", [])
    return {"id": x["id"], "account": acct, "from": _who(it), "subject": memory.mask(x.get("subject")),
            "summary_text": memory.mask(x.get("summary")) or None, "category": x.get("category"),
            "suspicious": bool(x.get("suspicious")), "due": x.get("due_date"), "late": x.get("late"),
            "ways": list(ways), "suggested": ways[0] if ways else None}


def today(app, args: dict) -> dict:
    day = dt.date.today().isoformat()
    handled, decide, picked, replies = [], [], 0, 0
    for acct in accounts_of(app, args):
        plans = app.store.plan_log_ids(acct)
        for e in app.store.log_entries(acct, limit=500):
            if (e.get("ts") or "")[:10] != day:
                continue
            if e["action"] in ("auto", "seen") and not e.get("undone"):
                handled.append(_step(app, acct, e, plans))
        for x in app.review.items(acct, day):
            if x["state"] != "pending" or x["result"] not in ("waiting", "suspicious"):
                continue
            if x["result"] == "suspicious" or x.get("action") != "reply":
                d = _decide_item(acct, x, app.store.get(x["id"]) or {})
                if d["ways"]:
                    decide.append(d)
        picked += sum(1 for it in app.store.digest_items(acct) if it.get("pick_week") and (it.get("created_at") or "")[:10] == day)
        try:
            replies += sum(1 for it in app.agent.waiting(acct)["needs"] if it.get("action") == "reply")
        except Exception:  # noqa: BLE001 -- a count; the rest stands
            pass
    late = {"overdue": 0, "due": 1, "old": 2}
    decide.sort(key=lambda d: (not d["suspicious"], late.get(d["late"] or "", 3)))
    handled.sort(key=lambda s: s["at"] or "", reverse=True)
    return {"day": day, "handled": handled, "picked": picked, "decide": decide, "replies": replies}


# ---- data/inbox -------------------------------------------------------------------------------------------------
LISTS = ("reply", "needs", "suspicious", "direct", "archived", "all")      # archived: not in "all", not the inbox
NEEDS = ("reply", "schedule", "follow_up")


def _kind(it: dict) -> str:
    if it.get("suspicious") or it.get("junk"):
        return "suspicious"
    a = it.get("action")
    return a if a in NEEDS else "direct" if a in ("archive", "ignore") else "unsorted"


def _in_list(kind: str, which: str) -> bool:
    return which == "all" or kind == which or (which == "needs" and kind in NEEDS + ("suspicious",))


def _line(acct: str, it: dict, due: set, archived: bool = False) -> dict:
    return {"id": it["id"], "account": acct, "archived": archived, "from": _who(it), "subject": memory.mask(it.get("subject")),
            "ask_text": memory.mask(it.get("summary")) or None, "date": it.get("date"), "due": it.get("due_date"),
            "start": it.get("start_date"), "overdue": bool(it.get("overdue")), "due_soon": it["id"] in due, "kind": _kind(it),
            "action": it.get("action"), "priority": it.get("priority"), "suspicious": bool(it.get("suspicious")),
            "flagged": bool(it.get("flagged")), "draft": bool(it.get("draft_text")), "open_url": it.get("open_url"),
            "calendar_added": bool(it.get("calendar_added"))}


PLAN_PARTS = ("todo", "tidy", "sus")


def _plan(app, args: dict) -> tuple[dict, set]:
    """The mail of a card's plan (`plan`, and `part`: todo, tidy, sus), still waiting in the inbox: what a tap on
    the card would act on. `state`: open (also once a tap was taken back), or handled (a tap carried it out), or gone
    (the card was made again, put away, or never was)."""
    pid, part = str(args.get("plan") or ""), args.get("part")
    plan = app.store.plan_get(pid) if pid else None
    if not plan or plan["account"] not in app.mail_accounts():
        return {"id": pid, "part": part, "state": "gone"}, set()
    state = "open" if plan["state"] == "open" else "handled" if plan["state"] in ("running", "done", "undone") else "gone"
    entries = [e for k, e in plan["entries"].items()
               if part not in PLAN_PARTS or e.get("part") == part or (part == "tidy" and k == "tidy")]
    ids = {int(i) for e in entries for i in e.get("ids") or []} if state == "open" else set()
    return {"id": pid, "part": part if part in PLAN_PARTS else None, "state": state, "account": plan["account"]}, ids


def inbox(app, args: dict) -> dict:
    """What is in the inbox and waits on the user, a line a mail: due soon first (soonest first), then newest.
    With `plan`: only the mail of that card (list "plan")."""
    plan, ids = _plan(app, args) if args.get("plan") else (None, set())
    which = "plan" if plan else args.get("list") if args.get("list") in LISTS else "reply"
    rows, counts = [], {k: 0 for k in LISTS[:-1] + ("unsorted",)}
    for acct in ([plan["account"]] if plan and ids else [] if plan else accounts_of(app, args)):
        pending, archived, due = app.inbox_mail(acct)
        for it in pending:
            if plan and it["id"] not in ids:
                continue
            line = _line(acct, it, due)
            for k in counts:
                counts[k] += int(_in_list(line["kind"], k))
            if plan or _in_list(line["kind"], which):
                rows.append(line)
        if not plan:                               # mail the user archived that still has a reply or an appointment
            counts["archived"] += len(archived)
            rows += [_line(acct, it, due, True) for it in archived] if which == "archived" else []
    rows.sort(key=lambda r: (r["date"] or "", r["account"], r["id"]), reverse=True)    # the same order each page
    rows.sort(key=lambda r: (not r["due_soon"], r["due"] or "" if r["due_soon"] else ""))
    at = int(args.get("cursor") or 0) if str(args.get("cursor") or "0").isdigit() else 0
    page = rows[at:at + LIST_MOST]
    more = at + LIST_MOST < len(rows)
    return {"list": which, "items": page, "counts": counts, "more": more, "cursor": str(at + LIST_MOST) if more else None,
            **({"plan": {k: v for k, v in plan.items() if k != "account"}} if plan else {})}


# ---- data/digest ------------------------------------------------------------------------------------------------
def _piece(app, acct: str, it: dict, picked: bool) -> dict:
    url = it.get("url") if str(it.get("url") or "").startswith("https://") else None
    return {"id": it["id"], "account": acct, "title": memory.mask(it.get("title")), "source": it.get("source"),
            "summary_text": memory.mask(it.get("summary")) or None,
            "why_text": memory.mask(it.get("pick_why") or it.get("why")) or None,
            "url": url, "host": it.get("host") if url else None, "mail_url": it.get("mail_url"), "date": it.get("date"),
            "feedback": it.get("feedback"), "opened": bool(it.get("opened")), "picked": picked}


def digest(app, args: dict) -> dict:
    """精选: the pieces of a range -- `week` (the last seven days), `today` (read out today, of the week's),
    `month:<YYYY-MM>`, `saved`, `archived` -- and how many are kept, set aside, and where they come from."""
    rng = str(args.get("range") or "week")
    items, months, reading, sources = [], [], {"queued": 0, "read": 0, "on": True, "problem": None, "problem_code": None}, []
    counts = {"saved": 0, "archived": 0, "sources": 0}
    accts = accounts_of(app, args)
    if not (rng in ("week", "today", "saved", "archived") or rng.startswith("month:")):
        raise ToolError("invalid", "不支持该范围。")
    cur = app.curator
    for acct in accts:                  # only the asked range is picked: picking every month took half a second
        kept = {"saved": app.store.digest_saved(acct), "archived": app.store.digest_archived(acct)}
        own = app.store.digest_sources(acct)
        counts["saved"] += len(kept["saved"])
        counts["archived"] += len(kept["archived"])
        counts["sources"] += len(own)
        sources += [{"key": s["key"], "account": acct, "name": memory.mask(s.get("name")) or None,
                     "origin": s.get("origin"), "added_at": s.get("added_at")} for s in own]
        shown = cur.months(acct)
        months += shown
        r = cur.reading(acct)
        reading = {"queued": reading["queued"] + (r.get("queued") or 0), "read": reading["read"] + (r.get("read") or 0),
                   "on": reading["on"] and r.get("on", True), "problem": reading["problem"] or r.get("problem"),
                   "problem_code": reading["problem_code"] if reading["problem"] else r.get("problem_code")}
        if rng in ("week", "today"):
            week, pending = cur.period(acct, "week")
            chosen = [(it, not pending) for it in week]
            if rng == "today":
                day = dt.date.today().isoformat()
                chosen = [(it, p) for it, p in chosen
                          if ((app.store.digest_item(it["id"]) or {}).get("created_at") or "")[:10] == day]
        elif rng.startswith("month:"):
            picked, pending = cur.period(acct, "month", rng[6:]) if rng[6:] in shown else ([], False)
            chosen = [(it, not pending) for it in picked]
        else:
            chosen = [(cur.public_item(acct, it), False) for it in kept[rng]]
        items += [_piece(app, acct, it, p) for it, p in chosen]
    if rng in ("saved", "archived") or len(accts) > 1:      # one mailbox's picks stay in the order chosen
        items.sort(key=lambda x: x["date"] or "", reverse=True)
    return {"range": rng, "items": items[:LIST_MOST], "counts": counts, "sources": sources,
            "months": sorted(set(months), reverse=True), "reading": reading}


def piece(app, args: dict) -> dict:
    """One piece of 精选, opened: the same as in data/digest. Reading it is the user opening it -- kept once as
    「打开过」, an activity in the memory (as mail_digest_open); the one read here that writes (docs/agent-data.md)."""
    acct = _first(app, args)
    n = _piece_of(app, acct, args)
    _failed(lambda: app.curator.opened({"source": acct, "id": n}))
    it = app.store.digest_item(n)
    return _piece(app, acct, it, bool(it.get("pick_week") or it.get("pick_month")))


# ---- data/clean -------------------------------------------------------------------------------------------------
# 订阅体检's groups (the old page's): what the check says of a sender the user has not decided on
SENDER_GROUPS = {"unsubscribe": "unsubscribe", "filter": "no_way", "never": "suspicious", "keep": "keep", "important": "keep"}
SENDER_ORDER = ("unsubscribe", "no_way", "suspicious", "handled", "keep")


def _sender(acct: str, sub: dict, days: int, kinds: Optional[dict] = None) -> dict:
    url = sub.get("body_link") if sub.get("method") == "body_link" else sub.get("unsub_http")
    mailto, d, rule = sub.get("unsub_mailto"), sub.get("decision"), sub.get("rule")
    return {"key": sub["key"], "account": acct, "name": memory.mask(sub.get("name")) or None,
            "address": sub.get("from_addr"), "domain": sub.get("from_domain"), "count": sub.get("count") or 0,
            "per_week": round((sub.get("count") or 0) * 7 / max(days, 1), 1), "read": sub.get("read") or 0,
            "verdict": sub.get("verdict"), "kind": (kinds or {}).get(sub["key"]),
            "last": sub.get("last_date"), "method": sub.get("method"), "one_click": sub.get("method") == "one_click",
            "unsubscribe_url": url if str(url or "").startswith("https://") else None,
            "mailto": mailto if str(mailto or "").lower().startswith("mailto:") else None,
            "reply_keyword": sub.get("reply_keyword") or None,
            "size": sub.get("bytes") or 0, "subjects": [memory.mask(s) for s in sub.get("subjects") or [] if s],
            "suspicious": bool(sub.get("suspicious")),
            "group": "handled" if d else "suspicious" if sub.get("suspicious") else SENDER_GROUPS.get(sub.get("verdict"), "keep"),
            "decision": {"status": d["status"], "at": d.get("at")} if d else None,
            "still_sending": bool(sub.get("still_sending")),
            "rule": {"id": rule["id"], "action": rule["action"]} if rule else None}


def _group(acct: str, g: dict) -> dict:
    return {"key": g["key"], "account": acct, "kind": g.get("shown") or g["kind"], "name": memory.mask(g.get("name")) or None,
            "address": g.get("addr"), "count": g["count"], "size": g.get("size") or 0,
            "earliest": g.get("earliest") or None, "latest": g.get("latest") or None}


def clean(app, args: dict) -> dict:
    """清理: what the subscription check found of every sender (`subscriptions`), and old mail in the inbox that can
    be archived, by source. Nothing is cleared without the user's tap (11-13)."""
    full, arch = None, None
    for acct in accounts_of(app, args):
        subs = app.subscriptions_view(acct)
        if subs.get("senders") is not None:
            days = int(subs.get("days") or 60)
            kinds = app.store.cache_get(acct, "sender_kinds")[0] or {}
            rows = [_sender(acct, x, days, kinds) for x in subs["senders"]]
            f = full or {"checked_at": None, "days": days, "scanned": 0, "unreturned": 0, "model_note": None, "model_check": None,
                         "senders": []}
            if not f["model_note"] and subs.get("model_note"):
                f["model_note"], f["model_check"] = subs.get("model_note"), subs.get("model_check")
            f["checked_at"] = max(f["checked_at"] or "", subs.get("at") or "") or None
            f["scanned"] += subs.get("scanned") or 0
            f["unreturned"] += subs.get("unreturned") or 0
            f["senders"] += rows
            full = f
        old = app.old_view(acct, "inbox")
        if old.get("scanned"):
            keep = set(old.get("keep") or [])
            a = arch or {"scanned_at": None, "days": old.get("days"), "judged": 0, "total": 0, "unreturned": 0, "judging": False,
                         "groups": []}
            a["scanned_at"] = max(a["scanned_at"] or "", old.get("at") or "") or None
            a["judged"] += old.get("judged") or 0
            a["total"] += old.get("total") or 0
            a["unreturned"] += old.get("unreturned") or 0
            a["judging"] = a["judging"] or bool((old.get("judging") or {}).get("on"))
            a["groups"] += [_group(acct, g) for g in old.get("groups") or [] if g["kind"] != "pending"
                            and g["action"] == "archive" and g["suggested"] and g["key"] not in keep]
            arch = a
    if full:
        full["senders"].sort(key=lambda x: (SENDER_ORDER.index(x["group"]), -x["size"], -x["count"]))
    if arch:
        arch["groups"].sort(key=lambda g: -g["count"])
    return {"subscriptions": full, "archive": arch}


def _first(app, args: dict) -> str:
    """The one mailbox a read is about: `account`, or the first (as every data/ read); not `all`."""
    if str(args.get("account") or "").strip().lower() == "all":
        raise ToolError("invalid", "此处仅支持一个邮箱。")
    accts = accounts_of(app, args)
    if not accts:
        raise ToolError("invalid", "未连接邮箱。")
    return accts[0]


def sender(app, args: dict) -> dict:
    """One sender of the subscription check, with what clearing its history would take: how many of its messages
    are in the mailbox now (the inbox and the archive) and their size -- asked of the mailbox, nothing moved.
    The count is asked in the background and waited for a moment only (App.sender_history): `history_state`
    ready (with `history`), pending (read again later), or error (`history_error_code`); the rest is there always."""
    acct = _first(app, args)
    subs = app.subscriptions_view(acct)
    sub = _sub_of(app, acct, args)
    h = app.sender_history(acct, sub["key"])
    out = {**_sender(acct, sub, int(subs.get("days") or 60), app.store.cache_get(acct, "sender_kinds")[0]),
           "history_state": h["state"]}
    if h["state"] == "ready":
        out["history"] = {"count": h.get("count") or 0, "size": h.get("bytes") or 0, "purge_days": h.get("purge_days")}
    elif h["state"] == "error":
        out["history_error_code"] = h["error_code"]
    return out


# ---- data/old ---------------------------------------------------------------------------------------------------
OLD_SCOPES = ("inbox", "archive")


def _scope(args: dict) -> str:
    scope = str(args.get("scope") or "inbox")
    if scope not in OLD_SCOPES:
        raise ToolError("invalid", "不支持该范围。")
    return scope


def old(app, args: dict) -> dict:
    """清理旧邮件, one mailbox and one place (`inbox`: older than the days kept; `archive`: the archived mail), as the
    old page listed it: every group with what a tick does to it (`action`) and whether it is suggested; the groups
    the user said to keep (`kept`) too. Starred or labelled-to-keep mail is never in a group (`held` counts it)."""
    acct = _first(app, args)
    scope = _scope(args)
    v = app.old_view(acct, scope)
    if not v.get("scanned"):
        return {"account": acct, "scope": scope, "scanned_at": None, "groups": []}
    keep = set(v.get("keep") or [])
    groups = [{**_group(acct, g), "action": g["action"], "suggested": bool(g["suggested"]) and g["key"] not in keep,
               "judged": g["kind"] != "pending", "kept": g["key"] in keep, "unread": g.get("unread") or 0,
               "subjects": [memory.mask(s) for s in g.get("subjects") or []]} for g in v.get("groups") or []]
    groups.sort(key=lambda g: (not g["suggested"], not g["judged"], -g["count"]))
    j = v.get("judging") or {}
    return {"account": acct, "scope": scope, "scanned_at": v.get("at"), "days": v.get("days"), "stale": bool(v.get("stale")),
            "total": v.get("total") or 0, "judged": v.get("judged") or 0, "size": v.get("size") or 0,
            "held": v.get("flagged") or 0, "unreturned": v.get("unreturned") or 0,
            "judging": {"on": bool(j.get("on")), "running": bool(j.get("running")), "problem": j.get("problem"),
                        "problem_code": j.get("problem_code")},
            "groups": groups}


# ---- data/job ---------------------------------------------------------------------------------------------------
def job(app, args: dict) -> dict:
    """A job started by an action here (`job` in its answer), running or finished: its numbers, and what it found.
    Kept until the app quits. A finished 清理 has `log_id`, undone with mail_undo_step."""
    j = app.jobs.get(str(args.get("id") or ""))
    if not j:
        raise ToolError("stale", "任务不存在。")
    r = j.get("result") if isinstance(j.get("result"), dict) else {}
    result = {k: v for k, v in r.items() if isinstance(v, (bool, int, float)) or k == "log_id"}
    if j["kind"] == "filter_preview" and r:
        result = {"rule": r.get("rule"), "checked": bool(r.get("checked")),
                  "ways": [{**{k: w.get(k) for k in ("kind", "value", "count", "judged", "risky")},
                            "examples": [memory.mask(s) for s in w.get("examples") or []]} for w in r.get("ways") or []]}
    return {"id": j["id"], "kind": j["kind"], "account": j.get("account"), "scope": j.get("scope"),
            "status": j["status"], "stage": j.get("stage") or None, "done": j.get("done") or 0, "total": j.get("total") or 0,
            "error": j.get("error"), "error_code": j.get("error_code") if j.get("error") else None,
            "result": result if j["status"] != "running" else None}


# ---- data/providers ---------------------------------------------------------------------------------------------
PROVIDER_FIELDS = ("name", "host", "port", "secret_label", "secret_hint", "steps", "links", "notes", "trouble",
                   "purge_days", "block")


def provider_list(app, args: dict) -> dict:
    """The kinds of mailbox 添加邮箱 offers, as the page here shows them: what the password field is called, the line
    under it, how to get the app password (steps, links), and what to try when it does not work."""
    return {"providers": [{"key": key, **{f: p.get(f) for f in PROVIDER_FIELDS}} for key, p in providers.PROVIDERS.items()]}


# ---- data/settings ----------------------------------------------------------------------------------------------
def settings(app, args: dict) -> dict:
    """设置: the mailboxes, the rules and the habits followed, background sorting and its notices, and the rest as
    values (「更多设置」). Adding or removing a mailbox and clearing everything are not here yet."""
    st = app.state()
    rules, habits, sites, learning = [], [], [], {}
    for acct in app.mail_accounts():
        view = app.rules_view(acct)
        rules += [{"id": r["id"], "account": acct, "kind": r["kind"], "value": r["value"], "who": r.get("who"),
                   "action": r["action"], "label": r.get("label"), "hits": r.get("hits") or 0,
                   "created_at": r.get("created_at"), "exported_at": r.get("exported_at"),
                   "gmail": bool(view.get("gmail"))} for r in view["rules"]]
        try:
            habits += [{"note_id": h.get("note_id"), "account": acct, "text": h.get("text")}
                       for h in memory.auto_habits(app.memory_for(acct), acct)]
        except Exception:  # noqa: BLE001 -- the memory unreadable just now: the rest stands
            pass
    first = (app.mail_accounts() or ["demo"])[0]
    try:
        mv = app.curator.memory_view(first)
        sites = [{"id": x.get("id"), "url": x.get("url"), "title": x.get("title"), "posts": x.get("posts") or 0,
                  "problem": bool(x.get("problem")), "error": x.get("problem") or None,
                  "checked_at": _clock(x.get("fetched_at"))} for x in mv.get("sites") or []]
        lv = mv.get("learning") or {}
        learning = {"on": bool(lv.get("enabled")) and bool(lv.get("on", True)), "stage": lv.get("stage"),
                    "running": bool(lv.get("running")), "sent_done": lv.get("sent_done"), "sent_total": lv.get("sent_total"),
                    "problem": lv.get("problem") or None, "problem_code": lv.get("problem_code") or None,
                    "waiting": lv.get("waiting") or 0, "learnt": lv.get("learnt") or 0}
    except Exception:  # noqa: BLE001
        pass
    a = st.get("auto") or {}
    e, hm = st.get("engine") or {}, st.get("hub_model") or {}
    return {"accounts": accounts(app, args)["accounts"], "rules": rules, "habits": habits,
            "auto": {"enabled": bool(a.get("enabled")), "every": a.get("every"), "intervals": a.get("intervals"),
                     "last": a.get("last"), "next_in_min": a.get("next_in_min"), "stalled": _stalled(a),
                     "notify": st.get("notify")},
            "more": {"mode_write": bool((st.get("mode") or {}).get("write")),
                     "mark_read": (st.get("flow") or {}).get("mark_read", True) is not False,
                     "threads": bool((st.get("threads") or {}).get("on")),
                     "fetch_days": (st.get("fetch") or {}).get("days"), "fetch_limit": (st.get("fetch") or {}).get("limit"),
                     "signature": (st.get("profile") or {}).get("signature") or "",
                     "power": (st.get("power") or {}).get("level"), "power_by_luoke": e.get("via") == "hub" or bool(st.get("hub_only")),
                     "model": hm.get("name") or e.get("model"), "learn": learning, "sites": sites}}


def _stalled(a: dict) -> Optional[str]:
    """On, but not running here (the old page's 「未运行」): its login item was removed (`removed`) or points at a
    program moved since (`moved`). Turning it on again (mail_settings auto_enabled true) puts it back."""
    d = a.get("desktop") or {}
    if not (a.get("enabled") and d.get("supported") and not a.get("service")):
        return None
    return "removed" if not d.get("installed") else "moved" if d.get("stale") else None


def _clock(t) -> Optional[str]:
    """An epoch as the time everywhere here ("YYYY-MM-DD HH:MM:SS", local)."""
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if isinstance(t, (int, float)) and t else None


# ---- data/activity ----------------------------------------------------------------------------------------------
def activity(app, args: dict) -> dict:
    """Every step taken on the mailbox, newest first, a page at a time (`cursor`: below that log id)."""
    try:
        below = int(args.get("cursor")) if args.get("cursor") else None
    except ValueError:
        below = None
    items = []
    for acct in accounts_of(app, args):
        plans = app.store.plan_log_ids(acct)
        for e in app.store.log_entries(acct, limit=2000):
            if below is not None and e["id"] >= below:
                continue
            step = _step(app, acct, e, plans)
            mail = e.get("mail")
            step["mail"] = {"subject": memory.mask(mail.get("subject")), "from": memory.mask(mail.get("from"))} if mail else None
            items.append(step)
    items.sort(key=lambda x: (x["at"] or "", x["log_id"]), reverse=True)
    page = items[:LIST_MOST]
    more = len(items) > LIST_MOST
    return {"items": page, "more": more, "cursor": str(page[-1]["log_id"]) if more else None}


# ---- data/mail --------------------------------------------------------------------------------------------------
def _ways_of(it: dict) -> List[str]:
    """What can be done with this mail, the suggestion first (the ops of mail_act, plus draft and calendar). Mail
    archived before its date: only 完成 (its date stops reminding)."""
    if it.get("suspicious"):
        return ["spam", "archive", "skip"] if it.get("state") == "pending" else []
    today = (samples.TODAY if it.get("account") == "demo" else dt.date.today()).isoformat()
    due = [e for e in triage.calendar_events(it) if e.get("kind") == "due" and e["date"] >= today]   # a deadline to come
    if it.get("state") != "pending":
        return ["complete"] + (["calendar"] if due else [])
    first = {"reply": ["draft"], "schedule": ["calendar"], "follow_up": ["flag"], "ignore": ["trash"]}.get(it.get("action") or "", [])
    first += ["calendar"] if due else []
    return first + [op for op in ("archive", "trash", "flag", "skip") if op not in first]


def mail(app, args: dict) -> dict:
    """One mail, opened (收件箱's line): from data/inbox's `id` (this app's own), or a search hit's (`<account>|<Message-ID>`)."""
    ident = str(args.get("id") or "")
    if "|" in ident:
        acct, _, mid = ident.partition("|")
        it = app.store.mail_by_id(acct, mid) if acct in app.mail_accounts() else None
    else:
        try:
            it = app.store.get(int(ident))
        except ValueError:
            it = None
        acct = (it or {}).get("account")
    if not it or (acct not in app.mail_accounts() and acct != "demo"):
        raise ToolError("stale", "未找到该邮件。")
    body = it.get("body")
    if body is None and it.get("uid"):                     # old mail: only its headers are here; read, nothing marked
        try:
            with app.open_mailbox(acct) as mb:
                raw = mb.fetch_raw(it["uid"], it.get("folder") or "INBOX")
            parsed = parse_message(raw) if raw else {}
            body = parsed.get("body") if parsed.get("message_id") == it.get("message_id") else None
        except MailError:
            body = None
    def names(k: str) -> list:                     # a sorted mail has them decoded (store._item); old mail, as stored
        v = it.get(k) if isinstance(it.get(k), list) else json.loads(it.get(k + "_json") or "[]")
        return [memory.mask(str(x)) for x in v]
    return {"id": it.get("id"), "account": acct, "from": _who(it), "to": names("to"), "cc": names("cc"),
            "date": it.get("date"), "subject": memory.mask(it.get("subject")),
            "body_text": memory.mask(body)[:BODY_MOST] if body else None, "truncated": bool(body) and len(body) > BODY_MOST,
            "attachments": names("attachments"), "ask_text": memory.mask(it.get("summary")) or None,
            "draft_text": memory.mask(it.get("draft_text")) or None, "suspicious": bool(it.get("suspicious")),
            "action": it.get("action"), "category": it.get("category"), "due": it.get("due_date"),
            "start": it.get("start_date"), "open_url": _open_url(app, acct, it), "ways": _ways_of(it) if it.get("state") == "pending" or _put_away(app, acct, it) else [],
            "events": _events(app, acct, it)}


def _put_away(app, acct: str, it: dict) -> bool:
    """Archived before its date, and its date still reminds (App.put_away)."""
    if it.get("state") != "done" or it.get("done_action") not in Store.PUT_AWAY or not it.get("due_date"):
        return False
    today = samples.TODAY if acct == "demo" else dt.date.today()
    return any(x["id"] == it["id"] for x in app.put_away(acct, today))


def _events(app, acct: str, it: dict) -> list:
    """The appointments in a mail, each with its `index` for mail_calendar: over (`past`), already handed to the
    calendar (`added`)."""
    if not it.get("account"):
        return []
    today = samples.TODAY if acct == "demo" else dt.date.today()
    return [{"index": e["index"], "title": memory.mask(e.get("title")) or None, "date": e.get("date"),
             "start": e.get("start"), "end": e.get("end"), "location": memory.mask(e.get("location")) or None,
             "past": bool(e.get("past")), "added": bool(e.get("added")), "kind": e.get("kind") or "event"}
            for e in app._events_view(it, today)]


# ---- actions ----------------------------------------------------------------------------------------------------
def _one(app, args: dict) -> str:
    acct = str(args.get("account") or "").lower()
    if acct not in app.mail_accounts() and acct != "demo":
        raise ToolError("invalid", "未连接该邮箱。")
    return acct


def _failed(fn: Callable[[], dict]) -> dict:
    try:
        return fn()
    except (ActionError, MailError, ValueError) as ex:
        raise ToolError("failed", str(ex))


def act(app, args: dict, meta: dict) -> dict:
    acct = _one(app, args)
    op = args.get("op")
    if op not in ACT_OPS:
        raise ToolError("invalid", "不支持该操作。")
    ids = [int(i) for i in args.get("ids") or [] if str(i).isdigit()]
    if not ids:
        raise ToolError("invalid", "未选择邮件。")
    if op == "unread":
        for i in ids:
            _failed(lambda i=i: app.review.act({"source": acct, "id": i, "op": "unread"}))
        return {"text": "已标为未读。"}
    r = _failed(lambda: app.items_act(acct, ids, op))
    return {"text": r.get("message") or "已完成。", "log_id": r.get("log_id"), "ok": bool(r.get("ok", True))}


def undo_step(app, args: dict, meta: dict) -> dict:
    acct = _one(app, args)
    try:
        log_id = int(args.get("log_id"))
    except (TypeError, ValueError):
        raise ToolError("invalid", "该步骤不存在。")
    e = app.store.log_entry(log_id)
    if not e or e.get("account") != acct:
        raise ToolError("stale", "该步骤不存在。")
    r = _failed(lambda: app.undo(acct, log_id))
    return {"text": r.get("message") or "已撤销。", "ok": bool(r.get("ok", True))}


def calendar(app, args: dict, meta: dict) -> dict:
    """加入日程: the appointment as a calendar file handed to 「日历」; the user confirms there."""
    acct = _one(app, args)
    index = args.get("index")
    cal = _failed(lambda: app.actions(acct, write=False).calendar(int(args["id"]), int(index) if index is not None else None))
    folder = os.path.join(config.CONFIG_DIR, "calendar")
    os.makedirs(folder, 0o700, exist_ok=True)
    path = os.path.join(folder, cal["filename"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(cal["ics"])
    desktop.open_url(path)
    return {"text": "已交给「日历」，请在日历中确认。"}


def _item_of(app, acct: str, args: dict) -> int:
    try:
        n = int(args.get("id"))
    except (TypeError, ValueError):
        raise ToolError("invalid", "邮件不存在。")
    it = app.store.get(n)
    if not it or it["account"] != acct:
        raise ToolError("stale", "未找到该邮件。")
    return n


def draft(app, args: dict, meta: dict) -> dict:
    """回复: a draft written by the model on this Mac, kept here (data/mail's `draft_text`); not in the mailbox yet."""
    acct = _one(app, args)
    n = _item_of(app, acct, args)
    r = _failed(lambda: app.draft(acct, n))
    return {"text": "草稿已生成。", "draft_text": memory.mask(r.get("text")), "draft_note_text": r.get("note")}


def draft_save(app, args: dict, meta: dict) -> dict:
    """The draft, as the user left it, into the mailbox's 「草稿」 (then open_url shows it in Gmail)."""
    acct = _one(app, args)
    n = _item_of(app, acct, args)
    text = str(args.get("text") or "")[:20000]
    if not text.strip():
        raise ToolError("invalid", "草稿为空。")
    r = _failed(lambda: app.actions(acct).save_draft(n, text, app.address(acct), app.cfg["profile"].get("signature") or ""))
    return {"text": r.get("message") or "已存入草稿箱。", "ok": bool(r.get("ok", True))}


def change(app, args: dict, meta: dict) -> dict:
    """改建议: another suggestion for these mails; `remember` (sender, domain, list) makes it a rule as well."""
    acct = _one(app, args)
    remember = args.get("remember") if args.get("remember") in ("sender", "domain", "list") else ""
    r = _failed(lambda: app.override({"source": acct, "ids": args.get("ids") or [], "action": args.get("action"),
                                      "remember": remember}))
    return {"text": r.get("message") or "已修改。", "log_id": r.get("log_id")}


def _each(app, args: dict, start: Callable[[str], dict], text: str) -> dict:
    """A job started for one mailbox, or for each (`account=all`): they wait their turn. A mailbox that cannot
    start (its password missing) does not stop the others; `jobs` has an id for each mailbox that started."""
    accts = app.mail_accounts() if str(args.get("account") or "").lower() == "all" else [_one(app, args)]
    jobs, failed = [], []
    for acct in accts:
        try:
            jobs.append(_failed(lambda acct=acct: start(acct)).get("id"))
        except ToolError as ex:
            failed.append({"account": acct, "message": str(ex)})
    if not jobs:
        raise ToolError("failed", failed[0]["message"] if failed else "未连接邮箱。")
    return {"text": text, "job": jobs[0], "jobs": jobs, "failed": failed}


def sync(app, args: dict, meta: dict) -> dict:
    return _each(app, args, lambda acct: app.start_sync({"source": acct}), "开始同步。")


def job_stop(app, args: dict, meta: dict) -> dict:
    job = app.jobs.get(str(args.get("id") or ""))
    if not job or job.get("status") != "running":
        raise ToolError("stale", "该任务已停止。")
    job["cancel"] = True
    return {"text": "当前步骤完成后停止。"}


def _piece_of(app, acct: str, args: dict) -> int:
    try:
        n = int(args.get("id"))
    except (TypeError, ValueError):
        raise ToolError("invalid", "该条目不存在。")
    it = app.store.digest_item(n)
    if not it or it["account"] != acct:
        raise ToolError("stale", "该条目已移除。")
    return n


def digest_feedback(app, args: dict, meta: dict) -> dict:
    """多来点这类 (`useful`, kept under 收藏 too) / 少来点这类 (`dismissed`) / taken back (null): into the memory."""
    acct = _one(app, args)
    n = _piece_of(app, acct, args)
    value = args.get("value") if args.get("value") in ("useful", "dismissed") else None
    _failed(lambda: app.curator.feedback({"source": acct, "id": n, "value": value}))
    return {"text": "已记录。"}


def digest_archive(app, args: dict, meta: dict) -> dict:
    acct = _one(app, args)
    n = _piece_of(app, acct, args)
    on = bool(args.get("on", True))
    _failed(lambda: app.curator.archive({"source": acct, "id": n, "on": on}))
    return {"text": "已移出精选。" if on else "已移回精选。"}


def digest_open(app, args: dict, meta: dict) -> dict:
    """The user opened a piece (its own page in the window): kept once, as an activity in the memory."""
    acct = _one(app, args)
    n = _piece_of(app, acct, args)
    _failed(lambda: app.curator.opened({"source": acct, "id": n}))
    return {"text": "已记录。"}


def digest_reading(app, args: dict, meta: dict) -> dict:
    acct = _one(app, args)
    _failed(lambda: app.curator.set_reading({"source": acct, "on": bool(args.get("on"))}))
    return {"text": "已继续阅读。" if args.get("on") else "当前邮件读完后暂停。"}


def digest_source(app, args: dict, meta: dict) -> dict:
    """A sender put into 精选 (`on: true`) or taken out."""
    acct = _one(app, args)
    key = str(args.get("key") or "")
    if not key:
        raise ToolError("invalid", "来源不存在。")
    _failed(lambda: app.curator.set_source({"source": acct, "key": key, "on": bool(args.get("on")), "name": args.get("name")}))
    return {"text": "已加入精选来源。" if args.get("on") else "已移出精选来源。"}


def _sub_of(app, acct: str, args: dict) -> dict:
    key = str(args.get("key") or "")
    subs = (app.subscriptions_view(acct).get("senders") or [])
    sub = next((x for x in subs if x["key"] == key), None)
    if not sub:
        raise ToolError("stale", "订阅体检中已无该发件人。")
    return sub


def _sender_op(app, acct: str, key: str, op: str) -> dict:
    r = _failed(lambda: app.sender_act({"source": acct, "key": key, "op": op}))
    return {"text": r.get("message") or "已完成。", "log_id": r.get("log_id"), "ok": bool(r.get("ok", True)),
            **({"open_url": r["open_url"]} if str(r.get("open_url") or "").startswith("https://") else {})}


def unsubscribe(app, args: dict, meta: dict) -> dict:
    """一键退订 (RFC 8058), for senders that take it -- one (`key`) or the several the user saw named in the window's
    confirmation and agreed to in one tap (`keys`). Cannot be taken back. A sender that turns the request down
    answers with its unsubscribe page, for the user to finish there: listed under `web` with its `open_url`."""
    acct = _one(app, args)
    keys = [str(k) for k in args.get("keys") or []] or [str(args.get("key") or "")]
    subs = {x["key"]: x for x in app.subscriptions_view(acct).get("senders") or []}
    if not keys or any(k not in subs for k in keys):
        raise ToolError("stale", "订阅体检中已无该发件人。")
    if any(subs[k].get("suspicious") or subs[k].get("method") != "one_click" for k in keys):
        raise ToolError("invalid", "仅支持一键退订的发件人可在此退订。")
    done, web, failed = [], [], []
    for k in keys:
        name = memory.mask(subs[k].get("name")) or subs[k].get("from_addr")
        try:
            r = app.sender_act({"source": acct, "key": k, "op": "unsubscribe"})
        except (ActionError, MailError, ValueError) as ex:
            failed.append({"key": k, "name": name, "message": str(ex)})
            continue
        if r.get("status") == "unsubscribe_opened" and str(r.get("open_url") or "").startswith("https://"):
            web.append({"key": k, "name": name, "open_url": r["open_url"]})
        elif r.get("ok", True) and r.get("status") == "unsubscribed":
            done.append({"key": k, "name": name})
        else:
            failed.append({"key": k, "name": name, "message": r.get("message") or "退订失败。"})
    if failed and not done and not web:
        raise ToolError("failed", failed[0]["message"])
    one = len(keys) == 1
    text = (f"已退订「{done[0]['name']}」。" if one and done else
            f"「{web[0]['name']}」需要在其网页上退订。" if one and web else
            f"已退订 {len(done)} 个" + (f"，{len(web)} 个需要在网页上退订" if web else "")
            + (f"，{len(failed)} 个退订失败" if failed else "") + "。")
    return {"text": text, "done": done, "web": web, "failed": failed,
            **({"open_url": web[0]["open_url"]} if one and web else {})}


def sender_keep(app, args: dict, meta: dict) -> dict:
    acct = _one(app, args)
    return _sender_op(app, acct, _sub_of(app, acct, args)["key"], "keep")


def sender_rule(app, args: dict, meta: dict) -> dict:
    acct = _one(app, args)
    op = {"archive": "archive_rule", "trash": "trash_rule", "spam": "spam_rule"}.get(str(args.get("action")))
    if not op:
        raise ToolError("invalid", "不支持该操作。")
    return _sender_op(app, acct, _sub_of(app, acct, args)["key"], op)


def sender_history(app, args: dict, meta: dict) -> dict:
    """A sender's mail already in the mailbox to 「已删除」 (the inbox and the archive): undone until the mailbox
    empties it; asks for 处理模式."""
    acct = _one(app, args)
    return _sender_op(app, acct, _sub_of(app, acct, args)["key"], "trash_history")


def sender_reset(app, args: dict, meta: dict) -> dict:
    """A decision of the subscription check taken back: the sender is suggested again; a rule made by the decision
    is deleted, and a 精选 source it added taken out. Undone from data/activity."""
    acct = _one(app, args)
    return _sender_op(app, acct, _sub_of(app, acct, args)["key"], "reset")


def sender_unsubscribed(app, args: dict, meta: dict) -> dict:
    """The user unsubscribed themselves (by the mail `mailto`, a reply with `reply_keyword`, or the sender's page):
    marked so, and the check tells if the sender goes on sending (`still_sending`)."""
    acct = _one(app, args)
    return _sender_op(app, acct, _sub_of(app, acct, args)["key"], "mark_unsubscribed")


def subscriptions_check(app, args: dict, meta: dict) -> dict:
    days = int(args.get("days") or 60)
    return _each(app, args, lambda acct: app.start_subscriptions({"source": acct, "days": days}), "开始检查订阅。")


def old_scan(app, args: dict, meta: dict) -> dict:
    scope = _scope(args)
    return _each(app, args, lambda acct: app.start_old_scan({"source": acct, "scope": scope}),
                 "开始读取收件箱中的旧邮件。" if scope == "inbox" else "开始读取已归档的邮件。")


def old_judge(app, args: dict, meta: dict) -> dict:
    acct = _one(app, args)
    _failed(lambda: app.set_judging({"source": acct, "scope": _scope(args), "on": bool(args.get("on"))}))
    return {"text": "已继续分析。" if args.get("on") else "当前一批完成后暂停。"}


def _old_apply(app, args: dict, action: str) -> dict:
    """These groups of old mail, each `action` by its verdict; `count` is what the user saw, and a list that
    changed since is refused. The job's result (data/job) has the `log_id`; undone with mail_undo_step."""
    acct = _one(app, args)
    scope = _scope(args)
    keys = [str(k) for k in args.get("keys") or []]
    groups = {g["key"]: g for g in app.old_view(acct, scope).get("groups") or []}
    if not keys or any(k not in groups or groups[k]["action"] != action for k in keys):
        raise ToolError("stale", "分组已变化，请重新确认。")
    job = _failed(lambda: app.start_old_apply({"source": acct, "scope": scope, "keys": keys,
                                                 "count": int(args.get("count") or -1), "only": True}))
    return {"text": "开始归档。" if action == "archive" else "开始移到已删除。", "job": job.get("id")}


def old_archive(app, args: dict, meta: dict) -> dict:
    """Old inbox mail archived (marked read, out of the inbox)."""
    return _old_apply(app, {**args, "scope": "inbox"}, "archive")


def old_trash(app, args: dict, meta: dict) -> dict:
    """Old mail the check found spent, to 「已删除」 (`scope`: the inbox's or the archived); asks for 处理模式, and
    can be undone until the mailbox empties it. What 精选 kept from it is starred instead."""
    return _old_apply(app, args, "trash")


def old_keep(app, args: dict, meta: dict) -> dict:
    acct = _one(app, args)
    on = bool(args.get("on", True))
    r = _failed(lambda: app.old_keep({"source": acct, "scope": _scope(args), "key": args.get("key"), "on": on}))
    return {"text": r.get("message") or "已完成。"}


def _route(app, route: str, body: dict) -> dict:
    """A change the browser page makes, made the same way (server.post_routes)."""
    from .server import post_routes            # noqa: PLC0415 -- server imports the agent
    return _failed(lambda: post_routes(app, body)[route]()) or {}


def _int(v):
    """A number, as a number or as its text (a picker's value is text: "30")."""
    try:
        return int(str(v).strip())
    except ValueError:
        raise ToolError("invalid", "请填写数字。")


SETTINGS = {
    # key -> (route, the body for a value); 处理模式 asks first on the page: here the user's tap on what the page asked
    "mode_write": ("/api/mode", lambda v, app: {"write": bool(v), "consent": True}),
    "mark_read": ("/api/flow", lambda v, app: {"mark_read": bool(v)}),
    "threads": ("/api/threads", lambda v, app: {"on": bool(v)}),
    "fetch_days": ("/api/profile", lambda v, app: {"days": _int(v)}),
    "fetch_limit": ("/api/profile", lambda v, app: {"limit": _int(v)}),
    "signature": ("/api/profile", lambda v, app: {"signature": str(v or "")}),
    "power": ("/api/power", lambda v, app: {"level": v}),
    "auto_enabled": ("/api/auto", lambda v, app: {"enabled": bool(v), "every": app.cfg["auto"].get("every"), "consent": True}),
    "auto_every": ("/api/auto", lambda v, app: {"enabled": bool(app.cfg["auto"].get("enabled")), "every": _int(v)}),
}


def set_setting(app, args: dict, meta: dict) -> dict:
    key = str(args.get("key") or "")
    if key not in SETTINGS:
        raise ToolError("invalid", "该设置项不存在。")
    route, body = SETTINGS[key]
    r = _route(app, route, body(args.get("value"), app))
    return {"text": r.get("message") or "已保存。"}


def set_name(app, args: dict, meta: dict) -> dict:
    acct = _one(app, args)
    r = _route(app, "/api/mail/name", {"source": acct, "name": args.get("name") or ""})
    return {"text": r.get("message") or "已保存。"}


def set_own(app, args: dict, meta: dict) -> dict:
    """设为螺壳自己的邮箱: what is sorted there is handled unasked (archive, delete) -- the page says so first."""
    acct = _one(app, args)
    _route(app, "/api/mail/own", {"source": acct, "own": bool(args.get("own"))})
    return {"text": "已设为螺壳自己的邮箱。" if args.get("own") else "已设为个人邮箱。"}


def learn(app, args: dict, meta: dict) -> dict:
    acct = _one(app, args)
    _route(app, "/api/memory/learn", {"source": acct, "on": bool(args.get("on"))})
    return {"text": "已开启。" if args.get("on") else "已暂停。"}


# ---- the mailboxes themselves: the app password, removing one, clearing everything -----------------------------
SECRET_MOST = 512


def connect(app, args: dict, meta: dict) -> dict:
    """添加邮箱，或改一个邮箱的授权码 / 地址（`key`：改的是哪个）. The app password comes from the window's password
    field (protocol §4 secureField) in this one tap: it goes to the keychain (App.save_mail) and nowhere else --
    never into the answer, an error, a view, the log or the memory. Then the mailbox is tried once: a refusal is
    said, and the mailbox stays saved, for the user to correct the password on it."""
    secret = args.get("secret")
    if secret is not None and (not isinstance(secret, str) or len(secret) > SECRET_MOST):
        raise ToolError("invalid", "授权码错误，请重新填写。")
    key = str(args.get("key") or "").lower() or None
    if key and not app.mailbox_cfg(key):                  # its password lost from the keychain: typed again here
        raise ToolError("stale", "未连接该邮箱，请刷新页面。")
    body = {"key": key, "secret": secret or "",
            **{k: args.get(k) for k in ("provider", "address", "host", "port")}}
    saved = _failed(lambda: app.save_mail(body))
    acct = saved["key"]
    try:
        r = app.test_mail({"key": acct})
    except (ActionError, MailError, ValueError) as ex:          # saved: the window goes back to the list
        raise ToolError("failed", f"已保存，连接失败：{ex}", data={"saved": True, "account": acct})
    return {"text": f"连接成功。收件箱共 {r.get('total', 0)} 封，未读 {r.get('unseen', 0)} 封。", "account": acct,
            "total": r.get("total", 0), "unseen": r.get("unseen", 0)}


def remove(app, args: dict, meta: dict) -> dict:
    """移除邮箱: its password leaves the keychain and what luoke-mail keeps about it here is deleted (the list, rules,
    清理, 精选, the record). The mailbox itself is not touched, nor what was written to the memory. Cannot be taken
    back: the window asks first and names the mailbox (§4 confirm). `exiting`: the last one gone, background sorting
    stops and this process with it, right after the answer."""
    acct = str(args.get("account") or "").lower()
    if not app.mailbox_cfg(acct):                         # its password lost from the keychain: removed all the same
        raise ToolError("stale", "未连接该邮箱，请刷新页面。")
    r = _failed(lambda: app.remove_mail({"key": acct}))
    return {"text": r.get("message") or "已移除。", "account": acct, "exiting": bool(r.get("exiting"))}


def forget(app, args: dict, meta: dict) -> dict:
    """全部清除: everything luoke-mail keeps on this computer -- the mailboxes' passwords, the mail list, settings,
    rules, the record. What it wrote to the memory goes too only when the user ticked that (`memory`), the memory
    being theirs (top-level archived ADR 0002). Cannot be taken back; the window asks first. `exiting` as for remove."""
    ticked = args.get("memory") is True                   # the user's memory: only a ticked box, never "false"
    r = _failed(lambda: app.forget({"memory": ticked}))
    return {"text": "已清除邮件助手在本机的全部数据。" + ("写入记忆的内容已一并删除。" if ticked else ""),
            "exiting": bool(r.get("exiting"))}


def site(app, args: dict, meta: dict) -> dict:
    """你的网站和博客: one added (`url`) or removed (`id`)."""
    acct = _one(app, args)
    op = args.get("op") if args.get("op") in ("add", "remove") else None
    if not op:
        raise ToolError("invalid", "不支持该操作。")
    r = _route(app, "/api/memory/site", {"source": acct, "op": op, "url": args.get("url"), "id": args.get("id")})
    return {"text": r.get("message") or ("已添加。" if op == "add" else "已删除。")}


def rule_add(app, args: dict, meta: dict) -> dict:
    acct = _one(app, args)
    r = _route(app, "/api/rules", {"source": acct, "kind": args.get("kind"), "value": args.get("value"),
                                   "action": args.get("action")})
    return {"text": r.get("message") or "已添加。", "log_id": r.get("log_id")}


def rule_delete(app, args: dict, meta: dict) -> dict:
    acct = _one(app, args)
    r = _route(app, "/api/rules/delete", {"source": acct, "id": args.get("id")})
    return {"text": r.get("message") or "已删除。", "log_id": r.get("log_id")}


def rule_preview(app, args: dict, meta: dict) -> dict:
    """What a rule would catch in the past year as a Gmail filter (a job; its result in the job)."""
    acct = _one(app, args)
    r = _route(app, "/api/rules/filter-preview", {"source": acct, "id": args.get("id")})
    return {"text": "开始预演。", "job": r.get("id") or r.get("job")}


def gmail_filters(app, args: dict, meta: dict) -> dict:
    """导出到 Gmail: the filters file handed to the window's save dialog (§4 saveFile)."""
    acct = _one(app, args)
    r = _failed(lambda: app.export_gmail_filters({"source": acct, "include_exported": bool(args.get("all"))}))
    return {"text": r["message"] + "。请在 Gmail 设置中导入。",
            "file": {"name": r["filename"], "mediaType": "application/xml", "text": r["xml"]}}


def register(s, app) -> None:
    """The resources and tools above, on the agent's socket (agent.Agent.start)."""
    def read(fn, a):
        """What cannot be read (a mailbox not connected, a mail no longer here) is said in the data, not as a failure
        of the protocol: {"error": {"code", "message"}}."""
        try:
            return fn(app, a or {})
        except ToolError as ex:
            return {"error": {"code": ex.code, "message": str(ex)}}

    for name, fn in (("accounts", accounts), ("today", today), ("inbox", inbox), ("digest", digest), ("clean", clean),
                     ("settings", settings), ("activity", activity), ("mail", mail), ("providers", provider_list),
                     ("sender", sender), ("old", old), ("job", job), ("piece", piece)):
        s.resource(f"data/{name}", lambda a, fn=fn: read(fn, a), name=f"原始数据：{name}", args=True)
    acct = {"account": {"type": "string"}}
    s.tool("mail_act", lambda a, m: act(app, a, m), title="处理邮件", read_only=False,
           input_schema={"type": "object", "required": ["account", "ids", "op"],
                         "properties": {**acct, "ids": {"type": "array", "items": {"type": "integer"}},
                                        "op": {"enum": list(ACT_OPS)}}})
    s.tool("mail_undo_step", lambda a, m: undo_step(app, a, m), title="撤销一步处理", read_only=False,
           input_schema={"type": "object", "required": ["account", "log_id"],
                         "properties": {**acct, "log_id": {"type": "integer"}}})
    s.tool("mail_calendar", lambda a, m: calendar(app, a, m), title="把邮件里的日程交给「日历」", read_only=False,
           input_schema={"type": "object", "required": ["account", "id"],
                         "properties": {**acct, "id": {"type": "integer"}, "index": {"type": "integer"}}})
    one = {"type": "object", "required": ["account", "id"], "properties": {**acct, "id": {"type": "integer"}}}
    s.tool("mail_draft", lambda a, m: draft(app, a, m), title="写回复草稿", read_only=False, input_schema=one)
    s.tool("mail_draft_save", lambda a, m: draft_save(app, a, m), title="把草稿存进邮箱的「草稿」", read_only=False,
           input_schema={"type": "object", "required": ["account", "id", "text"],
                         "properties": {**acct, "id": {"type": "integer"}, "text": {"type": "string"}}})
    s.tool("mail_change", lambda a, m: change(app, a, m), title="改邮件的建议", read_only=False,
           input_schema={"type": "object", "required": ["account", "ids", "action"],
                         "properties": {**acct, "ids": {"type": "array", "items": {"type": "integer"}},
                                        "action": {"type": "string"}, "remember": {"enum": ["", "sender", "domain", "list"]}}})
    s.tool("mail_sync", lambda a, m: sync(app, a, m), title="同步新邮件", read_only=False,
           input_schema={"type": "object", "required": ["account"], "properties": acct})
    s.tool("mail_job_stop", lambda a, m: job_stop(app, a, m), title="停下正在跑的任务", read_only=False,
           input_schema={"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}})
    one_piece = {"type": "object", "required": ["account", "id"], "properties": {**acct, "id": {"type": "integer"}}}
    s.tool("mail_digest_feedback", lambda a, m: digest_feedback(app, a, m), title="精选：多来点、少来点这类", read_only=False,
           input_schema={**one_piece, "properties": {**one_piece["properties"], "value": {"enum": ["useful", "dismissed", None]}}})
    s.tool("mail_digest_archive", lambda a, m: digest_archive(app, a, m), title="精选：看过了，移出列表", read_only=False,
           input_schema={**one_piece, "properties": {**one_piece["properties"], "on": {"type": "boolean"}}})
    s.tool("mail_digest_open", lambda a, m: digest_open(app, a, m), title="精选：记一次打开", read_only=False,
           input_schema=one_piece)
    s.tool("mail_digest_reading", lambda a, m: digest_reading(app, a, m), title="精选：暂停或接着读订阅", read_only=False,
           input_schema={"type": "object", "required": ["account", "on"], "properties": {**acct, "on": {"type": "boolean"}}})
    s.tool("mail_digest_source", lambda a, m: digest_source(app, a, m), title="精选：加入或移出来源", read_only=False,
           input_schema={"type": "object", "required": ["account", "key", "on"],
                         "properties": {**acct, "key": {"type": "string"}, "on": {"type": "boolean"},
                                        "name": {"type": "string"}}})
    key = {"type": "object", "required": ["account", "key"], "properties": {**acct, "key": {"type": "string"}}}
    s.tool("mail_unsubscribe", lambda a, m: unsubscribe(app, a, m), title="一键退订", read_only=False, destructive=True,
           input_schema={"type": "object", "required": ["account"],
                         "properties": {**acct, "key": {"type": "string"},
                                        "keys": {"type": "array", "items": {"type": "string"}}}})
    s.tool("mail_sender_keep", lambda a, m: sender_keep(app, a, m), title="订阅：留着别再问", read_only=False, input_schema=key)
    s.tool("mail_sender_rule", lambda a, m: sender_rule(app, a, m), title="订阅：这个发件人以后都这样处理", read_only=False,
           input_schema={**key, "required": ["account", "key", "action"],
                         "properties": {**key["properties"], "action": {"enum": ["archive", "trash", "spam"]}}})
    s.tool("mail_sender_history", lambda a, m: sender_history(app, a, m), title="订阅：清掉这个发件人的历史邮件",
           read_only=False, input_schema=key)
    s.tool("mail_sender_reset", lambda a, m: sender_reset(app, a, m), title="订阅：撤回决定，移回建议处理",
           read_only=False, input_schema=key)
    s.tool("mail_sender_unsubscribed", lambda a, m: sender_unsubscribed(app, a, m), title="订阅：标记为已退订",
           read_only=False, input_schema=key)
    s.tool("mail_subscriptions_check", lambda a, m: subscriptions_check(app, a, m), title="检查订阅", read_only=False,
           input_schema={"type": "object", "required": ["account"], "properties": {**acct, "days": {"type": "integer"}}})
    scope = {"scope": {"enum": list(OLD_SCOPES)}}
    groups = {"type": "object", "required": ["account", "keys", "count"],
              "properties": {**acct, **scope, "keys": {"type": "array", "items": {"type": "string"}}, "count": {"type": "integer"}}}
    s.tool("mail_old_scan", lambda a, m: old_scan(app, a, m), title="读旧邮件（收件箱的或归档的）", read_only=False,
           input_schema={"type": "object", "required": ["account"], "properties": {**acct, **scope}})
    s.tool("mail_old_judge", lambda a, m: old_judge(app, a, m), title="让本机模型看旧邮件，或暂停", read_only=False,
           input_schema={"type": "object", "required": ["account", "on"],
                         "properties": {**acct, **scope, "on": {"type": "boolean"}}})
    s.tool("mail_old_archive", lambda a, m: old_archive(app, a, m), title="按组归档旧邮件", read_only=False,
           input_schema={**groups, "properties": {k: v for k, v in groups["properties"].items() if k != "scope"}})
    s.tool("mail_old_trash", lambda a, m: old_trash(app, a, m), title="按组把旧邮件移到已删除", read_only=False,
           input_schema=groups)
    s.tool("mail_old_keep", lambda a, m: old_keep(app, a, m), title="旧邮件：这一组留着别再问", read_only=False,
           input_schema={**key, "properties": {**key["properties"], **scope, "on": {"type": "boolean"}}})
    s.tool("mail_settings", lambda a, m: set_setting(app, a, m), title="改一项设置", read_only=False,
           input_schema={"type": "object", "required": ["key", "value"],
                         "properties": {"key": {"enum": sorted(SETTINGS)}, "value": {}}})
    s.tool("mail_set_name", lambda a, m: set_name(app, a, m), title="改邮箱的显示名", read_only=False,
           input_schema={"type": "object", "required": ["account", "name"], "properties": {**acct, "name": {"type": "string"}}})
    s.tool("mail_set_own", lambda a, m: set_own(app, a, m), title="设为（或不再是）螺壳自己的邮箱", read_only=False,
           input_schema={"type": "object", "required": ["account", "own"], "properties": {**acct, "own": {"type": "boolean"}}})
    s.tool("mail_connect", lambda a, m: connect(app, a, m), title="添加邮箱、改授权码", read_only=False,
           input_schema={"type": "object", "required": ["provider", "address"],
                         "properties": {"key": {"type": "string"}, "provider": {"enum": sorted(providers.PROVIDERS)},
                                        "address": {"type": "string"},
                                        "secret": {"type": "string", "writeOnly": True, "maxLength": SECRET_MOST},
                                        "host": {"type": "string"}, "port": {"type": "integer"}}})
    s.tool("mail_remove", lambda a, m: remove(app, a, m), title="移除邮箱", read_only=False, destructive=True,
           input_schema={"type": "object", "required": ["account"], "properties": {**acct}})
    s.tool("mail_forget", lambda a, m: forget(app, a, m), title="清除邮件助手的全部数据", read_only=False, destructive=True,
           input_schema={"type": "object", "properties": {"memory": {"type": "boolean"}}})
    s.tool("mail_learn", lambda a, m: learn(app, a, m), title="从邮件中了解你：开或停", read_only=False,
           input_schema={"type": "object", "required": ["account", "on"], "properties": {**acct, "on": {"type": "boolean"}}})
    s.tool("mail_site", lambda a, m: site(app, a, m), title="你的网站和博客：添加或删除", read_only=False,
           input_schema={"type": "object", "required": ["account", "op"],
                         "properties": {**acct, "op": {"enum": ["add", "remove"]}, "url": {"type": "string"},
                                        "id": {"type": "integer"}}})
    s.tool("mail_rule_add", lambda a, m: rule_add(app, a, m), title="添加规则", read_only=False,
           input_schema={"type": "object", "required": ["account", "kind", "value", "action"],
                         "properties": {**acct, "kind": {"type": "string"}, "value": {"type": "string"},
                                        "action": {"type": "string"}}})
    s.tool("mail_rule_delete", lambda a, m: rule_delete(app, a, m), title="删除规则", read_only=False,
           input_schema={"type": "object", "required": ["account", "id"], "properties": {**acct, "id": {"type": "integer"}}})
    s.tool("mail_rule_preview", lambda a, m: rule_preview(app, a, m), title="预演 Gmail 过滤器", read_only=False,
           input_schema={"type": "object", "required": ["account", "id"], "properties": {**acct, "id": {"type": "integer"}}})
    s.tool("mail_gmail_filters", lambda a, m: gmail_filters(app, a, m), title="导出 Gmail 过滤器", read_only=False,
           input_schema={"type": "object", "required": ["account"], "properties": {**acct, "all": {"type": "boolean"}}})
