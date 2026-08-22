"""Derive the shipped core database from a full build.

The repository ships `data/kilauea_core.db.gz`: the full database minus the two
bulk time series, small enough to distribute. It used to be produced by hand,
which meant the shipped copy and the full build drifted apart with nothing to
say by how much. This module makes the derivation a command, so "core" has one
definition and any full build reproduces the same core.

The definition is exactly this: copy the database, delete the two bulk series,
VACUUM, and check that nothing else moved.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

log = logging.getLogger("kilauea.core")

# (table, WHERE clause identifying the bulk rows, human description)
#
# The 1-minute tiltmeter series is the whole of `tilt_sample`; `tilt_hourly`
# is the aggregate that the feature views actually read, and it stays.
#
# `so2_emission` holds two very different things: a few thousand traverse and
# daily-mean figures, and the 10-second FLYSPEC array stream that accounts for
# the row count. Only the stream goes.
BULK_SERIES = (
    ("tilt_sample", "1", "one-minute borehole tiltmeter samples"),
    ("so2_emission",
     "aggregation = 'individual' AND method = 'FLYSPEC array'",
     "the 10-second SO2 stream"),
)


def _drop_sidecars(path) -> None:
    """Remove the -wal/-shm files SQLite may leave beside ``path``."""
    for suffix in ("-wal", "-shm", "-journal"):
        side = Path(str(path) + suffix)
        if side.exists():
            side.unlink()


def _counts(conn) -> dict:
    """Row count per table, so the derivation can prove it touched nothing else."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            for t in tables}


def derive(src_conn, dest_path, *, force: bool = False) -> int:
    """Write the core database derived from the connected full build.

    ``src_conn`` is only read from. Returns a process exit code.
    """
    dest = Path(dest_path)
    if dest.exists() and not force:
        raise SystemExit(f"{dest} exists; pass --force to overwrite it")
    dest.parent.mkdir(parents=True, exist_ok=True)

    before = _counts(src_conn)
    missing = [t for t, _, _ in BULK_SERIES if t not in before]
    if missing:
        raise SystemExit(
            "not a full build: no %s table. Point --db at the database that "
            "`collect all` produced." % ", ".join(missing))

    # Write beside the target and move into place, so an interrupted run cannot
    # leave a half-built database where a good one used to be.
    tmp = dest.with_name(dest.name + ".partial")
    if tmp.exists():
        tmp.unlink()
    _drop_sidecars(tmp)

    out = sqlite3.connect(str(tmp))
    try:
        src_conn.backup(out)
        for table, where, what in BULK_SERIES:
            n = out.execute(
                "SELECT COUNT(*) FROM %s WHERE %s" % (table, where)).fetchone()[0]
            log.info("core-db: dropping %s rows from %s (%s)", f"{n:,}", table, what)
            out.execute("DELETE FROM %s WHERE %s" % (table, where))
        out.commit()
        # Ship a single file. The source runs in WAL mode and the copy inherits
        # it, which means a -wal and a -shm beside the database and a reader who
        # cannot open it read-only without write access to the directory. A
        # rollback-journal database is one file and opens read-only anywhere.
        out.execute("PRAGMA journal_mode=DELETE")
        out.execute("VACUUM")
        after = _counts(out)
    finally:
        out.close()
    _drop_sidecars(tmp)

    # Every table that is not one of the two must come through untouched. This
    # is the check that makes the shipped database trustworthy: a stray join or
    # a cascading delete would show up here rather than in someone's analysis.
    expected = dict(before)
    for table, where, _ in BULK_SERIES:
        expected[table] = src_conn.execute(
            "SELECT COUNT(*) FROM %s WHERE NOT (%s)" % (table, where)).fetchone()[0]
    drifted = {t: (expected[t], after.get(t))
               for t in expected if after.get(t) != expected[t]}
    if drifted:
        tmp.unlink(missing_ok=True)
        _drop_sidecars(tmp)
        raise SystemExit(
            "derivation changed tables it should not have: "
            + ", ".join(f"{t} expected {e:,} got {g:,}"
                        for t, (e, g) in sorted(drifted.items())))

    os.replace(tmp, dest)
    # A -wal/-shm left over from an earlier database at this path would be read
    # as belonging to the new one.
    _drop_sidecars(dest)

    src_bytes = sum(
        Path(p).stat().st_size for p in [src_conn.execute(
            "SELECT file FROM pragma_database_list WHERE name='main'"
        ).fetchone()[0]] if p and Path(p).exists())
    print("wrote %s" % dest)
    for table, where, what in BULK_SERIES:
        print("  dropped %12s rows from %-14s (%s)"
              % (f"{before[table] - expected[table]:,}", table, what))
    print("  %d tables carried through unchanged" % (len(expected) - len(BULK_SERIES)))
    if src_bytes:
        print("  %.0f MB -> %.0f MB" % (src_bytes / 1048576, dest.stat().st_size / 1048576))
    return 0
