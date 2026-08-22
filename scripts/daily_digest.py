#!/usr/bin/env python3
"""Standalone daily status digest for Kīlauea.

Deliberately independent of the database: it fetches the two sources that
change daily (the USGS episode table and the latest HVO notice) and prints a
short report. That makes it runnable anywhere with network access — including a
scheduled cloud session that has no copy of ``kilauea.db``.

    python3 scripts/daily_digest.py            # human-readable
    python3 scripts/daily_digest.py --json     # machine-readable

For maintaining the database itself use ``scripts/daily_update.sh``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kilauea import config, forecast  # noqa: E402
from kilauea.http import get, post_json  # noqa: E402
from kilauea.sources import episodes  # noqa: E402

HST = dt.timezone(dt.timedelta(hours=-10))


def latest_episodes(limit: int = 3) -> list[dict]:
    page = get(config.USGS_EPISODES_URL, timeout=180).text
    tables = [t for t in episodes._tables(page)
              if t and "Episode Number" in " ".join(t[0])]
    if not tables:
        raise RuntimeError("USGS episode table not found — page layout changed")
    rows = []
    for cells in tables[0][1:]:
        if len(cells) < 7 or not cells[0].strip().isdigit():
            continue
        start, _ = episodes.parse_hst(cells[1])
        year = start.astimezone(HST).year if start else None
        pause, _ = episodes.parse_hst(cells[2], default_year=year)
        rows.append({
            "episode_no": int(cells[0]),
            "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ") if start else None,
            "pause_utc": pause.strftime("%Y-%m-%dT%H:%M:%SZ") if pause else None,
            "duration_hours": episodes.parse_duration_hours(cells[3]),
            "repose_text": cells[4] or None,
            "fountain_height_m": episodes.parse_number(cells[5]),
            "volume_mcm": episodes.parse_number(cells[6]),
        })
    return sorted(rows, key=lambda r: r["episode_no"])[-limit:]


def latest_notice() -> dict | None:
    today = dt.datetime.now(dt.timezone.utc).date()
    payload = {
        "obsAbbr": "hvo", "noticeTypeCd": None, "volcCd": config.HANS_VOLC_CD,
        "startDate": (today - dt.timedelta(days=14)).isoformat(),
        "startUnixtime": int((dt.datetime.combine(today - dt.timedelta(days=14),
                                                  dt.time(), dt.timezone.utc)).timestamp()),
        "endDate": (today + dt.timedelta(days=1)).isoformat(),
        "endUnixtime": int((dt.datetime.combine(today + dt.timedelta(days=1),
                                                dt.time(), dt.timezone.utc)).timestamp()),
        "searchText": None, "preflightTotal": 40, "pageIndex": 0,
    }
    resp = post_json(f"{config.HANS_API}/search/search", payload)
    data = resp.get("noticeData") or []
    if not data:
        return None
    rec = max(data, key=lambda r: r.get("sentUnixtime") or 0)
    from kilauea.sources.hans import _ALERT_RE, _COLOR_RE, _first, _strip
    body = _strip(rec.get("noticeHtml", ""))
    return {
        "sent_utc": rec.get("sentUtc"),
        "alert_level": _first(_ALERT_RE, body),
        "color_code": _first(_COLOR_RE, body),
        "body": body,
        "url": rec.get("permLink"),
    }


def forecast_window(body: str) -> dict | None:
    """Reuse the notice-mining logic so the digest and the database agree."""
    for rm in forecast._RANGE_RE.finditer(body):
        lo = max(0, rm.start() - 220)
        hi = min(len(body), rm.end() + 120)
        sentence = " ".join(body[lo:hi].split())
        if forecast._INTENT_RE.search(sentence) and forecast._CONTEXT_RE.search(sentence):
            m1 = forecast._MONTHS[rm.group("m1").lower()]
            m2 = forecast._MONTHS[rm.group("m2").lower()] if rm.group("m2") else m1
            now = dt.datetime.now(HST)
            y1 = int(rm.group("y1")) if rm.group("y1") else now.year
            y2 = int(rm.group("y2")) if rm.group("y2") else y1
            if m2 < m1:
                y2 = y1 + 1
            try:
                start = dt.date(y1, m1, int(rm.group("d1")))
                end = dt.date(y2, m2, int(rm.group("d2")))
            except ValueError:
                continue
            if end < start or (end - start).days > 31:
                continue
            return {"start": start.isoformat(), "end": end.isoformat(),
                    "sentence": sentence}
    return None


def build() -> dict:
    eps = latest_episodes()
    notice = latest_notice()
    last = eps[-1] if eps else None
    now = dt.datetime.now(dt.timezone.utc)

    since_hours = None
    if last and last["pause_utc"]:
        since_hours = round(
            (now - dt.datetime.strptime(last["pause_utc"], "%Y-%m-%dT%H:%M:%SZ")
             .replace(tzinfo=dt.timezone.utc)).total_seconds() / 3600, 1)

    return {
        "generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latest_episode": last,
        "recent_episodes": eps,
        "hours_since_last_pause": since_hours,
        "alert_level": notice["alert_level"] if notice else None,
        "color_code": notice["color_code"] if notice else None,
        "notice_sent_utc": notice["sent_utc"] if notice else None,
        "notice_url": notice["url"] if notice else None,
        "hvo_forecast_window": forecast_window(notice["body"]) if notice else None,
    }


def render(d: dict) -> str:
    last = d["latest_episode"] or {}
    lines = [
        f"Kīlauea daily digest — {d['generated_utc']}",
        "",
        f"Alert level : {d['alert_level']} / aviation {d['color_code']}"
        f"   (notice {d['notice_sent_utc']})",
        f"Last episode: #{last.get('episode_no')} started {last.get('start_utc')}, "
        f"paused {last.get('pause_utc')}, {last.get('duration_hours')} h, "
        f"{last.get('volume_mcm')} Mm3, fountain {last.get('fountain_height_m')} m",
        f"Elapsed     : {d['hours_since_last_pause']} h since that pause"
        if d["hours_since_last_pause"] is not None else "Elapsed     : n/a",
    ]
    win = d["hvo_forecast_window"]
    if win:
        lines += ["", f"HVO forecast window: {win['start']} .. {win['end']} (HST)",
                  f"  \"{win['sentence'][:220]}\""]
    else:
        lines += ["", "HVO forecast window: none stated in the latest notice"]
    if d["notice_url"]:
        lines += ["", d["notice_url"]]
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-o", "--out", help="also write the digest to this path")
    a = ap.parse_args()
    digest = build()
    text = json.dumps(digest, indent=2, ensure_ascii=False) if a.json else render(digest)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
