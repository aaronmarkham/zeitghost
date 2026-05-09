"""Smoke tests — verify the package imports and core dataclasses behave."""

from pathlib import Path

import pytest


def test_package_imports():
    import zeitghost
    assert zeitghost.__version__


def test_article_dataclass():
    from zeitghost.fetcher import Article
    a = Article(title="t", url="https://x/y", summary="s",
                source_name="src", published="2026-05-07T00:00:00+00:00")
    assert a.region == "national"
    assert a.categories == []
    assert a.body == ""           # transient body field defaults to empty
    assert a.legacy_id is None    # imported-only field


def test_bias_prompt_uses_body_when_present():
    """analyze_article should send the trafilatura-fetched body to Claude
    when one is available, falling back to summary otherwise. We verify by
    formatting the prompt template and checking which content reached it."""
    from zeitghost.bias import ANALYSIS_PROMPT
    from zeitghost.fetcher import Article

    a = Article(title="T", url="https://x", summary="terse newsapi blurb",
                source_name="s", published="2026-05-08T00:00:00+00:00",
                body="Full article body fetched via trafilatura. Multiple paragraphs.")
    # Replicate the same content selection used inside analyze_article
    content = a.body if a.body else a.summary
    rendered = ANALYSIS_PROMPT.format(title=a.title, content=content,
                                      source_name=a.source_name)
    assert "Full article body fetched via trafilatura" in rendered
    assert "terse newsapi blurb" not in rendered

    # Fallback path: no body → summary used
    a.body = ""
    content = a.body if a.body else a.summary
    rendered = ANALYSIS_PROMPT.format(title=a.title, content=content,
                                      source_name=a.source_name)
    assert "terse newsapi blurb" in rendered


def test_analyzed_article_dataclass():
    from zeitghost.bias import AnalyzedArticle
    from zeitghost.fetcher import Article
    a = AnalyzedArticle(
        original=Article(title="t", url="https://x/y", summary="s",
                         source_name="src", published="2026-05-07T00:00:00+00:00"),
        bias_score=0.42,
        bias_label="center",
        variant_left_title="L", variant_left_summary="L sum",
        variant_right_title="R", variant_right_summary="R sum",
    )
    assert a.bias_label == "center"


def test_permalink_uses_legacy_id_when_present():
    """Imported HtmxNewsEngine articles must keep their /article/<int> URL
    so old shared links resolve."""
    from zeitghost.bias import AnalyzedArticle
    from zeitghost.fetcher import Article
    a = AnalyzedArticle(
        original=Article(title="t", url="https://x/y", summary="s",
                         source_name="src", published="2026-05-07T00:00:00+00:00",
                         legacy_id=46274),
        bias_score=0.5, bias_label="center",
        variant_left_title="", variant_left_summary="",
        variant_right_title="", variant_right_summary="",
    )
    assert a.permalink_slug == "46274"
    assert a.permalink == "article/46274.html"


def test_bias_tint_inline_exposes_css_variables():
    """Card inline style sets --bias-r/g/b CSS custom properties so the
    stylesheet can derive border + background tint from a single source."""
    from zeitghost.bias import AnalyzedArticle
    from zeitghost.fetcher import Article

    a = AnalyzedArticle(
        original=Article(title="t", url="https://x", summary="s",
                         source_name="s", published="2026-05-07T00:00:00+00:00"),
        bias_score=0.0, bias_label="left",
        variant_left_title="", variant_left_summary="",
        variant_right_title="", variant_right_summary="",
    )
    inline = a.bias_tint_inline
    assert "--bias-r: 79" in inline
    assert "--bias-g: 140" in inline
    assert "--bias-b: 201" in inline


def test_bias_tint_interpolates_blue_to_red():
    """Card border color: bias=0 → blue, bias=1 → red, bias=0.5 → midpoint."""
    from zeitghost.bias import AnalyzedArticle
    from zeitghost.fetcher import Article

    def tint(score):
        return AnalyzedArticle(
            original=Article(title="t", url="https://x", summary="s",
                             source_name="s", published="2026-05-07T00:00:00+00:00"),
            bias_score=score, bias_label="x",
            variant_left_title="", variant_left_summary="",
            variant_right_title="", variant_right_summary="",
        ).bias_tint

    # Endpoints land on the existing CSS palette (--left / --right)
    assert tint(0.0) == "rgb(79, 140, 201)"   # CSS --left
    assert tint(1.0) == "rgb(217, 100, 88)"   # CSS --right
    # Midpoint is a smooth interpolation, not pure gray
    mid = tint(0.5)
    assert mid.startswith("rgb(") and mid.endswith(")")
    # Sanity: midpoint should fall between the endpoints on R and B channels
    assert "rgb(148, 120, 144)" == mid  # exact midpoint of (79..217, 140..100, 201..88)


def test_permalink_falls_back_to_url_hash():
    """New articles (no legacy_id) get a stable hash-based permalink."""
    from zeitghost.bias import AnalyzedArticle
    from zeitghost.fetcher import Article
    a = AnalyzedArticle(
        original=Article(title="t", url="https://example.com/specific-story",
                         summary="s", source_name="src",
                         published="2026-05-07T00:00:00+00:00"),
        bias_score=0.5, bias_label="center",
        variant_left_title="", variant_left_summary="",
        variant_right_title="", variant_right_summary="",
    )
    # 10-char SHA-256 prefix of the URL
    assert len(a.permalink_slug) == 10
    assert a.permalink_slug.isalnum()
    # Stable: same URL always produces the same slug
    assert a.permalink_slug == AnalyzedArticle(
        original=Article(title="other", url="https://example.com/specific-story",
                         summary="x", source_name="x",
                         published="2026-05-07T00:00:00+00:00"),
        bias_score=0.1, bias_label="left",
        variant_left_title="", variant_left_summary="",
        variant_right_title="", variant_right_summary="",
    ).permalink_slug


def test_extract_json_strips_fences():
    from zeitghost.bias import _extract_json
    raw = "```json\n{\"bias_score\": 0.5}\n```"
    assert _extract_json(raw) == {"bias_score": 0.5}


def test_analytics_buckets_label():
    from zeitghost.analytics import _bucket_for
    assert _bucket_for(0.1) == "left"
    assert _bucket_for(0.5) == "center"
    assert _bucket_for(0.95) == "right"


def _mk(source, score, categories=None):
    from zeitghost.bias import AnalyzedArticle
    from zeitghost.fetcher import Article
    return AnalyzedArticle(
        original=Article(title="t", url=f"https://x/{source}-{score}", summary="s",
                         source_name=source, published="2026-05-07T00:00:00+00:00",
                         categories=categories or []),
        bias_score=score, bias_label="center",
        variant_left_title="", variant_left_summary="",
        variant_right_title="", variant_right_summary="",
    )


def test_analytics_compute_stats():
    from zeitghost.analytics import compute_source_stats

    articles = [_mk("CNN", 0.3), _mk("CNN", 0.4), _mk("CNN", 0.5),
                _mk("FOX", 0.7), _mk("FOX", 0.8)]
    stats = compute_source_stats(articles)
    assert stats[0].source_name == "CNN"
    assert stats[0].count == 3
    assert 0.39 < stats[0].mean_bias < 0.41
    assert stats[1].source_name == "FOX"
    assert stats[1].count == 2


def test_analytics_overall_stats():
    from zeitghost.analytics import compute_overall_stats

    articles = [_mk("CNN", 0.3, ["politics"]),
                _mk("CNN", 0.4, ["economy"]),
                _mk("FOX", 0.8, ["politics"])]
    overall = compute_overall_stats(articles)
    assert overall.total_articles == 3
    assert overall.total_sources == 2
    assert overall.total_categories == 2  # politics + economy


def test_analytics_bias_distribution_legacy_thresholds():
    from zeitghost.analytics import compute_bias_distribution

    # 0.48/0.52 thresholds match the HtmxNewsEngine /analytics page so user-
    # facing numbers stay consistent across the migration.
    articles = [_mk("a", 0.10), _mk("b", 0.47),       # left  (< 0.48)
                _mk("c", 0.50), _mk("d", 0.52),       # center (0.48..0.52)
                _mk("e", 0.53), _mk("f", 0.95)]       # right  (> 0.52)
    dist = compute_bias_distribution(articles)
    assert (dist.left, dist.center, dist.right) == (2, 2, 2)
    assert dist.total == 6
    assert dist.pct(2) == pytest.approx(33.33, abs=0.01)


def test_analytics_top_leaning_filters_min_articles():
    from zeitghost.analytics import compute_source_stats, top_leaning_sources

    # Source 'lonely' only has 2 articles → must be excluded (min=3 default)
    articles = ([_mk("lefty", 0.1)] * 3
                + [_mk("righty", 0.9)] * 4
                + [_mk("lonely", 0.05)] * 2)
    stats = compute_source_stats(articles)
    left = top_leaning_sources(stats, direction="left")
    right = top_leaning_sources(stats, direction="right")
    left_names = [s.source_name for s in left]
    right_names = [s.source_name for s in right]
    assert "lonely" not in left_names
    assert left_names[0] == "lefty"
    assert right_names[0] == "righty"


def test_source_slug_handles_punctuation_and_case():
    from zeitghost.analytics import source_slug
    assert source_slug("Fox News") == "fox-news"
    assert source_slug("The New York Times") == "the-new-york-times"
    assert source_slug("CNN.com") == "cnn-com"
    assert source_slug("AP / Wire") == "ap-wire"
    assert source_slug("---") == "unknown"  # falls back when stripped empty
    assert source_slug("Source #1!") == "source-1"


def test_monthly_stats_sorted_chronologically():
    """Per-source monthly bias-drift table must read oldest → newest so a
    reader scanning left-to-right sees the time progression naturally."""
    from zeitghost.analytics import compute_monthly_stats

    articles = [
        _mk("Fox", 0.85), _mk("Fox", 0.75),  # default published = 2026-05-07 (May)
    ]
    # Retroactively age some articles into earlier months
    articles[0].original.published = "2026-01-15T00:00:00+00:00"
    articles[1].original.published = "2026-03-20T00:00:00+00:00"
    articles.extend([_mk("Fox", 0.65)])  # May 2026 (default)

    months = compute_monthly_stats(articles)
    assert [m.year_month for m in months] == ["2026-01", "2026-03", "2026-05"]
    assert [m.label for m in months] == ["Jan 2026", "Mar 2026", "May 2026"]
    assert months[0].mean_bias == pytest.approx(0.85)
    assert months[2].count == 1


def test_monthly_stats_skips_malformed_dates():
    from zeitghost.analytics import compute_monthly_stats

    articles = [_mk("a", 0.5), _mk("b", 0.5), _mk("c", 0.5)]
    articles[0].original.published = ""  # missing
    articles[1].original.published = "not-a-date"
    articles[2].original.published = "2026-04-10T00:00:00+00:00"

    months = compute_monthly_stats(articles)
    assert len(months) == 1
    assert months[0].year_month == "2026-04"


def test_analytics_category_stats_sorted_alphabetically():
    """Categories sort alphabetically (changed from left-to-right per
    user feedback — fixed list is easier to scan when ordered by name)."""
    from zeitghost.analytics import compute_category_stats

    articles = [_mk("a", 0.2, ["progressive"]),
                _mk("b", 0.3, ["progressive"]),
                _mk("c", 0.5, ["mixed"]),
                _mk("d", 0.85, ["conservative"])]
    cats = compute_category_stats(articles)
    assert [c.category for c in cats] == ["conservative", "mixed", "progressive"]
    # Lean labels still reflect bias (independent of sort order)
    by_name = {c.category: c for c in cats}
    assert by_name["progressive"].lean == "left"
    assert by_name["mixed"].lean == "center"
    assert by_name["conservative"].lean == "right"


def test_legacy_dump_helpers_smoke():
    """Verify the legacy importer's pure helpers handle datetime + bias label."""
    from datetime import datetime, timezone

    from scripts.import_legacy_dump import _to_iso, _label_for

    assert _to_iso(None).endswith("+00:00")
    dt = datetime(2026, 5, 7, 12, 0, 0)
    iso = _to_iso(dt)
    assert iso.startswith("2026-05-07T12:00:00")
    assert "+00:00" in iso

    assert _label_for(0.1) == "left"
    assert _label_for(0.5) == "center"
    assert _label_for(0.95) == "right"
