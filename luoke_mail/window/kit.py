"""Building one of the window's pages (protocol §1a `page/<id>`, A2UI v0.9 in the agent catalog).

A page is a tree: every method makes one component and returns its id; containers take the ids of their children,
and `add()` puts a component at the top of the page. Nothing is drawn from mail content but plain text (`Text` is
plain text in the window), and no credential ever goes on a page (protocol §8: `memory.mask` on whatever came from
mail).

Buttons do not carry what they do. `do()` and `go()` register the action under the button's own id and put only
the page and that id in the button's context; window.py looks the press up by the component id the agent passes in
`ai.luoke/user-action`, so the agent cannot ask for anything that was not on the page the user saw.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import memory

CATALOG = "ai.luoke/a2ui/agent/1"
VERSION = "v0.9"
MAX_COMPONENTS = 200            # protocol §4
BIG_PAGE = 400                  # §4 bigPages: a page (not a card) on a core that takes it
MAX_TEXT = 2000
ICONS = frozenset({"mail", "calendar", "warning", "check", "close", "trash", "archive", "star", "reply", "clock",
                   "info", "person", "link", "memory", "question", "lightbulb"})
# §4 lucide: the names a core that lists it draws (on a button: its icon, the words as its tooltip)
LUCIDE = frozenset({"app-window", "archive", "brain", "calendar", "calendar-plus", "check", "chevron-down",
                    "chevron-right", "circle-help", "circle-minus", "clock", "copy", "cpu", "eye", "file", "folder",
                    "folder-open", "globe", "hard-drive", "inbox", "info", "key", "keyboard", "lightbulb", "link",
                    "lock", "mail", "package", "pencil-line", "power", "puzzle", "refresh-cw", "reply", "settings",
                    "shield-alert", "shield-check", "sparkles", "square-arrow-out-up-right", "star", "trash-2",
                    "trending-up", "triangle-alert", "user", "x"})
# added 11-13 for 设计规范 §18 (luoke-agent 5204efc): only on a core that says it has them -- a name a core does not
# know leaves the whole page undrawn
LATER_ICONS = frozenset({"undo-2", "history", "thumbs-up", "thumbs-down", "mail-x", "bell-off"})
LATER_ICONS_FEATURE = "lucide2"
DO, GO = "mail_do", "mail_view"   # the two tools a page's buttons call (agent.py)


def plain(text: Any, limit: int = 200) -> str:
    """One line, short, credentials masked: anything from mail before it goes on a page."""
    text = memory.mask(" ".join(str(text or "").split()))
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Full(Exception):
    """The page has as many components as the window takes: the caller stops adding and says what is left."""


class Page:
    def __init__(self, page: str, generation: int, *, features: frozenset = frozenset(), args: Optional[dict] = None):
        """`features`: what this core draws of protocol §4's later additions (the request's
        `clientCapabilities.experimental["ai.luoke/a2ui"].features`); an old core lists none and gets the page as
        before. `args`: what the page was opened with (§4 pageArgs: `page/<id>?id=…`), {} without."""
        self.page = page
        self.gen = generation
        self.features = frozenset(features)
        self.args = {k: str(v) for k, v in (args or {}).items()} if self.can("pageArgs") else {}
        self.max = BIG_PAGE if self.can("bigPages") else MAX_COMPONENTS
        self._refresh: Optional[Tuple[int, Optional[str]]] = None
        self._secret = False                      # a password field is on the page
        self._components: List[dict] = []
        self._top: List[str] = []
        self._data: Dict[str, Any] = {}
        self._n = 0
        self.actions: Dict[str, dict] = {}        # button id -> {"kind": "do" | "go", ...}
        self.reserve = 0                          # components kept back for what a page adds last
        self.sections = False                     # each Column at the top a card of its own (§4 pageSections)

    def can(self, feature: str) -> bool:
        return feature in self.features

    def drawn(self, icon: Optional[str]) -> bool:
        """This core draws the icon by that name (§4 lucide, and the names added since)."""
        return bool(icon) and self.can("lucide") and (
            icon in LUCIDE or (icon in LATER_ICONS and self.can(LATER_ICONS_FEATURE)))

    # ---- components -----------------------------------------------------------------------------------
    def _id(self, kind: str) -> str:
        self._n += 1
        return f"g{self.gen}-{kind}{self._n}"

    def room(self, n: int = 1) -> bool:
        return len(self._components) + n + self.reserve <= self.max

    def _put(self, comp: dict) -> str:
        if len(self._components) >= self.max:
            raise Full()
        self._components.append(comp)
        return comp["id"]

    def text(self, text: Any, variant: Optional[str] = None, limit: int = 300) -> str:
        comp = {"id": self._id("t"), "component": "Text", "text": plain(text, min(limit, MAX_TEXT))}
        if variant:
            comp["variant"] = variant
        return self._put(comp)

    def long_text(self, text: Any) -> List[str]:
        """A message's body: paragraphs as they are (masked), each at most MAX_TEXT."""
        body = memory.mask(str(text or "")).strip()
        parts = [p.strip() for p in body.split("\n\n") if p.strip()] or [""]
        out = []
        for p in parts:
            while p:
                out.append(self._put({"id": self._id("t"), "component": "Text", "text": p[:MAX_TEXT]}))
                p = p[MAX_TEXT:]
        return out

    def heading(self, text: Any, level: str = "h4") -> str:
        return self.text(text, level)

    def caption(self, text: Any, limit: int = 300) -> str:
        return self.text(text, "caption", limit)

    def icon(self, name: str) -> str:
        if name not in ICONS and not self.drawn(name):
            raise ValueError(name)
        return self._put({"id": self._id("i"), "component": "Icon", "name": name})

    def divider(self) -> str:
        return self._put({"id": self._id("d"), "component": "Divider"})

    def row(self, *children: Optional[str], justify: Optional[str] = None) -> str:
        comp = {"id": self._id("r"), "component": "Row", "children": [c for c in children if c]}
        if justify:
            comp["justify"] = justify
        return self._put(comp)

    def column(self, *children: Optional[str], collapsed: Optional[str] = None) -> str:
        """`collapsed`: shown folded, as this one line, where the core draws it (§4 collapsed); open elsewhere."""
        comp = {"id": self._id("c"), "component": "Column", "children": [c for c in children if c]}
        if collapsed and self.can("collapsed"):
            comp["collapsed"] = plain(collapsed, 200)
        return self._put(comp)

    def item(self, left: Sequence[Optional[str]], right: Sequence[Optional[str]] = (), *,
             open: Optional[str] = None) -> str:
        """One item a line (设计规范 §18.1): its words on the left, its buttons on the right -- drawn as their icons
        (§4 itemIcons, 11-12). `open`: one of its buttons, pressed by a tap on the whole line (§4 rowOpen); the
        window draws no button for it then, an older one draws it as it is."""
        comp = {"id": self._id("r"), "component": "Row", "justify": "spaceBetween",
                "children": [self.column(*left)] + [b for b in (*right, open) if b]}
        if open and self.can("rowOpen"):
            comp["open"] = open
        return self._put(comp)

    def submit(self, field: str, button: str) -> None:
        """Return in the text field `field` presses `button` (§4 fieldSubmit); the window draws no button then."""
        if self.can("fieldSubmit"):
            next(c for c in self._components if c["id"] == field)["submit"] = button

    def list(self, children: Sequence[str]) -> str:
        return self._put({"id": self._id("l"), "component": "List", "children": [c for c in children if c]})

    def card(self, *children: Optional[str]) -> str:
        inner = self.column(*children)
        return self._put({"id": self._id("k"), "component": "Card", "child": inner})

    def add(self, *ids: Optional[str]) -> None:
        self._top.extend(i for i in ids if i)

    def mark(self) -> int:
        return len(self._top)

    def fold(self, mark: int, collapsed: str) -> None:
        """What was added since `mark`, folded into one line (§4 collapsed) where the core draws it; as it is else."""
        if not self.can("collapsed") or len(self._top) <= mark:
            return
        inside = self._top[mark:]
        del self._top[mark:]
        self._top.append(self.column(*inside, collapsed=collapsed))

    # ---- inputs: their values come back in the press's context ----------------------------------------
    def choice(self, key: str, label: str, options: Sequence[Tuple[str, str]], selected: str,
               icons: Optional[Dict[str, str]] = None) -> str:
        """`selected`: what is suggested -- picked already, so being picked is what says it (设计规范 §18.1).
        `icons`: an option's icon where the core draws them (§4 optionIcons, lucide)."""
        values = [v for v, _ in options]
        if selected not in values:
            raise ValueError(f"{selected} 不在选项里")
        self._data.setdefault("choices", {})[key] = [selected]
        self._data.setdefault("_allowed", {})[key] = values
        draw_icons = icons and self.can("optionIcons") and self.can("lucide")
        return self._put({"id": self._id("p"), "component": "ChoicePicker", "label": label,
                          "options": [{"label": lbl, "value": v,
                                       **({"icon": icons[v]} if draw_icons and self.drawn(icons.get(v)) else {})}
                                      for v, lbl in options],
                          "value": {"path": f"/choices/{key}"}})

    def checkbox(self, key: str, label: str, value: bool = False) -> str:
        self._data.setdefault("checks", {})[key] = bool(value)
        return self._put({"id": self._id("x"), "component": "CheckBox", "label": label,
                          "value": {"path": f"/checks/{key}"}})

    def field(self, key: str, label: str, value: str = "", *, secret: bool = False) -> str:
        """A text field. `secret`: an app password (§4 secureField, pages only): drawn as dots, always empty here,
        under /secrets/<key> (a button names each one it reads, never the whole of them); what is typed reaches the
        tool only in the press of a button that reads it ({"$input": "secrets/<key>"}), and is never drawn back."""
        if secret and not self.can("secureField"):
            raise ValueError("这个窗口不能填授权码")
        kind = "secrets" if secret else "fields"
        self._data.setdefault(kind, {})[key] = "" if secret else value
        comp = {"id": self._id("f"), "component": "TextField", "label": label, "value": {"path": f"/{kind}/{key}"}}
        if secret:
            comp["variant"] = "obscured"
            self._secret = True
        return self._put(comp)

    # ---- buttons --------------------------------------------------------------------------------------
    def _button(self, label: str, event: str, context: dict, variant: Optional[str], bid: Optional[str] = None,
                icon: Optional[str] = None, tip: Optional[str] = None) -> str:
        """`icon`: drawn with it where the core has the name (§4 lucide); in a line's buttons only the icon shows
        and the words are its tooltip (§4 itemIcons, 11-12). `tip`: the tooltip when it says more than the words."""
        bid = bid or self._id("b")
        tid = self._put({"id": bid + "-t", "component": "Text", "text": plain(label, 60)})
        child = tid
        if self.drawn(icon):
            iid = self._put({"id": bid + "-i", "component": "Icon", "name": icon})
            child = self._put({"id": bid + "-r", "component": "Row", "children": [iid, tid]})
        comp = {"id": bid, "component": "Button", "child": child, "action": {"event": {"name": event, "context": context}}}
        if variant:
            comp["variant"] = variant
        if (tip or child != tid) and self.can("tooltip"):
            comp["tooltip"] = plain(tip or label, 200)
        return self._put(comp)

    @staticmethod
    def _variant(primary: bool, quiet: bool) -> Optional[str]:
        return "primary" if primary else ("borderless" if quiet else None)

    def do(self, label: str, op: str, args: Optional[dict] = None, *, inputs: Sequence[str] = (),
           primary: bool = False, quiet: bool = False, confirm: Optional[str] = None, sure: Optional[str] = None,
           icon: Optional[str] = None, tip: Optional[str] = None) -> str:
        """A button that changes something: `op` is one of window.OPS, `args` its arguments as they will be sent.
        `inputs` names the page's inputs it reads ("fields", "choices", "checks"): their values come back in the
        press and fill args' {"$input": "<kind>/<key>"} placeholders. Only on the user's tap (protocol §5).
        `confirm`: what the press asks first. A core that draws §4 confirm asks it under the button (`sure` its
        title, else 「<label>？」); an older one gets it on the page with 确认 / 取消 (Window._frame)."""
        if confirm and "secrets" in inputs:
            raise ValueError("填了授权码的按钮不能先确认：确认之前它会留在窗口里")      # Window.confirming keeps args
        bid = self._id("b")
        ctx: Dict[str, Any] = {"page": self.page, "button": bid}
        for kind in inputs:
            if kind not in self._data:
                raise ValueError(f"这一页没有 {kind}")
            ctx[kind] = {k: {"path": f"/secrets/{k}"} for k in self._data[kind]} if kind == "secrets" \
                else {"path": f"/{kind}"}
        self._button(label, DO, ctx, self._variant(primary, quiet), bid, icon, tip)
        native = bool(confirm) and self.can("confirm")
        if native:
            words = plain(label, 60)
            next(c for c in self._components if c["id"] == bid)["confirm"] = {
                "title": plain(sure or f"{words}？", 200), "text": str(confirm)[:MAX_TEXT], "label": words}
        self.actions[bid] = {"kind": "do", "op": op, "args": dict(args or {}), "inputs": list(inputs),
                             "label": label, "confirm": None if native else confirm}
        return bid

    def go(self, label: str, view: dict, *, primary: bool = False, quiet: bool = True, icon: Optional[str] = None,
           tip: Optional[str] = None) -> str:
        """A button that only changes what the page shows (which mailbox, which list, the next screen, one mail
        opened): `view` is merged into the page's view. Read-only, no tap needed."""
        bid = self._id("b")
        self._button(label, GO, {"page": self.page, "button": bid}, self._variant(primary, quiet), bid, icon, tip)
        self.actions[bid] = {"kind": "go", "view": dict(view)}
        return bid

    def open(self, label: str, args: Optional[dict] = None, *, page: Optional[str] = None, quiet: bool = True,
             primary: bool = False, icon: Optional[str] = None) -> Optional[str]:
        """The window opens a page of this app (§5 agent.open_page), with `args` when the core takes them (§4
        pageArgs): read as `page/<id>?<键>=<值>`. No args clears what the page was opened with. `primary`: the one
        thing to press in its part of the page (luoke-agent 11-12: how it is drawn is the window's). None on a core
        that cannot open pages (§4 openPage)."""
        if not self.can("openPage"):
            return None
        ctx: Dict[str, Any] = {"page": page or self.page}
        if args and self.can("pageArgs"):
            ctx["args"] = {k: str(v) for k, v in args.items()}
        return self._button(label, "agent.open_page", ctx, self._variant(primary, quiet), icon=icon)

    def refresh(self, seconds: int, row: Optional[str] = None) -> None:
        """Read this page again in `seconds` while it is in view (§4 pageRefresh) -- a job in progress. Not on a
        page with inputs (reading it again would undo what the user typed or ticked) or on an old core: there
        刷新 is a button, in `row` when given (finish())."""
        self._refresh = (max(2, min(int(seconds), 60)), row)

    def _refreshes(self) -> bool:
        return bool(self._refresh) and self.can("pageRefresh") and not any(
            self._data.get(k) for k in ("fields", "choices", "checks", "secrets"))

    def finish(self) -> None:
        """After the builder: 刷新 as a button where the page is not read again by itself."""
        if not self._refresh or self._refreshes():
            return
        try:
            bid = self.go("刷新", {})
        except Full:
            return
        row = next((c for c in self._components if c["id"] == self._refresh[1]), None)
        if row is not None and "children" in row:
            row["children"].append(bid)
        else:
            self._top.append(bid)

    def link(self, label: str, url: str) -> Optional[str]:
        """A web address the agent opens after saying where it goes (protocol §5); https only, else nothing."""
        if not str(url or "").startswith("https://"):
            return None
        return self._button(label, "agent.open_url", {"url": url}, "borderless")

    # ---- out ------------------------------------------------------------------------------------------
    def messages(self) -> List[dict]:
        data = {k: v for k, v in self._data.items() if not k.startswith("_")}
        root: Dict[str, Any] = {"id": "root", "component": "Column", "children": list(self._top)}
        if self.sections and self.can("pageSections"):
            root["layout"] = "sections"
        if self._refreshes():
            root["refresh"] = self._refresh[0]
        out = [{"version": VERSION, "createSurface": {"surfaceId": self.page, "catalogId": CATALOG}},
               {"version": VERSION, "updateComponents": {"surfaceId": self.page, "components":
                [root] + self._components}}]
        if data:
            out.append({"version": VERSION, "updateDataModel": {"surfaceId": self.page, "path": "/", "value": data}})
        return out

    def allowed(self) -> Dict[str, List[str]]:
        """What a press may send back: each picker's options, and under "$checks" the boxes this page drew."""
        return {**(self._data.get("_allowed") or {}), "$checks": list((self._data.get("checks") or {}).keys())}
