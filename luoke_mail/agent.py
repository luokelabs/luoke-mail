"""螺壳 (luoke-agent): what luoke-mail tells the user through the agent, and what the agent may do for them.

The tool protocol is luoke-agent's docs/工具协议.md (top-level ADR 0006): a subset of MCP over a Unix socket in the
data folder (`run/agent.sock`), served by the luoke-mail process that holds the instance lock.

- **outbox** (`luoke://luoke-mail/outbox`): the state of what luoke-mail wants to say now, not a stream. Per
  mailbox: the new mail sorted for the user (up to 8 that need them, each with its choices; the rest suggested
  for the archive or the trash in one row), and each suspicious message (urgent). Each comes with a plan.
- **plans** (store `agent_plans`): what a card proposes, kept across restarts -- the user may tap hours later.
  Only a plan luoke-mail made is carried out, only with the choices its card offered, only on the user's tap
  (`ai.luoke/user-action`, checked by the server), and only after checking again: the mail still where it was,
  processing mode on when it moves several to the trash or junk. It runs as the same operations the page uses (each with its undo), and is undone as one.
- Never through the agent: unsubscribing (it talks to a third party), switching processing mode on (the
  user's own setting). Processing mode is asked for only when a plan moves several messages to the trash or junk
  (top-level ADR 0016); background sorting only makes plans here.

Off in the first release (top-level ADR 0008): there is no agent yet, and a socket that lets another program ask
for actions is only risk. The page and the notifications work as before; the manifest declares no capabilities
(they wait in packaging/agent-capabilities.json). `LUOKE_MAIL_AGENT=1` turns it on, for trying it with the agent;
tests/ui_harness.py always serves it. When the agent ships, this becomes on and the capabilities go back.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time

from . import agent_cards as cards
from . import config, desktop, engines, memory
from .actions import ActionError
from .agent_mcp import USER_ACTION, ToolError, ToolServer
from .mailbox import MailError

ACCESSORY = "luoke-mail"
NEEDS_YOU = ("reply", "schedule", "follow_up")
SHOWN = 8                                  # messages with their own row on a card; the rest in one row
LISTENING = 600                            # the agent read the outbox this recently: luoke-mail's own notes wait
QUIET = 86400                              # mail the user chose to leave alone: not brought up again on its own for a day
HEADINGS = {"new-mail": "这批邮件我打算这样处理", "suspicious": "这封像是诈骗或钓鱼邮件"}
UNDONE = {"draft": "{} 封草稿删掉了", "archive": "{} 封回到收件箱", "trash": "{} 封回到收件箱", "spam": "{} 封回到收件箱",
          "flag": "{} 封的星标去掉了"}
DONE = {"archive": "归档", "trash": "移到垃圾箱", "spam": "移到垃圾邮件", "flag": "加星标", "draft": "写好草稿"}
OPTIONS = {
    "reply": [("draft", "写草稿"), ("flag", "待跟进"), ("archive", "归档"), ("keep", "先不动")],
    "schedule": [("flag", "待跟进"), ("archive", "归档"), ("keep", "先不动")],
    "follow_up": [("flag", "待跟进"), ("archive", "归档"), ("keep", "先不动")],
}
SUSPICIOUS = [("spam", "移到垃圾邮件"), ("keep", "先不动")]     # never unsubscribe: it confirms the address


def enabled() -> bool:
    return os.environ.get("LUOKE_MAIL_AGENT") == "1"


def socket_path() -> str:
    return os.path.join(config.CONFIG_DIR, "run", "agent.sock")


def _plain(text: str | None, limit: int) -> str:
    """Card text: one line, short; credentials masked."""
    text = memory.mask(" ".join(str(text or "").split()))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _who(it: dict) -> str:
    return _plain(it.get("from_name") or it.get("from_addr") or "", 24)


class Agent:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.read_at = float("-inf")       # when the agent last read the outbox (monotonic)
        self.server = None

    # ---- the socket ------------------------------------------------------------------------------------
    def start(self, path: str | None = None) -> None:
        s = ToolServer(ACCESSORY, __import__("luoke_mail").__version__)
        s.resource(f"luoke://{ACCESSORY}/outbox", self.outbox, name="要告诉你的事")
        s.tool("outbox_ack", self.ack, title="记下这件事已经说过",
               input_schema={"type": "object", "required": ["id", "state"],
                             "properties": {"id": {"type": "string"}, "state": {"enum": ["shown", "answered", "dismissed"]}}})
        s.tool("mail_apply_plan", self.apply_plan, title="按方案处理邮件", read_only=False,
               input_schema={"type": "object", "required": ["plan_id", "choices"],
                             "properties": {"plan_id": {"type": "string"}, "choices": {"type": "object"}}})
        s.tool("mail_undo", self.undo, title="撤销刚才的处理", read_only=False,
               input_schema={"type": "object", "required": ["execution_id"], "properties": {"execution_id": {"type": "string"}}})
        s.tool("mail_open", self.open_mail, title="在邮件助手里看",
               input_schema={"type": "object", "properties": {"plan_id": {"type": "string"}}})
        s.tool("mail_open_settings", self.open_settings, title="打开邮件助手的设置")
        s.tool("mail_open_calendar", self.open_calendar, title="把这封的日程交给「日历」",
               input_schema={"type": "object", "required": ["plan_id", "key"],
                             "properties": {"plan_id": {"type": "string"}, "key": {"type": "string"}}})
        try:
            self.server = s.start(path or socket_path())
        except OSError as ex:                  # the agent cannot reach luoke-mail; the page works as always
            print(f"[luoke-mail] agent socket not started: {ex}", file=sys.stderr)

    def close(self) -> None:
        if self.server:
            self.server.close()
            self.server = None

    def listening(self) -> bool:
        """The agent read the outbox lately: it tells the user, luoke-mail's own notification would repeat it."""
        return time.monotonic() - self.read_at < LISTENING

    # ---- the outbox ------------------------------------------------------------------------------------
    def outbox(self) -> dict:
        self.read_at = time.monotonic()
        out = []
        accts = self.app.mail_accounts()
        for acct in accts:
            label = f"（{acct}）" if len(accts) > 1 else ""
            pending = [it for it in self.store.items(acct, "pending") if it.get("action") and not it.get("flagged")]
            out += self._suspicious(acct, [it for it in pending if it.get("suspicious")], label)
            item = self._new_mail(acct, [it for it in pending if not it.get("suspicious")], label)
            if item:
                out.append(item)
        return {"items": out}

    def _offer(self, acct: str, kind: str, entries: dict) -> dict | None:
        """The plan for these messages and these choices: the same id while they stay the same. One the user
        answered or dismissed is not offered again; open ones for messages no longer here are withdrawn."""
        sig = hashlib.sha256(repr(sorted((k, tuple(e["ids"]), tuple(e["allowed"])) for k, e in entries.items())).encode()).hexdigest()
        plan = self.store.plan_offer(f"{kind}-{sig[:16]}", acct, kind, entries)
        return plan if plan["state"] == "open" else None

    @staticmethod
    def _rows(entries: dict) -> list[dict]:
        """A card's rows, from what the plan keeps: the same card again (after a restart, or to add a note)."""
        return [{"key": k, "label": e["label"], "detail": e.get("detail") or "", "options": [tuple(o) for o in e["options"]],
                 "selected": e["selected"]} for k, e in entries.items()]

    def _quiet(self, acct: str) -> set:
        """Mail the user chose 先不动 for lately: not a new thing to say on its own."""
        q = self.store.cache_get(acct, "agent_quiet")[0] or {}
        return {int(i) for i, t in q.items() if time.time() - t < QUIET}

    def _withdraw(self, acct: str, kind: str, keep: set) -> None:
        for pid in self.store.plans_open(acct, kind):
            if pid not in keep:
                self.store.plan_set(pid, state="withdrawn")

    def _new_mail(self, acct: str, pending: list[dict], label: str) -> dict | None:
        prio = {"high": 0, "medium": 1}
        needs = sorted((it for it in pending if it["action"] in NEEDS_YOU), key=lambda it: it.get("date") or "", reverse=True)
        needs.sort(key=lambda it: prio.get(it.get("priority"), 2))
        shown, more = needs[:SHOWN], needs[SHOWN:]
        rest = [it for it in pending if it["action"] in ("archive", "ignore")]
        quiet = self._quiet(acct)
        if not shown and not rest or {it["id"] for it in shown + rest} <= quiet:   # nothing, or only what was left alone
            self._withdraw(acct, "new-mail", set())
            return None
        entries, buttons = {}, []
        for it in shown:
            key = f"m{it['id']}"
            options = OPTIONS[it["action"]]
            selected = "draft" if it["action"] == "reply" else "flag"
            due = f"（{it['due_date']} 前）" if it.get("due_date") else ""
            entries[key] = {"ids": [it["id"]], "allowed": [v for v, _ in options], "selected": selected, "options": options,
                            "label": f"{_who(it)}：{_plain(it.get('subject'), 40)}",
                            "detail": _plain((it.get("summary") or "") + due, 120)}
            ev = it.get("event") or {}
            if it["action"] == "schedule" and ev.get("date"):
                buttons.append((key, it))
        if rest:
            archive = sum(1 for it in rest if it["action"] == "archive")
            words = "、".join(w for w in (f"{archive} 封打算归档" if archive else "",
                                         f"{len(rest) - archive} 封打算移到垃圾箱" if len(rest) > archive else "") if w)
            entries["rest"] = {"ids": [it["id"] for it in rest], "allowed": ["suggested", "keep"], "selected": "suggested",
                               "options": [("suggested", "照建议处理"), ("keep", "先不动")], "label": f"另外 {words}"}
        rows = self._rows(entries)
        plan = self._offer(acct, "new-mail", entries)
        self._withdraw(acct, "new-mail", {plan["plan_id"]} if plan else set())
        if not plan:
            return None
        pid = plan["plan_id"]
        extra = [self._calendar_button(acct, pid, key, it) for key, it in buttons[:3]]
        extra.append(cards.button("在邮件助手里看", "mail_open", {"plan_id": pid}))
        heading = f"这 {len(shown)} 封要你处理" + ("，其余打算整理" if rest else "") if shown else "这几封打算整理"
        dues = sorted(it["due_date"] for it in shown if it.get("due_date"))
        why = (f"刚分拣好，{len(shown) + len(more)} 封要你处理" + (f"，最早的要在 {dues[0]} 前回" if dues else "")) if shown \
            else f"刚分拣好，{len(rest)} 封可以整理"
        heads = "；".join(f"{_who(it)}：{_plain(it.get('summary') or it.get('subject'), 40)}" for it in shown[:2])
        summary = (heads + ("；" if heads and rest else "") + (f"另外 {len(rest)} 封打算整理" if rest else "") + "。") \
            if heads or rest else ""
        return {"id": f"new-mail/{pid}", "kind": "new-mail", "urgency": "normal" if shown else "low",
                "deadline": f"{dues[0]}T23:59:00" if dues else None, "created": plan["created_at"], "why_now": why,
                "title": (f"{len(shown) + len(more)} 封新邮件要处理" if shown else f"{len(rest)} 封新邮件可以整理") + label,
                "summary": _plain(summary, 300),
                "card": cards.plan_card(pid, heading, rows, apply_event="mail_apply_plan", plan_id=pid, extra_buttons=extra)}

    def _calendar_button(self, acct: str, pid: str, key: str, it: dict) -> dict:
        title = f"加入日程：{_plain(it.get('subject'), 16)}"
        try:
            url = self.app.actions(acct, write=False).calendar(it["id"]).get("google_url")
        except ActionError:
            url = None
        if url and url.startswith("https://"):
            return cards.open_url_button(title, url)      # 螺壳 says where it goes before it opens
        return cards.button(title, "mail_open_calendar", {"plan_id": pid, "key": key})

    def _suspicious(self, acct: str, items: list[dict], label: str) -> list[dict]:
        out, keep, quiet = [], set(), self._quiet(acct)
        for it in items:
            if it["id"] in quiet:
                continue
            key = f"m{it['id']}"
            entry = {"ids": [it["id"]], "allowed": ["spam", "keep"], "selected": "spam", "options": SUSPICIOUS,
                     "label": f"{_who(it)}：{_plain(it.get('subject'), 40)}",
                     "detail": _plain(it.get("reason") or it.get("summary"), 120)}
            plan = self._offer(acct, "suspicious", {key: entry})
            if not plan:
                continue
            pid = plan["plan_id"]
            keep.add(pid)
            row = self._rows({key: entry})[0]
            out.append({"id": f"suspicious/{pid}", "kind": "suspicious", "urgency": "urgent", "deadline": None,
                        "created": plan["created_at"], "why_now": "像是诈骗或钓鱼邮件，别点里面的链接",
                        "title": "一封可疑邮件" + label, "summary": _plain(f"{_who(it)}：{it.get('subject') or ''}", 200),
                        "card": cards.plan_card(pid, "这封像是诈骗或钓鱼邮件", [row], apply_event="mail_apply_plan",
                                                plan_id=pid, extra_buttons=[cards.button("在邮件助手里看", "mail_open", {"plan_id": pid})])})
        self._withdraw(acct, "suspicious", keep)
        return out

    def ack(self, args: dict, meta: dict) -> dict:
        state, item = args.get("state"), str(args.get("id") or "")
        if state not in ("shown", "answered", "dismissed"):
            raise ToolError("invalid", "state 只能是 shown、answered、dismissed。")
        plan = self.store.plan_get(item.split("/", 1)[-1])
        if plan and plan["state"] == "open":
            if state == "shown":
                self.store.plan_set(plan["plan_id"], shown_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            else:
                self.store.plan_set(plan["plan_id"], state="dismissed")
        return {"text": "好的"}

    # ---- carrying a plan out ---------------------------------------------------------------------------
    def apply_plan(self, args: dict, meta: dict) -> dict:
        pid, choices = str(args.get("plan_id") or ""), args.get("choices")
        plan = self.store.plan_get(pid)
        if not plan or plan["state"] != "open":
            raise ToolError("stale", "这批邮件已经处理过了，或者已经有了变化。")
        if not isinstance(choices, dict) or set(choices) - set(plan["entries"]):
            raise ToolError("invalid", "选项不在卡片给出的范围里。")
        picked = {}
        for key, e in plan["entries"].items():
            v = choices.get(key, [e["selected"]])
            if not isinstance(v, list) or len(v) != 1 or v[0] not in e["allowed"]:
                raise ToolError("invalid", "选项不在卡片给出的范围里。")
            picked[key] = v[0]
        acct = plan["account"]
        if acct not in self.app.mail_accounts():
            raise ToolError("stale", "这个邮箱已经不在邮件助手里了。")
        groups: dict[str, list[int]] = {}
        kept = [i for key, choice in picked.items() if choice == "keep" for i in plan["entries"][key]["ids"]]
        for key, choice in picked.items():
            for i in plan["entries"][key]["ids"]:
                it = self.store.get(i)
                if not it or it["account"] != acct or it["state"] != "pending":
                    groups.setdefault("_handled", []).append(i)
                    continue
                op = {"suggested": "archive" if it.get("action") == "archive" else "trash"}.get(choice, choice)
                if op != "keep":
                    groups.setdefault(op, []).append(i)
        # several to the trash or junk can't be taken back: that waits for the user's own setting (ADR 0016)
        if len(groups.get("trash", [])) + len(groups.get("spam", [])) > 1 and not self.app.cfg["mode"]["write"]:
            raise ToolError("needs_setup", "先在邮件助手的设置里开启处理模式。", card=cards.plan_card(
                pid, HEADINGS.get(plan["kind"], HEADINGS["new-mail"]), self._rows(plan["entries"]), apply_event="mail_apply_plan",
                plan_id=pid, note="批量移到垃圾箱前，先在邮件助手的设置里开启处理模式，再回来点。", update=True,
                id_prefix=f"s{int(time.time())}-",
                extra_buttons=[cards.button("打开设置", "mail_open_settings")]))       # the plan stays, the user's choices too
        self.store.plan_set(pid, state="running")
        acts = self.app.actions(acct)
        logs, said, failed = [], [], []
        try:
            for op in ("archive", "trash", "spam", "flag"):
                if groups.get(op):
                    r = acts.execute(groups[op], op)
                    if r.get("log_id"):
                        logs.append({"log": r["log_id"], "op": op, "n": len(r.get("done") or [])})
                    said.append(r["message"])
            drafts = groups.get("draft", [])
            for i in drafts:
                try:
                    signature = self.app.cfg["profile"].get("signature") or ""
                    text = acts.generate_draft(i, self.app.link("user-waiting"), memory.about(self.app.mem), signature)
                    logs.append({"log": acts.save_draft(i, text, self.app.address(acct), signature)["log_id"], "op": "draft", "n": 1})
                except (engines.EngineError, ActionError) as ex:
                    failed.append(str(ex))
            if len(drafts) > len(failed):
                said.append(f"已写好 {len(drafts) - len(failed)} 封草稿，放在草稿箱，请检查后自行发送")
        except Exception as ex:  # noqa: BLE001 - whatever stopped it, the plan must not stay "running"
            self.store.plan_set(pid, state="done" if logs else "open", logs=logs)
            if not logs:
                raise ToolError("failed", str(ex) if isinstance(ex, (MailError, ActionError)) else "没能处理，请稍后再试。")
            failed.append(str(ex) if isinstance(ex, (MailError, ActionError)) else "其余的没能处理")
        if groups.get("_handled"):
            said.append(f"{len(groups['_handled'])} 封已经处理过，没再动")
        if failed:
            said.append("另有没做成的：" + failed[0])
        text = ("；".join(said) or "都先不动。").rstrip("。") + "。"
        self.store.plan_set(pid, state="done", logs=logs)
        if kept:                                   # 先不动: left alone, and not brought up again on its own
            q = self.store.cache_get(acct, "agent_quiet")[0] or {}
            q = {i: t for i, t in q.items() if time.time() - t < QUIET}
            q.update({str(i): time.time() for i in kept})
            self.store.cache_set(acct, "agent_quiet", q)
        card = cards.result_card(pid, text, undo_event="mail_undo" if logs else None, undo_context={"execution_id": pid})
        return {"text": text, "card": card, "done": True}

    def undo(self, args: dict, meta: dict) -> dict:
        pid = str(args.get("execution_id") or "")
        plan = self.store.plan_get(pid)
        if not plan or plan["state"] != "done" or not plan["logs"]:
            raise ToolError("stale", "这次处理已经撤销过了，或者没有可撤销的。")
        acts = self.app.actions(plan["account"], write=False)
        back: dict[str, int] = {}
        for entry in reversed(plan["logs"]):
            try:
                acts.undo(entry["log"])
            except (MailError, ActionError) as ex:
                raise ToolError("failed", str(ex))
            words = UNDONE.get(entry["op"], "{} 封回到原来的样子")
            back[words] = back.get(words, 0) + entry["n"]
        self.store.plan_set(pid, state="undone", logs=[])
        text = "已撤销：" + "，".join(w.format(n) for w, n in back.items() if n) + "。" if any(back.values()) else "已撤销。"
        out = {"text": text, "done": True}
        surface = (meta.get(USER_ACTION) or {}).get("surfaceId")
        if isinstance(surface, str):
            out["card"] = cards.result_card(surface, text)
        return out

    # ---- read-only: opening things ---------------------------------------------------------------------
    def _page(self, where: str) -> None:
        if self.app.port:
            desktop.open_url(desktop.page_url(self.app.port, self.app.token) + f"#{where}")

    def open_mail(self, args: dict, meta: dict) -> dict:
        self._page("inbox")
        return {"text": "已在浏览器里打开邮件助手。"}

    def open_settings(self, args: dict, meta: dict) -> dict:
        self._page("settings")
        return {"text": "已在浏览器里打开邮件助手的设置。"}

    def open_calendar(self, args: dict, meta: dict) -> dict:
        plan = self.store.plan_get(str(args.get("plan_id") or ""))
        entry = (plan or {}).get("entries", {}).get(str(args.get("key") or ""))
        if not entry:
            raise ToolError("stale", "找不到这封邮件了。")
        try:
            cal = self.app.actions(plan["account"], write=False).calendar(entry["ids"][0])
        except ActionError as ex:
            raise ToolError("failed", str(ex))
        folder = os.path.join(config.CONFIG_DIR, "calendar")
        os.makedirs(folder, 0o700, exist_ok=True)
        path = os.path.join(folder, cal["filename"])
        with open(path, "w", encoding="utf-8") as f:
            f.write(cal["ics"])
        desktop.open_url(path)                 # 「日历」 opens it; the user confirms there
        return {"text": "已交给「日历」，请在日历里确认。"}
