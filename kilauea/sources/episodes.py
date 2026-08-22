"""USGS HVO — episodic fountaining table for the ongoing summit eruption.

Source: https://www.usgs.gov/volcanoes/kilauea/science/eruption-information
Grain:  one row per numbered fountaining episode (episode 1 = 2024-12-23).

USGS publishes the table in HST (Hawaii Standard Time, UTC-10 year-round; the
state does not observe DST), so the conversion is a fixed offset. Both the
verbatim HST string and the derived UTC timestamp are stored: if USGS revises a
time, the diff stays visible.

USGS labels these values "unreviewed, preliminary estimates ... not suitable for
research, quantitative analyses, or scientific publication". They are the best
public record of episode timing, but any model trained on them inherits that
caveat.
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import re

from .. import config, db
from ..http import get

log = logging.getLogger(__name__)

HST = dt.timezone(dt.timedelta(hours=-10))

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], 1)
}

_EPISODE_HEADER = "Episode Number"
_HAZARD_HEADER = "Fountain Height above vent level"


# --- HTML -> rows -------------------------------------------------------------

def _tables(page: str) -> list[list[list[str]]]:
    out = []
    for tab in re.findall(r"<table.*?</table>", page, re.S):
        rows = []
        for tr in re.findall(r"<tr.*?</tr>", tab, re.S):
            cells = [
                html.unescape(re.sub(r"<[^>]+>", " ", c)).replace("\xa0", " ")
                for c in re.findall(r"<t[dh].*?</t[dh]>", tr, re.S)
            ]
            rows.append([re.sub(r"\s+", " ", c).strip() for c in cells])
        if rows:
            out.append(rows)
    return out


# --- field parsers ------------------------------------------------------------

def parse_hst(text: str, *, default_year: int | None = None):
    """Parse 'December 23, 2024 - 2:20 a.m.' -> (datetime UTC, exact_flag).

    Handles the published variants:
      'December 23, 2024 - 4 p.m.'   (no minutes)
      'July 9 - 1:20 p.m.'           (year omitted, inherited from the episode)
      'December 23, 2024'            (no time at all -> midnight, exact=0)
    Returns (None, 0) when nothing parseable is present (e.g. 'TBD').
    """
    if not text:
        return None, 0
    t = text.strip()
    if t.upper() in {"TBD", "N/A", "-", "—", ""}:
        return None, 0

    m = re.search(
        r"(?P<mon>[A-Za-z]+)\s+(?P<day>\d{1,2})(?:,\s*(?P<year>\d{4}))?"
        r"(?:\s*[-–]\s*(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*"
        r"(?P<mer>[ap]\.?\s?m\.?))?",
        t,
        re.I,
    )
    if not m:
        return None, 0
    mon = _MONTHS.get(m.group("mon").lower())
    if not mon:
        return None, 0
    year = int(m.group("year")) if m.group("year") else default_year
    if year is None:
        return None, 0

    exact = 1
    hour, minute = 0, 0
    if m.group("hour"):
        hour = int(m.group("hour")) % 12
        minute = int(m.group("min") or 0)
        if m.group("mer").lower().replace(".", "").replace(" ", "").startswith("p"):
            hour += 12
    else:
        exact = 0

    try:
        local = dt.datetime(year, mon, int(m.group("day")), hour, minute, tzinfo=HST)
    except ValueError:
        log.warning("episodes: unparseable date %r", text)
        return None, 0
    return local.astimezone(dt.timezone.utc), exact


def parse_duration_hours(text: str) -> float | None:
    """'14 hours' / '8.5 days' / '35.5 hours' / 'TBD' -> hours."""
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(hour|hr|day|minute|min)", text, re.I)
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2).lower()
    if unit.startswith("day"):
        return val * 24
    if unit.startswith("min"):
        return val / 60
    return val


def parse_number(text: str) -> float | None:
    """'400 (may update)' -> 400.0 ; '' -> None."""
    if not text:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(m.group()) if m else None


def _iso(d: dt.datetime | None) -> str | None:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


# --- collector ----------------------------------------------------------------

def collect(conn, **_) -> None:
    with db.Run(conn, "episodes", "episode") as run:
        page = get(config.USGS_EPISODES_URL, timeout=180).text
        tables = _tables(page)

        ep_tables = [t for t in tables if t and _EPISODE_HEADER in " ".join(t[0])]
        hz_tables = [t for t in tables if t and _HAZARD_HEADER in " ".join(t[0])]
        if not ep_tables:
            raise RuntimeError(
                "episode table not found on the USGS page — the layout changed; "
                "collector needs updating rather than silently returning zero rows"
            )

        now = db.utcnow()
        episodes: dict[int, dict] = {}

        for tab in ep_tables:  # duplicated tables (mobile/desktop) merge by PK
            for cells in tab[1:]:
                if len(cells) < 7 or not cells[0].strip().isdigit():
                    continue
                no = int(cells[0])
                start_dt, start_exact = parse_hst(cells[1])
                year = start_dt.astimezone(HST).year if start_dt else None
                pause_dt, pause_exact = parse_hst(cells[2], default_year=year)

                notes = cells[7] if len(cells) > 7 else ""
                pre_text = pre_dt = None
                # "began on April 16, 2025 - 10:01 p.m." — the trailing "p.m."
                # contains periods, so the capture must be anchored on the
                # date-time shape rather than terminated at the first ".".
                pm = re.search(
                    r"began on\s+([A-Za-z]+\s+\d{1,2}(?:,\s*\d{4})?"
                    r"(?:\s*[-\u2013]\s*\d{1,2}(?::\d{2})?\s*[ap]\.?\s?m\.?)?)",
                    notes, re.I,
                )
                if pm:
                    pre_text = pm.group(1).strip()
                    pre_dt, _ = parse_hst(pre_text, default_year=year)

                dur_calc = (
                    (pause_dt - start_dt).total_seconds() / 3600
                    if start_dt and pause_dt else None
                )

                episodes[no] = dict(
                    episode_no=no,
                    start_hst_text=cells[1] or None,
                    pause_hst_text=cells[2] or None,
                    start_utc=_iso(start_dt),
                    pause_utc=_iso(pause_dt),
                    start_time_is_exact=start_exact,
                    pause_time_is_exact=pause_exact,
                    duration_text=cells[3] or None,
                    duration_hours=parse_duration_hours(cells[3]),
                    duration_hours_calc=round(dur_calc, 3) if dur_calc is not None else None,
                    repose_text=cells[4] or None,
                    repose_hours=parse_duration_hours(cells[4]),
                    repose_hours_calc=None,          # filled in the second pass
                    fountain_height_m=parse_number(cells[5]),
                    fountain_height_text=cells[5] or None,
                    volume_mcm=parse_number(cells[6]),
                    precursor_hst_text=pre_text,
                    precursor_utc=_iso(pre_dt),
                    precursor_lead_hours=(
                        round((start_dt - pre_dt).total_seconds() / 3600, 3)
                        if start_dt and pre_dt else None
                    ),
                    is_ongoing=1 if pause_dt is None else 0,
                    notes=notes or None,
                    retrieved_at=now,
                )

        # Second pass: measured repose = next episode's start minus this pause.
        ordered = sorted(episodes)
        for a, b in zip(ordered, ordered[1:]):
            cur, nxt = episodes[a], episodes[b]
            if cur["pause_utc"] and nxt["start_utc"]:
                p = dt.datetime.strptime(cur["pause_utc"], "%Y-%m-%dT%H:%M:%SZ")
                s = dt.datetime.strptime(nxt["start_utc"], "%Y-%m-%dT%H:%M:%SZ")
                cur["repose_hours_calc"] = round((s - p).total_seconds() / 3600, 3)

        rows = [episodes[k] for k in ordered]
        run.rows_seen = len(rows)

        hazards = []
        for tab in hz_tables:
            for cells in tab[1:]:
                if len(cells) < 6 or not cells[0].strip().split("-")[0].strip().isdigit():
                    continue
                hazards.append(
                    dict(
                        episode_no=int(cells[0].split("-")[0].strip()),
                        date_text=cells[1] or None,
                        fountain_height_text=cells[2] or None,
                        fountain_height_m=_height_metres(cells[2]),
                        wind_conditions=cells[3] or None,
                        plume_height_text=cells[4] or None,
                        impacts=cells[5] or None,
                        retrieved_at=now,
                    )
                )

        with db.tx(conn):
            db.upsert(conn, "episode", rows, conflict=["episode_no"])
            if hazards:
                db.upsert(conn, "episode_hazard", hazards, conflict=["episode_no", "date_text"])
            db.register_dataset(
                conn,
                key="usgs:hvo:episode_table",
                title="Kīlauea summit eruption — episode chronology",
                publisher="USGS Hawaiian Volcano Observatory",
                url=config.USGS_EPISODES_URL,
                license=(
                    "USGS public domain. Values are unreviewed preliminary "
                    "estimates and subject to revision."
                ),
            )


def _height_metres(text: str) -> float | None:
    """'1070 feet/325 meters' -> 325.0 ; '330 feet/100 meters' -> 100.0."""
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*met", text, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:feet|ft)", text, re.I)
    return round(float(m.group(1)) * 0.3048, 1) if m else None
