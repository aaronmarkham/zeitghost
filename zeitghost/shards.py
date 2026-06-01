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
from typing import TYPE_CHECKING

from spiritwriter.fabric.shard import MemoryShard, ShardAtom, AtomKind, DecayClass
from spiritwriter.fabric.store import ShardStore

if TYPE_CHECKING:  # keep the emitter import lazy at runtime (invariant #2)
    from spiritwriter.fabric.emitter import TraceEmitter

from zeitghost import __version__ as _zg_version
from zeitghost.bias import AnalyzedArticle, DEFAULT_MODEL
from zeitghost.fetcher import Article

log = logging.getLogger(__name__)

SCOPE_INTERNAL = "zeitghost:article"
SCOPE_SW_ARTICLE = "sw:article"

# Secret name (OS keychain key / env var) holding the 64-char-hex Ed25519 seed
# used to sign shards. Resolved via spiritwriter.secrets, which checks the
# keychain first then falls back to the environment — so the headless us-ny1
# builder can be handed the seed through a ZEITGHOST_SIGNING_KEY env var.
SIGNING_KEY_NAME = "ZEITGHOST_SIGNING_KEY"


def resolve_signing_seed() -> bytes | None:
    """Return the 32-byte Ed25519 signing seed, or None if none is configured.

    Signing is opt-in: an environment without `ZEITGHOST_SIGNING_KEY` set
    (local dev, CI, a freshly-provisioned container) simply writes unsigned
    shards rather than failing. Pass the result to `article_to_*_shard(...,
    signing_seed=...)`. Generate a key with `zeitghost gen-signing-key`.

    Returns None — and logs a warning — when the configured value isn't a
    valid 32-byte hex seed, so a fat-fingered key degrades to unsigned rather
    than crashing ingest.
    """
    # Lazy import keeps secrets/keyring off the module-import path
    # (robustness invariant #2: no I/O at import).
    from spiritwriter.secrets import configure, get_api_key

    configure(service_name="zeitghost")
    raw = get_api_key(SIGNING_KEY_NAME)
    if not raw:
        return None
    try:
        seed = bytes.fromhex(raw.strip())
    except ValueError:
        log.warning("%s is not valid hex — writing unsigned shards",
                    SIGNING_KEY_NAME)
        return None
    if len(seed) != 32:
        log.warning("%s must decode to 32 bytes (got %d) — writing unsigned shards",
                    SIGNING_KEY_NAME, len(seed))
        return None
    return seed


def signing_required(flag: bool = False) -> bool:
    """Whether ingest must fail-closed when no signing key is configured.

    True if the `--require-signing` flag is passed OR `ZEITGHOST_REQUIRE_SIGNING`
    is truthy. Prod (us-ny1) sets the env var once its `ZEITGHOST_SIGNING_KEY`
    is provisioned, so an accidentally-cleared key fails the run loudly instead
    of silently writing unsigned shards. Local dev and CI leave both unset, so
    signing stays opt-in there.
    """
    if flag:
        return True
    val = os.environ.get("ZEITGHOST_REQUIRE_SIGNING", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def init_trace_emitter(store: ShardStore, run_id: str | None = None
                       ) -> "tuple[TraceEmitter, Path]":
    """Create a per-ingest TraceEmitter writing a hash-chained JSONL under the
    store's `traces/` dir. Returns (emitter, jsonl_path).

    `run_id` defaults to a timestamped id (also embedded in each shard's
    `trace_ref` as `chain:<run_id>#<event_hash>`, so a shard points back to the
    exact run + event that produced it). `agent_id` mirrors `_agent_string()`
    so trace events and shard `agent` atoms name the same producer.

    Lazy import + runtime dir creation keep this off the module-import path
    (robustness invariant #2).
    """
    import uuid
    from datetime import datetime, timezone
    from spiritwriter.fabric.emitter import TraceEmitter

    if run_id is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"ingest-{stamp}-{uuid.uuid4().hex[:8]}"
    out_path = store.root / "traces" / f"{run_id}.jsonl"
    emitter = TraceEmitter(run_id=run_id, agent_id=_agent_string(),
                           out_path=str(out_path))
    return emitter, out_path


def _trace_shard(shard: MemoryShard, parent: str | None,
                 emitter: "TraceEmitter | None") -> None:
    """Emit this shard's lifecycle events and stamp its `trace_ref`.

    No-op when `emitter` is None (tracing is opt-in, like signing). Emits
    `shard_created`, points `shard.trace_ref` at that event, then emits
    `shard_superseded` when the write supersedes a parent revision. Must run
    BEFORE `_maybe_sign` so the signature covers the trace_ref; `trace_ref` is
    not part of the content-address, so `shard.shard_id` is unaffected.

    Best-effort / fail-open: a trace emit failure (e.g. an unwritable
    `traces/` dir) must never block the shard itself from being persisted —
    signing is the load-bearing provenance half, tracing the lighter one. On
    failure we log and leave `trace_ref` unset rather than stamping a ref to an
    event that didn't make it to disk.
    """
    if emitter is None:
        return
    try:
        extra = {"parent_shard_id": parent} if parent else {}
        emitter.shard_created(shard.shard_id, shard.scope, len(shard.atoms), **extra)
        shard.trace_ref = emitter.current_trace_ref()
        if parent:
            emitter.shard_superseded(parent, shard.shard_id)
    except Exception as e:
        log.warning("Trace emit failed for shard %s — persisting it untraced: %s",
                    shard.shard_id[:12], e)
        shard.trace_ref = None


def _maybe_sign(shard: MemoryShard, signing_seed: bytes | None) -> None:
    """Sign `shard` in place when a seed is supplied (sets `signature` and
    `created_by`). The signature covers {atoms, scope, origin, trace_ref, …}
    but NOT the content-address, so signing never changes `shard.shard_id` —
    safe to call before `store.put` and to return the id afterwards."""
    if signing_seed:
        shard.sign(signing_seed)


def _agent_string() -> str:
    """Identify the agent that wrote this shard: zeitghost version +
    the underlying spiritwriter wheel version. Used in shard atoms so
    the card's flip-panel can show what physically produced the analysis."""
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            sw = version("spiritwriter")
        except PackageNotFoundError:
            sw = "?"
    except ImportError:
        sw = "?"
    return f"zeitghost/{_zg_version} sw_core/{sw}"


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


def _entity_of(shard: MemoryShard) -> str:
    """Entity key for a shard: `meta['entity_key']` if present, else derived
    from the `source_url` atom. Returns "" when neither is available.

    Single source of truth for the "which article does this shard describe?"
    lookup shared by `known_url_entities`, `build_lineage_index`, and
    `load_articles_from_shards` — keep them in agreement so dedup, lineage
    chaining, and render-time collapse all key off the same identity.
    """
    ent = shard.meta.get("entity_key", "")
    if ent:
        return ent
    for atom in shard.atoms:
        if atom.key == "source_url" and atom.entity:
            return atom.entity
    return ""


def known_url_entities(store: ShardStore) -> set[str]:
    """Return entity keys (article:{hash}) already in the internal scope."""
    seen: set[str] = set()
    for shard in store.by_scope(SCOPE_INTERNAL):
        ent = _entity_of(shard)
        if ent:
            seen.add(ent)
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
        ent = _entity_of(shard)
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
                              lineage_index: dict[str, str] | None = None,
                              signing_seed: bytes | None = None,
                              emitter: "TraceEmitter | None" = None,
                              ) -> str:
    """Write the zeitghost-internal shard with full L/R variant data.

    Pass `lineage_index` (from `build_lineage_index(store, SCOPE_INTERNAL)`)
    to chain re-writes via `parent_shard_id`. Without it, every write is
    treated as a first revision.

    Provenance: the function embeds `model` + `agent` atoms so the card's
    flip panel can show what produced this analysis. `article.model` and
    `article.agent` are typically empty on fresh analysis — we then fall
    back to `DEFAULT_MODEL` / `_agent_string()`. On *re-analysis* of a
    loaded article, those fields are populated from the prior shard, so
    if a different model actually did the new analysis the caller must
    set `article.model` to the new value before calling this; otherwise
    the prior model's name is preserved on disk.
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
    # Provenance — which model + agent produced this analysis. Surfaced on
    # the card's flip-panel; lets us tell "was this re-analyzed by a newer
    # model?" by following the parent_shard_id chain.
    model_used = article.model or DEFAULT_MODEL
    agent_used = article.agent or _agent_string()
    atoms.append(ShardAtom(
        text=f"Model: {model_used}",
        kind=AtomKind.CONTEXT, entity=entity,
        key="model", value=model_used,
    ))
    atoms.append(ShardAtom(
        text=f"Agent: {agent_used}",
        kind=AtomKind.CONTEXT, entity=entity,
        key="agent", value=agent_used,
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
    _trace_shard(shard, parent, emitter)
    _maybe_sign(shard, signing_seed)
    store.put(shard)
    log.debug("Stored zeitghost shard %s for '%s'%s%s%s",
             shard.shard_id[:12], article.original.title[:40],
             f" (revision of {parent[:12]})" if parent else "",
             " [signed]" if shard.signature else "",
             " [traced]" if shard.trace_ref else "")
    return shard.shard_id


def article_to_sw_shard(article: AnalyzedArticle, store: ShardStore,
                        lineage_index: dict[str, str] | None = None,
                        signing_seed: bytes | None = None,
                        emitter: "TraceEmitter | None" = None) -> str:
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
    _trace_shard(shard, parent, emitter)
    _maybe_sign(shard, signing_seed)
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
    # Tally atom kinds for the card flip-panel's "ATOMS" section.
    # AtomKind is the imported enum — its .value is the canonical label.
    atom_kinds: dict[str, int] = {}
    for atom in shard.atoms:
        kind_name = atom.kind.value.lower()
        atom_kinds[kind_name] = atom_kinds.get(kind_name, 0) + 1

    # Pre-format the shard's created_at once, at load time, so the
    # template just renders. Guards against spiritwriter ever returning
    # a datetime instead of a string for `created_at`.
    raw_created = shard.created_at or ""
    if not isinstance(raw_created, str):
        raw_created = str(raw_created)
    shard_created_at = (
        raw_created[:19].replace("T", " ") + " UTC"
        if len(raw_created) >= 19 else ""
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
            shard_id=shard.shard_id,
            parent_shard_id=shard.parent_shard_id,
            shard_created_at=shard_created_at,
            shard_scope=shard.scope,
            shard_decay=shard.decay_class.value,
            shard_tags=list(shard.tags or []),
            shard_atom_kinds=atom_kinds,
            model=vals.get("model", ""),
            agent=vals.get("agent", ""),
            signed_by=shard.created_by or "",
            trace_ref=shard.trace_ref or "",
        )
    except (ValueError, TypeError) as e:
        log.warning("Failed to reconstruct article from shard %s: %s",
                    shard.shard_id[:12], e)
        return None


def load_articles_from_shards(store: ShardStore) -> list[AnalyzedArticle]:
    """Reconstruct AnalyzedArticle objects — one per entity, newest revision.

    Re-analysing an article writes a new shard linked to the prior one via
    `parent_shard_id` (see `build_lineage_index`), so the store accumulates a
    revision chain per article. The renderer wants the *current* state, so we
    collapse each chain to its newest shard here — otherwise a re-analyzed
    article would surface as two cards. Latest-wins by `created_at`, matching
    the selection `build_lineage_index` uses when picking parents.

    Shards with no resolvable entity key (neither `meta['entity_key']` nor a
    `source_url` atom) can't be deduped, so they're passed through individually
    rather than dropped.
    """
    latest: dict[str, MemoryShard] = {}
    orphans: list[MemoryShard] = []
    for shard in store.by_scope(SCOPE_INTERNAL):
        ent = _entity_of(shard)
        if not ent:
            orphans.append(shard)
            continue
        cur = latest.get(ent)
        # Strict `>` means equal timestamps (sub-second collision, or both "")
        # keep the first shard `by_scope` yields — ties go to first-seen. This
        # matches build_lineage_index's comparison, so the "latest" rendered
        # here is the same shard its parent-chaining treats as the head.
        if cur is None or (shard.created_at or "") > (cur.created_at or ""):
            latest[ent] = shard

    out = []
    for shard in (*latest.values(), *orphans):
        a = _shard_to_article(shard)
        if a:
            out.append(a)
    return out


def select_for_reanalysis(articles: list[AnalyzedArticle], *,
                          source: str | None = None,
                          since: str | None = None,
                          limit: int = 0) -> list[AnalyzedArticle]:
    """Pick which loaded articles to re-analyze, newest-published first.

    Filters are ANDed: `source` matches by source-name slug (so "Fox News" and
    "fox-news" both hit), `since` keeps articles published on/after a YYYY-MM-DD
    date (lexicographic on the ISO `published` prefix), `limit` caps the result
    (0 = no cap). Sorting newest-first means `--limit` re-scores the most recent
    articles — the ones most likely to benefit from a model refresh.

    Pure function (no store/LLM access) so the `reanalyze` command's selection
    logic is unit-testable without Claude.
    """
    from zeitghost.analytics import source_slug

    selected = list(articles)
    if source:
        want = source_slug(source)
        selected = [a for a in selected
                    if source_slug(a.original.source_name) == want]
    if since:
        selected = [a for a in selected
                    if (a.original.published or "")[:10] >= since]
    selected.sort(key=lambda a: a.original.published or "", reverse=True)
    if limit and limit > 0:
        selected = selected[:limit]
    return selected
