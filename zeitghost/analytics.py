"""Per-source bias analytics.

Reads zeitghost:article shards, groups by source_name, and computes
per-source bias rollups: count, mean, median, distribution histogram.
"""

import logging
import statistics
from dataclasses import dataclass, field

from zeitghost.bias import AnalyzedArticle

log = logging.getLogger(__name__)

# Histogram buckets for the bias-score distribution
HISTOGRAM_BUCKETS = [
    (0.0, 0.2, "left"),
    (0.2, 0.4, "center-left"),
    (0.4, 0.6, "center"),
    (0.6, 0.8, "center-right"),
    (0.8, 1.001, "right"),
]


@dataclass
class SourceStats:
    source_name: str
    count: int
    mean_bias: float
    median_bias: float
    histogram: dict[str, int] = field(default_factory=dict)
    most_recent: str = ""


def _bucket_for(score: float) -> str:
    for lo, hi, label in HISTOGRAM_BUCKETS:
        if lo <= score < hi:
            return label
    return "center"


def compute_source_stats(articles: list[AnalyzedArticle]) -> list[SourceStats]:
    """Group analyzed articles by source and compute bias rollups.

    Returned list is sorted by article count (descending).
    """
    by_source: dict[str, list[AnalyzedArticle]] = {}
    for a in articles:
        by_source.setdefault(a.original.source_name, []).append(a)

    out: list[SourceStats] = []
    for source, items in by_source.items():
        scores = [a.bias_score for a in items]
        if not scores:
            continue
        histogram = {label: 0 for _, _, label in HISTOGRAM_BUCKETS}
        for s in scores:
            histogram[_bucket_for(s)] += 1
        latest = max((a.original.published for a in items if a.original.published),
                     default="")
        out.append(SourceStats(
            source_name=source,
            count=len(items),
            mean_bias=statistics.fmean(scores),
            median_bias=statistics.median(scores),
            histogram=histogram,
            most_recent=latest,
        ))

    out.sort(key=lambda s: s.count, reverse=True)
    return out
