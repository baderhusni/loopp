"""Run tracing.

A "run" is one turn of the agent responding to one customer message. While the
agent loop executes, tools append events to a per-run :class:`RunContext` stored
in a context variable. The agent layer also appends assistant-text events and a
final summary. Completed runs are kept in memory and served to the admin
dashboard so a reviewer can inspect tool I/O, retries/failures, token usage and
latency for any run.
"""

from __future__ import annotations

import contextvars
import itertools
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any

_ctx: contextvars.ContextVar["RunContext | None"] = contextvars.ContextVar("run_ctx", default=None)
_run_ids = itertools.count(1)


class RunContext:
    """Accumulates ordered events for a single agent run."""

    def __init__(self, run_id: str, conversation_id: str, user_message: str, emitter=None):
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.user_message = user_message
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._start_perf = time.perf_counter()
        self._seq = itertools.count(1)
        self.events: list[dict] = []
        # Optional live listener (e.g. an SSE queue). Receives each event as it
        # happens so the "Live agent" view can render the run in real time.
        self._emitter = emitter

    def _push(self, event: dict) -> None:
        if self._emitter is not None:
            try:
                self._emitter(dict(event))
            except Exception:  # never let a live listener break the run
                pass

    def add(self, event: dict[str, Any]) -> dict:
        event = dict(event)
        event["seq"] = next(self._seq)
        event["t_ms"] = round((time.perf_counter() - self._start_perf) * 1000, 1)
        self.events.append(event)
        self._push(event)
        return event

    def emit(self, event: dict[str, Any]) -> None:
        """Send a stream-only event to the live listener WITHOUT storing it in the
        saved trace (e.g. a 'tool is being called' indicator)."""
        event = dict(event)
        event["t_ms"] = round((time.perf_counter() - self._start_perf) * 1000, 1)
        self._push(event)

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._start_perf) * 1000, 1)


def new_run_id() -> str:
    return f"run-{next(_run_ids):04d}"


def start(run_id: str, conversation_id: str, user_message: str, emitter=None) -> tuple[RunContext, Any]:
    ctx = RunContext(run_id, conversation_id, user_message, emitter=emitter)
    token = _ctx.set(ctx)
    return ctx, token


def record_tool_start(name: str, tool_input: dict) -> None:
    """Stream-only signal that a tool is about to run (for the live view)."""
    ctx = current()
    if ctx is not None:
        ctx.emit({"type": "tool_start", "name": name, "input": tool_input})


def finish(token: Any) -> None:
    _ctx.reset(token)


def current() -> RunContext | None:
    return _ctx.get()


def record_tool(name: str, tool_input: dict, output: Any, latency_ms: float, is_error: bool) -> None:
    """Called by the tool layer for every tool invocation."""
    ctx = current()
    if ctx is None:
        return
    ctx.add({
        "type": "tool",
        "name": name,
        "input": tool_input,
        "output": output,
        "latency_ms": round(latency_ms, 1),
        "is_error": is_error,
    })


def record_assistant_text(text: str) -> None:
    ctx = current()
    if ctx is None:
        return
    ctx.add({"type": "assistant", "text": text})


def record_info(message: str, **extra: Any) -> None:
    ctx = current()
    if ctx is None:
        return
    ctx.add({"type": "info", "message": message, **extra})


# ----------------------------- run storage ----------------------------------

_runs: dict[str, dict] = {}
_order: list[str] = []
_store_lock = Lock()


def save_run(record: dict) -> None:
    with _store_lock:
        _runs[record["run_id"]] = record
        _order.append(record["run_id"])


def get_run(run_id: str) -> dict | None:
    return _runs.get(run_id)


def list_runs(limit: int = 100) -> list[dict]:
    """Return run summaries, newest first."""
    with _store_lock:
        ids = list(reversed(_order))[:limit]
    out = []
    for rid in ids:
        r = _runs[rid]
        out.append({
            "run_id": r["run_id"],
            "conversation_id": r["conversation_id"],
            "started_at": r["started_at"],
            "user_message": r["user_message"],
            "decision": r.get("decision"),
            "engine": r.get("engine"),
            "duration_ms": r.get("duration_ms"),
            "num_tool_calls": sum(1 for e in r["events"] if e["type"] == "tool"),
            "num_errors": sum(1 for e in r["events"] if e["type"] == "tool" and e.get("is_error")),
            "usage": r.get("usage"),
        })
    return out
