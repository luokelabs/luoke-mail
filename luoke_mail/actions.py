"""Everything that changes the mailbox, the user's rules, or leaves the machine -- each runs only
after an explicit click, says what happened in plain words, and (when possible) can be undone.

Every change is written to the log with the steps needed to reverse it. Mailbox undo locates
messages by Message-ID, never by UID: UIDs change whenever mail moves between folders.
Demo items (account "demo") go through the same code paths without touching any mailbox.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sys
import urllib.parse
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Callable

from . import subscriptions, triage
from .mailbox import Mailbox, MailError, same_mid
from .store import Store

OPS = {"archive": "归档", "trash": "移到垃圾箱", "spam": "移到垃圾邮件", "flag": "加星标", "read": "标为已读",
       "skip": "跳过", "complete": "完成并归档", "label": "加标签"}
FOLDER_OPS = {"archive": "archive", "complete": "archive", "trash": "trash", "spam": "junk"}
DEMO = "demo"
NON_MAILBOX_UNDO = ("state", "demo", "demo_old", "archive_rows", "old_rows", "old_unflag", "restore_suggestions", "delete_rule",
                    "restore_rule", "sender_status", "digest_source")
BATCH = 200                  # messages per IMAP command when clearing old mail


class ActionError(Exception):
    pass


class Actions:
    def __init__(self, store: Store, account: str, provider: str | None, open_mailbox: Callable[[], Mailbox],
                 write_ok: bool = True):
        self.store = store
        self.account = account
        self.provider = provider
        self.open_mailbox = open_mailbox
        self.demo = account == DEMO
        self.write_ok = write_ok or self.demo

    # ---- mailbox operations ----------------------------------------------------------------
    def _mailbox_op(self, mb: Mailbox, op: str, pairs: list[tuple[str, str]], sent: dict | None = None,
                    label: str | None = None) -> tuple[list[dict], set[str]]:
        """Apply `op` to (uid, message_id) pairs in INBOX. Returns the undo steps and the uids it did not
        take effect on: moves are checked afterwards, never taken on the server's word. Before the first
        command that changes the mailbox, `sent["undo"]` holds the undo steps for everything asked: if the
        connection drops after that, the caller still knows what may have happened and how to undo it."""
        sent = sent if sent is not None else {}
        uids = [u for u, _ in pairs]
        if op in FOLDER_OPS:
            # Gmail: moving to All Mail is archiving (the Inbox label goes). Removing the label with
            # STORE -X-GM-LABELS was answered OK by Gmail and changed nothing.
            dest = mb.folder_for(FOLDER_OPS[op], create=FOLDER_OPS[op] == "archive")
            kind = "gmail_unarchive" if self.provider == "gmail" and FOLDER_OPS[op] == "archive" else "move_back"
            sent["undo"] = [{"kind": kind, "folder": dest, "message_id": mid, "uid": None, "unseen": op != "spam"}
                            for _, mid in pairs if mid]
            if op != "spam":
                mb.store_flags("INBOX", uids, "(\\Seen)")
            to = mb.move("INBOX", uids, dest)
            stuck = mb.remaining("INBOX", uids)
            if stuck:
                print(f"[luoke-mail] {op}: server said OK but {len(stuck)} message(s) are still in INBOX: {sorted(stuck)}", file=sys.stderr)
            return [{"kind": kind, "folder": dest, "message_id": mid, "uid": to.get(u), "unseen": op != "spam"}
                    for u, mid in pairs if mid and u not in stuck], stuck
        if op == "flag":
            sent["undo"] = [{"kind": "unflag", "message_id": mid, "uid": u} for u, mid in pairs if mid]
            mb.store_flags("INBOX", uids, "(\\Flagged)")
            return sent["undo"], set()
        if op == "read":
            sent["undo"] = [{"kind": "unseen", "message_id": mid, "uid": u} for u, mid in pairs if mid]
            mb.store_flags("INBOX", uids, "(\\Seen)")
            return sent["undo"], set()
        if op == "label":                       # Gmail: the mail stays where it is, with one more label
            if self.provider != "gmail" or not label:
                raise ActionError("只有 Gmail 可以加标签。")
            sent["undo"] = [{"kind": "unlabel", "message_id": mid, "label": label} for _, mid in pairs if mid]
            mb.store_labels("INBOX", uids, label)
            return sent["undo"], set()
        raise ActionError("不支持的操作。")

    def _interrupted(self, op: str, ids: list[int], undo: list[dict]) -> dict:
        """The connection dropped after the mailbox was asked to act, before the result could be checked: some
        or all of it may have happened. Say so, keep the mail on the list (the next sync, or doing it again,
        sees what the mailbox really did -- moved mail is no longer in the inbox), and keep the undo: it
        finds mail by Message-ID, so whatever did not move is simply skipped."""
        print(f"[luoke-mail] {op}: connection lost after the mailbox was asked to act on {len(ids)} message(s)", file=sys.stderr)
        msg = f"邮箱连接中断：{len(ids)} 封可能已{OPS[op]}，结果未能确认。可以再处理一次，已完成的会自动跳过"
        log_id = self.store.log(self.account, op, ids, False, msg, undo or None)
        return {"ok": False, "interrupted": True, "done": [], "gone": [], "stuck": ids, "log_id": log_id if undo else None,
                "message": msg}

    def execute(self, ids: list[int], op: str, by: str = "user", label: str | None = None) -> dict:
        """`by="rule"`: done by a confirmed habit, not by the user -- logged as such (ADR 0016). `label`: for "label"."""
        if op not in OPS:
            raise ActionError("不支持的操作。")
        items = [it for it in (self.store.get(i) for i in ids) if it and it["account"] == self.account]
        if not items:
            raise ActionError("没有可操作的邮件。")
        ids = [it["id"] for it in items]
        if op == "skip":
            self.store.set_state(ids, "skipped", "skip")
            msg = f"已跳过 {len(ids)} 封"
            return {"ok": True, "done": ids, "gone": [], "message": msg,
                    "log_id": self.store.log(self.account, op, ids, True, msg, [{"kind": "state"}])}
        if self.demo:
            return self._execute_demo(items, op)

        undo, done, gone, stuck, found, sent = [], [], [], [], [], {}
        try:
            with self.open_mailbox() as mb:
                for it in items:
                    uid = mb.find_uid("INBOX", it["message_id"], it.get("uid"))     # where the sync saw it, checked
                    if uid:
                        found.append((uid, it["message_id"], it["id"]))
                    else:
                        gone.append(it["id"])
                if found:
                    undo, left = self._mailbox_op(mb, op, [(u, mid) for u, mid, _ in found], sent, label)
                    done = [i for u, _, i in found if u not in left]
                    stuck = [i for u, _, i in found if u in left]
        except MailError:
            if "undo" not in sent:
                raise                         # nothing was asked of the mailbox yet: a plain error
            return self._interrupted(op, [i for _, _, i in found], sent["undo"])
        if op == "flag":
            for i in done:
                self.store.set_fields(i, flagged=1)
        elif op == "label":
            pass                                  # still in the inbox, still on the list: one more label
        elif done:
            self.store.set_state(done, "done", op)
        if gone:
            self.store.set_state(gone, "done", "gone")
        what = f"加上标签「{label}」" if op == "label" else OPS[op]
        parts = [f"已{what} {len(done)} 封" if done else None,
                 f"{len(stuck)} 封未能{OPS[op]}，请稍后重试" if stuck else None,
                 f"{len(gone)} 封已不在收件箱中，已从列表移除" if gone else None]
        msg = "；".join(p for p in parts if p) or f"没有邮件被{OPS[op]}"
        if by == "rule":
            msg = "按你的习惯自动" + msg.removeprefix("已")
        elif by == "own":                         # 螺壳's own mailbox: all of it done unasked (ADR 0016)
            msg = "在螺壳的邮箱中自动" + msg.removeprefix("已")
        log_id = self.store.log(self.account, "auto" if by in ("rule", "own") else op, done, not stuck, msg, undo or None)
        return {"ok": not stuck, "done": done, "gone": gone, "stuck": stuck, "log_id": log_id if undo else None, "message": msg}

    def _execute_demo(self, items: list[dict], op: str) -> dict:
        ids = [it["id"] for it in items]
        if op == "flag":
            for i in ids:
                self.store.set_fields(i, flagged=1)
        else:
            self.store.set_state(ids, "done", op)
        msg = f"已{OPS[op]} {len(ids)} 封（示例）"
        log_id = self.store.log(self.account, op, ids, True, msg, [{"kind": "demo", "op": op}])
        return {"ok": True, "done": ids, "gone": [], "log_id": log_id, "message": msg}

    def archive_sender(self, sub: dict) -> dict:
        """Archive every message from one list / sender that is still in the inbox (old mail included)."""
        label = sub.get("name") or sub.get("from_addr")
        mine = [it for it in self.store.items(self.account, "pending")
                if (sub.get("list_id") and it.get("list_id") == sub["list_id"]) or
                (not sub.get("list_id") and it.get("from_addr") == sub.get("from_addr"))]
        if self.demo:
            n = sub.get("count", len(mine))
            self.store.set_state([it["id"] for it in mine], "done", "archive")
            msg = f"已归档「{label}」的 {n} 封邮件（示例）"
            return {"ok": True, "message": msg, "count": n,
                    "log_id": self.store.log(self.account, "archive", [it["id"] for it in mine], True, msg, [{"kind": "demo"}])}
        sent: dict = {}
        try:
            with self.open_mailbox() as mb:
                uids = mb.search_sender(sub.get("list_id"), sub.get("from_addr"))
                if not uids:
                    return {"ok": True, "message": f"收件箱中没有「{label}」的邮件。", "count": 0, "log_id": None}
                mids = mb.message_ids(uids)
                undo, left = self._mailbox_op(mb, "archive", [(u, mids.get(u)) for u in uids], sent)
        except MailError:
            if "undo" not in sent:
                raise                                     # nothing was asked of the mailbox yet
            return self._interrupted("archive", [it["id"] for it in mine], sent["undo"])
        stuck_mids = {mids.get(u) for u in left}
        self.store.set_state([it["id"] for it in mine if it["message_id"] not in stuck_mids], "done", "archive")
        n = len(uids) - len(left)
        msg = f"已归档「{label}」的 {n} 封邮件" + (f"；{len(left)} 封未能归档，请稍后重试" if left else "")
        log_id = self.store.log(self.account, "archive", [it["id"] for it in mine], not left, msg, undo or None)
        return {"ok": not left, "message": msg, "count": n, "log_id": log_id if undo else None}

    def _history(self, mb: Mailbox, sub: dict) -> tuple[list[tuple[str, list[dict]]], set[str], str]:
        """Where this sender's mail lives: [(folder, messages)], the inbox message-ids, the trash folder.
        Gmail's All Mail already contains the inbox; elsewhere look in the inbox and the archive folder."""
        trash = mb.folder_for("trash")
        inbox = mb.sender_messages("INBOX", sub.get("list_id"), sub.get("from_addr"))
        inbox_mids = {m["message_id"] for m in inbox if m["message_id"]}
        if self.provider == "gmail":
            return [(mb.folder_for("archive"), mb.sender_messages(mb.folder_for("archive"), sub.get("list_id"), sub.get("from_addr")))], inbox_mids, trash
        out = [("INBOX", inbox)]
        from .mailbox import pick_folder
        arch = pick_folder(mb.folders(), "archive")
        if arch:
            out.append((arch, mb.sender_messages(arch, sub.get("list_id"), sub.get("from_addr"))))
        return out, inbox_mids, trash

    @staticmethod
    def _clearable(m: dict, keep: set, me: set) -> bool:
        """Whether one message of a sender's history may go to the trash: not starred, not under a keep label,
        nothing 订阅精选 kept in it, and not the user's own mail (searching From the user's own address in
        Gmail's All Mail finds everything they ever sent)."""
        return not (triage.held(m) or m.get("message_id") in keep or (m.get("from_addr") or "").lower() in me)

    def history_preview(self, sub: dict, keep: set = frozenset(), me: set = frozenset()) -> dict:
        if self.demo:
            return {"count": sub.get("count", 0), "bytes": sub.get("bytes", 0)}
        with self.open_mailbox() as mb:
            where, _, _ = self._history(mb, sub)
        msgs = [m for _, ms in where for m in ms if self._clearable(m, keep, me)]
        return {"count": len(msgs), "bytes": sum(m["size"] for m in msgs)}

    def trash_history(self, sub: dict, purge_days: int | None, before=None, keep: set = frozenset(),
                      me: set = frozenset()) -> dict:
        """Move every message from this list / sender -- inbox and archive -- to the trash, where the
        mail provider deletes it for good after `purge_days`. Undo puts each one back where it was, with its Gmail
        labels. Starred mail, mail under a keep label, what 订阅精选 kept (`keep`) and the user's own (`me`) stay.
        `before(headers)` sees the messages first (了解你 reads them: 先学后清), with the mailbox closed meanwhile
        (the model may take minutes). Moved in checked batches: a dropped connection keeps the undo of what went."""
        label = sub.get("name") or sub.get("from_addr")
        mine = [it for it in self.store.items(self.account, "pending")
                if (sub.get("list_id") and it.get("list_id") == sub["list_id"]) or
                (not sub.get("list_id") and it.get("from_addr") == sub.get("from_addr"))]
        when = f"垃圾箱将在 {purge_days} 天后自动清空，" if purge_days else "垃圾箱会被邮箱定期清空，"
        if self.demo:
            self.store.set_state([it["id"] for it in mine], "done", "trash")
            msg = f"已将「{label}」的 {sub.get('count', 0)} 封邮件移到垃圾箱（示例）"
            return {"ok": True, "message": msg, "count": sub.get("count", 0),
                    "log_id": self.store.log(self.account, "trash", [it["id"] for it in mine], True, msg, [{"kind": "demo"}])}
        with self.open_mailbox() as mb:
            where, inbox_mids, trash = self._history(mb, sub)
        where = [(folder, [m for m in msgs if self._clearable(m, keep, me)]) for folder, msgs in where]
        if before:
            before([m for _, ms in where for m in ms])
        undo, pending, gone_mids, total, size, stuck_mids = [], [], set(), 0, 0, set()

        def step(m: dict, folder: str) -> dict:
            back = "INBOX" if (self.provider == "gmail" and m["message_id"] in inbox_mids) else folder
            return {"kind": "move_back", "folder": trash, "message_id": m["message_id"], "to": back, "labels": m.get("labels") or []}
        try:
            with self.open_mailbox() as mb:
                for folder, msgs in where:
                    for i in range(0, len(msgs), BATCH):
                        here = self._still(mb, folder, msgs[i:i + BATCH])
                        if not here:
                            continue
                        uids = [m["uid"] for m in here]
                        pending = [step(m, folder) for m in here if m["message_id"]]
                        mb.move(folder, uids, trash)
                        left = mb.remaining(folder, uids)          # checked, not taken on trust
                        for m in here:
                            if m["uid"] in left:
                                stuck_mids.add(m["message_id"])
                                continue
                            total += 1
                            size += m["size"]
                            gone_mids.add(m["message_id"])
                        undo += [p for p in pending if p["message_id"] in gone_mids]
                        pending = []
        except MailError:
            if not undo and not pending:
                raise
            self.store.set_state([it["id"] for it in mine if it["message_id"] in gone_mids], "done", "trash")
            print(f"[luoke-mail] trash history: connection lost after {total} message(s) moved", file=sys.stderr)
            msg = f"邮箱连接中断：已将「{label}」的 {total} 封移到垃圾箱，另有一批结果未能确认。可以撤销，也可以再执行一次，已完成的会自动跳过。"
            log_id = self.store.log(self.account, "trash", [it["id"] for it in mine], False, msg, undo + pending)
            return {"ok": False, "interrupted": True, "message": msg, "count": total, "log_id": log_id}
        self.store.set_state([it["id"] for it in mine if it["message_id"] not in stuck_mids], "done", "trash")
        if not total and not stuck_mids:
            return {"ok": True, "message": f"邮箱中没有「{label}」可以清理的邮件。", "count": 0, "log_id": None}
        msg = (f"已将「{label}」的 {total} 封邮件（约 {human_size(size)}）移到垃圾箱。{when}在此之前可以撤销。" if total else "")
        if stuck_mids:
            msg += f"{len(stuck_mids)} 封未能移动，请稍后重试。"
        log_id = self.store.log(self.account, "trash", [it["id"] for it in mine], not stuck_mids, msg, undo or None)
        return {"ok": not stuck_mids, "message": msg, "count": total, "log_id": log_id if undo else None}

    # ---- old mail (清理): archive or trash, by the model's verdict -------------------------------------------
    @staticmethod
    def _still(mb: Mailbox, folder: str, rows: list[dict]) -> list[dict]:
        """The rows (from a scan) whose message is still in `folder` under the same UID: the UID now holds the
        same Message-ID (a message without one was scanned as <uid-N@...> and has none now). A UID that holds
        another message -- the folder was rebuilt, or the rows are not this mailbox's -- is left alone."""
        present = mb.remaining(folder, [r["uid"] for r in rows])
        rows = [r for r in rows if r["uid"] in present]
        if not rows:
            return []
        now = mb.state_of([r["uid"] for r in rows])
        ok = []
        for r in rows:
            st = now.get(r["uid"]) or {}
            same = same_mid(st.get("message_id"), r["message_id"]) or \
                not st.get("message_id") and str(r["message_id"] or "").startswith("<uid-")
            # starred or filed under a keep label since the scan, or put back in the inbox: the user wants it
            if same and not triage.held(st) and not (folder != "INBOX" and st.get("inbox")):
                ok.append(r)
        if len(ok) < len(rows):
            print(f"[luoke-mail] {len(rows) - len(ok)} message(s) in {folder!r} are not as scanned (another message, "
                  "starred, kept or back in the inbox); left alone", file=sys.stderr)
        return ok

    def clear_old(self, archive: list[dict], trash: list[dict], progress: Callable[[int, int], None] = lambda *a: None,
                  star: list[dict] = (), stop: Callable[[], bool] = lambda: False) -> dict:
        """Archive and/or trash old mail picked in 清理 (rows from the header scan), in batches, each checked.
        Inbox mail is marked read as it leaves the inbox. `star`: mail that would have gone to the trash but holds
        something 订阅精选 kept; it stays where it is, starred (starred mail is never cleared in bulk). One log
        entry; its undo puts every message back where it was, read or unread as before, gives trashed mail its
        Gmail labels again, and takes the stars off.

        Tens of thousands take a while (luoke-mail#32: 37,000 in 20 minutes): the entry is written when the first
        batch starts and each batch's undo is saved before it is sent and again once it is done, with its messages'
        local state -- so a process that quits half-way leaves what it moved undoable. `stop()` is asked between
        batches: what was done stays done, and undoable."""
        total = len(archive) + len(trash) + len(star)
        if any(r.get("account") not in (None, self.account) for r in (*archive, *trash, *star)):
            raise ActionError("这些邮件不属于这个邮箱，请刷新页面后重试。")
        if self.demo:
            for rows, state in ((archive, "moved"), (trash, "trashed")):
                self.store.old_set_state(self.account, [r["message_id"] for r in rows], state)
            self.store.old_flag(self.account, [r["message_id"] for r in star], True)
            msg = self._old_message(len(archive), len(trash), 0, sum(r.get("size") or 0 for r in trash), len(star)) + "（示例）"
            return {"ok": True, "moved": len(archive) + len(trash), "stuck": 0, "message": msg,
                    "log_id": self.store.log(self.account, "clean", [], True, msg,
                                             [{"kind": "old_rows", "message_ids": [r["message_id"] for r in archive + trash]},
                                              {"kind": "old_unflag", "message_ids": [r["message_id"] for r in star]}])}
        run = _OldRun(self, stop)
        try:
            self._clear_old(archive, trash, star, progress, total, run)
        except MailError:
            if run.log_id is None:
                raise
            print(f"[luoke-mail] clean: connection lost after {run.count()} message(s) moved", file=sys.stderr)
            msg = (f"邮箱连接中断：已处理 {run.count()} 封，另有一批结果未能确认。"
                   "可以撤销，也可以再执行一次，已完成的会自动跳过。")
            self.store.log_settle(run.log_id, False, msg)
            return {"ok": False, "interrupted": True, "moved": run.count(), "stuck": 0, "message": msg,
                    "log_id": run.log_id, "trashed": run.trashed}
        except _Stopped:
            msg = f"已停止：{self._old_message(len(run.moved), len(run.trashed), run.stuck, run.size, len(run.starred))}。可以撤销。" \
                if run.log_id is not None else "已停止，没有处理任何邮件。"
            if run.log_id is not None:
                self.store.log_settle(run.log_id, True, msg)
            return {"ok": True, "stopped": True, "moved": run.count(), "stuck": run.stuck, "message": msg,
                    "log_id": run.log_id, "trashed": run.trashed}
        if run.stuck:
            print(f"[luoke-mail] clean: {run.stuck} message(s) still in place after the move", file=sys.stderr)
        msg = self._old_message(len(run.moved), len(run.trashed), run.stuck, run.size, len(run.starred))
        if run.log_id is None:                        # nothing was there any more: said, nothing to undo
            self.store.log(self.account, "clean", [], not run.stuck, msg, None)
        else:
            self.store.log_settle(run.log_id, not run.stuck, msg)
        return {"ok": not run.stuck, "moved": run.count(), "stuck": run.stuck, "message": msg,
                "log_id": run.log_id if run.moved or run.trashed else None, "trashed": run.trashed}

    def _settle_old(self, moved: list[str], trashed: list[str], starred: list[str]) -> None:
        self.store.old_flag(self.account, starred, True)
        self.store.old_set_state(self.account, moved, "moved")
        self.store.old_set_state(self.account, trashed, "trashed")
        self.store.mark_done_by_mid(self.account, moved, "archive")          # it may also be on the inbox list
        self.store.mark_done_by_mid(self.account, trashed, "trash")

    def _clear_old(self, archive, trash, star, progress, total, run: "_OldRun") -> None:
        """The mailbox part of clear_old, one batch at a time through `run` (its undo saved before and after)."""
        done = 0
        with self.open_mailbox() as mb:
            plan = [(archive, mb.folder_for("archive", create=True) if archive else None, "moved"),
                    (trash, mb.folder_for("trash") if trash else None, "trashed")]
            for rows, dest, into in plan:
                by_folder: dict[str, list[dict]] = {}
                for r in rows:
                    by_folder.setdefault(r["folder"], []).append(r)
                for folder, frows in by_folder.items():
                    for i in range(0, len(frows), BATCH):
                        run.check()
                        progress(done, total)
                        chunk = frows[i:i + BATCH]
                        done += len(chunk)
                        here = self._still(mb, folder, chunk)                    # gone since the scan: nothing to do
                        if not here:
                            continue
                        uids = [r["uid"] for r in here]
                        gmail_archive = self.provider == "gmail" and into == "moved"
                        run.begin([{"kind": "gmail_unarchive" if gmail_archive else "move_back", "folder": dest,
                                    "message_id": r["message_id"], "uid": None, "to": folder,
                                    "unseen": folder == "INBOX" and not r.get("seen"),
                                    "labels": r.get("labels") if into == "trashed" else []} for r in here])
                        if folder == "INBOX":
                            mb.store_flags("INBOX", uids, "(\\Seen)")
                        to = mb.move(folder, uids, dest)
                        still = mb.remaining(folder, uids)
                        steps, went, size = [], [], 0
                        for r in here:
                            if r["uid"] in still:
                                run.stuck += 1
                                continue
                            went.append(r["message_id"])
                            if into == "trashed":
                                size += r.get("size") or 0
                            unseen = folder == "INBOX" and not r.get("seen")
                            if gmail_archive:                                     # archived: All Mail keeps its labels
                                steps.append({"kind": "gmail_unarchive", "folder": dest, "message_id": r["message_id"],
                                              "uid": to.get(r["uid"]), "unseen": unseen})
                            else:
                                steps.append({"kind": "move_back", "folder": dest, "message_id": r["message_id"],
                                              "uid": to.get(r["uid"]), "to": folder, "unseen": unseen,
                                              "labels": r.get("labels") if into == "trashed" else []})
                        run.end(steps, **{into: went}, size=size)
            by_folder = {}
            for r in star:
                by_folder.setdefault(r["folder"], []).append(r)
            for folder, frows in by_folder.items():
                for i in range(0, len(frows), BATCH):
                    run.check()
                    progress(done, total)
                    chunk = frows[i:i + BATCH]
                    done += len(chunk)
                    here = self._still(mb, folder, chunk)
                    if here:
                        steps = [{"kind": "unflag", "folder": folder, "message_id": r["message_id"]} for r in here]
                        run.begin(steps)
                        mb.store_flags(folder, [r["uid"] for r in here], "(\\Flagged)")
                        run.end(steps, starred=[r["message_id"] for r in here])
            progress(total, total)

    def _old_message(self, archived: int, trashed: int, stuck: int, size: int, starred: int = 0) -> str:
        where = "「所有邮件」" if self.provider == "gmail" or self.demo else "归档文件夹"
        parts = [f"已归档 {archived} 封（可在{where}中找到）" if archived else None,
                 f"已将 {trashed} 封（约 {human_size(size)}）移到垃圾箱" if trashed else None,
                 f"{starred} 封含精选内容，已保留并加星标" if starred else None,
                 f"{stuck} 封未能处理，请稍后重试" if stuck else None]
        return "；".join(p for p in parts if p) or "没有邮件需要处理"

    # ---- undo ------------------------------------------------------------------------------
    def undo(self, log_id: int, only: list[int] | None = None) -> dict:
        """Take an operation back; `only`: just these of its messages (今日处理 undoes one message at a time). The
        rest stays done; undoing the whole later skips what is already back (moves are safe to do again)."""
        e = self.store.log_entry(log_id)
        if not e or e["account"] != self.account:
            raise ActionError("操作记录不存在。")
        if e["undone"]:
            raise ActionError("此操作已撤销。")
        if not e["undo"]:
            raise ActionError("此操作无法撤销。")
        steps = e["undo"]
        if only is not None:
            mids = {it["message_id"] for it in (self.store.get(i) for i in only if i in e["item_ids"]) if it}
            if not mids:
                raise ActionError("这封邮件不在这次操作里。")
            steps = [st for st in steps if st.get("message_id") in mids]
            e = {**e, "item_ids": [i for i in e["item_ids"] if i in only]}
        missing = 0
        mailbox_steps = [s for s in steps if s["kind"] not in NON_MAILBOX_UNDO]
        if mailbox_steps and not self.demo:
            with self.open_mailbox() as mb:
                rest = self._undo_batched(mb, mailbox_steps)
                for s in rest:
                    missing += 0 if self._undo_mailbox_step(mb, s) else 1
        for s in steps:                          # local state: rules, suggestions, sender decisions
            if s["kind"] in ("old_rows", "archive_rows"):            # "archive_rows": written by 0.6.0
                self.store.old_set_state(self.account, s["message_ids"], "here")
                self.store.reopen_by_mid(self.account, s["message_ids"])
            elif s["kind"] == "digest_source":
                self.store.digest_remove_source(self.account, s["key"])
            elif s["kind"] == "old_unflag":
                self.store.old_flag(self.account, s["message_ids"], False)
            elif s["kind"] == "demo_old":                               # written by 0.5.0
                self.store.old_set_state(self.account, [m.get("message_id") for m in s["messages"]], "here")
            elif s["kind"] == "restore_suggestions":
                self.store.restore(s["items"])
            elif s["kind"] == "delete_rule":
                self.store.delete_rule(s["id"])
            elif s["kind"] == "restore_rule":
                self.store.restore_rule(s["rule"], self.account)
            elif s["kind"] == "sender_status":
                prev = s.get("previous")
                self.store.set_sender(self.account, s["key"], prev["status"] if prev else None,
                                      prev.get("name") if prev else None, prev.get("detail") if prev else None)
        if e["action"] == "flag":
            for i in e["item_ids"]:
                self.store.set_fields(i, flagged=0)
        elif e["action"] in OPS or e["action"] in ("draft", "auto"):
            self.store.set_state(e["item_ids"], "pending")      # (a label never took it off the list: no change)
        if only is None:
            self.store.mark_undone(log_id)
        msg = "已撤销" + (f"（{missing} 封已不在邮箱中）" if missing else "")
        return {"ok": True, "message": msg, "item_ids": e["item_ids"]}

    @staticmethod
    def _relabel(mb: Mailbox, folder: str, pairs: list[tuple[str | None, dict]]) -> None:
        """Gmail: copying a message into a label's folder gives it that label (moving to the trash took them)."""
        by_label: dict[str, list[str]] = {}
        for uid, s in pairs:
            for label in s.get("labels") or []:
                if uid:
                    by_label.setdefault(label, []).append(uid)
        for label, uids in by_label.items():
            try:
                mb.copy(folder, uids, label)
            except MailError as ex:                  # the label may have been deleted since: the mail is back anyway
                print(f"[luoke-mail] could not restore label {label!r}: {ex}", file=sys.stderr)

    @staticmethod
    def _undo_batched(mb: Mailbox, steps: list[dict]) -> list[dict]:
        """Moves whose destination UID was recorded go back in batches (a cleanup of a thousand messages
        undoes in seconds). Each UID is checked against its Message-ID first; anything that doesn't match, or
        has no UID, is returned for the one-by-one path."""
        rest, groups = [], {}
        for s in steps:
            if s["kind"] in ("gmail_unarchive", "move_back") and s.get("uid"):
                groups.setdefault((s["kind"], s["folder"], s.get("to") or "INBOX"), []).append(s)
            else:
                rest.append(s)
        for (kind, folder, to), group in groups.items():
            for i in range(0, len(group), BATCH):
                chunk = group[i:i + BATCH]
                mb.select(folder, readonly=False)
                mids = mb.message_ids([s["uid"] for s in chunk])
                ok = [s for s in chunk if mids.get(s["uid"]) == s["message_id"]]
                rest += [s for s in chunk if s not in ok]
                if not ok:
                    continue
                uids = [s["uid"] for s in ok]
                back = mb.copy(folder, uids, to) if kind == "gmail_unarchive" else mb.move(folder, uids, to)
                Actions._relabel(mb, to, [(back.get(s["uid"]), s) for s in ok])
                unseen = [back[s["uid"]] for s in ok if s.get("unseen", True) and s["uid"] in back]
                if unseen and to == "INBOX":
                    mb.store_flags("INBOX", unseen, "(\\Seen)", add=False)
                elif to == "INBOX":                       # no COPYUID: find them one by one
                    for s in ok:
                        if s.get("unseen", True):
                            u = mb.find_uid("INBOX", s["message_id"])
                            if u:
                                mb.store_flags("INBOX", [u], "(\\Seen)", add=False)
        return rest

    @staticmethod
    def _undo_mailbox_step(mb: Mailbox, s: dict) -> bool:
        kind, mid = s["kind"], s.get("message_id")
        if kind == "move_back":
            uid = mb.find_uid(s["folder"], mid, s.get("uid"))
            if not uid:
                return False
            dest = s.get("to") or "INBOX"
            if mb.find_uid(dest, mid):
                # the move was cut off before it finished (copied, original still in place): undoing it means
                # removing the copy, not moving it back next to the original
                mb.delete_exact(s["folder"], uid)
                return True
            mb.move(s["folder"], [uid], dest)
            if s.get("labels"):
                Actions._relabel(mb, dest, [(mb.find_uid(dest, mid), s)])
            if s.get("unseen", True) and dest == "INBOX":
                back = mb.find_uid("INBOX", mid)
                if back:
                    mb.store_flags("INBOX", [back], "(\\Seen)", add=False)
            return True
        if kind == "gmail_unarchive":           # copying from All Mail to INBOX gives it the Inbox label back
            uid = mb.find_uid(s["folder"], mid, s.get("uid"))
            if not uid:
                return False
            mb.copy(s["folder"], [uid], "INBOX")
            back = mb.find_uid("INBOX", mid) if s.get("unseen", True) else None
            if back:
                mb.store_flags("INBOX", [back], "(\\Seen)", add=False)
            return True
        if kind in ("unflag", "unseen"):
            folder = s.get("folder") or "INBOX"
            uid = mb.find_uid(folder, mid, s.get("uid"))
            if uid:
                mb.store_flags(folder, [uid], "(\\Flagged)" if kind == "unflag" else "(\\Seen)", add=False)
            return bool(uid)
        if kind == "unlabel":
            uid = mb.find_uid("INBOX", mid)
            if uid:
                mb.store_labels("INBOX", [uid], s["label"], add=False)
            return bool(uid)
        if kind == "delete_draft":
            uid = mb.find_uid(s["folder"], mid)
            if uid:
                mb.delete_exact(s["folder"], uid)
            return bool(uid)
        return True

    # ---- replies ---------------------------------------------------------------------------
    def generate_draft(self, item_id: int, link, about: str, signature: str) -> str:
        it = self._item(item_id)
        from . import samples                 # the demo inbox lives on its own date
        text = triage.draft_reply(it, it.get("reply_points") or [], link, about, signature,
                                  today=samples.TODAY if self.demo else None)
        self.store.set_fields(item_id, draft_text=text)
        return text

    def save_draft(self, item_id: int, text: str, user_address: str, display_name: str = "") -> dict:
        it = self._item(item_id)
        text = text.strip()
        if not text:
            raise ActionError("草稿内容为空。")
        mid = make_msgid(domain="luoke-mail.local")
        raw = build_draft(it, text, user_address, display_name, mid)
        self.store.set_fields(item_id, draft_text=text)
        if self.demo:
            undo = [{"kind": "demo", "op": "draft"}]
            msg = "已保存草稿（示例）"
        else:
            with self.open_mailbox() as mb:
                folder = mb.folder_for("drafts")
                mb.append(folder, raw)
            undo = [{"kind": "delete_draft", "folder": folder, "message_id": mid}]
            msg = "已保存到草稿箱，请检查后自行发送"
        self.store.set_state([item_id], "done", "draft")
        log_id = self.store.log(self.account, "draft", [item_id], True, msg, undo)
        return {"ok": True, "message": msg, "log_id": log_id}

    # ---- calendar --------------------------------------------------------------------------
    def calendar(self, item_id: int) -> dict:
        it = self._item(item_id)
        ev = it.get("event")
        if not ev or not ev.get("date"):
            raise ActionError("未识别到具体日期。")
        out = {"ics": event_ics(it, ev), "filename": f"luoke-{item_id}.ics"}
        if self.provider == "gmail" and not self.demo:
            out["google_url"] = google_calendar_url(it, ev)
        # what was put in the calendar, so a later reminder of the same appointment says so instead of offering it
        # again (10-22; until 主控 reads the calendar itself, top-level ADR 0014 amended)
        added = [x for x in (self.store.cache_get(self.account, "calendar_added")[0] or []) if x.get("message_id") != it.get("message_id")]
        added.append({"date": ev.get("date"), "start": ev.get("start"), "domain": (it.get("from_domain") or "").lower(),
                      "title": ev.get("title"), "message_id": it.get("message_id")})
        self.store.cache_set(self.account, "calendar_added", added[-200:])
        return out

    # ---- unsubscribe -----------------------------------------------------------------------
    def unsubscribe(self, sub: dict) -> dict:
        if sub.get("suspicious"):
            raise ActionError("此发件人发送过可疑邮件，退订会向对方确认你的邮箱地址有效。")
        label = sub.get("name") or sub.get("from_addr")
        key = sub["key"]
        if sub.get("method") == "one_click":
            ok, detail = (True, "") if self.demo else subscriptions.one_click(sub["unsub_http"])
            if ok:
                self.store.set_sender(self.account, key, "unsubscribed", label, "one_click")
                msg = f"已退订「{label}」" + ("（示例）" if self.demo else "。之后一两天内可能仍会收到少量邮件。")
                self.store.log(self.account, "unsubscribe", [], True, msg)
                return {"ok": True, "message": msg, "status": "unsubscribed"}
            # the sender's server refused the one-click request: fall back to its unsubscribe page
            import sys
            print(f"[luoke-mail] one-click unsubscribe refused for {label}: {detail}", file=sys.stderr)
            self.store.set_sender(self.account, key, "unsubscribe_opened", label, "web")
            msg = f"「{label}」不支持一键退订，已打开退订网页，请按页面提示完成。"
            self.store.log(self.account, "unsubscribe", [], True, msg)
            return {"ok": True, "message": msg, "status": "unsubscribe_opened", "open_url": sub["unsub_http"]}
        if sub.get("method") == "body_link" and sub.get("body_link"):
            host = urllib.parse.urlparse(sub["body_link"]).hostname or ""
            self.store.set_sender(self.account, key, "unsubscribe_opened", label, "body_link")
            self.store.log(self.account, "unsubscribe", [], True, f"打开了「{label}」的退订链接（{host}）")
            return {"ok": True, "status": "unsubscribe_opened", "open_url": sub["body_link"],
                    "message": f"已打开退订链接（{host}）。请确认网址属于该发件人，再按页面提示完成。"}
        if sub.get("method") == "reply" and sub.get("from_addr"):
            kw = sub.get("reply_keyword") or "TD"
            self.store.set_sender(self.account, key, "unsubscribe_opened", label, "reply")
            self.store.log(self.account, "unsubscribe", [], True, f"打开了给「{label}」的退订回复（{kw}）")
            q = urllib.parse.urlencode({"subject": kw, "body": kw}, quote_via=urllib.parse.quote)
            return {"ok": True, "status": "unsubscribe_opened", "mailto": f"mailto:{sub['from_addr']}?{q}",
                    "message": f"此发件人要求回复「{kw}」退订，已打开写信窗口，请发送这封邮件。"}
        if sub.get("unsub_http") or sub.get("unsub_mailto"):
            web = bool(sub.get("unsub_http"))
            self.store.set_sender(self.account, key, "unsubscribe_opened", label, "web" if web else "mailto")
            self.store.log(self.account, "unsubscribe", [], True, f"打开了「{label}」的退订{'网页' if web else '邮件'}")
            return {"ok": True, "status": "unsubscribe_opened", "open_url": sub.get("unsub_http") if web else None,
                    "mailto": None if web else sub.get("unsub_mailto"),
                    "message": "已打开退订网页，请按页面提示完成。" if web
                    else "已打开写信窗口，请发送这封退订邮件。"}
        raise ActionError("此发件人未提供退订方式。")

    def _item(self, item_id: int) -> dict:
        it = self.store.get(item_id)
        if not it or it["account"] != self.account:
            raise ActionError("邮件不存在。")
        return it


def human_size(n: int) -> str:
    if n >= 1000 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{max(1, round(n / 1024))} KB"


def build_draft(it: dict, text: str, user_address: str, display_name: str, message_id: str) -> bytes:
    m = EmailMessage()
    m["From"] = f"{display_name} <{user_address}>" if display_name else user_address
    m["To"] = it.get("reply_to") or it.get("from_addr") or ""
    subject = it.get("subject") or ""
    m["Subject"] = subject if subject.lower().startswith(("re:", "回复：", "回复:")) else f"Re: {subject}"
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = message_id
    if it.get("message_id") and not it["message_id"].startswith("<uid-"):
        m["In-Reply-To"] = it["message_id"]
        m["References"] = it["message_id"]
    m.set_content(text)
    return m.as_bytes()


def _event_bounds(ev: dict) -> tuple[str, str, bool]:
    day = dt.date.fromisoformat(ev["date"])
    if ev.get("start"):
        start = dt.datetime.combine(day, dt.time.fromisoformat(ev["start"]))
        end = dt.datetime.combine(day, dt.time.fromisoformat(ev["end"])) if ev.get("end") else start + dt.timedelta(hours=1)
        if end <= start:
            end = start + dt.timedelta(hours=1)
        return start.strftime("%Y%m%dT%H%M%S"), end.strftime("%Y%m%dT%H%M%S"), False
    return day.strftime("%Y%m%d"), (day + dt.timedelta(days=1)).strftime("%Y%m%d"), True


def google_calendar_url(it: dict, ev: dict) -> str:
    start, end, _ = _event_bounds(ev)
    q = {"action": "TEMPLATE", "text": ev.get("title") or it.get("subject") or "日程", "dates": f"{start}/{end}",
         "details": f"{it.get('summary') or ''}\n来自邮件：{it.get('subject') or ''}（{it.get('from_addr') or ''}）"}
    if ev.get("location"):
        q["location"] = ev["location"]
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(q)


def _ics_text(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """RFC 5545 3.1: lines of at most 75 octets, continued with a space, never splitting a character."""
    out, cur = [], ""
    for ch in line:
        limit = 75 if not out else 74                 # a continuation line starts with the space
        if len((cur + ch).encode("utf-8")) > limit:
            out.append(cur)
            cur = ""
        cur += ch
    out.append(cur)
    return [out[0]] + [" " + x for x in out[1:]]


def event_ics(it: dict, ev: dict) -> str:
    start, end, all_day = _event_bounds(ev)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # the same mail's event keeps one UID: imported twice it is updated, not added twice (luoke-mail#27)
    uid = hashlib.sha256(str(it.get("message_id") or it["id"]).encode()).hexdigest()[:24]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//luoke-mail//CN", "CALSCALE:GREGORIAN", "BEGIN:VEVENT",
             f"UID:{uid}@luoke-mail.local", f"DTSTAMP:{stamp}",
             f"DTSTART;VALUE=DATE:{start}" if all_day else f"DTSTART:{start}",
             f"DTEND;VALUE=DATE:{end}" if all_day else f"DTEND:{end}",
             f"SUMMARY:{_ics_text(ev.get('title') or it.get('subject') or '日程')}",
             f"DESCRIPTION:{_ics_text((it.get('summary') or '') + chr(10) + '来自邮件：' + (it.get('subject') or ''))}"]
    if ev.get("location"):
        lines.append(f"LOCATION:{_ics_text(ev['location'])}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(x for line in lines for x in _fold(line)) + "\r\n"


class _Stopped(Exception):
    """The user asked 清理 to stop: the batch in hand is not started."""


class _OldRun:
    """One clean-up under way: its log entry, written when the first batch starts, and each batch's undo saved
    before the batch is sent (`begin`) and again with what really moved, with the local state (`end`)."""

    def __init__(self, acts: "Actions", stop: Callable[[], bool]):
        self.acts, self.stop = acts, stop
        self.log_id: int | None = None
        self.batch = 0
        self.moved: list[str] = []
        self.trashed: list[str] = []
        self.starred: list[str] = []
        self.stuck = self.size = 0

    def count(self) -> int:
        return len(self.moved) + len(self.trashed)

    def check(self) -> None:
        if self.stop():
            raise _Stopped()

    def _unfinished(self) -> str:
        return f"清理未完成：已处理 {self.count()} 封。可以撤销，也可以再执行一次，已完成的会自动跳过。"

    def begin(self, steps: list[dict]) -> None:
        store = self.acts.store
        if self.log_id is None:
            self.log_id = store.log(self.acts.account, "clean", [], False, self._unfinished(), None)
        store.log_batch(self.log_id, self.batch, steps)

    def end(self, steps: list[dict], moved=(), trashed=(), starred=(), size: int = 0) -> None:
        moved, trashed, starred = list(moved), list(trashed), list(starred)
        store = self.acts.store
        store.log_batch(self.log_id, self.batch, steps + [{"kind": "old_rows", "message_ids": moved + trashed},
                                                          {"kind": "old_unflag", "message_ids": starred}])
        self.acts._settle_old(moved, trashed, starred)
        self.moved += moved
        self.trashed += trashed
        self.starred += starred
        self.size += size
        store.log_settle(self.log_id, False, self._unfinished())
        self.batch += 1


__all__ = ["Actions", "ActionError", "OPS", "MailError"]
