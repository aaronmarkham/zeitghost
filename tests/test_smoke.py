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
    # Endpoints match the CSS palette: --left #2C5274 (44, 82, 116) at bias=0
    assert "--bias-r: 44" in inline
    assert "--bias-g: 82" in inline
    assert "--bias-b: 116" in inline


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

    # Endpoints land on the CSS palette (--left oxford blue, --right terracotta)
    assert tint(0.0) == "rgb(44, 82, 116)"    # CSS --left
    assert tint(1.0) == "rgb(176, 70, 58)"    # CSS --right
    # Midpoint is a smooth interpolation between the endpoints
    mid = tint(0.5)
    assert mid.startswith("rgb(") and mid.endswith(")")
    assert "rgb(110, 76, 87)" == mid  # exact midpoint of (44..176, 82..70, 116..58)


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


def test_bias_lean_display_center_band():
    """Values within ±tolerance of 0.5 render as the verbal label CENTER
    (no near-zero magnitude like 'R · .00' or 'L · .01')."""
    from zeitghost.bias import bias_lean_display
    assert bias_lean_display(0.50) == "CENTER"
    assert bias_lean_display(0.49) == "CENTER"
    assert bias_lean_display(0.51) == "CENTER"
    # Just outside the default ±0.025 band — direction + magnitude
    assert bias_lean_display(0.475) != "CENTER"
    assert bias_lean_display(0.525) != "CENTER"


def test_bias_lean_display_direction_and_magnitude():
    """Above 0.5 → R, below → L. Each side runs 0–100% (magnitude is
    |tilt|×200), so the slider's endpoints read as full L / full R."""
    from zeitghost.bias import bias_lean_display
    assert bias_lean_display(0.74) == "R · 48%"
    assert bias_lean_display(0.32) == "L · 36%"
    assert bias_lean_display(0.04) == "L · 92%"
    assert bias_lean_display(0.96) == "R · 92%"
    # Endpoints — full deflection on each side
    assert bias_lean_display(0.0) == "L · 100%"
    assert bias_lean_display(1.0) == "R · 100%"
    # Just outside the center band
    assert bias_lean_display(0.535) == "R · 7%"
    assert bias_lean_display(0.58) == "R · 16%"


def test_bias_lean_display_tolerance_parameter():
    """Center tolerance can be tightened or loosened per-caller."""
    from zeitghost.bias import bias_lean_display
    # Tight tolerance: only literal 0.5 reads as CENTER
    assert bias_lean_display(0.50, center_tolerance=0.001) == "CENTER"
    assert bias_lean_display(0.49, center_tolerance=0.001) != "CENTER"
    # Loose tolerance: a wider band
    assert bias_lean_display(0.45, center_tolerance=0.1) == "CENTER"


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


def test_article_tags_compose_source_category_month():
    """Cross-cutting tags let the shard store filter by source/category/month
    without scanning every shard's atoms."""
    from zeitghost.shards import _article_tags

    a = _mk("Fox News", 0.7, ["politics", "economy"])
    a.original.published = "2026-05-09T14:30:00+00:00"
    tags = _article_tags(a)
    assert "source:fox-news" in tags
    assert "category:politics" in tags
    assert "category:economy" in tags
    assert "month:2026-05" in tags


def test_article_tags_handles_missing_fields():
    from zeitghost.shards import _article_tags

    a = _mk("", 0.5, [])
    a.original.published = ""
    # Empty source/published produces an empty tag list — never crashes
    assert _article_tags(a) == []


def test_tags_from_shard_matches_article_tags():
    """tags_from_shard (used by the migration script to read existing shards)
    must produce the same result as _article_tags would for the same data —
    otherwise the in-place migration creates inconsistent tagging."""
    from spiritwriter.fabric.shard import MemoryShard, ShardAtom, AtomKind, DecayClass
    from zeitghost.shards import _article_tags, tags_from_shard

    a = _mk("Fox News", 0.7, ["politics", "economy"])
    a.original.published = "2026-05-09T14:30:00+00:00"

    # Build a shard mimicking what article_to_internal_shard would have written
    # (back when tags weren't being set)
    entity = "article:somehash"
    atoms = [
        ShardAtom(text="x", kind=AtomKind.FACT, entity=entity,
                  key="source_name", value="Fox News"),
        ShardAtom(text="x", kind=AtomKind.FACT, entity=entity,
                  key="categories", value='["politics", "economy"]'),
        ShardAtom(text="x", kind=AtomKind.FACT, entity=entity,
                  key="published", value="2026-05-09T14:30:00+00:00"),
    ]
    untagged = MemoryShard(
        atoms=atoms, scope="zeitghost:article", origin="zeitghost",
        decay_class=DecayClass.STABLE, meta={"entity_key": entity},
    )

    assert tags_from_shard(untagged) == _article_tags(a)


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


def test_sparkline_handles_empty_input():
    from zeitghost.analytics import render_bias_sparkline
    assert render_bias_sparkline([]) == ""


def test_sparkline_renders_basic_shape():
    """Multi-month sparkline must include an SVG, the trajectory path, a
    dashed center reference line, and one circle per data point with a
    <title> for hover."""
    from zeitghost.analytics import compute_monthly_stats, render_bias_sparkline

    articles = [_mk("Fox", 0.85), _mk("Fox", 0.55), _mk("Fox", 0.65)]
    articles[0].original.published = "2026-01-15T00:00:00+00:00"
    articles[1].original.published = "2026-02-20T00:00:00+00:00"
    articles[2].original.published = "2026-03-10T00:00:00+00:00"

    svg = render_bias_sparkline(compute_monthly_stats(articles))
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    # Trajectory path with three points
    assert '<path d="M ' in svg
    # Dashed reference line
    assert 'stroke-dasharray="3,3"' in svg
    # One <circle> per month, each with a hover <title>
    assert svg.count("<circle") == 3
    assert svg.count("<title>") == 3
    # Hover text uses the symmetric lean display (no raw 0..1 leakage)
    assert "Jan 2026: R · 70%" in svg
    assert "Feb 2026: R · 10%" in svg
    assert "Mar 2026: R · 30%" in svg


def test_sparkline_single_month_renders_just_a_dot():
    """Edge case: source has exactly one month of data — render a single
    centered dot rather than a degenerate zero-length line."""
    from zeitghost.analytics import compute_monthly_stats, render_bias_sparkline

    articles = [_mk("X", 0.4)]
    articles[0].original.published = "2026-04-01T00:00:00+00:00"
    svg = render_bias_sparkline(compute_monthly_stats(articles))
    assert "<svg" in svg
    assert svg.count("<circle") == 1
    assert "<path" not in svg  # no trajectory path with a single point


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


def test_shard_metadata_round_trip(tmp_path):
    """An article written then re-read from a real ShardStore must carry
    its shard_id / parent_shard_id / model / agent / atom_kinds / tags onto
    the AnalyzedArticle so the card flip-panel has something to render."""
    from zeitghost.bias import AnalyzedArticle
    from zeitghost.fetcher import Article
    from zeitghost.shards import (
        init_store, article_to_internal_shard, load_articles_from_shards,
        build_lineage_index, SCOPE_INTERNAL,
    )

    store = init_store(tmp_path / "shards")
    a = AnalyzedArticle(
        original=Article(title="T", url="https://e.com/round-trip",
                         summary="S", source_name="AP",
                         published="2026-05-12T00:00:00+00:00",
                         categories=["politics"]),
        bias_score=0.52, bias_label="center",
        variant_left_title="L", variant_left_summary="L sum",
        variant_right_title="R", variant_right_summary="R sum",
    )

    # First write — no parent, fresh provenance atoms get added
    shard_id_1 = article_to_internal_shard(a, store)
    assert shard_id_1

    [reloaded] = load_articles_from_shards(store)
    # Identity + lineage are populated
    assert reloaded.shard_id == shard_id_1
    assert reloaded.parent_shard_id is None
    assert reloaded.shard_scope == SCOPE_INTERNAL
    assert reloaded.shard_decay  # non-empty, e.g. "stable"
    assert reloaded.shard_created_at  # ISO timestamp from spiritwriter
    # Provenance atoms made it through
    assert reloaded.model        # DEFAULT_MODEL was embedded
    assert reloaded.agent        # zeitghost/<ver> sw_core/<ver>
    assert "zeitghost/" in reloaded.agent
    # Atom kinds tallied — at least one FACT and one DECISION on every article
    assert reloaded.shard_atom_kinds
    assert sum(reloaded.shard_atom_kinds.values()) >= 8
    # Cross-cutting tags computed and round-tripped
    assert "source:ap" in reloaded.shard_tags
    assert "category:politics" in reloaded.shard_tags
    assert "month:2026-05" in reloaded.shard_tags

    # Second write — different content forces a new shard_id, parent links
    a.bias_score = 0.74  # re-analysis nudged the score
    lineage = build_lineage_index(store, SCOPE_INTERNAL)
    shard_id_2 = article_to_internal_shard(a, store, lineage_index=lineage)
    assert shard_id_2 != shard_id_1

    # Loading now returns both revisions; the newer one points at the older
    revisions = load_articles_from_shards(store)
    by_id = {r.shard_id: r for r in revisions}
    assert by_id[shard_id_2].parent_shard_id == shard_id_1


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
