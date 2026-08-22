"""Smithsonian Global Volcanism Program — Holocene eruption catalogue.

Source: GeoServer WFS behind volcano.si.edu (Volcanoes of the World v4).
Grain:  one row per GVP eruption record for Kīlauea (VNUM 332010).

This is the long-baseline catalogue: ~75 records from 4650 BCE to the present,
with VEI. It is the only table that supports the long-term eruption-probability
target; it is far too coarse for the short-term episode target.
"""
from __future__ import annotations

import datetime as dt
import logging

from .. import config, db
from ..http import get

log = logging.getLogger(__name__)

LAYER = "GVP-VOTW:Smithsonian_VOTW_Holocene_Eruptions"


def _iso(year, month, day) -> tuple[str | None, str]:
    """Return (ISO date or None, precision label).

    GVP encodes "unknown" as 0 for month/day. Years can be negative (BCE),
    which ISO-8601 cannot express portably, so pre-year-1 records get a NULL
    date and keep their numeric fields.
    """
    if year is None:
        return None, "unknown"
    if not month:
        return None, "year"
    if not day:
        return None, "month"
    if year < 1:
        return None, "day"
    try:
        return dt.date(int(year), int(month), int(day)).isoformat(), "day"
    except ValueError:
        # e.g. February 30 in a legacy record — keep the parts, drop the date.
        log.warning("GVP: invalid date %s-%s-%s", year, month, day)
        return None, "invalid"


def collect(conn, **_) -> None:
    with db.Run(conn, "gvp", "eruption") as run:
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeName": LAYER,
            "outputFormat": "application/json",
            "CQL_FILTER": f"Volcano_Number={config.VNUM}",
        }
        payload = get(config.GVP_WFS, params=params, timeout=180).json()
        feats = payload.get("features", [])
        run.rows_seen = len(feats)

        now = db.utcnow()
        rows = []
        for f in feats:
            p = f["properties"]
            s_date, s_prec = _iso(p.get("StartDateYear"), p.get("StartDateMonth"), p.get("StartDateDay"))
            e_date, e_prec = _iso(p.get("EndDateYear"), p.get("EndDateMonth"), p.get("EndDateDay"))
            duration = None
            if s_date and e_date:
                duration = (dt.date.fromisoformat(e_date) - dt.date.fromisoformat(s_date)).days

            rows.append(
                dict(
                    eruption_number=p.get("Eruption_Number"),
                    vnum=config.VNUM,
                    activity_type=p.get("Activity_Type"),
                    vei=p.get("ExplosivityIndexMax"),
                    vei_modifier=p.get("ExplosivityIndexModifier"),
                    activity_area=p.get("ActivityArea"),
                    activity_unit=p.get("ActivityUnit"),
                    evidence_method=p.get("StartEvidenceMethod"),
                    start_year=p.get("StartDateYear"),
                    start_month=p.get("StartDateMonth") or None,
                    start_day=p.get("StartDateDay") or None,
                    start_year_modifier=p.get("StartDateYearModifier"),
                    start_year_uncert=p.get("StartDateYearUncertainty"),
                    start_day_uncert=p.get("StartDateDayUncertainty"),
                    start_date=s_date,
                    start_precision=s_prec,
                    end_year=p.get("EndDateYear"),
                    end_month=p.get("EndDateMonth") or None,
                    end_day=p.get("EndDateDay") or None,
                    end_year_modifier=p.get("EndDateYearModifier"),
                    end_date=e_date,
                    end_precision=e_prec,
                    duration_days=duration,
                    source="GVP",
                    retrieved_at=now,
                )
            )

        with db.tx(conn):
            db.upsert(conn, "eruption", rows, conflict=["eruption_number"])
            db.register_dataset(
                conn,
                key="gvp:votw:holocene_eruptions",
                title="Volcanoes of the World — Holocene Eruptions (Kīlauea)",
                publisher="Smithsonian Institution, Global Volcanism Program",
                url="https://volcano.si.edu/volcano.cfm?vn=332010",
                license="GVP terms of use — cite Global Volcanism Program",
            )
