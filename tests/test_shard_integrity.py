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


def test_tampered_signature_fails_verify(tmp_path):
    """Flipping a byte of the signature must make verify() reject it — guards
    against a future regression that signs the wrong payload."""
    from cryptography.exceptions import InvalidSignature
    from zeitghost.shards import (
        init_store, article_to_internal_shard, SCOPE_INTERNAL,
    )
    seed = os.urandom(32)
    pub = _pubkey(seed)
    store = init_store(tmp_path / "shards")
    article_to_internal_shard(_article("https://e.com/tamper"), store,
                              signing_seed=seed)

    [shard] = list(store.by_scope(SCOPE_INTERNAL))
    # Corrupt one hex nibble of the signature (wraps so 'f' stays valid hex).
    sig = shard.signature
    flipped = ("e" if sig[0] != "e" else "d") + sig[1:]
    shard.signature = flipped
    with pytest.raises(InvalidSignature):
        shard.verify(pub)


# --- #5 end-to-end: analyze_article drops an unscored article --------------

def test_analyze_article_returns_none_when_bias_score_missing(monkeypatch):
    """End-to-end: when the LLM response omits bias_score, analyze_article
    returns None (skip) rather than constructing a default-0.5 article."""
    import asyncio
    import zeitghost.bias as bias

    class _FakeProvider:
        async def query(self, prompt, model=None):
            # Valid JSON, variants present, but NO bias_score key.
            return ('{"bias_label": "center", '
                    '"variant_left": {"title": "L", "summary": "ls"}, '
                    '"variant_right": {"title": "R", "summary": "rs"}}')

    monkeypatch.setattr(bias, "_get_provider", lambda: _FakeProvider())

    art = Article(title="T", url="https://e.com/no-score", summary="s",
                  source_name="src", published="2026-05-12T00:00:00+00:00")
    assert asyncio.run(bias.analyze_article(art)) is None


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


# --- signing_required (prod fail-closed switch) ----------------------------

def test_signing_required_off_by_default(monkeypatch):
    from zeitghost.shards import signing_required
    monkeypatch.delenv("ZEITGHOST_REQUIRE_SIGNING", raising=False)
    assert signing_required() is False
    assert signing_required(flag=False) is False


def test_signing_required_flag_wins(monkeypatch):
    from zeitghost.shards import signing_required
    monkeypatch.delenv("ZEITGHOST_REQUIRE_SIGNING", raising=False)
    assert signing_required(flag=True) is True


def test_signing_required_env_truthy_variants(monkeypatch):
    from zeitghost.shards import signing_required
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("ZEITGHOST_REQUIRE_SIGNING", truthy)
        assert signing_required() is True, truthy
    for falsy in ("0", "false", "no", "", "off"):
        monkeypatch.setenv("ZEITGHOST_REQUIRE_SIGNING", falsy)
        assert signing_required() is False, falsy


# --- #7 trace emitter ------------------------------------------------------

def test_no_trace_ref_without_emitter(tmp_path):
    """Tracing is opt-in: without an emitter, shards carry no trace_ref."""
    from zeitghost.shards import (
        init_store, article_to_internal_shard, SCOPE_INTERNAL,
    )
    store = init_store(tmp_path / "shards")
    article_to_internal_shard(_article("https://e.com/untraced"), store)
    [shard] = list(store.by_scope(SCOPE_INTERNAL))
    assert shard.trace_ref is None


def test_emitter_stamps_trace_ref_and_chain_verifies(tmp_path):
    """With an emitter, each written shard gets a trace_ref pointing at its
    shard_created event, and the run's event chain verifies."""
    from spiritwriter.fabric.emitter import verify_chain
    from zeitghost.shards import (
        init_store, init_trace_emitter, article_to_internal_shard,
        article_to_sw_shard, SCOPE_INTERNAL,
    )
    store = init_store(tmp_path / "shards")
    emitter, trace_path = init_trace_emitter(store, run_id="ingest-test-1234")

    a = _article("https://e.com/traced")
    article_to_internal_shard(a, store, emitter=emitter)
    article_to_sw_shard(a, store, emitter=emitter)

    [internal] = list(store.by_scope(SCOPE_INTERNAL))
    assert internal.trace_ref is not None
    assert internal.trace_ref.startswith("chain:ingest-test-1234#")

    events = emitter.get_events()
    # Two shard_created events (internal + sw), no supersede on a first write.
    assert [e["type"] for e in events] == ["shard_created", "shard_created"]
    assert verify_chain(events) is True
    assert trace_path.exists()


def test_emitter_emits_shard_superseded_on_revision(tmp_path):
    """Re-analysis (a write that chains onto a parent) emits shard_superseded
    linking new→old — the event the load-side dedup has no signal for yet."""
    from zeitghost.shards import (
        init_store, init_trace_emitter, article_to_internal_shard,
        build_lineage_index, SCOPE_INTERNAL,
    )
    store = init_store(tmp_path / "shards")
    emitter, _ = init_trace_emitter(store, run_id="ingest-test-rev")
    url = "https://e.com/revised"

    first = article_to_internal_shard(_article(url, 0.3), store, emitter=emitter)
    a2 = _article(url, 0.8)
    lineage = build_lineage_index(store, SCOPE_INTERNAL)
    second = article_to_internal_shard(a2, store, lineage_index=lineage, emitter=emitter)

    types = [e["type"] for e in emitter.get_events()]
    assert types == ["shard_created", "shard_created", "shard_superseded"]
    sup = emitter.get_events()[-1]
    assert sup["old_shard_id"] == first and sup["new_shard_id"] == second


def test_trace_ref_and_signer_round_trip_to_article(tmp_path):
    """A signed + traced shard surfaces both signed_by and trace_ref on the
    reconstructed AnalyzedArticle (what the flip-panel renders)."""
    from spiritwriter.fabric.shard import pubkey_thumbprint
    from zeitghost.shards import (
        init_store, init_trace_emitter, article_to_internal_shard,
        load_articles_from_shards,
    )
    seed = os.urandom(32)
    store = init_store(tmp_path / "shards")
    emitter, _ = init_trace_emitter(store, run_id="ingest-test-rt")
    article_to_internal_shard(_article("https://e.com/full"), store,
                              signing_seed=seed, emitter=emitter)

    [art] = load_articles_from_shards(store)
    assert art.signed_by == pubkey_thumbprint(_pubkey(seed))
    assert art.trace_ref.startswith("chain:ingest-test-rt#")
