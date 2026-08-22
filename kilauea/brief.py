"""Build the daily brief's data context from the database.

Everything the HTML brief states as a number comes from here, and everything
here comes from a table. Nothing is fetched at render time and nothing is
inferred by prose. Fields that cannot be sourced are emitted as ``null`` with an
``unavailable`` reason attached, so the writer can say "未確認" and say why,
instead of quietly omitting the row or guessing a value.

Every timestamp in the output is Hawaii Standard Time (UTC-10, no DST), already
formatted; the raw UTC value is kept beside it under ``*_utc`` for auditing.

    python3 -m kilauea brief-context -o briefs/context.json
"""
from __future__ import annotations

import datetime as dt
import json
import re
import statistics
from typing import Any

HST = dt.timezone(dt.timedelta(hours=-10))

_JP_WEEKDAY = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"]

# Section headers HVO uses in its daily updates. Extractors are scoped to a
# section so that boilerplate in Resources/Hazards cannot be mistaken for an
# observation.
_SECTIONS = [
    "Summary", "Overview", "Summit Observations", "Rift Zone Observations",
    "Analysis", "Resources", "Hazards", "More Information",
]


# --- helpers ------------------------------------------------------------------

def _parse_utc(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def hst(d: dt.datetime | None) -> str | None:
    if d is None:
        return None
    return d.astimezone(HST).strftime("%Y-%m-%d %H:%M HST")


def hst_pretty(d: dt.datetime | None) -> str | None:
    """'2026年8月19日 2:53 AM' — for prose."""
    if d is None:
        return None
    l = d.astimezone(HST)
    hour = l.hour % 12 or 12
    mer = "AM" if l.hour < 12 else "PM"
    return f"{l.year}年{l.month}月{l.day}日 {hour}:{l.minute:02d} {mer}"


def hst_date(d: dt.datetime | None) -> str | None:
    return d.astimezone(HST).strftime("%Y-%m-%d") if d else None


def unavailable(reason: str) -> dict:
    return {"value": None, "unavailable": reason}


def value(v, **extra) -> dict:
    out = {"value": v}
    out.update(extra)
    return out


# Abbreviations whose internal periods are not sentence ends. Protected before
# splitting and restored after, which is more reliable than a lookbehind chain.
_ABBREV = ["a.m.", "p.m.", "A.M.", "P.M.", "U.S.", "U.S.G.S.", "Mt.", "St.",
           "Dr.", "Mr.", "Ms.", "approx.", "no.", "No.", "vs.", "e.g.", "i.e."]
_ABBREV_SUB = [(a, a.replace(".", "\u0001")) for a in _ABBREV]


def _sentences(text: str) -> list[str]:
    """Split prose into sentences.

    Three traps, each of which silently hides numbers from every extractor
    downstream if handled wrongly:

    * ``12.1 microradians`` - a decimal point is not a sentence end. It is also
      never followed by whitespace, so requiring whitespace after the period is
      enough; guarding on "no digit before" would be wrong, because HVO ends
      sentences with digits all the time ("during episode 53.").
    * a single newline is a soft wrap, not a sentence end.
    * ``a.m.`` and ``U.S.`` contain periods followed by whitespace, so they are
      protected before the split and restored after.
    """
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text or "")
    for real, safe in _ABBREV_SUB:
        text = text.replace(real, safe)
    parts = re.split(r"\.\s+|\n{2,}", text)
    out = []
    for p in parts:
        p = " ".join(p.split())
        for real, safe in _ABBREV_SUB:
            p = p.replace(safe, real)
        if p:
            out.append(p)
    return out


def split_sections(body: str) -> dict[str, str]:
    """Slice a HVO daily update into its labelled sections."""
    if not body:
        return {}
    idx = []
    for name in _SECTIONS:
        m = re.search(rf"^\s*{re.escape(name)}:\s*", body, re.M)
        if m:
            idx.append((m.start(), m.end(), name))
    idx.sort()
    out = {}
    for i, (_, end, name) in enumerate(idx):
        stop = idx[i + 1][0] if i + 1 < len(idx) else len(body)
        out[name] = body[end:stop].strip()
    out["_full"] = body
    return out


def _num(s: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", (s or "").replace(",", ""))
    return float(m.group()) if m else None


_WORD_NUM = {"no": 0, "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
             "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


# --- notice extractors ---------------------------------------------------------

def extract_tilt(sections: dict, notice_id: str = "", sent_utc: str = "") -> dict:
    """Cumulative inflation, 24-hour change, co-episode deflation, station.

    Delegates to the same miner that populates ``tilt_reading`` rather than
    keeping a second set of patterns here. The duplicate implementation this
    replaces missed "The Summer Camp (SMC) tiltmeter has recorded..." because it
    only matched a bare three-letter code before the word "tiltmeter"; HVO
    varies the phrasing constantly, and one extractor that is exercised against
    786 notices is worth more than two that drift apart.
    """
    from .sources.tilt_notice import extract as _mine

    body = sections.get("_full", "")
    out = {
        "station": unavailable("通知に傾斜計名の記載がない"),
        "cumulative_urad": unavailable("通知に積算値の記載がない"),
        "change_24h_urad": unavailable("通知に24時間変化の記載がない"),
        "episode_deflation_urad": unavailable("通知に収縮量の記載がない"),
        "offline_stations": [],
        "quotes": [],
    }
    if not body:
        return out

    readings = _mine(notice_id or "context", sent_utc or "1970-01-01T00:00:00Z", body)
    seen_quotes = set()
    for r in readings:
        q = r["source_sentence"]
        if q not in seen_quotes:
            seen_quotes.add(q)
            out["quotes"].append(q[:400])

    def latest(kind):
        hits = [r for r in readings if r["kind"] == kind]
        return hits[-1] if hits else None

    cum = latest("inflation_cumulative")
    d24 = latest("change_24h")
    defl = latest("deflation_episode")

    if cum:
        out["cumulative_urad"] = value(cum["magnitude_urad"],
                                       source_sentence=cum["source_sentence"][:300])
        if cum["station"]:
            out["station"] = value(cum["station"],
                                   source_sentence=cum["source_sentence"][:300])
    if d24:
        out["change_24h_urad"] = value(d24["value_urad"],
                                       source_sentence=d24["source_sentence"][:300])
    if defl:
        out["episode_deflation_urad"] = value(
            defl["magnitude_urad"], station=defl["station"],
            source_sentence=defl["source_sentence"][:300])
        if out["station"]["value"] is None and defl["station"]:
            out["station"] = value(defl["station"],
                                   source_sentence=defl["source_sentence"][:300])

    # Station codes are printed in caps; matching case-insensitively here would
    # let ordinary words like "the" and "few" pass as station codes.
    for s in _sentences(body):
        if not re.search(r"offline", s, re.I):
            continue
        for m in re.finditer(r"\b([A-Z]{3})\b(?=[^.]{0,80}?(?:tiltmeter|station|webcam)"
                             r"[^.]{0,80}?offline)", s):
            if m.group(1) not in out["offline_stations"]:
                out["offline_stations"].append(m.group(1))
    return out


def extract_earthquakes(sections: dict) -> dict:
    scope = sections.get("Summit Observations") or ""
    for s in _sentences(scope):
        if "earthquake" not in s.lower():
            continue
        if not re.search(r"(?:past|last)\s+24\s+hours|since yesterday", s, re.I):
            continue
        m = re.search(r"\b(?:were|was)\s+(?:only\s+)?(\d+|" + "|".join(_WORD_NUM) + r")\b",
                      s, re.I)
        if not m:
            m = re.search(r"\b(\d+|" + "|".join(_WORD_NUM) + r")\s+earthquakes?\b", s, re.I)
        if m:
            token = m.group(1).lower()
            n = _WORD_NUM.get(token, None)
            if n is None:
                n = int(token) if token.isdigit() else None
            if n is not None:
                return value(n, source_sentence=s[:300])
    return unavailable("通知に山頂域の24時間地震数の記載がない")


def extract_so2(sections: dict) -> dict:
    """Measured emission rate if published, otherwise the typical-range statement."""
    scope = " ".join(filter(None, [sections.get("Summit Observations"),
                                   sections.get("Overview")]))
    typical = None
    for s in _sentences(scope):
        if not re.search(r"SO2|sulfur dioxide", s, re.I):
            continue
        if re.search(r"typically varies|typically range", s, re.I):
            m = re.search(r"between\s+([\d,]+)\s*(?:to|and|-)\s*([\d,]+)\s*tonnes per day", s, re.I)
            if m:
                typical = {"low_tpd": _num(m.group(1)), "high_tpd": _num(m.group(2)),
                           "source_sentence": s[:300]}
            continue
        m = re.search(r"(?:emission rate[s]?|SO2)[^.]{0,80}?(?:of|was|were|measured at|approximately)"
                      r"\s*([\d,]+)\s*tonnes per day", s, re.I)
        if m:
            return {"measured": value(_num(m.group(1)), source_sentence=s[:300]),
                    "typical_range": typical}
    return {"measured": unavailable("通知に山頂SO2の実測値の記載がない"),
            "typical_range": typical}


def extract_rift(sections: dict) -> dict:
    scope = sections.get("Rift Zone Observations") or ""
    sents = [s for s in _sentences(scope) if s]
    if not sents:
        return unavailable("通知にリフトゾーンの節がない")
    quiet = bool(re.search(r"\bremain low\b|\blow\b|\bno significant\b|below the detection limit",
                           " ".join(sents), re.I))
    return value("静穏" if quiet else "要確認", quotes=[s[:300] for s in sents[:3]])


def extract_outage_note(sections: dict) -> dict:
    for s in _sentences(sections.get("_full", "")):
        if re.search(r"outage|offline|storm damage|power", s, re.I) and \
           re.search(r"webcam|tiltmeter|monitoring network|station", s, re.I):
            return value(s[:400])
    return unavailable("通知に観測網の障害に関する記載がない")


def extract_wind(sections: dict) -> dict:
    """Wind at the vents, as HVO states it.

    Deliberately taken from the notice rather than from a weather API: the
    nearest NWS stations reporting wind are airports more than 50 km away, and
    what matters for vog and tephra is the wind over the vents, which is what
    HVO reports.
    """
    scope = " ".join(filter(None, [sections.get("Summit Observations"),
                                   sections.get("Overview")]))
    for sent in _sentences(scope):
        if not re.search(r"\bwinds?\b", sent, re.I):
            continue
        if not re.search(r"mph|miles per hour|knots|m/s", sent, re.I):
            continue
        speed = re.search(r"(\d+)\s*(?:to|-|–)\s*(\d+)\s*(?:mph|miles per hour)", sent, re.I)
        gust = re.search(r"gusts?[^.]{0,30}?(\d+)\s*mph", sent, re.I)
        direction = re.search(r"(?:coming )?from the\s+([a-z\- ]+?)(?:\s+between|\s+at|,|\.)", sent, re.I)
        blown = re.search(r"(?:blown|blowing) to the\s+([a-z\-]+)", sent, re.I)
        return value({
            "speed_low_mph": float(speed.group(1)) if speed else None,
            "speed_high_mph": float(speed.group(2)) if speed else None,
            "gust_mph": float(gust.group(1)) if gust else None,
            "from_direction": direction.group(1).strip() if direction else None,
            "plume_drift": blown.group(1).strip() if blown else None,
        }, source_sentence=sent[:300])
    return unavailable("通知に風向風速の記載がない")


def extract_status_phrase(sections: dict) -> dict:
    summary = sections.get("Summary") or ""
    s = _sentences(summary)
    return value(s[0][:400]) if s else unavailable("通知にSummaryの節がない")


# --- forecast ------------------------------------------------------------------

def _deflation_model(conn, before_ep: int | None = None, k: int = 14) -> dict | None:
    """Fit repose duration against the deflation the preceding episode produced.

    The summit deflates by a measurable amount during each fountaining episode
    and then refills; the larger the deflation, the longer the refill. Across
    the episodes with a published deflation figure this correlates at r ~ 0.87,
    which makes it the strongest single predictor available - and, unlike the
    re-inflation trajectory, it is known the moment an episode ends rather than
    a week later.

    Fitted on the most recent ``k`` episodes only, because the eruption's
    behaviour has drifted over its twenty months.
    """
    rows = conn.execute(
        """SELECT e.episode_no, t.deflation_urad, e.repose_hours_calc / 24.0 AS repose_days
           FROM episode e
           JOIN v_episode_tilt_notice t
             ON t.episode_no = e.episode_no AND t.station = 'UWD'
           WHERE t.deflation_urad IS NOT NULL AND e.repose_hours_calc IS NOT NULL
           ORDER BY e.episode_no""").fetchall()
    if before_ep is not None:
        rows = [r for r in rows if r["episode_no"] < before_ep]
    rows = rows[-k:]
    if len(rows) < 5:
        return None

    xs = [r["deflation_urad"] for r in rows]
    ys = [r["repose_days"] for r in rows]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    var = statistics.pvariance(xs)
    if var == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs) / var
    intercept = my - slope * mx
    sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
    r = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs) / (sx * sy)
         if sx and sy else None)
    return {"slope_days_per_urad": round(slope, 4),
            "intercept_days": round(intercept, 3),
            "r": round(r, 3) if r is not None else None,
            "n": len(rows)}


def _backtest(conn, first: int = 40) -> dict | None:
    """Walk-forward evaluation of the deflation model, refitted at each episode.

    Also returns the median signed residual, which is applied as a bias
    correction: the raw fit runs early, and saying so in the output is more
    useful than quietly shifting the number.
    """
    eps = {r["episode_no"]: r for r in conn.execute(
        "SELECT episode_no, pause_utc, repose_hours_calc FROM episode "
        "WHERE pause_utc IS NOT NULL")}
    defl = {r["episode_no"]: r["deflation_urad"] for r in conn.execute(
        "SELECT episode_no, deflation_urad FROM v_episode_tilt_notice "
        "WHERE station = 'UWD' AND deflation_urad IS NOT NULL")}

    residuals: list[float] = []
    corrected: list[float] = []
    for n in sorted(eps):
        if n < first or n not in defl or not eps[n]["repose_hours_calc"]:
            continue
        model = _deflation_model(conn, before_ep=n)
        if not model:
            continue
        pred = model["slope_days_per_urad"] * defl[n] + model["intercept_days"]
        actual = eps[n]["repose_hours_calc"] / 24.0
        bias = statistics.median(residuals) if len(residuals) >= 3 else 0.0
        corrected.append(pred - bias - actual)
        residuals.append(pred - actual)

    if not corrected:
        return None
    return {
        "n": len(corrected),
        "mae_days": round(statistics.mean(abs(e) for e in corrected), 2),
        "median_error_days": round(statistics.median(corrected), 2),
        "within_2_days": sum(1 for e in corrected if abs(e) <= 2),
        "within_3_days": sum(1 for e in corrected if abs(e) <= 3),
        "bias_days": round(statistics.median(residuals), 2) if residuals else 0.0,
        "note": ("エピソード終了直後に予測した場合の実績。休止の途中で観測を足しても"
                 "この誤差が縮む保証はない"),
    }


def own_forecast(conn, reposes: list[float], pause: dt.datetime | None,
                 tilt: dict, episode_no: int | None, now: dt.datetime) -> dict:
    """This brief's own onset estimate, recomputed identically every day.

    Three independent reads are produced. The first is the estimate; the other
    two are reported beside it because they fail in opposite directions and
    their disagreement is itself information.

    1. **Deflation model (primary).** Repose duration regressed on the deflation
       the last episode produced, fitted on the most recent 14 episodes and
       bias-corrected from its own walk-forward residuals. Backtested from
       episode 40 onward it lands within two days about three times in four.
    2. **Repose median.** The median of the last ten intervals. Ignores how big
       the last episode was, so it runs late after a small episode and early
       after a large one.
    3. **Tilt recovery.** The date cumulative re-inflation would reach the
       historical recovery fraction at the observed rate. Runs early by
       construction - inflation decelerates and deflationary excursions subtract
       - so it is reported as an early-risk date, not folded into the window.
    """
    out: dict[str, Any] = {}
    if pause is None:
        return {"window_start": None, "window_end": None, "point_date": None,
                "unavailable": "直前エピソードの停止時刻が未取得"}

    # --- read 2: repose median -------------------------------------------------
    repose_date = None
    if reposes:
        last10, last3 = reposes[-10:], reposes[-3:]
        med = statistics.median(last10)
        repose_date = pause + dt.timedelta(hours=med)
        out["repose_median_days"] = round(med / 24, 2)
        out["repose_last3_days"] = [round(r / 24, 2) for r in last3]
        out["repose_date_hst"] = hst(repose_date)

    # --- read 1: deflation model ----------------------------------------------
    deflation = None
    if episode_no is not None:
        row = conn.execute(
            "SELECT deflation_urad FROM v_episode_tilt_notice "
            "WHERE episode_no = ? AND station = 'UWD'", (episode_no,)).fetchone()
        deflation = row["deflation_urad"] if row else None

    model = _deflation_model(conn)
    backtest = _backtest(conn)
    point = None
    if model and deflation:
        bias = backtest["bias_days"] if backtest else 0.0
        days = model["slope_days_per_urad"] * deflation + model["intercept_days"] - bias
        point = pause + dt.timedelta(days=days)
        out["deflation_urad"] = deflation
        out["deflation_model"] = model
        out["deflation_model_days"] = round(days, 2)
        out["deflation_date_hst"] = hst(point)
    out["backtest"] = backtest or unavailable("採点できる過去エピソードが足りない")

    # --- read 3: tilt recovery -------------------------------------------------
    tilt_date = None
    cum = tilt["cumulative_urad"]["value"]
    rate = tilt["change_24h_urad"]["value"]
    target = tilt["episode_deflation_urad"]["value"] or deflation
    if cum is not None and rate and rate > 0 and target and target > cum:
        tilt_date = now + dt.timedelta(days=(target - cum) / rate)
        out["tilt_recovery_date_hst"] = hst(tilt_date)

    # --- assemble --------------------------------------------------------------
    primary = point or repose_date
    if primary is None:
        return {**out, "window_start": None, "window_end": None, "point_date": None,
                "unavailable": "収縮量も休止履歴も揃わず、推定を出せない"}

    half = max(backtest["mae_days"], 1.5) if backtest else 2.5
    method = "deflation model" if point else "repose median"
    out.update({
        "method": method,
        "point_date": hst_date(primary),
        "point_datetime_hst": hst(primary),
        "window_start": hst_date(primary - dt.timedelta(days=half)),
        "window_end": hst_date(primary + dt.timedelta(days=half)),
        "window_half_width_days": round(half, 2),
        "early_risk_date": hst_date(tilt_date) if tilt_date else None,
    })

    rationale = []
    if point and model:
        rationale.append(
            f"第{episode_no}回の収縮量は UWD で {deflation} µrad。直近{model['n']}回で"
            f"「休止日数 = {model['slope_days_per_urad']} × 収縮量 + "
            f"{model['intercept_days']}」を当てはめ（相関 r={model['r']}）、"
            f"walk-forward の系統誤差 {backtest['bias_days'] if backtest else 0} 日を"
            f"補正して {out['deflation_model_days']} 日、つまり {hst(point)}。")
    if backtest:
        rationale.append(
            f"この手法を第40回以降で遡って採点すると、平均絶対誤差 "
            f"{backtest['mae_days']} 日、{backtest['within_2_days']}/{backtest['n']} 回が"
            f"±2日以内に入る。ウィンドウ幅はこの誤差から取った。")
    if repose_date:
        rationale.append(
            f"休止履歴だけを見る従来の読みでは {hst(repose_date)}"
            f"（直近10回の中央値 {out.get('repose_median_days')} 日）。"
            f"この読みは直前エピソードの規模を無視するため、小さい回のあとは遅く出る。")
    if tilt_date:
        rationale.append(
            f"傾斜の積算は {tilt['station']['value'] or '観測点不明'} で {cum} µrad、"
            f"同じ観測点の収縮量 {target} µrad が目標。直近24時間の "
            f"{rate} µrad/日 が続けば {hst(tilt_date)} に到達するが、"
            f"膨張は減速し収縮エクスカーションが差し引かれるため早い側の目安である。")
    out["rationale_ja"] = rationale
    return out


# --- context -------------------------------------------------------------------

def build_context(conn, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(HST)

    notice = conn.execute(
        """SELECT notice_id, sent_utc, alert_level, color_code, summary, body_text, url
           FROM alert_notice WHERE notice_type_cd = 'DU'
           ORDER BY sent_utc DESC LIMIT 1""").fetchone()
    latest_any = conn.execute(
        """SELECT notice_id, sent_utc, notice_type_cd, url FROM alert_notice
           ORDER BY sent_utc DESC LIMIT 1""").fetchone()

    ctx: dict[str, Any] = {
        "generated": {
            "utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hst": hst(now),
            "hst_pretty": hst_pretty(now),
            "date_line": f"{_JP_WEEKDAY[local.weekday()]} · {local.year}年{local.month}月{local.day}日 · ハワイ標準時",
            "timezone_note": "ハワイ標準時 (HST) は年間を通じて UTC-10。夏時間はない",
        }
    }

    if notice is None:
        ctx["source_notice"] = unavailable("データベースに日次更新の通知が入っていない")
        ctx["_fatal"] = "alert_notice に Daily Update が無い。collect hans を先に実行すること"
        return ctx

    sent = _parse_utc(notice["sent_utc"])
    sections = split_sections(notice["body_text"])
    age_h = (now - sent).total_seconds() / 3600

    ctx["source_notice"] = {
        "notice_id": notice["notice_id"],
        "sent_hst": hst(sent),
        "sent_hst_pretty": hst_pretty(sent),
        "url": notice["url"],
        "age_hours": round(age_h, 1),
        "is_today_hst": hst_date(sent) == hst_date(now),
        "staleness_note": (
            "本日分の日次更新が発表済み" if hst_date(sent) == hst_date(now)
            else "本日分の日次更新は未発表。直近の発表分を基準としている"),
    }
    if latest_any and latest_any["notice_id"] != notice["notice_id"]:
        ctx["source_notice"]["newer_non_daily_notice"] = {
            "notice_id": latest_any["notice_id"],
            "type": latest_any["notice_type_cd"],
            "sent_hst": hst(_parse_utc(latest_any["sent_utc"])),
            "url": latest_any["url"],
        }

    ctx["alert"] = {
        "level": value(notice["alert_level"]) if notice["alert_level"]
                 else unavailable("通知に警戒レベルの記載がない"),
        "color_code": value(notice["color_code"]) if notice["color_code"]
                      else unavailable("通知に航空カラーコードの記載がない"),
        "summary": extract_status_phrase(sections),
    }

    # --- episode state --------------------------------------------------------
    ep = conn.execute(
        "SELECT * FROM episode ORDER BY episode_no DESC LIMIT 1").fetchone()
    if ep is None:
        ctx["episode"] = unavailable("episode テーブルが空。collect episodes を先に実行すること")
        pause = None
    else:
        start = _parse_utc(ep["start_utc"])
        pause = _parse_utc(ep["pause_utc"])
        erupting = ep["is_ongoing"] == 1
        ctx["episode"] = {
            "number": ep["episode_no"],
            "is_erupting": erupting,
            "state_label": "噴火中" if erupting else "休止中",
            "start_hst": hst(start),
            "start_hst_pretty": hst_pretty(start),
            "pause_hst": hst(pause),
            "pause_hst_pretty": hst_pretty(pause),
            "duration_hours_published": ep["duration_hours"],
            "duration_hours_measured": ep["duration_hours_calc"],
            "fountain_height_m": ep["fountain_height_m"],
            "fountain_height_text": ep["fountain_height_text"],
            "volume_mcm": ep["volume_mcm"],
            "notes": ep["notes"],
            "hours_since_pause": round((now - pause).total_seconds() / 3600, 1) if pause else None,
            "days_since_pause": round((now - pause).total_seconds() / 86400, 2) if pause else None,
            "next_episode_number": ep["episode_no"] + 1,
        }
        plume = re.search(r"maximum plume height was approximately\s*([\d,]+)\s*"
                          r"(?:feet\s*)?\(([\d,]+)\s*meters?\)", notice["body_text"] or "", re.I)
        ctx["episode"]["plume_height"] = (
            value({"feet": _num(plume.group(1)), "metres": _num(plume.group(2))},
                  source_sentence=plume.group(0))
            if plume else unavailable("通知に噴煙高の記載がない"))

    # --- monitoring -----------------------------------------------------------
    tilt = extract_tilt(sections, notice["notice_id"], notice["sent_utc"])
    ctx["tilt"] = tilt
    ctx["earthquakes_summit_24h_hvo"] = extract_earthquakes(sections)
    ctx["so2"] = extract_so2(sections)
    ctx["rift_zones"] = extract_rift(sections)
    ctx["monitoring_outage"] = extract_outage_note(sections)
    ctx["wind"] = extract_wind(sections)

    since_ms = int((now - dt.timedelta(hours=24)).timestamp() * 1000)
    ctx["earthquakes_catalog"] = {
        "note": "USGS ComCat の実測。HVOが言う summit area より狭い半径で数えているため、通知の件数と一致しないことがある",
        "catalog_latest_hst": hst(_parse_utc(
            conn.execute("SELECT MAX(time_utc) FROM earthquake").fetchone()[0])),
        "within_5km_24h": conn.execute(
            "SELECT COUNT(*) FROM earthquake WHERE time_ms > ? AND dist_from_summit_km <= 5",
            (since_ms,)).fetchone()[0],
        "within_10km_24h": conn.execute(
            "SELECT COUNT(*) FROM earthquake WHERE time_ms > ? AND dist_from_summit_km <= 10",
            (since_ms,)).fetchone()[0],
        "within_20km_24h": conn.execute(
            "SELECT COUNT(*) FROM earthquake WHERE time_ms > ? AND dist_from_summit_km <= 20",
            (since_ms,)).fetchone()[0],
    }

    # --- HVO's published window -----------------------------------------------
    fc = conn.execute(
        """SELECT * FROM hvo_forecast ORDER BY issued_utc DESC, id DESC LIMIT 1""").fetchone()
    if fc and (now - _parse_utc(fc["issued_utc"])).days <= 7:
        ctx["hvo_forecast"] = {
            "window_start": fc["window_start_date"],
            "window_end": fc["window_end_date"],
            "issued_hst": hst(_parse_utc(fc["issued_utc"])),
            "sentence": fc["sentence"],
            "target_episode_no": fc["target_episode_no"],
        }
    else:
        ctx["hvo_forecast"] = unavailable(
            "直近7日の通知に日付を明示した予測ウィンドウの記載がない")

    hist = conn.execute(
        """SELECT COUNT(*) n, SUM(hit) hits, AVG(window_days) w, AVG(lead_hours) lead
           FROM v_hvo_forecast_skill WHERE hit IS NOT NULL""").fetchone()
    ctx["hvo_forecast_track_record"] = (
        {"scored_episodes": hist["n"], "hits": hist["hits"],
         "hit_rate_pct": round(100 * (hist["hits"] or 0) / hist["n"], 1),
         "mean_window_days": round(hist["w"], 2),
         "mean_lead_hours": round(hist["lead"], 1)}
        if hist and hist["n"] else unavailable("採点済みのHVO予測がない"))

    # --- this brief's own forecast --------------------------------------------
    reposes = [r[0] for r in conn.execute(
        "SELECT repose_hours_calc FROM episode WHERE repose_hours_calc IS NOT NULL "
        "ORDER BY episode_no")]
    ctx["own_forecast"] = own_forecast(
        conn, reposes, pause, tilt, ep["episode_no"] if ep else None, now)

    # --- tilt series for the figure -------------------------------------------
    ctx["tilt_series"] = _tilt_series(conn, ep, tilt, sent)

    # --- latest aviation notice ------------------------------------------------
    ctx["vona_latest"] = _vona(conn)

    # --- park ------------------------------------------------------------------
    ctx["park"] = _park(conn, now)

    ctx["primary_sources"] = {
        "volcano_updates": "https://www.usgs.gov/volcanoes/kilauea/volcano-updates",
        "notice": notice["url"],
        "hans": "https://volcanoes.usgs.gov/hans-public",
        "monitoring": "https://www.usgs.gov/volcanoes/kilauea/monitoring",
        "eruption_information": "https://www.usgs.gov/volcanoes/kilauea/science/eruption-information",
        "park_conditions": "https://www.nps.gov/havo/planyourvisit/conditions.htm",
        "park_eruption_viewing": "https://www.nps.gov/havo/planyourvisit/eruption-viewing.htm",
    }
    return ctx


def _tilt_series(conn, ep, tilt: dict, sent: dt.datetime | None) -> dict:
    """Points for the figure, in µrad relative to the pre-episode level.

    Since ``tilt_reading`` exists there is a real daily series to draw rather
    than three anchors and a straight line: every value HVO published during
    this repose becomes a point. The episode's own collapse is still only two
    points (start and end), because HVO publishes the total, not the shape.
    """
    if ep is None:
        return {"points": [], "unavailable": "直前エピソードが未取得"}
    start = _parse_utc(ep["start_utc"])
    pause = _parse_utc(ep["pause_utc"])
    row = conn.execute(
        "SELECT deflation_urad, station FROM v_episode_tilt_notice "
        "WHERE episode_no = ? AND deflation_urad IS NOT NULL "
        "ORDER BY CASE station WHEN 'UWD' THEN 0 ELSE 1 END LIMIT 1",
        (ep["episode_no"],)).fetchone()
    defl = row["deflation_urad"] if row else tilt["episode_deflation_urad"]["value"]
    station = (row["station"] if row else None) or tilt["station"]["value"]
    if not (start and pause and defl):
        return {"points": [],
                "unavailable": "収縮量が未取得のため、図の実測アンカーを作れない"}

    points = [
        {"hst": hst(start), "value": 0.0, "label": f"第{ep['episode_no']}回 開始"},
        {"hst": hst(pause), "value": round(-defl, 2),
         "label": f"第{ep['episode_no']}回 終了（収縮 {defl} µrad）"},
    ]
    for r in conn.execute(
            """SELECT observed_ms, cumulative_urad, day_hst FROM v_tilt_reinflation
               WHERE episode_no = ? AND station IS ? ORDER BY observed_ms""",
            (ep["episode_no"], station)):
        when = dt.datetime.fromtimestamp(r["observed_ms"] / 1000, dt.timezone.utc)
        points.append({"hst": hst(when), "value": round(-defl + r["cumulative_urad"], 2),
                       "label": f"積算 +{r['cumulative_urad']} µrad"})
    return {
        "station": station,
        "unit": "µrad（直前エピソード開始時を0とする）",
        "points": points,
        "observed_count": len(points),
        "interpolation_note": ("各点はHVOが公表した値。点と点の間を結ぶ線は補間であり、"
                               "連続観測値ではない。最新点より右には公表値がない"),
    }


def _vona(conn) -> dict:
    """The most recent aviation notice, which carries minute-precision onsets."""
    r = conn.execute(
        """SELECT notice_id, sent_utc, activity_status, onset_utc, colour_code,
                  previous_colour, cloud_movement, remarks, episode_no
           FROM vona ORDER BY sent_ms DESC LIMIT 1""").fetchone()
    if not r:
        return unavailable("vona テーブルが空。collect vona を先に実行すること")
    return {
        "sent_hst": hst(_parse_utc(r["sent_utc"])),
        "activity_status": r["activity_status"],
        "onset_hst": hst(_parse_utc(r["onset_utc"])),
        "colour_code": r["colour_code"],
        "previous_colour": r["previous_colour"],
        "plume_drift": r["cloud_movement"],
        "episode_no": r["episode_no"],
        "remarks": r["remarks"],
        "note": ("VONAは航空向けの定型電文で、ONSETは分精度のUTC。"
                 "エピソード表の丸めた時刻より細かい"),
    }


def _park(conn, now: dt.datetime) -> dict:
    rows = conn.execute(
        """SELECT * FROM park_status WHERE id IN
           (SELECT MAX(id) FROM park_status GROUP BY page)""").fetchall()
    if not rows:
        return unavailable("park_status テーブルが空。collect park を先に実行すること")
    out: dict[str, Any] = {}
    for r in rows:
        fetched = _parse_utc(r["fetched_utc"])
        out[r["page"]] = {
            "url": r["url"],
            "fetched_hst": hst(fetched),
            "fetched_age_days": round((now - fetched).total_seconds() / 86400, 1) if fetched else None,
            "page_last_updated": r["page_updated"],
            "notices": json.loads(r["notices_json"] or "[]"),
            "viewpoints": json.loads(r["viewpoints_json"] or "[]"),
        }
    out["_note"] = ("NPSは機械可読なフィードを出していない。page_last_updated は"
                    "ページ自身が表示している最終更新日であり、取得日とは別である")
    return out
