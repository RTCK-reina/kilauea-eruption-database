"""Hawaiʻi Volcanoes National Park — closures and eruption-viewing status.

Grain: one row per page fetch. NPS publishes no machine-readable feed for this
park, so the page text is stored verbatim alongside the extracted notice titles
and viewpoint names, together with the "Last updated" date the page prints. A
brief built on this table can state how old the park information is instead of
implying it is current.
"""
from __future__ import annotations

import html
import json
import logging
import re

from .. import db
from ..http import get

log = logging.getLogger(__name__)

PAGES = {
    "conditions": "https://www.nps.gov/havo/planyourvisit/conditions.htm",
    "eruption_viewing": "https://www.nps.gov/havo/planyourvisit/eruption-viewing.htm",
}

# Viewpoint names the park uses for caldera-rim eruption viewing.
_VIEWPOINT_NAMES = [
    "Uēkahuna", "Uekahuna",
    "Kīlauea Overlook", "Kilauea Overlook",
    "Kūkamāhuākea", "Kukamahuakea",
    "Kūpinaʻi Pali", "Kupina'i Pali",
    "Keanakākoʻi", "Keanakako'i",
    "Welcome Center",
    "Kīlauea Iki", "Kilauea Iki",
    "Devastation Trail",
    "Waldron Ledge",
]


def _text(markup: str) -> str:
    t = re.sub(r"<script.*?</script>", " ", markup, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h[1-6]>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t).replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _notices(text: str) -> list[dict]:
    """Pull the 'Park Notices' block, which is where closures are announced."""
    out = []
    m = re.search(r"Park Notices(.{0,2000}?)(?:Plan Your Visit|Last updated)", text, re.S | re.I)
    block = m.group(1) if m else text
    for para in [p.strip() for p in block.split("\n") if p.strip()]:
        if len(para) < 12:
            continue
        # NPS prints "Title" then the body as the following line; keep both as one
        # record and let the consumer decide how to present it.
        out.append({"text": para[:600]})
    return out[:12]


def _viewpoints(text: str) -> list[str]:
    seen, out = set(), []
    for name in _VIEWPOINT_NAMES:
        if name in text:
            key = re.sub(r"[^a-z]", "", name.lower())
            if key not in seen:
                seen.add(key)
                out.append(name)
    return out


def _last_updated(text: str) -> str | None:
    m = re.search(r"Last updated:\s*([A-Z][a-z]+ \d{1,2},? \d{4})", text)
    return m.group(1) if m else None


def collect(conn, **_) -> None:
    with db.Run(conn, "park", "park_status") as run:
        now = db.utcnow()
        rows = []
        for page, url in PAGES.items():
            try:
                text = _text(get(url, timeout=120).text)
            except Exception as exc:  # noqa: BLE001 - one page must not sink the run
                log.warning("park: %s fetch failed (%s)", page, exc)
                continue
            rows.append(dict(
                fetched_utc=now,
                page=page,
                url=url,
                page_updated=_last_updated(text),
                notices_json=json.dumps(_notices(text), ensure_ascii=False),
                viewpoints_json=json.dumps(_viewpoints(text), ensure_ascii=False),
                body_text=text[:20000],
            ))
            log.info("park: %s fetched (last updated %s, %d viewpoints)",
                     page, _last_updated(text), len(_viewpoints(text)))
        run.rows_seen = len(rows)
        with db.tx(conn):
            db.upsert(conn, "park_status", rows, conflict=["page", "fetched_utc"])
            db.register_dataset(
                conn,
                key="nps:havo:conditions",
                title="Hawaiʻi Volcanoes National Park — alerts, conditions and eruption viewing",
                publisher="National Park Service",
                url=PAGES["conditions"],
                license="NPS public domain",
            )
