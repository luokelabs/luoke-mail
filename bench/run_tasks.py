#!/usr/bin/env python3
"""Run luoke-mail's evaluation sets for the tasks other than sorting (bench/task_sets.py) and score them.

  python3 bench/run_tasks.py --base-url http://127.0.0.1:8000/v1 --model gemma-4-26b [--api-key KEY] [--task cleanup,...]
  python3 bench/run_tasks.py --hub SOCKET --label "..."          # through 螺壳主控 (a hub named, never found on its own)

Tasks: cleanup (清理判断), senders (订阅体检), learn-sent, learn-orgs (了解你), thread (往来邮件卡在谁那里). Each runs
through the app's own code (triage.classify_old / classify_senders, learn.from_sent / from_received, threads.judge),
batched as the app does. A task passes its
hard criteria only with no hard failure at all (luoke-core V1 §7: 第 1 层硬性条目全过):
  cleanup     something to keep judged as promotion / newsletter / notification (a trash suggestion)
  senders     statements, orders, login alerts or people judged marketing / newsletter / notification (an unsubscribe)
  learn-*     a fact containing a word it must never contain (a third party's claim, a secret, a one-off event...)
  thread      a settled conversation reported as waiting on the user (`done_as_you`), or one waiting on the user
              not reported as such (`missed_you`); each item has `state` (them / you / done) and `expect`
Results -> bench/results/tasks/<task>/<label>.json, with the set's sha256 (the evidence record); `--out DIR` writes
<DIR>/<task>/<label>.json instead (luoke-core's release script keeps its evidence in its own folder).
Stdlib only, Python 3.9.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from luoke_mail import __version__, engines, hub, learn, threads, triage  # noqa: E402
import task_sets as S  # noqa: E402

TASKS = ("cleanup", "senders", "learn-sent", "learn-orgs", "thread")
UNSUB = ("marketing", "newsletter", "notification")


def set_hash(data) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


# ---- the runs: (items with results, score) -----------------------------------------------------------------
def run_cleanup(link) -> tuple[list, dict]:
    out = []
    for scope, items in S.CLEANUP.items():
        rows = [dict(r, message_id=f"<{scope}{i}@task.example>", _i=i) for i, (r, _, _) in enumerate(items)
                if not triage.held(r)]             # as in the app: starred or kept by a label, never asked about
        sizes = {}
        for r in rows:
            sizes[r["from_addr"]] = sizes.get(r["from_addr"], 0) + 1
        rows.sort(key=lambda r: (-sizes[r["from_addr"]], r["from_addr"], r["date"]))   # as 清理 batches them
        got, i = {}, 0
        while i < len(rows):
            kinds, count = triage.classify_old(rows[i:], link, S.TODAY, scope)
            for n, k in kinds.items():
                got[rows[i + n - 1]["_i"]] = k
            i += count
        for idx, (r, ok, hard) in enumerate(items):
            k = "held" if triage.held(r) else got.get(idx)
            out.append({"scope": scope, "subject": r["subject"], "from": r["from_addr"], "expect": list(ok), "hard": hard,
                        "kind": k, "ok": k in ok or (k == "held" and not set(ok) <= set(S.TRASH)),
                        "hard_failure": hard and k in S.TRASH})
    trash = [o for o in out if set(o["expect"]) <= set(S.TRASH)]
    return out, {"n": len(out), "ok": sum(o["ok"] for o in out), "hard_n": sum(o["hard"] for o in out),
                 "hard_failures": sum(o["hard_failure"] for o in out), "unanswered": sum(o["kind"] is None for o in out),
                 "trash_found": sum(o["kind"] in S.TRASH for o in trash), "trash_n": len(trash)}


def run_senders(link) -> tuple[list, dict]:
    items = S.SENDERS
    got, i = {}, 0
    senders = [s for s, _, _ in items]
    while i < len(senders):
        kinds, count = triage.classify_senders(senders[i:], link)
        for n, k in kinds.items():
            got[i + n - 1] = k
        i += count
    out = [{"sender": s["from_addr"], "expect": list(ok), "hard": hard, "kind": got.get(idx), "ok": got.get(idx) in ok,
            "hard_failure": hard and got.get(idx) in UNSUB} for idx, (s, ok, hard) in enumerate(items)]
    unsub = [o for o in out if not o["hard"]]
    return out, {"n": len(out), "ok": sum(o["ok"] for o in out), "hard_n": sum(o["hard"] for o in out),
                 "hard_failures": sum(o["hard_failure"] for o in out), "unanswered": sum(o["kind"] is None for o in out),
                 "unsub_found": sum(o["kind"] in UNSUB for o in unsub), "unsub_n": len(unsub)}


def _has(text: str, words) -> bool:
    low = text.lower()
    return any(w.lower() in low for w in words)


def run_learn(link, kind: str) -> tuple[list, dict]:
    out, found, wanted, bad = [], 0, 0, 0
    for b, case in enumerate(S.LEARN_SENT if kind == "learn-sent" else S.LEARN_ORGS):
        if kind == "learn-sent":
            facts, _ = learn.from_sent(case["mails"], S.ME, link)
        else:
            facts, _ = learn.from_received(case["senders"], link)
        hits = []
        for e in case["expect"]:
            if kind == "learn-sent":
                topics, words = e
                hit = any(f["topic"].split(":")[0] in topics and _has(f["text"], words) for f in facts)
            else:
                pos, topics, words = e
                hit = any(pos in f["n"] and f["topic"].split(":")[0] in topics and _has(f["text"], words) for f in facts)
            hits.append(hit)
        never = [f["text"] for f in facts if _has(f["text"], case["never"])]
        found, wanted, bad = found + sum(hits), wanted + len(hits), bad + len(never)
        out.append({"batch": b + 1, "facts": [{"topic": f["topic"], "text": f["text"], "n": f["n"]} for f in facts],
                    "expect_found": hits, "hard_failures": never})
    return out, {"n": wanted, "ok": found, "hard_failures": bad, "facts": sum(len(o["facts"]) for o in out)}


def run_thread(link) -> tuple[list, dict]:
    out = []
    for msgs, ok, hard in S.THREAD:
        got = threads.judge(msgs, link)
        state = got["state"]
        why = None
        if hard and "done" in ok and "you" not in ok and state == "you":
            why = "done_as_you"
        elif hard and "you" in ok and state != "you":
            why = "missed_you"
        out.append({"subject": msgs[-1]["subject"], "last_me": msgs[-1]["me"], "expect": list(ok), "hard": hard,
                    "state": state, "what": got["what"], "ok": state in ok, "hard_failure": bool(why), "why": why})
    return out, {"n": len(out), "ok": sum(o["ok"] for o in out), "hard_n": sum(o["hard"] for o in out),
                 "hard_failures": sum(o["hard_failure"] for o in out),
                 "done_as_you": sum(o["why"] == "done_as_you" for o in out),
                 "missed_you": sum(o["why"] == "missed_you" for o in out)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url")
    ap.add_argument("--model")
    ap.add_argument("--api-key", default=os.environ.get("LUOKE_API_KEY"))
    ap.add_argument("--hub", default=None, metavar="SOCKET")
    ap.add_argument("--label", default=None)
    ap.add_argument("--task", default=",".join(TASKS))
    ap.add_argument("--out", default=os.path.join(ROOT, "bench", "results", "tasks"), help="where results go")
    a = ap.parse_args()
    if a.hub:
        if not os.path.exists(a.hub):
            ap.error(f"{a.hub} 不存在：螺壳主控没有在那里运行")
        link = hub.Hub(a.hub, "user-waiting")
    elif a.base_url and a.model:
        link = engines.Direct(a.base_url, a.model, a.api_key)
    else:
        ap.error("--base-url and --model, or --hub SOCKET")
    label = a.label or a.model or "luoke-core"
    sets = {"cleanup": S.CLEANUP, "senders": S.SENDERS, "learn-sent": S.LEARN_SENT, "learn-orgs": S.LEARN_ORGS,
            "thread": S.THREAD}
    runs = {"cleanup": run_cleanup, "senders": run_senders, "learn-sent": lambda l: run_learn(l, "learn-sent"),
            "learn-orgs": lambda l: run_learn(l, "learn-orgs"), "thread": run_thread}
    for task in [t.strip() for t in a.task.split(",") if t.strip()]:
        t0 = time.time()
        items, score = runs[task](link)
        score.update(seconds=round(time.time() - t0, 1), passes=score["hard_failures"] == 0)
        print(f"{task:11s} {score['ok']}/{score['n']} · hard failures {score['hard_failures']} · "
              f"{'PASS' if score['passes'] else 'FAIL'} · {score['seconds']}s  "
              + " ".join(f"{k}={v}" for k, v in score.items() if k not in ("ok", "n", "hard_failures", "passes", "seconds")), flush=True)
        for it in items:
            if it.get("hard_failure") or (isinstance(it.get("hard_failures"), list) and it["hard_failures"]):
                print("   hard:", json.dumps(it, ensure_ascii=False)[:240], flush=True)
        out_dir = os.path.join(a.out, task)
        os.makedirs(out_dir, exist_ok=True)
        rec = {"task": f"luoke-mail.{task}", "accessory": f"luoke-mail/{__version__}", "label": label,
               "model": link.name if a.hub else a.model, "base_url": "unix:" + a.hub if a.hub else a.base_url,
               "machine": engines.machine_summary(), "python": platform.python_version(), "date": time.strftime("%Y-%m-%d"),
               "set_sha256": set_hash(sets[task]), "score": score, "items": items}
        with open(os.path.join(out_dir, label.replace("/", "_") + ".json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
