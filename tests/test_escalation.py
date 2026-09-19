"""Control transfer.

The invariant being defended: exactly one party may act on a live session at a
time, and both parties agree on which. These are the cases where a naive
implementation lets two writers in.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from scribe.escalation import (
    AutoResolver,
    ControlOwner,
    ControlToken,
    ControlViolation,
    EscalationBroker,
    Resolution,
)
from scribe.escalation.console import build_app
from scribe.escalation.session_host import SessionHost
from scribe.types.results import FailureClass


@pytest.fixture
def token() -> ControlToken:
    t = ControlToken(lease_seconds=60)
    t.begin_run("run-1")
    return t


@pytest.fixture
def broker(token) -> EscalationBroker:
    return EscalationBroker(token, sinks=[], claim_timeout=2, resolve_timeout=5)


def raise_one(broker: EscalationBroker):
    return broker.raise_intervention(
        run_id="run-1", kind="replay", reason="stuck on a modal",
        subject="cap@1.0.0", failure_class=FailureClass.UNEXPECTED_STATE,
        step_id="open_member", step_index=3)


# -- the single-writer invariant -------------------------------------------
def test_automation_cannot_act_once_it_has_offered_the_session(token, broker):
    raise_one(broker)
    with pytest.raises(ControlViolation):
        token.assert_automation("run-1")


def test_operator_cannot_act_before_claiming(token, broker):
    raise_one(broker)
    with pytest.raises(ControlViolation):
        token.assert_human("j.rivera")


def test_a_second_operator_cannot_seize_a_claimed_session(token, broker):
    raise_one(broker)
    broker.claim("j.rivera")
    with pytest.raises(ControlViolation):
        token.claim("someone.else")
    with pytest.raises(ControlViolation):
        token.assert_human("someone.else")


def test_automation_cannot_act_while_a_human_holds_control(token, broker):
    raise_one(broker)
    broker.claim("j.rivera")
    with pytest.raises(ControlViolation):
        token.assert_automation("run-1")


def test_an_abandoned_lease_returns_the_session_rather_than_wedging_it():
    """An operator who closes the tab must not lock the session forever."""
    t = ControlToken(lease_seconds=0.2)
    t.begin_run("run-1")
    t.offer()
    t.claim("j.rivera")
    time.sleep(0.3)
    assert t.state()[0] == ControlOwner.HANDOFF_PENDING
    with pytest.raises(ControlViolation):
        t.assert_human("j.rivera")


def test_full_cycle_returns_control_to_automation(token, broker):
    raise_one(broker)
    broker.claim("j.rivera")
    broker.record_human_action("j.rivera", "click", "clicked Acknowledge")
    broker.resolve("j.rivera", Resolution.RESUME)
    assert token.state()[0] == ControlOwner.RESUMING
    broker.resume_automation("run-1")
    token.assert_automation("run-1")
    assert len(broker.current.human_actions) == 1


# -- waiting ---------------------------------------------------------------
def test_an_unclaimed_intervention_times_out_instead_of_hanging(broker):
    raise_one(broker)
    started = time.monotonic()
    assert broker.wait_for_resolution() == Resolution.TIMEOUT
    assert time.monotonic() - started < 5


def test_resolution_unblocks_the_waiting_run(token):
    broker = EscalationBroker(token, sinks=[], claim_timeout=5, resolve_timeout=5)
    broker.sinks.append(AutoResolver(broker, Resolution.APPROVED, delay=0.1))
    raise_one(broker)
    assert broker.wait_for_resolution() == Resolution.APPROVED


def test_the_briefing_carries_what_a_person_needs():
    t = ControlToken(); t.begin_run("run-1")
    broker = EscalationBroker(t, sinks=[])
    text = raise_one(broker).briefing()
    for expected in ("cap@1.0.0", "stuck on a modal", "open_member"):
        assert expected in text


def test_a_broken_sink_does_not_take_down_the_run(token, broker):
    class Exploding:
        def publish(self, request):
            raise RuntimeError("pager is down")

        def describe(self):
            return "exploding"

    broker.sinks.append(Exploding())
    assert raise_one(broker) is not None          # still raised, still offered
    assert token.state()[0] == ControlOwner.HANDOFF_PENDING


# -- the console -----------------------------------------------------------
def test_console_rejects_input_from_someone_who_does_not_hold_control(broker):
    raise_one(broker)
    client = TestClient(build_app(broker, None))
    response = client.post("/api/input",
                           json={"operator": "impostor", "kind": "click", "x": 1, "y": 2})
    assert response.status_code == 409


def test_console_claim_then_resolve_round_trip(broker, token):
    raise_one(broker)
    client = TestClient(build_app(broker, None))
    assert client.post("/api/claim", json={"operator": "j.rivera"}).json()["ok"]
    assert client.get("/api/state").json()["control"]["owner"] == "human"
    assert client.post("/api/resolve",
                       json={"operator": "j.rivera", "resolution": "resume"}).json()["ok"]
    assert token.state()[0] == ControlOwner.RESUMING


def test_console_rejects_an_unknown_resolution(broker):
    raise_one(broker)
    client = TestClient(build_app(broker, None))
    client.post("/api/claim", json={"operator": "j.rivera"})
    assert client.post("/api/resolve",
                       json={"operator": "j.rivera", "resolution": "nonsense"}
                       ).status_code == 400


# -- the session host ------------------------------------------------------
class FakePage:
    def __init__(self):
        self.url = "http://x/console"
        self.clicks = []
        self.thread_ids = set()

    class _Mouse:
        def __init__(self, outer):
            self.outer = outer

        def click(self, x, y):
            self.outer.thread_ids.add(threading.get_ident())
            self.outer.clicks.append((x, y))

    @property
    def mouse(self):
        return FakePage._Mouse(self)

    def screenshot(self, **kw):
        return b"PNG"

    def wait_for_timeout(self, ms):
        pass

    def viewport_size(self):
        return {"width": 1280, "height": 900}


class FakeSurface:
    def __init__(self):
        self.page = FakePage()


def test_commands_from_another_thread_execute_on_the_owning_thread():
    """Playwright is thread-bound. Control transfer must move *authority*,
    not the thread that touches the browser."""
    surface = FakeSurface()
    host = SessionHost(surface, ControlToken())
    owner_thread = threading.get_ident()

    cmd = {}

    def caller():
        cmd["handle"] = host.submit("click", x=10, y=20)

    t = threading.Thread(target=caller)
    t.start()
    t.join()

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not surface.page.clicks:
        host.pump(0.1)

    assert surface.page.clicks == [(10.0, 20.0)]
    assert surface.page.thread_ids == {owner_thread}
    cmd["handle"].wait(timeout=1)


def test_human_actions_are_recorded_for_the_audit_trail():
    surface = FakeSurface()
    host = SessionHost(surface, ControlToken())
    seen = []
    host.on_human_action = lambda kind, detail: seen.append((kind, detail))
    host.submit("click", x=1, y=2)
    host.pump(0.3)
    assert seen and seen[0][0] == "click"
