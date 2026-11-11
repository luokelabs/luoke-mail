"""螺壳主控 (luoke-core): asking for a model through its task layer instead of a model service directly.

The contract is luoke-core's docs/V1.md §2 and §8 (frozen as contract 1): HTTP/1.1 over a Unix socket only this
macOS account can reach; every request says who is asking (`X-Luoke-Accessory: luoke-mail/<version>`) and which
task (`luoke-mail.<task>`). luoke-mail only decides the messages, the output format and how many items a request
carries; the hub picks the model, the output budget, thinking and sampling, and refuses what is too long. Its
limits come from `GET /tasks`, never written here. Generation always streams (SSE), with the hub's own
`luoke.progress` / `luoke.error` events. Before the answer starts a refusal is an HTTP status with
{code, message, retry_after}; after, an `luoke.error` event, and what came so far is thrown away.

Until the user switches to it, luoke-mail keeps talking to the model service directly (engines.Direct).
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
import time

from . import __version__
from .engines import Busy, EngineError, Link, MemoryFull, later

ACCESSORY = "luoke-mail"
DEFAULT_SOCKET = os.path.expanduser("~/Library/Application Support/luoke-core/run/hub.sock")
LAB_SOCKET = os.path.expanduser("~/Library/Application Support/luoke-core-lab/run/hub.sock")
TASKS_TTL = 60.0                    # seconds a `GET /tasks` answer is used at most (sooner when its version changed)
PATIENCE = 600.0                    # how long a waiting request keeps asking while the hub says busy

NOT_RUNNING = "螺壳没有运行。打开螺壳后重试。"


def socket_path(explicit: str | None = None) -> str | None:
    """Where the hub listens. A socket named (the settings, else `LUOKE_HUB_SOCKET` for luoke-core's lab builds)
    is that one or none -- never another hub instead: a test or a dev build must not end up at the
    hub in daily use. Nothing named: the released hub's place, then the lab build's."""
    named = explicit or os.environ.get("LUOKE_HUB_SOCKET")
    if named:
        return named if os.path.exists(named) else None
    for p in (DEFAULT_SOCKET, LAB_SOCKET):
        if os.path.exists(p):
            return p
    return None


class _Unix(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.unix_path)
        except OSError:
            s.close()
            raise
        self.sock = s


class Refused(EngineError):
    """The hub said no (a reason code): `code`, and `retry_after` seconds when asking again makes sense."""

    def __init__(self, message: str, code: str, retry_after: float | None = None, detail: str = ""):
        super().__init__(message)
        self.code, self.retry_after, self.detail = code, retry_after, detail


def _refusal(code: str, data: dict) -> EngineError:
    """A reason code as luoke-mail tells it: in a sentence for the page, only the code to stderr."""
    detail = str(data.get("message") or "")
    print(f"[luoke-mail] hub refused: {code}", file=sys.stderr)
    retry = data.get("retry_after")
    retry = float(retry) if isinstance(retry, (int, float)) else None
    if code in ("busy", "paused", "interrupted", "not_ready"):
        text = {"paused": "螺壳已暂停后台任务，稍后会继续。",
                "not_ready": "模型正在加载，请稍后重试。"}.get(code, "模型正忙，稍后会继续。")
        return Busy(text, retry if retry is not None else (60.0 if code == "paused" else 30.0), code, data.get("reason"))
    if code == "needs_user":
        names = "、".join(dict.fromkeys(o.get("name", "") for o in data.get("occupants") or [] if o.get("name")))
        return MemoryFull("本机内存不足，暂时无法处理" + (f"（正在占用内存：{names}）" if names else "")
                          + "。关闭占用内存的其他应用后重试。")
    if code == "task_unavailable":
        return Refused("螺壳暂时无法处理这项任务" + (f"：{detail}" if detail else "。"), code, retry, detail)
    if code == "unsupported":
        return Refused("这台 Mac 上的螺壳不支持这项功能。", code, None, detail)
    if code == "engine_failed":
        return Refused("模型出错，请稍后重试。", code, retry, detail)
    if code == "input_too_long":
        return Refused("邮件太长，无法处理。", code, None, detail)
    return Refused("螺壳拒绝了这个请求。", code, None, detail)


_cache: dict = {}
_down: dict = {}            # socket path -> (monotonic, error): /tasks failed just now (luoke-mail#24)
DOWN_TTL = 5
_cache_lock = threading.Lock()


def _note_version(path: str | None, response) -> None:
    """Every answer from the hub says which version of its task list is current (`X-Luoke-Tasks-Version`;
    recipe, tier, models, any parameter, the engine): when it is not the one luoke-mail read, read again --
    limits and batch sizes change when the backend or the recipe does."""
    v = response.getheader("X-Luoke-Tasks-Version") if response is not None else None
    if not v:
        return
    with _cache_lock:
        hit = _cache.get(path)
        if hit and str(hit[1].get("tasks_version")) != v:
            del _cache[path]
LEASE_TASK = f"{ACCESSORY}.triage"     # what a warm-up asks for: the base model every task of luoke-mail uses


class Warmth:
    """While the page is open and visible the hub keeps the model loaded (a warm-up lease, renewed halfway
    through), so a draft or a sync starts without waiting for a load. Asked in the background: checking in must
    never wait on the hub. The hub may say not now (low power, memory pressure): asked again a minute later."""

    def __init__(self):
        self.lease, self.next, self.lock = None, float("-inf"), threading.Lock()

    def keep(self, path: str | None) -> None:
        with self.lock:
            now = time.monotonic()
            if now < self.next:
                return
            self.next, lease = now + 30, self.lease          # no second ask while this one is out
        threading.Thread(target=self._ask, args=(path, lease), daemon=True).start()

    def _ask(self, path: str | None, lease: str | None) -> None:
        try:
            r = Hub(path).call("POST", "/warmup", {"task": LEASE_TASK, **({"lease_id": lease} if lease else {})})
        except EngineError:
            r = {}
        with self.lock:
            if r.get("accepted"):
                self.lease = r.get("lease_id")
                self.next = time.monotonic() + max(5.0, float(r.get("expires_in") or 60) / 2)
            else:
                self.lease, self.next = None, time.monotonic() + 60


class Hub(Link):
    """One way of asking: which socket, how urgent (`user-waiting` for a single answer someone is watching,
    `background` for the rest), whether background work the user started may run on battery, and -- for work
    that should wait rather than give up -- `patient`, a function saying whether to stop waiting (cancelled)."""

    def __init__(self, path: str | None = None, priority: str = "background", battery: bool = False,
                 patient=None, progress=None, stop=None):
        self.path = path or socket_path()
        self.priority, self.battery, self.patient = priority, battery, patient
        self.stop = stop or patient         # the work was cancelled: stop waiting out the hub's pace
        self.progress = progress            # progress(stage[, seconds]): "queued" / "loading" / "running", as the hub
                                            # reports; "cooldown" while its pace between background requests holds us
        self.name = "螺壳"

    def level(self) -> str:
        return self.priority() if callable(self.priority) else self.priority

    # ---- plain calls ----------------------------------------------------------------------------------
    def _conn(self, timeout: float) -> _Unix:
        if not self.path:
            raise EngineError(NOT_RUNNING)
        return _Unix(self.path, timeout)

    def _headers(self, extra: dict | None = None) -> dict:
        return {"X-Luoke-Accessory": f"{ACCESSORY}/{__version__}", "Content-Type": "application/json", **(extra or {})}

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 10) -> dict:
        try:
            c = self._conn(timeout)
            try:
                c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                          headers=self._headers())
                r = c.getresponse()
                raw = r.read()
                if path != "/tasks":
                    _note_version(self.path, r)
            finally:
                c.close()
        except EngineError:
            raise
        except (OSError, http.client.HTTPException) as ex:
            print(f"[luoke-mail] hub unreachable: {type(ex).__name__}", file=sys.stderr)
            raise EngineError(NOT_RUNNING) from ex
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            data = {}
        if r.status >= 400:
            raise _refusal(str(data.get("code") or r.status), data)
        return data

    def tasks(self, fresh: bool = False) -> dict:
        """`GET /tasks`, kept for a minute: this accessory's tasks with their status and parameters."""
        with _cache_lock:
            hit = _cache.get(self.path)
            if hit and not fresh and time.monotonic() - hit[0] < TASKS_TTL:
                return hit[1]
            down = _down.get(self.path)
            if down and not fresh and time.monotonic() - down[0] < DOWN_TTL:
                raise down[1]           # not running a moment ago: every page poll need not knock (and log) again
        try:
            data = self.call("GET", "/tasks")
        except EngineError as ex:
            with _cache_lock:
                _down[self.path] = (time.monotonic(), ex)
            raise
        with _cache_lock:
            _cache[self.path] = (time.monotonic(), data)
            _down.pop(self.path, None)
        return data

    def task(self, task: str) -> dict:
        return ((self.tasks().get("tasks") or {}).get(f"{ACCESSORY}.{task}")) or {}

    def ready(self, task: str) -> bool:
        """The hub has this task and it is available now: not missing from the recipe (no evaluation yet), not
        waiting for a download, not paused. Unreachable counts as not ready."""
        try:
            return self.task(task).get("status") == "available"
        except EngineError:
            return False

    def limits(self, task: str) -> dict:
        try:
            params = self.task(task).get("params") or {}
        except EngineError:
            return {"max_chars": None, "batch": None, "sender_verdict": None}    # the request will say what is wrong
        return {"max_chars": params.get("max_chars"), "batch": params.get("batch"),
                "sender_verdict": params.get("sender_verdict")}

    # ---- generation -----------------------------------------------------------------------------------
    def chat(self, task, messages, schema, *, items=1, max_tokens=700, timeout=300):
        """(content, usage, seconds). `max_tokens` is the hub's to decide (per item, in the recipe)."""
        body = {"messages": messages, "stream": True, "luoke_items": max(1, int(items)),
                "response_format": {"type": "json_schema", "json_schema": {"name": task.replace("-", "_"), "schema": schema}}}
        waited = 0.0
        while True:
            try:
                return self._chat(task, body, timeout)
            except Busy as ex:
                wait = ex.retry_after or 30.0
                # the hub's pace between background requests (功耗) is not more urgent work: wait it out here.
                # Giving up made several-request steps (了解你's organisations, a month of 精选) start over after
                # every refusal, asking the same thing all day (2026-10-15)
                pacing = ex.code == "busy" and ex.reason == "power_cooldown"
                if self.level() == "background" and not pacing:
                    later(wait)                       # every idle task waits, not just this one
                if waited + wait > PATIENCE or not pacing and (self.patient is None or ex.code == "paused"):
                    raise                             # paused (on battery, too hot...): not worth waiting here
                if pacing and self.progress:
                    self.progress("cooldown", wait)       # say it waits, not stuck (luoke-mail#25)
                end = time.monotonic() + wait
                while time.monotonic() < end:
                    if self.stop and self.stop():
                        raise
                    time.sleep(min(1.0, end - time.monotonic()))
                waited += wait
                if pacing and self.progress:
                    self.progress("running")

    def _chat(self, task: str, body: dict, timeout: float) -> tuple[str, dict, float]:
        t0 = time.time()
        headers = {"X-Luoke-Priority": self.level()}
        if self.battery:
            headers["X-Luoke-Allow-Battery"] = "1"
        c = self._conn(timeout)
        try:
            try:
                c.request("POST", f"/tasks/{ACCESSORY}.{task}/v1/chat/completions",
                          body=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=self._headers(headers))
                r = c.getresponse()
            except (OSError, http.client.HTTPException) as ex:
                print(f"[luoke-mail] hub unreachable: {type(ex).__name__}", file=sys.stderr)
                raise EngineError(NOT_RUNNING) from ex
            _note_version(self.path, r)
            if r.status != 200:
                raw = r.read()
                try:
                    data = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    data = {}
                raise _refusal(str(data.get("code") or r.status), data)
            parts, usage, event = [], {}, None
            try:
                while True:
                    raw = r.readline()
                    if not raw:
                        raise EngineError("模型的回答没有完整传回，请稍后重试。")
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("event: "):
                        event = line[7:].strip()
                    elif line.startswith("data: "):
                        data = line[6:]
                        if event == "luoke.error":
                            raise _refusal(str(json.loads(data).get("code") or "engine_failed"), json.loads(data))
                        if event:                     # luoke.progress: queued / loading / running
                            if event == "luoke.progress" and self.progress:
                                self.progress(str(json.loads(data).get("stage") or ""))
                            event = None
                            continue
                        if data.strip() == "[DONE]":
                            break
                        chunk = json.loads(data)
                        if chunk.get("model"):
                            self.name = chunk["model"]
                        usage = chunk.get("usage") or usage
                        for ch in chunk.get("choices") or []:
                            piece = (ch.get("delta") or {}).get("content")
                            if piece:
                                parts.append(piece)
                    elif not line:
                        event = None
            except (OSError, http.client.HTTPException, ValueError) as ex:
                print(f"[luoke-mail] hub stream broke: {type(ex).__name__}", file=sys.stderr)
                if isinstance(ex, socket.timeout):
                    raise EngineError("模型响应超时，请稍后重试。") from ex
                raise EngineError("模型的回答没有完整传回，请稍后重试。") from ex
        finally:
            c.close()
        return "".join(parts).strip(), usage, time.time() - t0
