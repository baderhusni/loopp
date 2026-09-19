"""Model access, with two interchangeable backends.

`api` is the documented default: the Anthropic SDK with an API key, using
native tool calling and prompt caching. `cli` drives the local `claude` binary
in headless mode, which authenticates from an existing Claude session and so
works where no API key is provisioned -- including the environment this
project's evidence was produced in.

Both return the same validated `AgentMove`, so the discovery loop is written
once and cannot drift between them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol

from .schema import TOOLS, AgentMove, parse_move, tools_as_text

DEFAULT_MODEL = "claude-opus-5"


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, **kw: Any) -> None:
        self.calls += 1
        for k, v in kw.items():
            setattr(self, k, getattr(self, k) + (v or 0))


class LLMClient(Protocol):
    name: str
    model: str
    usage: Usage

    def decide(self, system: str, messages: list[dict[str, Any]]) -> tuple[AgentMove, str]:
        """Return the next move and the raw text the model produced."""

    def ask_json(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """One-shot structured question, used for outcome labelling."""


# --------------------------------------------------------------------------
class AnthropicClient:
    """Anthropic SDK. Native tool use, forced to exactly one call per turn."""

    name = "api"

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = 4000) -> None:
        try:
            import anthropic
        except ImportError as exc:                       # pragma: no cover
            raise LLMError("the `anthropic` package is not installed") from exc
        self._anthropic = anthropic
        try:
            self._client = anthropic.Anthropic()
        except Exception as exc:
            raise LLMError(
                "could not construct an Anthropic client -- set ANTHROPIC_API_KEY, "
                "or use the `cli` backend (SCRIBE_LLM_BACKEND=cli)") from exc
        self.model, self.max_tokens = model, max_tokens
        self.usage = Usage()

    def decide(self, system: str, messages: list[dict[str, Any]]) -> tuple[AgentMove, str]:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            tools=[{**t, "strict": True} for t in TOOLS],
            tool_choice={"type": "any"},
            disable_parallel_tool_use=True,
            thinking={"type": "adaptive"},
        )
        self.usage.add(input_tokens=response.usage.input_tokens,
                       output_tokens=response.usage.output_tokens)
        block = next((b for b in response.content if b.type == "tool_use"), None)
        if block is None:
            raise LLMError(f"model returned no action (stop_reason={response.stop_reason})")
        return parse_move(block.name, dict(block.input)), json.dumps(
            {"tool": block.name, "input": dict(block.input)})

    def ask_json(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        response = self._client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        self.usage.add(input_tokens=response.usage.input_tokens,
                       output_tokens=response.usage.output_tokens)
        text = "".join(b.text for b in response.content if b.type == "text")
        return json.loads(text)


# --------------------------------------------------------------------------
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _first_json_object(text: str) -> dict[str, Any]:
    """Pull one JSON object out of a model response, fences and prose included."""
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(candidate)):
            ch = candidate[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = candidate.find("{", start + 1)
    raise LLMError(f"no JSON object in model response: {text[:300]!r}")


class ClaudeCliClient:
    """The local `claude` binary, headless.

    Used where no API key is available. Tool calling is expressed as a JSON
    contract instead of native tools, and the result is validated against the
    same `AgentMove` model, so the loop cannot tell the difference.
    """

    name = "cli"

    def __init__(self, model: str = DEFAULT_MODEL, timeout: float = 240.0,
                 binary: str = "claude") -> None:
        path = shutil.which(binary)
        if not path:
            raise LLMError(f"`{binary}` is not on PATH; use the `api` backend instead")
        self.binary, self.model, self.timeout = path, model, timeout
        self.usage = Usage()

    def _run(self, system: str, prompt: str) -> str:
        cmd = [
            self.binary, "-p", prompt,
            "--system-prompt", system,
            "--exclude-dynamic-system-prompt-sections",
            "--allowed-tools", "",
            "--output-format", "json",
            "--model", self.model,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout, env=os.environ.copy())
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"`claude` timed out after {self.timeout}s") from exc
        if proc.returncode != 0:
            raise LLMError(f"`claude` exited {proc.returncode}: {proc.stderr[:400]}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMError(f"unparseable CLI output: {proc.stdout[:300]!r}") from exc
        if payload.get("is_error"):
            raise LLMError(f"CLI reported an error: {payload.get('result')!r}")
        usage = payload.get("usage") or {}
        self.usage.add(input_tokens=usage.get("input_tokens"),
                       output_tokens=usage.get("output_tokens"),
                       cost_usd=payload.get("total_cost_usd"))
        return payload.get("result") or ""

    def decide(self, system: str, messages: list[dict[str, Any]]) -> tuple[AgentMove, str]:
        full_system = (
            f"{system}\n\n"
            f"## How to answer\n"
            f"Reply with exactly ONE JSON object and nothing else -- no prose, no code "
            f"fences. Shape: {{\"tool\": \"<one of the tools below>\", \"thought\": "
            f"\"...\", ...tool inputs...}}\n\n"
            f"## Tools\n{tools_as_text()}")
        move = _first_json_object(self._run(full_system, _render(messages)))
        tool = move.get("tool")
        if not tool:
            raise LLMError(f"response has no `tool` field: {move}")
        return parse_move(tool, move), json.dumps(move)

    def ask_json(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        sys_prompt = (f"{system}\n\nReply with exactly ONE JSON object matching this "
                      f"schema and nothing else:\n{json.dumps(schema)}")
        return _first_json_object(self._run(sys_prompt, prompt))


def _render(messages: list[dict[str, Any]]) -> str:
    """Flatten the conversation for a backend with no message history."""
    out = []
    for m in messages:
        content = m["content"]
        if not isinstance(content, str):
            content = "\n".join(
                c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
        out.append(f"[{m['role'].upper()}]\n{content}")
    return "\n\n".join(out)


# --------------------------------------------------------------------------
def build_client(backend: str = "auto", model: str = DEFAULT_MODEL) -> LLMClient:
    """`auto` prefers the API when a key is present, else the local CLI."""
    if backend == "api":
        return AnthropicClient(model)
    if backend == "cli":
        return ClaudeCliClient(model)
    if backend != "auto":
        raise LLMError(f"unknown LLM backend {backend!r}; use api, cli, or auto")
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicClient(model)
        except LLMError:
            pass
    return ClaudeCliClient(model)
