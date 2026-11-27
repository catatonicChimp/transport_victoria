"""Root conftest.py — stubs out homeassistant for pure unit tests.

The importer, time_utils, repository, and rate_limiter modules only depend on
stdlib + the local custom_components package. They do NOT need the real HA
runtime. We inject minimal stubs so imports resolve without installing HA.

Each stub module uses __getattr__ to return a no-op sentinel for any name
that hasn't been explicitly set, so we don't have to enumerate every symbol
that gets imported at module level.

When the REAL ``homeassistant`` package is importable (the "classic" harness
venv used for the T8 subentry config-flow tests), the stubbing is skipped
entirely so pytest-homeassistant-custom-component drives a real runtime.
"""

from __future__ import annotations

import sys
import types

import voluptuous as vol


def _real_homeassistant() -> bool:
    """Return True when the real homeassistant package is importable."""
    try:
        import homeassistant  # noqa: F401
    except ImportError:
        return False
    return getattr(homeassistant, "__file__", None) is not None


if not _real_homeassistant():  # pragma: no cover - only reached in the light env

    class _StubMeta(type):
        """Metaclass so class-level attribute access (e.g. ``SensorDeviceClass.
        TIMESTAMP``, ``BinarySensorDeviceClass.PROBLEM``) also returns a sentinel."""

        def __getattr__(cls, name):
            return _Stub()


    class _Stub(metaclass=_StubMeta):
        """Universal stub: callable, iterable, usable as base class, returns itself."""
        def __init__(self, *a, **kw): pass
        def __call__(self, *a, **kw): return _Stub()
        def __getattr__(self, name): return _Stub()
        def __iter__(self): return iter([])
        def __class_getitem__(cls, item): return cls
        def __init_subclass__(cls, **kw): pass


    def _stub_module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__getattr__ = lambda attr: _Stub()  # type: ignore[method-assign]
        sys.modules[name] = mod
        return mod


    # Register all HA sub-packages we need to stub
    _HA_MODULES = [
        "homeassistant",
        "homeassistant.const",
        "homeassistant.components",
        "homeassistant.components.http",
        "homeassistant.components.binary_sensor",
        "homeassistant.components.sensor",
        "homeassistant.components.select",
        "homeassistant.components.device_tracker",
        "homeassistant.components.device_tracker.config_entry",
        "homeassistant.config_entries",
        "homeassistant.core",
        "homeassistant.exceptions",
        "homeassistant.helpers",
        "homeassistant.helpers.config_validation",
        "homeassistant.helpers.aiohttp_client",
        "homeassistant.helpers.device_registry",
        "homeassistant.helpers.entity_platform",
        "homeassistant.helpers.event",
        "homeassistant.helpers.selector",
        "homeassistant.helpers.update_coordinator",
        "homeassistant.util",
        "homeassistant.util.loop",
        "aiohttp",
        "voluptuous",
    ]

    for _mod_name in _HA_MODULES:
        _stub_module(_mod_name)

    # google.transit.gtfs_realtime_pb2 is a real TEST dependency
    # (gtfs-realtime-bindings, see requirements_test.txt). Protobuf fixtures are
    # committed as serialized bytes and decoded through the production client, so
    # the real classes must win over the sentinel stubs. Only fall back to stubs if
    # the dependency is absent (keeping the legacy, dependency-free unit tests
    # importable).
    try:
        from google.transit import gtfs_realtime_pb2 as _gtfs_rt_pb2  # noqa: F401
    except ImportError:
        for _mod_name in ("google", "google.transit", "google.transit.gtfs_realtime_pb2"):
            _stub_module(_mod_name)

    # Set concrete names that our code uses in isinstance / raise / inherit contexts

    _ce = sys.modules["homeassistant.config_entries"]
    _ce.ConfigEntry = _Stub  # type: ignore[attr-defined]
    _ce.ConfigFlow = _Stub  # type: ignore[attr-defined]
    _ce.ConfigFlowResult = _Stub  # type: ignore[attr-defined]
    _ce.OptionsFlow = _Stub  # type: ignore[attr-defined]
    _ce.ConfigSubentryFlow = _Stub  # type: ignore[attr-defined]

    _core = sys.modules["homeassistant.core"]
    _core.HomeAssistant = _Stub  # type: ignore[attr-defined]
    _core.callback = lambda fn: fn  # type: ignore[attr-defined]

    _exc = sys.modules["homeassistant.exceptions"]
    _exc.ConfigEntryAuthFailed = Exception  # type: ignore[attr-defined]
    _exc.ConfigEntryNotReady = Exception  # type: ignore[attr-defined]

    class _DataUpdateCoordinator:
        def __init__(self, *a, **kw): pass
        def __class_getitem__(cls, item): return cls

        async def async_config_entry_first_refresh(self):
            return None

    _coord = sys.modules["homeassistant.helpers.update_coordinator"]
    _coord.DataUpdateCoordinator = _DataUpdateCoordinator  # type: ignore[attr-defined]


    class _UpdateFailed(Exception):
        """Distinct exception type (not an alias of Exception) so tests can assert
        `pytest.raises(UpdateFailed)` and verify the exact type."""


    _coord.UpdateFailed = _UpdateFailed  # type: ignore[attr-defined]
    class _CoordinatorEntityBase:
        """Distinct base for CoordinatorEntity (must not alias TrackerEntity).

        Stores the coordinator (like the real CoordinatorEntity.__init__) so the
        light-env entity tests can instantiate board/watch entities directly.
        """

        @classmethod
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, coordinator=None, *args, **kwargs):
            self.coordinator = coordinator


    _coord.CoordinatorEntity = _CoordinatorEntityBase  # type: ignore[attr-defined]

    _aio = sys.modules["aiohttp"]
    _aio.ClientSession = _Stub  # type: ignore[attr-defined]
    _aio.ClientError = Exception  # type: ignore[attr-defined]
    _aio.ClientResponseError = Exception  # type: ignore[attr-defined]
    _aio.ClientTimeout = _Stub  # type: ignore[attr-defined]

    _vol = sys.modules["voluptuous"]
    _vol.Schema = _Stub  # type: ignore[attr-defined]
    _vol.Required = _Stub  # type: ignore[attr-defined]
    _vol.Optional = _Stub  # type: ignore[attr-defined]

    _cv = sys.modules["homeassistant.helpers.config_validation"]
    _cv.config_entry_only_config_schema = lambda _domain: vol.Schema({})  # type: ignore[attr-defined]

    _http = sys.modules["homeassistant.components.http"]
    _http.StaticPathConfig = _Stub  # type: ignore[attr-defined]

    _dt = sys.modules["homeassistant.util"]
    _dt.dt = types.SimpleNamespace(  # type: ignore[attr-defined]
        as_local=lambda x: x,
        utcnow=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        # now() returns a naive local datetime (as HA's dt_util.now() does).
        now=lambda: __import__("datetime").datetime.now(),
        # get_time_zone(name) → a real zoneinfo.ZoneInfo (as HA's dt_util does).
        get_time_zone=lambda name: __import__("zoneinfo").ZoneInfo(name or "UTC"),
    )

    _bs = sys.modules["homeassistant.components.binary_sensor"]
    _bs.BinarySensorDeviceClass = _Stub  # type: ignore[attr-defined]
    _bs.BinarySensorEntity = _Stub  # type: ignore[attr-defined]

    _se = sys.modules["homeassistant.components.sensor"]
    _se.SensorDeviceClass = _Stub  # type: ignore[attr-defined]
    _se.SensorEntity = _Stub  # type: ignore[attr-defined]

    _select = sys.modules["homeassistant.components.select"]
    _select.SelectEntity = _Stub  # type: ignore[attr-defined]

    _const = sys.modules["homeassistant.const"]
    _const.EntityCategory = _Stub  # type: ignore[attr-defined]

    _dt_reg = sys.modules["homeassistant.helpers.device_registry"]
    _dt_reg.DeviceInfo = _Stub  # type: ignore[attr-defined]

    _dt_tr = sys.modules["homeassistant.components.device_tracker"]
    _dt_tr.SourceType = types.SimpleNamespace(GPS=object())  # type: ignore[attr-defined]

    class _TrackerEntityBase:
        """Distinct from _Stub so multi-inheritance in device_tracker does not collapse."""

    _dt_tr_ce = sys.modules["homeassistant.components.device_tracker.config_entry"]
    _dt_tr_ce.TrackerEntity = _TrackerEntityBase  # type: ignore[attr-defined]

    _aio_client = sys.modules["homeassistant.helpers.aiohttp_client"]
    _aio_client.async_get_clientsession = lambda *a, **k: None  # type: ignore[attr-defined]

    _evt = sys.modules["homeassistant.helpers.event"]
    _evt.async_track_time_interval = lambda *a, **k: lambda: None  # type: ignore[attr-defined]
