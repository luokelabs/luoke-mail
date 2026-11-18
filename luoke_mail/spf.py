"""The sending services a domain names in its own SPF record (`include:` and `redirect=`, followed as the record does,
at most 10 lookups): mail one of them sends for the domain fails DMARC when the service signs with its own name,
though the domain's owner has said it may send (2026-11-16: a vendor's newsletter, sent and signed by a marketing service
its own record includes). mailbox.sender_check calls that `relay`.

DNS through the system's `dig`; no answer (offline, no dig, a timeout, the time for DNS used up) is an empty set, so a
failure stays one. The names come from mail and from records anyone can write: only a well-formed host name is asked
(review 2026-11-16: `include:-f.netrc` made dig read a file), and DNS gets a few seconds a check and a budget over ten
minutes -- the lookups are made because a mail came, much as a remote picture would be fetched. Five seconds is
for all of one mail's lookups, each ending by then."""
from __future__ import annotations

import re
import subprocess
import threading
import time

LOOKUPS = 10                    # what RFC 7208 allows one check
KEEP = 3600                     # seconds an answer is kept
PER_CHECK = 5                   # seconds of DNS one check may take
BUDGET, WINDOW = 20, 600        # seconds of DNS in any ten minutes, for all checks
_cache: dict[str, tuple[float, set[str]]] = {}
_spent: list[tuple[float, float]] = []          # (when, seconds) of each lookup in the window
_lock = threading.Lock()
_NAME = re.compile(r"(?=.{4,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")


def host_name(name: str) -> str:
    """`name` lowercased if it is a host name DNS can be asked about (labels of letters, digits, `_`, `-`, none starting
    or ending with `-`, at least two, 253 at most in all), else ""."""
    name = (name or "").lower().strip(".")
    return name if _NAME.fullmatch(name) else ""


def _budget_left() -> bool:
    now = time.monotonic()
    with _lock:
        _spent[:] = [x for x in _spent if now - x[0] < WINDOW]
        return sum(s for _, s in _spent) < BUDGET


def _txt(name: str, wait: float = 4) -> list[str]:
    """The TXT records of `name` (each record's strings joined), [] when there is no answer within `wait` seconds."""
    t = time.monotonic()
    try:
        out = subprocess.run(["/usr/bin/dig", "+short", "+time=2", "+tries=1", "-t", "TXT", "-q", name],
                             capture_output=True, text=True, timeout=max(min(wait, 4), 0.5)).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    with _lock:
        _spent.append((t, time.monotonic() - t))
    return ["".join(re.findall(r'"((?:[^"\\]|\\.)*)"', line)) for line in out.splitlines() if line.startswith('"')]


def _record(name: str, wait: float) -> str | None:
    return next((t for t in _txt(name, wait) if t.lower().startswith("v=spf1 ") or t.lower() == "v=spf1"), None)


def deadline() -> float:
    """When a check that starts now must be done: one mail's lookups, all of them, take PER_CHECK at most."""
    return time.monotonic() + PER_CHECK


def services(domain: str, until: float | None = None) -> set[str]:
    """The domains `domain`'s SPF record includes or redirects to, followed down (lowercase, full names). `until`:
    the check's deadline (deadline()), shared when one mail asks about more than one domain -- review 2026-11-17: the
    From domain and then its organisation, each with its own five seconds and a lookup started just before the end,
    came to some 18 seconds."""
    domain = host_name(domain)
    if not domain:
        return set()
    with _lock:
        hit = _cache.get(domain)
        if hit and time.time() - hit[0] < KEEP:
            return hit[1]
    out: set[str] = set()
    until = until or deadline()
    todo, seen, whole = [domain], set(), True
    while todo and len(seen) < LOOKUPS:
        left = until - time.monotonic()
        if left < 0.5 or not _budget_left():
            whole = False                         # what was found so far, not kept
            break
        name = todo.pop(0)
        if name in seen:
            continue
        seen.add(name)
        terms = (_record(name, left) or "").split()[1:]                  # a lookup ends by the deadline too
        # redirect= counts only where the record has no `all` of its own (RFC 7208 6.1)
        ends = any(re.fullmatch(r"[+?~-]?all", x, re.I) for x in terms)
        for tok in terms:
            # only what lets mail through: `+include` / `include`; not -include, ~include, ?include
            m = re.fullmatch(r"\+?include:(\S+)", tok, re.I) or (None if ends else re.fullmatch(r"redirect=(\S+)", tok, re.I))
            found = host_name(m.group(1)) if m else ""
            if found:
                out.add(found)
                todo.append(found)
    if whole:
        with _lock:
            _cache[domain] = (time.time(), out)
    return out
