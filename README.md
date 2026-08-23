# Kīlauea eruption database

[![tests](https://github.com/RTCK-reina/kilauea-eruption-database/actions/workflows/tests.yml/badge.svg)](https://github.com/RTCK-reina/kilauea-eruption-database/actions/workflows/tests.yml)

A reproducible SQLite database of Kīlauea eruption, deformation, seismicity and
gas data, assembled from public USGS and Smithsonian sources, with feature views
built for three forecasting targets.

```
python3 -m kilauea init            # create schema + views
python3 -m kilauea collect all     # full build (~40 min, ~4.3 GB)
python3 -m kilauea update          # daily incremental refresh
python3 -m kilauea validate        # data integrity report
python3 -m kilauea status          # row counts and time coverage
python3 -m kilauea core-db         # derive the distributable core database
python3 -m kilauea cache           # what is in the download cache, and what is safe to delete
```

Requires Python 3.9+ and SQLite 3.28+. **No third-party packages**: the
collectors run unattended from cron on a Mac whose system Python has nothing
installed, so everything uses the standard library. Verified on macOS stock
`/usr/bin/python3` (3.9.6, SQLite 3.54). The database lands in `data/kilauea.db`;
downloaded source files are cached under `data/raw/` so re-runs do not refetch.

### `data/kilauea_core.db` — start here

A pre-built database ships with this repository, gzipped, so you can query
immediately without waiting for a full collection run. It is the complete build
**minus the two bulk time series**: `tilt_sample` (18.4 M one-minute samples)
and the 10-second SO2 stream. Everything else is identical, including all views,
`tilt_hourly`, and the HVO forecast benchmark.

```
gunzip -k data/kilauea_core.db.gz          # 38 MB -> 172 MB, once
python3 -m kilauea status   --db data/kilauea_core.db
python3 -m kilauea baseline --db data/kilauea_core.db
```

GitHub blocks files over 100 MiB, so the database is stored compressed and
`data/kilauea_core.db` itself is gitignored — decompressing it will not dirty
the working tree.

"Core" is a derivation, not a separate build. `core-db` copies a full database,
deletes exactly two things, VACUUMs, and refuses to write the result unless
every other table came through with its row count unchanged:

```
scripts/cut_release.sh              # derive, compress, split, write the notes
scripts/cut_release.sh --publish    # ... and upload the release
```

`cut_release.sh` does the derivation and the release together on purpose: the
committed archive and the release assets have to describe the same instant.
It hashes the database before and after, aborts if a `daily_update.sh` run
landed in the middle, and refuses to publish unless the refreshed archive is
already committed and pushed — `gh release create` tags the current tip, so a
release cut before the commit would ship the previous archive under notes
claiming otherwise. The archive is written with `gzip -n`, so an unchanged
database compresses to identical bytes and git records nothing.

The two are `tilt_sample` in full, and the rows of `so2_emission` where
`aggregation = 'individual' AND method = 'FLYSPEC array'` — the 10-second
stream. The traverse and daily-mean SO2 figures stay, as does `tilt_hourly`.
The core build can therefore never drift away from a full one by hand. Refresh
it sparingly all the same: the archive is a 38 MB binary, and a refresh whose
content genuinely changed adds that much to the git history permanently. CI
decompresses whatever is committed and opens it on every push, so a stale or
corrupt archive fails the build rather than reaching a user.

### The full database

`python3 -m kilauea collect all` rebuilds the full `data/kilauea.db` (~4.3 GB,
~40 minutes, ~1 GB of downloads) from the upstream sources. Do that when you
need raw tilt at native resolution; the hourly aggregate in the core database
covers the feature views.

If you would rather not wait for the collectors, the same build is attached to
the latest release: gzipped (4.3 GB to 565 MB) and split into 500 MB parts, so a
failed transfer costs one part rather than the whole file.

```
gh release download -R RTCK-reina/kilauea-eruption-database \
    -p 'kilauea.db.gz.part*' -p 'SHA256SUMS.txt'
shasum -a 256 -c SHA256SUMS.txt            # verify the parts before joining
cat kilauea.db.gz.part* | gunzip > data/kilauea.db
sqlite3 data/kilauea.db 'PRAGMA quick_check;'
python3 -m kilauea status --db data/kilauea.db
```

The release snapshot is a point-in-time copy. `python3 -m kilauea update` brings
it up to date incrementally from there.

---

## What is in it

| Table | Grain | Coverage | Source |
|---|---|---|---|
| `eruption` | one GVP eruption record | 4650 BCE – present, 75 records | Smithsonian GVP (WFS) |
| `episode` | one fountaining episode | 2024-12-23 – present, 53 episodes | USGS HVO eruption-information page |
| `episode_hazard` | tephra / Pele's hair impact report | 2025 – present | same page |
| `alert_notice` | one issued volcano notice | 2006-12 – present, ~6,800 | USGS HANS search API |
| `earthquake` | one located event within 30 km of the summit | 1959 – present, ~208,000 | USGS ComCat (FDSN) |
| `tilt_sample` | one borehole tiltmeter sample (1 min) | 2014, 2018, 2020–2025H1, ~18.4 M | USGS ScienceBase releases |
| `tilt_hourly` | station × hour aggregate | derived from `tilt_sample` | — |
| `so2_emission` | one SO2 emission-rate measurement or mean | 2008 – 2022, ~2.4 M | USGS ScienceBase releases |
| `plume_height` | one plume-height observation | 2008–2015, Apr–Aug 2018, ~23,000 | USGS ScienceBase |
| `gravity_hourly` | station × hour of continuous gravity | 2010-08 – 2018-06, ~64,000 (HOVL, PUOC) | USGS ScienceBase |
| `hvo_forecast` | one onset window published by HVO | 2024-12 – present, ~320 | mined from `alert_notice` text |
| `tilt_reading` | one tilt figure mined from notice prose | 2024-12 – present, ~1,200 over 555 days | `alert_notice.body_text` |
| `vona` | one aviation notice, parsed from its telex format | 2024-12 – present, 117 | `alert_notice` (type VV) |
| `gnss_position` | one station-day of GNSS position | 1999-05 – present (2–3 day lag), ~40,000 | Nevada Geodetic Laboratory |
| `park_status` | one fetch of an NPS park page | rolling | nps.gov (conditions, eruption viewing) |
| `brief_run` | one generated daily brief | rolling | written by `brief-context --record` |
| `thermal_observation` | — | empty by design, see below | — |
| `dataset`, `source_run` | provenance | every run is logged | — |

### Conventions

* Every `*_utc` column is ISO-8601 UTC, `YYYY-MM-DDTHH:MM:SSZ`.
* Hawaii is UTC−10 year-round (no DST), so HST↔UTC is a fixed offset. Where the
  upstream source publishes HST, the verbatim string is kept alongside the
  derived UTC value so a conversion error stays auditable.
* `*_ms` columns are Unix epoch milliseconds, indexed, for range joins.
* Upserts are keyed on natural identifiers, so every collector is idempotent —
  re-running never duplicates rows.

---

## The three forecasting targets

### A. Next-episode onset — `v_episode_target_onset`

The primary target. Since 2024-12-23 the summit eruption has produced 53
numbered fountaining episodes separated by repose intervals of 16 hours to 30
days. `y_hours_to_next_onset` is the measured gap from the end of episode *N* to
the onset of *N+1*.

Features are restricted to the repose window `[pause(N-1), start(N))`: previous
episode duration and volume, measured repose, seismicity counts split by depth
(shallow <5 km vs deeper), cumulative seismic moment, and the range of hourly
tilt at UWD over the same window.

53 samples is a small dataset. Treat this as a survival / time-to-event problem
with strong priors rather than a deep-learning problem, and hold out the most
recent episodes chronologically — random k-fold will leak, because repose
duration has drifted systematically over the eruption.

### B. Long-term eruption probability — `v_eruption_intervals`

From the GVP catalogue: onset-to-onset intervals, repose durations and VEI, for
records with day-level date precision. Records coarser than that (most of the
prehistoric ones) keep their numeric year fields in `eruption` and are excluded
from the interval view rather than being given a fabricated date.

### C. Episode size — `v_episode_target_size`

Volume erupted, maximum fountain height and duration, regressed on
information available at onset. Note that fountain height and volume have
*not* moved together over the eruption (compare episodes 29–32 with 34–35), so
they should be modelled separately.

`v_episode_features` is the joined table underneath all three.

### The benchmark you have to beat

HVO publishes its own window for the next episode in the daily update, e.g.
*"Preliminary data suggest that another episode is likely between Friday August
21 and Thursday August 27."* `kilauea/forecast.py` mines those sentences into
`hvo_forecast`, resolves each to the episode that actually followed, and scores
it. `v_hvo_forecast_skill` keeps the last window issued before each onset:

```sql
SELECT COUNT(*)                        AS n,
       SUM(hit)                        AS hits,
       ROUND(AVG(window_days), 2)      AS mean_window_days,
       ROUND(AVG(ABS(error_hours)), 1) AS mean_abs_error_hours,
       ROUND(AVG(lead_hours), 1)       AS mean_lead_hours
FROM v_hvo_forecast_skill WHERE hit IS NOT NULL;
```

On the current data that is **30 hits out of 38 episodes (79 %), with a mean
stated window of 3.7 days and a mean lead time of 45 hours**. A model that
cannot narrow the window below 3.7 days at comparable hit rate and lead time is
not adding anything. Only date ranges stated explicitly are captured; vaguer
statements ("likely to start in the next 24 hours") are left out rather than
converted into a window by guesswork, so this is a lower bound on HVO's
performance, not an upper one.

`python3 -m kilauea baseline` scores three repose-history-only baselines
walk-forward against the same quantity:

```
baseline       n    MAE h  median AE h   RMSE h   ±12h %   ±24h %   79% window (d)
median_5      51     81.1         46.0    111.9     19.6     33.3            11.68
last          51     88.7         72.2    119.9     21.6     31.4            12.29
trend_5       51    108.7         90.9    142.1     13.7     15.7            15.63
```

Repose history alone needs an ~11.7-day window to reach HVO's 79 % hit rate;
HVO does it in 3.7 days. That gap is the entire justification for carrying tilt
and seismicity in this database — it is where a model has to find its edge, and
it is measurable rather than assumed.

---

## Known caveats — read before modelling

**The episode table is preliminary.** USGS states that these values are
"unreviewed, preliminary estimates derived from rapid analyses ... not suitable
for research, quantitative analyses, or scientific publication." They are the
best public record of episode timing, and the collector re-fetches them daily so
revisions propagate, but any model inherits that caveat. The reviewed versions
are expected as formal USGS data releases later.

**Tilt is only comparable within a levelling segment.** HVO re-levels the
borehole tiltmeters periodically, which resets the absolute datum. Each sample
carries the source file as `segment`; a difference that straddles two segments
is meaningless. `tilt_hourly.n_segments > 1` flags the hours where a boundary
falls — 100 % of them should be excluded from any tilt-difference feature.

**Tilt comes from two places, and they are not interchangeable.** The
ScienceBase releases (`tilt_sample`, 1-minute instrument output) lag by about
six months. HVO also states summit tilt numerically in almost every daily
update, and `tilt_reading` mines those sentences: roughly one figure per station
per day, current to within a day, covering the whole episodic eruption. Those
are HVO's rounded and hedged numbers — "about 16.5 microradians" — so the
`qualifier` column keeps the hedging visible and they must never be pooled with
`tilt_sample`. `episode_source` records whether HVO named the episode or it was
inferred from the notice date.

**VONA onsets are a cross-check, not a better clock.** The aviation notices
parse exactly and carry minute-precision UTC times, but HVO does not use the
ONSET field consistently: the first message of an episode has twice stated a
time exactly four hours early (episodes 46 and 52), a CONTINUES message has
stated its own observation time instead (episode 51), and an ENDED message
states the end time for some episodes and the start time for others. The
episode table stays authoritative; `v_vona_episode` exposes each message type's
value separately so the disagreement is inspectable rather than averaged away.

**Gravity is raw.** Not corrected for solid Earth tides, which are roughly an
order of magnitude larger than the volcanic signal. Detide before use.

**GNSS is an independent record, not a better forecast input.** NGL publishes
daily positions for five stations within 6 km of the summit, in the
Pacific-plate-fixed frame; BYRL, CRIM and OUTL run 2–3 days behind, which is the
freshest deformation data available anywhere in this database. But the daily
solutions scatter 4–9 mm vertically while the inflation between episodes is only
10–25 mm, and the uplift rate correlates with repose duration at just
**r = +0.22** — against r = +0.87 for the tilt deflation. It is stored because
it is genuinely independent of the tiltmeters and may support a different
question; it does not currently enter the forecast.

Use `up_abs_m`, never `up_m`. NGL reports a position as an integer reference
plus an offset, and *the reference differs between the final and rapid series
for the same station* — CRIM steps by 1,000 mm at the July 2026 boundary if the
raw offsets are compared. `v_gnss_summit_daily` uses the absolute columns and
`validate` guards against the regression.

**Gravity has a few corrupt raw timestamps.** The 2010-2012 dataloggers were
not GPS-disciplined; hour buckets falling outside the instruments' operating
life are dropped on ingest and the count is logged.

**ComCat completeness varies.** The catalogue's magnitude of completeness
changes with network density across the 1959–present span; event counts are not
comparable decade to decade without a completeness correction.

**SO2 stops in 2022 in this build.** The 2023–2025 traverse release exists on
ScienceBase but its files are S3-backed and not yet publicly downloadable; the
collector detects this and skips them loudly rather than ingesting the HTML
error page. Re-run `collect so2` once USGS publishes them.

**`thermal_observation` is empty on purpose.** The published thermal-camera
releases contain image sequences and documentation, not a derived numeric time
series. The datasets are registered in `dataset` so the catalogue is complete,
but no values are synthesised from imagery. If a future release ships a derived
table, extend `kilauea/sources/thermal.py`.

**The 2018 release also ships 1 Hz tilt** covering the same window as the
1-minute series. It is skipped by default (~15 M redundant rows); pass
`--tilt-1sec` to ingest it.

---

## What is on disk

`data/` is a few gigabytes and only one of the pieces is irreplaceable-ish:

| Path | Size | If you delete it |
|---|---|---|
| `data/kilauea.db` | ~4.3 GB | rebuild with `collect all` (~40 min), or download the release |
| `data/kilauea_core.db` | 172 MB | `gunzip -k data/kilauea_core.db.gz`, or `core-db` |
| `data/kilauea_core.db.gz` | 38 MB | tracked in git |
| `data/raw/` | ~380 MB | ~1 GB of downloads on the next `collect all` or `update --full` |
| `data/digests/`, `briefs/`, `logs/` | small | regenerated; `daily_update.sh` already prunes them |

`python3 -m kilauea cache` prints the same breakdown for `data/raw` with live
numbers. `--prune` removes interrupted downloads — a `.part` file is never
resumed, so one left by a dropped connection is dead weight — and `--prune-all`
removes the cache entirely.

## Daily updates

`scripts/daily_update.sh` refreshes the sources that actually change day to day
(episode table, HANS notices, ComCat seismicity, HVO forecast windows), rebuilds
the views and runs the integrity checks. ComCat is re-fetched with a 7-day
overlap because magnitudes and locations are revised after the fact.

Install it on the machine that holds the database:

```
crontab -e
# then add:
PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin
30 5 * * *  cd ~/Developer/Kilauea && ./scripts/daily_update.sh        >> /tmp/kilauea.log 2>&1
0  4 * * 0  cd ~/Developer/Kilauea && ./scripts/daily_update.sh --full >> /tmp/kilauea.log 2>&1
```

The daily job runs at 05:30 so the brief context is already fresh when the
Cowork brief task fires at 06:00. The weekly `--full` run picks up new
ScienceBase releases; it does *not* re-walk the notice archive, because that
endpoint answers in about 75 seconds per page and 347 pages would be a
seven-hour job. Use `collect hans --since 1980-01-01` if you ever need a
complete sweep — on an empty table the collector does that automatically and
warns, because there is no other way to bootstrap.

On macOS, `cron` needs Full Disk Access for `/usr/sbin/cron` if the repository
lives under a protected folder (Documents, Desktop, iCloud Drive); a path under
`~/Developer` avoids that. `launchd` is the supported alternative if you prefer
it.

## The daily HTML brief

The brief is written in Japanese; `kilauea/brief.py` and
`briefs/BRIEF_PROMPT.md` are the only Japanese in the repository, and that is
deliberate — it is the author's own morning read, not project documentation.
Everything the database and the CLI emit is English.

The brief is split so that each half runs where it can:

* **`python3 -m kilauea brief-context`** does the deterministic half. It reads
  only the database and emits one JSON document with every number the brief
  states — alert level, episode actuals, summit tilt, seismicity, SO2, HVO's
  published window, park status — each carrying the sentence it was extracted
  from. Values that cannot be sourced come out as
  `{"value": null, "unavailable": "<理由>"}` rather than being omitted, so the
  writer has to say 未確認 and say why. `--record` also inserts a `brief_run`
  row, which `v_brief_skill` later scores against the actual onset.
* **A Cowork scheduled task** does the authored half. It reads that JSON and
  writes the prose, the layout and the SVG, then screenshots the result before
  sending. The full specification lives in `briefs/BRIEF_PROMPT.md`; the
  scheduled task's prompt is a condensed copy of it.

`scripts/daily_update.sh` runs the first half, so cron keeps
`briefs/context_latest.json` fresh. The scheduled task falls back to rebuilding
the context in its own container if the Mac is unreachable or the JSON is more
than 36 hours old.

Most of the numbers are mined from `alert_notice.body_text`, because HVO
publishes tilt, seismicity and gas figures as prose rather than as data. Two
parsing traps are handled explicitly and covered by tests: a period between
digits is a decimal point, not a sentence end (`12.1 microradians`), and a
single newline is a soft wrap, not a sentence end — splitting on either one
hides the number from every extractor downstream. Extraction is also scoped to
the notice's own section headers, so the boilerplate under *Resources* cannot be
mistaken for an observation.

### The forecast, and why it is built this way

`own_forecast` is recomputed identically every day from three reads.

The primary one falls out of `tilt_reading`: the summit deflates by a
measurable amount during each episode and then refills, and across the episodes
with a published figure, **repose duration correlates with that deflation at
r ≈ 0.87**. Fitting repose days against deflation on the last 14 episodes, and
bias-correcting from the model's own walk-forward residuals, gives an estimate
the moment an episode ends. Backtested from episode 40 onward, refitting at
every step:

| method | MAE (days) | median error | within ±2 d |
|---|---|---|---|
| deflation model, bias-corrected | **2.32** | −0.40 | 8/11 |
| deflation model, raw | 2.76 | −1.60 | 7/11 |
| tilt recovery, bias-corrected | 3.40 | +0.81 | 4/11 |
| repose median | 4.94 | +2.11 | 1/11 |
| tilt recovery, raw | 4.72 | −3.65 | 1/11 |

The window is that MAE either side of the point estimate. The other two reads
are reported beside it because they fail in opposite directions — repose median
runs late, tilt recovery runs early — and their disagreement is itself worth
seeing. The tilt-recovery date is exposed as an early-risk flag rather than
folded into the window: it runs early by construction, because inflation
decelerates and the deflationary excursions HVO describes subtract from the
total.

Note what the comparison with HVO is and is not. HVO's published windows are
issued a mean 45 hours ahead; this model predicts at episode end, roughly two
weeks ahead. A 2.3-day MAE at that lead is not the same achievement as a
3.7-day window at 45 hours, and the two should not be read as a scoreboard.

### `scripts/daily_digest.py`

A standalone status report that needs no database — it fetches the episode table
and the latest HVO notice and prints the current alert level, the last episode,
elapsed time since its pause, and HVO's stated forecast window. Useful for a
scheduled check from a machine that does not hold the database, and for
verifying that the upstream page layouts have not changed:

```
python3 scripts/daily_digest.py            # human-readable
python3 scripts/daily_digest.py --json     # machine-readable
```

---

## Layout

```
kilauea/
  config.py          paths, endpoints, pinned ScienceBase item IDs
  db.py              connection, upsert, run logging
  http.py            retrying session + on-disk download cache
  schema.sql         physical schema
  views.sql          analysis views (rebuilt on every run)
  forecast.py        mines HVO's published onset windows from notice text
  baseline.py        reference baselines for the onset target
  validate.py        integrity checks
  cli.py             command line
  sources/           one module per data source
  brief.py           the daily brief's data context, built from SQL only
  sources/tilt_notice.py  summit tilt mined from notice prose (no network)
  sources/vona.py         aviation notices parsed from their telex format
  sources/park.py         NPS closures and eruption viewing
  sources/gnss.py         NGL daily GNSS positions (HTTPS only; port 80 is closed)
scripts/cut_release.sh     derive the core archive and publish a full-database release
scripts/daily_update.sh    database refresh + brief context (cron)
scripts/daily_digest.py    standalone status report (no database)
briefs/BRIEF_PROMPT.md     full specification for the HTML brief
briefs/context_latest.json written by daily_update.sh, read by the brief task
tests/test_parsing.py
```

Each collector is independent: a failure in one is logged to `source_run` and
does not abort the others, and `python3 -m kilauea status` shows the last
outcome per source.

## Sources

* Smithsonian Institution, Global Volcanism Program — Volcanoes of the World
  https://volcano.si.edu/volcano.cfm?vn=332010
* USGS Hawaiian Volcano Observatory — Kīlauea eruption information
  https://www.usgs.gov/volcanoes/kilauea/science/eruption-information
* USGS Hazard Notification System
  https://volcanoes.usgs.gov/hans-public/search/
* USGS ANSS Comprehensive Catalog (FDSN event service)
  https://earthquake.usgs.gov/fdsnws/event/1/
* USGS ScienceBase data releases (tiltmeter, SO2, plume height, gravity)
  https://www.sciencebase.gov/catalog/
* Hawaiʻi Volcanoes National Park — alerts and eruption viewing
  https://www.nps.gov/havo/planyourvisit/conditions.htm
* Nevada Geodetic Laboratory — daily GNSS position time series
  https://geodesy.unr.edu/ (cite Blewitt et al. 2018, Eos 99)

USGS material is public domain. GVP data is subject to the Global Volcanism
Program terms of use and should be cited as such in any publication.

---

## Licence

The code is MIT-licensed — see `LICENSE`. The data is not the author's to
license: it is redistributed here under the terms of the sources listed above,
which must be cited directly in any publication rather than via this repository.

---

## Running it from the Cowork device bridge

The database and collectors are meant to run in your own Terminal on the Mac.
Inside Cowork's device VM two limits apply:

* **No network.** `collect` and `update` cannot run there — they need USGS and
  Smithsonian. Run them from the Mac's Terminal.
* **No SQLite writes on the mounted folder.** The mount cannot host a journal
  file, so any write fails with `disk I/O error`. The reporting commands
  (`status`, `validate`, `baseline`) open the database read-only and work fine.

If a write was attempted anyway and left a stale `kilauea_core.db-journal` next
to the database, SQLite will refuse to open it read-only (it wants to roll the
journal back, which is a write). Delete that journal file and it opens again.
