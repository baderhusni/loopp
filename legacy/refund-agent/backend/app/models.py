"""Pydantic models for the HTTP API."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000, description="The customer's message")
    conversation_id: Optional[str] = Field(None, description="Continue an existing conversation")
    engine: Optional[str] = Field(None, description="Override engine: 'claude', 'mock', or 'auto'")


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    conversation_id: str
    run_id: str
    reply: str
    decision: Optional[str] = None
    engine: str
    model: Optional[str] = None
    usage: Optional[Usage] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[float] = None
    wall_ms: Optional[float] = None
    num_tool_calls: int = 0
    fallback_reason: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    sdk_available: bool
    default_engine: str
    reference_date: str
    model: str
