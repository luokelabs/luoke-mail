#!/usr/bin/env python3
"""luoke-mail's background work through 螺壳主控, on demo data: load for the hub's coexistence tests (luoke-core
V1, "要验证的三件事": interactive scoring next to background batches on a 16 GB Mac).

  python3 bench/background_load.py --hub SOCKET [--rounds 3] [--tasks triage,cleanup,digest-read] [--long] [--json FILE]

Asks the way luoke-mail's background work does (docs/DESIGN.md「接入螺壳主控」), with the same code:
- triage: background sorting -- the 44 demo-inbox messages, one request each, `background`; when the hub says
  busy / interrupted / not_ready it waits `retry_after` and asks again (up to 10 minutes), paused is not waited for.
- cleanup: idle work -- the demo archive's headers, as many per request as the hub's batch size and character
  limit allow; on busy it stops, waits what the hub said (every idle task waits), and goes on.
- digest-read: idle work -- the demo newsletters, one per request. They are short; `--long` pads each to the
  task's character limit (real newsletters are cut to it), the longest prefill luoke-mail sends.
No mailbox, no data folder, nothing written. The hub is the one named with `--hub SOCKET`, never found on its own
(on a machine in daily use that would be the hub in use).
Each request is printed (task, items, characters, seconds, waits, outcome); a summary per task at the end.
Stdlib only, Python 3.9 (/usr/bin/python3).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from luoke_mail import digest, engines, hub, samples, triage  # noqa: E402

LOG: list[dict] = []


class Watched(hub.Hub):
    """The hub link luoke-mail uses, recording each request: how long, how many times told to wait, how it ended."""

    def chat(self, task, messages, schema, *, items=1, max_tokens=700, timeout=300):
        rec = {"task": task, "items": items, "chars": engines.chars(messages), "waits": 0, "waited": 0.0,
               "outcome": "ok", "at": time.strftime("%H:%M:%S")}
        real = self._chat

        def counting(t, body, to):
            try:
                return real(t, body, to)
            except engines.Busy as ex:
                rec["waits"] += 1
                rec["waited"] += ex.retry_after or 30.0
                rec["last_refusal"] = ex.code
                raise
        self._chat = counting
        t0 = time.monotonic()
        try:
            return super().chat(task, messages, schema, items=items, max_tokens=max_tokens, timeout=timeout)
        except engines.Busy as ex:
            rec["outcome"] = ex.code
            raise
        except engines.EngineError as ex:
            rec["outcome"] = getattr(ex, "code", None) or "error"
            rec["error"] = str(ex)
            raise
        finally:
            self._chat = real
            rec["secs"] = round(time.monotonic() - t0, 2)
            LOG.append(rec)
            print(f"{rec['at']} {task:12s} items={items:<3d} chars={rec['chars']:<6d} {rec['secs']:7.1f}s "
                  f"waits={rec['waits']} {rec['outcome']}", flush=True)


def idle(fn):
    """Idle work: on busy it stops; every idle task waits what the hub said, then this one goes on."""
    while True:
        try:
            return fn()
        except engines.Busy:
            time.sleep(max(1.0, engines.wait_for()))


def run_triage(path: str) -> None:
    link = Watched(path, "background", patient=lambda: False)
    for m in samples.demo_inbox():
        try:
            triage.analyze(m, link, today=samples.TODAY, user_address=samples.ME)
        except engines.Busy:
            pass                                  # paused: luoke-mail leaves the rest for the next round
        except engines.EngineError:
            pass


def run_cleanup(path: str) -> None:
    link = Watched(path, "background")
    rows = [{**r, "message_id": r.get("message_id") or f"<a{i}@demo>"} for i, r in enumerate(samples.demo_archive())]
    i = 0
    while i < len(rows):
        try:
            _, count = idle(lambda: triage.classify_old(rows[i:], link, samples.TODAY, "archive"))
        except engines.EngineError:
            count = triage.OLD_BATCH
        i += count


def run_digest(path: str, long: bool) -> None:
    link = Watched(path, "background")
    limit = link.limits("digest-read").get("max_chars") or digest.TEXT_LIMIT
    for i, addr in enumerate(samples.NEWSLETTER_POOLS):          # the demo's newsletters, one issue each
        row = {"message_id": f"<load-{i}@demo>", "from_addr": addr, "subject": f"{addr.split('@')[1]} #{i + 1}",
               "date": samples.TODAY.isoformat()}
        mail = digest.read(samples.demo_newsletter(row))
        if long:
            mail["text"] = (mail["text"] + "\n\n") * (limit // max(1, len(mail["text"])) + 1)
        try:
            idle(lambda: digest.extract(mail, "", [], [], dt.date.today(), link))
        except engines.EngineError:
            pass


def summary() -> dict:
    out = {}
    for task in sorted({r["task"] for r in LOG}):
        rs = [r for r in LOG if r["task"] == task]
        secs = sorted(r["secs"] for r in rs if r["outcome"] == "ok")
        out[task] = {"requests": len(rs), "ok": len(secs), "outcomes": {o: sum(1 for r in rs if r["outcome"] == o) for o in {r["outcome"] for r in rs}},
                     "secs_mean": round(sum(secs) / len(secs), 2) if secs else None,
                     "secs_p90": secs[int(0.9 * (len(secs) - 1))] if secs else None,
                     "told_to_wait": sum(r["waits"] for r in rs), "waited_secs": round(sum(r["waited"] for r in rs), 1)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hub", required=True, metavar="SOCKET", help="the hub to load: one started for testing, named")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--tasks", default="triage,cleanup,digest-read")
    ap.add_argument("--long", action="store_true", help="pad newsletters to the task's character limit")
    ap.add_argument("--json", default=None, help="write every request and the summary here")
    a = ap.parse_args()
    if not os.path.exists(a.hub):              # that one or none: never another hub instead
        ap.error(f"{a.hub} 不存在：螺壳主控没有在那里运行")
    path = a.hub
    tasks = [t.strip() for t in a.tasks.split(",") if t.strip()]
    print(f"hub {path}; tasks {tasks}; rounds {a.rounds}; tier {hub.Hub(path).tasks().get('tier')}", flush=True)
    t0 = time.time()
    for n in range(1, a.rounds + 1):
        print(f"--- round {n}", flush=True)
        for t in tasks:
            {"triage": lambda: run_triage(path), "cleanup": lambda: run_cleanup(path),
             "digest-read": lambda: run_digest(path, a.long)}[t]()
    s = {"wall_secs": round(time.time() - t0, 1), "tasks": summary()}
    print(json.dumps(s, ensure_ascii=False, indent=1))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"summary": s, "requests": LOG}, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
