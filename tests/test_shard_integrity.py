"""Shard integrity & lineage-correctness tests.

Covers three behaviors:
- #5  bias_score is never default-filled (skip an unscored article).
- #2  load collapses a revision chain to the latest shard per entity.
- #4  shards are signed when (and only when) a signing seed is configured.
"""

import os

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from zeitghost.bias import AnalyzedArticle
from zeitghost.fetcher import Article


def _article(url, score=0.5):
    return AnalyzedArticle(
        original=Article(title="T", url=url, summary="S", source_name="AP",
                         published="2026-05-12T00:00:00+00:00",
                         categories=["politics"]),
        bias_score=score, bias_label="center",
        variant_left_title="L", variant_left_summary="L sum",
        variant_right_title="R", variant_right_summary="R sum",
    )


def _pubkey(seed: bytes) -> bytes:
    return (Ed25519PrivateKey.from_private_bytes(seed).public_key()
            .public_bytes(encoding=serialization.Encoding.Raw,
                         format=serialization.PublicFormat.Raw))


# --- #5: bias_score skip-not-default --------------------------------------

def test_parse_bias_score_skips_missing_not_defaults():
    """A missing or non-numeric score yields None (caller skips) — never a
    silent 0.5 that would mislabel an unscored article as 'center'."""
    from zeitghost.bias import _parse_bias_score

    # Present and numeric — passes through, including the valid 0.0 edge.
    assert _parse_bias_score({"bias_score": 0.0}) == 0.0
    assert _parse_bias_score({"bias_score": 0.73}) == pytest.approx(0.73)
    assert _parse_bias_score({"bias_score": "0.7"}) == pytest.approx(0.7)
    # Absent / null / unparseable — None, so analyze_article returns None.
    assert _parse_bias_score({}) is None
    assert _parse_bias_score({"bias_score": None}) is None
    assert _parse_bias_score({"bias_score": "left-ish"}) is None
    assert _parse_bias_score({"bias_score": {}}) is None


# --- #2: load collapses revision chains to latest --------------------------

def test_load_returns_latest_revision_only(tmp_path):
    """Two revisions of the same article (chained via parent_shard_id) collapse
    to a single card on load — the newest one."""
    from zeitghost.shards import (
        init_store, article_to_internal_shard, load_articles_from_shards,
        build_lineage_index, SCOPE_INTERNAL,
    )
    store = init_store(tmp_path / "shards")
    url = "https://e.com/evolving-story"

    a = _article(url, score=0.30)
    first = article_to_internal_shard(a, store)

    a2 = _article(url, score=0.80)  # re-analysis, same URL → same entity
    lineage = build_lineage_index(store, SCOPE_INTERNAL)
    second = article_to_internal_shard(a2, store, lineage_index=lineage)
    assert second != first

    loaded = load_articles_from_shards(store)
    assert len(loaded) == 1
    assert loaded[0].shard_id == second
    assert loaded[0].parent_shard_id == first
    assert loaded[0].bias_score == pytest.approx(0.80)


def test_load_keeps_distinct_entities_separate(tmp_path):
    """Collapse is per-entity — two different articles both load."""
    from zeitghost.shards import (
        init_store, article_to_internal_shard, load_articles_from_shards,
    )
    store = init_store(tmp_path / "shards")
    article_to_internal_shard(_article("https://e.com/a"), store)
    article_to_internal_shard(_article("https://e.com/b"), store)

    loaded = load_articles_from_shards(store)
    assert {a.original.url for a in loaded} == {"https://e.com/a", "https://e.com/b"}


# --- #4: opt-in signing ----------------------------------------------------

def test_shard_unsigned_without_seed(tmp_path):
    from spiritwriter.fabric.store import ShardStore  # noqa: F401 (type clarity)
    from zeitghost.shards import (
        init_store, article_to_internal_shard, SCOPE_INTERNAL,
    )
    store = init_store(tmp_path / "shards")
    article_to_internal_shard(_article("https://e.com/unsigned"), store)

    [shard] = list(store.by_scope(SCOPE_INTERNAL))
    assert shard.signature is None
    assert shard.created_by is None


def test_shard_signed_with_seed_verifies_and_round_trips(tmp_path):
    """A signed shard persists its signature + created_by, and the signature
    verifies against the seed's public key after a store round-trip."""
    from spiritwriter.fabric.shard import pubkey_thumbprint
    from zeitghost.shards import (
        init_store, article_to_internal_shard, article_to_sw_shard,
        SCOPE_INTERNAL, SCOPE_SW_ARTICLE,
    )
    seed = os.urandom(32)
    pub = _pubkey(seed)
    store = init_store(tmp_path / "shards")

    article_to_internal_shard(_article("https://e.com/signed"), store,
                              signing_seed=seed)
    article_to_sw_shard(_article("https://e.com/signed"), store,
                        signing_seed=seed)

    for scope in (SCOPE_INTERNAL, SCOPE_SW_ARTICLE):
        [shard] = list(store.by_scope(scope))
        assert shard.signature is not None
        assert shard.created_by == pubkey_thumbprint(pub)
        # verify() raises on a bad signature; True means the chain holds.
        assert shard.verify(pub) is True


# --- resolve_signing_seed --------------------------------------------------

def test_resolve_signing_seed_from_env(monkeypatch):
    from zeitghost.shards import resolve_signing_seed, SIGNING_KEY_NAME
    seed = os.urandom(32)
    monkeypatch.setenv(SIGNING_KEY_NAME, seed.hex())
    assert resolve_signing_seed() == seed


def test_resolve_signing_seed_absent_is_none(monkeypatch):
    from zeitghost.shards import resolve_signing_seed, SIGNING_KEY_NAME
    monkeypatch.delenv(SIGNING_KEY_NAME, raising=False)
    # No env var (and the test keychain won't have this key) → opt-out.
    assert resolve_signing_seed() is None


def test_resolve_signing_seed_malformed_is_none(monkeypatch):
    """A fat-fingered key degrades to unsigned rather than crashing ingest."""
    from zeitghost.shards import resolve_signing_seed, SIGNING_KEY_NAME
    monkeypatch.setenv(SIGNING_KEY_NAME, "not-hex-at-all")
    assert resolve_signing_seed() is None
    monkeypatch.setenv(SIGNING_KEY_NAME, "ab")  # valid hex, but 1 byte ≠ 32
    assert resolve_signing_seed() is None
