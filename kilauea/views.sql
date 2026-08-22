-- =============================================================================
-- Analysis views. Rebuilt from scratch on every run (cheap, always consistent).
--
-- Three prediction targets are served:
--   A. next-episode onset timing   -> v_episode_features / v_episode_target_onset
--   B. long-term eruption probability -> v_eruption_intervals
--   C. episode magnitude (volume / fountain height) -> v_episode_target_size
--
-- LEAKAGE RULE, applied throughout: a feature attached to episode N may only use
-- information available before episode N started. Repose, tilt and seismicity
-- features are therefore computed over the window [pause(N-1), start(N)), and
-- anything measured during or after episode N lives only in the target columns.
-- =============================================================================

DROP VIEW IF EXISTS v_episode_base;
DROP VIEW IF EXISTS v_episode_quakes;
DROP VIEW IF EXISTS v_episode_tilt;
DROP VIEW IF EXISTS v_episode_features;
DROP VIEW IF EXISTS v_episode_target_onset;
DROP VIEW IF EXISTS v_episode_target_size;
DROP VIEW IF EXISTS v_eruption_intervals;
DROP VIEW IF EXISTS v_alert_level_changes;
DROP VIEW IF EXISTS v_daily_seismicity;
DROP VIEW IF EXISTS v_coverage;

-- --- A/C. episode-level -------------------------------------------------------

-- One row per episode, with the previous episode's closing state attached and
-- the repose window that precedes this episode made explicit.
CREATE VIEW v_episode_base AS
SELECT
    e.episode_no,
    e.start_utc,
    e.pause_utc,
    e.is_ongoing,
    CAST((julianday(e.start_utc) - 2440587.5) * 86400000 AS INTEGER) AS start_ms,
    CAST((julianday(e.pause_utc) - 2440587.5) * 86400000 AS INTEGER) AS pause_ms,
    e.duration_hours,
    e.duration_hours_calc,
    e.repose_hours,
    e.repose_hours_calc,
    e.fountain_height_m,
    e.volume_mcm,
    e.precursor_utc,
    e.precursor_lead_hours,
    p.episode_no          AS prev_episode_no,
    p.pause_utc           AS prev_pause_utc,
    CAST((julianday(p.pause_utc) - 2440587.5) * 86400000 AS INTEGER) AS prev_pause_ms,
    p.duration_hours      AS prev_duration_hours,
    p.fountain_height_m   AS prev_fountain_height_m,
    p.volume_mcm          AS prev_volume_mcm,
    p.repose_hours_calc   AS repose_before_hours,     -- measured gap before THIS episode
    q.volume_mcm          AS prev2_volume_mcm,
    q.repose_hours_calc   AS prev2_repose_hours
FROM episode e
LEFT JOIN episode p ON p.episode_no = e.episode_no - 1
LEFT JOIN episode q ON q.episode_no = e.episode_no - 2;

-- Seismicity in the repose window immediately before each episode, split by the
-- depth ranges HVO uses to separate the shallow summit reservoir (<5 km) from
-- deeper magma transport.
CREATE VIEW v_episode_quakes AS
SELECT
    b.episode_no,
    COUNT(q.event_id)                                                   AS eq_count_repose,
    SUM(CASE WHEN q.depth_km < 5  THEN 1 ELSE 0 END)                    AS eq_count_shallow,
    SUM(CASE WHEN q.depth_km >= 5 THEN 1 ELSE 0 END)                    AS eq_count_deep,
    MAX(q.magnitude)                                                    AS eq_max_mag,
    AVG(q.magnitude)                                                    AS eq_mean_mag,
    SUM(POWER(10.0, 1.5 * q.magnitude + 9.1))                           AS eq_moment_sum_nm,
    SUM(CASE WHEN q.time_ms >= b.start_ms - 86400000  THEN 1 ELSE 0 END) AS eq_count_24h,
    SUM(CASE WHEN q.time_ms >= b.start_ms - 259200000 THEN 1 ELSE 0 END) AS eq_count_72h,
    AVG(q.depth_km)                                                     AS eq_mean_depth_km
FROM v_episode_base b
LEFT JOIN earthquake q
       ON q.time_ms >= COALESCE(b.prev_pause_ms, b.start_ms - 86400000)
      AND q.time_ms <  b.start_ms
GROUP BY b.episode_no;

-- Tilt behaviour during the same repose window, per station.
-- Only hours inside a single levelling segment are used for the delta, because
-- a releveling resets the datum (see kilauea/sources/tilt.py).
CREATE VIEW v_episode_tilt AS
SELECT
    b.episode_no,
    t.station,
    COUNT(*)                                     AS tilt_hours,
    MIN(t.east_mean)                             AS east_min,
    MAX(t.east_mean)                             AS east_max,
    MAX(t.east_mean) - MIN(t.east_mean)          AS east_range_urad,
    MIN(t.north_mean)                            AS north_min,
    MAX(t.north_mean)                            AS north_max,
    MAX(t.north_mean) - MIN(t.north_mean)        AS north_range_urad,
    AVG(t.east_std)                              AS east_intrahour_std,
    SUM(CASE WHEN t.n_segments > 1 THEN 1 ELSE 0 END) AS releveling_hours
FROM v_episode_base b
JOIN tilt_hourly t
  ON t.hour_ms >= COALESCE(b.prev_pause_ms, b.start_ms - 86400000)
 AND t.hour_ms <  b.start_ms
GROUP BY b.episode_no, t.station;

-- Model-ready table: features observable before onset + the two target families.
CREATE VIEW v_episode_features AS
SELECT
    b.episode_no,
    b.start_utc,
    b.prev_pause_utc,
    -- ---- features known before onset ----
    b.prev_duration_hours,
    b.prev_volume_mcm,
    b.prev_fountain_height_m,
    b.prev2_volume_mcm,
    b.prev2_repose_hours,
    b.repose_before_hours,
    q.eq_count_repose,
    q.eq_count_shallow,
    q.eq_count_deep,
    q.eq_count_24h,
    q.eq_count_72h,
    q.eq_max_mag,
    q.eq_mean_mag,
    q.eq_mean_depth_km,
    q.eq_moment_sum_nm,
    t.tilt_hours          AS uwd_tilt_hours,
    t.east_range_urad     AS uwd_east_range_urad,
    t.north_range_urad    AS uwd_north_range_urad,
    t.releveling_hours    AS uwd_releveling_hours,
    b.precursor_lead_hours,
    -- ---- targets ----
    b.repose_hours_calc   AS target_repose_after_hours,
    b.duration_hours      AS target_duration_hours,
    b.volume_mcm          AS target_volume_mcm,
    b.fountain_height_m   AS target_fountain_height_m,
    b.is_ongoing
FROM v_episode_base b
LEFT JOIN v_episode_quakes q USING (episode_no)
LEFT JOIN v_episode_tilt   t ON t.episode_no = b.episode_no AND t.station = 'UWD';

-- Target A: given everything up to the end of episode N, how long until N+1?
CREATE VIEW v_episode_target_onset AS
SELECT
    f.episode_no,
    f.start_utc,
    f.target_repose_after_hours AS y_hours_to_next_onset,
    f.target_duration_hours,
    f.target_volume_mcm,
    f.target_fountain_height_m,
    f.repose_before_hours,
    f.prev_volume_mcm,
    f.eq_count_repose,
    f.uwd_east_range_urad
FROM v_episode_features f
WHERE f.target_repose_after_hours IS NOT NULL;

-- Target C: given only pre-onset information, how big will this episode be?
CREATE VIEW v_episode_target_size AS
SELECT
    f.episode_no,
    f.start_utc,
    f.repose_before_hours,
    f.prev_volume_mcm,
    f.prev_duration_hours,
    f.prev_fountain_height_m,
    f.eq_count_repose,
    f.eq_count_24h,
    f.eq_max_mag,
    f.uwd_east_range_urad,
    f.uwd_north_range_urad,
    f.precursor_lead_hours,
    f.target_volume_mcm      AS y_volume_mcm,
    f.target_fountain_height_m AS y_fountain_height_m,
    f.target_duration_hours  AS y_duration_hours
FROM v_episode_features f
WHERE f.is_ongoing = 0;

-- --- B. long-term eruption recurrence ------------------------------------------
-- Inter-eruption intervals from the GVP catalogue, restricted to records with a
-- day-precision start date so the interval is meaningful. Everything coarser
-- keeps its numeric year fields in ``eruption`` for separate treatment.
CREATE VIEW v_eruption_intervals AS
SELECT
    e.eruption_number,
    e.start_date,
    e.end_date,
    e.vei,
    e.duration_days,
    LAG(e.start_date) OVER w  AS prev_start_date,
    LAG(e.end_date)   OVER w  AS prev_end_date,
    LAG(e.vei)        OVER w  AS prev_vei,
    julianday(e.start_date) - julianday(LAG(e.start_date) OVER w) AS onset_interval_days,
    julianday(e.start_date) - julianday(LAG(e.end_date)   OVER w) AS repose_days
FROM eruption e
WHERE e.start_precision = 'day'
WINDOW w AS (ORDER BY e.start_date);

-- --- supporting views -----------------------------------------------------------

CREATE VIEW v_alert_level_changes AS
SELECT sent_utc, alert_level, color_code, prev_alert_level, prev_color_code,
       notice_type_cd, notice_id
FROM (
    SELECT a.*,
           LAG(alert_level) OVER (ORDER BY sent_utc) AS lag_level
    FROM alert_notice a
    WHERE alert_level IS NOT NULL
)
WHERE lag_level IS NULL OR lag_level <> alert_level
ORDER BY sent_utc;

CREATE VIEW v_daily_seismicity AS
SELECT substr(time_utc, 1, 10)                        AS day_utc,
       COUNT(*)                                       AS n_events,
       SUM(CASE WHEN depth_km < 5  THEN 1 ELSE 0 END) AS n_shallow,
       SUM(CASE WHEN depth_km >= 5 THEN 1 ELSE 0 END) AS n_deep,
       MAX(magnitude)                                 AS max_mag,
       AVG(depth_km)                                  AS mean_depth_km,
       SUM(POWER(10.0, 1.5 * magnitude + 9.1))        AS moment_sum_nm
FROM earthquake
GROUP BY day_utc;

CREATE VIEW v_coverage AS
SELECT 'eruption'      AS tbl, COUNT(*) AS n, MIN(start_date) AS t0, MAX(start_date) AS t1 FROM eruption
UNION ALL SELECT 'episode',            COUNT(*), MIN(start_utc), MAX(start_utc) FROM episode
UNION ALL SELECT 'episode_hazard',     COUNT(*), NULL, NULL FROM episode_hazard
UNION ALL SELECT 'alert_notice',       COUNT(*), MIN(sent_utc), MAX(sent_utc) FROM alert_notice
UNION ALL SELECT 'earthquake',         COUNT(*), MIN(time_utc), MAX(time_utc) FROM earthquake
UNION ALL SELECT 'tilt_sample',        COUNT(*), MIN(time_utc), MAX(time_utc) FROM tilt_sample
UNION ALL SELECT 'tilt_hourly',        COUNT(*), MIN(hour_utc), MAX(hour_utc) FROM tilt_hourly
UNION ALL SELECT 'so2_emission',       COUNT(*), MIN(time_utc), MAX(time_utc) FROM so2_emission
UNION ALL SELECT 'plume_height',       COUNT(*), MIN(time_utc), MAX(time_utc) FROM plume_height
UNION ALL SELECT 'gravity_hourly',     COUNT(*), MIN(hour_utc), MAX(hour_utc) FROM gravity_hourly
UNION ALL SELECT 'thermal_observation',COUNT(*), MIN(time_utc), MAX(time_utc) FROM thermal_observation;

-- --- benchmark: HVO's own onset forecasts ---------------------------------------
DROP VIEW IF EXISTS v_hvo_forecast_skill;
DROP VIEW IF EXISTS v_hvo_forecast_latest;

-- One row per episode: HVO's last published window before that onset, and
-- whether the onset landed inside it. This is the baseline a model must beat.
CREATE VIEW v_hvo_forecast_skill AS
SELECT f.*
FROM hvo_forecast f
JOIN (SELECT target_episode_no, MAX(issued_utc) AS issued_utc
      FROM hvo_forecast WHERE target_episode_no IS NOT NULL
      GROUP BY target_episode_no) last
  ON last.target_episode_no = f.target_episode_no
 AND last.issued_utc = f.issued_utc;

-- The most recent forecast on record, for operational comparison.
CREATE VIEW v_hvo_forecast_latest AS
SELECT * FROM hvo_forecast ORDER BY issued_utc DESC, id DESC LIMIT 1;

-- --- brief self-scoring ---------------------------------------------------------
DROP VIEW IF EXISTS v_brief_skill;

-- Each generated brief next to the episode that actually followed it, so this
-- brief's own window and single-day call can be scored on the same footing as
-- HVO's published window in v_hvo_forecast_skill.
CREATE VIEW v_brief_skill AS
SELECT
    b.id,
    b.generated_hst,
    b.last_episode_no,
    b.hvo_window_start,
    b.hvo_window_end,
    b.brief_window_start,
    b.brief_window_end,
    b.brief_point_date,
    e.episode_no                              AS actual_episode_no,
    e.start_utc                               AS actual_onset_utc,
    date(e.start_utc, '-10 hours')            AS actual_onset_date_hst,
    CASE WHEN e.start_utc IS NULL THEN NULL
         WHEN date(e.start_utc, '-10 hours') BETWEEN b.hvo_window_start
                                                 AND b.hvo_window_end
         THEN 1 ELSE 0 END                    AS hvo_hit,
    CASE WHEN e.start_utc IS NULL THEN NULL
         WHEN date(e.start_utc, '-10 hours') BETWEEN b.brief_window_start
                                                 AND b.brief_window_end
         THEN 1 ELSE 0 END                    AS brief_window_hit,
    CASE WHEN e.start_utc IS NULL THEN NULL
         ELSE CAST(julianday(date(e.start_utc, '-10 hours'))
                 - julianday(b.brief_point_date) AS INTEGER)
    END                                       AS brief_point_error_days
FROM brief_run b
LEFT JOIN episode e ON e.episode_no = b.last_episode_no + 1;

-- =============================================================================
-- Tilt mined from notice prose (tilt_reading)
--
-- This is the series that closes the six-month ScienceBase lag. Values are
-- HVO's own rounded figures; ``qualifier`` records the hedging, and
-- ``episode_source`` records whether HVO stated the episode or it was inferred
-- from the notice date.
-- =============================================================================

DROP VIEW IF EXISTS v_tilt_reinflation;
DROP VIEW IF EXISTS v_episode_tilt_notice;

-- Daily re-inflation trajectory during the repose that follows each episode.
CREATE VIEW v_tilt_reinflation AS
SELECT
    r.episode_no,
    r.station,
    substr(r.observed_hst, 1, 10)                    AS day_hst,
    r.observed_ms,
    MAX(r.magnitude_urad)                            AS cumulative_urad,
    (julianday(r.observed_utc) - julianday(e.pause_utc)) AS days_since_pause,
    r.episode_source,
    r.qualifier
FROM tilt_reading r
JOIN episode e ON e.episode_no = r.episode_no
WHERE r.kind = 'inflation_cumulative'
  AND e.pause_utc IS NOT NULL
  AND r.observed_utc >= e.pause_utc
GROUP BY r.episode_no, r.station, day_hst;

-- One row per episode per station: the deflation the episode produced and the
-- re-inflation observed during the repose that followed it.
CREATE VIEW v_episode_tilt_notice AS
SELECT
    e.episode_no,
    COALESCE(d.station, i.station)                   AS station,
    d.deflation_urad,
    d.deflation_qualifier,
    i.reinflation_last_urad,
    i.reinflation_max_urad,
    i.reinflation_days,
    i.reinflation_readings,
    i.reinflation_rate_urad_per_day,
    CASE WHEN d.deflation_urad > 0 AND i.reinflation_last_urad IS NOT NULL
         THEN ROUND(i.reinflation_last_urad / d.deflation_urad, 4) END
                                                     AS recovery_fraction,
    c.change_24h_last_urad
FROM episode e
LEFT JOIN (
    SELECT episode_no, station,
           MAX(magnitude_urad) AS deflation_urad,
           MIN(qualifier)      AS deflation_qualifier
    FROM tilt_reading WHERE kind = 'deflation_episode'
    GROUP BY episode_no, station) d ON d.episode_no = e.episode_no
LEFT JOIN (
    SELECT episode_no, station,
           COUNT(*)                     AS reinflation_readings,
           MAX(cumulative_urad)         AS reinflation_max_urad,
           MAX(days_since_pause)        AS reinflation_days,
           -- last observed cumulative value, not the max: HVO reports drops
           -- back after a deflationary excursion and the latest value is the
           -- state a forecast has to work from
           (SELECT cumulative_urad FROM v_tilt_reinflation z
             WHERE z.episode_no = y.episode_no AND z.station IS y.station
             ORDER BY z.observed_ms DESC LIMIT 1) AS reinflation_last_urad,
           ROUND(MAX(cumulative_urad) / NULLIF(MAX(days_since_pause), 0), 3)
                                        AS reinflation_rate_urad_per_day
    FROM v_tilt_reinflation y
    GROUP BY episode_no, station) i
    ON i.episode_no = e.episode_no AND i.station IS d.station
LEFT JOIN (
    SELECT episode_no, station,
           (SELECT magnitude_urad FROM tilt_reading w
             WHERE w.episode_no = v.episode_no AND w.station IS v.station
               AND w.kind = 'change_24h'
             ORDER BY w.observed_ms DESC LIMIT 1) AS change_24h_last_urad
    FROM tilt_reading v WHERE v.kind = 'change_24h'
    GROUP BY episode_no, station) c
    ON c.episode_no = e.episode_no AND c.station IS d.station;

-- Fold the mined tilt into the episode feature table. Every column here is
-- observable before the next episode starts, so the leakage rule still holds.
DROP VIEW IF EXISTS v_episode_features_tilt;
CREATE VIEW v_episode_features_tilt AS
SELECT
    f.*,
    t.deflation_urad                  AS tn_deflation_urad,
    t.reinflation_last_urad           AS tn_reinflation_last_urad,
    t.reinflation_max_urad            AS tn_reinflation_max_urad,
    t.reinflation_days                AS tn_reinflation_days,
    t.reinflation_readings            AS tn_reinflation_readings,
    t.reinflation_rate_urad_per_day   AS tn_reinflation_rate,
    t.recovery_fraction               AS tn_recovery_fraction,
    t.change_24h_last_urad            AS tn_change_24h_last_urad,
    p.deflation_urad                  AS tn_prev_deflation_urad
FROM v_episode_features f
LEFT JOIN v_episode_tilt_notice t
       ON t.episode_no = f.episode_no AND t.station = 'UWD'
LEFT JOIN v_episode_tilt_notice p
       ON p.episode_no = f.episode_no - 1 AND p.station = 'UWD';

-- --- VONA per episode ------------------------------------------------------------
DROP VIEW IF EXISTS v_vona_episode;

-- HVO's VONAs do not use the ONSET field consistently, and this view does not
-- pretend otherwise. Observed within this dataset:
--   * the first message of an episode sometimes states a time exactly four
--     hours early, corrected in the follow-up (episodes 46, 52);
--   * a CONTINUES message sometimes states the time of its own observation
--     rather than the onset (episode 51);
--   * an ENDED message states the *end* time for some episodes (53) and the
--     *start* time for others (51).
-- No single "true" onset can be derived from that, so each message type's value
-- is exposed separately and the episode table stays authoritative for timing.
-- What VONA does contribute reliably is minute-precision end times, the
-- aviation colour transition, and plume drift direction.
CREATE VIEW v_vona_episode AS
SELECT
    e.episode_no,
    (SELECT v.onset_utc FROM vona v
      WHERE v.episode_no = e.episode_no AND v.remarks LIKE '%STARTED%'
        AND v.onset_utc IS NOT NULL ORDER BY v.sent_ms LIMIT 1)   AS started_onset_utc,
    (SELECT v.onset_utc FROM vona v
      WHERE v.episode_no = e.episode_no AND v.remarks LIKE '%CONTINUES%'
        AND v.onset_utc IS NOT NULL ORDER BY v.sent_ms DESC LIMIT 1) AS continues_onset_utc,
    (SELECT v.onset_utc FROM vona v
      WHERE v.episode_no = e.episode_no AND v.remarks LIKE '%ENDED%'
        AND v.onset_utc IS NOT NULL ORDER BY v.sent_ms DESC LIMIT 1) AS ended_onset_utc,
    (SELECT COUNT(DISTINCT v.onset_utc) FROM vona v
      WHERE v.episode_no = e.episode_no AND v.onset_utc IS NOT NULL) AS distinct_onsets,
    (SELECT COUNT(*) FROM vona v WHERE v.episode_no = e.episode_no)   AS messages,
    (SELECT v.colour_code FROM vona v WHERE v.episode_no = e.episode_no
      ORDER BY v.sent_ms DESC LIMIT 1)                               AS final_colour_code,
    (SELECT v.cloud_movement FROM vona v
      WHERE v.episode_no = e.episode_no AND v.cloud_movement IS NOT NULL
      ORDER BY v.sent_ms DESC LIMIT 1)                               AS cloud_movement
FROM (SELECT DISTINCT episode_no FROM vona WHERE episode_no IS NOT NULL) e;

-- --- GNSS ------------------------------------------------------------------------
DROP VIEW IF EXISTS v_gnss_summit_daily;
DROP VIEW IF EXISTS v_episode_gnss;

-- Absolute positions only. The raw *_m offsets are referenced to a value that
-- differs between NGL's final and rapid series, so any statistic computed over
-- a window spanning both is meaningless unless the reference is added back.
CREATE VIEW v_gnss_summit_daily AS
SELECT g.station, g.date_utc, g.date_ms, g.solution,
       ROUND(g.up_abs_m * 1000, 2)    AS up_mm,
       ROUND(g.east_abs_m * 1000, 2)  AS east_mm,
       ROUND(g.north_abs_m * 1000, 2) AS north_mm,
       ROUND(g.sig_up_m * 1000, 2)    AS sig_up_mm
FROM gnss_position g
WHERE g.frame = 'PA' AND g.up_abs_m IS NOT NULL;

-- Per episode: summit uplift observed during the repose that followed it.
-- Daily GNSS scatter is 4-7 mm vertical and the inflation between episodes is
-- 10-25 mm, so ``up_range_mm`` is only marginally above noise; ``days_observed``
-- and ``mean_sigma_up_mm`` are there to judge whether a row is worth using.
CREATE VIEW v_episode_gnss AS
SELECT
    e.episode_no,
    g.station,
    COUNT(*)                                          AS days_observed,
    MIN(g.date_utc)                                   AS first_day,
    MAX(g.date_utc)                                   AS last_day,
    ROUND((MAX(g.up_abs_m) - MIN(g.up_abs_m)) * 1000, 2) AS up_range_mm,
    ROUND(AVG(g.sig_up_m) * 1000, 2)                  AS mean_sigma_up_mm,
    COUNT(DISTINCT g.solution)                        AS solution_types
FROM episode e
JOIN gnss_position g
  ON g.frame = 'PA' AND g.up_abs_m IS NOT NULL
 AND g.date_ms >= CAST((julianday(e.pause_utc) - 2440587.5) * 86400000 AS INTEGER)
 AND g.date_ms <  CAST((julianday(e.pause_utc) - 2440587.5) * 86400000 AS INTEGER)
                  + CAST(COALESCE(e.repose_hours_calc, 0) * 3600000 AS INTEGER)
WHERE e.pause_utc IS NOT NULL AND e.repose_hours_calc IS NOT NULL
GROUP BY e.episode_no, g.station;
