"""Summit tilt mined from the prose of HVO daily updates.

Why this exists: the ScienceBase tiltmeter releases lag by roughly six months,
so the episodes that matter most for forecasting have no deformation data
attached. HVO states summit tilt numerically in almost every daily update
("The Uēkahuna tiltmeter (UWD) recorded about 16.5 microradians of deflationary
tilt during episode 37"). Mining those sentences yields about one reading per
station per day, current to within a day, across the whole episodic eruption.

Grain: one row per (notice, station, kind, value).

These are HVO's rounded and hedged figures, not instrument output. The
``qualifier`` column preserves the hedging so a model cannot silently treat
"about 16" as a measurement. They belong in ``tilt_reading``, never in
``tilt_sample``.

This collector needs no network: it reads ``alert_notice`` rows already in the
database.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from .. import db
from ..brief import _sentences, split_sections

log = logging.getLogger(__name__)

HST = dt.timezone(dt.timedelta(hours=-10))

# Station names HVO writes out, mapped to the code used in the tilt tables.
_NAME_TO_CODE = {
    "uēkahuna": "UWD", "uekahuna": "UWD",
    "sandhill": "SDH", "sand hill": "SDH",
    "summer camp": "SMC",
}
# Three-letter tokens that appear in these sentences but are not stations.
_NOT_A_STATION = {"HVO", "USGS", "HST", "UTC", "SO2", "GPS", "NWS", "NPS",
                  "MPH", "DOI", "FAA", "CO2", "H2O"}

_NUM_RE = re.compile(
    r"(?P<qual>about|approximately|nearly|roughly|more than|at least|over|almost)?\s*"
    r"(?P<num>\d+(?:\.\d+)?)\s*(?:µrad|microradians?)", re.I)

_DEFLATION_RE = re.compile(r"deflat", re.I)
_INFLATION_RE = re.compile(r"inflat|recover|reinflat|re-inflat|gained", re.I)
_DURING_RE = re.compile(r"during (?:the |this )?(?:episode|fountain|eruptive)|"
                        r"during episode\s*\d+|lost during|tilt loss during", re.I)
_SINCE_RE = re.compile(r"since (?:the end of|the episode ended|episode|fountaining ended|"
                       r"then|yesterday|sunday|monday|tuesday|wednesday|thursday|friday|"
                       r"saturday)|has recovered|total recovery|has re-?inflated|"
                       r"has inflated|reinflating|recovered", re.I)
_24H_RE = re.compile(r"(?:last|past)\s*(?:24\s*hours|day)|over yesterday|since yesterday|"
                     r"in the past day|overnight", re.I)
_RATE_RE = re.compile(r"per (?:day|hour)|/\s*day|a day|per 24", re.I)
_EPISODE_RE = re.compile(r"episode\s+(\d{1,3})", re.I)


def _nearest(rx: re.Pattern, sentence: str, pos: int) -> int | None:
    """Distance from ``pos`` to the closest match of ``rx``, or None."""
    best = None
    for m in rx.finditer(sentence):
        d = 0 if m.start() <= pos <= m.end() else min(abs(m.start() - pos),
                                                      abs(m.end() - pos))
        if best is None or d < best:
            best = d
    return best


def _station_positions(sentence: str) -> list[tuple[int, str]]:
    """Character offsets of every station reference, resolved to a code."""
    found: list[tuple[int, str]] = []
    # "(UWD)", "UWD tiltmeter", "tiltmeter UWD", and "tiltmeter at SMC" all occur.
    for m in re.finditer(r"\(([A-Z]{3})\)|\b([A-Z]{3})\s+tiltmeter|"
                         r"[Tt]iltmeter\s+(?:at\s+|near\s+)?([A-Z]{3})\b", sentence):
        code = m.group(1) or m.group(2) or m.group(3)
        if code and code not in _NOT_A_STATION:
            found.append((m.start(), code))
    for name, code in _NAME_TO_CODE.items():
        for m in re.finditer(re.escape(name), sentence, re.I):
            found.append((m.start(), code))
    found.sort()
    # Collapse "Uēkahuna tiltmeter (UWD)" into one reference.
    out: list[tuple[int, str]] = []
    for pos, code in found:
        if out and out[-1][1] == code and pos - out[-1][0] < 40:
            continue
        out.append((pos, code))
    return out


def _attribute(stations: list[tuple[int, str]], pos: int) -> str | None:
    before = [c for p, c in stations if p <= pos]
    if before:
        return before[-1]
    return stations[0][1] if stations else None


def _classify(sentence: str, start: int, end: int) -> str | None:
    """Decide what a given µrad figure measures, from the nearest keywords.

    A single HVO sentence routinely carries two figures with opposite meanings -
    "about 11 microradians of deflationary tilt during episode 8 and about 1.3
    microradians of inflationary tilt since the end of episode 8" - so asking
    whether a keyword appears *anywhere nearby* mislabels the second figure.
    Direction and framing are therefore both decided by whichever keyword sits
    closest to the number.
    """
    mid = (start + end) // 2

    # A rate ("less than 1 microradian per day") is not a cumulative amount.
    tail = sentence[end:end + 40]
    if _RATE_RE.search(tail):
        return "rate_per_day"

    d_def = _nearest(_DEFLATION_RE, sentence, mid)
    d_inf = _nearest(_INFLATION_RE, sentence, mid)
    if d_def is None and d_inf is None:
        return None
    deflationary = d_inf is None or (d_def is not None and d_def <= d_inf)

    d_during = _nearest(_DURING_RE, sentence, mid)
    d_since = _nearest(_SINCE_RE, sentence, mid)
    d_24h = _nearest(_24H_RE, sentence, mid)

    # The 24-hour qualifier binds tightly; only accept it when it is genuinely
    # the closest framing and within reach of the number.
    if d_24h is not None and d_24h <= 90 and \
       (d_during is None or d_24h < d_during) and (d_since is None or d_24h < d_since):
        return "change_24h"

    if deflationary:
        during_wins = d_during is not None and (d_since is None or d_during <= d_since)
        return "deflation_episode" if during_wins else "deflation_excursion"
    return "inflation_cumulative"


def extract(notice_id: str, sent_utc: str, body: str) -> list[dict]:
    sections = split_sections(body)
    scope = " ".join(filter(None, [
        sections.get("Summit Observations"),
        sections.get("Overview"),
        sections.get("Analysis"),
        sections.get("Summary"),
    ]))
    when = dt.datetime.strptime(sent_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)

    # Notice-level fallback: HVO often names the instrument once and then says
    # "this instrument" or drops the name entirely. If the whole notice mentions
    # exactly one station, an unattributed figure belongs to it.
    notice_stations = {code for _, code in _station_positions(scope)}
    default_station = next(iter(notice_stations)) if len(notice_stations) == 1 else None

    rows: list[dict] = []
    for sentence in _sentences(scope):
        if "microradian" not in sentence.lower() and "µrad" not in sentence:
            continue
        stations = _station_positions(sentence)
        ep = _EPISODE_RE.search(sentence)
        for m in _NUM_RE.finditer(sentence):
            kind = _classify(sentence, m.start("num"), m.end("num"))
            if kind is None:
                continue
            mag = float(m.group("num"))
            if mag <= 0 or mag > 200:      # guard against page furniture
                continue
            sign = -1.0 if kind.startswith("deflation") else 1.0
            rows.append(dict(
                notice_id=notice_id,
                observed_utc=sent_utc,
                observed_hst=when.astimezone(HST).strftime("%Y-%m-%d %H:%M HST"),
                observed_ms=int(when.timestamp() * 1000),
                station=_attribute(stations, m.start()) or default_station,
                kind=kind,
                value_urad=sign * mag,
                magnitude_urad=mag,
                episode_no=int(ep.group(1)) if ep else None,
                episode_source="stated" if ep else None,
                qualifier=(m.group("qual") or "exact").lower(),
                source_sentence=sentence[:600],
            ))
    return rows


def _infer_episodes(conn, rows: list[dict]) -> None:
    """Attach an episode number to readings that do not state one.

    HVO often writes "during the episode" without a number. The episode is
    unambiguous from the date - it is the one most recently ended when the
    notice was issued - but the inference is recorded in ``episode_source`` so
    it can be excluded from any analysis that needs HVO's own attribution.
    """
    episodes = conn.execute(
        """SELECT episode_no,
                  CAST((julianday(start_utc) - 2440587.5) * 86400000 AS INTEGER) s,
                  CAST((julianday(pause_utc) - 2440587.5) * 86400000 AS INTEGER) p
           FROM episode WHERE start_utc IS NOT NULL ORDER BY episode_no""").fetchall()
    if not episodes:
        return
    for r in rows:
        if r.get("episode_no") is not None:
            continue
        prior = [e for e in episodes if e[1] is not None and e[1] <= r["observed_ms"]]
        if not prior:
            continue
        r["episode_no"] = prior[-1][0]
        r["episode_source"] = "inferred_from_date"


def collect(conn, *, since: str | None = None, **_) -> None:
    since = since or "2024-12-01"
    with db.Run(conn, "tilt_notice", "tilt_reading") as run:
        notices = conn.execute(
            """SELECT notice_id, sent_utc, body_text FROM alert_notice
               WHERE sent_utc >= ? AND body_text IS NOT NULL
               ORDER BY sent_utc""", (since,)).fetchall()
        batch: list[dict] = []
        for n in notices:
            batch.extend(extract(n["notice_id"], n["sent_utc"], n["body_text"]))
        _infer_episodes(conn, batch)
        run.rows_seen = len(batch)
        with db.tx(conn):
            conn.execute("DELETE FROM tilt_reading WHERE observed_utc >= ?", (since,))
            db.upsert(conn, "tilt_reading", batch,
                      conflict=["notice_id", "station", "kind", "magnitude_urad",
                                "episode_no"])
        by_kind: dict[str, int] = {}
        for r in batch:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        log.info("tilt_notice: %d readings from %d notices %s",
                 len(batch), len(notices), by_kind)
