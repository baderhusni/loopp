"""Command line for Scribe.

    scribe discover ...   run the model against a live app and record a capability
    scribe replay ...     run a recorded capability, deterministically
    scribe show ...       print the reviewable summary of an artifact
    scribe approve ...    promote a draft so it can run unattended
    scribe catalog ...    list capabilities, or emit them as agent tool schemas
    scribe invoke ...     call a capability by id, the way an agent would
    scribe console ...    operator console for taking over a live session
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .apps import profile_for
from .catalog import Catalog, load_capability, load_overlay, save_capability
from .config import SETTINGS, resolve_chromium
from .escalation import ControlToken, EscalationBroker, LogSink
from .evidence import EvidenceRecorder
from .policy import PolicyEngine
from .types.artifact import ApprovalState
from .types.results import CAPABILITY_FAULTS, ReplayStatus


def _kv(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit(f"bad --param {item!r}; expected name=value")
        key, _, value = item.partition("=")
        out[key.strip()] = value
    return out


def _policy(args) -> PolicyEngine:
    policy = PolicyEngine.load(args.policy or SETTINGS.policy_file)
    if getattr(args, "screenshots", None):
        policy.screenshot_policy = args.screenshots
    # Fixture apps bind whatever port is free; allow the caller's base URL when
    # it is an explicit loopback address. Never widens beyond localhost.
    base = getattr(args, "base_url", None)
    if base:
        from urllib.parse import urlparse
        parsed = urlparse(base)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        app = policy.app(args.app)
        if origin not in app.allowed_origins and parsed.hostname in ("127.0.0.1", "localhost"):
            app.allowed_origins.append(origin)
    return policy


def _surface(args):
    from .surface.web import WebSurface
    return WebSurface(headless=not getattr(args, "headed", False),
                      executable_path=resolve_chromium())


def _broker(args, run_id: str, surface=None) -> tuple[EscalationBroker, Any]:
    """Broker, plus the console sink to shut down afterwards (or None)."""
    token = ControlToken()
    token.begin_run(run_id)
    sinks = [LogSink()]
    if getattr(args, "escalation_dir", None):
        from .escalation import FileSink
        sinks.append(FileSink(args.escalation_dir))
    broker = EscalationBroker(
        token, sinks=sinks,
        claim_timeout=getattr(args, "claim_timeout", 300.0),
        resolve_timeout=getattr(args, "resolve_timeout", 1800.0))

    console = None
    if getattr(args, "console", False) and surface is not None:
        from .escalation.console import ConsoleSink
        from .escalation.session_host import SessionHost

        host = SessionHost(surface, token)
        host.on_human_action = lambda kind, detail: broker.record_human_action(
            broker.current.claimed_by if broker.current else "operator", kind, detail,
            url=_safe_url(surface))
        broker.attach_pump(host.pump)
        console = ConsoleSink(broker, host, host_addr=args.console_host,
                              port=args.console_port)
        url = console.start()
        sinks.append(console)
        print(f"operator console: {url}", file=sys.stderr)
    return broker, console


def _safe_url(surface) -> str:
    try:
        return surface.page.url
    except Exception:
        return ""


# --------------------------------------------------------------------------
def cmd_discover(args) -> int:
    from .agent import ArtifactRecorder, DiscoveryAgent, build_client
    from .agent.prompts import OUTCOME_SCHEMA, OUTCOME_SYSTEM, outcome_prompt

    profile = profile_for(args.app)
    policy = _policy(args)
    params = _kv(args.param)
    run_id = uuid.uuid4().hex[:10]
    recorder = EvidenceRecorder(Path(args.evidence or SETTINGS.evidence_dir), run_id,
                                "discovery", policy.redactor,
                                screenshot_policy="always")
    llm = build_client(args.backend, args.model)
    surface = _surface(args)
    broker, console = _broker(args, run_id, surface)

    print(f"discovery run {run_id}  model={llm.model} via {llm.name}", file=sys.stderr)
    try:
        agent = DiscoveryAgent(
            surface=surface, policy=policy, recorder=recorder, llm=llm, profile=profile,
            goal=args.goal, params=params, base_url=args.base_url, broker=broker,
            max_steps=args.max_steps, run_id=run_id)
        result = agent.run()
        print(result.headline(), file=sys.stderr)
        for entry in result.trace:
            flag = "!" if entry.error else " "
            print(f" {flag} {entry.index:>2}. {entry.tool:<9} {entry.thought[:96]}",
                  file=sys.stderr)
            if entry.error:
                print(f"      -> {entry.error[:140]}", file=sys.stderr)

        if not result.success:
            recorder.write_result({"run_id": run_id, "goal": args.goal,
                                   "success": False, "stop_reason": result.stop_reason})
            print(f"\nno capability recorded: {result.stop_reason}", file=sys.stderr)
            return 2

        builder = ArtifactRecorder(profile, base_url=args.base_url,
                                   tenant_id=args.tenant)
        cap = builder.build(result, capability_id=args.id,
                            name=args.name or args.goal[:60],
                            params=params, model=f"{llm.model} ({llm.name})",
                            version=args.version)

        if args.probe:
            _probe_outcomes(args, cap, policy, surface, recorder, llm, params,
                            run_id, OUTCOME_SYSTEM, OUTCOME_SCHEMA, outcome_prompt)

        out = Path(args.out or (Path(SETTINGS.capabilities_dir) / args.app /
                                f"{args.id.split('.')[-1]}.json"))
        save_capability(cap, out)
        recorder.write_text("capability.json",
                            json.dumps(cap.model_dump(mode="json"), indent=2))
        recorder.write_text("capability-review.txt", cap.review_summary())
        recorder.write_result({
            "run_id": run_id, "goal": args.goal, "success": True,
            "stop_reason": result.stop_reason, "capability": cap.id,
            "version": cap.version, "moves": len(result.trace),
            "llm_calls": result.llm_calls, "cost_usd": round(result.cost_usd, 4),
            "artifact_path": str(out)})
        print(f"\n{cap.review_summary()}\n", file=sys.stderr)
        print(f"wrote {out}  (draft -- review, then `scribe approve`)", file=sys.stderr)
        print(f"evidence: {recorder.dir}", file=sys.stderr)
        return 0
    finally:
        if console is not None:
            console.stop()
        surface.close()
        recorder.close()


def _probe_outcomes(args, cap, policy, surface, recorder, llm, params, run_id,
                    system, schema, prompt_for) -> None:
    """Replay the fresh recording against a deliberately unhappy input.

    The happy path alone cannot tell you what "no such member" looks like. So we
    re-run what was just recorded -- deterministically, no model in the loop --
    with an input we expect to fail, show the model the screen that came back,
    and let it name the outcome. The detector is grounded in a real observation
    rather than in a guess about the app's wording.
    """
    from .replay.engine import ReplayEngine
    from .types.artifact import BusinessOutcome
    from .types.conditions import TextPresent
    from .types.core import ExtractSource, ExtractSpec

    probe = _kv(args.probe)
    print(f"\nprobing for business outcomes with {probe}", file=sys.stderr)
    engine = ReplayEngine(surface=surface, policy=policy, recorder=recorder,
                          app_id=args.app, broker=None, allow_draft=True,
                          run_id=f"{run_id}-probe")
    outcome = engine.run(cap, {**params, **probe}, base_url=args.base_url,
                         tenant_id=args.tenant)
    screen = surface.observe()
    verdict = llm.ask_json(system, prompt_for(cap.description, probe, screen.text), schema)
    recorder.event("probe_result", probe=probe, replay_status=outcome.status.value,
                   verdict=verdict)
    if not verdict.get("is_business_outcome"):
        print(f"  probe was not a business outcome: {verdict.get('reasoning', '')[:160]}",
              file=sys.stderr)
        return
    detect = verdict["detect_text"]
    if detect.lower() not in screen.text.lower():
        print(f"  discarding probe outcome: {detect!r} is not on the screen",
              file=sys.stderr)
        return
    cap.outcomes.append(BusinessOutcome(
        code=verdict["code"], title=verdict["title"],
        when=TextPresent(text=detect),
        message_from=ExtractSpec(output="_message", source=ExtractSource.PAGE_REGEX,
                                 pattern=f"({detect}[^\\n]*)", group=1)))
    print(f"  recorded business outcome {verdict['code']}: {detect!r}", file=sys.stderr)


# --------------------------------------------------------------------------
def cmd_replay(args) -> int:
    from .replay.engine import ReplayEngine

    cap = load_capability(args.capability)
    policy = _policy(args)
    overlay = None
    if args.overlay:
        overlay = load_overlay(args.overlay)
    elif args.tenant:
        overlay = Catalog(SETTINGS.capabilities_dir).overlay_for(cap.id, args.tenant)
        if overlay is None and args.tenant != cap.app.recorded_on_tenant:
            print(f"note: no overlay for tenant {args.tenant}; using the base recording",
                  file=sys.stderr)
    base_url = args.base_url or (overlay.base_url if overlay else None)
    if not base_url:
        raise SystemExit("--base-url is required (or provide an overlay that sets one)")
    args.base_url = base_url
    policy = _policy(args)

    run_id = uuid.uuid4().hex[:10]
    recorder = EvidenceRecorder(Path(args.evidence or SETTINGS.evidence_dir), run_id,
                                "replay", policy.redactor,
                                screenshot_policy=policy.screenshot_policy)
    surface = _surface(args)
    broker, console = ((None, None) if args.no_escalation
                       else _broker(args, run_id, surface))
    try:
        engine = ReplayEngine(surface=surface, policy=policy, recorder=recorder,
                              app_id=cap.app.app_id, broker=broker, overlay=overlay,
                              allow_draft=args.allow_draft, run_id=run_id)
        result = engine.run(cap, _kv(args.param), base_url=base_url,
                            tenant_id=args.tenant or cap.app.recorded_on_tenant)
        print(json.dumps(result.model_dump(mode="json"), indent=2, default=str)
              if args.json else _render_result(result))
        if args.record_stats:
            _record_stats(args.capability, cap, result)
        return {ReplayStatus.SUCCESS: 0, ReplayStatus.BUSINESS_OUTCOME: 0,
                ReplayStatus.ESCALATED: 3, ReplayStatus.REJECTED: 4}.get(result.status, 1)
    finally:
        if console is not None:
            console.stop()
        surface.close()
        recorder.close()


def _render_result(result) -> str:
    lines = [result.headline()]
    if result.outputs:
        lines.append("outputs:")
        lines += [f"  {k} = {v!r}" for k, v in result.outputs.items()]
    if result.outcome:
        lines.append(f"outcome: {result.outcome.code} -- {result.outcome.title}")
        if result.outcome.message:
            lines.append(f"  app said: {result.outcome.message}")
    if result.failure:
        f = result.failure
        lines.append(f"failure: {f.failure_class.value}")
        if f.step_id:
            lines.append(f"  at step   : #{f.step_index} {f.step_id}")
        if f.expected:
            lines.append(f"  expected  : {f.expected}")
        if f.observed:
            lines.append(f"  observed  : {f.observed}")
        if f.remediation:
            lines.append(f"  next step : {f.remediation}")
        if f.evidence:
            lines.append(f"  evidence  : {', '.join(f.evidence)}")
    if result.escalation:
        e = result.escalation
        lines.append(f"escalation: {e.intervention_id} -> {e.resolution} "
                     f"({e.human_actions} human action(s))")
    if result.recoveries:
        lines.append("recovered from:")
        lines += [f"  {r.rule_id} at {r.at_step}" for r in result.recoveries]
    if result.drift:
        lines.append("drift signals:")
        lines += [f"  [{d.kind}] {d.step_id}: {d.detail}" for d in result.drift]
    lines.append(f"steps: " + ", ".join(
        f"{s.step_id}={s.status.value}" for s in result.steps))
    lines.append(f"evidence: {result.evidence_dir}")
    return "\n".join(lines)


def _record_stats(path: str, cap, result) -> None:
    cap.stats.replays += 1
    if result.status == ReplayStatus.SUCCESS:
        cap.stats.successes += 1
    elif result.status == ReplayStatus.BUSINESS_OUTCOME:
        cap.stats.business_outcomes += 1
    elif result.status == ReplayStatus.FAILED and result.failure is not None:
        if result.failure.failure_class in CAPABILITY_FAULTS:
            cap.stats.failures += 1
        else:
            cap.stats.environment_failures += 1
    cap.stats.last_replay_at = datetime.now(timezone.utc)
    save_capability(cap, path)


# --------------------------------------------------------------------------
def cmd_show(args) -> int:
    cap = load_capability(args.capability)
    if args.json:
        print(json.dumps(cap.model_dump(mode="json"), indent=2))
        return 0
    print(cap.review_summary())
    print("\ncall contract (what an agent sees):")
    print(json.dumps({"name": cap.id, "input_schema": cap.input_schema(),
                      "output_schema": cap.output_schema(),
                      "outcomes": cap.outcome_codes()}, indent=2))
    if cap.stats.replays:
        rate = cap.stats.success_rate
        print(f"\nreplay stats: {cap.stats.replays} runs, "
              f"{cap.stats.successes} success, {cap.stats.business_outcomes} outcome, "
              f"{cap.stats.failures} capability failure(s), "
              f"{cap.stats.environment_failures} environment failure(s)"
              + (f"\n              answered correctly {rate:.0%} of the runs this "
                 f"capability could have got right" if rate is not None else ""))
    return 0


def cmd_approve(args) -> int:
    cap = load_capability(args.capability)
    cap.approval.state = (ApprovalState.DEPRECATED if args.deprecate
                          else ApprovalState.APPROVED)
    cap.approval.approved_by = args.by
    cap.approval.approved_at = datetime.now(timezone.utc)
    cap.approval.note = args.note or ""
    save_capability(cap, args.capability)
    print(f"{cap.id}@{cap.version} -> {cap.approval.state.value} (by {args.by})")
    return 0


def cmd_catalog(args) -> int:
    catalog = Catalog(args.dir or SETTINGS.capabilities_dir)
    if args.as_tools:
        print(json.dumps(catalog.as_anthropic_tools(approved_only=not args.include_drafts),
                         indent=2))
        return 0
    print(catalog.describe())
    return 0


def cmd_invoke(args) -> int:
    """Call a capability by id -- the path an agent would take."""
    catalog = Catalog(args.dir or SETTINGS.capabilities_dir)
    entry = catalog.get(args.capability_id, args.version)
    args.capability = str(entry.path)
    args.overlay = None
    return cmd_replay(args)


def cmd_console(args) -> int:
    from .escalation.console import serve_standalone
    return serve_standalone(host=args.host, port=args.port)


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="scribe", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p, *, app_default="meridian_core"):
        p.add_argument("--app", default=app_default)
        p.add_argument("--policy", default=None, help="path to the policy file")
        p.add_argument("--evidence", default=None, help="evidence root directory")
        p.add_argument("--headed", action="store_true", help="show the browser")
        p.add_argument("--escalation-dir", default=None,
                       help="also write intervention requests here as JSON")
        p.add_argument("--claim-timeout", type=float, default=300.0)
        p.add_argument("--resolve-timeout", type=float, default=1800.0)
        p.add_argument("--console", action="store_true",
                       help="serve the operator console over this live session")
        p.add_argument("--console-host", default=SETTINGS.console_host)
        p.add_argument("--console-port", type=int, default=SETTINGS.console_port)

    d = sub.add_parser("discover", help="record a capability with a model in the loop")
    common(d)
    d.add_argument("--goal", required=True)
    d.add_argument("--base-url", required=True)
    d.add_argument("--id", required=True, help="capability id, e.g. app.flow_name")
    d.add_argument("--name", default=None)
    d.add_argument("--version", default="1.0.0")
    d.add_argument("--param", action="append", help="name=value (repeatable)")
    d.add_argument("--probe", action="append",
                   help="name=value to probe for a business outcome after recording")
    d.add_argument("--tenant", default=None)
    d.add_argument("--out", default=None)
    d.add_argument("--backend", default="auto", choices=["auto", "api", "cli"])
    d.add_argument("--model", default=SETTINGS.model)
    d.add_argument("--max-steps", type=int, default=SETTINGS.max_steps)
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("replay", help="run a capability deterministically")
    common(r)
    r.add_argument("capability")
    r.add_argument("--base-url", default=None)
    r.add_argument("--param", action="append")
    r.add_argument("--tenant", default=None)
    r.add_argument("--overlay", default=None)
    r.add_argument("--allow-draft", action="store_true",
                   help="run a capability a human has not approved")
    r.add_argument("--no-escalation", action="store_true",
                   help="fail instead of asking a human")
    r.add_argument("--screenshots", choices=["always", "on_failure", "never"],
                   default=None)
    r.add_argument("--record-stats", action="store_true")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_replay)

    s = sub.add_parser("show", help="print an artifact for review")
    s.add_argument("capability")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    a = sub.add_parser("approve", help="promote a draft capability")
    a.add_argument("capability")
    a.add_argument("--by", required=True)
    a.add_argument("--note", default=None)
    a.add_argument("--deprecate", action="store_true")
    a.set_defaults(func=cmd_approve)

    c = sub.add_parser("catalog", help="list capabilities / emit agent tool schemas")
    c.add_argument("--dir", default=None)
    c.add_argument("--as-tools", action="store_true")
    c.add_argument("--include-drafts", action="store_true")
    c.set_defaults(func=cmd_catalog)

    i = sub.add_parser("invoke", help="call a capability by id, as an agent would")
    common(i)
    i.add_argument("capability_id")
    i.add_argument("--version", default=None)
    i.add_argument("--dir", default=None)
    i.add_argument("--base-url", default=None)
    i.add_argument("--param", action="append")
    i.add_argument("--tenant", default=None)
    i.add_argument("--allow-draft", action="store_true")
    i.add_argument("--no-escalation", action="store_true")
    i.add_argument("--screenshots", choices=["always", "on_failure", "never"],
                   default=None)
    i.add_argument("--record-stats", action="store_true")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_invoke)

    o = sub.add_parser("console", help="run the operator console standalone")
    o.add_argument("--host", default=SETTINGS.console_host)
    o.add_argument("--port", type=int, default=SETTINGS.console_port)
    o.set_defaults(func=cmd_console)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
