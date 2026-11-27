"""Immutable GTFS schedule generations + atomic pointer store.

Why generations instead of overwriting one ``gtfs.db`` in place:

  SQLite keeps ``-wal`` / ``-shm`` sidecar files next to a database while it is
  in WAL mode.  If we built a fresh database and ``os.replace``'d it over the
  live ``gtfs.db``, any live connection (or sidecars left by an abnormal
  shutdown) could apply *old* WAL pages against the *new* database file and
  silently corrupt it.  Swapping the main ``.db`` is therefore unsafe.

Instead we never overwrite a database file:

  * Each successful import builds into a NON-discoverable scratch path under a
    ``generations/build/`` subdirectory, then — after full validation,
    ``PRAGMA journal_mode = DELETE`` + close (no sidecars) — is atomically
    ``os.rename``'d into its final, uniquely-named *generation* name in the
    ``generations/`` directory (e.g. ``gtfs-<uuid>.db``).  ``resolve_generation``
    and ``gc_generations`` only ever consider finalized generation files (i.e.
    files that have been renamed into the top-level ``generations/`` dir), never
    in-progress builds or crashed-build artifacts.

  * A tiny pointer file (``current.json``) names the active generation.  It is
    updated atomically (write ``current.json.tmp`` then ``os.replace``), so the
    pointer is never half-written — a crash mid-publish leaves either the old
    pointer (old generation, still on disk) or the new one (new generation,
    already closed).

  * Readers resolve the pointer at every connection open (see
    :func:`resolve_generation`).  A reader that already opened a generation
    keeps its own file handle on that immutable file, so a concurrent refresh
    can publish a new generation without disturbing in-flight queries.

  * Old generations are garbage-collected: we always keep the most recent two
    (current + previous) and delete anything else older than a retention
    threshold.  The 24h age threshold is only a heuristic for "no reader still
    has it open" — on POSIX, unlinking a file an open reader still holds is
    safe (the reader keeps the inode), so the heuristic is conservative rather
    than load-bearing.  The file named by the current pointer is never deleted.

Pointer resolution order (documented): (1) the generation named by a valid
``current.json``; (2) the newest finalized generation by mtime (pointer missing
or corrupt — "last known good"); (3) a legacy flat ``<anchor>`` file from a
pre-generations install.

The public functions here are all synchronous and must be called from an
executor thread.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

from ..const import (
    GTFS_BUILD_RETENTION_SECONDS,
    GTFS_GENERATION_KEEP_COUNT,
    GTFS_GENERATION_RETENTION_SECONDS,
    GTFS_GENERATIONS_DIR_NAME,
    GTFS_POINTER_NAME,
)

_LOGGER = logging.getLogger(__name__)

# Pointer paths we've already warned about (deduped so a persistently corrupt
# pointer doesn't spam the log on every resolve). Cleared when a valid pointer
# is read, so a *future* corruption is logged again.
_warned_pointers: set[str] = set()

# Pointer→missing-generation warnings, deduped (like malformed pointers) so a
# pointer that persistently names a since-deleted generation logs once.
_warned_missing_generations: set[str] = set()

# Process-global registry of build paths that are ACTIVELY being written by the
# importer. The GC sweep skips any registered path regardless of its mtime age.
_active_builds: set[str] = set()


def register_active_build(path: Path) -> None:
    """Mark a build path as actively in-progress (skipped by GC)."""
    _active_builds.add(str(path))


def unregister_active_build(path: Path) -> None:
    """Mark a build path as no longer in progress (idempotent)."""
    _active_builds.discard(str(path))


# SQLite sidecar suffixes that accompany a build file while it is being written.
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _build_base_path(path: Path) -> Path:
    """Return the main build path for a file (strips a sidecar suffix, if any).

    A sidecar ``<build>.db-wal`` maps back to ``<build>.db`` so the GC sweep can
    treat the sidecars of a registered active build as active too.
    """
    name = path.name
    for suffix in _SIDECAR_SUFFIXES:
        if name.endswith(suffix):
            return path.with_name(name[: -len(suffix)])
    return path


def _warn_pointer(path: Path, reason: str) -> None:
    key = str(path)
    if key in _warned_pointers:
        return
    _warned_pointers.add(key)
    _LOGGER.warning("GTFS pointer %s is %s — ignoring", path, reason)


def generations_dir(anchor: Path) -> Path:
    """Return the generations subdirectory for the given DB anchor path."""
    return anchor.parent / GTFS_GENERATIONS_DIR_NAME


def build_dir(anchor: Path) -> Path:
    """Return the non-discoverable in-progress build subdirectory."""
    return generations_dir(anchor) / "build"


def pointer_path(anchor: Path) -> Path:
    """Return the pointer file path for the given DB anchor path."""
    return anchor.parent / GTFS_POINTER_NAME


def read_pointer(anchor: Path) -> dict | None:
    """Return the parsed pointer dict, or None if missing/corrupt/invalid.

    Validates the JSON shape: the ``generation`` value must be a non-empty
    basename (no path separators) so a malicious/corrupt pointer can't name a
    file outside the generations directory. Malformed pointers are logged once
    (deduped by path) and treated as missing.
    """
    p = pointer_path(anchor)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        _warn_pointer(p, "unreadable JSON")
        return None

    if not isinstance(data, dict):
        _warn_pointer(p, "not a JSON object")
        return None

    gen = data.get("generation")
    if (
        not isinstance(gen, str)
        or not gen
        or gen != Path(gen).name
        or "/" in gen
        or "\\" in gen
        or gen in (".", "..")
    ):
        _warn_pointer(p, "invalid generation filename")
        return None

    # Pointer is valid again — reset the warning dedupe so a future corruption
    # is logged afresh.
    _warned_pointers.discard(str(p))
    return data


def _finalized_generations(anchor: Path) -> list[Path]:
    """Return finalized generation files (top-level *.db), newest first.

    In-progress builds live under ``generations/build/`` and are never returned.
    """
    gd = generations_dir(anchor)
    if not gd.is_dir():
        return []
    return sorted(
        (f for f in gd.glob("*.db") if f.is_file()),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )


def resolve_generation(anchor: Path) -> Path | None:
    """Resolve the anchor to the current generation file, or None.

    Order of preference:
      1. the generation named by a valid ``current.json`` (if it exists);
      2. the newest *finalized* generation file by mtime (pointer missing/corrupt
         — "last known good" fallback);
      3. a legacy flat ``<anchor>`` file (pre-generations installs).
    """
    ptr = read_pointer(anchor)
    if ptr is not None:
        gen = generations_dir(anchor) / ptr["generation"]
        key = str(pointer_path(anchor)) + ":" + ptr["generation"]
        if gen.is_file():
            # Generation is present again — reset the dedupe so a FUTURE
            # disappearance is logged afresh (mirrors _warned_pointers).
            _warned_missing_generations.discard(key)
            return gen
        # Deduped like malformed-pointer warnings so a persistently missing
        # generation doesn't spam the log on every resolve.
        if key not in _warned_missing_generations:
            _warned_missing_generations.add(key)
            _LOGGER.warning(
                "GTFS pointer names %s but it is missing — falling back",
                ptr["generation"],
            )

    for f in _finalized_generations(anchor):
        return f

    if anchor.exists():
        return anchor

    return None


def resolve_db(anchor: Path) -> Path:
    """Resolve the anchor to a concrete DB path (generation or the anchor itself).

    Never returns None: if nothing resolves, the anchor path is returned so
    callers can treat it as "missing" the same way they would a plain path.
    """
    return resolve_generation(anchor) or anchor


def new_generation_path(anchor: Path) -> Path:
    """Return the FINAL (discoverable) generation path for a new import.

    The file at this path only comes into existence after ``finalize_generation``
    renames a finished build into it.
    """
    gd = generations_dir(anchor)
    gd.mkdir(parents=True, exist_ok=True)
    return gd / f"gtfs-{uuid.uuid4().hex}.db"


def new_build_path(anchor: Path) -> Path:
    """Return a NON-discoverable scratch path for an in-progress import.

    Lives under ``generations/build/`` so ``resolve_generation`` and
    ``gc_generations`` never see it (they only glob the top-level ``*.db``).
    """
    bd = build_dir(anchor)
    bd.mkdir(parents=True, exist_ok=True)
    return bd / f"gtfs-{uuid.uuid4().hex}.db"


def finalize_generation(build_path: Path, final_path: Path) -> None:
    """Rename a fully-built generation from its build path to its final name.

    This rename is what makes the generation discoverable; until it happens the
    file is invisible to readers/GC. Both paths are on the same filesystem, so
    ``os.rename`` is atomic.
    """
    os.rename(build_path, final_path)


def publish_pointer(
    anchor: Path,
    generation: Path,
    *,
    sha: str,
    horizon: str,
    written_at: str,
) -> dict:
    """Atomically publish the pointer naming ``generation``.

    Writes ``current.json.tmp`` then ``os.replace``'s it over ``current.json``
    (atomic on the same filesystem). Returns the pointer dict written.
    """
    ptr = {
        "generation": generation.name,
        "sha": sha,
        "horizon": horizon,
        "written_at": written_at,
    }
    p = pointer_path(anchor)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(ptr, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)
    return ptr


def gc_generations(
    anchor: Path,
    *,
    keep_count: int = GTFS_GENERATION_KEEP_COUNT,
    retention_seconds: int = GTFS_GENERATION_RETENTION_SECONDS,
    build_retention_seconds: int = GTFS_BUILD_RETENTION_SECONDS,
) -> None:
    """Delete stale generation files and leftover build artifacts.

    Keeps the newest ``keep_count`` generations (by mtime) and any generation
    newer than ``retention_seconds``; deletes the rest.  The generation named by
    the current pointer is never deleted (it is always among the newest, but we
    exclude it explicitly for safety).

    Sweeps the ``build/`` subdirectory, but only deletes a build artifact that is
    (a) NOT registered as active (:func:`register_active_build`) AND (b) older
    than ``build_retention_seconds``. Active builds (imports are minutes-scale)
    are thus never deleted mid-import, including from callers that bypass the
    public guard. Residual race (accepted): an *abandoned* build older than the
    retention window could in theory be swept while a non-guarded caller still
    works on it — all in-tree callers are guarded or sub-minute.
    """
    gd = generations_dir(anchor)
    if not gd.is_dir():
        return

    current = read_pointer(anchor)
    current_name = current["generation"] if current else None

    files = _finalized_generations(anchor)

    now = time.time()
    for idx, f in enumerate(files):
        if f.name == current_name:
            continue
        if idx < keep_count:
            continue
        try:
            age = now - f.stat().st_mtime
        except OSError:
            age = 0.0
        if age >= retention_seconds:
            try:
                f.unlink()
                _LOGGER.debug("GC removed old GTFS generation %s", f.name)
            except OSError as exc:
                _LOGGER.warning("GC could not remove %s: %s", f.name, exc)

    # Clean abandoned (unregistered + stale) build artifacts.
    bd = build_dir(anchor)
    if bd.is_dir():
        for f in sorted(bd.glob("*")):
            if not f.is_file():
                continue
            # A sidecar (<build>-wal/-shm/-journal) of a REGISTERED active build is
            # itself active — skip by resolving the base build path.
            if str(f) in _active_builds or str(_build_base_path(f)) in _active_builds:
                continue  # actively being written — never sweep
            try:
                age = now - f.stat().st_mtime
            except OSError:
                age = 0.0
            if age >= build_retention_seconds:
                try:
                    f.unlink()
                    _LOGGER.debug("GC removed abandoned build %s", f.name)
                except OSError as exc:
                    _LOGGER.warning(
                        "GC could not remove build artifact %s: %s", f.name, exc
                    )
