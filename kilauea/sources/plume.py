"""USGS ScienceBase — volcanic plume heights at the Kīlauea summit.

Grain: one row per plume-height observation.

Two tables ship in the release: 2008-2015 (inclination-angle measurements,
height above the vent) and April-August 2018 (camera-frame measurements, two
alternative height columns whose meaning depends on which is non-zero — the
release's own footnote, honoured below).
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
_FORMATS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y")


def _parse(value: str):
    value = (value or "").strip()
    for fmt in _FORMATS:
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=HST)
        except ValueError:
            continue
    return None


def _num(v):
    if v is None:
        return None
    v = str(v).strip().replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", v)
    return float(m.group()) if m else None


def collect(conn, *, item_id: str | None = None, **_) -> None:
    item_id = item_id or config.SB_PLUME_ITEM
    with db.Run(conn, "plume", "plume_height") as run:
        meta = sciencebase.item(item_id)
        key = f"sb:{item_id}"
        with db.tx(conn):
            db.register_dataset(conn, **sciencebase.dataset_record(item_id, meta))

        seen = 0
        now = db.utcnow()
        for frec in sciencebase.files(item_id, suffixes=(".csv",),
                                      max_bytes=config.SB_MAX_FILE_BYTES):
            if "/manager/" in frec["url"]:
                log.warning("plume: %s not publicly downloadable - skipped", frec["name"])
                continue
            path = cached_download(frec["url"], frec["name"], subdir=f"plume/{item_id}")
            text = path.read_text(encoding="utf-8", errors="replace")
            reader = csv.reader(io.StringIO(text))
            header = next(reader, None)
            if not header:
                continue
            low = [h.lower() for h in header]

            rows = []
            if any("above hmm" in h for h in low):
                # 2018 camera table. The release states: if 'above HMM' is 0 the
                # real height is the camera-frame value, and vice versa.
                i_hmm = next(i for i, h in enumerate(low) if "above hmm" in h)
                i_cam = next((i for i, h in enumerate(low) if "camera frame" in h), None)
                for rec in reader:
                    if not rec or len(rec) <= i_hmm:
                        continue
                    when = _parse(rec[0])
                    if when is None:
                        continue
                    hmm = _num(rec[i_hmm])
                    cam = _num(rec[i_cam]) if i_cam is not None and len(rec) > i_cam else None
                    if hmm:
                        height, ref = hmm, "above Halemaʻumaʻu rim"
                    elif cam:
                        height, ref = cam, "camera frame"
                    else:
                        continue
                    rows.append(dict(
                        time_utc=when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        time_ms=int(when.timestamp() * 1000),
                        height_m=height, height_ref=ref, method="webcam frame analysis",
                        dataset_key=key, source_file=frec["name"], retrieved_at=now,
                    ))
            else:
                i_h = next((i for i, h in enumerate(low) if "ht above vent" in h or "height" in h), None)
                if i_h is None:
                    log.warning("plume: no height column in %s (%s)", frec["name"], header)
                    continue
                for rec in reader:
                    if not rec or len(rec) <= i_h:
                        continue
                    when = _parse(rec[0])
                    height = _num(rec[i_h])
                    if when is None or height is None:
                        continue
                    rows.append(dict(
                        time_utc=when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        time_ms=int(when.timestamp() * 1000),
                        height_m=height, height_ref="above vent elevation",
                        method="inclination angle + range",
                        dataset_key=key, source_file=frec["name"], retrieved_at=now,
                    ))

            seen += len(rows)
            with db.tx(conn):
                db.upsert(conn, "plume_height", rows,
                          conflict=["time_utc", "height_m", "source_file"])
            log.info("plume: %s -> %d rows", frec["name"], len(rows))
        run.rows_seen = seen
