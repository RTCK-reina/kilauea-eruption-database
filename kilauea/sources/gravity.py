"""USGS ScienceBase — continuous gravity at Kīlauea (HOVL, PUOC).

Grain: one row per station per hour. The release publishes raw 2 Hz gravimeter
output (~3.5 GB of zipped CSV); at that rate the useful signal for eruption
forecasting is the hourly trend, so each archive is decimated to hourly
statistics on ingest and the raw archive is not retained in the database.

Per the release's data dictionary, columns are:
  1 date/time (UTC, MM/DD/YYYY hh:mm:ss.ss)   2 gravity (mGal)
  3 long level (counts)                       4 cross level (counts)
  5 meter temperature (C)                     6 battery voltage (V)
The values are RAW: not corrected for solid Earth tides. Any use as a feature
must detide first — the tidal signal is roughly an order of magnitude larger
than the volcanic one.
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import math
import re
import zipfile

from .. import config, db
from ..http import cached_download
from . import sciencebase

log = logging.getLogger(__name__)

_ARCHIVE_RE = re.compile(r"^([A-Z]{4})_(\d{4})\.zip$", re.I)


_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _hour_key(line: str, cache: dict) -> int | None:
    """Map a raw timestamp field to its UTC hour index.

    Two stamp formats occur across the release, which is why this parses rather
    than slices at fixed offsets:
        2010-2016:  ``01/11/2016 07:59:58.403``      (MM/DD/YYYY, 4-digit year)
        2018:       ``04-30-18 11:00:00.087759``     (MM-DD-YY,   2-digit year)

    At 2 Hz a year-file holds tens of millions of rows, so the conversion is
    memoised on the 'date + hour' prefix - at most 8,760 distinct values a year.
    """
    space = line.find(" ")
    if space <= 0:
        return None
    colon = line.find(":", space)
    if colon < 0:
        return None
    prefix = line[:colon]
    if prefix in cache:
        return cache[prefix]

    try:
        date_part, hour_part = prefix.rsplit(" ", 1)
        sep = "/" if "/" in date_part else "-"
        mm, dd, yy = date_part.split(sep)
        year = int(yy)
        if year < 100:                      # two-digit year, 2018-era files
            year += 2000
        when = dt.datetime(year, int(mm), int(dd), int(hour_part),
                           tzinfo=dt.timezone.utc)
    except (ValueError, IndexError):
        cache[prefix] = None
        return None

    value = int((when - _EPOCH).total_seconds()) // 3600
    cache[prefix] = value
    return value


def collect(conn, *, item_id: str | None = None, keep_archives: bool = False, **_) -> None:
    item_id = item_id or config.SB_GRAVITY_ITEM
    with db.Run(conn, "gravity", "gravity_hourly") as run:
        meta = sciencebase.item(item_id)
        key = f"sb:{item_id}"
        with db.tx(conn):
            db.register_dataset(conn, **sciencebase.dataset_record(item_id, meta))

        seen = 0
        for frec in sciencebase.files(item_id, suffixes=(".zip",),
                                      max_bytes=config.SB_MAX_FILE_BYTES_GRAVITY):
            m = _ARCHIVE_RE.match(frec["name"])
            if not m:
                continue
            station, year = m.group(1).upper(), m.group(2)
            if "/manager/" in frec["url"]:
                log.warning("gravity: %s not publicly downloadable - skipped", frec["name"])
                continue

            path = cached_download(frec["url"], frec["name"], subdir=f"gravity/{item_id}")
            buckets: dict[int, list[float]] = {}
            hour_cache: dict[str, int | None] = {}
            n_read = 0
            with zipfile.ZipFile(path) as zf:
                members = [
                    n for n in zf.namelist()
                    if n.lower().endswith(".csv") and not n.startswith("__MACOSX")
                ]
                for member in members:
                    with zf.open(member) as fh:
                        for line in io.TextIOWrapper(fh, encoding="utf-8",
                                                     errors="replace"):
                            # Hot loop: slice out the two fields we need instead
                            # of running the csv reader over tens of millions of
                            # rows per archive.
                            first = line.find(",")
                            if first < 13:
                                continue
                            hr = _hour_key(line, hour_cache)
                            if hr is None:
                                continue
                            second = line.find(",", first + 1)
                            try:
                                g = float(line[first + 1:second if second > 0 else None])
                            except ValueError:
                                continue
                            if g != g:      # NaN
                                continue
                            b = buckets.get(hr)
                            if b is None:
                                b = buckets[hr] = []
                            b.append(g)
                            n_read += 1

            # The 2010-2012 dataloggers were not GPS-disciplined and the raw
            # files contain a handful of corrupt timestamps (stray 1980s and
            # 2050s stamps). Each archive covers exactly one year and says so in
            # its file name, so that is the bound applied - a stray stamp cannot
            # then widen the table's apparent coverage. One day of slack absorbs
            # the UTC edges.
            y = int(year)
            lo_h = int((dt.datetime(y, 1, 1, tzinfo=dt.timezone.utc)
                        - dt.timedelta(days=1)).timestamp()) // 3600
            hi_h = int((dt.datetime(y + 1, 1, 1, tzinfo=dt.timezone.utc)
                        + dt.timedelta(days=1)).timestamp()) // 3600
            bogus = [h for h in buckets if not (lo_h <= h <= hi_h)]
            if bogus:
                log.warning("gravity: %s %s dropping %d hour bucket(s) whose "
                            "timestamps fall outside %s", station, year,
                            len(bogus), year)
                for h in bogus:
                    del buckets[h]

            rows = []
            for hr, vals in sorted(buckets.items()):
                mean = sum(vals) / len(vals)
                std = (
                    math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))
                    if len(vals) > 1 else None
                )
                rows.append(dict(
                    station=station,
                    hour_utc=dt.datetime.fromtimestamp(hr * 3600, dt.timezone.utc)
                              .strftime("%Y-%m-%dT%H:00:00Z"),
                    hour_ms=hr * 3600000,
                    gravity_mean=mean, gravity_std=std,
                    gravity_min=min(vals), gravity_max=max(vals),
                    unit="mGal (raw, not tide-corrected)",
                    n_samples=len(vals), dataset_key=key,
                ))
            seen += n_read
            with db.tx(conn):
                db.upsert(conn, "gravity_hourly", rows, conflict=["station", "hour_ms"])
            if n_read == 0:
                log.error(
                    "gravity: %s %s produced no samples - the archive's timestamp "
                    "format is not recognised; fix _hour_key rather than ignoring "
                    "the gap", station, year)
            log.info("gravity: %s %s -> %d hours from %d samples",
                     station, year, len(rows), n_read)

            if not keep_archives:
                # These archives total several GB and carry no information the
                # hourly table does not, once ingested.
                path.unlink(missing_ok=True)
        run.rows_seen = seen
