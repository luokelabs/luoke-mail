"""Whose turn it is in a conversation (2026-10-17: 「需要你处理的」 -- the user waiting on someone who
hasn't answered, a long thread that is stuck with somebody). The model reads the last few messages of one thread
and says who has to act now -- the other side, the user, or nobody (it is settled) -- since when, and in a short
phrase what is waiting. One thread per request (task `thread`).

Mail text is untrusted data, as everywhere: the prompt says so, the answer is constrained by a schema, and
nothing here acts on the mailbox."""
from __future__ import annotations

import json
import re

from . import engines, prompts

STATES = {"them": "在等对方", "you": "现在等你", "done": "已了结"}
MOST_MESSAGES = 6          # the last ones of a thread: enough to see who answered whom
MESSAGE_CHARS = 600

# what the model is asked for, and what it means here: "them" / "you" were read by a 9B model as "who is
# waiting" rather than "whose turn" (internal-1019: four conversations owed by the user came back as them, the
# reason saying 「等用户……」); whose turn it is reads one way only. The answers are the schema's
# (prompts/thread.schema.json; a test checks they are the same)
ANSWERS = {"user_turn": "you", "other_turn": "them", "done": "done"}


def build_messages(thread: list[dict], n: int | None = None) -> list[dict]:
    """`thread`: [{"me": bool, "name", "date" (YYYY-MM-DD...), "subject", "text"}], oldest first; the last `n`."""
    shown = thread[-(n or MOST_MESSAGES):]
    entries = [{"n": i, "date": (m.get("date") or "")[:10], "me": bool(m.get("me")), "name": m.get("name") or "",
                "subject": m.get("subject") or "", "text": " ".join((m.get("text") or "").split())[:MESSAGE_CHARS]}
               for i, m in enumerate(shown, 1)]
    p = prompts.load("thread")
    return p.messages({"states": p.described("state")}, messages=entries, last_me=bool(shown and shown[-1].get("me")))


def parse(content: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    obj = json.loads(m.group(0) if m else text)
    if not isinstance(obj, dict):
        raise ValueError("答复不是 JSON 对象")
    state = str(obj.get("state") or "").strip().lower()
    state = ANSWERS.get(state, state)
    if state not in STATES:
        raise ValueError("state 不在可选值里")
    since = str(obj.get("since") or "").strip()[:10]
    since = since if re.fullmatch(r"\d{4}-\d{2}-\d{2}", since) else ""
    what = " ".join(str(obj.get("what") or "").split())[:40]
    return {"state": state, "since": since if state != "done" else "", "what": what if state != "done" else ""}


def judge(thread: list[dict], link: engines.Link) -> dict:
    """{"state", "since", "what"} for one thread; after two unusable answers, "done" -- a failure never says
    the user is the one holding things up."""
    n = link.fit("thread", lambda k: build_messages(thread, k), min(len(thread), MOST_MESSAGES), least=1)
    messages = build_messages(thread, n)
    for _ in range(2):
        content, _, _ = link.chat("thread", messages, prompts.load("thread").schema(), items=1, max_tokens=300)
        try:
            return parse(content)
        except (ValueError, TypeError):
            continue
    return {"state": "done", "since": "", "what": ""}


# ---- which conversations to look at ------------------------------------------------------------------------------
_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|wg|sv|回复|答复|转发)\s*[:：]\s*|\[[^\]]*\]\s*)+", re.I)
WAIT_DAYS = 3              # the user wrote last and has heard nothing for this long: worth a reminder (a default)
LONG = 3                   # messages: a conversation long enough to say where it stands


def subject_key(subject: str | None) -> str:
    return " ".join(_PREFIX.sub("", subject or "").split()).lower()


def collect(received: list[dict], sent: list[dict], me: set[str], today: str, own_text) -> list[dict]:
    """Conversations worth a look: [{"key", "with" (address), "name", "subject", "messages" (oldest first),
    "last_mid", "items" (ids of the received ones on the list)}]. A conversation is a subject (without Re: / 回复：)
    with one other person. Kept: those where the user wrote last and has waited WAIT_DAYS or more, and the long
    ones (LONG messages or more). `own_text(mail)`: the writer's own words, without what they quoted."""
    convs: dict[tuple, list[dict]] = {}
    for m in received:
        who = (m.get("from_addr") or "").lower()
        if who and who not in me and not m.get("bulk"):
            convs.setdefault((subject_key(m.get("subject")), who), []).append({**m, "me": False})
    for m in sent:
        for to in m.get("to") or []:
            addr = (to.get("addr") if isinstance(to, dict) else str(to)).lower()
            if addr and addr not in me:
                convs.setdefault((subject_key(m.get("subject")), addr), []).append({**m, "me": True})
    out = []
    for (key, who), msgs in convs.items():
        if not key:
            continue
        msgs.sort(key=lambda m: m.get("date") or "")
        last = msgs[-1]
        waiting = last["me"] and (last.get("date") or "")[:10] <= _days_before(today, WAIT_DAYS)
        if not (waiting or len(msgs) >= LONG):
            continue
        other = next((m for m in msgs if not m["me"]), None)
        out.append({"key": f"{key}|{who}", "with": who, "name": (other or {}).get("from_name") or _name_of(last, who) or who,
                    "subject": last.get("subject") or "（无主题）", "last_mid": last.get("message_id") or "",
                    "last_date": (last.get("date") or "")[:10], "waiting": waiting,
                    "items": [m["id"] for m in msgs if not m["me"] and m.get("id")],
                    "messages": [{"me": m["me"], "name": m.get("from_name"), "date": m.get("date"), "subject": m.get("subject"),
                                  "text": own_text(m)} for m in msgs]})
    return sorted(out, key=lambda c: c["last_date"], reverse=True)


def _days_before(today: str, n: int) -> str:
    import datetime as dt
    return (dt.date.fromisoformat(today[:10]) - dt.timedelta(days=n)).isoformat()


def _name_of(mail: dict, addr: str) -> str | None:
    for to in mail.get("to") or []:
        if isinstance(to, dict) and (to.get("addr") or "").lower() == addr:
            return to.get("name") or None
    return None
