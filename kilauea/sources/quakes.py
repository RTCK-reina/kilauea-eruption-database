"""USGS ComCat (FDSN event service) — seismicity around the Kīlauea summit.

Grain: one row per located earthquake within ``QUAKE_RADIUS_KM`` of the summit.
~209,000 events from 1959 to the present, dominated by the 2018 caldera-collapse
sequence (43,000 events in that year alone).

The FDSN service caps a single response at 20,000 events, so the request is
walked forward in adaptive time windows: a window that returns the cap is split
and retried, which keeps the walk correct across the 2018 swarm without
hard-coding it.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import time

from .. import config, db
from ..http import get

log = logging.getLogger(__name__)

MAX_EVENTS = 20000
EARTH_R_KM = 6371.0088


def _haversine(lat, lon) -> float:
    p1, p2 = math.radians(config.SUMMIT_LAT), math.radians(lat)
    dphi = p2 - p1
    dlam = math.radians(lon - config.SUMMIT_LON)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def _query(start: dt.datetime, end: dt.datetime, *, count_only=False):
    params = {
        "format": "geojson",
        "starttime": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "endtime": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "latitude": config.SUMMIT_LAT,
        "longitude": config.SUMMIT_LON,
        "maxradiuskm": config.QUAKE_RADIUS_KM,
        "orderby": "time-asc",
    }
    if count_only:
        # The count endpoint echoes whatever `format` asks for; text gives a
        # bare integer, geojson gives {"count": N, ...}.
        params = {**params, "format": "text"}
        return int(get(f"{config.FDSN_EVENT}/count", params=params, timeout=180).text.strip())
    return get(f"{config.FDSN_EVENT}/query", params=params, timeout=300).json()


def _windows(start: dt.datetime, end: dt.datetime, depth=0):
    """Yield (start, end) windows each holding at most MAX_EVENTS events."""
    n = _query(start, end, count_only=True)
    if n == 0:
        return
    if n < MAX_EVENTS or depth >= 12:
        if n >= MAX_EVENTS:
            log.warning(
                "quakes: window %s..%s still has %d events at max split depth; "
                "the response will be truncated by the service",
                start.date(), end.date(), n,
            )
        yield (start, end, n)
        return
    mid = start + (end - start) / 2
    yield from _windows(start, mid, depth + 1)
    yield from _windows(mid, end, depth + 1)


def collect(conn, *, since: str | None = None, sleep: float = 0.2, **_) -> None:
    with db.Run(conn, "quakes", "earthquake") as run:
        if since:
            start = dt.datetime.fromisoformat(since)
        else:
            row = conn.execute("SELECT MAX(time_ms) FROM earthquake").fetchone()[0]
            if row:
                # Re-fetch the last 7 days: ComCat revises magnitudes and
                # locations for days after an event, and adds late-reviewed ones.
                start = dt.datetime.utcfromtimestamp(row / 1000) - dt.timedelta(days=7)
            else:
                start = dt.datetime.fromisoformat(config.QUAKE_START)
        end = dt.datetime.utcnow() + dt.timedelta(days=1)
        log.info("quakes: %s .. %s (r=%.0f km)", start.date(), end.date(), config.QUAKE_RADIUS_KM)

        now = db.utcnow()
        total = 0
        for wstart, wend, expected in _windows(start, end):
            payload = _query(wstart, wend)
            feats = payload.get("features", [])
            rows = []
            for f in feats:
                p, g = f["properties"], f["geometry"]
                lon, lat, depth = (list(g["coordinates"]) + [None, None, None])[:3]
                t = p.get("time")
                if t is None or lat is None or lon is None:
                    continue
                tutc = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc)
                rows.append(
                    dict(
                        event_id=f["id"],
                        time_utc=tutc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                        time_ms=int(t),
                        latitude=lat,
                        longitude=lon,
                        depth_km=depth,
                        magnitude=p.get("mag"),
                        mag_type=p.get("magType"),
                        place=p.get("place"),
                        net=p.get("net"),
                        status=p.get("status"),
                        rms=p.get("rms"),
                        gap=p.get("gap"),
                        nst=p.get("nst"),
                        horizontal_error_km=p.get("horizontalError"),
                        depth_error_km=p.get("depthError"),
                        dist_from_summit_km=round(_haversine(lat, lon), 3),
                        retrieved_at=now,
                    )
                )
            with db.tx(conn):
                db.upsert(conn, "earthquake", rows, conflict=["event_id"])
            total += len(rows)
            log.info(
                "quakes: %s..%s  %d events (expected %d), running total %d",
                wstart.date(), wend.date(), len(rows), expected, total,
            )
            time.sleep(sleep)

        run.rows_seen = total
        with db.tx(conn):
            db.register_dataset(
                conn,
                key="usgs:comcat:earthquakes",
                title=f"ANSS ComCat earthquakes within {config.QUAKE_RADIUS_KM:.0f} km of Kīlauea summit",
                publisher="USGS Earthquake Hazards Program",
                url="https://earthquake.usgs.gov/fdsnws/event/1/",
                license="USGS public domain",
            )
