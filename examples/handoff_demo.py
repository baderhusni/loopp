"""End-to-end human-in-the-loop demo.

A replay is deliberately put in a state it cannot clear on its own: the
capability's "dismiss the system notice" recovery rule is removed, and the
fixture app is told to throw a notice modal in the way. The run gets stuck,
raises an intervention, and offers the live session to a human.

A scripted operator then does what a person would do at the console, through
the console's own HTTP API and no back door: poll for the intervention, take
control, look at what is on screen, click the button, hand the session back.
The replay re-verifies where it is and carries on to a successful finish.

The only thing being stood in for here is the pair of hands. The intervention,
the control token, the input forwarding, the recording of what the human did,
and the resume-and-re-verify are the real implementation.

    python examples/handoff_demo.py --base-url http://127.0.0.1:8799
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from scribe.catalog import load_capability                       # noqa: E402
from scribe.config import resolve_chromium                       # noqa: E402
from scribe.escalation import ControlToken, EscalationBroker, LogSink   # noqa: E402
from scribe.escalation.console import ConsoleSink                # noqa: E402
from scribe.escalation.session_host import SessionHost           # noqa: E402
from scribe.evidence import EvidenceRecorder                     # noqa: E402
from scribe.policy import PolicyEngine                           # noqa: E402
from scribe.replay.engine import ReplayEngine                    # noqa: E402
from scribe.surface.web import WebSurface                        # noqa: E402

OPERATOR = "j.rivera"


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read())


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read())


def arm_fault(base_url: str, kind: str, **kw) -> None:
    _post(f"{base_url}/__fault", {"kind": kind, **kw})


def operator_session(console_url: str, log: list[str]) -> None:
    """Everything below happens over the console's public HTTP API."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            state = _get(f"{console_url}api/state")
        except urllib.error.URLError:
            time.sleep(0.3)
            continue
        if state["control"]["owner"] != "handoff_pending":
            time.sleep(0.3)
            continue

        log.append(f"operator sees intervention: {state['intervention']['reason'][:90]}")
        _post(f"{console_url}api/claim", {"operator": OPERATOR})
        log.append("operator took control of the live session")

        # Look at the same screen the automation was looking at.
        controls = _get(f"{console_url}api/controls")
        target = next((c for c in controls["controls"]
                       if c["name"].strip().lower() == "acknowledge"), None)
        if target is None:
            log.append(f"nothing to acknowledge; saw {[c['name'] for c in controls['controls']]}")
            _post(f"{console_url}api/resolve",
                  {"operator": OPERATOR, "resolution": "abort", "note": "nothing to do"})
            return

        x, y, w, h = target["bbox"]
        _post(f"{console_url}api/input",
              {"operator": OPERATOR, "kind": "click", "x": x + w / 2, "y": y + h / 2})
        log.append(f"operator clicked {target['name']!r} on the live page")

        _post(f"{console_url}api/resolve",
              {"operator": OPERATOR, "resolution": "resume",
               "note": "cleared the system notice; automation can continue"})
        log.append("operator handed the session back")
        return
    log.append("operator timed out waiting for an intervention")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:8799")
    ap.add_argument("--capability",
                    default=str(REPO / "capabilities" / "meridian_core" /
                                "member_savings_balance.json"))
    ap.add_argument("--member-id", default="100412")
    ap.add_argument("--console-port", type=int, default=8901)
    ap.add_argument("--evidence", default=str(REPO / "evidence"))
    args = ap.parse_args()

    cap = load_capability(args.capability)
    # Take away the capability's own answer to a notice modal, so the run has
    # to ask a person. Everything else about it is untouched.
    cap.recovery = [r for r in cap.recovery if r.id != "dismiss_system_notice"]

    policy = PolicyEngine.load(REPO / "policies" / "policy.yaml")
    policy.screenshot_policy = "always"
    if args.base_url not in policy.app("meridian_core").allowed_origins:
        policy.app("meridian_core").allowed_origins.append(args.base_url)

    run_id = uuid.uuid4().hex[:10]
    recorder = EvidenceRecorder(Path(args.evidence), run_id, "handoff", policy.redactor,
                                screenshot_policy="always")
    surface = WebSurface(executable_path=resolve_chromium(), headless=True)

    token = ControlToken()
    token.begin_run(run_id)
    broker = EscalationBroker(token, sinks=[LogSink()], claim_timeout=90,
                              resolve_timeout=180,
                              on_event=lambda t, **kw: recorder.event(t, **kw))
    host = SessionHost(surface, token)
    host.on_human_action = lambda kind, detail: broker.record_human_action(
        broker.current.claimed_by if broker.current else OPERATOR, kind, detail)
    broker.attach_pump(host.pump)

    console = ConsoleSink(broker, host, port=args.console_port)
    console_url = console.start()
    broker.sinks.append(console)
    print(f"operator console: {console_url}")

    log: list[str] = []
    threading.Thread(target=operator_session, args=(console_url, log), daemon=True).start()

    try:
        # One charge is exactly right: the notice lands on the inquiry screen
        # as the console loads it, and is gone once the operator acknowledges
        # it. Arming more would put a fresh notice in front of the operator's
        # own handback, which tests the fixture rather than the handoff.
        arm_fault(args.base_url, "interstitial", count=1)
        engine = ReplayEngine(surface=surface, policy=policy, recorder=recorder,
                              app_id=cap.app.app_id, broker=broker, run_id=run_id)
        result = engine.run(cap, {"member_id": args.member_id},
                            base_url=args.base_url, tenant_id="northstar-cu")

        print("\n--- operator activity -------------------------------------")
        for line in log:
            print(f"  {line}")
        print("\n--- replay result -----------------------------------------")
        print(" ", result.headline())
        if result.escalation:
            e = result.escalation
            print(f"  intervention : {e.intervention_id}")
            print(f"  raised at    : step {e.raised_at_step}")
            print(f"  resolution   : {e.resolution}")
            print(f"  human actions: {e.human_actions}")
        if result.outputs:
            print(f"  outputs      : {result.outputs}")
        print(f"  control now  : {token.state()[0].value}")
        print(f"  evidence     : {result.evidence_dir}")
        return 0 if result.ok() else 1
    finally:
        arm_fault(args.base_url, "clear")
        console.stop()
        surface.close()
        recorder.close()


if __name__ == "__main__":
    raise SystemExit(main())
