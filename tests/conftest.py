"""Session-scoped GTFS generation fixtures built from the offline catalogue.

Every fixture constructs a real generation DB by running the production
importer against an in-memory-built PTV ZIP; no SQLite binaries are committed
and no network is used.  Dates are pinned (see ``tests.gtfs_builder``), so the
DB contents and query results are deterministic.

Build cost is paid once per session (each bundle imports in a few ms).
"""

from __future__ import annotations

import pytest

from . import gtfs_fixtures as fx
from .gtfs_builder import build_generation, import_bundle


@pytest.fixture(scope="session")
def metro_db(tmp_path_factory: pytest.TempPathFactory):
    return import_bundle(tmp_path_factory.mktemp("metro"), **fx.metro_spec())


@pytest.fixture(scope="session")
def tram_db(tmp_path_factory: pytest.TempPathFactory):
    return import_bundle(tmp_path_factory.mktemp("tram"), **fx.tram_spec())


@pytest.fixture(scope="session")
def bus_db(tmp_path_factory: pytest.TempPathFactory):
    return import_bundle(tmp_path_factory.mktemp("bus"), **fx.bus_spec())


@pytest.fixture(scope="session")
def vline_db(tmp_path_factory: pytest.TempPathFactory):
    return import_bundle(tmp_path_factory.mktemp("vline"), **fx.vline_spec())


@pytest.fixture(scope="session")
def expired_db(tmp_path_factory: pytest.TempPathFactory):
    return import_bundle(
        tmp_path_factory.mktemp("expired"), **fx.expired_calendar_spec()
    )


@pytest.fixture(scope="session")
def extension_db(tmp_path_factory: pytest.TempPathFactory):
    return import_bundle(
        tmp_path_factory.mktemp("extension"), **fx.dates_only_extension_spec()
    )


@pytest.fixture(scope="session")
def removed_today_db(tmp_path_factory: pytest.TempPathFactory):
    return import_bundle(
        tmp_path_factory.mktemp("removed"), **fx.removed_today_spec()
    )


@pytest.fixture(scope="session")
def two_bundle_db(tmp_path_factory: pytest.TempPathFactory):
    return build_generation(
        tmp_path_factory.mktemp("two-bundle"), fx.two_bundle_specs()
    )
