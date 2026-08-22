"""USGS ScienceBase — thermal camera releases (metadata only).

The published thermal-camera releases for the Kīlauea summit contain image
sequences and documentation, not a derived numeric time series. There is
therefore nothing honest to load into ``thermal_observation``: this collector
registers the datasets and their child items so the catalogue is complete, and
records explicitly that no time series was ingested.

If a future release ships a derived table (max radiant temperature, lava-lake
level), extend ``_TABLE_SUFFIXES`` and the parser below rather than inventing
values from the imagery.
"""
from __future__ import annotations

import logging

from .. import config, db
from ..http import get
from . import sciencebase

log = logging.getLogger(__name__)

_TABLE_SUFFIXES = (".csv", ".txt")


def _children(item_id: str) -> list[dict]:
    try:
        resp = get(
            config.SCIENCEBASE_ITEMS,
            params={"parentId": item_id, "format": "json", "max": 100,
                    "fields": "title,id,dates"},
            timeout=120,
        ).json()
    except Exception as exc:  # noqa: BLE001
        log.warning("thermal: child lookup failed for %s (%s)", item_id, exc)
        return []
    return resp.get("items", [])


def collect(conn, *, item_id: str | None = None, **_) -> None:
    item_id = item_id or config.SB_THERMAL_ITEM
    with db.Run(conn, "thermal", "thermal_observation") as run:
        meta = sciencebase.item(item_id)
        with db.tx(conn):
            db.register_dataset(conn, **sciencebase.dataset_record(item_id, meta))
            for child in _children(item_id):
                db.register_dataset(
                    conn,
                    key=f"sb:{child['id']}",
                    title=child.get("title"),
                    publisher="U.S. Geological Survey (ScienceBase)",
                    url=f"https://www.sciencebase.gov/catalog/item/{child['id']}",
                    license="USGS public domain",
                )

        tables = [
            f["name"]
            for f in sciencebase.files(item_id, suffixes=_TABLE_SUFFIXES,
                                       max_bytes=config.SB_MAX_FILE_BYTES)
        ]
        run.rows_seen = 0
        if tables:
            log.warning(
                "thermal: item %s now ships tabular files %s - the parser needs "
                "extending to ingest them", item_id, tables)
        else:
            log.info(
                "thermal: %s is imagery + documentation only; datasets registered, "
                "no observations ingested", item_id)
