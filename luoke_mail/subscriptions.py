"""Subscription health: who sends you the most mail you never read, and how to stop it.

No model involved -- just headers and the \\Seen flag of recent inbox mail. Unsubscribing:
- RFC 8058 one-click (List-Unsubscribe-Post) -> we POST it for the user, after an explicit click;
- an https link only -> the page opens in the user's browser;
- mailto only -> the user's mail app opens with the address filled in.
Senders that ever sent something flagged suspicious are never unsubscribed: answering a scammer's
"unsubscribe" link confirms your address is live.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from collections import Counter

from . import __version__

MIN_COUNT = 3
MAX_READ_RATIO = 0.2


def sender_key(h: dict) -> str:
    return ("list:" + h["list_id"]) if h.get("list_id") else ("from:" + (h.get("from_addr") or "?"))


def _method(hs: list[dict]) -> tuple[str | None, dict]:
    """Best way to unsubscribe, preferring standard headers over links found in the body."""
    for kind in ("one_click", "web", "mailto"):
        for h in hs:
            if kind == "one_click" and h.get("unsub_one_click"):
                return kind, {"unsub_http": h["unsub_http"]}
            if kind == "web" and h.get("unsub_http"):
                return kind, {"unsub_http": h["unsub_http"]}
            if kind == "mailto" and h.get("unsub_mailto"):
                return kind, {"unsub_mailto": h["unsub_mailto"]}
    for h in hs:
        b = h.get("body_unsub") or {}
        if b.get("kind") == "link":
            return "body_link", {"body_link": b["url"]}
        if b.get("kind") == "reply":
            return "reply", {"reply_keyword": b["keyword"]}
    return None, {}


def summarize(headers: list[dict], suspicious_senders: set[str], days: int) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for h in headers:
        groups.setdefault(sender_key(h), []).append(h)
    out = []
    for key, hs in groups.items():
        hs.sort(key=lambda h: h.get("date") or "", reverse=True)
        bulk = any(h.get("bulk") for h in hs)
        if not bulk and len(hs) < MIN_COUNT:
            continue
        count = len(hs)
        read = sum(1 for h in hs if h.get("seen"))
        method, how = _method(hs)
        addrs = {h.get("from_addr") for h in hs}
        sus = any(a in suspicious_senders for a in addrs)
        name = Counter(h.get("from_name") or h.get("from_addr") for h in hs).most_common(1)[0][0]
        ratio = read / count if count else 0
        if sus:
            verdict, why = "never", "此发件人发送过可疑邮件。请勿退订或点击其中的链接，建议移到垃圾邮件。"
        elif count >= MIN_COUNT and ratio <= MAX_READ_RATIO and method:
            verdict = "unsubscribe"
            why = f"{days} 天内收到 {count} 封，已读 {read} 封。"
        elif count >= MIN_COUNT and ratio <= MAX_READ_RATIO:
            verdict = "filter"
            why = f"{days} 天内收到 {count} 封，已读 {read} 封，但没有退订方式。"
        else:
            verdict, why = "keep", f"{days} 天内收到 {count} 封，已读 {read} 封。"
        out.append({
            "key": key, "name": name, "from_addr": hs[0].get("from_addr"), "from_domain": hs[0].get("from_domain"),
            "list_id": hs[0].get("list_id"), "count": count, "read": read, "last_date": hs[0].get("date"),
            "bytes": sum(h.get("size") or 0 for h in hs),
            "subjects": [h.get("subject") for h in hs[:3]], "method": method,
            "unsub_http": how.get("unsub_http"), "unsub_mailto": how.get("unsub_mailto"),
            "body_link": how.get("body_link"), "reply_keyword": how.get("reply_keyword"),
            "suspicious": sus, "verdict": verdict, "why": why,
        })
    order = {"unsubscribe": 0, "filter": 1, "never": 2, "keep": 3}
    out.sort(key=lambda s: (order[s["verdict"]], -s["bytes"], -s["count"]))
    return out


def recent_subjects(headers: list[dict], keys: set[str], n: int = 8) -> dict[str, list[str]]:
    """The latest `n` subjects of each sender in `keys` (for the model's look before suggesting unsubscribe)."""
    out: dict[str, list[tuple[str, str]]] = {}
    for h in headers:
        k = sender_key(h)
        if k in keys:
            out.setdefault(k, []).append((h.get("date") or "", h.get("subject") or ""))
    return {k: [s for _, s in sorted(v, reverse=True)[:n]] for k, v in out.items()}


def needs_body_check(headers: list[dict], suspicious_senders: set[str]) -> dict[str, dict]:
    """Newest header of each sender that has no unsubscribe header (and isn't suspicious):
    the scan then reads just that one body looking for a hidden unsubscribe link."""
    groups: dict[str, list[dict]] = {}
    for h in headers:
        groups.setdefault(sender_key(h), []).append(h)
    out = {}
    for key, hs in groups.items():
        if any(h.get("unsub_http") or h.get("unsub_mailto") for h in hs):
            continue
        if any(h.get("from_addr") in suspicious_senders for h in hs):
            continue
        if not any(h.get("bulk") for h in hs) and len(hs) < MIN_COUNT:
            continue
        out[key] = max(hs, key=lambda h: h.get("date") or "")
    return out


def one_click(url: str, timeout: float = 15) -> tuple[bool, str]:
    """RFC 8058: POST "List-Unsubscribe=One-Click" to the https URL from the header."""
    if not url.lower().startswith("https://"):
        return False, "一键退订地址不是 https，已拒绝。"
    req = urllib.request.Request(url, data=b"List-Unsubscribe=One-Click", method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": f"luoke-mail/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 400, f"对方服务器返回 HTTP {r.status}"
    except urllib.error.HTTPError as e:
        e.close()
        return False, f"对方服务器返回 HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, f"连不上对方服务器：{type(e).__name__}"
