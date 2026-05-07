"""Smoke tests — verify the package imports and core dataclasses behave."""

from pathlib import Path


def test_package_imports():
    import zeitghost
    assert zeitghost.__version__


def test_article_dataclass():
    from zeitghost.fetcher import Article
    a = Article(title="t", url="https://x/y", summary="s",
                source_name="src", published="2026-05-07T00:00:00+00:00")
    assert a.region == "national"
    assert a.categories == []


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


def test_extract_json_strips_fences():
    from zeitghost.bias import _extract_json
    raw = "```json\n{\"bias_score\": 0.5}\n```"
    assert _extract_json(raw) == {"bias_score": 0.5}


def test_analytics_buckets_label():
    from zeitghost.analytics import _bucket_for
    assert _bucket_for(0.1) == "left"
    assert _bucket_for(0.5) == "center"
    assert _bucket_for(0.95) == "right"


def test_analytics_compute_stats():
    from zeitghost.analytics import compute_source_stats
    from zeitghost.bias import AnalyzedArticle
    from zeitghost.fetcher import Article

    def mk(source, score):
        return AnalyzedArticle(
            original=Article(title="t", url=f"https://x/{source}-{score}", summary="s",
                             source_name=source, published="2026-05-07T00:00:00+00:00"),
            bias_score=score, bias_label="center",
            variant_left_title="", variant_left_summary="",
            variant_right_title="", variant_right_summary="",
        )

    articles = [mk("CNN", 0.3), mk("CNN", 0.4), mk("CNN", 0.5),
                mk("FOX", 0.7), mk("FOX", 0.8)]
    stats = compute_source_stats(articles)
    assert stats[0].source_name == "CNN"
    assert stats[0].count == 3
    assert 0.39 < stats[0].mean_bias < 0.41
    assert stats[1].source_name == "FOX"
    assert stats[1].count == 2


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
