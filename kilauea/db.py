"""SQLite connection management, schema bootstrap and upsert helpers."""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Sequence

from . import config

log = logging.getLogger(__name__)


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _try_pragma(conn: sqlite3.Connection, pragma: str, *, critical: bool = False) -> bool:
    """Apply a PRAGMA, tolerating filesystems that reject it.

    Network shares, some FUSE mounts and iCloud Drive cannot host a WAL
    shared-memory file and raise "disk I/O error" on journal-mode changes. The
    database is still perfectly usable in whatever mode it already has, so a
    rejected tuning pragma is a warning, not a failure.
    """
    try:
        conn.execute(f"PRAGMA {pragma}")
        return True
    except sqlite3.OperationalError as exc:
        (log.error if critical else log.debug)(
            "PRAGMA %s rejected by the filesystem (%s)", pragma, exc)
        return False


def connect(path: Path | str | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the database.

    ``read_only`` opens with SQLite's ro URI mode, which needs no journal file
    and no write lock. That matters on mounts that cannot host SQLite writes at
    all (the Cowork device bridge, network shares): reporting commands still
    work there even though collection does not.
    """
    path = Path(path or config.DB_PATH)
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        _try_pragma(conn, "temp_store=MEMORY")
        _try_pragma(conn, "cache_size=-200000")
        return conn

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row

    # Prefer WAL; fall back through the rollback journals; if none can be set,
    # keep the mode the file already has rather than refusing to open it.
    for mode in ("WAL", "TRUNCATE", "DELETE"):
        if _try_pragma(conn, f"journal_mode={mode}"):
            if mode != "WAL":
                log.warning(
                    "WAL is unavailable on %s; using journal_mode=%s. Writes "
                    "will be slower - put the database on a local disk if that "
                    "matters.", path, mode)
            break
    else:
        log.warning("could not set a journal mode on %s; using the file's "
                    "existing mode", path)

    _try_pragma(conn, "synchronous=NORMAL")
    _try_pragma(conn, "foreign_keys=ON", critical=True)
    _try_pragma(conn, "temp_store=MEMORY")
    _try_pragma(conn, "cache_size=-200000")  # ~200 MB page cache
    return conn


def _reference_schema() -> dict[str, list[sqlite3.Row]]:
    """Authoritative column list per table, read from SQLite itself.

    Built by executing schema.sql into a throwaway in-memory database and
    reading PRAGMA table_info. Hand-parsing the DDL is not worth it: table
    constraints like ``UNIQUE(a, b)`` and inline ``REFERENCES t(col)`` both
    contain commas and parentheses, and getting that wrong hands ALTER TABLE a
    fragment of a constraint as a column name.
    """
    ref = sqlite3.connect(":memory:")
    ref.row_factory = sqlite3.Row
    try:
        ref.executescript(config.SCHEMA_SQL.read_text())
        tables = [r[0] for r in ref.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        return {t: list(ref.execute(f"PRAGMA table_info({t})")) for t in tables}
    finally:
        ref.close()


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Add columns that schema.sql declares but an existing database lacks.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op on a table that already exists, so
    without this a schema change would apply only to databases built from
    scratch and an older one would fail later with a confusing "no such
    column". SQLite's ALTER TABLE ADD COLUMN covers the additive case, which is
    the only kind of change this schema makes.
    """
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    applied = []
    for table, columns in _reference_schema().items():
        if table not in existing:
            continue
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in columns:
            name = col["name"]
            if name in have:
                continue
            if col["pk"]:
                log.warning("migrate: cannot add primary-key column %s.%s to an "
                            "existing table; rebuild it", table, name)
                continue
            spec = col["type"] or "TEXT"
            if col["dflt_value"] is not None:
                spec += f" DEFAULT {col['dflt_value']}"
            elif col["notnull"]:
                # A NOT NULL column needs a default to be addable; leaving it
                # nullable on the live table is better than refusing to migrate.
                log.info("migrate: adding %s.%s as nullable (declared NOT NULL, "
                         "no default)", table, name)
            conn.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {spec}')
            applied.append(f"{table}.{name}")
    if applied:
        log.info("migrate: added %d column(s): %s", len(applied), ", ".join(applied))
    return applied


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(config.SCHEMA_SQL.read_text())
    migrate(conn)
    conn.execute(
        """INSERT INTO volcano(vnum, name, latitude, longitude, elevation_m,
                               observatory, region)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(vnum) DO UPDATE SET name=excluded.name""",
        (
            config.VNUM,
            "Kilauea",
            config.SUMMIT_LAT,
            config.SUMMIT_LON,
            config.SUMMIT_ELEV_M,
            "Hawaiian Volcano Observatory",
            "Hawaii",
        ),
    )


def build_views(conn: sqlite3.Connection) -> None:
    conn.executescript(config.VIEWS_SQL.read_text())


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def upsert(
    conn: sqlite3.Connection,
    table: str,
    rows: Sequence[dict],
    *,
    conflict: Iterable[str] | None = None,
    update: bool = True,
    chunk: int = 5000,
) -> int:
    """Insert ``rows`` into ``table``, updating on conflict.

    Returns the number of rows submitted (SQLite's ``total_changes`` counts
    updates too, so this is the honest number to report as "rows processed"
    rather than "rows newly inserted"; the caller compares table counts before
    and after when it needs the true delta.
    """
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ",".join("?" * len(cols))
    collist = ",".join(f'"{c}"' for c in cols)

    if conflict:
        conflict_cols = ",".join(f'"{c}"' for c in conflict)
        if update:
            setters = ",".join(
                f'"{c}"=excluded."{c}"' for c in cols if c not in set(conflict)
            )
            tail = (
                f" ON CONFLICT({conflict_cols}) DO UPDATE SET {setters}"
                if setters
                else f" ON CONFLICT({conflict_cols}) DO NOTHING"
            )
        else:
            tail = f" ON CONFLICT({conflict_cols}) DO NOTHING"
    else:
        tail = " ON CONFLICT DO NOTHING" if not update else ""

    sql = f"INSERT INTO {table} ({collist}) VALUES ({placeholders}){tail}"

    n = 0
    for i in range(0, len(rows), chunk):
        batch = rows[i : i + chunk]
        conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in batch])
        n += len(batch)
    return n


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class Run:
    """Context manager recording a collection run in ``source_run``."""

    def __init__(self, conn: sqlite3.Connection, source: str, table: str | None = None):
        self.conn = conn
        self.source = source
        self.table = table
        self.id: int | None = None
        self.rows_seen = 0
        self._before = 0

    def __enter__(self) -> "Run":
        cur = self.conn.execute(
            "INSERT INTO source_run(source, started_at, status) VALUES (?,?, 'running')",
            (self.source, utcnow()),
        )
        self.id = cur.lastrowid
        if self.table:
            self._before = count(self.conn, self.table)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        written = (count(self.conn, self.table) - self._before) if self.table else 0
        if exc:
            self.conn.execute(
                """UPDATE source_run SET finished_at=?, status='error', message=?,
                       rows_seen=?, rows_written=? WHERE id=?""",
                (utcnow(), f"{type(exc).__name__}: {exc}"[:2000], self.rows_seen, written, self.id),
            )
            log.error("source %s failed: %s", self.source, exc)
        else:
            self.conn.execute(
                """UPDATE source_run SET finished_at=?, status='ok',
                       rows_seen=?, rows_written=? WHERE id=?""",
                (utcnow(), self.rows_seen, written, self.id),
            )
            log.info(
                "source %s: %d seen, %+d rows in %s",
                self.source, self.rows_seen, written, self.table or "-",
            )
        return False


def register_dataset(conn: sqlite3.Connection, **kw) -> None:
    kw.setdefault("retrieved_at", utcnow())
    upsert(conn, "dataset", [kw], conflict=["key"])
