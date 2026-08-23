"""The raw download cache under `data/raw/`.

The collectors keep every ScienceBase archive they fetch, keyed by a hash of the
URL, so a re-run never refetches ~1 GB. That is the right default and it is also
the reason `data/` is several gigabytes with nothing in the repository able to
say what the gigabytes are. This module answers that, and offers the two safe
things to delete: interrupted downloads, and the cache as a whole.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import config

# An interrupted download leaves a .part file. `cached_download` starts a fresh
# one rather than resuming, so a .part is never read again -- but one being
# written right now is in use, hence the age floor.
PARTIAL_SUFFIX = ".part"
PARTIAL_MIN_AGE_S = 3600


def _tree(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file():
            yield p


def summary(root=None) -> dict:
    """Sizes and file counts per collector, plus any interrupted downloads."""
    root = Path(root) if root else config.RAW_DIR
    if not root.exists():
        return {"root": root, "groups": [], "total_bytes": 0, "partials": []}

    groups, partials, total = [], [], 0
    now = time.time()
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        size = count = 0
        for f in _tree(child):
            size += f.stat().st_size
            count += 1
        groups.append({"name": child.name, "bytes": size, "files": count})
        total += size
    for f in _tree(root):
        if f.name.endswith(PARTIAL_SUFFIX):
            st = f.stat()
            partials.append({"path": f, "bytes": st.st_size,
                             "age_s": now - st.st_mtime})
    return {"root": root, "groups": groups, "total_bytes": total,
            "partials": partials}


def _mb(n: int) -> str:
    return f"{n / 1048576:,.1f} MB"


def render(info: dict) -> str:
    out = [f"raw download cache: {info['root']}"]
    if not info["groups"]:
        out.append("  (empty)")
    for g in info["groups"]:
        out.append(f"  {g['name']:<10} {_mb(g['bytes']):>12}  {g['files']:>3} files")
    out.append(f"  {'total':<10} {_mb(info['total_bytes']):>12}")
    if info["partials"]:
        out.append("")
        out.append("interrupted downloads (never resumed; --prune removes them):")
        for p in info["partials"]:
            out.append(f"  {_mb(p['bytes']):>12}  {p['age_s'] / 3600:>6.1f} h old  "
                       f"{p['path'].name}")
    out.append("")
    out.append("Everything here is a verbatim copy of a public upstream file and can "
               "be refetched.")
    out.append("Deleting it costs about 1 GB of downloads on the next "
               "`collect all` or `update --full`;")
    out.append("it does not affect an already-built database.")
    return "\n".join(out)


def prune_partials(root=None, *, min_age_s: int = PARTIAL_MIN_AGE_S) -> list:
    """Delete interrupted downloads older than ``min_age_s``. Returns what went."""
    info = summary(root)
    gone = []
    for p in info["partials"]:
        if p["age_s"] >= min_age_s:
            p["path"].unlink()
            gone.append(p)
    return gone


def prune_all(root=None) -> int:
    """Delete the whole cache. Returns the bytes freed."""
    import shutil

    root = Path(root) if root else config.RAW_DIR
    freed = summary(root)["total_bytes"]
    for child in sorted(root.iterdir()) if root.exists() else []:
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    return freed
