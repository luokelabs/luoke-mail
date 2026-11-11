"""订阅精选: the model reads subscription mail for the user and keeps only what is worth their time.

A newsletter holds many pieces (a morning brief lists a dozen stories), so the unit is the piece, not the
email. Each piece is scored against what the user said they care about (关注的话题 / 不感兴趣 in luoke-mem)
and the pieces they saved (收藏) or marked 没兴趣, and marked lasting or not: in a backlog of old mail only lasting
pieces are still worth reading, while this week's news is worth it this week. Newsletter text is untrusted
data: it never changes what the model is asked to do, and nothing it says becomes part of the memory."""
from __future__ import annotations

import datetime as dt
import email
import re
from email import policy
from html.parser import HTMLParser
from urllib.parse import urlparse

from . import engines, prompts
from .mailbox import parse_message
from .triage import _loads

TEXT_LIMIT = 12000           # characters of newsletter text the model reads (prefill time on small machines)
SHORT_TEXT = 4000            # what is tried when the model service has no memory left for the full text
LINK_LIMIT = 40
MAX_ITEMS = 8
PICK = 2                     # score from which a piece is shown
FEEDBACK_EXAMPLES = 8        # of each kind, newest first

_INVISIBLE = re.compile("[​‌‍⁠﻿­͏؜᠎]")
_SKIP_LINK = re.compile(r"unsubscribe|opt-?out|preferences|manage|privacy|terms|退订|取消订阅|隐私|偏好设置|"
                        r"view.?in.?browser|在浏览器|浏览器中查看|/share|sharer|intent/tweet|mailto:", re.I)


# ---- newsletter text: words and numbered links ------------------------------------------------------------
class _Reader(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "table", "section", "article", "blockquote"}
    SKIP = {"script", "style", "head", "title", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[dict] = []
        self.skip = 0
        self.anchor: tuple[str, int] | None = None

    def link(self, url: str, text: str) -> int | None:
        if not url.lower().startswith(("http://", "https://")) or _SKIP_LINK.search(url) or _SKIP_LINK.search(text):
            return None
        for l in self.links:
            if l["url"] == url:
                return l["n"]
        if len(self.links) >= LINK_LIMIT:
            return None
        self.links.append({"n": len(self.links) + 1, "text": text[:60], "url": url, "host": urlparse(url).hostname or ""})
        return len(self.links)

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self.anchor = (href.strip(), len(self.parts))
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "a" and self.anchor:
            href, start = self.anchor
            self.anchor = None
            text = " ".join("".join(self.parts[start:]).split())
            n = self.link(href, text) if text else None
            if n:
                self.parts.append(f"〔{n}〕")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _tidy(text: str) -> str:
    text = _INVISIBLE.sub("", text).replace(" ", " ")
    lines = [" ".join(l.split()) for l in text.splitlines()]
    out, blank = [], False
    for l in lines:
        if not l:
            blank = bool(out)
            continue
        out.append(("\n" if blank else "") + l)
        blank = False
    return "\n".join(out)


def read(raw: bytes) -> dict:
    """Headers, readable text with 〔n〕 after each link, and the numbered links."""
    m = parse_message(raw)
    msg = email.message_from_bytes(raw, policy=policy.default)
    html = plain = ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/html", "text/plain"):
            continue
        try:
            body = part.get_content()
        except Exception:  # noqa: BLE001 - unknown charset: best effort
            body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if ctype == "text/html":
            html += body
        else:
            plain += body
    r = _Reader()
    if html:
        try:
            r.feed(html)
            r.close()
        except Exception:  # noqa: BLE001 - malformed HTML
            r.parts = [re.sub(r"<[^>]+>", " ", html)]
        text = "".join(r.parts)
    else:
        def number(mt):
            url = mt.group(0).rstrip(").,;，。")
            line = plain[max(0, plain.rfind("\n", 0, mt.start())):mt.start()].strip()
            n = r.link(url, line[-60:] or url)
            return f"〔{n}〕" if n else ""
        text = re.sub(r"https?://[^\s<>\"']+", number, plain)
    return {"message_id": m.get("message_id"), "subject": m.get("subject"), "from_name": m.get("from_name"),
            "from_addr": m.get("from_addr"), "date": m.get("date"), "text": _tidy(text)[:TEXT_LIMIT], "links": r.links}


# ---- reading one newsletter -------------------------------------------------------------------------------
def build_messages(mail: dict, taste: str, liked: list[str], disliked: list[str], today: dt.date,
                   language: str = "中文") -> list[dict]:
    links = [{"n": l["n"], "text": l["text"], "host": l["host"]} for l in mail.get("links") or []]
    return prompts.load("digest-read").messages(
        {"language": language}, today=today.isoformat(), taste=taste or "", liked=list(liked), disliked=list(disliked),
        from_name=mail.get("from_name") or "", from_addr=mail.get("from_addr") or "", date=mail.get("date") or "",
        subject=mail.get("subject") or "", text=mail.get("text") or "", links=links)


def _clean(v, limit: int) -> str:
    return " ".join(str(v or "").split())[:limit]


def parse(content: str, links: list[dict]) -> list[dict]:
    obj = _loads(content)
    items = obj.get("items") if isinstance(obj, dict) else obj if isinstance(obj, list) else None
    if not isinstance(items, list):
        raise ValueError("输出里没有 items")
    by_n = {l["n"]: l for l in links}
    out = []
    for it in items[:MAX_ITEMS]:
        if not isinstance(it, dict) or not _clean(it.get("title"), 60):
            continue
        try:
            score = max(0, min(3, int(it.get("score") or 0)))
        except (TypeError, ValueError):
            score = 0
        try:
            link = by_n.get(int(it["link"])) if it.get("link") is not None else None
        except (TypeError, ValueError):
            link = None
        topics = [_clean(t, 20) for t in (it.get("topics") or []) if _clean(t, 20)][:3] if isinstance(it.get("topics"), list) else []
        out.append({"title": _clean(it.get("title"), 60), "summary": _clean(it.get("summary"), 200),
                    "url": link["url"] if link else None, "host": link["host"] if link else None, "topics": topics,
                    "lasting": bool(it.get("lasting")), "score": score, "why": _clean(it.get("why"), 80) if score >= PICK else ""})
    return out


def extract(mail: dict, taste: str, liked: list[str], disliked: list[str], today: dt.date, link: engines.Link,
            language: str = "中文") -> list[dict] | None:
    """The pieces of one newsletter, scored; None after two unusable answers. Raises EngineError when the model
    service can't be reached (the caller tries again later). A text longer than one request may carry is cut:
    the first part usually holds the main pieces."""
    text = mail.get("text") or ""
    schema = prompts.load("digest-read").schema()

    def build(n: int) -> list[dict]:
        return build_messages({**mail, "text": text[:n]} if n < len(text) else mail, taste, liked, disliked, today, language)
    messages = build(link.fit("digest-read", build, len(text)))
    try:
        content, _, _ = link.chat("digest-read", messages, schema, max_tokens=1400, timeout=600)
    except engines.MemoryFull as ex:
        if not ex.prompt_too_big or len(text) <= SHORT_TEXT:
            raise
        # too long a newsletter for the memory left right now (a model service used directly)
        messages = build(SHORT_TEXT)
        content, _, _ = link.chat("digest-read", messages, schema, max_tokens=1400, timeout=600)
    try:
        return parse(content, mail.get("links") or [])
    except ValueError:                           # one more try, telling the model what was wrong
        messages = link.again("digest-read", messages, content, prompts.load("digest-read").retry)
        content, _, _ = link.chat("digest-read", messages, schema, max_tokens=1400, timeout=600)
    try:
        return parse(content, mail.get("links") or [])
    except ValueError:
        return None


# ---- what to show: picks, best first, the same story once ---------------------------------------------------
def _grams(s: str) -> set[str]:
    s = re.sub(r"[\W_]+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def similar(a: str, b: str) -> bool:
    ga, gb = _grams(a), _grams(b)
    return len(ga & gb) / max(1, len(ga | gb)) >= 0.5


def picks(items: list[dict], lasting_only: bool = False, limit: int | None = None) -> list[dict]:
    """Pieces worth showing (scored 2+, or saved; never one marked 没兴趣), best and newest first; a story
    several newsletters carried shows once, with how many others had it. With `limit`, once that many are
    chosen the rest are only compared with them (to count the others that carried the same story): thousands
    of pieces must not mean millions of comparisons."""
    keep = [i for i in items if i.get("feedback") == "useful"
            or (i["score"] >= PICK and i.get("feedback") != "dismissed" and (i["lasting"] or not lasting_only))]
    keep.sort(key=lambda i: (i.get("feedback") == "useful", i["score"], i.get("date") or ""), reverse=True)
    out: list[dict] = []
    grams: list[set[str]] = []
    for it in keep:
        g = _grams(it["title"])
        hit = next((k for k, og in enumerate(grams) if len(g & og) / max(1, len(g | og)) >= 0.5), None)
        if hit is not None:
            out[hit]["also"] += 1
        elif limit is None or len(out) < limit:
            out.append({**it, "also": 0})
            grams.append(g)
    return out


def week_start(now: dt.datetime) -> dt.datetime:
    """Monday 00:00 of the week `now` is in."""
    d = now.date() - dt.timedelta(days=now.weekday())
    return dt.datetime(d.year, d.month, d.day)
