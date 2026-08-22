"""VONA aviation notices, parsed from their fixed telex format.

Grain: one row per VONA message.

These already sit in ``alert_notice`` with ``notice_type_cd = 'VV'``, but their
body is ``KEY: VALUE`` lines rather than prose, so it parses exactly rather than
by keyword. The value is the ``ONSET`` field: a minute-precision UTC timestamp
for the start or end of fountaining, issued within minutes of the event. The
episode table's times are rounded and published in HST hours later.

No network: this reads notices already in the database.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from .. import db

log = logging.getLogger(__name__)

_FIELDS = {
    "notice_number": r"NOTICE\s*NR:\s*(.+)",
    "dtg": r"DTG:\s*(\S+)",
    "colour_code": r"CURRENT\s*COLOU?R\s*CODE:\s*(\S+)",
    "previous_colour": r"PREVIOUS\s*COLOU?R\s*CODE:\s*(\S+)",
    "activity_status": r"ACT\s*STS:\s*(.+)",
    "onset": r"ONSET:\s*(\S+)",
    "duration_text": r"DUR:\s*(.+)",
    "ash_cloud_height": r"VA\s*CLD\s*HGT:\s*(.+)",
    "cloud_movement": r"MOV:\s*(.+)",
}
_EPISODE_RE = re.compile(r"EPISODE\s+(\d{1,3})", re.I)


def _grab(rx: str, body: str) -> str | None:
    m = re.search(rx, body, re.I)
    return " ".join(m.group(1).split()) if m else None


def _dtg(value: str | None) -> dt.datetime | None:
    """Parse '20260813/1126Z' -> aware UTC datetime."""
    if not value:
        return None
    m = re.match(r"(\d{8})/(\d{4})Z?", value.strip())
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M").replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _iso(d: dt.datetime | None) -> str | None:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def collect(conn, *, since: str | None = None, **_) -> None:
    since = since or "2000-01-01"
    with db.Run(conn, "vona", "vona") as run:
        notices = conn.execute(
            """SELECT notice_id, sent_utc, body_text FROM alert_notice
               WHERE notice_type_cd = 'VV' AND sent_utc >= ? AND body_text IS NOT NULL
               ORDER BY sent_utc""", (since,)).fetchall()
        now = db.utcnow()
        rows = []
        for n in notices:
            body = n["body_text"]
            if "VONA" not in body.upper():
                continue
            vals = {k: _grab(rx, body) for k, rx in _FIELDS.items()}
            onset = _dtg(vals["onset"])
            sent = dt.datetime.strptime(n["sent_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc)
            remarks = _grab(r"RMK:\s*(.+?)(?:\n[A-Z ]{3,}:|\Z)", body) or _grab(r"RMK:\s*(.+)", body)
            ep = _EPISODE_RE.search(remarks or "")
            rows.append(dict(
                notice_id=n["notice_id"],
                sent_utc=n["sent_utc"],
                sent_ms=int(sent.timestamp() * 1000),
                notice_number=vals["notice_number"],
                dtg_utc=_iso(_dtg(vals["dtg"])),
                colour_code=vals["colour_code"],
                previous_colour=vals["previous_colour"],
                activity_status=vals["activity_status"],
                onset_utc=_iso(onset),
                onset_ms=int(onset.timestamp() * 1000) if onset else None,
                duration_text=vals["duration_text"],
                ash_cloud_height=vals["ash_cloud_height"],
                cloud_movement=vals["cloud_movement"],
                remarks=(remarks or "")[:1500] or None,
                episode_no=int(ep.group(1)) if ep else None,
                retrieved_at=now,
            ))
        run.rows_seen = len(rows)
        with db.tx(conn):
            db.upsert(conn, "vona", rows, conflict=["notice_id"])
        with_onset = sum(1 for r in rows if r["onset_utc"])
        with_ep = sum(1 for r in rows if r["episode_no"])
        log.info("vona: %d messages, %d with an onset timestamp, %d naming an episode",
                 len(rows), with_onset, with_ep)
