"""Command line entry point.

    python -m kilauea init                 create the database and views
    python -m kilauea collect all          run every collector
    python -m kilauea collect quakes tilt  run selected collectors
    python -m kilauea update               daily incremental refresh
    python -m kilauea views                rebuild analysis views
    python -m kilauea validate             data integrity report
    python -m kilauea status               row counts and coverage
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time

from . import baseline, brief, config, db, forecast, validate as validate_mod
from .sources import (episodes, gnss, gravity, gvp, hans, park, plume, quakes,
                      so2, thermal, tilt, tilt_notice, vona)

COLLECTORS = {
    "gvp": gvp.collect,
    "episodes": episodes.collect,
    "hans": hans.collect,
    "quakes": quakes.collect,
    "tilt": tilt.collect,
    "so2": so2.collect,
    "plume": plume.collect,
    "gravity": gravity.collect,
    "thermal": thermal.collect,
    "park": park.collect,
    "gnss": gnss.collect,
    # Derived from alert_notice prose rather than fetched.
    "tilt_notice": tilt_notice.collect,
    "vona": vona.collect,
    # Derived from alert_notice text rather than fetched; listed here so it can
    # be re-run on its own and so it shows up in `status`.
    "hvo_forecast": forecast.extract,
}

# Cheap sources that genuinely change day to day.
DAILY = ["episodes", "hans", "quakes", "park", "gnss", "tilt_notice", "vona",
         "hvo_forecast"]


def _check_runtime() -> None:
    """Fail fast on an environment that cannot run the schema.

    The views use SQL window functions (SQLite 3.28+) and the upserts use
    ON CONFLICT DO UPDATE (3.24+). A silent syntax error deep in a collector is
    much harder to diagnose than this. Python 3.9 is the floor because that is
    what ships with macOS and what an unattended cron job will actually use.
    """
    if sys.version_info < (3, 9):
        raise SystemExit(
            f"Python 3.9+ required, found {sys.version.split()[0]}")
    ver = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
    if ver < (3, 28, 0):
        raise SystemExit(
            f"SQLite 3.28+ required for the window functions in views.sql, "
            f"found {sqlite3.sqlite_version}")


def _setup_logging(verbose: bool) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stderr),
                logging.FileHandler(config.LOG_DIR / "kilauea.log")]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _run(conn, names, **opts) -> int:
    failures = 0
    for name in names:
        fn = COLLECTORS[name]
        started = time.time()
        logging.info("=== %s ===", name)
        try:
            fn(conn, **opts)
        except Exception as exc:  # noqa: BLE001 - one bad source must not sink the run
            failures += 1
            logging.exception("collector %s failed: %s", name, exc)
        else:
            logging.info("%s finished in %.1fs", name, time.time() - started)
    return failures


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kilauea", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--db", help="override the database path")

    # Shared so that both `kilauea --db X status` and `kilauea status --db X`
    # work; argparse otherwise only accepts the flag before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS so that an absent flag on the subparser does not clobber a value
    # already parsed by the main parser.
    common.add_argument("--db", default=argparse.SUPPRESS,
                        help="override the database path")

    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", parents=[common], help="create schema and views")

    c = sub.add_parser("collect", parents=[common], help="run collectors")
    c.add_argument("sources", nargs="+",
                   help="'all' or any of: " + ", ".join(COLLECTORS))
    c.add_argument("--since", help="YYYY-MM-DD lower bound (hans, quakes)")
    c.add_argument("--keep-archives", action="store_true",
                   help="keep downloaded gravity archives instead of deleting them")
    c.add_argument("--tilt-1sec", action="store_true",
                   help="also ingest the 2018 release's 1 Hz tilt files "
                        "(~15M extra rows, redundant with the 1-minute series)")

    u = sub.add_parser("update", parents=[common], help="incremental daily refresh")
    u.add_argument("--full", action="store_true",
                   help="run every source, including the slow ScienceBase ones "
                        "(the notice archive stays incremental either way)")

    sub.add_parser("views", parents=[common], help="rebuild analysis views")
    sub.add_parser("status", parents=[common], help="print row counts and time coverage")

    b = sub.add_parser("baseline", parents=[common],
                       help="score reference baselines for the onset target")
    b.add_argument("-k", type=int, default=5,
                   help="window length for the median/trend baselines")

    bc = sub.add_parser("brief-context", parents=[common],
                        help="emit the daily brief's data context as JSON")
    bc.add_argument("-o", "--out", help="write the JSON here as well as to stdout")
    bc.add_argument("--record", action="store_true",
                    help="also insert a brief_run row for later scoring")

    v = sub.add_parser("validate", parents=[common], help="run data integrity checks")
    v.add_argument("--strict", action="store_true",
                   help="exit non-zero when any check fails")

    args = ap.parse_args(argv)
    _check_runtime()
    _setup_logging(args.verbose)

    from pathlib import Path
    db_path = getattr(args, "db", None)
    if db_path:
        config.DB_PATH = Path(db_path)

    # Reporting commands must not conjure an empty database when the path is
    # wrong - that turns a typo into a silent "0 rows" answer.
    read_only = args.cmd in {"status", "validate", "baseline"} or (
        args.cmd == "brief-context" and not args.record)
    if read_only or args.cmd in {"views", "brief-context"}:
        if not Path(config.DB_PATH).exists():
            raise SystemExit(
                f"database not found: {config.DB_PATH}\n"
                f"run `python3 -m kilauea collect all` to build it, or pass "
                f"--db with the correct path")

    conn = db.connect(db_path, read_only=read_only)
    if not read_only:
        db.init(conn)

    try:
        if args.cmd == "init":
            db.build_views(conn)
            print(f"initialised {config.DB_PATH}")
            return 0

        if args.cmd == "collect":
            names = list(COLLECTORS) if "all" in args.sources else args.sources
            unknown = [n for n in names if n not in COLLECTORS]
            if unknown:
                ap.error(f"unknown source(s): {', '.join(unknown)}")
            # Not full=True: `collect all` on a fresh database would otherwise
            # walk the whole notice archive, which is 347 pages at ~75 s each
            # from a home connection. Pass --since 1980-01-01 to force it.
            failures = _run(conn, names, since=args.since,
                            keep_archives=args.keep_archives,
                            include_1sec=args.tilt_1sec)
            db.build_views(conn)
            print_status(conn)
            return 1 if failures else 0

        if args.cmd == "update":
            # --full widens the source list; it does NOT re-walk the whole
            # notice archive. From this machine the HANS POST endpoint answers
            # in ~75 s, so 347 pages would be a seven-hour job every Sunday.
            # Use `collect hans --since 1980-01-01` for a complete sweep.
            names = list(COLLECTORS) if args.full else DAILY
            failures = _run(conn, names)
            db.build_views(conn)
            print_status(conn)
            return 1 if failures else 0

        if args.cmd == "views":
            db.build_views(conn)
            print("views rebuilt")
            return 0

        if args.cmd == "brief-context":
            import json as _json
            ctx = brief.build_context(conn)
            if args.record:
                _record_brief(conn, ctx)
            text = _json.dumps(ctx, ensure_ascii=False, indent=2)
            if args.out:
                from pathlib import Path as _P
                _P(args.out).parent.mkdir(parents=True, exist_ok=True)
                _P(args.out).write_text(text + "\n", encoding="utf-8")
            print(text)
            return 0

        if args.cmd == "baseline":
            print(baseline.run(conn, k=args.k))
            return 0

        if args.cmd == "status":
            print_status(conn)
            return 0

        if args.cmd == "validate":
            report = validate_mod.run(conn)
            print(validate_mod.render(report))
            failed = [r for r in report if r["status"] == "FAIL"]
            return 1 if (failed and args.strict) else 0
    finally:
        if not read_only:
            conn.execute("PRAGMA optimize")
        conn.close()
    return 0


def _record_brief(conn, ctx: dict) -> None:
    """Persist the context's headline numbers so the brief can be scored later."""
    ep = ctx.get("episode") or {}
    tilt = ctx.get("tilt") or {}
    fc = ctx.get("hvo_forecast") or {}
    own = ctx.get("own_forecast") or {}
    so2 = (ctx.get("so2") or {}).get("measured") or {}
    row = dict(
        generated_utc=ctx["generated"]["utc"],
        generated_hst=ctx["generated"]["hst"],
        source_notice_id=(ctx.get("source_notice") or {}).get("notice_id"),
        source_notice_hst=(ctx.get("source_notice") or {}).get("sent_hst"),
        alert_level=(ctx.get("alert") or {}).get("level", {}).get("value"),
        color_code=(ctx.get("alert") or {}).get("color_code", {}).get("value"),
        is_erupting=1 if ep.get("is_erupting") else 0,
        last_episode_no=ep.get("number"),
        hours_since_pause=ep.get("hours_since_pause"),
        summit_tilt_station=tilt.get("station", {}).get("value"),
        tilt_cumulative_urad=tilt.get("cumulative_urad", {}).get("value"),
        tilt_24h_urad=tilt.get("change_24h_urad", {}).get("value"),
        tilt_episode_deflation_urad=tilt.get("episode_deflation_urad", {}).get("value"),
        summit_eq_24h=(ctx.get("earthquakes_summit_24h_hvo") or {}).get("value"),
        so2_tpd=so2.get("value"),
        hvo_window_start=fc.get("window_start"),
        hvo_window_end=fc.get("window_end"),
        brief_window_start=own.get("window_start"),
        brief_window_end=own.get("window_end"),
        brief_point_date=own.get("point_date"),
    )
    with db.tx(conn):
        db.upsert(conn, "brief_run", [row], conflict=["generated_utc"])


def print_status(conn) -> None:
    print(f"\ndatabase: {config.DB_PATH}")
    rows = conn.execute("SELECT tbl, n, t0, t1 FROM v_coverage").fetchall()
    width = max(len(r["tbl"]) for r in rows)
    print(f"{'table'.ljust(width)}  {'rows':>12}  coverage")
    for r in rows:
        span = f"{r['t0'] or '-'} .. {r['t1'] or '-'}" if r["n"] else "(empty)"
        print(f"{r['tbl'].ljust(width)}  {r['n']:>12,}  {span}")

    last = conn.execute(
        """SELECT source, status, started_at, rows_seen, rows_written, message
           FROM source_run WHERE id IN (
               SELECT MAX(id) FROM source_run GROUP BY source)
           ORDER BY source"""
    ).fetchall()
    if last:
        print("\nlast run per source:")
        for r in last:
            note = f"  {r['message'][:70]}" if r["message"] else ""
            print(f"  {r['source']:<14} {r['status']:<6} {r['started_at']}  "
                  f"seen={r['rows_seen']:>9,} written={r['rows_written']:>9,}{note}")


if __name__ == "__main__":
    raise SystemExit(main())
