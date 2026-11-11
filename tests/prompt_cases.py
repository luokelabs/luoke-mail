"""Every request the model tasks build for the evaluation sets and the sample mail, recorded: what tests/test_prompts.py
compares with tests/prompt_snapshot.json, so moving or tidying the prompt files cannot change a message by accident.

Each case runs the app's own code (as bench/run_tasks.py does) against a link that records each request and answers
"x": an unusable answer, so the correction asked for after one is recorded too. Twice: with no limits, and with a
small character limit and batch (and the recipe's switches off), for the cut-down builds.

  python3 tests/prompt_cases.py --update        # after a deliberate change to a prompt: write the new snapshot
  python3 tests/prompt_cases.py --dump FILE     # every request in full, for a diff
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import isolate  # noqa: E402,F401

ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bench"))

from luoke_mail import digest, editor, engines, mailbox, samples, triage  # noqa: E402
import run_tasks  # noqa: E402

SNAPSHOT = os.path.join(HERE, "prompt_snapshot.json")
TODAY = dt.date(2026, 10, 12)
TASKS = ("triage", "draft", "senders", "cleanup", "digest-read", "digest-pick", "learn-sent", "learn-orgs", "thread")


class Recorder(engines.Link):
    name = "recorder"

    def __init__(self, limits: dict | None = None):
        self._limits = limits or {}
        self.calls: list[dict] = []

    def limits(self, task):
        return {"max_chars": None, "batch": None, "sender_verdict": None, **self._limits}

    def chat(self, task, messages, schema, *, items=1, max_tokens=700, timeout=300):
        self.calls.append({"task": task, "messages": messages, "schema": schema, "items": items,
                           "max_tokens": max_tokens, "timeout": timeout})
        return "x", {}, 0.0


def _mails() -> list[dict]:
    """The sample mail, the bench's own among them; a few with a platform's verdict, with the hidden text cut, with nothing in them,
    and one sent weeks before it is read."""
    mails = [s["mail"] for s in samples.build()]
    mails[3] = {**mails[3], "auth": "platform"}
    mails[5] = {**mails[5], "body": (mails[5]["body"] or "") + "\n" * 2 + "长" * 3000 + "\n\n" + mailbox.UNSEEN_NOTE + "\n藏起来的字"}
    bare = {**mails[0], "body": "", "to": [], "cc": [], "date": None, "subject": None, "from_name": None, "images": 0}
    return mails + [bare, {**bare, "images": 2}, {**mails[1], "date": "2026-08-03 09:00"}]


def _drafts(link):
    mails = _mails()
    for i, m in enumerate(mails):
        points = [] if i % 2 else ["周五下午可以", "请对方确认地点"]
        about = "" if i % 3 else "关于用户：在示例公司做产品"
        for signature in ("", "小周"):
            try:
                triage.draft_reply(m, points, link, about=about, signature=signature, today=TODAY)
            except engines.EngineError:
                pass


def _sorting(link):
    with contextlib.redirect_stderr(io.StringIO()):          # "unusable model output twice", each time
        _sort(link)


def _sort(link):
    rule = {"kind": "sender", "value": "news@shop.example", "action": "archive"}
    for i, m in enumerate(_mails()):
        try:
            triage.analyze(m, link, about="" if i % 3 else "关于用户：在示例公司做产品", language="中文" if i % 4 else "English",
                           today=TODAY, user_address="" if i % 5 == 1 else samples.ME, rule=rule if i % 6 == 2 else None,
                           digest_source=i % 7 == 3)
        except engines.EngineError:
            pass


def _newsletters() -> list[dict]:
    out = []
    for i, m in enumerate(_mails()):
        if i % 4:
            continue
        links = [{"n": 1, "text": "全文", "host": "news.example", "url": "https://news.example/a"},
                 {"n": 2, "text": "第二篇", "host": "blog.example", "url": "https://blog.example/b"}] if i % 8 else []
        out.append({"subject": m.get("subject"), "from_name": m.get("from_name"), "from_addr": m.get("from_addr"),
                    "date": m.get("date") if i % 3 else None, "text": m.get("body") or "", "links": links})
    return out


def _reading(link):
    for i, m in enumerate(_newsletters()):
        taste = "" if i % 2 else "用户关注的话题：本地模型、隐私"
        liked = [] if i % 3 else ["端侧推理的新进展"]
        disliked = [] if i % 5 else ["币价行情"]
        digest.extract(m, taste, liked, disliked, TODAY, link, "中文" if i % 4 else "English")


def _candidates(n: int) -> list[dict]:
    words = ["本地模型", "隐私", "芯片", "行情", "开源", "论文", "发布会", "教程"]
    return [{"title": f"{words[i % len(words)]}的第 {i} 条消息", "summary": "" if i % 3 == 0 else f"摘要 {i}：讲了什么",
             "source": "" if i % 7 == 0 else f"来源{i % 4}", "date": f"2026-10-{1 + i % 28:02d}", "also": i % 3,
             "score": i % 4, "why": "", "feedback": None} for i in range(n)]


def _picking(link):
    editor.choose(_candidates(12), 5, "", [], [], link)
    editor.choose(_candidates(30), 8, "用户关注的话题：本地模型", ["端侧推理"], ["币价行情"], link)
    editor.edit(_candidates(70), editor.WEEK_QUOTA, "用户关注的话题：开源", [], ["发布会"], link)


def _eval(task):
    return {"senders": run_tasks.run_senders, "cleanup": run_tasks.run_cleanup, "thread": run_tasks.run_thread,
            "learn-sent": lambda l: run_tasks.run_learn(l, "learn-sent"),
            "learn-orgs": lambda l: run_tasks.run_learn(l, "learn-orgs")}[task]


RUNS = {"triage": _sorting, "draft": _drafts, "digest-read": _reading, "digest-pick": _picking}


def record(task: str) -> list[dict]:
    """The requests for one task: with no limits, then with small ones."""
    calls = []
    for limits in ({}, {"max_chars": 2500, "batch": 3, "sender_verdict": False}):
        link = Recorder(limits)
        (RUNS.get(task) or _eval(task))(link)
        calls += [c for c in link.calls if c["task"] == task]
    return calls


def digest_of(calls: list[dict]) -> str:
    h = hashlib.sha256()
    for c in calls:
        h.update(json.dumps(c, ensure_ascii=False).encode("utf-8") + b"\n")
    return h.hexdigest()


def snapshot() -> dict:
    out = {}
    for task in TASKS:
        calls = record(task)
        out[task] = {"requests": len(calls), "sha256": digest_of(calls)}
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--dump")
    a = ap.parse_args()
    if a.dump:
        with open(a.dump, "w", encoding="utf-8") as f:
            json.dump({t: record(t) for t in TASKS}, f, ensure_ascii=False, indent=1, sort_keys=True)
    snap = snapshot()
    print(json.dumps(snap, ensure_ascii=False, indent=1))
    if a.update:
        with open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
