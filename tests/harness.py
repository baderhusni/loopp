"""Shared test harness: a live fixture app plus a wired-up replay engine."""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import uuid
from pathlib import Path

from scribe.escalation import ControlToken, EscalationBroker
from scribe.evidence import EvidenceRecorder
from scribe.policy import PolicyEngine
from scribe.replay.engine import ReplayEngine
from scribe.surface.web import WebSurface

REPO = Path(__file__).resolve().parents[1]
from scribe.config import resolve_chromium

CHROMIUM = resolve_chromium()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def fixture_app(tenant: str = "northstar-cu", session_ttl: float = 3600.0):
    """Run MERIDIAN CORE on a free port for the duration of the block."""
    import sys
    sys.path.insert(0, str(REPO))
    from target_app.server import build_server

    port = free_port()
    httpd = build_server(port, tenant, session_ttl)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


@contextlib.contextmanager
def engine(tmp_path: Path, *, broker: EscalationBroker | None = None,
           allow_draft: bool = False, overlay=None, screenshots: str = "on_failure",
           claim_timeout: float = 2.0):
    """A replay engine against a real browser, with evidence under tmp_path."""
    os.environ.setdefault("MERIDIAN_OPERATOR_ID", "svc_automation")
    os.environ.setdefault("MERIDIAN_PASSCODE", "Tr0ubadour!")
    policy = PolicyEngine.load(REPO / "policies" / "policy.yaml")
    policy.screenshot_policy = screenshots
    run_id = uuid.uuid4().hex[:8]
    recorder = EvidenceRecorder(tmp_path, run_id, "replay", policy.redactor,
                                screenshot_policy=screenshots)
    surface = WebSurface(executable_path=CHROMIUM, headless=True)
    if broker is None:
        # No operator is attached in tests, so an escalatable failure must not
        # sit on the production-default claim timeout. Escalation tests pass
        # their own broker with a sink.
        broker = EscalationBroker(ControlToken(), sinks=[], claim_timeout=claim_timeout)
    broker.token.begin_run(run_id)
    try:
        yield ReplayEngine(surface=surface, policy=policy, recorder=recorder,
                           app_id="meridian_core", broker=broker, overlay=overlay,
                           allow_draft=allow_draft, run_id=run_id)
    finally:
        surface.close()
        recorder.close()


def allow_port(policy: PolicyEngine, base_url: str) -> None:
    """Fixture apps bind a random port; widen the allowlist for that one origin."""
    policy.app("meridian_core").allowed_origins.append(base_url)
