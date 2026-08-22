"""USGS ScienceBase — Kīlauea borehole tiltmeter time series.

Grain: ``tilt_sample`` holds one row per station per native sample (1 minute);
``tilt_hourly`` holds the derived per-station hourly aggregate used by the
feature views.

Important physical caveat, encoded in the ``segment`` column: HVO re-levels
these instruments periodically, and each releveling resets the absolute tilt
datum. Absolute values are therefore only comparable WITHIN a segment (one
source CSV). Any feature built on tilt must use differences inside a segment,
never a difference that straddles a segment boundary. ``tilt_hourly.n_segments``
flags the hours where a boundary falls.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import math
import re
import zipfile
from collections import defaultdict

from .. import config, db
from ..http import cached_download
from . import sciencebase

log = logging.getLogger(__name__)

_SENSOR_RE = re.compile(r"^([A-Z]{3})_(analog|digital)\.zip$", re.I)
_HEADER_ALIASES = {
    "utcdatetime": "time",
    "datetime": "time",
    "date_time": "time",
    "time": "time",
    "xtilt(microrad)": "x",
    "ytilt(microrad)": "y",
    "easttilt(microrad)": "east",
    "northtilt(microrad)": "north",
    "holetemp(celsius)": "hole_temp",
    "boxtemp(celsius)": "box_temp",
    "voltage(v)": "voltage",
}


def _norm(h: str) -> str:
    return _HEADER_ALIASES.get(re.sub(r"\s+", "", h).lower().lstrip("﻿"), "")


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def _parse_time(s: str):
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def _read_csv(fh, station: str, segment: str, dataset_key: str):
    reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"))
    header = next(reader, None)
    if not header:
        return
    idx = {}
    for i, h in enumerate(header):
        key = _norm(h)
        if key:
            idx[key] = i
    if "time" not in idx:
        log.warning("tilt: %s has no recognisable time column (%s)", segment, header[:4])
        return

    def cell(row, key):
        i = idx.get(key)
        return row[i] if i is not None and i < len(row) else None

    for row in reader:
        if not row:
            continue
        t = _parse_time(cell(row, "time") or "")
        if t is None:
            continue
        yield dict(
            station=station,
            time_utc=t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            time_ms=int(t.timestamp() * 1000),
            x_urad=_f(cell(row, "x")),
            y_urad=_f(cell(row, "y")),
            east_urad=_f(cell(row, "east")),
            north_urad=_f(cell(row, "north")),
            hole_temp_c=_f(cell(row, "hole_temp")),
            box_temp_c=_f(cell(row, "box_temp")),
            voltage_v=_f(cell(row, "voltage")),
            segment=segment,
            dataset_key=dataset_key,
        )


def _station_meta(zf: zipfile.ZipFile, station: str, sensor: str) -> dict:
    """Parse the per-station README shipped inside each zip."""
    meta = dict(code=station, sensor_type=sensor.lower())
    for name in zf.namelist():
        if name.startswith("__MACOSX"):
            continue
        if name.lower().endswith("readme.txt"):
            txt = zf.read(name).decode("utf-8", "replace")
            def grab(label):
                m = re.search(rf"^{label}:\s*(.+)$", txt, re.I | re.M)
                return m.group(1).strip() if m else None
            meta["name"] = grab("Name")
            lat, lon = grab("Latitude"), grab("Longitude")
            meta["latitude"] = _f(lat)
            meta["longitude"] = _f(lon)
            meta["instrument"] = grab("Tiltmeter")
            depth = grab("Instrument Depth")
            meta["depth_m"] = _f((depth or "").split()[0]) if depth else None
            meta["notes"] = txt.strip()[:4000]
            break
    return meta


# The 1 Hz files land with a ``segment`` ending in ``_1sec``. The underscore
# is a LIKE wildcard, so it has to be escaped -- and the backslash has to
# survive Python's own escaping, hence the raw string. Written as a plain
# string this collapses to ``ESCAPE ''`` and SQLite rejects the statement.
_STALE_1SEC_WHERE = r"segment LIKE '%\_1sec%' ESCAPE '\'"


def collect(conn, *, items: list[str] | None = None, include_1sec: bool = False, **_) -> None:
    """Ingest tiltmeter releases.

    ``include_1sec``: the 2018 release ships both a 1-minute series and a 1 Hz
    series covering the same window (~570 MB of CSV, ~15 million extra rows).
    The 1 Hz files are redundant for the hourly features and are skipped unless
    this is set.
    """
    items = items or config.SB_TILT_ITEMS
    with db.Run(conn, "tilt", "tilt_sample") as run:
        if not include_1sec:
            # Keep the table consistent with the requested configuration even if
            # an earlier run (or an interrupted one) ingested the 1 Hz files.
            stale = conn.execute(
                "SELECT COUNT(*) FROM tilt_sample WHERE " + _STALE_1SEC_WHERE
            ).fetchone()[0]
            if stale:
                log.warning("tilt: removing %d 1 Hz samples left by a previous run "
                            "(include_1sec is off)", stale)
                with db.tx(conn):
                    conn.execute(
                        "DELETE FROM tilt_sample WHERE " + _STALE_1SEC_WHERE)
        seen = 0
        for item_id in items:
            meta = sciencebase.item(item_id)
            key = f"sb:{item_id}"
            with db.tx(conn):
                db.register_dataset(conn, **sciencebase.dataset_record(item_id, meta))
            log.info("tilt: %s — %s", item_id, (meta.get("title") or "")[:80])

            for frec in sciencebase.files(
                item_id, suffixes=(".zip",), max_bytes=config.SB_MAX_FILE_BYTES
            ):
                m = _SENSOR_RE.match(frec["name"])
                if not m:
                    continue
                station, sensor = m.group(1).upper(), m.group(2)
                path = cached_download(frec["url"], frec["name"], subdir=f"tilt/{item_id}")

                with zipfile.ZipFile(path) as zf:
                    with db.tx(conn):
                        db.upsert(conn, "tilt_station", [_station_meta(zf, station, sensor)],
                                  conflict=["code"])

                    members = [
                        n for n in zf.namelist()
                        if n.lower().endswith(".csv") and not n.startswith("__MACOSX")
                    ]
                    if not include_1sec:
                        highrate = [n for n in members if "_1sec" in n.lower()]
                        if highrate:
                            log.info(
                                "tilt: %s skipping %d 1 Hz file(s) covering the same "
                                "window as the 1-minute series (pass --tilt-1sec to "
                                "ingest them): %s",
                                station, len(highrate),
                                ", ".join(n.rsplit("/", 1)[-1] for n in highrate),
                            )
                        members = [n for n in members if "_1sec" not in n.lower()]
                    for member in members:
                        segment = member.rsplit("/", 1)[-1][:-4]
                        buf: list[dict] = []
                        with zf.open(member) as fh:
                            for row in _read_csv(fh, station, segment, key):
                                buf.append(row)
                                if len(buf) >= 50000:
                                    seen += len(buf)
                                    with db.tx(conn):
                                        db.upsert(conn, "tilt_sample", buf,
                                                  conflict=["station", "time_ms"])
                                    buf = []
                        if buf:
                            seen += len(buf)
                            with db.tx(conn):
                                db.upsert(conn, "tilt_sample", buf,
                                          conflict=["station", "time_ms"])
                    log.info("tilt: %s %s — %d files, %d samples so far",
                             item_id[:8], station, len(members), seen)
        run.rows_seen = seen
    rebuild_hourly(conn)


def rebuild_hourly(conn) -> None:
    """Recompute ``tilt_hourly`` from ``tilt_sample``.

    Done in Python rather than pure SQL because SQLite has no built-in stddev,
    and because the per-hour distinct segment count is what flags the hours in
    which a releveling makes the absolute tilt datum jump.

    Reads on a separate connection so the streaming cursor is not disturbed by
    the write transactions it feeds.
    """
    log.info("tilt: rebuilding hourly aggregates")
    reader = db.connect()
    try:
        with db.Run(conn, "tilt_hourly", "tilt_hourly") as run:
            cur = reader.execute(
                """SELECT station, time_ms/3600000 AS hr, east_urad, north_urad, segment
                   FROM tilt_sample ORDER BY station, hr"""
            )
            rows: list[dict] = []
            key = None
            east: list[float] = []
            north: list[float] = []
            segs: set[str] = set()
            count = 0
            seen = 0

            def emit():
                if key is None:
                    return
                station, hr = key
                rows.append(dict(
                    station=station,
                    hour_utc=dt.datetime.fromtimestamp(hr * 3600, dt.timezone.utc)
                              .strftime("%Y-%m-%dT%H:00:00Z"),
                    hour_ms=hr * 3600000,
                    east_mean=_mean(east), north_mean=_mean(north),
                    east_min=min(east) if east else None,
                    east_max=max(east) if east else None,
                    north_min=min(north) if north else None,
                    north_max=max(north) if north else None,
                    east_std=_std(east), north_std=_std(north),
                    n_samples=count, n_segments=len(segs),
                ))

            for station, hr, e, n_, segment in cur:
                if key != (station, hr):
                    emit()
                    if len(rows) >= 20000:
                        with db.tx(conn):
                            db.upsert(conn, "tilt_hourly", rows, conflict=["station", "hour_ms"])
                        rows = []
                    key = (station, hr)
                    east, north, segs, count = [], [], set(), 0
                if e is not None:
                    east.append(e)
                if n_ is not None:
                    north.append(n_)
                if segment:
                    segs.add(segment)
                count += 1
                seen += 1

            emit()
            if rows:
                with db.tx(conn):
                    db.upsert(conn, "tilt_hourly", rows, conflict=["station", "hour_ms"])
            run.rows_seen = seen
    finally:
        reader.close()


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _std(xs):
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
