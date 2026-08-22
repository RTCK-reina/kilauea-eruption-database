"""Reference baselines for target A (next-episode onset).

Purpose: give any future model something honest to beat, and confirm the
database is actually usable for the task. Three baselines are evaluated
walk-forward (fit on episodes 1..N, predict N+1) so no future information
leaks:

  * ``last``      — repose after episode N+1 equals repose after episode N
  * ``median_k``  — median of the previous k repose intervals
  * ``trend``     — least-squares line through the previous k intervals

They are scored against the same quantity HVO forecasts, and reported next to
HVO's own published windows so the comparison is like-for-like: HVO states a
*window*, so a point prediction is converted to one by taking the ±half-width
that the baseline's own error distribution implies.

Deliberately dependency-free (no numpy/sklearn): with 53 samples the modelling
bottleneck is sample size, not algorithm.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass
class Score:
    name: str
    n: int
    mae_hours: float
    median_ae_hours: float
    rmse_hours: float
    hit_rate_pm12h: float
    hit_rate_pm24h: float
    implied_window_days: float


def _repose_series(conn) -> list[tuple[int, float]]:
    """(episode_no, measured repose after that episode) in chronological order."""
    return [
        (r[0], r[1])
        for r in conn.execute(
            """SELECT episode_no, repose_hours_calc FROM episode
               WHERE repose_hours_calc IS NOT NULL ORDER BY episode_no"""
        )
    ]


def _predictions(series: list[float], k: int = 5) -> dict[str, list[tuple[float, float]]]:
    """Walk-forward (prediction, actual) pairs for each baseline."""
    out: dict[str, list[tuple[float, float]]] = {"last": [], f"median_{k}": [], f"trend_{k}": []}
    for i in range(1, len(series)):
        history = series[:i]
        actual = series[i]
        out["last"].append((history[-1], actual))

        window = history[-k:]
        out[f"median_{k}"].append((statistics.median(window), actual))

        if len(window) >= 3:
            xs = list(range(len(window)))
            mx, my = statistics.mean(xs), statistics.mean(window)
            denom = sum((x - mx) ** 2 for x in xs)
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, window)) / denom if denom else 0.0
            out[f"trend_{k}"].append((my + slope * (len(window) - mx), actual))
        else:
            out[f"trend_{k}"].append((statistics.mean(window), actual))
    return out


def _score(name: str, pairs: list[tuple[float, float]]) -> Score:
    errs = [p - a for p, a in pairs]
    abs_errs = [abs(e) for e in errs]
    n = len(pairs)
    return Score(
        name=name,
        n=n,
        mae_hours=statistics.mean(abs_errs),
        median_ae_hours=statistics.median(abs_errs),
        rmse_hours=(sum(e * e for e in errs) / n) ** 0.5,
        hit_rate_pm12h=100 * sum(1 for e in abs_errs if e <= 12) / n,
        hit_rate_pm24h=100 * sum(1 for e in abs_errs if e <= 24) / n,
        # Window that would contain ~79% of outcomes (HVO's observed hit rate),
        # expressed in days, so it is directly comparable to window_days.
        implied_window_days=2 * _quantile(abs_errs, 0.79) / 24,
    )


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def hvo_reference(conn) -> dict | None:
    row = conn.execute(
        """SELECT COUNT(*) n, SUM(hit) hits, AVG(window_days) win, AVG(lead_hours) lead
           FROM v_hvo_forecast_skill WHERE hit IS NOT NULL"""
    ).fetchone()
    if not row or not row[0]:
        return None
    return {"n": row[0], "hit_rate": 100 * (row[1] or 0) / row[0],
            "window_days": row[2], "lead_hours": row[3]}


def run(conn, k: int = 5) -> str:
    series = _repose_series(conn)
    if len(series) < 6:
        return "not enough episodes with measured repose to score baselines"

    values = [v for _, v in series]
    scored = [_score(name, pairs) for name, pairs in _predictions(values, k).items()]

    lines = [
        "",
        f"target A baselines — next-episode onset (walk-forward, n={len(values) - 1})",
        "=" * 96,
        f"{'baseline':<12}{'n':>4}{'MAE h':>9}{'median AE h':>13}"
        f"{'RMSE h':>9}{'±12h %':>9}{'±24h %':>9}{'79% window (d)':>17}",
    ]
    for s in sorted(scored, key=lambda x: x.mae_hours):
        lines.append(
            f"{s.name:<12}{s.n:>4}{s.mae_hours:>9.1f}{s.median_ae_hours:>13.1f}"
            f"{s.rmse_hours:>9.1f}{s.hit_rate_pm12h:>9.1f}{s.hit_rate_pm24h:>9.1f}"
            f"{s.implied_window_days:>17.2f}"
        )

    ref = hvo_reference(conn)
    lines.append("-" * 96)
    if ref:
        lines.append(
            f"HVO published windows: n={ref['n']}, hit rate {ref['hit_rate']:.1f}%, "
            f"mean stated window {ref['window_days']:.2f} d, "
            f"mean lead {ref['lead_hours']:.1f} h"
        )
        lines.append(
            "A model is only useful if it narrows the window below "
            f"{ref['window_days']:.2f} d at a comparable hit rate AND keeps a "
            "lead time of a day or more."
        )
    else:
        lines.append("no scored HVO forecasts available for comparison")
    lines.append(
        "Note: these baselines use repose history only. They ignore tilt and "
        "seismicity, which is exactly the headroom a real model has to exploit."
    )
    return "\n".join(lines)
