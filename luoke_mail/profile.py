"""Reading the user's own website or blog (only addresses they gave): its title, what it is about, its posts.

Most people can't say what they care about, but their own site says it. What is read here is kept in their
memory as records (profiler.py), from which luoke-mem works out 关注的话题 (luoke-mem 0.19). Everything read
here is data; instructions inside it are ignored."""
from __future__ import annotations

import email.utils
import html
import ipaddress
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from . import __version__

FETCH_TIMEOUT = 10
FETCH_LIMIT = 1_500_000
MAX_POSTS = 30
FEED_PATHS = ("/feed", "/rss.xml", "/atom.xml", "/index.xml", "/feed.xml")

# ---- the user's own website or blog ------------------------------------------------------------------------
class _Page(HTMLParser):
    """Title, description, the feed it announces, and the links to pages on the same site."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.title, self.about, self.feed_url = "", "", None
        self.links: list[tuple[str, str]] = []
        self._in, self._href, self._anchor, self._skip = None, None, "", 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "title":
            self._in = "title"
        elif tag == "meta" and (a.get("name") or a.get("property") or "").lower() in ("description", "og:description"):
            self.about = self.about or (a.get("content") or "")
        elif tag == "link" and "alternate" in (a.get("rel") or "").lower() and \
                (a.get("type") or "").lower() in ("application/rss+xml", "application/atom+xml"):
            self.feed_url = self.feed_url or urllib.parse.urljoin(self.base, a.get("href") or "")
        elif tag == "a":
            self._in, self._href, self._anchor = "a", a.get("href"), ""

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in = None
        elif tag == "a" and self._in == "a":
            text = " ".join(self._anchor.split())
            url = urllib.parse.urljoin(self.base, self._href or "")
            if len(text) >= 6 and urllib.parse.urlparse(url).hostname == urllib.parse.urlparse(self.base).hostname:
                self.links.append((text, url))
            self._in = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._in == "title":
            self.title += data
        elif self._in == "a":
            self._anchor += data


def _get(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": f"luoke-mail/{__version__} (reading the owner's own site)"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        return r.read(FETCH_LIMIT), r.headers.get_content_charset() or "utf-8"


def _strip(text: str, limit: int) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())[:limit]


def _is_feed(raw: bytes) -> bool:
    head = raw.lstrip()[:400].lower()
    return b"<rss" in head or b"<feed" in head


def _date(text: str | None) -> str:
    """A feed's date -- RSS pubDate (RFC 822) or Atom's ISO 8601 -- as "YYYY-MM-DD HH:MM:SS", or "" when unreadable."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        return email.utils.parsedate_to_datetime(text).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, IndexError):
        pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?))?", text)
    return f"{m.group(1)} {(m.group(2) or '00:00:00') + (':00' if m.group(2) and len(m.group(2)) == 5 else '')}" if m else ""


def _feed(raw: bytes) -> list[dict]:
    """RSS or Atom entries: title, summary, link, and when it was published ("" when the feed does not say)."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] not in ("item", "entry"):
            continue
        kids = {c.tag.split("}")[-1]: c for c in el}
        title, link = kids.get("title"), kids.get("link")
        summary = next((kids[k] for k in ("description", "summary", "content") if kids.get(k) is not None), None)  # an element
        # with no children is falsy: never `or` them
        href = ((link.get("href") or link.text) if link is not None else "") or ""
        if title is not None and (title.text or "").strip():
            when = next((kids[k].text for k in ("pubDate", "published", "updated", "date") if kids.get(k) is not None), "")
            out.append({"title": _strip(title.text, 80), "summary": _strip(summary.text if summary is not None else "", 200),
                        "url": href.strip(), "date": _date(when)})
        if len(out) >= MAX_POSTS:
            break
    return out


def _may_read(url: str, base: str) -> bool:
    """An address the page itself points to (its feed) is read only over http(s), and not on this Mac or the local
    network -- unless the address the user gave is there too."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname.lower()
    if host == (urllib.parse.urlparse(base).hostname or "").lower():
        return True
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def fetch_site(url: str, get=_get) -> dict:
    """The user's own site: its title and description, and its posts -- from its feed when it has one, otherwise
    from the links on its front page. Read-only, and only addresses the user gave. ValueError for a bad address."""
    p = urllib.parse.urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("请填写以 http:// 或 https:// 开头的网址。")
    raw, charset = get(url)
    if _is_feed(raw):
        return {"url": url, "title": "", "about": "", "posts": _feed(raw)}
    page = _Page(url)
    try:
        page.feed(raw.decode(charset, "replace"))
        page.close()
    except Exception:  # noqa: BLE001 - a broken page still has whatever was read
        pass
    posts: list[dict] = []
    tries = [page.feed_url] if page.feed_url else [urllib.parse.urljoin(url, path) for path in FEED_PATHS]
    for feed_url in filter(lambda u: _may_read(u, url), tries):
        try:
            body, _ = get(feed_url)
        except Exception:  # noqa: BLE001 - no feed there
            continue
        if _is_feed(body):
            posts = _feed(body)
            if posts:
                break
    if not posts:
        posts = [{"title": t[:80], "summary": "", "url": u} for t, u in dict.fromkeys(page.links)][:MAX_POSTS]
    return {"url": url, "title": " ".join(page.title.split())[:80], "about": _strip(page.about, 200), "posts": posts}


# ---- from the evidence to interests ------------------------------------------------------------------------
