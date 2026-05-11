"""Shard integration — article metadata, lineage, dedup.

Two scopes per article (mirrors perseus-news convention):
- `zeitghost:article` — internal, full data including both variants.
- `sw:article` — consumer-agnostic, atom keys match frio/perseus convention so
  downstream consumers (frio facility pages, spiritwriter.ai) can read uniformly.
  Entity key uses full SHA-256 of URL for IPFS compatibility.
"""

import hashlib
import json
import logging
import os
from pathlib import Path

from spiritwriter.fabric.shard import MemoryShard, ShardAtom, AtomKind, DecayClass
from spiritwriter.fabric.store import ShardStore

from zeitghost.bias import AnalyzedArticle
from zeitghost.fetcher import Article

log = logging.getLogger(__name__)

SCOPE_INTERNAL = "zeitghost:article"
SCOPE_SW_ARTICLE = "sw:article"


def init_store(store_path: Path | None = None) -> ShardStore:
    """Initialize the shard store."""
    path = store_path or Path(os.environ.get(
        "ZEITGHOST_SHARD_STORE",
        str(Path.home() / ".zeitghost" / "shards"),
    ))
    path.mkdir(parents=True, exist_ok=True)
    return ShardStore(path)


def _url_entity(url: str) -> str:
    return f"article:{hashlib.sha256(url.encode()).hexdigest()}"


def known_url_entities(store: ShardStore) -> set[str]:
    """Return entity keys (article:{hash}) already in the internal scope."""
    seen: set[str] = set()
    for shard in store.by_scope(SCOPE_INTERNAL):
        ent = shard.meta.get("entity_key", "")
        if ent:
            seen.add(ent)
            continue
        for atom in shard.atoms:
            if atom.key == "source_url" and atom.entity:
                seen.add(atom.entity)
                break
    return seen


def is_known(article_url: str, known: set[str]) -> bool:
    return _url_entity(article_url) in known


def build_lineage_index(store: ShardStore, scope: str) -> dict[str, str]:
    """Pre-compute {entity_key → most-recent shard_id} for `scope`.

    Pass the result to `article_to_*_shard(lineage_index=...)` so re-writes
    set `parent_shard_id` on the new MemoryShard, forming an immutable
    revision chain instead of silently superseding history. Mirrors
    perseus-news's `build_sw_lineage_index()`.

    Computed once per batch to avoid an O(N²) scan of the store on each
    write. For first-time writes (cold store), pass an empty dict.
    """
    latest: dict[str, tuple[str, str]] = {}  # entity → (shard_id, created_at)
    for shard in store.by_scope(scope):
        ent = shard.meta.get("entity_key", "")
        if not ent:
            for atom in shard.atoms:
                if atom.key == "source_url" and atom.entity:
                    ent = atom.entity
                    break
        if not ent:
            continue
        existing = latest.get(ent)
        if existing is None or (shard.created_at or "") > (existing[1] or ""):
            latest[ent] = (shard.shard_id, shard.created_at or "")
    return {ent: sid for ent, (sid, _) in latest.items()}


def tags_from_shard(shard: MemoryShard) -> list[str]:
    """Re-compute tags from a raw shard's atoms (no AnalyzedArticle round-trip).

    Used by the in-place augmentation migration which has to read existing
    shards rather than freshly-analyzed Article objects. Produces the same
    output `_article_tags(article)` would have for the same data.
    """
    from zeitghost.analytics import source_slug

    vals = {a.key: a.value for a in shard.atoms if a.key}
    tags: list[str] = []
    src = vals.get("source_name", "")
    if src:
        tags.append(f"source:{source_slug(src)}")
    cats_raw = vals.get("categories", "")
    if cats_raw:
        try:
            cats = (json.loads(cats_raw) if cats_raw.startswith("[")
                    else [c for c in cats_raw.split(",") if c])
        except json.JSONDecodeError:
            cats = []
        for cat in cats[:5]:
            if cat:
                tags.append(f"category:{cat}")
    pub = vals.get("published", "")
    if len(pub) >= 7:
        tags.append(f"month:{pub[:7]}")
    return tags


def _article_tags(article: AnalyzedArticle) -> list[str]:
    """Cross-cutting tags applied to every shard so the store can answer
    `by_scope` + filter queries without scanning every shard's atoms.

    Conventions:
      source:<slug>   — per-source rollup (matches /source/<slug>.html URLs)
      category:<cat>  — per-category filter
      month:YYYY-MM   — time-window filter for the time-travel features
    """
    # Local import to avoid the analytics → bias → shards cycle on cold paths
    from zeitghost.analytics import source_slug

    tags: list[str] = []
    if article.original.source_name:
        tags.append(f"source:{source_slug(article.original.source_name)}")
    for cat in article.original.categories[:5]:
        if cat:
            tags.append(f"category:{cat}")
    pub = article.original.published or ""
    if len(pub) >= 7:
        tags.append(f"month:{pub[:7]}")
    return tags


def article_to_internal_shard(article: AnalyzedArticle, store: ShardStore,
                              lineage_index: dict[str, str] | None = None
                              ) -> str:
    """Write the zeitghost-internal shard with full L/R variant data.

    Pass `lineage_index` (from `build_lineage_index(store, SCOPE_INTERNAL)`)
    to chain re-writes via `parent_shard_id`. Without it, every write is
    treated as a first revision.
    """
    entity = _url_entity(article.original.url)
    atoms = [
        ShardAtom(text=f"Source: {article.original.url}",
                  kind=AtomKind.FACT, entity=entity,
                  key="source_url", value=article.original.url),
        ShardAtom(text=f"Title: {article.original.title[:80]}",
                  kind=AtomKind.FACT, entity=entity,
                  key="source_title", value=article.original.title),
        ShardAtom(text=f"Summary: {article.original.summary[:120]}",
                  kind=AtomKind.FACT, entity=entity,
                  key="source_summary", value=article.original.summary),
        ShardAtom(text=f"Source: {article.original.source_name}",
                  kind=AtomKind.FACT, entity=entity,
                  key="source_name", value=article.original.source_name),
        ShardAtom(text=f"Published: {article.original.published}",
                  kind=AtomKind.FACT, entity=entity,
                  key="published", value=article.original.published),
        ShardAtom(text=f"Categories: {','.join(article.original.categories)}",
                  kind=AtomKind.FACT, entity=entity,
                  key="categories", value=json.dumps(article.original.categories)),
        ShardAtom(text=f"Bias: {article.bias_score:.2f} ({article.bias_label})",
                  kind=AtomKind.DECISION, entity=entity,
                  key="bias_score", value=str(article.bias_score)),
        ShardAtom(text=f"Bias label: {article.bias_label}",
                  kind=AtomKind.DECISION, entity=entity,
                  key="bias_label", value=article.bias_label),
        ShardAtom(text=f"Left title: {article.variant_left_title[:80]}",
                  kind=AtomKind.DECISION, entity=entity,
                  key="variant_left_title", value=article.variant_left_title),
        ShardAtom(text=f"Left summary: {article.variant_left_summary[:80]}",
                  kind=AtomKind.DECISION, entity=entity,
                  key="variant_left_summary", value=article.variant_left_summary),
        ShardAtom(text=f"Right title: {article.variant_right_title[:80]}",
                  kind=AtomKind.DECISION, entity=entity,
                  key="variant_right_title", value=article.variant_right_title),
        ShardAtom(text=f"Right summary: {article.variant_right_summary[:80]}",
                  kind=AtomKind.DECISION, entity=entity,
                  key="variant_right_summary", value=article.variant_right_summary),
    ]
    if article.analysis_notes:
        atoms.append(ShardAtom(
            text=f"Analysis: {article.analysis_notes[:120]}",
            kind=AtomKind.CONTEXT, entity=entity,
            key="analysis_notes", value=article.analysis_notes,
        ))
    # Preserve HtmxNewsEngine row id for old share-link URL parity.
    if article.original.legacy_id is not None:
        atoms.append(ShardAtom(
            text=f"Legacy id: {article.original.legacy_id}",
            kind=AtomKind.FACT, entity=entity,
            key="legacy_id", value=str(article.original.legacy_id),
        ))

    parent = (lineage_index or {}).get(entity)
    shard = MemoryShard(
        atoms=atoms,
        scope=SCOPE_INTERNAL,
        origin="zeitghost",
        decay_class=DecayClass.STABLE,
        parent_shard_id=parent,
        tags=_article_tags(article),
        meta={"entity_key": entity},
    )
    store.put(shard)
    log.debug("Stored zeitghost shard %s for '%s'%s",
             shard.shard_id[:12], article.original.title[:40],
             f" (revision of {parent[:12]})" if parent else "")
    return shard.shard_id


def article_to_sw_shard(article: AnalyzedArticle, store: ShardStore,
                        lineage_index: dict[str, str] | None = None) -> str:
    """Write the consumer-agnostic sw:article shard.

    Atom keys (`title`, `summary`, etc.) match frio's `shard_from_article()`.
    Downstream consumers read these uniformly across perseus/zeitghost/frio.
    """
    url = article.original.url
    entity = _url_entity(url)
    atoms = [
        ShardAtom(text=f"Source: {url}",
                  kind=AtomKind.FACT, entity=entity,
                  key="source_url", value=url),
        ShardAtom(text=f"Title: {article.original.title[:80]}",
                  kind=AtomKind.FACT, entity=entity,
                  key="title", value=article.original.title),
        ShardAtom(text=f"Summary: {article.original.summary[:80]}",
                  kind=AtomKind.FACT, entity=entity,
                  key="summary", value=article.original.summary),
        ShardAtom(text=f"Source: {article.original.source_name}",
                  kind=AtomKind.FACT, entity=entity,
                  key="source_name", value=article.original.source_name),
        ShardAtom(text=f"Published: {article.original.published}",
                  kind=AtomKind.FACT, entity=entity,
                  key="published", value=article.original.published),
        ShardAtom(text=f"Bias score: {article.bias_score:.2f}",
                  kind=AtomKind.DECISION, entity=entity,
                  key="bias_score", value=str(article.bias_score)),
        ShardAtom(text=f"Bias label: {article.bias_label}",
                  kind=AtomKind.DECISION, entity=entity,
                  key="bias_label", value=article.bias_label),
    ]
    if article.original.categories:
        atoms.append(ShardAtom(
            text=f"Categories: {','.join(article.original.categories)}",
            kind=AtomKind.FACT, entity=entity,
            key="categories", value=json.dumps(article.original.categories)))
    if article.original.legacy_id is not None:
        atoms.append(ShardAtom(
            text=f"Legacy id: {article.original.legacy_id}",
            kind=AtomKind.FACT, entity=entity,
            key="legacy_id", value=str(article.original.legacy_id)))

    parent = (lineage_index or {}).get(entity)
    shard = MemoryShard(
        atoms=atoms,
        scope=SCOPE_SW_ARTICLE,
        origin="zeitghost",
        decay_class=DecayClass.STABLE,
        parent_shard_id=parent,
        tags=_article_tags(article),
        meta={"entity_key": entity},
    )
    store.put(shard)
    return shard.shard_id


def _shard_to_article(shard: MemoryShard) -> AnalyzedArticle | None:
    """Reconstruct an AnalyzedArticle from a zeitghost:article shard."""
    vals = {a.key: a.value for a in shard.atoms if a.key}
    url = vals.get("source_url")
    if not url:
        return None
    cats_raw = vals.get("categories", "")
    try:
        categories = json.loads(cats_raw) if cats_raw.startswith("[") else [
            c for c in cats_raw.split(",") if c
        ]
    except json.JSONDecodeError:
        categories = []
    legacy_raw = vals.get("legacy_id")
    try:
        legacy_id = int(legacy_raw) if legacy_raw else None
    except (ValueError, TypeError):
        legacy_id = None

    original = Article(
        title=vals.get("source_title", ""),
        url=url,
        summary=vals.get("source_summary", ""),
        source_name=vals.get("source_name", ""),
        published=vals.get("published", ""),
        region="national",
        categories=categories,
        legacy_id=legacy_id,
    )
    try:
        return AnalyzedArticle(
            original=original,
            bias_score=float(vals.get("bias_score", 0.5)),
            bias_label=vals.get("bias_label", "center"),
            variant_left_title=vals.get("variant_left_title", original.title),
            variant_left_summary=vals.get("variant_left_summary", ""),
            variant_right_title=vals.get("variant_right_title", original.title),
            variant_right_summary=vals.get("variant_right_summary", ""),
            analysis_notes=vals.get("analysis_notes", ""),
        )
    except (ValueError, TypeError) as e:
        log.warning("Failed to reconstruct article from shard %s: %s",
                    shard.shard_id[:12], e)
        return None


def load_articles_from_shards(store: ShardStore) -> list[AnalyzedArticle]:
    """Reconstruct AnalyzedArticle objects from all zeitghost:article shards."""
    out = []
    for shard in store.by_scope(SCOPE_INTERNAL):
        a = _shard_to_article(shard)
        if a:
            out.append(a)
    return out
