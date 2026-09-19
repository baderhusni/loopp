"""Run evidence.

Two audiences, one artefact tree. An engineer needs enough to debug a failure
without reproducing it; an auditor needs to see what the automation did on a
member's record and what a human did after taking over. So every event is
structured, ordered, and scrubbed on the way to disk -- there is no `print` path
that bypasses the redactor.

Layout:

    evidence/<kind>-<run_id>/
      run.jsonl     every event, in order
      run.json      the final typed result
      screens/      per-step screenshots (subject to capture policy)
      dom/          raw frame dumps, written only on failure
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..policy.redact import Redactor


@dataclass
class EvidenceRecorder:
    root: Path
    run_id: str
    kind: str                       # discovery | replay
    redactor: Redactor
    screenshot_policy: str = "on_failure"   # always | on_failure | never

    _seq: int = field(default=0, init=False)
    _fh: Any = field(default=None, init=False)
    _started: float = field(default_factory=time.monotonic, init=False)

    def __post_init__(self) -> None:
        self.dir = Path(self.root) / f"{self.kind}-{self.run_id}"
        (self.dir / "screens").mkdir(parents=True, exist_ok=True)
        (self.dir / "dom").mkdir(parents=True, exist_ok=True)
        self._fh = (self.dir / "run.jsonl").open("a", encoding="utf-8")

    # -- events ----------------------------------------------------------
    def event(self, type_: str, **fields: Any) -> None:
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_ms": int((time.monotonic() - self._started) * 1000),
            "type": type_,
            **self.redactor.structure(fields),
        }
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()

    def note(self, message: str, **fields: Any) -> None:
        self.event("note", message=message, **fields)

    # -- artefacts -------------------------------------------------------
    def screenshot_path(self, label: str) -> str:
        self._seq += 1
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:48]
        return str(self.dir / "screens" / f"{self._seq:03d}-{safe}.png")

    def should_capture(self, *, failure: bool) -> bool:
        if self.screenshot_policy == "never":
            return False
        if self.screenshot_policy == "always":
            return True
        return failure

    def dom_path(self, label: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:48]
        return str(self.dir / "dom" / f"{self._seq:03d}-{safe}.html")

    def rel(self, path: str | None) -> str:
        """Paths in the result are relative to the run dir, so it stays portable."""
        if not path:
            return ""
        try:
            return str(Path(path).relative_to(self.dir))
        except ValueError:
            return str(path)

    def write_result(self, payload: Any) -> str:
        """Persist the final typed result, scrubbed."""
        data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
        scrubbed = self.redactor.structure(data)
        out = self.dir / "run.json"
        out.write_text(json.dumps(scrubbed, indent=2, default=str), encoding="utf-8")
        return str(out)

    def write_text(self, name: str, body: str) -> str:
        out = self.dir / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.redactor.text(body), encoding="utf-8")
        return str(out)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "EvidenceRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
