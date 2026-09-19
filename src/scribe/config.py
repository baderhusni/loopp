"""Runtime configuration read from the environment, with sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


# Some sandboxes ship a preinstalled Chromium that does not match the Playwright
# build the package would download. Honour an explicit path first, then fall back
# to the conventional preinstall location, then let Playwright resolve its own.
_PREINSTALLED = ("/opt/pw-browsers/chromium",)


def resolve_chromium() -> str | None:
    explicit = os.environ.get("SCRIBE_CHROMIUM_PATH")
    if explicit:
        return explicit
    for candidate in _PREINSTALLED:
        if Path(candidate).exists():
            return candidate
    return None


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    """Defaults are read once at import; every field can be overridden per call."""

    # Browser. SCRIBE_CHROMIUM_PATH lets a sandbox point at a preinstalled
    # Chromium instead of one downloaded by `playwright install`.
    chromium_path: str | None = None
    headless: bool = _bool("SCRIBE_HEADLESS", True)

    # Model. Two backends, because not every environment has an API key:
    #   api  -- the Anthropic SDK with ANTHROPIC_API_KEY
    #   cli  -- the local `claude` CLI in headless mode (subscription auth)
    llm_backend: str = os.environ.get("SCRIBE_LLM_BACKEND", "auto")
    model: str = os.environ.get("SCRIBE_MODEL", "claude-opus-5")
    max_steps: int = int(os.environ.get("SCRIBE_MAX_STEPS", "24"))

    # Paths
    capabilities_dir: Path = REPO_ROOT / "capabilities"
    evidence_dir: Path = REPO_ROOT / "evidence"
    policy_file: Path = REPO_ROOT / "policies" / "policy.yaml"

    # Operator console
    console_host: str = os.environ.get("SCRIBE_CONSOLE_HOST", "127.0.0.1")
    console_port: int = int(os.environ.get("SCRIBE_CONSOLE_PORT", "8900"))

    def browser_kwargs(self) -> dict:
        return {"headless": self.headless,
                "executable_path": self.chromium_path or resolve_chromium()}


SETTINGS = Settings()
