"""Perception against the real, deliberately hostile fixture.

These are the claims the whole design rests on, so they are checked against a
live browser rather than a mock.
"""

from __future__ import annotations

import pytest

from harness import CHROMIUM, fixture_app
from scribe.surface.base import normalize_name
from scribe.surface.web import WebSurface
from scribe.types.core import ControlRef, LocatorCandidate, LocatorStrategy

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def signed_on():
    with fixture_app() as base:
        surface = WebSurface(executable_path=CHROMIUM, headless=True)
        try:
            surface.navigate(base + "/")
            surface.page.fill("input[name=f_uid]", "svc_automation")
            surface.page.fill("input[name=f_pwd]", "Tr0ubadour!")
            surface.page.click("input[name=btnLogon]")
            surface.settle(300)
            yield surface, base
        finally:
            surface.close()


def named(obs, role, name):
    return next((e for e in obs.elements
                 if e.role == role and normalize_name(e.name) == normalize_name(name)), None)


def test_the_name_is_recovered_from_the_neighbouring_table_cell(signed_on):
    """The central claim.

    The member-id field has no <label>, no aria-label, and an id regenerated on
    every render. Its name is a sibling <td>. Chromium's own accessibility tree
    reports no name for it at all -- `name_source` records that we got there by
    table adjacency instead.
    """
    surface, _ = signed_on
    obs = surface.observe()
    field = named(obs, "textbox", "Member ID")
    assert field is not None, [e.render() for e in obs.elements]
    assert field.name_source == "table_adjacent"


def test_controls_are_found_inside_a_frameset(signed_on):
    surface, _ = signed_on
    obs = surface.observe()
    assert "content" in obs.frames
    field = named(obs, "textbox", "Member ID")
    assert field.frame_path == ["content"]


def test_element_ids_really_are_regenerated_per_render(signed_on):
    """Justifies ranking id-based locators last."""
    surface, base = signed_on
    surface.navigate(base + "/members/search")
    first = surface.page.get_attribute("input[name=f_mid]", "id")
    surface.navigate(base + "/members/search")
    second = surface.page.get_attribute("input[name=f_mid]", "id")
    assert first and second and first != second


def test_role_and_name_resolve_the_control(signed_on):
    surface, base = signed_on
    surface.navigate(base + "/members/search")
    obs = surface.observe()
    ref = ControlRef(role="textbox", name="Member ID", candidates=[
        LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME,
                         args={"role": "textbox", "name": "Member ID"}, confidence=0.95)])
    found = surface.resolve(ref, observation=obs)
    assert found is not None
    assert found.strategy == LocatorStrategy.ROLE_NAME
    assert found.rank == 0


def test_a_tenant_alias_resolves_a_renamed_control(signed_on):
    """One recording against a deployment that calls the field something else."""
    surface, base = signed_on
    surface.navigate(base + "/members/search")
    obs = surface.observe()
    ref = ControlRef(role="textbox", name="Member Number", candidates=[
        LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME,
                         args={"role": "textbox", "name": "Member Number"},
                         confidence=0.95)])
    assert surface.resolve(ref, observation=obs) is None
    assert surface.resolve(ref, observation=obs,
                           aliases={"Member Number": "Member ID"}) is not None


def test_a_missing_control_resolves_to_none_rather_than_something_nearby(signed_on):
    surface, base = signed_on
    surface.navigate(base + "/members/search")
    ref = ControlRef(role="button", name="Post Transaction", candidates=[
        LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME,
                         args={"role": "button", "name": "Post Transaction"},
                         confidence=0.95)])
    assert surface.resolve(ref) is None


def test_no_coordinate_locator_is_recorded_for_a_web_surface(signed_on):
    """A point always matches something, so recording one would guarantee the
    resolver never reports a missing control."""
    surface, base = signed_on
    surface.navigate(base + "/members/search")
    obs = surface.observe()
    for element in obs.elements:
        assert all(c.strategy != LocatorStrategy.COORDINATES for c in element.candidates)


def test_grids_and_caption_value_pairs_are_both_extracted(signed_on):
    surface, base = signed_on
    surface.navigate(base + "/members/detail?f_mid=100412")
    obs = surface.observe()
    grid = next(t for t in obs.tables if "Balance" in t.headers)
    assert ["SAVINGS", "SAV-0100412-01", "4,182.55", "OPEN"] in grid.rows
    assert ("Member Name", "Dolores Vance") in [(p.label, p.value) for p in obs.pairs]


def test_the_rendered_view_is_a_control_list_not_a_dom_dump(signed_on):
    surface, base = signed_on
    surface.navigate(base + "/members/search")
    rendered = surface.observe().render()
    assert "CONTROLS:" in rendered
    assert 'textbox "Member ID"' in rendered
    assert "<table" not in rendered and "<input" not in rendered
