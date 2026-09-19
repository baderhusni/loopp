"""Playwright-backed surface for web applications, legacy ones included.

Perception works the way an operator's eyes do rather than the way a scraper
does. Per frame we build a control graph with an accessible-name algorithm that
falls back through ARIA, `<label>`, control text, and -- the step that matters
on this class of app -- the neighbouring table cell.

That last fallback is not a nicety. On the fixture app, Chromium's own
accessibility tree reports an **empty name** for the member-id field, because
its label is a sibling `<td>` rather than a `<label for=...>`. An automation
that trusts the AX tree alone cannot name the most important control on the
screen. We still read the AX tree and keep its answer, but as a cross-check:
where it disagrees, `name_source` records which evidence produced the name so a
reviewer knows how much to trust a locator built from it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from playwright.sync_api import Browser, BrowserContext, Error as PWError, Page, sync_playwright

from ..types.core import (
    STRATEGY_CONFIDENCE,
    ControlRef,
    LocatorCandidate,
    LocatorStrategy,
    SurfaceKind,
)
from ..types.observation import FieldPair, Observation, TableSnapshot, UIElement
from .base import AmbiguousTarget, ResolvedControl, SurfaceError, normalize_name

_JS = (Path(__file__).parent / "control_graph.js").read_text()

# Element ids on this class of app are commonly regenerated per render
# (ASP.NET ctl00$..., our fixture's ctl_9f21a). An id-based locator that looks
# like this is recorded, but at a confidence that keeps it last.
_VOLATILE_ID = re.compile(r"(ctl[_\d]|[0-9a-f]{6,}|\b\d{4,}\b)", re.I)


class WebSurface:
    """One live browser session. Owns the page; the escalation layer borrows it."""

    kind = SurfaceKind.LEGACY_WEB

    def __init__(
        self,
        *,
        headless: bool = True,
        executable_path: str | None = None,
        viewport: tuple[int, int] = (1280, 900),
        slow_mo: float = 0,
        default_timeout_ms: int = 10_000,
    ) -> None:
        self._pw = sync_playwright().start()
        args = ["--no-sandbox", "--disable-dev-shm-usage"]
        launch: dict[str, Any] = {"headless": headless, "args": args, "slow_mo": slow_mo}
        if executable_path:
            launch["executable_path"] = executable_path
        self._browser: Browser = self._pw.chromium.launch(**launch)
        self._ctx: BrowserContext = self._browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]})
        self._ctx.set_default_timeout(default_timeout_ms)
        self._page: Page = self._ctx.new_page()
        self._last_status: int | None = None
        self._page.on("response", self._note_response)
        self._cdp = None
        self._ax_available = True

    # -- lifecycle -------------------------------------------------------
    @property
    def page(self) -> Page:
        """The live page. The operator console drives this exact object."""
        return self._page

    def close(self) -> None:
        for shut in (self._ctx.close, self._browser.close, self._pw.stop):
            try:
                shut()
            except Exception:
                pass

    def _note_response(self, response) -> None:
        try:
            if response.frame == self._page.main_frame and response.request.resource_type == "document":
                self._last_status = response.status
        except Exception:
            pass

    @property
    def last_status(self) -> int | None:
        return self._last_status

    # -- perception ------------------------------------------------------
    def observe(self, *, screenshot: bool = False, screenshot_path: str | None = None) -> Observation:
        frames = []
        elements: list[UIElement] = []
        tables: list[TableSnapshot] = []
        pairs: list[FieldPair] = []
        texts: list[str] = []
        frame_texts: dict[str, str] = {}
        counter = 0

        for frame in self._page.frames:
            path = self._frame_path(frame)
            try:
                raw = frame.evaluate(_JS)
            except PWError:
                continue  # frame detached mid-observation; skip it
            frames.append("/".join(path) or "(top)")
            ax = self._ax_names(frame)

            for e in raw.get("elements", []):
                counter += 1
                ref = f"e{counter}"
                # Re-key the in-page marker to a globally unique ref so refs are
                # unambiguous across frames.
                try:
                    frame.evaluate(
                        "([old, new_]) => { const el = document.querySelector("
                        "`[data-scribe-ref=\"${old}\"]`); if (el) el.setAttribute("
                        "'data-scribe-ref', new_); }",
                        [e["ref"], ref])
                except PWError:
                    continue
                ax_role, ax_name = ax.get(e["ref"], (None, None))
                name = e["name"] or (ax_name or "")
                source = e["name_source"]
                if not e["name"] and ax_name:
                    source = "ax"
                elements.append(UIElement(
                    ref=ref,
                    role=ax_role or e["role"],
                    name=name,
                    value=e.get("value") or "",
                    enabled=bool(e.get("enabled", True)),
                    frame_path=path,
                    name_source=source,
                    bbox=tuple(e["bbox"]) if e.get("bbox") else None,
                    candidates=self._candidates(e, name, ax_role or e["role"], path, ax_name),
                ))

            for t in raw.get("tables", []):
                tables.append(TableSnapshot(
                    caption=t.get("caption", ""), headers=t.get("headers", []),
                    rows=t.get("rows", []), frame_path=path))
            for p in raw.get("pairs", []):
                pairs.append(FieldPair(label=p["label"], value=p["value"], frame_path=path))
            key = "/".join(path) or "(top)"
            frame_texts[key] = raw.get("text", "")
            if raw.get("text"):
                label = "/".join(path)
                texts.append(f"--- {label} ---\n{raw['text']}" if label else raw["text"])

        shot = None
        if screenshot and screenshot_path:
            shot = self.screenshot(screenshot_path)

        return Observation(
            url=self._page.url,
            title=self._safe_title(),
            http_status=self._last_status,
            elements=elements,
            tables=tables,
            pairs=pairs,
            text="\n\n".join(texts),
            frame_texts=frame_texts,
            frames=frames,
            screenshot_path=shot,
        )

    def _safe_title(self) -> str:
        try:
            return self._page.title()
        except PWError:
            return ""

    @staticmethod
    def _frame_path(frame) -> list[str]:
        """Frame names from the top document down. Identity on a frameset app."""
        path, cur = [], frame
        while cur.parent_frame is not None:
            path.append(cur.name or f"frame[{cur.url.rsplit('/', 1)[-1]}]")
            cur = cur.parent_frame
        return list(reversed(path))

    def _ax_names(self, frame) -> dict[str, tuple[str | None, str | None]]:
        """Chromium's own accessible names, keyed by our in-page marker.

        Best effort and deliberately non-fatal: this is corroboration, not the
        source of truth. On legacy markup it is frequently empty, which is the
        point being measured.
        """
        if not self._ax_available:
            return {}
        try:
            if self._cdp is None:
                self._cdp = self._ctx.new_cdp_session(self._page)
                self._cdp.send("DOM.enable")
                self._cdp.send("Accessibility.enable")
            self._cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
            fid = self._cdp_frame_id(frame)
            tree = self._cdp.send(
                "Accessibility.getFullAXTree", {"frameId": fid} if fid else {})
            nodes = [n for n in tree.get("nodes", []) if n.get("backendDOMNodeId")]
            if not nodes:
                return {}
            pushed = self._cdp.send(
                "DOM.pushNodesByBackendIdsToFrontend",
                {"backendNodeIds": [n["backendDOMNodeId"] for n in nodes]})
            out: dict[str, tuple[str | None, str | None]] = {}
            for node, node_id in zip(nodes, pushed.get("nodeIds", [])):
                if not node_id:
                    continue
                try:
                    attrs = self._cdp.send("DOM.getAttributes", {"nodeId": node_id})["attributes"]
                except Exception:
                    continue
                d = dict(zip(attrs[0::2], attrs[1::2]))
                ref = d.get("data-scribe-ref")
                if ref:
                    out[ref] = ((node.get("role") or {}).get("value"),
                                (node.get("name") or {}).get("value"))
            return out
        except Exception:
            self._ax_available = False  # degrade to DOM-only for the rest of the run
            return {}

    def _cdp_frame_id(self, frame) -> str | None:
        try:
            tree = self._cdp.send("Page.getFrameTree")["frameTree"]
        except Exception:
            return None
        want = frame.url

        def walk(node):
            f = node["frame"]
            if f.get("url") == want:
                return f["id"]
            for child in node.get("childFrames", []):
                got = walk(child)
                if got:
                    return got
            return None

        return walk(tree)

    @staticmethod
    def _candidates(raw: dict, name: str, role: str, frame_path: list[str],
                    ax_name: str | None) -> list[LocatorCandidate]:
        """Record several ways to find this control again, ranked by durability."""
        out: list[LocatorCandidate] = []
        conf = STRATEGY_CONFIDENCE

        if name:
            note = ("name confirmed by the browser accessibility tree"
                    if ax_name and normalize_name(ax_name) == normalize_name(name)
                    else f"name recovered from {raw['name_source']}; "
                         "browser AX tree reports no name")
            out.append(LocatorCandidate(
                strategy=LocatorStrategy.ROLE_NAME,
                args={"role": role, "name": name, "frame_path": frame_path},
                confidence=conf[LocatorStrategy.ROLE_NAME]
                if ax_name else conf[LocatorStrategy.ROLE_NAME] - 0.05,
                note=note))

        if raw.get("name_source") in ("table_adjacent", "label", "text") and name:
            out.append(LocatorCandidate(
                strategy=LocatorStrategy.LABEL_ADJACENT,
                args={"label": name, "role": role, "frame_path": frame_path},
                confidence=conf[LocatorStrategy.LABEL_ADJACENT],
                note="field identified by the caption rendered beside it"))

        if raw.get("link_text"):
            out.append(LocatorCandidate(
                strategy=LocatorStrategy.LINK_TEXT,
                args={"text": raw["link_text"], "frame_path": frame_path},
                confidence=conf[LocatorStrategy.LINK_TEXT],
                note="anchor text"))

        if raw.get("field_name"):
            out.append(LocatorCandidate(
                strategy=LocatorStrategy.FIELD_NAME,
                args={"field_name": raw["field_name"], "role": role,
                      "frame_path": frame_path},
                confidence=conf[LocatorStrategy.FIELD_NAME],
                note="form control name= attribute; survives restyling, "
                     "breaks on a backend rewrite"))

        if raw.get("css"):
            elem_id = raw.get("elem_id") or ""
            volatile = bool(elem_id and _VOLATILE_ID.search(elem_id))
            out.append(LocatorCandidate(
                strategy=LocatorStrategy.CSS,
                args={"selector": raw["css"], "frame_path": frame_path},
                confidence=conf[LocatorStrategy.CSS],
                note="structural path; last resort"
                     + (" (element id looks machine-generated, excluded)" if volatile else "")))

        # No coordinate candidate is recorded for a web surface, deliberately.
        # A point always "matches" -- there is something at every pixel -- so a
        # coordinate in the ladder would guarantee the resolver never reports
        # TARGET_NOT_FOUND, and instead click whatever has since moved under it.
        # Coordinates belong to surfaces that genuinely have nothing better
        # (canvas, Citrix, screenshot-only remote apps), where the resolver
        # accepts them only when a caller opts in. The element's box is still
        # carried on the observation for the operator console.
        return out

    # -- targeting -------------------------------------------------------
    def resolve(
        self,
        ref: ControlRef,
        *,
        observation: Observation | None = None,
        aliases: Mapping[str, str] | None = None,
        allow_coordinates: bool = False,
    ) -> ResolvedControl | None:
        obs = observation or self.observe()
        aliases = aliases or {}

        for rank, cand in enumerate(ref.ranked()):
            if (cand.strategy == LocatorStrategy.COORDINATES
                    and not allow_coordinates):
                continue
            found = self._try_candidate(cand, ref, obs, aliases)
            if found is None:
                continue
            found.rank = rank
            return found
        return None

    def _try_candidate(self, cand: LocatorCandidate, ref: ControlRef,
                       obs: Observation, aliases: Mapping[str, str]) -> ResolvedControl | None:
        s, a = cand.strategy, cand.args
        want_frame = a.get("frame_path") or ref.frame_path

        if s in (LocatorStrategy.ROLE_NAME, LocatorStrategy.LABEL_ADJACENT,
                 LocatorStrategy.LINK_TEXT, LocatorStrategy.FIELD_NAME):
            raw_name = a.get("name") or a.get("label") or a.get("text") or ref.name
            # Tenant wording differences are applied here, so one recording works
            # against a deployment that renamed the control.
            name = aliases.get(raw_name, raw_name)
            role = a.get("role") or ref.role
            pool = [e for e in obs.elements
                    if s == LocatorStrategy.FIELD_NAME or self._role_ok(e.role, role)]
            if want_frame:
                exact_frame = [e for e in pool if e.frame_path == want_frame]
                pool = exact_frame or pool

            if s == LocatorStrategy.FIELD_NAME:
                hits = [e for e in pool
                        if self._field_name_of(obs, e) == a.get("field_name")]
                mode = "exact"
            else:
                hits, mode = self._match_by_name(pool, name)
            if not hits:
                return None
            if len(hits) > 1:
                raise AmbiguousTarget(ref, len(hits))
            return ResolvedControl(
                handle=hits[0].ref, strategy=s, rank=0, confidence=cand.confidence,
                match_mode=mode, matches=1, frame_path=hits[0].frame_path,
                detail=f'{hits[0].role} "{hits[0].name}"')

        if s == LocatorStrategy.CSS:
            frame = self._frame_for(want_frame)
            if frame is None:
                return None
            try:
                loc = frame.locator(a["selector"])
                count = loc.count()
            except PWError:
                return None
            if count == 0:
                return None
            if count > 1:
                raise AmbiguousTarget(ref, count)
            handle = f"css::{json.dumps([want_frame, a['selector']])}"
            return ResolvedControl(handle=handle, strategy=s, rank=0,
                                   confidence=cand.confidence, frame_path=want_frame,
                                   detail=a["selector"])

        if s == LocatorStrategy.COORDINATES:
            pt = a.get("point")
            if not pt:
                return None
            return ResolvedControl(handle=f"point::{pt[0]},{pt[1]}", strategy=s, rank=0,
                                   confidence=cand.confidence, frame_path=want_frame,
                                   detail=f"({pt[0]:.0f},{pt[1]:.0f})")
        return None

    @staticmethod
    def _role_ok(actual: str, wanted: str) -> bool:
        if not wanted or actual == wanted:
            return True
        # Roles that are interchangeable for targeting purposes.
        equiv = {("textbox", "searchbox"), ("combobox", "listbox"),
                 ("button", "link"), ("link", "button")}
        return (actual, wanted) in equiv

    @staticmethod
    def _match_by_name(pool: list[UIElement], name: str) -> tuple[list[UIElement], str]:
        want = normalize_name(name)
        if not want:
            return [], "none"
        for mode, pred in (
            ("exact", lambda n: n == want),
            ("prefix", lambda n: n.startswith(want) or want.startswith(n)),
            ("contains", lambda n: want in n or n in want),
        ):
            hits = [e for e in pool if pred(normalize_name(e.name))]
            if hits:
                return hits, mode
        return [], "none"

    @staticmethod
    def _field_name_of(obs: Observation, el: UIElement) -> str:
        for c in el.candidates:
            if c.strategy == LocatorStrategy.FIELD_NAME:
                return c.args.get("field_name", "")
        return ""

    def _frame_for(self, frame_path: list[str]):
        if not frame_path:
            return self._page.main_frame
        for f in self._page.frames:
            if self._frame_path(f) == frame_path:
                return f
        # Frame names can differ between deployments; fall back to the last name.
        for f in self._page.frames:
            if f.name and frame_path and f.name == frame_path[-1]:
                return f
        return None

    def _locator(self, target: ResolvedControl):
        if target.handle.startswith("point::"):
            raise SurfaceError(
                "this control was located only by coordinate, which supports "
                "clicking but not typing -- resolve it semantically first")
        if target.handle.startswith("css::"):
            frame_path, selector = json.loads(target.handle[5:])
            frame = self._frame_for(frame_path)
            if frame is None:
                raise SurfaceError(f"frame {frame_path} is gone")
            return frame.locator(selector).first
        frame = self._frame_for(target.frame_path)
        if frame is None:
            raise SurfaceError(f"frame {target.frame_path} is gone")
        return frame.locator(f'[data-scribe-ref="{target.handle}"]').first

    # -- acting ----------------------------------------------------------
    def click(self, target: ResolvedControl) -> None:
        if target.handle.startswith("point::"):
            x, y = (float(v) for v in target.handle[7:].split(","))
            self._page.mouse.click(x, y)
            return
        self._locator(target).click()

    def fill(self, target: ResolvedControl, value: str) -> None:
        self._locator(target).fill(value)

    def select(self, target: ResolvedControl, value: str) -> None:
        loc = self._locator(target)
        try:
            loc.select_option(label=value)
        except PWError:
            loc.select_option(value=value)

    def press(self, key: str, target: ResolvedControl | None = None) -> None:
        if target is None:
            self._page.keyboard.press(key)
        else:
            self._locator(target).press(key)

    def navigate(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")

    def settle(self, ms: int) -> None:
        try:
            self._page.wait_for_load_state("networkidle", timeout=max(ms, 500))
        except PWError:
            pass
        self._page.wait_for_timeout(ms)

    # -- evidence --------------------------------------------------------
    def screenshot(self, path: str) -> str | None:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._page.screenshot(path=path, full_page=False)
            return path
        except PWError:
            return None

    def raw_snapshot(self, path: str) -> str | None:
        """All frames' HTML. Only written on failure -- it is large and sensitive."""
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            chunks = []
            for f in self._page.frames:
                try:
                    chunks.append(f"<!-- frame: {'/'.join(self._frame_path(f)) or '(top)'} "
                                  f"url={f.url} -->\n{f.content()}")
                except PWError:
                    continue
            Path(path).write_text("\n\n".join(chunks), encoding="utf-8")
            return path
        except Exception:
            return None
