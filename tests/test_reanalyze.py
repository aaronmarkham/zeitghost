"""Tests for `zeitghost reanalyze` — selection logic + end-to-end revision write."""

import json

import pytest
from click.testing import CliRunner

from zeitghost.bias import AnalyzedArticle
from zeitghost.fetcher import Article


def _analyzed(url, source="AP", published="2026-05-20T00:00:00+00:00", score=0.3):
    return AnalyzedArticle(
        original=Article(title=f"T {url}", url=url, summary="s", source_name=source,
                         published=published, categories=["politics"]),
        bias_score=score, bias_label="center",
        variant_left_title="L", variant_left_summary="ls",
        variant_right_title="R", variant_right_summary="rs",
    )


# --- select_for_reanalysis (pure) ------------------------------------------

def test_select_filters_by_source_name_or_slug():
    from zeitghost.shards import select_for_reanalysis
    arts = [_analyzed("https://e/1", source="Fox News"),
            _analyzed("https://e/2", source="CNN")]
    # Matches by display name and by slug form
    assert {a.original.url for a in select_for_reanalysis(arts, source="Fox News")} == {"https://e/1"}
    assert {a.original.url for a in select_for_reanalysis(arts, source="fox-news")} == {"https://e/1"}


def test_select_filters_by_since():
    from zeitghost.shards import select_for_reanalysis
    arts = [_analyzed("https://e/old", published="2026-01-01T00:00:00+00:00"),
            _analyzed("https://e/new", published="2026-05-30T00:00:00+00:00")]
    out = select_for_reanalysis(arts, since="2026-03-01")
    assert {a.original.url for a in out} == {"https://e/new"}


def test_select_limit_takes_newest_first():
    from zeitghost.shards import select_for_reanalysis
    arts = [_analyzed("https://e/jan", published="2026-01-01T00:00:00+00:00"),
            _analyzed("https://e/mar", published="2026-03-01T00:00:00+00:00"),
            _analyzed("https://e/may", published="2026-05-01T00:00:00+00:00")]
    out = select_for_reanalysis(arts, limit=2)
    assert [a.original.url for a in out] == ["https://e/may", "https://e/mar"]


def test_select_source_and_since_are_anded():
    """--source and --since combine (AND), not OR."""
    from zeitghost.shards import select_for_reanalysis
    arts = [
        _analyzed("https://e/fox-new", source="Fox News", published="2026-05-30T00:00:00+00:00"),
        _analyzed("https://e/fox-old", source="Fox News", published="2026-01-01T00:00:00+00:00"),
        _analyzed("https://e/cnn-new", source="CNN", published="2026-05-30T00:00:00+00:00"),
    ]
    out = select_for_reanalysis(arts, source="Fox News", since="2026-03-01")
    assert {a.original.url for a in out} == {"https://e/fox-new"}


# --- CLI end-to-end (fake provider) ----------------------------------------

class _FakeProvider:
    """Stands in for the Anthropic provider — returns valid analysis JSON."""
    def __init__(self, score):
        self.score = score
        self.calls = 0

    async def query(self, prompt, model=None):
        self.calls += 1
        return json.dumps({
            "bias_score": self.score, "bias_label": "right",
            "variant_left": {"title": "Lt", "summary": "ls"},
            "variant_right": {"title": "Rt", "summary": "rs"},
            "analysis_notes": "n",
        })


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """Point init_store at a tmp dir and clear signing env bleed."""
    monkeypatch.setenv("ZEITGHOST_SHARD_STORE", str(tmp_path / "shards"))
    monkeypatch.delenv("ZEITGHOST_SIGNING_KEY", raising=False)
    monkeypatch.delenv("ZEITGHOST_REQUIRE_SIGNING", raising=False)
    return tmp_path


def _seed_one_article(url="https://e.com/r1"):
    from zeitghost.shards import (init_store, article_to_internal_shard,
                                  article_to_sw_shard)
    store = init_store()
    a = _analyzed(url, score=0.3)
    first = article_to_internal_shard(a, store)
    article_to_sw_shard(a, store)
    return first


def test_reanalyze_writes_chained_revision(store_env, monkeypatch):
    """End-to-end: an existing article is re-scored and written as a new shard
    that chains onto the original (parent_shard_id) and records the new model."""
    from zeitghost.cli import main
    from zeitghost.shards import init_store, load_articles_from_shards

    first_internal = _seed_one_article()
    fake = _FakeProvider(score=0.85)
    monkeypatch.setattr("zeitghost.bias._get_provider", lambda: fake)

    result = CliRunner().invoke(
        main, ["reanalyze", "--limit", "1", "--model", "claude-test-x"])
    assert result.exit_code == 0, result.output
    assert fake.calls == 1

    [latest] = load_articles_from_shards(init_store())
    assert latest.parent_shard_id == first_internal   # chained as a revision
    assert latest.model == "claude-test-x"            # new producer recorded
    assert latest.bias_score == pytest.approx(0.85)   # re-scored


def test_reanalyze_refuses_whole_corpus(store_env, monkeypatch):
    """Bare `reanalyze` (no --limit/--source/--since) is refused, never calls Claude."""
    from zeitghost.cli import main
    _seed_one_article()
    fake = _FakeProvider(score=0.85)
    monkeypatch.setattr("zeitghost.bias._get_provider", lambda: fake)

    result = CliRunner().invoke(main, ["reanalyze"])
    assert result.exit_code != 0
    assert "Refusing to re-analyze" in result.output
    assert fake.calls == 0


def test_reanalyze_dry_run_makes_no_calls_or_writes(store_env, monkeypatch):
    from zeitghost.cli import main
    from zeitghost.shards import init_store, load_articles_from_shards

    _seed_one_article()
    fake = _FakeProvider(score=0.85)
    monkeypatch.setattr("zeitghost.bias._get_provider", lambda: fake)

    result = CliRunner().invoke(main, ["reanalyze", "--limit", "1", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert fake.calls == 0                            # no Claude spend
    [only] = load_articles_from_shards(init_store())
    assert only.parent_shard_id is None               # no revision written


def test_reanalyze_rejects_malformed_since(store_env, monkeypatch):
    """A non-YYYY-MM-DD --since fails loud rather than silently mis-comparing."""
    from zeitghost.cli import main
    _seed_one_article()
    fake = _FakeProvider(score=0.85)
    monkeypatch.setattr("zeitghost.bias._get_provider", lambda: fake)

    result = CliRunner().invoke(main, ["reanalyze", "--since", "5/1/2026"])
    assert result.exit_code != 0
    assert "YYYY-MM-DD" in result.output
    assert fake.calls == 0


def test_reanalyze_stamps_model_via_analyze_article(store_env, monkeypatch):
    """The .model cleanup: analyze_article itself records the model (not a
    caller-side patch), so a re-scored shard carries the chosen model."""
    from zeitghost.cli import main
    from zeitghost.shards import init_store, load_articles_from_shards

    _seed_one_article()
    monkeypatch.setattr("zeitghost.bias._get_provider", lambda: _FakeProvider(0.6))
    result = CliRunner().invoke(
        main, ["reanalyze", "--limit", "1", "--model", "claude-haiku-test"])
    assert result.exit_code == 0, result.output
    [latest] = load_articles_from_shards(init_store())
    assert latest.model == "claude-haiku-test"
