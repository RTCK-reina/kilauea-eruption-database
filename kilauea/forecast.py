"""Extract HVO's own published onset forecasts from HANS notice text.

HVO states an explicit window for the next fountaining episode in its daily
updates, e.g.

    "Preliminary data indicate the onset of the next fountaining episode is
     likely between August 21 and August 27."
    "The most likely time window for the start of episode 15 is between
     Sunday, March 23 and Monday, March 24 ..."

Those windows are scored here against the actual onsets in ``episode``. The
result is the benchmark any model of target A has to beat: a forecast that is
worse than HVO's published window is not useful, however good its cross-
validation score looks.

Only sentences that state two parseable calendar dates are captured. Vaguer
statements ("likely to start in the next 24 hours") are deliberately left out
rather than being converted into a window by guesswork.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from . import db

log = logging.getLogger(__name__)

HST = dt.timezone(dt.timedelta(hours=-10))

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
_MONTH_RE = "|".join(_MONTHS)

# A sentence that talks about an episode starting, and states a "between A and B"
# or "from A to B" date range.
# Intent words that mark a sentence as a forward-looking onset statement rather
# than a retrospective one.
_INTENT_RE = re.compile(
    r"\b(?:likely|expect|expected|anticipat\w*|forecast\w*|window|will begin|"
    r"will start|could start|most likely)\b", re.I)
_CONTEXT_RE = re.compile(r"\b(?:episode|eruption|fountain\w*)\b", re.I)
_RANGE_RE = re.compile(
    rf"\b(?:between|from)\s+(?:\w+day,?\s+)?(?P<m1>{_MONTH_RE})\s+(?P<d1>\d{{1,2}})"
    rf"(?:,?\s*(?P<y1>\d{{4}}))?\s*(?:and|to|through|-|\u2013)\s*"
    rf"(?:\w+day,?\s+)?(?:(?P<m2>{_MONTH_RE})\s+)?(?P<d2>\d{{1,2}})"
    rf"(?:,?\s*(?P<y2>\d{{4}}))?",
    re.I,
)
_EPISODE_RE = re.compile(r"\bepisode\s+(\d{1,3})\b", re.I)


def _date(month: int, day: int, year: int):
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _iso(d: dt.date, end_of_day: bool = False) -> str:
    t = dt.time(23, 59, 59) if end_of_day else dt.time(0, 0, 0)
    return (dt.datetime.combine(d, t, HST)
            .astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _parse_utc(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def extract(conn, *, since: str | None = None, **_) -> None:
    """Populate ``hvo_forecast`` from ``alert_notice`` and score it.

    ``since`` defaults to the start of the current episodic eruption; the CLI
    passes ``None`` when no bound was given, so the default is applied here
    rather than in the signature.
    """
    since = since or "2024-12-01"
    with db.Run(conn, "hvo_forecast", "hvo_forecast") as run:
        episodes = [
            (r["episode_no"], _parse_utc(r["start_utc"]))
            for r in conn.execute(
                "SELECT episode_no, start_utc FROM episode "
                "WHERE start_utc IS NOT NULL ORDER BY start_utc")
        ]

        notices = conn.execute(
            "SELECT notice_id, sent_utc, body_text FROM alert_notice "
            "WHERE sent_utc > ? AND body_text IS NOT NULL ORDER BY sent_utc",
            (since,),
        ).fetchall()

        rows, seen = [], 0
        for notice in notices:
            issued = _parse_utc(notice["sent_utc"])
            issued_local = issued.astimezone(HST).date()

            body = notice["body_text"]
            for rm in _RANGE_RE.finditer(body):
                # Sentence splitting on "." is unreliable here ("a.m.", "U.S.",
                # "Mr."), so the context is a fixed character window around the
                # date range instead.
                lo = max(0, rm.start() - 220)
                hi = min(len(body), rm.end() + 160)
                window = body[lo:hi]
                # Qualify on the untrimmed window: the intent word ("likely",
                # "expected") often sits in the sentence before the one carrying
                # the dates. Trimming first would drop those.
                context = " ".join(window.split())
                # Trim the fixed character window back to sentence boundaries so
                # the stored quote does not start or end mid-word. Periods
                # between digits (12.1) are not boundaries.
                head = list(re.finditer(r"(?<!\d)\.(?!\d)\s+", window[:rm.start() - lo]))
                if head:
                    window = window[head[-1].end():]
                    offset = head[-1].end()
                else:
                    offset = 0
                tail = re.search(r"(?<!\d)\.(?!\d)(?:\s|$)",
                                 window[rm.end() - lo - offset:])
                if tail:
                    window = window[:rm.end() - lo - offset + tail.end()]
                sentence = " ".join(window.split())
                if not (_INTENT_RE.search(context) and _CONTEXT_RE.search(context)):
                    continue
                seen += 1

                m1 = _MONTHS[rm.group("m1").lower()]
                m2 = _MONTHS[rm.group("m2").lower()] if rm.group("m2") else m1
                y1 = int(rm.group("y1")) if rm.group("y1") else issued_local.year
                y2 = int(rm.group("y2")) if rm.group("y2") else y1
                # A window that crosses New Year is stated without the year.
                if m1 < issued_local.month - 6:
                    y1 += 1
                if m2 < m1:
                    y2 = y1 + 1

                start = _date(m1, int(rm.group("d1")), y1)
                end = _date(m2, int(rm.group("d2")), y2)
                if not start or not end or end < start:
                    continue
                # A stated window longer than a month is almost certainly a
                # mis-parse of an unrelated sentence, not a forecast.
                if (end - start).days > 31:
                    continue
                # A forecast window has to extend past the issue time; a range
                # entirely in the past is a retrospective description.
                if end < issued_local:
                    continue

                ep_match = _EPISODE_RE.search(sentence)
                stated = int(ep_match.group(1)) if ep_match else None

                target_no = actual = None
                for no, onset in episodes:
                    if onset > issued:
                        target_no, actual = no, onset
                        break

                win_start = _parse_utc(_iso(start))
                win_end = _parse_utc(_iso(end, end_of_day=True))
                hit = err = lead = None
                if actual is not None:
                    lead = round((actual - issued).total_seconds() / 3600, 2)
                    if win_start <= actual <= win_end:
                        hit, err = 1, 0.0
                    else:
                        hit = 0
                        err = round(
                            ((actual - win_end) if actual > win_end
                             else (actual - win_start)).total_seconds() / 3600, 2)

                rows.append(dict(
                    notice_id=notice["notice_id"],
                    issued_utc=notice["sent_utc"],
                    sentence=sentence[:1000],
                    window_start_date=start.isoformat(),
                    window_end_date=end.isoformat(),
                    window_start_utc=_iso(start),
                    window_end_utc=_iso(end, end_of_day=True),
                    window_days=round((win_end - win_start).total_seconds() / 86400, 3),
                    stated_episode_no=stated,
                    target_episode_no=target_no,
                    actual_onset_utc=actual.strftime("%Y-%m-%dT%H:%M:%SZ") if actual else None,
                    lead_hours=lead,
                    hit=hit,
                    error_hours=err,
                ))

        run.rows_seen = seen
        with db.tx(conn):
            # Clear only the window being re-extracted. Deleting the whole table
            # would silently discard every row outside `since` on an incremental
            # run - the daily update passes a recent `since`.
            conn.execute("DELETE FROM hvo_forecast WHERE issued_utc >= ?", (since,))
            db.upsert(conn, "hvo_forecast", rows,
                      conflict=["notice_id", "window_start_date", "window_end_date"])
        log.info("hvo_forecast: %d windows extracted from %d notices",
                 len(rows), len(notices))
