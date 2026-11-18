"""Why something stopped, as a code: the window words it (top-level ADR 0002, 2026-11-17), the old pages keep
the sentence. A model or mailbox failure carries its own code (`EngineError.code`, `MailError.code`); the rest is
told by its kind. The codes are listed in docs/agent-data.md (「停下的原因」)."""
from __future__ import annotations

import socket

from .engines import EngineError
from .mailbox import MailError


def code_of(ex: BaseException) -> str:
    code = getattr(ex, "code", None)
    if isinstance(ex, (EngineError, MailError)) and isinstance(code, str) and code:
        return code
    if isinstance(ex, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(ex, OSError):
        return "network"
    if isinstance(ex, ValueError):           # what the request or the setting does not allow, said by the code
        return "invalid"
    return "error"
