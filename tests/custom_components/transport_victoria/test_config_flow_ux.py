"""T17 config-flow usability overhaul tests (light env, stub style).

Verifies the pure helper decisions and the light-env-observable flow behaviour
of the streamlined steps:

- searchable-dropdown mode selection (threshold helper)
- reorder actions (up/down/first/last) and re-render-after-apply
- board options single-screen direct routing (no two-hop menu)
- API-key reuse/prefill when another entry already carries a key
- the "Add another line?" menu after pin options

These run in the light env (homeassistant stubbed); real-runtime equivalents
live in ``test_board_subentry_flow.py`` (classic harness).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.transport_victoria import config_flow as cf
from custom_components.transport_victoria.board import PinData
from custom_components.transport_victoria.const import (
    CONF_API_KEY,
    CONF_BOARD_NAME,
    CONF_DEPARTURE_COUNT,
    CONF_ENTRY_TYPE,
    CONF_INCLUDE,
    CONF_LABEL_FROM,
    CONF_LABEL_TO,
    CONF_SUBENTRY_ORDER,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    SUBENTRY_TYPE_PIN,
)


# ---------------------------------------------------------------------------
# Pure helper decisions
# ---------------------------------------------------------------------------


def test_select_selector_mode_threshold():
    """Lists longer than the threshold render as a searchable dropdown."""
    assert cf._select_selector_mode(6) == "list"
    assert cf._select_selector_mode(7) == "dropdown"
    assert cf._select_selector_mode(100) == "dropdown"
    # Destination candidates are always rendered as a dropdown (capped list).
    assert cf._select_selector_mode(cf._DEST_CANDIDATE_CAP) == "dropdown"


def test_reorder_subentry_actions():
    """up/down/first/last move a pin; boundaries are no-ops."""
    assert cf._reorder_subentry(["a", "b", "c"], "b", "up") == ["b", "a", "c"]
    assert cf._reorder_subentry(["a", "b", "c"], "b", "down") == ["a", "c", "b"]
    assert cf._reorder_subentry(["a", "b", "c"], "c", "first") == ["c", "a", "b"]
    assert cf._reorder_subentry(["a", "b", "c"], "a", "last") == ["b", "c", "a"]
    # Boundaries are no-ops.
    assert cf._reorder_subentry(["a", "b", "c"], "a", "up") == ["a", "b", "c"]
    assert cf._reorder_subentry(["a", "b", "c"], "c", "down") == ["a", "b", "c"]
    # Missing id / single element are no-ops.
    assert cf._reorder_subentry(["a", "b"], "zzz", "down") == ["a", "b"]
    assert cf._reorder_subentry(["a"], "a", "first") == ["a"]


# ---------------------------------------------------------------------------
# API-key reuse / prefill
# ---------------------------------------------------------------------------


def _fake_hass_with_entries(entries):
    return SimpleNamespace(
        config=SimpleNamespace(config_dir="/unused"),
        config_entries=SimpleNamespace(async_entries=lambda domain: entries),
        async_create_task=lambda coro: None,
    )


async def test_existing_api_key_helper(monkeypatch):
    """_get_existing_api_key returns the key from another entry."""
    flow = cf.PTVConfigFlow()
    flow.hass = _fake_hass_with_entries(
        [SimpleNamespace(data={CONF_API_KEY: "secret-key"})]
    )
    assert flow._get_existing_api_key() == "secret-key"


async def test_key_reuse_when_another_entry_exists(monkeypatch):
    """A second entry reuses the stored key and skips the key step."""
    monkeypatch.setattr(cf, "async_trigger_gtfs_refresh", lambda hass, *a, **k: None)
    flow = cf.PTVConfigFlow()
    flow.hass = _fake_hass_with_entries(
        [SimpleNamespace(data={CONF_API_KEY: "secret-key"})]
    )
    advanced = {}

    async def fake_entry_type(user_input=None):
        advanced["called"] = True
        return {"step_id": "entry_type"}

    monkeypatch.setattr(flow, "async_step_entry_type", fake_entry_type)

    result = await flow.async_step_user(None)

    assert advanced["called"] is True
    assert flow._config[CONF_API_KEY] == "secret-key"


# ---------------------------------------------------------------------------
# Board options single-screen routing + reorder re-render
# ---------------------------------------------------------------------------


def _board_options_flow():
    entry = SimpleNamespace(
        entry_id="board-entry",
        title="Board",
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_BOARD, CONF_BOARD_NAME: "Board"},
        options={CONF_SUBENTRY_ORDER: ["sid1", "sid2"], CONF_DEPARTURE_COUNT: 5},
        subentries={
            "sid1": SimpleNamespace(data={}),
            "sid2": SimpleNamespace(data={}),
        },
    )
    flow = cf.PTVOptionsFlow()
    flow.config_entry = entry
    flow.hass = SimpleNamespace(
        config=SimpleNamespace(config_dir="/unused"),
        config_entries=SimpleNamespace(
            async_update_entry=lambda entry, **kw: None,
            async_remove_subentry=lambda entry, sid: None,
        ),
    )
    return flow, entry


async def test_board_menu_direct_routing(monkeypatch):
    """The board options screen is a single form that routes directly."""
    flow, _entry = _board_options_flow()
    called = {}

    async def fake_settings(user_input=None):
        called["settings"] = True
        return {"step_id": "board_settings"}

    async def fake_reorder(user_input=None):
        called["reorder"] = True
        return {"step_id": "board_reorder"}

    async def fake_remove(user_input=None):
        called["remove"] = True
        return {"step_id": "board_remove"}

    monkeypatch.setattr(flow, "async_step_board_settings", fake_settings)
    monkeypatch.setattr(flow, "async_step_board_reorder", fake_reorder)
    monkeypatch.setattr(flow, "async_step_board_remove", fake_remove)

    shown = {}

    def fake_show_form(**kwargs):
        shown.update(kwargs)
        return {"type": "form", "step_id": "board_menu"}

    monkeypatch.setattr(flow, "async_show_form", fake_show_form)

    # No input → a single form (not a menu).
    result = await flow._step_board_menu(None)
    assert result["type"] == "form"
    assert shown["step_id"] == "board_menu"

    # Each selection routes straight to its sub-step (single hop).
    await flow._step_board_menu({"board_action": "reorder"})
    assert called["reorder"] is True
    await flow._step_board_menu({"board_action": "remove"})
    assert called["remove"] is True
    await flow._step_board_menu({"board_action": "settings"})
    assert called["settings"] is True


async def test_reorder_applies_and_rerenders(monkeypatch):
    """A move is applied immediately and the reorder step re-renders."""
    flow, entry = _board_options_flow()

    updated = {}

    def fake_update_entry(entry, **kwargs):
        updated.update(kwargs)
        entry.options = {**entry.options, **kwargs.get("options", {})}

    monkeypatch.setattr(flow.hass.config_entries, "async_update_entry", fake_update_entry)

    shown = []

    def fake_show_form(**kwargs):
        shown.append(kwargs)
        return {"type": "form", "step_id": "board_reorder"}

    monkeypatch.setattr(flow, "async_show_form", fake_show_form)

    created = []

    def fake_create_entry(**kwargs):
        created.append(kwargs)
        return {"type": "create_entry"}

    monkeypatch.setattr(flow, "async_create_entry", fake_create_entry)

    # Move sid1 down → applied + re-render (flow NOT finalised yet).
    result = await flow.async_step_board_reorder(
        {"pin_select": "sid1", "reorder_action": "down"}
    )
    assert result["step_id"] == "board_reorder"
    assert created == []
    assert updated["options"][CONF_SUBENTRY_ORDER] == ["sid2", "sid1"]

    # "Done" → finalises with the accumulated order.
    result = await flow.async_step_board_reorder(
        {"pin_select": "sid1", "reorder_action": "done"}
    )
    assert result["type"] == "create_entry"
    assert created[-1]["data"][CONF_SUBENTRY_ORDER] == ["sid2", "sid1"]


# ---------------------------------------------------------------------------
# "Add another line?" menu after pin options
# ---------------------------------------------------------------------------


def _subentry_flow():
    flow = cf.PTVBoardSubentryFlow()
    flow.hass = SimpleNamespace(
        config=SimpleNamespace(config_dir="/unused"),
        config_entries=SimpleNamespace(
            async_add_subentry=lambda entry, subentry: None,
        ),
    )
    return flow


async def test_pin_options_advances_to_add_another_menu(monkeypatch):
    """Submitting pin options advances to the add-another menu (not create)."""
    flow = _subentry_flow()
    draft = PinData(
        pin_key="pk",
        route_type=3,
        route_id="R1",
        boarding_stop_id="S",
        boarding_stop_name="Sunshine",
    )
    monkeypatch.setattr(flow, "_draft_pin", lambda: draft)

    menu = {}

    def fake_show_menu(**kwargs):
        menu.update(kwargs)
        return {"type": "menu", "step_id": "add_another"}

    monkeypatch.setattr(flow, "async_show_menu", fake_show_menu)

    result = await flow.async_step_options(
        {CONF_LABEL_FROM: "A", CONF_LABEL_TO: "B", CONF_INCLUDE: True}
    )

    assert result["step_id"] == "add_another"
    assert flow._pending_pin is not None
    assert flow._pending_pin.label_from == "A"
    assert flow._pending_pin.label_to == "B"


async def test_add_another_finish_creates_entry(monkeypatch):
    """"Done" creates the pending pin and finalises the flow."""
    flow = _subentry_flow()
    flow._pending_pin = PinData(
        pin_key="pk",
        route_type=3,
        route_id="R1",
        boarding_stop_id="S",
        boarding_stop_name="Sunshine",
        label_from="A",
        label_to="B",
    )

    created = {}

    def fake_create_entry(**kwargs):
        created.update(kwargs)
        return {"type": "create_entry"}

    monkeypatch.setattr(flow, "async_create_entry", fake_create_entry)

    result = await flow.async_step_finish(None)

    assert result["type"] == "create_entry"
    assert created["unique_id"] == "pk"
    assert created["data"]["pin_key"] == "pk"


async def test_add_another_line_commits_and_restarts(monkeypatch):
    """"Add another line" commits the pin and restarts at the origin step."""
    flow = _subentry_flow()
    flow._pending_pin = PinData(
        pin_key="pk",
        route_type=3,
        route_id="R1",
        boarding_stop_id="S",
        boarding_stop_name="Sunshine",
        label_from="A",
        label_to="B",
    )

    captured = {}

    class FakeSubentry:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(cf, "ConfigSubentry", FakeSubentry)
    monkeypatch.setattr(flow, "_get_entry", lambda: "fake-entry")

    added = {}

    def fake_add_subentry(entry, subentry):
        added["called"] = True

    monkeypatch.setattr(flow.hass.config_entries, "async_add_subentry", fake_add_subentry)

    restarted = {}

    async def fake_origin(user_input=None):
        restarted["called"] = True
        return {"step_id": "origin"}

    monkeypatch.setattr(flow, "async_step_origin", fake_origin)

    result = await flow.async_step_add_another_line(None)

    assert restarted["called"] is True
    assert added["called"] is True
    assert captured["unique_id"] == "pk"
    assert captured["subentry_type"] == SUBENTRY_TYPE_PIN
    assert captured["data"]["pin_key"] == "pk"
    # Add-flow state is reset for the next pin.
    assert flow._pending_pin is None
    assert flow._boarding_stop_id == ""
