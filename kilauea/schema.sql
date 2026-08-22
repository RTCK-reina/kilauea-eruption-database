-- =============================================================================
-- Kīlauea eruption database — physical schema
-- SQLite 3.35+ (uses ON CONFLICT ... DO UPDATE and generated columns)
--
-- Time convention: every *_utc column stores ISO-8601 UTC as
-- 'YYYY-MM-DDTHH:MM:SSZ'. Local Hawaii time (HST = UTC-10, no DST) is kept
-- verbatim alongside it wherever the upstream source publishes HST, so that a
-- transcription error in our conversion stays auditable.
-- =============================================================================

-- journal_mode is set in kilauea/db.py (with a fallback for
-- filesystems that cannot host a WAL shared-memory file).
PRAGMA foreign_keys = ON;

-- --- provenance ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS source_run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT    NOT NULL,
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    status        TEXT    NOT NULL DEFAULT 'running',  -- running|ok|error
    rows_seen     INTEGER DEFAULT 0,
    rows_written  INTEGER DEFAULT 0,
    message       TEXT
);
CREATE INDEX IF NOT EXISTS ix_source_run_source ON source_run(source, started_at DESC);

CREATE TABLE IF NOT EXISTS dataset (
    key           TEXT PRIMARY KEY,   -- e.g. 'sb:67ead922d34ed02007f83585'
    title         TEXT,
    publisher     TEXT,
    url           TEXT,
    doi           TEXT,
    period_start  TEXT,
    period_end    TEXT,
    license       TEXT,
    retrieved_at  TEXT
);

-- --- reference ----------------------------------------------------------------

CREATE TABLE IF NOT EXISTS volcano (
    vnum          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    latitude      REAL,
    longitude     REAL,
    elevation_m   REAL,
    observatory   TEXT,
    region        TEXT
);

-- --- 1. long-term eruption catalogue (Smithsonian GVP) -------------------------
-- Grain: one row per GVP eruption record. ~75 rows for Kīlauea, 4650 BCE-present.

CREATE TABLE IF NOT EXISTS eruption (
    eruption_number       INTEGER PRIMARY KEY,
    vnum                  TEXT NOT NULL REFERENCES volcano(vnum),
    activity_type         TEXT,          -- Confirmed Eruption / Uncertain ...
    vei                   INTEGER,       -- ExplosivityIndexMax
    vei_modifier          TEXT,
    activity_area         TEXT,
    activity_unit         TEXT,
    evidence_method       TEXT,

    start_year            INTEGER,
    start_month           INTEGER,
    start_day             INTEGER,
    start_year_modifier   TEXT,
    start_year_uncert     INTEGER,
    start_day_uncert      INTEGER,
    start_date            TEXT,          -- ISO date when Y/M/D are all known
    start_precision       TEXT,          -- day|month|year

    end_year              INTEGER,
    end_month             INTEGER,
    end_day               INTEGER,
    end_year_modifier     TEXT,
    end_date              TEXT,
    end_precision         TEXT,

    duration_days         REAL,          -- NULL when either bound is imprecise
    source                TEXT DEFAULT 'GVP',
    retrieved_at          TEXT
);
CREATE INDEX IF NOT EXISTS ix_eruption_start ON eruption(start_year, start_month, start_day);

-- --- 2. episodic fountaining (USGS HVO, Dec 2024 onward) -----------------------
-- Grain: one row per numbered fountaining episode of the ongoing summit
-- eruption. This is the primary short-term forecasting target.

CREATE TABLE IF NOT EXISTS episode (
    episode_no            INTEGER PRIMARY KEY,
    start_hst_text        TEXT,          -- verbatim from USGS, e.g. 'December 23, 2024 - 2:20 a.m.'
    pause_hst_text        TEXT,
    start_utc             TEXT,
    pause_utc             TEXT,
    start_time_is_exact   INTEGER DEFAULT 1,  -- 0 when USGS gave only a date or a range
    pause_time_is_exact   INTEGER DEFAULT 1,
    duration_text         TEXT,
    duration_hours        REAL,          -- eruptive duration as published
    duration_hours_calc   REAL,          -- pause_utc - start_utc, for cross-checking
    repose_text           TEXT,          -- 'Pause Duration Following Episode'
    repose_hours          REAL,          -- published repose AFTER this episode
    repose_hours_calc     REAL,          -- next start_utc - this pause_utc
    fountain_height_m     REAL,
    fountain_height_text  TEXT,          -- verbatim, e.g. '400 (may update)'
    volume_mcm            REAL,          -- million cubic metres of lava
    precursor_hst_text    TEXT,          -- 'Precursory low-level activity began on ...'
    precursor_utc         TEXT,
    precursor_lead_hours  REAL,          -- start_utc - precursor_utc
    is_ongoing            INTEGER DEFAULT 0,  -- 1 when the pause is not yet published
    notes                 TEXT,
    retrieved_at          TEXT
);

-- Grain: one row per episode with documented tephra / Pele's hair impacts.
CREATE TABLE IF NOT EXISTS episode_hazard (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_no            INTEGER,
    date_text             TEXT,
    fountain_height_text  TEXT,
    fountain_height_m     REAL,
    wind_conditions       TEXT,
    plume_height_text     TEXT,
    impacts               TEXT,
    retrieved_at          TEXT,
    UNIQUE(episode_no, date_text)
);

-- --- 3. official alert notices (USGS HANS) -------------------------------------
-- Grain: one row per issued notice. ~6,900 rows for Kīlauea.

CREATE TABLE IF NOT EXISTS alert_notice (
    notice_id             TEXT PRIMARY KEY,   -- DOI-USGS-HVO-...
    sent_utc              TEXT NOT NULL,
    sent_unixtime         INTEGER,
    notice_type_cd        TEXT,      -- DU=Daily Update, VAN/VONA, IU=Information ...
    notice_type_title     TEXT,
    volc_cds              TEXT,
    alert_level           TEXT,      -- NORMAL|ADVISORY|WATCH|WARNING
    color_code            TEXT,      -- GREEN|YELLOW|ORANGE|RED
    prev_alert_level      TEXT,
    prev_color_code       TEXT,
    summary               TEXT,      -- extracted "Summary:" paragraph
    body_text             TEXT,      -- full notice, tags stripped
    url                   TEXT,
    retrieved_at          TEXT
);
CREATE INDEX IF NOT EXISTS ix_alert_sent ON alert_notice(sent_utc);
CREATE INDEX IF NOT EXISTS ix_alert_level ON alert_notice(alert_level, sent_utc);

-- --- 4. seismicity (USGS ComCat / FDSN) ----------------------------------------
-- Grain: one row per located earthquake within QUAKE_RADIUS_KM of the summit.

CREATE TABLE IF NOT EXISTS earthquake (
    event_id              TEXT PRIMARY KEY,
    time_utc              TEXT NOT NULL,
    time_ms               INTEGER NOT NULL,
    latitude              REAL,
    longitude             REAL,
    depth_km              REAL,
    magnitude             REAL,
    mag_type              TEXT,
    place                 TEXT,
    net                   TEXT,
    status                TEXT,
    rms                   REAL,
    gap                   REAL,
    nst                   INTEGER,
    horizontal_error_km   REAL,
    depth_error_km        REAL,
    dist_from_summit_km   REAL,
    retrieved_at          TEXT
);
CREATE INDEX IF NOT EXISTS ix_quake_time ON earthquake(time_ms);
CREATE INDEX IF NOT EXISTS ix_quake_mag  ON earthquake(magnitude);
CREATE INDEX IF NOT EXISTS ix_quake_depth ON earthquake(depth_km);

-- --- 5. ground tilt (USGS ScienceBase data releases) ----------------------------

CREATE TABLE IF NOT EXISTS tilt_station (
    code          TEXT PRIMARY KEY,
    name          TEXT,
    latitude      REAL,
    longitude     REAL,
    instrument    TEXT,
    sensor_type   TEXT,      -- analog|digital
    depth_m       REAL,
    notes         TEXT
);

-- Grain: one row per station per native sample (1 minute).
CREATE TABLE IF NOT EXISTS tilt_sample (
    station       TEXT NOT NULL REFERENCES tilt_station(code),
    time_utc      TEXT NOT NULL,
    time_ms       INTEGER NOT NULL,
    x_urad        REAL,
    y_urad        REAL,
    east_urad     REAL,
    north_urad    REAL,
    hole_temp_c   REAL,
    box_temp_c    REAL,
    voltage_v     REAL,
    segment       TEXT,      -- source file stem: tilt is re-levelled, so absolute
                             -- values are only comparable WITHIN a segment
    dataset_key   TEXT,
    PRIMARY KEY (station, time_ms)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_tilt_time ON tilt_sample(time_ms);

-- Grain: one row per station per hour. Derived from tilt_sample; this is what
-- the feature views join against.
CREATE TABLE IF NOT EXISTS tilt_hourly (
    station       TEXT NOT NULL,
    hour_utc      TEXT NOT NULL,
    hour_ms       INTEGER NOT NULL,
    east_mean     REAL,
    north_mean    REAL,
    east_min      REAL,
    east_max      REAL,
    north_min     REAL,
    north_max     REAL,
    east_std      REAL,
    north_std     REAL,
    n_samples     INTEGER,
    n_segments    INTEGER,
    PRIMARY KEY (station, hour_ms)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_tilt_hourly_time ON tilt_hourly(hour_ms);

-- --- 6. gas emission ------------------------------------------------------------
-- Grain: one row per published SO2 emission-rate measurement or daily mean.

CREATE TABLE IF NOT EXISTS so2_emission (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    site                  TEXT,      -- summit|LERZ|MERZ|SWRZ
    method                TEXT,      -- FLYSPEC array|road traverse|...
    aggregation           TEXT,      -- individual|daily_mean
    time_utc              TEXT,
    time_ms               INTEGER,
    date_local            TEXT,
    rate_tpd              REAL,      -- tonnes per day
    uncertainty_tpd       REAL,
    n_measurements        INTEGER,
    dataset_key           TEXT,
    source_file           TEXT,
    retrieved_at          TEXT,
    UNIQUE(site, method, aggregation, time_utc, rate_tpd, source_file)
);
CREATE INDEX IF NOT EXISTS ix_so2_time ON so2_emission(time_ms);

-- --- 7. plume height ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS plume_height (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    time_utc              TEXT,
    time_ms               INTEGER,
    height_m              REAL,
    height_ref            TEXT,      -- above vent / above sea level
    method                TEXT,
    dataset_key           TEXT,
    source_file           TEXT,
    retrieved_at          TEXT,
    UNIQUE(time_utc, height_m, source_file)
);
CREATE INDEX IF NOT EXISTS ix_plume_time ON plume_height(time_ms);

-- --- 8. continuous gravity ------------------------------------------------------
-- Grain: one row per station per hour (raw is 1 s; decimated on ingest).

CREATE TABLE IF NOT EXISTS gravity_hourly (
    station       TEXT NOT NULL,
    hour_utc      TEXT NOT NULL,
    hour_ms       INTEGER NOT NULL,
    gravity_mean  REAL,
    gravity_std   REAL,
    gravity_min   REAL,
    gravity_max   REAL,
    unit          TEXT,
    n_samples     INTEGER,
    dataset_key   TEXT,
    PRIMARY KEY (station, hour_ms)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_gravity_time ON gravity_hourly(hour_ms);

-- --- 9. thermal camera ----------------------------------------------------------
-- Grain: one row per published thermal image / derived statistic.

CREATE TABLE IF NOT EXISTS thermal_observation (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    camera        TEXT,
    time_utc      TEXT,
    time_ms       INTEGER,
    metric        TEXT,      -- max_temp_c | lake_level_m | ...
    value         REAL,
    unit          TEXT,
    dataset_key   TEXT,
    source_file   TEXT,
    retrieved_at  TEXT,
    UNIQUE(camera, time_utc, metric, source_file)
);
CREATE INDEX IF NOT EXISTS ix_thermal_time ON thermal_observation(time_ms);

-- --- 10. HVO's own published onset forecasts ------------------------------------
-- Grain: one row per date-window forecast sentence found in a HANS notice.
-- Derived from ``alert_notice.body_text`` by kilauea/forecast.py. This exists so
-- that any model has an honest benchmark: HVO publishes an explicit window for
-- the next fountaining episode, and beating it is the bar to clear.

CREATE TABLE IF NOT EXISTS hvo_forecast (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id          TEXT REFERENCES alert_notice(notice_id),
    issued_utc         TEXT NOT NULL,
    sentence           TEXT,
    window_start_date  TEXT,      -- HST calendar date
    window_end_date    TEXT,
    window_start_utc   TEXT,      -- 00:00 HST of window_start_date
    window_end_utc     TEXT,      -- 24:00 HST of window_end_date
    window_days        REAL,
    stated_episode_no  INTEGER,   -- when the notice names the episode
    target_episode_no  INTEGER,   -- resolved: first episode starting after issue
    actual_onset_utc   TEXT,
    lead_hours         REAL,      -- actual onset minus issue time
    hit                INTEGER,   -- 1 when the onset falls inside the window
    error_hours        REAL,      -- signed distance to the nearest window edge; 0 when hit
    UNIQUE(notice_id, window_start_date, window_end_date)
);
CREATE INDEX IF NOT EXISTS ix_forecast_issued ON hvo_forecast(issued_utc);

-- --- 11. daily HTML brief runs --------------------------------------------------
-- Grain: one row per generated status brief. Records the state the brief was
-- built from and the brief's own onset estimate, so that estimate can later be
-- scored against the actual onset exactly like HVO's published window.

CREATE TABLE IF NOT EXISTS brief_run (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_utc        TEXT NOT NULL,
    generated_hst        TEXT NOT NULL,
    source_notice_id     TEXT REFERENCES alert_notice(notice_id),
    source_notice_hst    TEXT,
    alert_level          TEXT,
    color_code           TEXT,
    is_erupting          INTEGER,
    last_episode_no      INTEGER,
    hours_since_pause    REAL,
    summit_tilt_station  TEXT,
    tilt_cumulative_urad REAL,
    tilt_24h_urad        REAL,
    tilt_episode_deflation_urad REAL,
    summit_eq_24h        INTEGER,
    so2_tpd              REAL,          -- NULL when no measurement was published
    hvo_window_start     TEXT,
    hvo_window_end       TEXT,
    brief_window_start   TEXT,          -- this brief's own estimate
    brief_window_end     TEXT,
    brief_point_date     TEXT,          -- single-day call
    actual_onset_utc     TEXT,          -- filled in later by score_briefs()
    brief_hit            INTEGER,
    brief_point_error_h  REAL,
    output_file          TEXT,
    UNIQUE(generated_utc)
);
CREATE INDEX IF NOT EXISTS ix_brief_generated ON brief_run(generated_utc);

-- --- 12. Hawaii Volcanoes National Park status ----------------------------------
-- Grain: one row per fetch of an NPS page. The park's own pages carry no machine
-- readable feed, so the extracted alert titles and viewpoint names are stored
-- alongside the page's stated "last updated" date; a brief built from this table
-- can therefore say how stale the park information is.

CREATE TABLE IF NOT EXISTS park_status (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_utc    TEXT NOT NULL,
    page           TEXT NOT NULL,      -- conditions | eruption_viewing
    url            TEXT,
    page_updated   TEXT,               -- "Last updated:" as printed by NPS
    notices_json   TEXT,               -- JSON array of {title, text}
    viewpoints_json TEXT,              -- JSON array of open viewpoint names
    body_text      TEXT,
    UNIQUE(page, fetched_utc)
);
CREATE INDEX IF NOT EXISTS ix_park_fetched ON park_status(page, fetched_utc DESC);

-- --- 13. tilt readings mined from HVO notice prose -------------------------------
-- Grain: one row per (notice, station, kind, value).
--
-- The ScienceBase tiltmeter releases lag by about six months, which leaves the
-- most recent episodes without deformation data - the binding constraint on
-- forecasting. HVO does, however, state summit tilt numerically in almost every
-- daily update. This table turns that prose into a series: roughly one reading
-- per station per day, current to within a day, covering the whole episodic
-- eruption.
--
-- These are HVO's own rounded, qualified figures ("about 16.5 microradians"),
-- not instrument output. They are not interchangeable with tilt_sample; the
-- qualifier column keeps the hedging visible.

CREATE TABLE IF NOT EXISTS tilt_reading (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id       TEXT NOT NULL REFERENCES alert_notice(notice_id),
    observed_utc    TEXT NOT NULL,     -- the notice's issue time
    observed_hst    TEXT NOT NULL,
    observed_ms     INTEGER NOT NULL,
    station         TEXT,              -- UWD | SDH | SMC | NULL when unattributed
    kind            TEXT NOT NULL,     -- deflation_episode | deflation_excursion
                                       -- | inflation_cumulative | change_24h | rate_per_day
    value_urad      REAL NOT NULL,     -- signed: deflation negative, inflation positive
    magnitude_urad  REAL NOT NULL,     -- as printed, unsigned
    episode_no      INTEGER,           -- episode the reading refers to
    episode_source  TEXT,              -- stated | inferred_from_date
    qualifier       TEXT,              -- about | approximately | nearly | more than | exact
    source_sentence TEXT,
    -- episode_no is part of the key: one notice can state the same figure for
    -- two different episodes, and dropping one of them loses coverage.
    UNIQUE(notice_id, station, kind, magnitude_urad, episode_no)
);
CREATE INDEX IF NOT EXISTS ix_tiltread_time ON tilt_reading(observed_ms);
CREATE INDEX IF NOT EXISTS ix_tiltread_kind ON tilt_reading(kind, station, observed_ms);

-- --- 14. VONA (Volcano Observatory Notice for Aviation) --------------------------
-- Grain: one row per VONA message.
--
-- VONAs are already in alert_notice as notice_type_cd 'VV', but their body is a
-- fixed key:value telex format rather than prose, so it parses exactly. They
-- carry the onset timestamp to the minute in UTC - finer and less ambiguous
-- than the episode table's rounded HST - plus the aviation colour transition,
-- ash-cloud height and drift direction.

CREATE TABLE IF NOT EXISTS vona (
    notice_id        TEXT PRIMARY KEY REFERENCES alert_notice(notice_id),
    sent_utc         TEXT NOT NULL,
    sent_ms          INTEGER NOT NULL,
    notice_number    TEXT,          -- e.g. 2026/56
    dtg_utc          TEXT,          -- the message's own date-time group
    colour_code      TEXT,
    previous_colour  TEXT,
    activity_status  TEXT,          -- ERUPTION IMMINENT | ERUPTION OCCURRED | ...
    onset_utc        TEXT,          -- ONSET field, minute precision
    onset_ms         INTEGER,
    duration_text    TEXT,
    ash_cloud_height TEXT,
    cloud_movement   TEXT,          -- drift direction
    remarks          TEXT,
    episode_no       INTEGER,       -- parsed from the remarks when stated
    retrieved_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_vona_sent ON vona(sent_ms);
CREATE INDEX IF NOT EXISTS ix_vona_onset ON vona(onset_ms);

-- --- 15. GNSS daily positions (Nevada Geodetic Laboratory) -----------------------
-- Grain: one row per station per day.
--
-- Continuous three-component deformation, which neither the tiltmeter releases
-- (six-month lag) nor the notice prose (one scalar per day) provide. UWEV sits
-- 430 m from the summit and is co-located with the UWD tiltmeter, so the two
-- measure the same inflation from different physics.
--
-- Positions are in the Pacific-plate-fixed frame (PA), which removes plate
-- motion and leaves volcanic deformation. NGL publishes two solution types:
-- 'final' (reprocessed, ~6 week lag) and 'rapid' (~10 day lag). Both are
-- ingested; ``solution`` distinguishes them and rapid rows are superseded by
-- final ones as they appear.

CREATE TABLE IF NOT EXISTS gnss_position (
    station      TEXT NOT NULL,
    date_utc     TEXT NOT NULL,      -- YYYY-MM-DD
    date_ms      INTEGER NOT NULL,
    solution     TEXT NOT NULL,      -- final | rapid
    frame        TEXT NOT NULL,      -- PA (Pacific plate fixed) | IGS20
    decimal_year REAL,
    mjd          INTEGER,
    -- NGL reports a position as an integer reference (e0/n0/u0) plus an offset.
    -- The reference DIFFERS between the final and rapid series for the same
    -- station, so the offset alone is not comparable across solution types -
    -- mixing them produces metre-scale phantom jumps. Always use the *_abs_m
    -- columns, which add the reference back in.
    east_ref_m   REAL,
    north_ref_m  REAL,
    up_ref_m     REAL,
    east_m       REAL,               -- offset from the station's reference east
    north_m      REAL,
    up_m         REAL,
    east_abs_m   REAL,               -- east_ref_m + east_m
    north_abs_m  REAL,
    up_abs_m     REAL,
    sig_east_m   REAL,
    sig_north_m  REAL,
    sig_up_m     REAL,
    latitude     REAL,
    longitude    REAL,
    height_m     REAL,
    retrieved_at TEXT,
    PRIMARY KEY (station, date_utc, frame)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_gnss_date ON gnss_position(date_ms);
CREATE INDEX IF NOT EXISTS ix_gnss_station ON gnss_position(station, date_ms);
