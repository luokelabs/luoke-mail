#!/usr/bin/env python3
"""Email-triage benchmark: run the synthetic inbox through any OpenAI-compatible local model.

  python3 bench/run_bench.py --base-url http://127.0.0.1:8000/v1 --model Qwen3.8-27B-oQ4e-mtp [--api-key KEY]
  python3 bench/run_bench.py --hub SOCKET --label "..."     # through 螺壳主控's task luoke-mail.triage (a hub named)
  ... --label "<label of an earlier result>" --fill   # only the items added since; merged into that result

Scores (each email lists the acceptable answers, see luoke_mail/samples.py):
  action      suggested action is acceptable                       (the main number)
  safety      phishing / prompt-injection emails flagged suspicious (recall)
  false_alarm normal emails wrongly flagged suspicious
  dates       exact due date / event date on emails that state one
  false_due   a due date invented for mail that needs nothing (effective dates, promo dates...)
  priority    acceptable priority on non-suspicious emails
  json        valid output on the first try
  basic/hard  emails fully right (action + suspicious flag + the expected date), per level
Stdlib only. Results -> bench/results/<label>.json, or <DIR>/<label>.json with --out DIR (luoke-core's release
script keeps its evidence in its own folder)
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from luoke_mail import engines, hub, samples, triage  # noqa: E402


def score(items: list[dict], results: list[dict]) -> dict:
    """`watch` questions are new and only observed: they count in nothing below, and show in `watch` / `watch_n`."""
    pairs = list(zip(items, results))
    watched = [(it, r) for it, r in pairs if it.get("watch")]
    items = [it for it, _ in pairs if not it.get("watch")]
    results = [r for it, r in pairs if not it.get("watch")]
    n = len(items)
    ok = lambda xs: sum(1 for x in xs if x)  # noqa: E731
    sus = [(it, r) for it, r in zip(items, results) if it["expect"]["suspicious"]]
    normal = [(it, r) for it, r in zip(items, results) if not it["expect"]["suspicious"]]
    dated = [(it, r) for it, r in zip(items, results) if it["expect"]["due"] or it["expect"]["event_date"]]
    quiet = [(it, r) for it, r in zip(items, results)
             if not it["expect"]["due"] and not it["expect"]["event_date"] and it["expect"]["action"] <= {"archive", "ignore"}]

    def date_ok(it, t):
        e = it["expect"]
        if e.get("date_optional") and not t.get("due_date") and not (t.get("event") or {}).get("date"):
            return True
        if e["due"] and t.get("due_date") != e["due"]:
            return False
        if e["event_date"] and ((t.get("event") or {}).get("date") != e["event_date"]):
            return False
        if e.get("more_dates") and [x.get("date") for x in (t.get("event") or {}).get("more") or []] != e["more_dates"]:
            return False
        return True
    pri = [(it, r) for it, r in normal if it["expect"]["priority"]]
    good = [r for r in results if r.get("triage")]

    def fully_right(it, r):
        t, e = r.get("triage"), it["expect"]
        return bool(t) and t["action"] in e["action"] and t["suspicious"] == e["suspicious"] and date_ok(it, t)
    levels = {lv: [fully_right(it, r) for it, r in zip(items, results) if it.get("level", "basic") == lv] for lv in ("basic", "hard")}
    return {
        "watch": ok(fully_right(it, r) for it, r in watched), "watch_n": len(watched),
        "watch_wrong": [it["tag"] for it, r in watched if not fully_right(it, r)],
        "basic": ok(levels["basic"]), "basic_n": len(levels["basic"]),
        "hard": ok(levels["hard"]), "hard_n": len(levels["hard"]),
        "n": n,
        "action": ok(r.get("triage") and r["triage"]["action"] in it["expect"]["action"] for it, r in zip(items, results)),
        "safety": ok(r.get("triage") and r["triage"]["suspicious"] for _, r in sus), "safety_n": len(sus),
        "false_alarm": ok(r.get("triage") and r["triage"]["suspicious"] for _, r in normal), "normal_n": len(normal),
        "dates": ok(r.get("triage") and date_ok(it, r["triage"]) for it, r in dated), "dates_n": len(dated),
        "false_due": ok(r.get("triage") and r["triage"].get("due_date") for _, r in quiet), "quiet_n": len(quiet),
        "priority": ok(r.get("triage") and r["triage"]["priority"] in it["expect"]["priority"] for it, r in pri), "priority_n": len(pri),
        "json_first_try": ok(r.get("triage") and not r["triage"]["retried"] for r in results),
        "failed": n - len(good),
        "avg_seconds": round(sum(r["triage"]["seconds"] for r in good) / max(len(good), 1), 2),
        "avg_tokens_out": round(sum(r["triage"]["tokens_out"] for r in good) / max(len(good), 1)),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url")
    ap.add_argument("--model")
    ap.add_argument("--hub", default=None, metavar="SOCKET",
                    help="ask 螺壳主控 (luoke-core's task layer) at this socket instead -- named, never found on its own")
    ap.add_argument("--api-key", default=os.environ.get("LUOKE_API_KEY"))
    ap.add_argument("--label", default=None, help="name in the results file (default: model id)")
    ap.add_argument("--out", default=os.path.join(ROOT, "bench", "results"), help="where the result goes")
    ap.add_argument("--warmup", action="store_true", help="send one untimed request first (model load)")
    ap.add_argument("--fill", action="store_true", help="run only the items the result with this label lacks (same "
                    "model and machine as before), and merge them into it")
    a = ap.parse_args()
    if a.hub is None and not (a.base_url and a.model):
        ap.error("--base-url and --model, or --hub")
    if a.hub is not None:
        if not os.path.exists(a.hub):
            ap.error(f"{a.hub} 不存在：螺壳主控没有在那里运行")
        link = hub.Hub(a.hub, "user-waiting")               # one at a time, the result watched: as a person would
        a.model, a.base_url = a.model or "luoke-core", "unix:" + link.path
    else:
        link = engines.Direct(a.base_url, a.model, a.api_key)

    label = a.label or a.model
    out_dir = a.out
    path = os.path.join(out_dir, label.replace("/", "_") + ".json")
    every = samples.build()
    earlier = None
    if a.fill:
        with open(path, encoding="utf-8") as f:
            earlier = json.load(f)
        have = {r["tag"] for r in earlier["results"] if r.get("triage")}
        items = [it for it in every if it["tag"] not in have]
        print(f"filling {len(items)} item(s) into {path}: {', '.join(it['tag'] for it in items)}")
    else:
        items = every
    if a.warmup:
        try:
            triage.analyze(items[0]["mail"], link, today=samples.TODAY, user_address=samples.ME)
        except Exception as e:  # noqa: BLE001
            print("warmup failed:", e)
    results = []
    t0 = time.time()
    for i, it in enumerate(items, 1):
        r = {"tag": it["tag"]}
        try:
            r["triage"] = triage.analyze(it["mail"], link, today=samples.TODAY, user_address=samples.ME)
            t = r["triage"]
            mark = "ok " if t["action"] in it["expect"]["action"] else "XX "
            ev = (t.get("event") or {}).get("date")
            print(f"{i:2d} {mark}{it['tag']:18s} {t['action']:9s} {t['priority']:6s} due={t['due_date']} ev={ev} sus={t['suspicious']}{' cleared' if t.get('cleared') else ''}{' risks=' + ','.join(t['risks']) if t.get('risks') else ''} {t['seconds']}s", flush=True)
        except Exception as e:  # noqa: BLE001
            r["error"] = str(e)[:300]
            print(f"{i:2d} ERR {it['tag']:18s} {r['error'][:100]}", flush=True)
        results.append(r)
    wall = round(time.time() - t0, 1)
    filled = None
    if earlier:                                    # the earlier answers, then the new ones, in sample order
        new = {r["tag"]: r for r in results}
        old = {r["tag"]: r for r in earlier["results"]}
        results = [new.get(it["tag"]) or old[it["tag"]] for it in every if it["tag"] in new or it["tag"] in old]
        items = [it for it in every if it["tag"] in new or it["tag"] in old]
        filled = (earlier.get("filled") or []) + [{"date": time.strftime("%Y-%m-%d"), "tags": sorted(new)}]
    s = score(items, results)
    s["wall_seconds"] = wall
    print(f"\n{label}: basic {s['basic']}/{s['basic_n']} · hard {s['hard']}/{s['hard_n']} · action {s['action']}/{s['n']} · safety {s['safety']}/{s['safety_n']} · false alarm {s['false_alarm']}/{s['normal_n']}"
          f" · dates {s['dates']}/{s['dates_n']} · false due {s['false_due']}/{s['quiet_n']} · priority {s['priority']}/{s['priority_n']}"
          f" · json first try {s['json_first_try']}/{s['n']} · {s['avg_seconds']}s/email")
    os.makedirs(out_dir, exist_ok=True)
    out = {"label": label, "model": link.name if a.hub is not None else a.model, "base_url": a.base_url, "machine": engines.machine_summary(),
           "python": platform.python_version(), "date": time.strftime("%Y-%m-%d"), "score": s, "results": results}
    if earlier:                                    # first run's date and machine stay; the fill is recorded
        out.update({k: earlier[k] for k in ("model", "base_url", "machine", "python", "date") if k in earlier})
        out.update({k: earlier[k] for k in ("source",) if k in earlier}, filled=filled)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved", path)


if __name__ == "__main__":
    main()
