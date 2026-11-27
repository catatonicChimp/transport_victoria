"""Unit tests for gtfs/store.py (generation store + atomic pointer + GC)."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

import pytest

from custom_components.transport_victoria.gtfs import store


@pytest.fixture
def anchor(tmp_path: Path) -> Path:
    return tmp_path / "gtfs.db"


def _write_generation(anchor: Path, name: str, age_seconds: float = 0.0) -> Path:
    gd = store.generations_dir(anchor)
    gd.mkdir(parents=True, exist_ok=True)
    p = gd / name
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    if age_seconds:
        mtime = time.time() - age_seconds
        import os
        os.utime(p, (mtime, mtime))
    return p


class TestPointer:
    def test_publish_and_read_roundtrip(self, anchor: Path):
        gen = store.new_generation_path(anchor)
        gen.write_bytes(b"x")
        ptr = store.publish_pointer(
            anchor, gen, sha="abc", horizon="20270101", written_at="2026-11-28T00:00:00+00:00"
        )
        assert ptr["generation"] == gen.name
        assert ptr["sha"] == "abc"

        read = store.read_pointer(anchor)
        assert read == ptr
        # Pointer is written atomically (tmp file gone).
        assert not store.pointer_path(anchor).with_name("current.json.tmp").exists()

    def test_read_pointer_missing(self, anchor: Path):
        assert store.read_pointer(anchor) is None

    def test_read_pointer_corrupt(self, anchor: Path):
        store.pointer_path(anchor).write_text("not json", encoding="utf-8")
        assert store.read_pointer(anchor) is None


class TestResolve:
    def test_resolve_prefers_pointer(self, anchor: Path):
        gen = _write_generation(anchor, "gtfs-new.db")
        store.publish_pointer(anchor, gen, sha="a", horizon="x", written_at="y")
        assert store.resolve_db(anchor) == gen

    def test_resolve_falls_back_to_newest_generation(self, anchor: Path):
        # Pointer missing but generations exist → newest by mtime.
        old = _write_generation(anchor, "gtfs-old.db", age_seconds=1000)
        new = _write_generation(anchor, "gtfs-new.db", age_seconds=0)
        assert store.resolve_db(anchor) == new
        assert store.resolve_db(anchor) != old

    def test_resolve_falls_back_to_legacy_flat_anchor(self, anchor: Path):
        anchor.write_bytes(b"x")
        assert store.resolve_db(anchor) == anchor

    def test_resolve_returns_anchor_when_nothing(self, anchor: Path):
        assert store.resolve_db(anchor) == anchor
        assert not store.resolve_db(anchor).exists()


class TestGc:
    def test_keeps_current_and_previous(self, anchor: Path):
        gen1 = _write_generation(anchor, "gtfs-1.db", age_seconds=100_000)
        gen2 = _write_generation(anchor, "gtfs-2.db", age_seconds=90_000)
        gen3 = _write_generation(anchor, "gtfs-3.db", age_seconds=0)
        store.publish_pointer(anchor, gen3, sha="a", horizon="x", written_at="y")

        store.gc_generations(anchor)

        # Newest two kept (gen3 current, gen2 previous); gen1 deleted (old).
        remaining = {p.name for p in store.generations_dir(anchor).glob("*.db")}
        assert gen3.name in remaining
        assert gen2.name in remaining
        assert gen1.name not in remaining

    def test_never_deletes_current_pointer_target(self, anchor: Path):
        gen = _write_generation(anchor, "gtfs-current.db", age_seconds=1_000_000)
        store.publish_pointer(anchor, gen, sha="a", horizon="x", written_at="y")
        store.gc_generations(anchor)
        assert gen.exists()

    def test_recent_generation_not_deleted_even_if_old_rank(self, anchor: Path):
        # A recent (young) generation is kept even if it's beyond keep_count.
        for i in range(3):
            _write_generation(anchor, f"gtfs-{i}.db", age_seconds=0)
        cur = _write_generation(anchor, "gtfs-cur.db", age_seconds=0)
        store.publish_pointer(anchor, cur, sha="a", horizon="x", written_at="y")
        store.gc_generations(anchor)
        # All files are young (< retention) so none should be deleted.
        remaining = {p.name for p in store.generations_dir(anchor).glob("*.db")}
        assert len(remaining) == 4


# ---------------------------------------------------------------------------
# Two-namespace discipline (in-progress builds never discoverable)
# ---------------------------------------------------------------------------

class TestBuildNamespace:
    def test_resolve_prefers_legacy_over_build_artifact(self, anchor: Path):
        # A partial in-build artifact + a valid legacy flat DB, no pointer.
        bd = store.build_dir(anchor)
        bd.mkdir(parents=True, exist_ok=True)
        (bd / "gtfs-partial.db").write_bytes(b"partial garbage")
        anchor.write_bytes(b"legacy")
        # Resolution must pick the legacy DB, never the build artifact.
        assert store.resolve_db(anchor) == anchor

    def test_resolve_ignores_in_progress_build(self, anchor: Path):
        # A finalized generation + an in-progress build artifact, no pointer.
        gen = _write_generation(anchor, "gtfs-final.db")
        bd = store.build_dir(anchor)
        bd.mkdir(parents=True, exist_ok=True)
        (bd / "gtfs-building.db").write_bytes(b"partial")
        assert store.resolve_db(anchor) == gen

    def test_crash_mid_build_then_publish_works(self, anchor: Path):
        # Simulate a crashed build artifact.
        bd = store.build_dir(anchor)
        bd.mkdir(parents=True, exist_ok=True)
        (bd / "gtfs-crashed.db").write_bytes(b"partial")
        # Nothing finalized, no legacy → resolution is None (nothing usable).
        assert store.resolve_generation(anchor) is None
        # A later successful import publishes and resolves.
        gen = store.new_generation_path(anchor)
        gen.write_bytes(b"final")
        store.publish_pointer(anchor, gen, sha="a", horizon="x", written_at="y")
        assert store.resolve_db(anchor) == gen

    def test_gc_cleans_stale_unregistered_build_artifact(self, anchor: Path):
        bd = store.build_dir(anchor)
        bd.mkdir(parents=True, exist_ok=True)
        stale = bd / "gtfs-crashed.db"
        stale.write_bytes(b"partial")
        # Age it past the build retention window (3 hours).
        old = time.time() - 3 * 3600
        import os
        os.utime(stale, (old, old))

        cur = _write_generation(anchor, "gtfs-cur.db", age_seconds=0)
        store.publish_pointer(anchor, cur, sha="a", horizon="x", written_at="y")
        store.gc_generations(anchor)
        assert not stale.exists()

    def test_gc_preserves_registered_active_build(self, anchor: Path):
        bd = store.build_dir(anchor)
        bd.mkdir(parents=True, exist_ok=True)
        active = bd / "gtfs-active.db"
        active.write_bytes(b"partial")
        # Age it past the retention window, but register it as active.
        old = time.time() - 3 * 3600
        import os
        os.utime(active, (old, old))
        store.register_active_build(active)

        cur = _write_generation(anchor, "gtfs-cur.db", age_seconds=0)
        store.publish_pointer(anchor, cur, sha="a", horizon="x", written_at="y")
        store.gc_generations(anchor)
        assert active.exists()  # never swept while registered

        store.unregister_active_build(active)

    def test_gc_preserves_young_build_artifact(self, anchor: Path):
        bd = store.build_dir(anchor)
        bd.mkdir(parents=True, exist_ok=True)
        young = bd / "gtfs-young.db"
        young.write_bytes(b"partial")  # current mtime → age ~0
        cur = _write_generation(anchor, "gtfs-cur.db", age_seconds=0)
        store.publish_pointer(anchor, cur, sha="a", horizon="x", written_at="y")
        store.gc_generations(anchor)
        assert young.exists()  # younger than retention window → kept

    def test_gc_never_touches_finalized_generations(self, anchor: Path):
        cur = _write_generation(anchor, "gtfs-cur.db", age_seconds=0)
        prev = _write_generation(anchor, "gtfs-prev.db", age_seconds=0)
        bd = store.build_dir(anchor)
        bd.mkdir(parents=True, exist_ok=True)
        stale = bd / "gtfs-stale.db"
        stale.write_bytes(b"partial")
        old = time.time() - 3 * 3600
        import os
        os.utime(stale, (old, old))

        store.publish_pointer(anchor, cur, sha="a", horizon="x", written_at="y")
        store.gc_generations(anchor)
        # Finalized generations are never touched by the build sweep.
        assert cur.exists()
        assert prev.exists()
        assert not stale.exists()

    def _make_build(self, anchor: Path, name: str, *, with_sidecars: bool = False, age: float = 0.0) -> Path:
        bd = store.build_dir(anchor)
        bd.mkdir(parents=True, exist_ok=True)
        main = bd / name
        main.write_bytes(b"partial")
        paths = [main]
        if with_sidecars:
            for suffix in ("-wal", "-shm", "-journal"):
                side = bd / (name + suffix)
                side.write_bytes(b"sidecar")
                paths.append(side)
        if age:
            old = time.time() - age
            import os
            for p in paths:
                os.utime(p, (old, old))
        return main

    def test_gc_skips_sidecars_of_registered_build(self, anchor: Path):
        main = self._make_build(anchor, "gtfs-active.db", with_sidecars=True, age=3 * 3600)
        store.register_active_build(main)

        cur = _write_generation(anchor, "gtfs-cur.db", age_seconds=0)
        store.publish_pointer(anchor, cur, sha="a", horizon="x", written_at="y")
        store.gc_generations(anchor)

        # Main + all sidecars survive (sidecars resolved to the registered base).
        bd = store.build_dir(anchor)
        assert main.exists()
        assert (bd / "gtfs-active.db-wal").exists()
        assert (bd / "gtfs-active.db-shm").exists()
        assert (bd / "gtfs-active.db-journal").exists()

        store.unregister_active_build(main)

    def test_gc_sweeps_sidecars_of_unregistered_stale_build(self, anchor: Path):
        self._make_build(anchor, "gtfs-stale.db", with_sidecars=True, age=3 * 3600)

        cur = _write_generation(anchor, "gtfs-cur.db", age_seconds=0)
        store.publish_pointer(anchor, cur, sha="a", horizon="x", written_at="y")
        store.gc_generations(anchor)

        # Main + sidecars all swept together.
        bd = store.build_dir(anchor)
        assert not (bd / "gtfs-stale.db").exists()
        assert not (bd / "gtfs-stale.db-wal").exists()
        assert not (bd / "gtfs-stale.db-shm").exists()
        assert not (bd / "gtfs-stale.db-journal").exists()


class TestPointerValidation:
    def test_corrupt_pointer_warning_deduped(self, anchor: Path, caplog):
        store.pointer_path(anchor).write_text("not json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert store.read_pointer(anchor) is None
            assert store.read_pointer(anchor) is None
        warnings = [r for r in caplog.records if "GTFS pointer" in r.message]
        assert len(warnings) == 1

    def test_pointer_with_path_separator_rejected(self, anchor: Path):
        store.pointer_path(anchor).write_text(
            json.dumps({"generation": "../../evil.db", "sha": "a"}),
            encoding="utf-8",
        )
        assert store.read_pointer(anchor) is None

    def test_pointer_with_absolute_path_rejected(self, anchor: Path):
        store.pointer_path(anchor).write_text(
            json.dumps({"generation": "/tmp/evil.db", "sha": "a"}),
            encoding="utf-8",
        )
        assert store.read_pointer(anchor) is None
