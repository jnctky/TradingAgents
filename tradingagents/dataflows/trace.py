"""Structured data-source tracing for the vendor router.

Why this exists: the human-readable run log (`message_tool.log`) records what
the agents called, but not *which vendor answered*. Only a few vendors happen
to prefix their payload with a source line, so provenance was inconsistent —
technical indicators carried no origin at all. The router is the only place
that knows the answer, so it reports it here.

Output is one JSON object per line (JSONL), appended with a lock so parallel
tool calls in one run cannot interleave partial writes.

Destination resolution:
  TA_TRACE_FILE  explicit path (set per ticker by the portfolio batch runner)
  TA_TRACE_DIR   directory, then `{dir}/{YYYY-MM-DD}/{ticker}.jsonl`
  default        ~/.tradingagents/logs/data_sources/{YYYY-MM-DD}/{ticker}.jsonl

Tracing is strictly best-effort: any failure here is swallowed. Losing a trace
line must never break an analysis.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date as _date_cls
from datetime import datetime
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()

#: How much of a vendor payload travels with the trace. Enough for a human to
#: verify a figure, small enough that 40 calls stay well under a megabyte.
SAMPLE_CHARS = 2000

#: Arguments can contain long date ranges but nothing huge; cap defensively.
_ARGS_CHARS = 500


def _today() -> str:
    return _date_cls.today().isoformat()


def _resolve_path() -> Path:
    explicit = os.environ.get("TA_TRACE_FILE")
    if explicit:
        return Path(explicit)
    root = os.environ.get("TA_TRACE_DIR")
    base = Path(root) if root else Path.home() / ".tradingagents" / "logs" / "data_sources"
    ticker = os.environ.get("TA_TRACE_TICKER") or "cli"
    return base / _today() / f"{ticker}.jsonl"


def _render_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Keep a display string plus a structured copy when the kwargs are JSON-safe."""
    parts = [str(a) for a in args]
    parts += [f"{k}={v}" for k, v in kwargs.items()]
    display = ", ".join(parts)[:_ARGS_CHARS]
    safe: dict[str, Any] = {}
    for key, value in kwargs.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
    return display, safe


def _sample(result: Any) -> tuple[str, int]:
    if isinstance(result, str):
        text = result
    elif isinstance(result, (list, dict)):
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(result)
    else:
        text = repr(result)
    return text[:SAMPLE_CHARS], len(text)


def record_call(
    *,
    method: str,
    category: str,
    vendor: str | None,
    chain: list[str],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    ms: float,
    result: Any = None,
    error: str | None = None,
) -> None:
    """Append one trace record. Never raises."""
    try:
        args_display, args_struct = _render_args(args, kwargs)
        sample, chars = ("", 0) if result is None else _sample(result)
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "tool": method,
            "category": category,
            "vendor": vendor,
            "chain": list(chain),
            "args": args_struct,
            "argsDisplay": args_display,
            "ms": round(ms),
            "chars": chars,
            "ok": error is None,
        }
        if error:
            record["error"] = error[:300]
        if sample:
            record["sample"] = sample
        line = json.dumps(record, ensure_ascii=False) + "\n"
        path = _resolve_path()
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:  # noqa: BLE001 - tracing must never break a run
        pass


class stopwatch:
    """Tiny context helper: `with stopwatch() as sw: ... ; sw.ms`."""

    def __init__(self) -> None:
        self.started = 0.0
        self.ms = 0.0

    def __enter__(self) -> "stopwatch":
        self.started = time.perf_counter()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.ms = (time.perf_counter() - self.started) * 1000
