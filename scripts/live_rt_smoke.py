#!/usr/bin/env python3
"""Live GTFS-Realtime endpoint smoke test (MANUAL — never run in CI).

Calls every DISTINCT published GTFS-RT endpoint ONCE through the production
``PTVRealtimeClient`` (which enforces the per-feed + global rate limits and the
``regional-bus`` → ``bus`` URL-mode resolution) and prints one line per endpoint:

    endpoint → HTTP status → decode ok → header age   (note)

The API key is read from ``/Users/ataylor/devel/ptv/.secrets/tv_portal.env``
(``TV_RT_KEY``).  It is never printed.  If the env file or the key is missing
the script exits 0 with a clear message.

Run from a venv that has ``aiohttp`` and ``gtfs-realtime-bindings`` installed,
e.g.::

    /Users/ataylor/devel/ptv-p12-harness/.venv/bin/python \
        scripts/live_rt_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

# The 10 DISTINCT realtime (URL-mode, feed_type) endpoints the portal serves
# (verified via CKAN + live probes, 2026-11-27).  Regional buses ride the
# combined "bus" RT feed — ``rt_url_mode("regional-bus") == "bus"`` — so there is
# NO separate ``regional-bus`` / ``rural-bus`` / ``coach`` endpoint (those URL
# segments return 404).  Trams have no vehicle-positions and V/Line has no
# service-alerts (known-missing 404s, not probed here).
ENDPOINTS: list[tuple[str, str]] = [
    ("metro", "trip-updates"),
    ("metro", "service-alerts"),
    ("metro", "vehicle-positions"),
    ("tram", "trip-updates"),
    ("tram", "service-alerts"),
    ("bus", "trip-updates"),
    # 403 with the maintainer's key: the "Bus Service Alerts" portal product is
    # not enabled on the key (My Account → Products).  Known access-limited.
    ("bus", "service-alerts"),
    ("bus", "vehicle-positions"),
    ("vline", "trip-updates"),
    ("vline", "vehicle-positions"),
]

ENV_PATH = Path("/Users/ataylor/devel/ptv/.secrets/tv_portal.env")
ENV_KEY = "TV_RT_KEY"


def read_key() -> str | None:
    """Read TV_RT_KEY from the env file, or None when it is absent."""
    if not ENV_PATH.is_file():
        return None
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == ENV_KEY:
            key = value.strip().strip('"').strip("'")
            return key or None
    return None


def _header_age(meta) -> str:
    """Human-readable age of the feed's header timestamp (or 'n/a')."""
    ts = meta.header_timestamp
    if ts is None:
        return "n/a"
    age = datetime.now(tz=timezone.utc) - ts
    return f"{int(age.total_seconds())}s"


async def run() -> int:
    import aiohttp  # deferred so a bare import never hard-fails

    from custom_components.transport_victoria.realtime.client import (
        FeedError,
        PTVRealtimeClient,
    )

    key = read_key()
    if key is None:
        print(
            f"TV_RT_KEY not found in {ENV_PATH} — set it and re-run. "
            "Exiting without any requests.",
            file=sys.stderr,
        )
        return 0

    async with aiohttp.ClientSession() as session:
        client = PTVRealtimeClient(key, session)

        print(f"{'endpoint':28} {'status':>18}  decode  header-age   note")
        print("-" * 92)

        for mode, feed_type in ENDPOINTS:
            label = f"{mode}/{feed_type}"
            status = "ok"
            decode_ok = "no"
            age = "n/a"
            note = ""
            try:
                feed, meta = await client.fetch_feed_with_meta(mode, feed_type)
                decode_ok = "yes"
                age = _header_age(meta)
            except FeedError as exc:
                status = str(exc.status)
                if getattr(exc, "unavailable", False):
                    if exc.status == 403:
                        status = "403 (access-limited)"
                        note = "portal product not enabled for this key"
                    else:
                        status = "404 (not published)"
            except aiohttp.ClientError as exc:
                status = f"net:{type(exc).__name__}"
            except Exception as exc:  # noqa: BLE001 - per-endpoint isolation
                status = f"err:{type(exc).__name__}"

            print(f"{label:28} {status:>18}  {decode_ok:7} {age:11} {note}")

    print("-" * 92)
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except ImportError as exc:
        print(
            f"Missing dependency ({exc.name or exc}) — run from a venv with "
            "aiohttp and gtfs-realtime-bindings installed.",
            file=sys.stderr,
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
