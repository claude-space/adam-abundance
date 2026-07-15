"""Top-writer identification (PRD §16.3) — normalize per-writer performance so
the ranking controls for the beats writers get (category / intent) rather than
raw sessions. Pure + deterministic; the Analytics/Production caller supplies the
window's article rows (from BigQuery) and persists the result as `writer_stats`.

Normalization: each article's sessions are divided by the mean sessions of its
(category, intent) cohort in the window → a performance ratio that neutralizes
"who happened to get the hot beat". A writer's ``norm_score`` is the mean of
their articles' ratios (>1.0 = beats the cohort average). Writers below a
minimum-article floor are excluded; the top-N by ``norm_score`` are flagged
``is_top``. N and the floor are config (§13.14).
"""

from __future__ import annotations

from typing import Any


def normalize_writers(
    articles: list[dict[str, Any]],
    *,
    min_articles: int = 5,
    top_n: int = 10,
) -> list[dict[str, Any]]:
    """``articles``: ``[{author, sessions, category, intent}]`` for the window.

    Returns per-writer rows ``[{author, article_count, avg_sessions, norm_score,
    is_top}]`` sorted by ``norm_score`` desc, with the top ``top_n`` flagged.
    """
    # Cohort mean sessions per (category, intent) — over ALL articles in the
    # window (not just the qualifying writers') so the baseline is stable.
    cohorts: dict[tuple[str, str], list[float]] = {}
    for a in articles:
        key = (a.get("category") or "", a.get("intent") or "")
        cohorts.setdefault(key, []).append(float(a.get("sessions") or 0))
    cohort_mean = {k: (sum(v) / len(v)) for k, v in cohorts.items() if v}

    per: dict[str, dict[str, Any]] = {}
    for a in articles:
        author = a.get("author")
        if not author:
            continue
        sess = float(a.get("sessions") or 0)
        base = cohort_mean.get((a.get("category") or "", a.get("intent") or ""), 0.0)
        ratio = (sess / base) if base > 0 else 1.0     # no cohort signal → neutral
        acc = per.setdefault(author, {"article_count": 0, "sessions_sum": 0.0, "ratio_sum": 0.0})
        acc["article_count"] += 1
        acc["sessions_sum"] += sess
        acc["ratio_sum"] += ratio

    out: list[dict[str, Any]] = []
    for author, acc in per.items():
        n = acc["article_count"]
        if n < min_articles:
            continue
        out.append({"author": author, "article_count": n,
                    "avg_sessions": round(acc["sessions_sum"] / n, 1),
                    "norm_score": round(acc["ratio_sum"] / n, 3), "is_top": False})
    out.sort(key=lambda w: (w["norm_score"], w["avg_sessions"]), reverse=True)
    for w in out[:top_n]:
        w["is_top"] = True
    return out
