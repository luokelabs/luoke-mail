"""了解你: understanding the user from their mail, so they don't have to write about themselves.

Who said it decides what an inference may do (luoke-mem's principles):
- mail the user sent is their own words: facts drawn from it take effect once two messages agree;
- mail they received is what someone else said (and could be a scam): what is drawn from it goes to the memory,
  which decides how sure it is (top-level ADR 0015), only from organisations that wrote several times over months;
- what the user did shows a habit, which the memory works out from the records.
Nothing is held back for privacy on this machine (top-level ADR 0013): only credentials -- passwords, codes, keys
and tokens, full card and ID numbers -- are masked before anything is written (memory.mask), and one-off values
(an amount, a meeting's time) are not facts about the user. Sensitive context is marked for the memory, which
decides what needs the user's yes. Mail text is data; instructions inside it are ignored."""
from __future__ import annotations

import re

from . import engines, prompts
from .memory import marks, mask, sensitive  # noqa: F401 - credentials masked, whatever the model says
from .triage import _loads

SENT_BATCH = 8
SENT_CHARS = 800            # of the user's own text per message (quoted replies already removed)
RECEIVED_BATCH = 10
# the topics a fact may have are the schema's (its x-describe: the model is offered them, not held to them)
FACT_TOPICS = tuple(prompts.load("learn-sent").values("topic"))
SINGLE = ("姓名", "署名", "职位", "单位", "城市", "语言")      # one value at a time: a newer one replaces the older
LIFE_TOPICS = tuple(prompts.load("learn-orgs").values("topic"))


# where someone else's words begin in a sent message: a forward or a quote, wherever it starts (the parser keeps a
# forward with only a line above it -- right for sorting, where the forwarded mail is the point; not here)
_NOT_MINE = [re.compile(r"^-{2,}\s*(Original Message|原始邮件|Forwarded message|转发的?邮件(信息)?)\s*-{2,}", re.M | re.I),
             re.compile(r"^Begin forwarded message:?\s*$", re.M | re.I),
             re.compile(r"^On .{5,200} wrote:\s*$", re.M), re.compile(r"^在 .{3,200}写道[：:]\s*$", re.M),
             re.compile(r"^(From|发件人)[：:].+\n(Sent|Date|发送时间|时间)[：:]", re.M)]


def own_text(mail: dict) -> str:
    """What the user wrote -- not what they forwarded or quoted (someone else's words: the model must not take
    them for the user's) -- the start (what it is about) and the end (where the signature is)."""
    body = (mail.get("body") or "").strip()
    cut = min([m.start() for m in (p.search(body) for p in _NOT_MINE) if m] or [len(body)])
    if cut < len(body):
        body = body[:cut].strip() or "（转发，没有附言）"
    if len(body) <= SENT_CHARS:
        return body
    return body[:SENT_CHARS - 200] + "\n…\n" + body[-200:]


def _clean(v, limit: int) -> str:
    return " ".join(str(v or "").split())[:limit]


# ---- the user's own mail ----------------------------------------------------------------------------------
def sent_messages(mails: list[dict], me: str) -> list[dict]:
    p = prompts.load("learn-sent")
    entries = [{"n": i, "date": (m.get("date") or "")[:10], "to": "、".join((m.get("to") or [])[:4]),
                "cc": "、".join((m.get("cc") or [])[:4]), "subject": m.get("subject") or "", "text": own_text(m)}
               for i, m in enumerate(mails, 1)]
    return p.messages({"topics": p.described("topic")}, me=me, mails=entries)


# how a fact names its organisation when not by the very same string: 「在示例商城有账户」 for shop.example
_NAMED = re.compile(r"(?:在|用|于|是|通过|持有)([^\s，。、：:；;]{2,}?)(?:的|有|开|办|注册|订|里|上|账)")
_GENERIC = {"www", "com", "net", "org", "co", "jp", "cn", "hk", "mail", "info", "news", "the", "inc", "ltd"}


def names_org(text: str, who: str) -> bool:
    """Whether a fact already says which organisation it is about -- then 「shop.example：」 is not put before it
    (review, 10-22: 「shop.example：在示例商城有账户」). The same name, a word of it, or 在 / 用 + a name."""
    low = text.casefold()
    if who.casefold() in low:
        return True
    if any(len(w) >= 3 and w not in _GENERIC and w in low for w in re.split(r"[^0-9a-z\u4e00-\u9fff]+", who.casefold())):
        return True
    return bool(_NAMED.search(text))


def parse_facts(content: str, count: int, topics=FACT_TOPICS) -> list[dict]:
    obj = _loads(content)
    items = obj.get("facts") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        raise ValueError("输出里没有 facts")
    out = []
    for f in items:
        if not isinstance(f, dict):
            continue
        # an organisation's name whole (20 characters cut 「Example Cloud Services Co., Ltd.」 short: review, 10-22)
        topic, text, who = _clean(f.get("topic"), 8), _clean(f.get("text"), 60), _clean(f.get("who"), 40)
        ns = sorted({int(x) for x in (f.get("n") or []) if isinstance(x, (int, float)) and 1 <= int(x) <= count})
        text, who = mask(text), mask(who)                          # a credential is hidden, the fact kept (ADR 0013)
        if topic not in topics or not text or not ns:
            continue
        if topics is LIFE_TOPICS and who and text.strip(" ：:") == who:   # 「shop.example」 alone says nothing
            continue
        if topics is LIFE_TOPICS and who and not names_org(text, who):     # 「持有信用卡」 says nothing without the bank
            text = f"{who}：{text}"
        # 「其他」 is a catch-all, not a topic: one fact on it must not take the place of another (luoke-mem 0.19.1)
        key = topic if topic in SINGLE or not (who or topic == "其他") else f"{topic}:{who or text}"
        out.append({"topic": key, "text": text, "n": ns})
    return out


def from_sent(mails: list[dict], me: str, link: engines.Link) -> tuple[list[dict] | None, int]:
    """Facts in the first of the user's sent mails -- as many as one request may carry: ([{"topic", "text",
    "n": [message positions]}], how many mails it read). None for the facts when the model gave no usable answer."""
    p = prompts.load("learn-sent")
    count = link.fit("learn-sent", lambda n: sent_messages(mails[:n], me), len(mails), least=1)
    mails = mails[:count]
    messages = sent_messages(mails, me)
    for _ in range(2):
        content, _, _ = link.chat("learn-sent", messages, p.schema(), items=len(mails), max_tokens=900)
        try:
            return parse_facts(content, len(mails)), count
        except ValueError:
            messages = link.again("learn-sent", messages, content, p.retry)
    return None, count                    # no usable answer: not the same as nothing found (an empty list)


# ---- what organisations wrote to the user (someone else's words) ------------------------------------------
def received_messages(senders: list[dict]) -> list[dict]:
    p = prompts.load("learn-orgs")
    entries = [{"n": i, "name": s["name"], "addr": s["addr"], "count": s["count"], "first": s["first"][:7],
                "last": s["last"][:7], "subjects": list(s["subjects"][:6])} for i, s in enumerate(senders, 1)]
    return p.messages({"topics": p.described("topic")}, senders=entries)


def from_received(senders: list[dict], link: engines.Link) -> tuple[list[dict] | None, int]:
    """Facts about the first organisations -- as many as one request may carry, at most RECEIVED_BATCH -- and how
    many it looked at. None for the facts when the model gave no usable answer."""
    senders = senders[:link.batch("learn-orgs", RECEIVED_BATCH)]
    p = prompts.load("learn-orgs")
    count = link.fit("learn-orgs", lambda n: received_messages(senders[:n]), len(senders), least=1)
    senders = senders[:count]
    messages = received_messages(senders)
    for _ in range(2):
        content, _, _ = link.chat("learn-orgs", messages, p.schema(), items=len(senders), max_tokens=700)
        try:
            return parse_facts(content, len(senders), LIFE_TOPICS), count
        except ValueError:
            messages = link.again("learn-orgs", messages, content, p.retry)
    return None, count
