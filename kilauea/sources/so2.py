"""USGS ScienceBase — SO2 emission rates from Kīlauea.

Grain: one row per published measurement (``aggregation='individual'``) or per
published mean (``aggregation='daily_mean'`` / ``'traverse_mean'``).

Four releases cover 2008-2013, 2014-2017, 2018-2022 and 2023-2025. Only the
derived rate tables are ingested; each release also ships multi-gigabyte raw
spectra archives, which carry no additional forecasting information and are
skipped via ``SB_MAX_FILE_BYTES``.

The time zone differs between releases and is detected per file: the 2008-2017
tables are stamped in HST, the 2018+ tables in UTC. Assuming one or the other
would shift half the series by 10 hours, so the offset is read from the header.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re

from .. import config, db
from ..http import cached_download
from . import sciencebase

log = logging.getLogger(__name__)

HST = dt.timezone(dt.timedelta(hours=-10))

# name fragment -> (site, method, aggregation)
_FILE_RULES = [
    (r"summit.*flyspec.*dailyaverage", ("summit", "FLYSPEC array", "daily_mean")),
    (r"summit.*flyspec.*10s",          ("summit", "FLYSPEC array", "individual")),
    (r"summit.*srt.*rdtraverse",       ("summit", "road traverse (SRT)", "individual")),
    (r"summit.*dfw.*rdtraverse",       ("summit", "road traverse (DFW)", "individual")),
    (r"erz.*rdtraverse",               ("ERZ", "road traverse", "individual")),
    (r"kil_summit.*indiv",             ("summit", "DOAS traverse", "individual")),
    (r"kil_summit.*means",             ("summit", "DOAS traverse", "traverse_mean")),
    (r"kil_lerz.*indiv",               ("LERZ", "DOAS traverse", "individual")),
    (r"kil_lerz.*means",               ("LERZ", "DOAS traverse", "traverse_mean")),
    (r"kil_merz.*indiv",               ("MERZ", "DOAS traverse", "individual")),
    (r"kil_merz.*means",               ("MERZ", "DOAS traverse", "traverse_mean")),
    (r"kil_swrz.*indiv",               ("SWRZ", "DOAS traverse", "individual")),
    (r"kil_swrz.*means",               ("SWRZ", "DOAS traverse", "traverse_mean")),
]

_DATE_FORMATS = (
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
)


def classify(name: str):
    low = name.lower()
    for pattern, meta in _FILE_RULES:
        if re.search(pattern, low):
            return meta
    return None


def _num(v):
    if v is None:
        return None
    v = str(v).strip().replace(",", "")
    if not v or v.lower() in {"nan", "na", "n/a", "-"}:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", v)
    return float(m.group()) if m else None


def _parse_dt(value: str, tz: dt.timezone):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def _pick(header: list[str], *patterns: str) -> int | None:
    for i, h in enumerate(header):
        low = h.lower()
        if all(re.search(p, low) for p in patterns):
            return i
    return None


def _looks_like_csv(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return not head.startswith(("<!doctype", "<html", "<?xml"))


def collect(conn, *, items: list[str] | None = None, **_) -> None:
    items = items or config.SB_SO2_ITEMS
    with db.Run(conn, "so2", "so2_emission") as run:
        seen = 0
        for item_id in items:
            meta = sciencebase.item(item_id)
            key = f"sb:{item_id}"
            with db.tx(conn):
                db.register_dataset(conn, **sciencebase.dataset_record(item_id, meta))

            for frec in sciencebase.files(item_id, suffixes=(".csv",),
                                          max_bytes=config.SB_MAX_FILE_BYTES):
                rule = classify(frec["name"])
                if not rule:
                    continue
                site, method, aggregation = rule
                if "/manager/" in frec["url"]:
                    # S3-backed files on a not-yet-published item are served as
                    # the ScienceBase SPA shell, not the CSV. Skip loudly.
                    log.warning(
                        "so2: %s is not publicly downloadable yet (S3-backed, "
                        "item %s) - skipped", frec["name"], item_id)
                    continue

                path = cached_download(frec["url"], frec["name"], subdir=f"so2/{item_id}")
                text = path.read_text(encoding="utf-8", errors="replace")
                if not _looks_like_csv(text):
                    log.warning("so2: %s returned HTML, not CSV - skipped", frec["name"])
                    continue

                reader = csv.reader(io.StringIO(text))
                header = next(reader, None)
                if not header:
                    continue
                joined = " ".join(header).lower()
                tz = dt.timezone.utc if "utc" in joined else HST

                i_date = _pick(header, r"date")
                i_date = 0 if i_date is None else i_date
                i_rate = (
                    _pick(header, r"mean", r"so2", r"emission")
                    or _pick(header, r"average", r"so2")
                    or _pick(header, r"so2", r"emission", r"rate")
                    or _pick(header, r"so2")
                )
                if i_rate is None:
                    log.warning("so2: no rate column in %s (%s)", frec["name"], header)
                    continue
                i_err = (
                    _pick(header, r"stdev")
                    or _pick(header, r"standard deviation")
                    or _pick(header, r"error")
                )
                i_n = _pick(header, r"^n$") or _pick(header, r"number") or _pick(header, r"points")

                now = db.utcnow()
                rows = []
                for rec in reader:
                    if not rec or len(rec) <= i_rate:
                        continue
                    when = _parse_dt(rec[i_date], tz)
                    rate = _num(rec[i_rate])
                    if when is None or rate is None:
                        continue
                    n_meas = None
                    if i_n is not None and len(rec) > i_n:
                        raw_n = _num(rec[i_n])
                        n_meas = int(raw_n) if raw_n else None
                    rows.append(dict(
                        site=site, method=method, aggregation=aggregation,
                        time_utc=when.astimezone(dt.timezone.utc)
                                     .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        time_ms=int(when.timestamp() * 1000),
                        date_local=when.astimezone(HST).strftime("%Y-%m-%d"),
                        rate_tpd=rate,
                        uncertainty_tpd=(
                            _num(rec[i_err]) if i_err is not None and len(rec) > i_err else None
                        ),
                        n_measurements=n_meas,
                        dataset_key=key, source_file=frec["name"], retrieved_at=now,
                    ))
                seen += len(rows)
                with db.tx(conn):
                    db.upsert(conn, "so2_emission", rows,
                              conflict=["site", "method", "aggregation", "time_utc",
                                        "rate_tpd", "source_file"])
                log.info("so2: %s -> %d rows (%s, tz=%s)", frec["name"], len(rows), site, tz)
        run.rows_seen = seen
