# Test suites — two-environment reality

This package has tests in **two** environments because the subentry config-flow
API depends on real Home Assistant (2026.1+ `ConfigSubentryFlow`).

## 1. Light env (fast, pure-logic + stubbed HA)

Uses the repo venv (Python 3.13: pytest, pytest-asyncio, voluptuous,
gtfs-realtime-bindings only). The root `conftest.py` stubs out `homeassistant`
so pure-logic modules import without HA.

```bash
ln -sfn /Users/ataylor/devel/tv/.venv .venv
.venv/bin/python -m pytest tests -q          # 243 passed, 1 skipped
```

This runs every test except the subentry flow test (which is skipped here — it
needs the real HA runtime).

## 2. Classic harness env (real HA, subentry config-flow)

Uses the classic harness venv (homeassistant 2026.2.3 +
pytest-homeassistant-custom-component). The root `conftest.py` detects the real
package and skips its stubbing, so pytest-homeassistant-custom-component drives
a real runtime.

```bash
ln -sfn /Users/ataylor/devel/ptv-p12-harness/.venv .venv-classic
.venv-classic/bin/python -m pytest \
    tests/custom_components/transport_victoria/test_board_subentry_flow.py -q
```

Only the subentry flow tests are meaningful under this venv; the rest of the
suite is exercised by the light env. The subentry tests build an offline bus
fixture via `tests.gtfs_builder` (no network), point the flow's DB anchor at it,
and drive `hass.config_entries.subentries` / `hass.config_entries.options`
directly.
