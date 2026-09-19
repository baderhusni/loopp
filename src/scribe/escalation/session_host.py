"""Single-owner access to the live browser session.

The operator console and the automation drive the *same* page -- that is the
whole point of the handoff; a fresh session would lose the member context, the
half-filled form, and the sign-on. But Playwright's sync API is bound to the
thread that created it, so the console's HTTP handlers cannot touch the page
directly.

So ownership and authority are separated:

  * one thread owns the browser for the life of the run and is the only thread
    that ever calls into Playwright;
  * everyone else submits commands to a queue and waits for the result;
  * the control token decides *whose* commands are accepted.

The automation is idle while a human holds control -- it is sitting in
`wait_for_resolution`, pumping this queue -- so the operator's clicks execute
promptly on the owning thread, and there is never a moment when two parties are
driving.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .control import ControlToken


@dataclass
class SessionCommand:
    kind: str                      # click | type | key | navigate | screenshot | info
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    _done: threading.Event = field(default_factory=threading.Event, repr=False)
    result: Any = None
    error: str = ""

    def wait(self, timeout: float = 15.0) -> Any:
        if not self._done.wait(timeout):
            raise TimeoutError(f"session command {self.kind} timed out")
        if self.error:
            raise RuntimeError(self.error)
        return self.result


class SessionHost:
    """Owns one live surface. Commands run on the owning thread, in order."""

    def __init__(self, surface, token: ControlToken) -> None:
        self.surface = surface
        self.token = token
        self._queue: "queue.Queue[SessionCommand]" = queue.Queue()
        self._owner_thread = threading.get_ident()
        self._last_png: bytes | None = None
        self._last_png_at: float = 0.0
        self._lock = threading.Lock()
        self.on_human_action: Callable[[str, str], None] | None = None

    # -- caller side (any thread) ----------------------------------------
    def submit(self, kind: str, **payload: Any) -> SessionCommand:
        cmd = SessionCommand(kind=kind, payload=payload)
        self._queue.put(cmd)
        return cmd

    def screenshot(self, max_age: float = 0.4, timeout: float = 15.0) -> bytes | None:
        """Cached so a polling console does not queue a screenshot per frame."""
        with self._lock:
            fresh = (self._last_png is not None
                     and (time.monotonic() - self._last_png_at) < max_age)
            if fresh:
                return self._last_png
        if threading.get_ident() == self._owner_thread:
            return self._capture()
        try:
            return self.submit("screenshot").wait(timeout)
        except (TimeoutError, RuntimeError):
            with self._lock:
                return self._last_png

    # -- owner side ------------------------------------------------------
    def pump(self, timeout: float = 0.1) -> int:
        """Execute queued commands. Called by the thread that owns the browser."""
        executed = 0
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                cmd = self._queue.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            try:
                cmd.result = self._execute(cmd)
            except Exception as exc:
                cmd.error = f"{type(exc).__name__}: {exc}"
            finally:
                cmd._done.set()
                executed += 1
        return executed

    def _execute(self, cmd: SessionCommand) -> Any:
        page = self.surface.page
        kind, p = cmd.kind, cmd.payload
        if kind == "screenshot":
            return self._capture()
        if kind == "info":
            return {"url": page.url, "title": self._title(page)}
        if kind == "observe":
            # The same control graph the automation perceives, handed to the
            # operator so the console can say what is on screen and where --
            # rather than making a person guess at pixels.
            obs = self.surface.observe()
            return {
                "url": obs.url,
                "controls": [
                    {"role": e.role, "name": e.name, "value": e.value,
                     "enabled": e.enabled, "frame": "/".join(e.frame_path),
                     "bbox": list(e.bbox) if e.bbox else None}
                    for e in obs.elements],
            }
        if kind == "click":
            page.mouse.click(float(p["x"]), float(p["y"]))
            self._note("click", f"clicked at ({p['x']:.0f}, {p['y']:.0f})")
        elif kind == "type":
            page.keyboard.type(str(p.get("text", "")))
            self._note("type", f"typed {len(str(p.get('text', '')))} character(s)")
        elif kind == "key":
            page.keyboard.press(str(p.get("key", "Enter")))
            self._note("key", f"pressed {p.get('key')}")
        elif kind == "navigate":
            page.goto(str(p["url"]), wait_until="domcontentloaded")
            self._note("navigate", f"navigated to {p['url']}")
        else:
            raise ValueError(f"unknown session command {kind!r}")
        try:
            page.wait_for_timeout(120)
        except Exception:
            pass
        self._capture()
        return {"url": page.url}

    def _capture(self) -> bytes | None:
        try:
            png = self.surface.page.screenshot(full_page=False)
        except Exception:
            return self._last_png
        with self._lock:
            self._last_png, self._last_png_at = png, time.monotonic()
        return png

    @staticmethod
    def _title(page) -> str:
        try:
            return page.title()
        except Exception:
            return ""

    def _note(self, kind: str, detail: str) -> None:
        if self.on_human_action:
            self.on_human_action(kind, detail)

    def viewport(self) -> dict[str, int]:
        try:
            vp = self.surface.page.viewport_size or {}
        except Exception:
            vp = {}
        return {"width": vp.get("width", 1280), "height": vp.get("height", 900)}
