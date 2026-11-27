"""T8 board consumer-union wiring tests (light env, stub style).

Verifies that :func:`_bundles_for_all_entries` / :func:`_board_bundles_for_entry`
compute the correct union of bundle folders — a board's pins span modes and so
contribute their route_type bundles, while watch entries keep their single-mode
mapping.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.transport_victoria import (
    _board_bundles_for_entry,
    _bundles_for_all_entries,
)
from custom_components.transport_victoria.board import PinData
from custom_components.transport_victoria.const import (
    CONF_ENTRY_TYPE,
    CONF_MODE,
    DOMAIN,
    ENTRY_TYPE_BOARD,
    ENTRY_TYPE_ROUTE_TRACKER,
    ENTRY_TYPE_WATCH,
    MODE_METRO,
)


class _Subentry:
    def __init__(self, data):
        self.data = data


class _Entry:
    def __init__(self, data, subentries=()):
        self.data = data
        self.subentries = {sid: _Subentry(sd) for sid, sd in subentries}


def _pin(route_type: int) -> dict:
    return PinData(
        pin_key=f"k{route_type}",
        route_type=route_type,
        route_id=f"R{route_type}",
        boarding_stop_id="S",
        boarding_stop_name="Stop",
    ).to_dict()


def _hass(*entries):
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda domain: list(entries))
    )


def test_board_bundles_for_entry_union():
    entry = _Entry(
        {CONF_ENTRY_TYPE: ENTRY_TYPE_BOARD},
        subentries=[("a", _pin(400)), ("b", _pin(3))],
    )
    # metro (400 → folder "2") + bus (3 → folders "4","11").
    assert set(_board_bundles_for_entry(entry)) == {"2", "4", "11"}


def test_bundles_for_all_entries_union_across_board_and_watch():
    board = _Entry(
        {CONF_ENTRY_TYPE: ENTRY_TYPE_BOARD},
        subentries=[("a", _pin(400))],
    )
    watch = _Entry(
        {CONF_ENTRY_TYPE: ENTRY_TYPE_WATCH, CONF_MODE: MODE_METRO},
    )
    tracker = _Entry({CONF_ENTRY_TYPE: ENTRY_TYPE_ROUTE_TRACKER})

    result = _bundles_for_all_entries(_hass(board, watch, tracker))
    # board metro pin → "2"; watch metro → "2" (deduped); tracker skipped.
    assert result == ["2"]


def test_bundles_for_all_entries_skips_trackers_and_empty():
    tracker = _Entry({CONF_ENTRY_TYPE: ENTRY_TYPE_ROUTE_TRACKER})
    assert _bundles_for_all_entries(_hass(tracker)) == []
