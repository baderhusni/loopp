"""The surface seam.

Everything above this file speaks `Observation`, `UIElement`, and `ControlRef`.
Nothing above it knows what a CSS selector is. That is the whole point: swapping
a Playwright browser for a Windows UI Automation client or an OCR-plus-clicks
driver is a matter of writing one more class here, not of touching the agent
loop, the recorder, the replay engine, or the artifact schema.

A surface owes the rest of the system four things:

  perceive   -- produce a normalized control graph (`observe`)
  target     -- turn a recorded `ControlRef` into something clickable (`resolve`)
  act        -- click / type / select / press / navigate
  evidence   -- screenshots and a raw snapshot for debugging

Resolution is kept separate from acting on purpose. "I could not find the
control" and "I found it and the click failed" are different failures with
different owners, and a combined `act(action)` call blurs them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, runtime_checkable

from ..types.core import ControlRef, LocatorStrategy, SurfaceKind
from ..types.observation import Observation


@dataclass
class ResolvedControl:
    """A control found on the live surface, plus how well it was found."""

    handle: str                     # opaque to callers; meaningful to the surface
    strategy: LocatorStrategy
    rank: int                       # 0 = the preferred candidate worked
    confidence: float
    match_mode: str = "exact"       # exact | prefix | contains
    matches: int = 1                # >1 means ambiguous
    frame_path: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def used_fallback(self) -> bool:
        return self.rank > 0

    @property
    def is_structural(self) -> bool:
        return self.strategy in (
            LocatorStrategy.CSS, LocatorStrategy.XPATH, LocatorStrategy.COORDINATES)


class SurfaceError(RuntimeError):
    """The surface itself failed -- driver crashed, frame detached, target gone."""


class AmbiguousTarget(SurfaceError):
    def __init__(self, ref: ControlRef, matches: int) -> None:
        super().__init__(
            f'{matches} controls match {ref.role} "{ref.name}"; refusing to guess')
        self.ref, self.matches = ref, matches


@runtime_checkable
class Surface(Protocol):
    """What every surface implementation must provide."""

    kind: SurfaceKind

    def observe(self, *, screenshot: bool = False) -> Observation: ...

    def resolve(
        self,
        ref: ControlRef,
        *,
        observation: Observation | None = None,
        aliases: Mapping[str, str] | None = None,
        allow_coordinates: bool = False,
    ) -> ResolvedControl | None:
        """Find the control, or return None. Raises AmbiguousTarget on a tie.

        `allow_coordinates` is off by default and exists for surfaces with no
        semantic layer at all. A coordinate always matches something, so
        enabling it anywhere else turns a missing control into a misdirected
        click.
        """

    def click(self, target: ResolvedControl) -> None: ...
    def fill(self, target: ResolvedControl, value: str) -> None: ...
    def select(self, target: ResolvedControl, value: str) -> None: ...
    def press(self, key: str, target: ResolvedControl | None = None) -> None: ...
    def navigate(self, url: str) -> None: ...

    def screenshot(self, path: str) -> str | None: ...
    def raw_snapshot(self, path: str) -> str | None:
        """Surface-native dump for debugging (DOM, AX tree, window tree)."""

    def close(self) -> None: ...


def normalize_name(text: str) -> str:
    """Compare control names the way a person would read them aloud."""
    return " ".join((text or "").replace(" ", " ").split()).strip(" :*").casefold()
