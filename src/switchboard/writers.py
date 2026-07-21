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
    """``articles``: ``[{author, sessions, category, intent, cost, words}]`` for
    the window (``cost``/``words`` optional — the real per-article WriterCost and
    WordCount when available).

    Returns per-writer rows ``[{author, article_count, avg_sessions, norm_score,
    is_top, usd_per_article, usd_per_word}]`` sorted by ``norm_score`` desc, with
    the top ``top_n`` flagged. The two cost fields are None when no cost data was
    supplied for that writer.
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
        acc = per.setdefault(author, {"article_count": 0, "sessions_sum": 0.0, "ratio_sum": 0.0,
                                      "cost_sum": 0.0, "cost_n": 0, "words_sum": 0.0, "words_n": 0})
        acc["article_count"] += 1
        acc["sessions_sum"] += sess
        acc["ratio_sum"] += ratio
        # Only PAID articles (WriterCost > 0) count toward the per-writer rate, so
        # it's comparable to the brand pay baseline (median of paid articles) and
        # a staff/salaried writer with no per-article cost honestly shows none —
        # rather than a $0-diluted average. Words are paired to the same articles
        # so the derived per-word rate matches.
        try:
            cost = float(a.get("cost")) if a.get("cost") is not None else 0.0
        except (TypeError, ValueError):
            cost = 0.0
        if cost > 0:
            acc["cost_sum"] += cost
            acc["cost_n"] += 1
            try:
                w = float(a.get("words") or 0)
            except (TypeError, ValueError):
                w = 0.0
            if w > 0:
                acc["words_sum"] += w
                acc["words_n"] += 1

    out: list[dict[str, Any]] = []
    for author, acc in per.items():
        n = acc["article_count"]
        if n < min_articles:
            continue
        per_article = round(acc["cost_sum"] / acc["cost_n"], 2) if acc["cost_n"] else None
        # Per-word from the writer's own avg cost ÷ avg words — a fair rate even
        # when article lengths vary.
        avg_words = (acc["words_sum"] / acc["words_n"]) if acc["words_n"] else 0.0
        per_word = round(per_article / avg_words, 4) if (per_article and avg_words > 0) else None
        out.append({"author": author, "article_count": n,
                    "avg_sessions": round(acc["sessions_sum"] / n, 1),
                    "norm_score": round(acc["ratio_sum"] / n, 3), "is_top": False,
                    "usd_per_article": per_article, "usd_per_word": per_word})
    out.sort(key=lambda w: (w["norm_score"], w["avg_sessions"]), reverse=True)
    for w in out[:top_n]:
        w["is_top"] = True
    return out
