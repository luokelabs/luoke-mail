"""Local OpenAI-compatible engines: discovery, model recommendations and the chat call.

Every supported engine (oMLX, LM Studio, Ollama, llama.cpp server) speaks /v1/chat/completions
and accepts `response_format: {"type": "json_schema", ...}`, so one code path serves them all.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

KNOWN_ENGINES = [
    {"name": "oMLX", "base_url": "http://127.0.0.1:8000/v1"},
    {"name": "LM Studio", "base_url": "http://127.0.0.1:1234/v1"},
    {"name": "Ollama", "base_url": "http://127.0.0.1:11434/v1"},
    {"name": "llama.cpp", "base_url": "http://127.0.0.1:8080/v1"},
]

# Recommendations by unified-memory/RAM tier. `match` substrings are checked against the
# engine's model ids (case-insensitive) to mark what the user already has installed.
# Numbers come from bench/run_bench.py (40 synthetic emails, 12 hard; "全对" = action + suspicious flag + date),
# 2026-10-06: M5 Max 128 GB for the big models, Mac mini M1 16 GB for the 16 GB tier.
RECOMMENDATIONS = [
    {"min_gb": 96, "title": "Qwen3.8 Flash-Next（MoE，激活约 6B）", "prefer": ["oq4e"], "match": ["flash-next"],
     "why": "测试 40 封全部正确，每封约 3 秒（oQ4e 4-bit），约占 70 GB 内存。", "size_gb": 70},
    {"min_gb": 48, "title": "gemma-4-26B-A4B 或 Qwen3.6-35B-A3B（MoE）", "prefer": ["8bit", "oq6", "q8"],
     "match": ["gemma-4-26b-a4b", "qwen3.6-35b-a3b"],
     "why": "gemma 测试 40 封全部正确，每封约 2.5 秒；Qwen3.6 错 2 封，每封约 2 秒（8-bit / 6-bit），约占 27–30 GB 内存。", "size_gb": 29},
    {"min_gb": 32, "title": "Qwen3.8-27B", "prefer": ["oq4e", "4bit", "q4"], "match": ["qwen3.8-27b"],
     "why": "测试 40 封全部正确，每封约 8 秒（oQ4e 4-bit），约占 16 GB 内存。", "size_gb": 16},
    {"min_gb": 16, "title": "Ternary Bonsai 2 27B（三值量化）", "prefer": ["pq2", "mlx"], "match": ["bonsai"],
     "why": "16 GB 内存下最准确：测试 40 封错 1 封，可疑邮件全部识别；M1 上每封约 29 秒，适合后台整理。需要 oMLX 0.7 或 PrismML 版 llama.cpp，约占 8 GB 内存。",
     "size_gb": 8},
    {"min_gb": 16, "title": "Qwen3.5-9B", "prefer": ["q4_k_m", "4bit", "q4"], "match": ["qwen3.5-9b", "qwen3.5:9b"],
     "why": "速度约为 Bonsai 的两倍（M1 上每封约 16–22 秒），测试 45 封错 6 封，可疑邮件 13 封识别 12 封，"
            "偶尔把询问时间的邮件误判为日程（4-bit），约占 6 GB 内存。",
     "size_gb": 6},
]
# Below 16 GB nothing is recommended: the small models (Qwen3.5-4B, gemma-4-E4B) miss too many suspicious emails
# for the hard criteria (no false alarm, at most one suspicious email missed; bench/summarize.py). The page says
# so, and any model can still be chosen by hand.
MIN_RECOMMENDED_GB = 16


def system_memory_gb() -> float | None:
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout
            return round(int(out.strip()) / 2**30, 1)
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return round(int(line.split()[1]) / 2**20, 1)
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return round(st.ullTotalPhys / 2**30, 1)
    except Exception:  # noqa: BLE001
        return None
    return None


def cpu_name() -> str | None:
    try:
        if sys.platform == "darwin":
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                                  timeout=5).stdout.strip() or None
        return platform.processor() or None
    except Exception:  # noqa: BLE001
        return None


def machine_summary() -> dict:
    return {"os": platform.system(), "machine": platform.machine(), "cpu": cpu_name(), "memory_gb": system_memory_gb()}


def _request(url: str, api_key: str | None = None, body: dict | None = None, timeout: float = 5):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def probe(base_url: str, api_key: str | None = None, timeout: float = 1.5) -> dict:
    """{'ok': bool, 'models': [...], 'auth_required': bool, 'error': str|None}"""
    base_url = base_url.rstrip("/")
    try:
        d = _request(base_url + "/models", api_key, timeout=timeout)
        models = sorted({m.get("id") for m in d.get("data", []) if m.get("id")})
        return {"ok": True, "models": models, "auth_required": False, "error": None}
    except urllib.error.HTTPError as e:
        e.close()
        return {"ok": False, "models": [], "auth_required": e.code in (401, 403), "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001 - not running / not reachable
        return {"ok": False, "models": [], "auth_required": False, "error": type(e).__name__}


EMBEDDING_HINTS = ("embed", "rerank", "siglip", "whisper", "markitdown", "dflash", "tts", "asr")


def scan(extra: list[dict] | None = None, api_keys: dict | None = None) -> dict:
    api_keys = api_keys or {}
    engines = []
    seen = set()
    for e in KNOWN_ENGINES + (extra or []):
        base = e["base_url"].rstrip("/")
        if base in seen:
            continue
        seen.add(base)
        r = probe(base, api_keys.get(base))
        chat_models = [m for m in r["models"] if not any(h in m.lower() for h in EMBEDDING_HINTS)]
        engines.append({"name": e["name"], "base_url": base, **r, "models": chat_models})
    mem = system_memory_gb()
    recs = []
    for rec in RECOMMENDATIONS:
        installed = [(eng["name"], eng["base_url"], m) for eng in engines for m in eng["models"]
                     if any(s in m.lower() for s in rec["match"])]
        # the quant we actually benchmarked first, then MTP builds (they decode faster)
        installed.sort(key=lambda x: (not any(p in x[2].lower() for p in rec.get("prefer", [])), "mtp" not in x[2].lower()))
        recs.append({**rec, "fits": mem is None or mem >= rec["min_gb"],
                     "installed": [{"engine": n, "base_url": b, "model": m} for n, b, m in installed]})
    best = next((r for r in recs if r["fits"] and r["installed"]), None)
    return {"memory_gb": mem, "too_small": mem is not None and mem < MIN_RECOMMENDED_GB, "engines": engines, "recommendations": recs,
            "suggested": best["installed"][0] if best else None}


class EngineError(Exception):
    pass


class MemoryFull(EngineError):
    """The model service has no room: for the model at all, or (`prompt_too_big`) for this long a prompt. Not a
    connection problem, and trying a simpler request does not help; a shorter prompt might."""

    def __init__(self, message: str, prompt_too_big: bool = False):
        super().__init__(message)
        self.prompt_too_big = prompt_too_big


MEMORY_FULL = "模型服务内存不足，暂时无法处理。可以关闭其他应用，或在「设置」中换用较小的模型。"


def _memory_full(code: int, detail: str) -> MemoryFull | None:
    """oMLX refuses with 400 (prefill memory guard) or 507 (model does not fit); other servers say "out of memory"."""
    low = detail.lower()
    if code == 507 or "does not fit" in low or "memory guard" in low or "out of memory" in low:
        return MemoryFull(MEMORY_FULL, prompt_too_big="prefill" in low)
    return None


# 功耗 (设置): after every model call the next one waits `factor` times as long as that call took, so the local
# model only runs part of the time -- the machine stays cooler and quieter, and everything takes longer. It is
# here because every call to the model goes through chat_json: one setting covers sorting, 清理, 精选, 了解你.
POWER = {"full": 0.0, "balanced": 1.0, "quiet": 3.0}
_pace = {"factor": 0.0, "free_at": 0.0, "full_until": 0.0, "full_wait": 0.0}
FULL_FIRST, FULL_MAX = 120.0, 900.0        # backing off when the model does not fit: 2 min, doubling up to 15


def full_for() -> float:
    """Seconds left before background work may ask the model service again after it had no room for the model.
    Every load attempt of a big model reads tens of GB and heats the machine, so idle work waits, all of it."""
    with _pace_lock:
        return max(0.0, _pace["full_until"] - time.monotonic())
_pace_lock = threading.Lock()


def set_power(level: str) -> None:
    with _pace_lock:
        _pace["factor"] = POWER.get(level, 0.0)
        if not _pace["factor"]:
            _pace["free_at"] = 0.0                 # back to full speed: nothing waits any more


def _rest() -> None:
    while True:
        with _pace_lock:
            wait = min(_pace["free_at"] - time.monotonic(), 600.0)
        if wait <= 0:
            return
        time.sleep(min(wait, 1.0))                 # short steps: switching to 全速 takes effect at once


# ---- the two ways to a model ----------------------------------------------------------------------------
# Everything that asks a model goes through a link: `Direct` (a local OpenAI-compatible service the user picked,
# what luoke-mail has always done) or `hub.Hub` (螺壳主控's task layer, luoke-core V1 §2). Each call names its
# task (`luoke-mail.<task>`, the names in projects/luoke-mail.md) and how many items it carries; the hub decides
# the model, the output budget and the limits, the direct link ignores them.
# (0.14: no "interests" -- the memory works out the topics, luoke-mem.topics)
TASKS = ("triage", "draft", "senders", "cleanup", "digest-read", "digest-pick", "learn-sent", "learn-orgs", "thread")


class Busy(EngineError):
    """Not now: the model is busy with something more urgent, background work is paused, or this request was
    interrupted for one a person is waiting on. Nothing is wrong with the request -- ask again later
    (`retry_after` seconds, when the hub said). Background work that gets it stops and comes back."""

    def __init__(self, message: str, retry_after: float | None = None, code: str = "busy", reason: str | None = None):
        super().__init__(message)
        self.retry_after, self.code = retry_after, code
        self.reason = reason                # the hub's why (V1 §8): power_cooldown, interactive, idle_job, battery...


_later = {"until": 0.0}


def later(seconds: float) -> None:
    """The hub said background work should wait this long: every idle task waits, not just the one that asked."""
    with _pace_lock:
        _later["until"] = max(_later["until"], time.monotonic() + max(1.0, seconds))


def wait_for() -> float:
    """Seconds before background work may ask for the model again: after the model did not fit (direct), or after
    the hub said busy / paused."""
    with _pace_lock:
        hub = max(0.0, _later["until"] - time.monotonic())
    return max(full_for(), hub)


class Link:
    name = ""                                       # what answered: the model, or the hub's combination

    def chat(self, task: str, messages: list[dict], schema: dict, *, items: int = 1, max_tokens: int = 700,
             timeout: float = 300) -> tuple[str, dict, float]:
        raise NotImplementedError

    def limits(self, task: str) -> dict:
        """{"max_chars": int | None, "batch": int | None}: what one request of this task may carry; and for
        triage `sender_verdict`: False when the recipe says not to tell this base the mailbox's verdict on the
        sender (None: not said, so it is told)."""
        return {"max_chars": None, "batch": None, "sender_verdict": None}

    def ready(self, task: str) -> bool:
        """Can this task be asked for now (idle work asks before starting)."""
        return True

    def batch(self, task: str, default: int) -> int:
        b = self.limits(task).get("batch")
        return max(1, min(default, int(b))) if b else default

    def fit(self, task: str, build, n: int, least: int = 0) -> int:
        """The largest m (least <= m <= n) for which the messages `build(m)` stay within the task's character
        limit -- m being how many characters of a text, or how many entries of a list, go in. `least` when
        nothing fits: the hub then refuses, and says so."""
        limit = self.limits(task).get("max_chars")
        if not limit or chars(build(n)) <= limit:
            return n
        lo, hi = least, n                           # build(hi) does not fit
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if chars(build(mid)) <= limit:
                lo = mid
            else:
                hi = mid
        return lo

    def again(self, task: str, messages: list[dict], content: str, note: str) -> list[dict]:
        """The messages for asking once more after an unusable answer: the answer (shortened to what fits) and
        a correction."""
        limit = self.limits(task).get("max_chars")
        room = 2000 if not limit else max(0, min(2000, limit - chars(messages) - len(note)))
        return messages + [{"role": "assistant", "content": content[:room]}, {"role": "user", "content": note}]


def chars(messages: list[dict]) -> int:
    return sum(len(m.get("content") or "") for m in messages)


class Direct(Link):
    """A local OpenAI-compatible service and a model the user picked. Paced by 功耗, backs off when the model
    does not fit; the task and item count do not matter here."""

    def __init__(self, base_url: str, model: str, api_key: str | None = None):
        self.base_url, self.model, self.api_key, self.name = base_url, model, api_key, model

    def chat(self, task, messages, schema, *, items=1, max_tokens=700, timeout=300):
        return chat_json(self.base_url, self.model, messages, schema, self.api_key, max_tokens=max_tokens,
                         timeout=timeout)


def chat_json(base_url: str, model: str, messages: list[dict], schema: dict, api_key: str | None = None,
              max_tokens: int = 700, timeout: float = 300, no_think: bool = True) -> tuple[str, dict, float]:
    """Return (content, usage, seconds). Tries strict json_schema first, then degrades for engines
    that reject parts of the request (thinking switch, json_schema). Paced by 功耗."""
    _rest()
    t0 = time.monotonic()
    try:
        out = _chat_json(base_url, model, messages, schema, api_key, max_tokens, timeout, no_think)
        with _pace_lock:
            _pace["full_wait"] = 0.0                # it fits again
        return out
    except MemoryFull as ex:
        if not ex.prompt_too_big:                   # the model itself has no room: back off, longer each time
            with _pace_lock:
                _pace["full_wait"] = min(FULL_MAX, _pace["full_wait"] * 2 or FULL_FIRST)
                _pace["full_until"] = time.monotonic() + _pace["full_wait"]
        raise
    finally:
        took = time.monotonic() - t0
        with _pace_lock:
            _pace["free_at"] = max(_pace["free_at"], time.monotonic() + _pace["factor"] * took)


def _chat_json(base_url: str, model: str, messages: list[dict], schema: dict, api_key: str | None,
               max_tokens: int, timeout: float, no_think: bool) -> tuple[str, dict, float]:
    url = base_url.rstrip("/") + "/chat/completions"
    base = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
    attempts = []
    rf = {"type": "json_schema", "json_schema": {"name": "email_triage", "strict": True, "schema": schema}}
    if no_think:
        attempts.append({**base, "response_format": rf, "chat_template_kwargs": {"enable_thinking": False}})
    attempts.append({**base, "response_format": rf})
    attempts.append({**base, "response_format": {"type": "json_object"}})
    attempts.append(base)
    last = None
    for body in attempts:
        t0 = time.time()
        try:
            d = _request(url, api_key, body, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            e.close()
            print(f"[luoke-mail] model server HTTP {e.code}", file=sys.stderr)     # not its words: they can quote the mail
            full = _memory_full(e.code, detail)
            if full:
                raise full from e
            if e.code in (401, 403):
                raise EngineError("模型服务需要 API Key，或 Key 不正确。请重新连接模型服务。") from e
            if e.code == 404:
                raise EngineError(f"模型服务中没有「{model}」。请确认模型已下载，或选择其他模型。") from e
            last = EngineError("模型服务未正常响应，可能是内存不足或模型尚未加载。请稍后重试，或换用较小的模型。")
            continue   # 400/422/500: try a simpler request
        except Exception as e:  # noqa: BLE001
            print(f"[luoke-mail] model server unreachable: {type(e).__name__}", file=sys.stderr)
            if "timed out" in str(e).lower() or isinstance(e, TimeoutError):
                raise EngineError("模型响应超时。首次加载大模型可能需要一两分钟，请稍后重试。") from e
            raise EngineError("无法连接模型服务，请确认模型服务已启动。") from e
        # connected and answered -- but an answer is not always a completion (some servers report errors with 200)
        choices = d.get("choices") if isinstance(d, dict) else None
        if choices and isinstance(choices[0], dict) and "message" in choices[0]:
            return (choices[0]["message"].get("content") or "").strip(), d.get("usage", {}), time.time() - t0
        detail = json.dumps(d, ensure_ascii=False)[:300]
        print("[luoke-mail] model server answered without a completion", file=sys.stderr)
        full = _memory_full(200, detail)
        if full:
            raise full
        last = EngineError("模型服务未正常响应，可能是内存不足或模型尚未加载。请稍后重试，或换用较小的模型。")
    raise last or EngineError("模型服务暂时不可用。")
