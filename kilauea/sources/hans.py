"""USGS Hazard Notification System (HANS) — official volcano notices.

Source: the same POST search API the public HANS search page uses.
Grain:  one row per issued notice for Kīlauea (volcano code hi3).

Each notice carries the volcano alert level and aviation colour code at issue
time, plus the narrative text. ~6,900 notices are available, reaching back to
HVO's earliest electronic notices. The narrative is retained in full because it
is the only public record of things like precursor tilt behaviour described in
words rather than published as a time series.
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import re
import time

from .. import config, db
from ..http import get, post_json

log = logging.getLogger(__name__)

PAGE_SIZE = 20  # fixed server-side

_ALERT_RE = re.compile(r"Current Volcano Alert Level:\s*([A-Z /]+?)\s*(?:<|\n|$)", re.I)
_COLOR_RE = re.compile(r"Current Aviation Color Code:\s*([A-Z /]+?)\s*(?:<|\n|$)", re.I)
_PREV_ALERT_RE = re.compile(r"Previous Volcano Alert Level:\s*([A-Z /]+?)\s*(?:<|\n|$)", re.I)
_PREV_COLOR_RE = re.compile(r"Previous Aviation Color Code:\s*([A-Z /]+?)\s*(?:<|\n|$)", re.I)
_SUMMARY_RE = re.compile(r"Summary:\s*(.+?)(?:\n\n|$)", re.S)


def _strip(markup: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", markup or "", flags=re.I)
    text = re.sub(r"</p>|</div>|</center>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _first(rx: re.Pattern, text: str) -> str | None:
    m = rx.search(text)
    return m.group(1).strip().upper() if m else None


def _notice_type_titles() -> dict[str, str]:
    """Map notice type codes (DU, VAN, IU, ...) to human titles.

    The search endpoint returns only the code; the lookup table lives on a
    separate endpoint. A failure here must not sink the whole run, so it
    degrades to an empty map.
    """
    try:
        recs = get(f"{config.HANS_API}/search/getHansNoticeTypes", timeout=60).json()
    except Exception as exc:  # noqa: BLE001 - non-fatal enrichment
        log.warning("HANS: notice type lookup failed (%s); titles will be NULL", exc)
        return {}
    out = {}
    for r in recs if isinstance(recs, list) else []:
        cd = r.get("noticeTypeCd") or r.get("notice_type_cd")
        title = r.get("noticeTypeTitle") or r.get("notice_type_title")
        if cd:
            out[cd] = title
    return out


def _payload(start: dt.date, end: dt.date, page_index: int, total=None) -> dict:
    return {
        "obsAbbr": "hvo",
        "noticeTypeCd": None,
        "volcCd": config.HANS_VOLC_CD,
        "startDate": start.isoformat(),
        "startUnixtime": int(dt.datetime.combine(start, dt.time(), dt.timezone.utc).timestamp()),
        "endDate": end.isoformat(),
        "endUnixtime": int(dt.datetime.combine(end, dt.time(), dt.timezone.utc).timestamp()),
        "searchText": None,
        "preflightTotal": total,
        "pageIndex": page_index,
    }


def collect(conn, *, since: str | None = None, sleep: float = 0.3,
            full: bool = False, **_) -> None:
    """Fetch notices.

    ``since`` (YYYY-MM-DD) bounds the window explicitly. With neither ``since``
    nor ``full``, the window starts 30 days before the newest notice already
    stored: a daily run then costs two or three pages instead of re-walking all
    347, while the overlap still catches anything issued late. ``full`` forces
    the complete sweep.
    """
    with db.Run(conn, "hans", "alert_notice") as run:
        if since:
            start = dt.date.fromisoformat(since)
        elif full:
            start = dt.date(1980, 1, 1)
        else:
            newest = conn.execute("SELECT MAX(sent_utc) FROM alert_notice").fetchone()[0]
            if newest:
                start = dt.date.fromisoformat(newest[:10]) - dt.timedelta(days=30)
                log.info("HANS: incremental from %s (pass --since to widen)", start)
            else:
                start = dt.date(1980, 1, 1)
                log.warning("HANS: empty table, walking the whole archive from %s. "
                            "This is ~347 pages and can take hours on a slow link.",
                            start)
        end = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)

        pre = post_json(f"{config.HANS_API}/search/preflight/", _payload(start, end, 0))
        total = int(pre.get("noticeTotal", 0))
        run.rows_seen = total
        log.info("HANS: %d notices between %s and %s", total, start, end)
        if not total:
            return

        titles = _notice_type_titles()
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        now = db.utcnow()
        batch: list[dict] = []

        for page in range(pages):
            resp = post_json(
                f"{config.HANS_API}/search/search", _payload(start, end, page, total)
            )
            for rec in resp.get("noticeData", []):
                body = _strip(rec.get("noticeHtml", ""))
                sent = rec.get("sentUtc")
                ident = rec.get("noticeIdentifier") or _identifier_from(body, sent)
                if not ident:
                    continue
                summary = _SUMMARY_RE.search(body)
                batch.append(
                    dict(
                        notice_id=ident,
                        sent_utc=(sent or "").replace(" ", "T") + "Z" if sent else None,
                        sent_unixtime=rec.get("sentUnixtime"),
                        notice_type_cd=rec.get("noticeTypeCd"),
                        notice_type_title=titles.get(rec.get("noticeTypeCd")),
                        volc_cds=rec.get("volcCds"),
                        alert_level=_first(_ALERT_RE, body),
                        color_code=_first(_COLOR_RE, body),
                        prev_alert_level=_first(_PREV_ALERT_RE, body),
                        prev_color_code=_first(_PREV_COLOR_RE, body),
                        summary=summary.group(1).strip() if summary else None,
                        body_text=body,
                        url=rec.get("permLink") or rec.get("noticeUrl"),
                        retrieved_at=now,
                    )
                )
            if len(batch) >= 500:
                with db.tx(conn):
                    db.upsert(conn, "alert_notice", batch, conflict=["notice_id"])
                batch.clear()
            if page and page % 25 == 0:
                log.info("HANS: page %d/%d", page, pages)
            time.sleep(sleep)

        with db.tx(conn):
            if batch:
                db.upsert(conn, "alert_notice", batch, conflict=["notice_id"])
            db.register_dataset(
                conn,
                key="usgs:hans:notices",
                title="USGS Hazard Notification System — Kīlauea notices",
                publisher="USGS Volcano Hazards Program",
                url="https://volcanoes.usgs.gov/hans-public/search/",
                license="USGS public domain",
            )


def _identifier_from(body: str, sent: str | None) -> str | None:
    """Fallback key when the API omits the DOI identifier."""
    m = re.search(r"DOI-USGS-[A-Z]+-[0-9T:+\-]+", body)
    if m:
        return m.group()
    return f"HVO-{sent.replace(' ', 'T')}Z" if sent else None
