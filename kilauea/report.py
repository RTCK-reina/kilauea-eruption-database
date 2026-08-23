"""Text renderings of what is in the database.

`status` and the release notes both describe the same thing — which tables the
build carries and how far they reach — so they render it from the same view and
the same function. The release notes for the 2026-08-22 snapshot were built by
scraping `status` output with awk, which is exactly the kind of second copy that
goes stale without telling anyone.
"""
from __future__ import annotations


def coverage_table(conn) -> str:
    """The `v_coverage` view as an aligned text table."""
    rows = conn.execute("SELECT tbl, n, t0, t1 FROM v_coverage").fetchall()
    if not rows:
        return "(no coverage view; run `python3 -m kilauea init`)"
    width = max(len(r["tbl"]) for r in rows)
    out = [f"{'table'.ljust(width)}  {'rows':>12}  coverage"]
    for r in rows:
        span = f"{r['t0'] or '-'} .. {r['t1'] or '-'}" if r["n"] else "(empty)"
        out.append(f"{r['tbl'].ljust(width)}  {r['n']:>12,}  {span}")
    return "\n".join(out)


def release_notes(conn, *, tag: str, repo: str, full_sha: str, core_sha: str,
                  full_bytes: int, gz_bytes: int, part_sums: str,
                  part_bytes: int) -> str:
    """The body of a full-database release.

    Everything a reader needs to check what they downloaded, and to know how the
    archive committed to the repository relates to it.
    """
    date = tag.replace("db-", "")
    return f"""\
The full `data/kilauea.db` build: everything the collectors produce, including
the two bulk time series the in-repository core build leaves out — `tilt_sample`
(one-minute borehole tiltmeter samples) and the 10-second SO2 stream.

Snapshot taken {date}. {full_bytes / 1e9:.2f} GB uncompressed, {gz_bytes / 1e6:.0f} MB gzipped, split into
{part_bytes // (1024 * 1024)} MB parts so a failed transfer costs one part rather than the whole file.

`data/kilauea_core.db.gz` in this tag's source tree is derived from **this exact
build** — `python3 -m kilauea core-db` on the database below — so the two are the
same data as of the same instant, and the difference between them is only the
two series named above.

    kilauea.db      sha256 {full_sha}
    kilauea_core.db sha256 {core_sha}

## Restore

```
gh release download -R {repo} \\
    -p 'kilauea.db.gz.part*' -p 'SHA256SUMS.txt'
shasum -a 256 -c SHA256SUMS.txt
cat kilauea.db.gz.part* | gunzip > data/kilauea.db
sqlite3 data/kilauea.db 'PRAGMA quick_check;'
python3 -m kilauea status --db data/kilauea.db
```

`python3 -m kilauea update` carries the snapshot forward incrementally, so there
is no need to re-download after this date.

## SHA-256 of the parts

```
{part_sums.strip()}
```

## Contents

```
{coverage_table(conn)}
```

Sources are public USGS (HVO, HANS, ComCat, ScienceBase), Smithsonian GVP, NPS
and Nevada Geodetic Laboratory data — see the README for citation requirements.
"""
