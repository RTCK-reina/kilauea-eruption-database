"""Nevada Geodetic Laboratory — daily GNSS positions for the Kīlauea summit.

Grain: one row per station per day per reference frame.

Why this source: it is the only continuous, three-component deformation record
available with a usable lag. The ScienceBase tiltmeter releases run about six
months behind, and the notice prose gives a single scalar per day. NGL publishes
a *final* solution (reprocessed, roughly six weeks behind) and a *rapid* one
(roughly ten days behind); both are loaded and ``solution`` distinguishes them,
so a rapid row can be superseded by the final row for the same day.

Positions are taken in the Pacific-plate-fixed frame, which removes plate motion
and leaves the volcanic signal. Use ``up_abs_m`` for inflation, never ``up_m``:
NGL reports a position as an integer reference plus an offset, and the reference
differs between the final and rapid series for the same station, so comparing
raw offsets across solution types produces metre-scale phantom jumps (CRIM shows
a 1,000 mm step at the final/rapid boundary in July 2026). UWEV sits 430 m from
the summit, co-located with the UWD tiltmeter, so the two measure the same
reservoir by different physics.

Note the host is HTTPS-only — port 80 is closed — and the archive path changed
to ``/gps_timeseries/IGS20/...``; the older ``/gps_timeseries/tenv3/IGS14/...``
URLs return 404.
"""
from __future__ import annotations

import datetime as dt
import logging

from .. import config, db
from ..http import get

log = logging.getLogger(__name__)

BASE = "https://geodesy.unr.edu/gps_timeseries/IGS20"

# Summit and near-summit stations, nearest first. UWEV is the summit station.
SUMMIT_STATIONS = ["UWEV", "BYRL", "CRIM", "OUTL", "AHUP"]

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _parse_date(token: str) -> dt.date | None:
    """'26JUL04' -> date(2026, 7, 4). NGL uses a two-digit year."""
    if len(token) != 7:
        return None
    try:
        year = int(token[:2])
        month = _MONTHS[token[2:5].upper()]
        day = int(token[5:])
    except (ValueError, KeyError):
        return None
    # The archive starts in the 1990s and no station predates 1980.
    year += 1900 if year >= 80 else 2000
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _f(parts, i):
    try:
        return float(parts[i])
    except (IndexError, ValueError):
        return None


def parse_tenv3(text: str, station: str, solution: str, frame: str,
                retrieved_at: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 23 or parts[0].lower() == "site":
            continue
        if parts[0].upper() != station.upper():
            continue
        day = _parse_date(parts[1])
        if day is None:
            continue
        e_ref, n_ref, u_ref = _f(parts, 7), _f(parts, 9), _f(parts, 11)
        e_off, n_off, u_off = _f(parts, 8), _f(parts, 10), _f(parts, 12)
        rows.append(dict(
            station=station.upper(),
            date_utc=day.isoformat(),
            date_ms=int(dt.datetime.combine(
                day, dt.time(), dt.timezone.utc).timestamp() * 1000),
            solution=solution,
            frame=frame,
            decimal_year=_f(parts, 2),
            mjd=int(parts[3]) if parts[3].isdigit() else None,
            east_ref_m=e_ref,
            north_ref_m=n_ref,
            up_ref_m=u_ref,
            east_m=e_off,
            north_m=n_off,
            up_m=u_off,
            east_abs_m=(e_ref + e_off) if None not in (e_ref, e_off) else None,
            north_abs_m=(n_ref + n_off) if None not in (n_ref, n_off) else None,
            up_abs_m=(u_ref + u_off) if None not in (u_ref, u_off) else None,
            sig_east_m=_f(parts, 14),
            sig_north_m=_f(parts, 15),
            sig_up_m=_f(parts, 16),
            latitude=_f(parts, 20),
            longitude=_f(parts, 21),
            height_m=_f(parts, 22),
            retrieved_at=retrieved_at,
        ))
    return rows


def collect(conn, *, stations: list[str] | None = None, frame: str = "PA", **_) -> None:
    stations = stations or SUMMIT_STATIONS
    suffix = f".{frame}" if frame != "IGS20" else ""
    with db.Run(conn, "gnss", "gnss_position") as run:
        now = db.utcnow()
        seen = 0
        for station in stations:
            for solution, path in (("final", f"{BASE}/tenv3/{frame}"),
                                   ("rapid", f"{BASE}/rapids/{frame}")):
                url = f"{path}/{station}{suffix}.tenv3"
                try:
                    text = get(url, timeout=180).text
                except Exception as exc:  # noqa: BLE001 - one station must not sink the run
                    log.warning("gnss: %s %s unavailable (%s)", station, solution, exc)
                    continue
                rows = parse_tenv3(text, station, solution, frame, now)
                if not rows:
                    log.warning("gnss: %s %s returned no parseable rows", station, solution)
                    continue
                # Final supersedes rapid for the same day: insert rapid without
                # overwriting, final with overwrite.
                with db.tx(conn):
                    db.upsert(conn, "gnss_position", rows,
                              conflict=["station", "date_utc", "frame"],
                              update=(solution == "final"))
                seen += len(rows)
                log.info("gnss: %s %s -> %d days (%s .. %s)", station, solution,
                         len(rows), rows[0]["date_utc"], rows[-1]["date_utc"])
        run.rows_seen = seen
        with db.tx(conn):
            db.register_dataset(
                conn,
                key="ngl:gnss:kilauea",
                title="Nevada Geodetic Laboratory daily GNSS positions, Kīlauea summit",
                publisher="Nevada Geodetic Laboratory, University of Nevada, Reno",
                url="https://geodesy.unr.edu/",
                license="Free for research use; cite Blewitt et al. (2018), Eos 99",
            )
