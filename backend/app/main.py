"""FastAPI application — the HTTP boundary.

This layer is deliberately thin: it validates input, calls the agent
orchestration layer (`agent.run_turn`), and serves trace/admin data. All LLM
orchestration and policy logic lives in agent.py / tools.py / policy.py, keeping
a clean separation between UI, API, and the orchestration layer.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import agent, store, trace
from .models import ChatRequest, ChatResponse, HealthResponse

app = FastAPI(
    title="Acme Refund Support Agent",
    description="AI customer-support agent that processes or denies e-commerce refunds.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local demo only
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        sdk_available=agent.sdk_available(),
        default_engine=agent.resolve_engine(None),
        reference_date=store.reference_date(),
        model=os.getenv("AGENT_MODEL", "sonnet"),
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    result = await agent.run_turn(req.conversation_id, req.message, req.engine)
    return ChatResponse(**result)


@app.get("/api/runs")
def list_runs(limit: int = 100) -> dict:
    return {"runs": trace.list_runs(limit=limit)}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = trace.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return run


@app.get("/api/policy", response_class=PlainTextResponse)
def get_policy() -> str:
    return store.policy_text()


@app.get("/api/customers")
def customers() -> dict:
    """Synthetic CRM, exposed for the admin 'data explorer' panel and demoing."""
    return {"reference_date": store.reference_date(), "customers": store.all_customers()}


@app.get("/api/audit")
def audit() -> dict:
    return {"refunds": store.refunds(), "escalations": store.escalations()}


# --- Optionally serve the built frontend so one process serves the whole app ---
_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="frontend")
