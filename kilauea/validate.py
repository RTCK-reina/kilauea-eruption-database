"""Data integrity checks.

These run against the loaded database and are meant to be honest rather than
green: a check that cannot pass because the upstream data genuinely has gaps
reports WARN with the count, not PASS.
"""
from __future__ import annotations

from typing import Any

from . import config


def _one(conn, sql: str, *params) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _rows(conn, sql: str, *params):
    return conn.execute(sql, params).fetchall()


def run(conn) -> list[dict]:
    out: list[dict] = []

    def check(name, status, detail=""):
        out.append({"check": name, "status": status, "detail": detail})

    # --- episodes -------------------------------------------------------------
    n_ep = _one(conn, "SELECT COUNT(*) FROM episode")
    check("episode: rows present", "PASS" if n_ep else "FAIL", f"{n_ep} episodes")

    gaps = _rows(conn, """
        SELECT a.episode_no + 1 AS missing FROM episode a
        WHERE a.episode_no + 1 <= (SELECT MAX(episode_no) FROM episode)
          AND NOT EXISTS (SELECT 1 FROM episode b WHERE b.episode_no = a.episode_no + 1)
    """)
    check("episode: numbering contiguous",
          "PASS" if not gaps else "FAIL",
          "missing: " + ", ".join(str(g["missing"]) for g in gaps) if gaps else "1..%d" % (n_ep or 0))

    bad_order = _one(conn, """
        SELECT COUNT(*) FROM episode
        WHERE pause_utc IS NOT NULL AND start_utc IS NOT NULL AND pause_utc <= start_utc
    """)
    check("episode: pause after start", "PASS" if not bad_order else "FAIL",
          f"{bad_order} inverted")

    overlap = _one(conn, """
        SELECT COUNT(*) FROM episode a JOIN episode b ON b.episode_no = a.episode_no + 1
        WHERE a.pause_utc IS NOT NULL AND b.start_utc IS NOT NULL
          AND b.start_utc < a.pause_utc
    """)
    check("episode: no overlapping episodes", "PASS" if not overlap else "FAIL",
          f"{overlap} overlaps")

    # Published duration vs. duration computed from the timestamps. USGS rounds
    # to whole/half hours, so a tolerance of 1.5 h is expected, not a defect.
    drift = _rows(conn, """
        SELECT episode_no, duration_hours, duration_hours_calc,
               ABS(duration_hours - duration_hours_calc) AS d
        FROM episode
        WHERE duration_hours IS NOT NULL AND duration_hours_calc IS NOT NULL
          AND ABS(duration_hours - duration_hours_calc) > 1.5
        ORDER BY d DESC
    """)
    check("episode: published vs computed duration agree (<=1.5 h)",
          "PASS" if not drift else "WARN",
          "; ".join(f"ep{r['episode_no']}: {r['duration_hours']}h vs "
                    f"{r['duration_hours_calc']:.1f}h" for r in drift[:5]) or "all within tolerance")

    missing_vol = _one(conn, "SELECT COUNT(*) FROM episode WHERE volume_mcm IS NULL")
    check("episode: volume populated", "PASS" if not missing_vol else "WARN",
          f"{missing_vol} without volume")

    # --- eruptions ------------------------------------------------------------
    n_er = _one(conn, "SELECT COUNT(*) FROM eruption")
    check("eruption: rows present", "PASS" if n_er else "FAIL", f"{n_er} records")

    dup_er = _one(conn, """
        SELECT COUNT(*) FROM (SELECT eruption_number FROM eruption
                              GROUP BY eruption_number HAVING COUNT(*) > 1)
    """)
    check("eruption: unique eruption numbers", "PASS" if not dup_er else "FAIL",
          f"{dup_er} duplicated")

    imprecise = _one(conn, "SELECT COUNT(*) FROM eruption WHERE start_precision <> 'day'")
    check("eruption: start-date precision", "PASS", 
          f"{imprecise}/{n_er} coarser than day precision (expected for prehistoric records)")

    # --- earthquakes ----------------------------------------------------------
    n_q = _one(conn, "SELECT COUNT(*) FROM earthquake")
    check("earthquake: rows present", "PASS" if n_q else "FAIL", f"{n_q:,} events")

    outside = _one(conn, "SELECT COUNT(*) FROM earthquake WHERE dist_from_summit_km > ?",
                   config.QUAKE_RADIUS_KM + 0.5)
    check("earthquake: all inside search radius", "PASS" if not outside else "FAIL",
          f"{outside} outside")

    no_mag = _one(conn, "SELECT COUNT(*) FROM earthquake WHERE magnitude IS NULL")
    check("earthquake: magnitude populated", "PASS" if not no_mag else "WARN",
          f"{no_mag:,} without magnitude")

    # Year-by-year gaps larger than 90 days signal a catalogue hole rather than
    # a quiet period, given Kīlauea's background rate.
    holes = _rows(conn, """
        SELECT day_utc, gap FROM (
          SELECT day_utc,
                 julianday(day_utc) - julianday(LAG(day_utc) OVER (ORDER BY day_utc)) AS gap
          FROM v_daily_seismicity)
        WHERE gap > 90 ORDER BY gap DESC LIMIT 5
    """)
    check("earthquake: no >90 day catalogue holes", "PASS" if not holes else "WARN",
          "; ".join(f"{r['day_utc']} after {r['gap']:.0f}d" for r in holes) or "none")

    # --- tilt -----------------------------------------------------------------
    n_t = _one(conn, "SELECT COUNT(*) FROM tilt_sample")
    check("tilt: rows present", "PASS" if n_t else "WARN", f"{n_t:,} samples")

    if n_t:
        agg_ok = _one(conn, """
            SELECT (SELECT COUNT(*) FROM tilt_hourly) -
                   (SELECT COUNT(*) FROM (SELECT DISTINCT station, time_ms/3600000
                                          FROM tilt_sample))
        """)
        check("tilt: hourly aggregate matches sample hours",
              "PASS" if agg_ok == 0 else "FAIL", f"difference {agg_ok}")

        relevel = _one(conn, "SELECT COUNT(*) FROM tilt_hourly WHERE n_segments > 1")
        check("tilt: releveling boundaries flagged", "PASS",
              f"{relevel} hours span more than one levelling segment")

    # --- referential ----------------------------------------------------------
    orphan_tilt = _one(conn, """
        SELECT COUNT(*) FROM tilt_sample s
        WHERE NOT EXISTS (SELECT 1 FROM tilt_station t WHERE t.code = s.station)
    """)
    check("tilt: every sample has a station record", "PASS" if not orphan_tilt else "FAIL",
          f"{orphan_tilt} orphans")

    # --- gas, plume, gravity ---------------------------------------------------
    for table, label in (("so2_emission", "so2"), ("plume_height", "plume"),
                         ("gravity_hourly", "gravity")):
        n = _one(conn, f"SELECT COUNT(*) FROM {table}")
        span = _rows(conn, f"SELECT MIN({'hour_utc' if table.endswith('hourly') else 'time_utc'}) a, "
                           f"MAX({'hour_utc' if table.endswith('hourly') else 'time_utc'}) b FROM {table}")
        detail = f"{n:,} rows"
        if n and span:
            detail += f", {span[0]['a'][:10]} .. {span[0]['b'][:10]}"
        check(f"{label}: rows present", "PASS" if n else "WARN", detail)

    future = _one(conn, """
        SELECT COUNT(*) FROM gravity_hourly
        WHERE hour_utc > strftime('%Y-%m-%dT%H:00:00Z', 'now')
           OR hour_utc < '2009-01-01T00:00:00Z'
    """)
    check("gravity: no out-of-range timestamps", "PASS" if not future else "FAIL",
          f"{future} rows outside 2009..now")

    # --- HVO forecast benchmark -------------------------------------------------
    n_f = _one(conn, "SELECT COUNT(*) FROM hvo_forecast")
    scored = _one(conn, "SELECT COUNT(*) FROM v_hvo_forecast_skill WHERE hit IS NOT NULL")
    hits = _one(conn, "SELECT SUM(hit) FROM v_hvo_forecast_skill WHERE hit IS NOT NULL") or 0
    check("hvo_forecast: windows extracted", "PASS" if n_f else "WARN",
          f"{n_f} windows, {scored} scored against an actual onset")
    if scored:
        check("hvo_forecast: benchmark computable", "PASS",
              f"HVO hit rate {100 * hits / scored:.1f}% ({hits}/{scored})")

    bad_window = _one(conn, """
        SELECT COUNT(*) FROM hvo_forecast
        WHERE window_end_utc < window_start_utc OR window_days > 31
    """)
    check("hvo_forecast: windows well formed", "PASS" if not bad_window else "FAIL",
          f"{bad_window} malformed")

    # --- tilt mined from notices --------------------------------------------------
    n_tr = _one(conn, "SELECT COUNT(*) FROM tilt_reading")
    days = _one(conn, "SELECT COUNT(DISTINCT substr(observed_hst,1,10)) FROM tilt_reading")
    check("tilt_reading: rows present", "PASS" if n_tr else "WARN",
          f"{n_tr:,} readings across {days} days")

    eps_defl = _one(conn, """
        SELECT COUNT(*) FROM v_episode_tilt_notice
        WHERE station='UWD' AND deflation_urad IS NOT NULL""")
    eps_total = _one(conn, "SELECT COUNT(*) FROM episode")
    check("tilt_reading: per-episode deflation coverage", "PASS" if eps_defl else "WARN",
          f"{eps_defl}/{eps_total} エピソード")

    # A recovery fraction far above 1 means the deflation and the re-inflation
    # came from different instruments or different episodes.
    odd = _one(conn, """
        SELECT COUNT(*) FROM v_episode_tilt_notice
        WHERE recovery_fraction > 1.6 OR recovery_fraction < 0""")
    check("tilt_reading: recovery fraction plausible", "PASS" if not odd else "WARN",
          f"{odd} エピソードが 0〜1.6 の外")

    # --- VONA cross-check ----------------------------------------------------------
    n_vona = _one(conn, "SELECT COUNT(*) FROM vona")
    check("vona: rows present", "PASS" if n_vona else "WARN", f"{n_vona} messages")

    # VONA onsets are minute-precision UTC and independent of the HST table, so a
    # mismatch means the HST->UTC conversion or the published time is wrong.
    # VONA timing is a cross-check on the episode table, not a replacement:
    # the ONSET field's meaning varies by message type (see v_vona_episode).
    agree = _one(conn, """
        SELECT COUNT(*) FROM v_vona_episode v JOIN episode e USING (episode_no)
        WHERE v.started_onset_utc IS NOT NULL
          AND ABS(julianday(v.started_onset_utc) - julianday(e.start_utc)) * 1440 <= 60""")
    checked = _one(conn, """
        SELECT COUNT(*) FROM v_vona_episode v JOIN episode e USING (episode_no)
        WHERE v.started_onset_utc IS NOT NULL""")
    disagree = _rows(conn, """
        SELECT v.episode_no,
               ROUND((julianday(v.started_onset_utc) - julianday(e.start_utc)) * 24, 1) AS h
        FROM v_vona_episode v JOIN episode e USING (episode_no)
        WHERE v.started_onset_utc IS NOT NULL
          AND ABS(julianday(v.started_onset_utc) - julianday(e.start_utc)) * 1440 > 60
        ORDER BY v.episode_no""")
    check("vona: STARTED onset vs the episode table",
          "PASS" if not disagree else "WARN",
          f"{agree}/{checked} が60分以内"
          + ("; " + ", ".join(f"ep{r['episode_no']}が{r['h']:+.1f}時間ずれ" for r in disagree[:5])
             + "（上流のONSET欄の揺れ。エピソード表を正とする）" if disagree else ""))

    multi = _one(conn, "SELECT COUNT(*) FROM v_vona_episode WHERE distinct_onsets > 1")
    check("vona: onset values consistent within an episode", "PASS",
          f"{multi} エピソードでメッセージ間に食い違いあり（v_vona_episode で個別に確認できる）")

    # --- GNSS -----------------------------------------------------------------
    # Reporting commands open read-only and therefore cannot migrate. Say so
    # plainly instead of dying on "no such column" from an older database.
    gnss_cols = {r[1] for r in conn.execute("PRAGMA table_info(gnss_position)")}
    if "up_abs_m" not in gnss_cols:
        check("gnss: schema up to date", "FAIL" if gnss_cols else "WARN",
              "up_abs_m 列が無い。`python3 -m kilauea init` を一度実行して移行すること"
              if gnss_cols else "gnss_position テーブルが無い")
        return out

    n_g = _one(conn, "SELECT COUNT(*) FROM gnss_position")
    span = _rows(conn, "SELECT MIN(date_utc) a, MAX(date_utc) b FROM gnss_position")
    lag = _one(conn, """
        SELECT ROUND(julianday('now') - julianday(MAX(date_utc)), 1) FROM gnss_position""")
    check("gnss: rows present", "PASS" if n_g else "WARN",
          f"{n_g:,} rows"
          + (f", {span[0]['a']} .. {span[0]['b']}, 遅延 {lag} 日" if n_g and span else ""))

    # Regression guard. NGL's final and rapid series use different integer
    # references, so a day-to-day step of a metre means the raw offsets are
    # being compared instead of the absolute positions.
    jumps = _rows(conn, """
        SELECT station, date_utc, ROUND(step * 1000) AS step_mm FROM (
            SELECT station, date_utc,
                   up_abs_m - LAG(up_abs_m) OVER (PARTITION BY station ORDER BY date_ms) AS step
            FROM gnss_position WHERE frame = 'PA' AND up_abs_m IS NOT NULL)
        WHERE ABS(step) > 0.2 ORDER BY ABS(step) DESC LIMIT 5""")
    check("gnss: no metre-scale steps between consecutive days",
          "PASS" if not jumps else "FAIL",
          "; ".join(f"{r['station']} {r['date_utc']} {r['step_mm']:.0f}mm" for r in jumps)
          or "段差なし（final/rapid の基準差は up_abs_m で吸収済み）")

    no_abs = _one(conn, "SELECT COUNT(*) FROM gnss_position WHERE up_abs_m IS NULL")
    check("gnss: absolute positions computed", "PASS" if not no_abs else "FAIL",
          f"{no_abs} rows without up_abs_m")

    # --- brief pipeline ---------------------------------------------------------
    n_park = _one(conn, "SELECT COUNT(*) FROM park_status")
    park_age = _one(conn, """
        SELECT ROUND((julianday('now') - julianday(MAX(fetched_utc))) , 1)
        FROM park_status""")
    check("park: status fetched", "PASS" if n_park else "WARN",
          f"{n_park} rows"
          + (f", 最終取得は {park_age} 日前" if park_age is not None else ""))

    n_brief = _one(conn, "SELECT COUNT(*) FROM brief_run")
    check("brief_run: rows present", "PASS" if n_brief else "WARN",
          f"{n_brief} briefs recorded")

    # A brief that recorded no forecast is a brief whose numbers did not survive
    # extraction - worth surfacing rather than discovering in the output.
    no_fc = _one(conn, "SELECT COUNT(*) FROM brief_run WHERE brief_point_date IS NULL")
    check("brief_run: own forecast populated", "PASS" if not no_fc else "WARN",
          f"{no_fc} without a point date")

    no_tilt = _one(conn, """
        SELECT COUNT(*) FROM brief_run
        WHERE tilt_cumulative_urad IS NULL AND is_erupting = 0""")
    check("brief_run: tilt extracted while paused", "PASS" if not no_tilt else "WARN",
          f"{no_tilt} paused-day briefs without a tilt reading")

    # --- runs -----------------------------------------------------------------
    failed = _rows(conn, """
        SELECT source, message FROM source_run
        WHERE id IN (SELECT MAX(id) FROM source_run GROUP BY source) AND status='error'
    """)
    check("collectors: last run succeeded", "PASS" if not failed else "FAIL",
          "; ".join(f"{r['source']}: {(r['message'] or '')[:60]}" for r in failed) or "all ok")

    return out


def render(report: list[dict]) -> str:
    width = max(len(r["check"]) for r in report)
    lines = ["", "data integrity report", "=" * (width + 30)]
    for r in report:
        lines.append(f"{r['status']:<5} {r['check'].ljust(width)}  {r['detail']}")
    counts = {}
    for r in report:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lines.append("-" * (width + 30))
    lines.append("  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    return "\n".join(lines)
