"""The editor: from everything the reader found worth a look, choose the few worth the user's time.

Reading scores each newsletter on its own, and on its own a model is generous: in a real backlog 43% of the
pieces came out "worth a look". An editor compares instead: candidates go to the model in batches, it keeps the
best of each batch, and the winners meet again until the quota is left (WEEK_QUOTA for the last seven days,
MONTH_QUOTA for each earlier month). How many pieces are chosen is set by design, not by how strict the model
happens to be. What counts, in order: the user's interests (and dislikes), weight (analysis, research and real
changes over news flashes, prices and promotions), how many newsletters carried the story, and the pieces the
user saved (收藏) or marked 没兴趣. Piece text came from newsletters: untrusted data, never instructions."""
from __future__ import annotations

import math

from . import engines, prompts
from .triage import _loads

WEEK_QUOTA = 20
MONTH_QUOTA = 10
BATCH = 30

def messages(cands: list[dict], k: int, taste: str, liked: list[str], disliked: list[str]) -> list[dict]:
    entries = [{"n": i, "title": it["title"], "summary": it.get("summary") or "", "source": it.get("source") or "",
                "date": (it.get("date") or "")[:10], "also": it.get("also") or 0} for i, it in enumerate(cands, 1)]
    return prompts.load("digest-pick").messages({"k": k}, taste=taste or "", liked=list(liked), disliked=list(disliked),
                                                cands=entries)


def _fallback(cands: list[dict], k: int) -> list[tuple[dict, str]]:
    """Without a usable answer: the reader's score, then how widely it was carried, then the newest."""
    best = sorted(cands, key=lambda i: (i.get("feedback") == "useful", i["score"], i.get("also") or 0, i.get("date") or ""),
                  reverse=True)
    return [(i, i.get("why") or "") for i in best[:k]]


def choose(cands: list[dict], k: int, taste: str, liked: list[str], disliked: list[str],
           link: engines.Link) -> list[tuple[dict, str]]:
    """One round on at most BATCH candidates: the k worth reading, each with why. Raises EngineError when the
    model service can't be reached; an unusable answer falls back to the reader's own ranking."""
    if len(cands) <= k:
        return [(i, i.get("why") or "") for i in cands]
    schema = prompts.load("digest-pick").schema()
    content, _, _ = link.chat("digest-pick", messages(cands, k, taste, liked, disliked), schema, items=k,
                              max_tokens=60 + 60 * k)
    try:
        obj = _loads(content)
        out, seen = [], set()
        for p in obj.get("picks") or []:
            n = int(p.get("n"))
            if 1 <= n <= len(cands) and n not in seen:
                seen.add(n)
                out.append((cands[n - 1], " ".join(str(p.get("why") or "").split())[:60]))
    except (ValueError, TypeError, AttributeError):
        out = []
    # fewer than asked is an answer (what the user is not interested in is left out: "exactly k" made the model pad
    # with it -- prompt review, 10-22); none at all is not, and the reader's ranking stands in
    if not out:
        return _fallback(cands, k)
    return out[:k]


def edit(cands: list[dict], quota: int, taste: str, liked: list[str], disliked: list[str], link: engines.Link,
         stop=None) -> list[tuple[dict, str]] | None:
    """Rounds of batches until few enough are left to compare at once, then the final choice. Each round keeps
    about twice the quota overall, so a candidate is never thrown out only because its batch happened to be strong.
    A batch is also no bigger than one request may carry (the hub's character limit): when even the last few
    are too long to compare at once, the last round's picks are the choice.
    `stop()` is asked between batches (a month can take a dozen model calls): None when it said stop."""
    def room(chunk: list[dict], k: int) -> int:
        """How many of `chunk` one request can compare."""
        return link.fit("digest-pick", lambda n: messages(chunk[:n], min(k, n), taste, liked, disliked), len(chunk), least=2)

    pool, last = list(cands), False
    while True:
        whole = min(len(pool), max(BATCH, 2 * quota))
        if not last and len(pool) <= whole and room(pool, quota) >= len(pool):
            if stop and stop():
                return None
            return choose(pool, min(quota, len(pool)), taste, liked, disliked, link)
        last = last or len(pool) <= 2 * quota           # few enough: only too long to compare at once
        share = (quota if last else 2 * quota) / len(pool)
        winners, i = [], 0
        while i < len(pool):
            if stop and stop():
                return None
            chunk = pool[i:i + BATCH]
            chunk = chunk[:room(chunk, max(2, math.ceil(len(chunk) * share)))]
            i += len(chunk)
            k = max(1 if last else 2, math.ceil(len(chunk) * share))
            winners += choose(chunk, k, taste, liked, disliked, link)
        if last:
            return winners[:quota]
        if len(winners) >= len(pool):                    # nothing was narrowed (tiny batches): this is the choice
            last = True
            continue
        pool = [it for it, _ in winners]
