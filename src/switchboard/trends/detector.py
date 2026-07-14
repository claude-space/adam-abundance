"""Trend detection: cluster cross-source signals and score them 0–100.

Pure and deterministic — no I/O, no DB — so it is unit-testable anywhere.
The clustering approach mirrors daily-reporting-agent's competitor watch
(greedy token-Jaccard grouping with OEM anchors) without importing it
(reference repos are read-only).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable

# -- tokenization --------------------------------------------------------------

_STOP = {
    "the", "and", "for", "with", "that", "this", "its", "has", "have", "will",
    "you", "your", "are", "was", "were", "been", "from", "into", "over", "after",
    "before", "about", "than", "them", "they", "their", "there", "here", "what",
    "when", "where", "why", "how", "who", "all", "any", "can", "could", "should",
    "would", "may", "might", "just", "not", "now", "new", "news", "says", "said",
    "get", "gets", "got", "one", "two", "more", "most", "first", "best", "top",
    "car", "cars", "auto", "vehicle", "vehicles", "2024", "2025", "2026", "2027",
}

_OEMS = (
    "acura", "alfa romeo", "aston martin", "audi", "bentley", "bmw", "bugatti",
    "buick", "byd", "cadillac", "chevrolet", "chevy", "chrysler", "citroen",
    "dodge", "ducati", "ferrari", "fiat", "fisker", "ford", "genesis", "gmc",
    "harley-davidson", "honda", "hyundai", "infiniti", "jaguar", "jeep", "kia",
    "koenigsegg", "lamborghini", "land rover", "lexus", "lincoln", "lotus",
    "lucid", "maserati", "mazda", "mclaren", "mercedes", "mini", "mitsubishi",
    "nio", "nissan", "pagani", "peugeot", "polestar", "porsche", "ram",
    "renault", "rimac", "rivian", "rolls-royce", "scout", "skoda", "stellantis",
    "subaru", "suzuki", "tesla", "toyota", "vinfast", "volkswagen", "volvo",
    "xiaomi", "yamaha",
)
_OEM_RE = re.compile(r"\b(" + "|".join(re.escape(o) for o in _OEMS) + r")\b", re.IGNORECASE)

_BREAKING_RE = re.compile(
    r"\b(recall|recalls|crash|crashes|lawsuit|sues?|bankrupt\w*|layoffs?|merger|"
    r"acquisition|acquires?|unveil\w*|reveal\w*|debuts?|launches?|leak\w*|"
    r"first look|breaking|dies|death|fire|explo\w*|investigat\w*|ban|banned|"
    r"tariff\w*|strike|resign\w*|steps down|record|halts?|discontinu\w*)\b",
    re.IGNORECASE,
)
_EVERGREEN_RE = re.compile(
    r"\b(review|first drive|tested|comparison|vs\.?|buying guide|best of|"
    r"ranked|top \d+|history of|explained|deep dive|spy shots)\b",
    re.IGNORECASE,
)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-]+")


def tokens(text: str) -> set[str]:
    return {t for t in _WORD_RE.findall((text or "").lower()) if len(t) > 2 and t not in _STOP}


def oems(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(m.group(1).lower() for m in _OEM_RE.finditer(text or "")))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def parse_when(raw: str | None) -> datetime | None:
    """Best-effort timestamp parse (ISO or RFC-2822); None when unparseable."""
    if not raw:
        return None
    raw = raw.strip()
    for parser in (datetime.fromisoformat, parsedate_to_datetime):
        try:
            dt = parser(raw.replace("Z", "+00:00") if parser is datetime.fromisoformat else raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


# -- clustering ----------------------------------------------------------------

@dataclass
class Cluster:
    items: list[dict[str, Any]] = field(default_factory=list)
    token_set: set[str] = field(default_factory=set)

    @property
    def sources(self) -> set[str]:
        """Distinct outlets (fall back to origin API when the outlet is unknown)."""
        return {(i.get("source") or i.get("origin") or "?").lower() for i in self.items}

    @property
    def headline(self) -> str:
        titled = [i for i in self.items if i.get("title")]
        if not titled:
            return self.items[0].get("url", "(untitled)") if self.items else "(untitled)"
        return max(titled, key=lambda i: len(tokens(i["title"]) & self.token_set))["title"]

    @property
    def oem_anchor(self) -> tuple[str, ...]:
        counts: Counter[str] = Counter()
        for i in self.items:
            counts.update(oems(i.get("title", "") + " " + i.get("snippet", "")))
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))  # order-independent
        return tuple(o for o, _ in ordered[:3])

    def cluster_key(self) -> str:
        """Stable dedupe key: OEM anchor + the most shared title tokens.
        Ties break alphabetically so the key is order-independent."""
        counts: Counter[str] = Counter()
        for i in self.items:
            counts.update(tokens(i.get("title", "")))
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        top = sorted(t for t, _ in ordered[:5])
        anchor = self.oem_anchor[:1]
        return "-".join([*anchor, *top][:6]) or "untitled"

    def times(self) -> list[datetime]:
        return sorted(t for t in (parse_when(i.get("published_at")) for i in self.items) if t)


def cluster_signals(items: Iterable[dict[str, Any]], *, jaccard_threshold: float = 0.35) -> list[Cluster]:
    """Greedy clustering: an item joins the first cluster whose token overlap is
    high enough or which shares its OEM anchor plus two significant tokens."""
    seen_urls: set[str] = set()
    clusters: list[Cluster] = []
    for item in items:
        url = (item.get("url") or "").split("?", 1)[0].rstrip("/")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        toks = tokens(item.get("title", ""))
        if not toks:
            continue
        item_oems = set(oems(item.get("title", "")))
        placed = False
        for c in clusters:
            shared = toks & c.token_set
            if _jaccard(toks, c.token_set) >= jaccard_threshold or (
                item_oems and item_oems & set(c.oem_anchor) and len(shared) >= 2
            ):
                c.items.append(item)
                c.token_set |= toks
                placed = True
                break
        if not placed:
            clusters.append(Cluster(items=[item], token_set=set(toks)))
    return clusters


# -- coverage-gap --------------------------------------------------------------

def _title_match(cluster: Cluster, titles: Iterable[str]) -> bool:
    """True when any title fuzzily matches the cluster: ≥3 shared significant
    tokens or Jaccard ≥ 0.5 against the cluster's token set."""
    for title in titles:
        t = tokens(title)
        if len(t & cluster.token_set) >= 3 or _jaccard(t, cluster.token_set) >= 0.5:
            return True
    return False


def covered_by_titles(cluster: Cluster, our_titles: Iterable[str]) -> bool:
    """Have we already published a matching story? (coverage-gap check)"""
    return _title_match(cluster, our_titles)


def corroborated_by_titles(cluster: Cluster, titles: Iterable[str]) -> bool:
    """Did an independent monitor (e.g. HC Viral Hits) land on the same topic?
    Same fuzzy match as the coverage check — drives the cross-monitor boost."""
    return _title_match(cluster, titles)


# -- scoring -------------------------------------------------------------------

# Bonus when an independent monitor (HC Viral Hits) landed on the same topic —
# two systems agreeing is a strong signal, so it counts like a watchlist hit.
_CROSS_MONITOR_BONUS = 15.0


def score_cluster(
    cluster: Cluster,
    *,
    watchlist: Iterable[str] = (),
    covered: bool | None = None,
    corroborated: bool = False,
    now: datetime | None = None,
) -> tuple[float, dict[str, float]]:
    """Score 0–100 with an explainable breakdown (surfaced in the console)."""
    now = now or datetime.now(timezone.utc)
    breakdown: dict[str, float] = {}

    n_sources = len(cluster.sources)
    breakdown["outlet_breadth"] = min((n_sources - 1) * 14.0, 42.0)

    times = cluster.times()
    velocity = 0.0
    if len(times) >= 2:
        hours = max((times[-1] - times[0]).total_seconds() / 3600.0, 0.5)
        velocity = len(times) / hours
    breakdown["velocity"] = min(velocity * 8.0, 16.0)

    text = " ".join(i.get("title", "") for i in cluster.items).lower()
    breakdown["watchlist"] = 15.0 if any(w.lower() in text for w in watchlist if w) else 0.0

    if covered is True:
        breakdown["coverage_gap"] = 0.0
    elif covered is False:
        breakdown["coverage_gap"] = 15.0
    else:  # unknown — mild benefit of the doubt
        breakdown["coverage_gap"] = 8.0

    is_breaking = bool(_BREAKING_RE.search(text)) and not _EVERGREEN_RE.search(text)
    breakdown["breaking"] = 12.0 if is_breaking else 0.0

    # Cross-monitor corroboration: HC Viral Hits independently landed on the same
    # topic. Two monitors agreeing is a strong signal → bonus points.
    if corroborated:
        breakdown["cross_monitor"] = _CROSS_MONITOR_BONUS

    if times and (now - times[-1]) > timedelta(hours=48):
        breakdown["stale_penalty"] = -10.0

    total = max(0.0, min(sum(breakdown.values()), 100.0))
    return total, breakdown


def is_breaking(cluster: Cluster) -> bool:
    text = " ".join(i.get("title", "") for i in cluster.items).lower()
    return is_breaking_text(text)


def is_breaking_text(text: str) -> bool:
    text = (text or "").lower()
    return bool(_BREAKING_RE.search(text)) and not _EVERGREEN_RE.search(text)


def cluster_velocity(cluster: Cluster) -> float:
    times = cluster.times()
    if len(times) < 2:
        return 0.0
    hours = max((times[-1] - times[0]).total_seconds() / 3600.0, 0.5)
    return round(len(times) / hours, 2)
