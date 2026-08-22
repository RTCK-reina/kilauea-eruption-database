"""Unit tests for the fiddly parsers: HST timestamps, durations, tilt CSV.

Run with:  python3 -m pytest tests -q     (or: python3 tests/test_parsing.py)
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kilauea.sources.episodes import (  # noqa: E402
    parse_duration_hours, parse_hst, parse_number, _height_metres,
)
from kilauea.sources.so2 import classify  # noqa: E402


def _utc(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def test_parse_hst_full():
    got, exact = parse_hst("December 23, 2024 - 2:20 a.m.")
    assert got == _utc("2024-12-23T12:20:00Z"), got
    assert exact == 1


def test_parse_hst_no_minutes():
    got, exact = parse_hst("December 23, 2024 - 4 p.m.")
    assert got == _utc("2024-12-24T02:00:00Z"), got


def test_parse_hst_noon_and_midnight():
    assert parse_hst("May 6, 2025 - 12:00 p.m.")[0] == _utc("2025-05-06T22:00:00Z")
    assert parse_hst("May 6, 2025 - 12:30 a.m.")[0] == _utc("2025-05-06T10:30:00Z")


def test_parse_hst_year_inherited():
    got, _ = parse_hst("July 9 - 1:20 p.m.", default_year=2025)
    assert got == _utc("2025-07-09T23:20:00Z"), got


def test_parse_hst_date_only_is_inexact():
    got, exact = parse_hst("December 23, 2024")
    assert got == _utc("2024-12-23T10:00:00Z")
    assert exact == 0


def test_parse_hst_tbd():
    assert parse_hst("TBD") == (None, 0)
    assert parse_hst("") == (None, 0)


def test_duration():
    assert parse_duration_hours("14 hours") == 14
    assert parse_duration_hours("8.5 days") == 204
    assert parse_duration_hours("35.5 hours") == 35.5
    assert parse_duration_hours("30 minutes") == 0.5
    assert parse_duration_hours("TBD") is None


def test_number_with_annotation():
    assert parse_number("400 (may update)") == 400.0
    assert parse_number("") is None


def test_height_metres_prefers_metric():
    assert _height_metres("1070 feet/325 meters") == 325.0
    assert _height_metres("330 feet") == 100.6


def test_so2_classification():
    assert classify("KIL_summit_SO2_2018-2022_means.csv") == (
        "summit", "DOAS traverse", "traverse_mean")
    assert classify("ERZSO2_RdTraverse_2014-2017.csv")[0] == "ERZ"
    assert classify("random.csv") is None


def test_schema_and_views_build():
    from kilauea import config
    conn = sqlite3.connect(":memory:")
    conn.executescript(config.SCHEMA_SQL.read_text())
    conn.executescript(config.VIEWS_SQL.read_text())
    views = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'")]
    assert "v_episode_features" in views
    for v in views:
        conn.execute(f"SELECT * FROM {v} LIMIT 1")


def test_upsert_is_idempotent():
    from kilauea import config, db
    conn = sqlite3.connect(":memory:")
    conn.executescript(config.SCHEMA_SQL.read_text())
    conn.execute("INSERT INTO volcano(vnum, name) VALUES ('332010','Kilauea')")
    row = dict(eruption_number=1, vnum="332010", vei=0, start_year=2024)
    for _ in range(3):
        db.upsert(conn, "eruption", [row], conflict=["eruption_number"])
    assert conn.execute("SELECT COUNT(*) FROM eruption").fetchone()[0] == 1


# --- brief context extractors -------------------------------------------------

_NOTICE = """HAWAIIAN VOLCANO OBSERVATORY DAILY UPDATE

Current Volcano Alert Level: ADVISORY
Current Aviation Color Code: YELLOW

Summary: Kilauea volcano is not erupting; the summit eruption is paused.

Overview:
The summit eruption is paused. Episode 53 ended on August 13.
Currently, a few of the fixed webcams and the UWD summit tiltmeter are offline.

Summit Observations:
There was only one earthquake recorded in the summit area in the past 24 hours.
The SMC tiltmeter has recorded a total of 12.1 microradians of inflationary tilt
since the end of episode 53 and increased by 3 microradians in the last 24 hours.
Deflationary tilt measured at the summit tiltmeter at Summer Camp (SMC) totaled
19.1 microradians during episode 53. The UWD tiltmeter is temporarily offline.
During inter-episode pauses, the sulfur dioxide (SO2) emission rate from the
summit typically varies between 1,000 to 5,000 tonnes per day.

Rift Zone Observations:
Rates of seismicity and ground deformation remain low in the East Rift Zone.

Resources:
Recent earthquakes in Hawaii (map and list): https://www.usgs.gov/observatories/hvo
"""


def test_sections_split():
    from kilauea import brief
    sec = brief.split_sections(_NOTICE)
    assert "Summit Observations" in sec and "Rift Zone Observations" in sec
    assert "earthquake recorded in the summit area" in sec["Summit Observations"]


def test_tilt_extraction():
    from kilauea import brief
    t = brief.extract_tilt(brief.split_sections(_NOTICE))
    assert t["station"]["value"] == "SMC"
    assert t["cumulative_urad"]["value"] == 12.1
    assert t["change_24h_urad"]["value"] == 3.0
    assert t["episode_deflation_urad"]["value"] == 19.1
    # Station codes are caps-only: "The"/"few" must not be read as stations.
    assert t["offline_stations"] == ["UWD"], t["offline_stations"]


def test_earthquake_word_number():
    from kilauea import brief
    assert brief.extract_earthquakes(brief.split_sections(_NOTICE))["value"] == 1


def test_earthquake_scoped_to_summit_section():
    """The Resources boilerplate mentions earthquakes and must not be picked up."""
    from kilauea import brief
    sec = brief.split_sections(_NOTICE)
    sec["Summit Observations"] = "Tremor is low."
    assert brief.extract_earthquakes(sec)["value"] is None


def test_so2_typical_vs_measured():
    from kilauea import brief
    s = brief.extract_so2(brief.split_sections(_NOTICE))
    assert s["measured"]["value"] is None
    assert "未確認" in s["measured"]["unavailable"] or s["measured"]["unavailable"]
    assert s["typical_range"]["low_tpd"] == 1000 and s["typical_range"]["high_tpd"] == 5000


def test_decimal_safe_sentence_split():
    from kilauea import brief
    assert any("12.1 microradians" in s for s in brief._sentences(_NOTICE))


def _forecast_conn():
    """In-memory database with just enough history to fit the deflation model."""
    import datetime as dt
    from kilauea import config, db
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(config.SCHEMA_SQL.read_text())
    conn.execute("INSERT INTO volcano(vnum, name) VALUES ('332010','Kilauea')")
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    eps, reads = [], []
    for n in range(1, 21):
        # repose_days = 0.7 * deflation + 1, so the fit has a clean signal
        defl = 14.0 + (n % 5)
        start = base + dt.timedelta(days=14 * n)
        pause = start + dt.timedelta(hours=9)
        nxt = pause + dt.timedelta(days=0.7 * defl + 1)
        eps.append(dict(episode_no=n,
                        start_utc=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        pause_utc=pause.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        repose_hours_calc=(nxt - pause).total_seconds() / 3600))
        reads.append(dict(notice_id=f"N{n}", observed_utc=pause.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          observed_hst=pause.strftime("%Y-%m-%d %H:%M HST"),
                          observed_ms=int(pause.timestamp() * 1000), station="UWD",
                          kind="deflation_episode", value_urad=-defl, magnitude_urad=defl,
                          episode_no=n, episode_source="stated", qualifier="exact"))
        conn.execute("INSERT INTO alert_notice(notice_id, sent_utc) VALUES (?,?)",
                     (f"N{n}", pause.strftime("%Y-%m-%dT%H:%M:%SZ")))
    db.upsert(conn, "episode", eps, conflict=["episode_no"])
    db.upsert(conn, "tilt_reading", reads,
              conflict=["notice_id", "station", "kind", "magnitude_urad", "episode_no"])
    conn.executescript(config.VIEWS_SQL.read_text())
    return conn


def test_own_forecast_uses_the_deflation_model():
    import datetime as dt
    from kilauea import brief
    conn = _forecast_conn()
    ep = conn.execute("SELECT * FROM episode ORDER BY episode_no DESC LIMIT 1").fetchone()
    pause = dt.datetime.strptime(ep["pause_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)
    reposes = [r[0] for r in conn.execute(
        "SELECT repose_hours_calc FROM episode WHERE repose_hours_calc IS NOT NULL "
        "ORDER BY episode_no")]
    tilt = {"cumulative_urad": {"value": None}, "change_24h_urad": {"value": None},
            "episode_deflation_urad": {"value": None}, "station": {"value": "UWD"}}
    f = brief.own_forecast(conn, reposes, pause, tilt, ep["episode_no"],
                           pause + dt.timedelta(days=3))
    assert f["method"] == "deflation model", f.get("method")
    assert f["window_start"] < f["point_date"] < f["window_end"]
    # the synthetic relation is exact, so the fit should recover it closely
    assert abs(f["deflation_model"]["slope_days_per_urad"] - 0.7) < 0.05, f["deflation_model"]
    assert f["early_risk_date"] is None      # no tilt readings supplied


def test_own_forecast_falls_back_to_repose_median():
    import datetime as dt
    from kilauea import brief
    conn = _forecast_conn()
    conn.execute("DELETE FROM tilt_reading")
    pause = dt.datetime(2026, 8, 13, 11, 23, tzinfo=dt.timezone.utc)
    f = brief.own_forecast(conn, [316.1] * 10, pause, 
                           {"cumulative_urad": {"value": None},
                            "change_24h_urad": {"value": None},
                            "episode_deflation_urad": {"value": None},
                            "station": {"value": None}},
                           None, pause + dt.timedelta(days=3))
    assert f["method"] == "repose median", f.get("method")
    assert f["point_date"] == "2026-08-26", f["point_date"]


def test_own_forecast_without_history():
    import datetime as dt
    from kilauea import brief
    conn = _forecast_conn()
    conn.execute("DELETE FROM tilt_reading")
    f = brief.own_forecast(conn, [], None, {"cumulative_urad": {"value": None},
                                            "change_24h_urad": {"value": None},
                                            "episode_deflation_urad": {"value": None},
                                            "station": {"value": None}},
                           None, dt.datetime(2026, 8, 19, tzinfo=dt.timezone.utc))
    assert f["point_date"] is None and "unavailable" in f


# --- tilt mined from notice prose ---------------------------------------------

_TILT_NOTICE = """Summit Observations:
The Uēkahuna tiltmeter (UWD) recorded about 11 microradians of deflationary tilt
during episode 8 and about 1.3 microradians of inflationary tilt since the end of
episode 8. Summit inflation continues at a reduced rate of less than 1 microradian
per day. Deflationary tilt measured at the summit tiltmeter at Summer Camp (SMC)
totaled 19.1 microradians during episode 53. Inflation resumed immediately.
"""


def _tilt_rows():
    from kilauea.sources import tilt_notice
    return tilt_notice.extract("N1", "2026-08-18T18:39:49Z", _TILT_NOTICE)


def test_tilt_notice_splits_opposite_figures_in_one_sentence():
    """11 µrad deflation and 1.3 µrad inflation share a sentence."""
    rows = {(r["kind"], r["magnitude_urad"]) for r in _tilt_rows()}
    assert ("deflation_episode", 11.0) in rows, rows
    assert ("inflation_cumulative", 1.3) in rows, rows


def test_tilt_notice_separates_rates():
    """'less than 1 microradian per day' is a rate, not a cumulative amount."""
    kinds = {r["magnitude_urad"]: r["kind"] for r in _tilt_rows()}
    assert kinds.get(1.0) == "rate_per_day", kinds


def test_tilt_notice_splits_after_digit_final_sentence():
    """'...during episode 53. Inflation resumed' must be two sentences."""
    rows = [r for r in _tilt_rows() if r["magnitude_urad"] == 19.1]
    assert rows and rows[0]["kind"] == "deflation_episode"
    assert "Inflation resumed" not in rows[0]["source_sentence"]


def test_tilt_notice_attributes_stations():
    stations = {r["magnitude_urad"]: r["station"] for r in _tilt_rows()}
    assert stations.get(19.1) == "SMC", stations
    assert stations.get(11.0) == "UWD", stations


def test_tilt_notice_rejects_non_station_acronyms():
    from kilauea.sources import tilt_notice
    rows = tilt_notice.extract("N2", "2026-08-18T18:39:49Z",
        "Summit Observations:\nThe NWS reports that UWD recorded 5.0 microradians of "
        "inflationary tilt since the end of episode 50.\n")
    assert all(r["station"] in (None, "UWD") for r in rows), [r["station"] for r in rows]


# --- VONA ----------------------------------------------------------------------

_VONA = """VOLCANO OBSERVATORY NOTICE FOR AVIATION (VONA)
DTG: 20260813/1126Z
VOLCANO: KILAUEA 332010
CURRENT COLOUR CODE: YELLOW
PREVIOUS COLOUR CODE: ORANGE
ACT STS: ERUPTION OCCURRED
ONSET: 20260813/1123Z
DUR: UNKNOWN
VA CLD HGT: NO VA CLD PRODUCED
MOV: SW
RMK: LAVA FOUNTAIN EPISODE 53 ENDED AT KILAUEA SUMMIT.
"""


def test_vona_parsing():
    from kilauea.sources import vona
    rows = vona.collect.__globals__  # noqa: F841 - module import check
    parsed = _parse_vona()
    assert parsed["onset_utc"] == "2026-08-13T11:23:00Z", parsed["onset_utc"]
    assert parsed["colour_code"] == "YELLOW"
    assert parsed["previous_colour"] == "ORANGE"
    assert parsed["episode_no"] == 53
    assert parsed["cloud_movement"] == "SW"


def _parse_vona():
    """Exercise the field parsers without touching a database."""
    from kilauea.sources import vona as v
    vals = {k: v._grab(rx, _VONA) for k, rx in v._FIELDS.items()}
    onset = v._dtg(vals["onset"])
    remarks = v._grab(r"RMK:\s*(.+)", _VONA)
    ep = v._EPISODE_RE.search(remarks or "")
    return {"onset_utc": v._iso(onset), "colour_code": vals["colour_code"],
            "previous_colour": vals["previous_colour"], "cloud_movement": vals["cloud_movement"],
            "episode_no": int(ep.group(1)) if ep else None}


def test_wind_extraction():
    from kilauea import brief
    sec = brief.split_sections(
        "Summit Observations:\nWinds are coming from the northeast between 11 to 20 "
        "miles per hour (mph), with gusts as high as 28 mph.\n")
    w = brief.extract_wind(sec)["value"]
    assert w["speed_low_mph"] == 11 and w["speed_high_mph"] == 20
    assert w["gust_mph"] == 28 and w["from_direction"] == "northeast"





# --- schema migration ----------------------------------------------------------

def test_migration_adds_missing_columns():
    """CREATE TABLE IF NOT EXISTS is a no-op, so schema changes need ALTER."""
    from kilauea import db
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE gnss_position (station TEXT NOT NULL, "
                 "date_utc TEXT NOT NULL, date_ms INTEGER NOT NULL, "
                 "solution TEXT NOT NULL, frame TEXT NOT NULL, up_m REAL, "
                 "PRIMARY KEY(station, date_utc, frame))")
    db.init(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(gnss_position)")}
    assert "up_abs_m" in cols and "up_ref_m" in cols, sorted(cols)


def test_migration_is_idempotent():
    from kilauea import db
    conn = sqlite3.connect(":memory:")
    db.init(conn)
    assert db.migrate(conn) == []


def test_migration_survives_constraint_clauses():
    """UNIQUE(a, b) and REFERENCES t(col) both contain commas and parentheses."""
    from kilauea import db
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE episode_hazard (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "episode_no INTEGER)")
    db.init(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(episode_hazard)")}
    assert "impacts" in cols and "wind_conditions" in cols, sorted(cols)


# --- GNSS ----------------------------------------------------------------------

def test_gnss_tenv3_parsing_and_absolute_position():
    from kilauea.sources.gnss import parse_tenv3, _parse_date
    import datetime as dt
    assert _parse_date("26JUL04") == dt.date(2026, 7, 4)
    assert _parse_date("99MAY30") == dt.date(1999, 5, 30)
    line = ("UWEV 26JUL04 2026.5051 61225 2425 6 -155.3 931 0.849335 2148256 "
            "0.673184 1257 0.629615 0.0030 0.000848 0.000750 0.003460 -0.009637 "
            "-0.110125 -0.035860 19.4208830142 -155.2911431073 1257.62961")
    r = parse_tenv3(line, "UWEV", "final", "PA", "now")[0]
    assert r["up_m"] == 0.629615 and r["up_ref_m"] == 1257
    # the absolute value is what is comparable across final and rapid solutions
    assert abs(r["up_abs_m"] - 1257.629615) < 1e-6, r["up_abs_m"]
    assert abs(r["up_abs_m"] - r["height_m"]) < 1e-4


def test_gnss_ignores_other_stations_in_the_same_file():
    from kilauea.sources.gnss import parse_tenv3
    text = ("BYRL 26JUL04 2026.5051 61225 2425 6 -155.3 931 0.1 2148256 0.2 "
            "1100 0.3 0.003 0.001 0.001 0.003 0 0 0 19.4 -155.2 1100.3")
    assert parse_tenv3(text, "UWEV", "final", "PA", "now") == []


def test_tilt_stale_1sec_filter_is_valid_sql():
    """The 1 Hz cleanup predicate must survive Python escaping and reach SQLite.

    Written as a plain string the ESCAPE clause collapses to ``ESCAPE ''`` and
    every ``collect tilt`` run without ``--include-1sec`` dies with
    "ESCAPE expression must be a single character".
    """
    from kilauea.sources.tilt import _STALE_1SEC_WHERE

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tilt_sample (segment TEXT)")
    conn.executemany(
        "INSERT INTO tilt_sample VALUES (?)",
        [("UWE_1sec_2018",), ("UWE_1min_2018",), ("UWEX1secY",)],
    )
    n = conn.execute(
        "SELECT COUNT(*) FROM tilt_sample WHERE " + _STALE_1SEC_WHERE
    ).fetchone()[0]
    # UWEX1secY is the guard: without ESCAPE the underscore matches any char.
    assert n == 1, n


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"ok   {name}")
    raise SystemExit(1 if failures else 0)
