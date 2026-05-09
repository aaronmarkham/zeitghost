"""Bias analytics — overall, per-source, per-category, and bias-distribution
rollups computed from zeitghost:article shards.
"""

import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import date

from zeitghost.bias import AnalyzedArticle

log = logging.getLogger(__name__)

# Per-source 5-bucket histogram (used by the all-sources table)
HISTOGRAM_BUCKETS = [
    (0.0, 0.2, "left"),
    (0.2, 0.4, "center-left"),
    (0.4, 0.6, "center"),
    (0.6, 0.8, "center-right"),
    (0.8, 1.001, "right"),
]

# Coarser 3-bucket thresholds (used by the headline distribution + lean
# labels). Match HtmxNewsEngine's legacy thresholds for parity with the
# numbers users have been seeing in shared screenshots / ads.
LEFT_THRESHOLD = 0.48
RIGHT_THRESHOLD = 0.52


def _coarse_lean(score: float) -> str:
    if score < LEFT_THRESHOLD: return "left"
    if score > RIGHT_THRESHOLD: return "right"
    return "center"


def source_slug(source_name: str) -> str:
    """URL-safe slug for a source name. Used to build /source/<slug>.html
    page paths and link to them from the analytics table."""
    s = re.sub(r"[^a-z0-9]+", "-", source_name.lower()).strip("-")
    return s or "unknown"


@dataclass
class OverallStats:
    """Top-of-page totals."""
    total_articles: int
    total_sources: int
    total_categories: int


@dataclass
class BiasDistribution:
    """3-bucket distribution headline (left / center / right)."""
    left: int
    center: int
    right: int

    @property
    def total(self) -> int:
        return self.left + self.center + self.right

    def pct(self, n: int) -> float:
        return (n / self.total * 100) if self.total else 0.0


@dataclass
class CategoryStats:
    category: str
    count: int
    mean_bias: float

    @property
    def lean(self) -> str:
        return _coarse_lean(self.mean_bias)


@dataclass
class SourceStats:
    source_name: str
    count: int
    mean_bias: float
    median_bias: float
    histogram: dict[str, int] = field(default_factory=dict)
    most_recent: str = ""

    @property
    def lean(self) -> str:
        return _coarse_lean(self.mean_bias)

    @property
    def slug(self) -> str:
        return source_slug(self.source_name)


@dataclass
class MonthlyStats:
    """Per-calendar-month bias rollup, used on per-source time-travel pages."""
    label: str       # "Mar 2026"
    year_month: str  # "2026-03"
    count: int
    mean_bias: float

    @property
    def lean(self) -> str:
        return _coarse_lean(self.mean_bias)


def _sparkline_dot_color(score: float) -> str:
    """RGB string interpolating the bias palette — same math as
    AnalyzedArticle.bias_tint so per-month dots match per-card colors."""
    r = round(79 + (217 - 79) * score)
    g = round(140 + (100 - 140) * score)
    b = round(201 + (88 - 201) * score)
    return f"rgb({r},{g},{b})"


def render_bias_sparkline(monthly: list["MonthlyStats"],
                          width: int = 600, height: int = 60,
                          padding: int = 6) -> str:
    """Generate an inline SVG sparkline of monthly mean bias.

    Y-axis: bias_score, with 1.0 (right) at the top and 0.0 (left) at the
    bottom — so an upward line means the source drifted right over time.
    A dashed reference line at 0.5 marks center. Dots at each month are
    colored by their value (blue→red), with a hoverable <title> showing
    the month and exact mean. Returns "" when there's nothing to plot.
    """
    n = len(monthly)
    if n == 0:
        return ""

    plot_w = width - 2 * padding
    plot_h = height - 2 * padding
    center_y = padding + 0.5 * plot_h

    def _y(bias: float) -> float:
        return padding + (1.0 - bias) * plot_h

    if n == 1:
        x = width / 2
        y = _y(monthly[0].mean_bias)
        return (
            f'<svg class="bias-sparkline" viewBox="0 0 {width} {height}" '
            f'xmlns="http://www.w3.org/2000/svg" role="img" '
            f'aria-label="Bias sparkline (single month)">'
            f'<line x1="{padding}" y1="{center_y:.1f}" x2="{width - padding}" '
            f'y2="{center_y:.1f}" stroke="#30363d" stroke-width="1" '
            f'stroke-dasharray="3,3" />'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" '
            f'fill="{_sparkline_dot_color(monthly[0].mean_bias)}" '
            f'stroke="#0d1117" stroke-width="1.5">'
            f'<title>{monthly[0].label}: {monthly[0].mean_bias:.2f}</title>'
            f'</circle></svg>'
        )

    points = []
    for i, m in enumerate(monthly):
        x = padding + (i / (n - 1)) * plot_w
        points.append((x, _y(m.mean_bias), m))

    path_d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y, _ in points)

    parts = [
        f'<svg class="bias-sparkline" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Mean bias by month">',
        # Center reference (0.5)
        f'<line x1="{padding}" y1="{center_y:.1f}" x2="{width - padding}" '
        f'y2="{center_y:.1f}" stroke="#30363d" stroke-width="1" '
        f'stroke-dasharray="3,3" />',
        # Trajectory
        f'<path d="{path_d}" fill="none" stroke="#58a6ff" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round" />',
    ]
    for x, y, m in points:
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" '
            f'fill="{_sparkline_dot_color(m.mean_bias)}" '
            f'stroke="#0d1117" stroke-width="1.5">'
            f'<title>{m.label}: {m.mean_bias:.2f}</title>'
            f'</circle>'
        )
    parts.append('</svg>')
    return ''.join(parts)


def compute_monthly_stats(articles: list[AnalyzedArticle]) -> list[MonthlyStats]:
    """Group articles by their published year-month and roll up. Sorted
    chronologically (oldest → newest) so a reader scans bias drift left to
    right."""
    by_month: dict[str, list[float]] = {}
    for a in articles:
        pub = a.original.published or ""
        if len(pub) < 7:
            continue
        ym = pub[:7]  # YYYY-MM
        # Defensive: skip malformed dates rather than crash the build
        try:
            int(ym[:4]); int(ym[5:7])
        except ValueError:
            continue
        by_month.setdefault(ym, []).append(a.bias_score)

    out = []
    for ym in sorted(by_month):
        scores = by_month[ym]
        try:
            d = date(int(ym[:4]), int(ym[5:7]), 1)
            label = d.strftime("%b %Y")
        except ValueError:
            label = ym
        out.append(MonthlyStats(
            label=label, year_month=ym,
            count=len(scores),
            mean_bias=statistics.fmean(scores),
        ))
    return out


def _bucket_for(score: float) -> str:
    for lo, hi, label in HISTOGRAM_BUCKETS:
        if lo <= score < hi:
            return label
    return "center"


def compute_overall_stats(articles: list[AnalyzedArticle]) -> OverallStats:
    """Top-line counts: articles, distinct sources, distinct categories."""
    sources: set[str] = set()
    categories: set[str] = set()
    for a in articles:
        if a.original.source_name:
            sources.add(a.original.source_name)
        for cat in a.original.categories:
            if cat:
                categories.add(cat)
    return OverallStats(
        total_articles=len(articles),
        total_sources=len(sources),
        total_categories=len(categories),
    )


def compute_bias_distribution(articles: list[AnalyzedArticle]) -> BiasDistribution:
    """Three-bucket count using the legacy thresholds (0.48 / 0.52)."""
    left = center = right = 0
    for a in articles:
        if a.bias_score < LEFT_THRESHOLD:
            left += 1
        elif a.bias_score > RIGHT_THRESHOLD:
            right += 1
        else:
            center += 1
    return BiasDistribution(left=left, center=center, right=right)


def compute_category_stats(articles: list[AnalyzedArticle]) -> list[CategoryStats]:
    """Per-category mean bias + count. Sorted alphabetically by category name."""
    by_cat: dict[str, list[float]] = {}
    for a in articles:
        for cat in a.original.categories:
            if not cat:
                continue
            by_cat.setdefault(cat, []).append(a.bias_score)
    out = [
        CategoryStats(category=cat, count=len(scores),
                      mean_bias=statistics.fmean(scores))
        for cat, scores in by_cat.items()
    ]
    out.sort(key=lambda c: c.category.lower())
    return out


def top_leaning_sources(stats: list[SourceStats], *,
                        direction: str = "left",
                        n: int = 10,
                        min_articles: int = 3) -> list[SourceStats]:
    """Return the top-N most-leaning sources with at least `min_articles`.

    direction='left'  → sorted ascending by mean_bias (most left first)
    direction='right' → sorted descending (most right first)
    """
    eligible = [s for s in stats if s.count >= min_articles]
    return sorted(eligible, key=lambda s: s.mean_bias,
                  reverse=(direction == "right"))[:n]


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

    out.sort(key=lambda s: s.source_name.lower())
    return out
