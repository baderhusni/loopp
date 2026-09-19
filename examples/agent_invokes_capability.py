"""An AI agent discovering a capability and calling it by name.

This is the point of the whole system, so it is worth showing rather than
asserting: the capability catalog emits ordinary tool schemas, a model picks one
and supplies typed arguments, the capability runs deterministically with no
model in its decision loop, and the typed result comes back.

The second question is the interesting one. Asking for a member that does not
exist returns `MEMBER_NOT_FOUND` as a *result*, not an exception -- so the agent
can answer the question instead of reporting a malfunction.

    python examples/agent_invokes_capability.py --base-url http://127.0.0.1:8799
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from scribe.agent.llm import build_client                        # noqa: E402
from scribe.catalog import Catalog                               # noqa: E402
from scribe.config import resolve_chromium                       # noqa: E402
from scribe.evidence import EvidenceRecorder                     # noqa: E402
from scribe.policy import PolicyEngine                           # noqa: E402
from scribe.replay.engine import ReplayEngine                    # noqa: E402
from scribe.surface.web import WebSurface                        # noqa: E402

PICK_SYSTEM = """\
You are a servicing agent for a credit union. You cannot see the back-office
system yourself; you call capabilities that operate it for you.

Given the caller's question and the capabilities available, choose exactly one
capability and the arguments it needs. If none of them fit, say so instead of
forcing one.
"""

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "capability": {"type": "string", "description": "Tool name, or '' if none fit."},
        "arguments": {"type": "object", "additionalProperties": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": ["capability", "arguments", "reasoning"],
    "additionalProperties": False,
}

ANSWER_SYSTEM = """\
You are answering a colleague at a credit union.

You called a capability and got a structured result back. `status` is either
`success` (with `outputs`), or `business_outcome` -- a legitimate answer such as
"no such member", which you should relay plainly rather than describing as an
error -- or `failed`, which means the system itself did not work and you should
say so.

Answer in one or two sentences. Do not invent figures that are not in the result.
"""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

QUESTIONS = [
    "What's the current savings balance for member 100413?",
    "Can you check the savings balance on member 999999 for me?",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:8799")
    ap.add_argument("--backend", default="auto", choices=["auto", "api", "cli"])
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--evidence", default=str(REPO / "evidence"))
    args = ap.parse_args()

    catalog = Catalog(REPO / "capabilities")
    tools = catalog.as_anthropic_tools()
    if not tools:
        print("no approved capabilities in the catalog -- record and approve one first")
        return 2

    print("=== capability catalog, as tool schemas an agent can be handed ===")
    print(json.dumps(tools, indent=2))

    llm = build_client(args.backend, args.model)
    policy = PolicyEngine.load(REPO / "policies" / "policy.yaml")
    if args.base_url not in policy.app("meridian_core").allowed_origins:
        policy.app("meridian_core").allowed_origins.append(args.base_url)

    for question in QUESTIONS:
        print(f"\n=== caller asks: {question}")
        choice = llm.ask_json(
            PICK_SYSTEM,
            f"Capabilities available:\n{json.dumps(tools, indent=2)}\n\n"
            f"Question: {question}",
            PICK_SCHEMA)
        print(f"  agent picks : {choice['capability']} {choice['arguments']}")
        print(f"  because     : {choice['reasoning'][:150]}")
        if not choice["capability"]:
            continue

        entry = catalog.get(choice["capability"].replace("__", "."))
        run_id = uuid.uuid4().hex[:10]
        recorder = EvidenceRecorder(Path(args.evidence), run_id, "invoke",
                                    policy.redactor,
                                    screenshot_policy=policy.screenshot_policy)
        surface = WebSurface(executable_path=resolve_chromium(), headless=True)
        try:
            engine = ReplayEngine(surface=surface, policy=policy, recorder=recorder,
                                  app_id=entry.capability.app.app_id, broker=None,
                                  run_id=run_id)
            result = engine.run(entry.capability, choice["arguments"],
                                base_url=args.base_url,
                                tenant_id=entry.capability.app.recorded_on_tenant)
        finally:
            surface.close()
            recorder.close()

        # Exactly what the calling agent receives -- typed, and free of the
        # screens, clicks, and frames it never needs to know about.
        payload = {
            "status": result.status.value,
            "outputs": result.outputs,
            "outcome": ({"code": result.outcome.code, "message": result.outcome.message}
                        if result.outcome else None),
            "error": (result.failure.failure_class.value if result.failure else None),
        }
        print(f"  capability returns: {json.dumps(payload)}")
        print(f"  (no model in that decision loop: used_llm={result.used_llm})")

        answer = llm.ask_json(
            ANSWER_SYSTEM,
            f"Question: {question}\n\nCapability result:\n{json.dumps(payload)}",
            ANSWER_SCHEMA)
        print(f"  agent answers: {answer['answer']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
