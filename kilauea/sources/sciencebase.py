"""Shared helpers for USGS ScienceBase data releases."""
from __future__ import annotations

import logging
from typing import Iterator

from .. import config
from ..http import get

log = logging.getLogger(__name__)


def item(item_id: str) -> dict:
    return get(f"{config.SCIENCEBASE_ITEM}/{item_id}", params={"format": "json"}, timeout=180).json()


def files(item_id: str, *, suffixes: tuple[str, ...] = (), max_bytes: int | None = None) -> Iterator[dict]:
    """Yield file records for a ScienceBase item.

    ``max_bytes`` filters out the multi-gigabyte raw spectra / imagery bundles
    that ship alongside the derived time series in several HVO releases.
    """
    meta = item(item_id)
    seen = set()
    buckets = [meta.get("files") or []]
    buckets += [f.get("files") or [] for f in meta.get("facets") or []]
    for bucket in buckets:
        for f in bucket:
            name = f.get("name") or ""
            if name in seen:
                continue
            if suffixes and not name.lower().endswith(suffixes):
                continue
            size = f.get("size") or 0
            if max_bytes and size > max_bytes:
                log.info("sciencebase: skipping %s (%.1f GB > limit)", name, size / 1e9)
                continue
            if not f.get("url"):
                continue
            seen.add(name)
            yield {**f, "_item_id": item_id, "_item_title": meta.get("title")}


def dataset_record(item_id: str, meta: dict | None = None) -> dict:
    meta = meta or item(item_id)
    doi = None
    for ident in meta.get("identifiers") or []:
        if (ident.get("type") or "").lower() == "doi":
            doi = ident.get("key")
    dates = {d.get("type"): d.get("dateString") for d in meta.get("dates") or []}
    return dict(
        key=f"sb:{item_id}",
        title=meta.get("title"),
        publisher="U.S. Geological Survey (ScienceBase)",
        url=f"https://www.sciencebase.gov/catalog/item/{item_id}",
        doi=doi,
        period_start=dates.get("Start") or dates.get("start"),
        period_end=dates.get("End") or dates.get("end"),
        license="USGS public domain",
    )
