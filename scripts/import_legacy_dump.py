"""One-shot importer: HtmxNewsEngine PostgreSQL → zeitghost shards.

The HtmxNewsEngine backup is INSERT-statement format (written by a custom
psycopg2 script, not pg_dump), so we restore it into a temporary Postgres
container and read from there rather than parsing SQL by hand.

Workflow on us-ny1 (or anywhere with Docker + the zeitghost image):

    # 1. Bring up a temp Postgres on the zeitghost network
    docker run -d --name tmp-pg --network=docker_default \\
        -e POSTGRES_PASSWORD=tmp -e POSTGRES_DB=legacy postgres:16
    sleep 5  # wait for postgres to accept connections

    # 2. Copy the dump in and restore it
    docker cp /path/to/backup-2026-05-07_153259.sql tmp-pg:/tmp/dump.sql
    docker exec -e PGPASSWORD=tmp tmp-pg \\
        psql -U postgres -d legacy -f /tmp/dump.sql

    # 3. Run importer from inside the zeitghost-builder container
    docker exec zeitghost-builder python -m scripts.import_legacy_dump \\
        --db-url postgresql://postgres:tmp@tmp-pg:5432/legacy --dry-run
    # (dry-run reports what would be imported; drop --dry-run to commit)

    # 4. Trigger an immediate site rebuild
    docker exec zeitghost-builder python -m zeitghost.cli build

    # 5. Tear down the temp Postgres
    docker stop tmp-pg && docker rm tmp-pg

Articles whose `political_bias_score` is NULL — saved before HtmxNewsEngine's
async OpenAI analysis completed — are SKIPPED rather than default-filled to
0.5, which would silently mislabel an unanalyzed article as "center."
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from zeitghost.bias import AnalyzedArticle
from zeitghost.fetcher import Article
from zeitghost.shards import (
    init_store, known_url_entities, is_known,
    article_to_internal_shard, article_to_sw_shard,
)

log = logging.getLogger(__name__)


def _to_iso(raw: Any) -> str:
    """Normalize a published-at value to ISO-8601 string. Handles datetime
    objects (psycopg2 default) and strings."""
    if raw is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=timezone.utc)
        return raw.isoformat()
    return str(raw)


def _label_for(score: float) -> str:
    if score < 0.2: return "left"
    if score < 0.4: return "center-left"
    if score < 0.6: return "center"
    if score < 0.8: return "center-right"
    return "right"


def _row_to_components(row: dict) -> tuple[Article, float, str | None] | None:
    """Map a news_article row to (Article, bias_score, variant_type).

    Returns None for rows that were saved before async bias analysis finished
    (NULL political_bias_score). Defaulting these to 0.5 would silently
    mislabel unanalyzed articles as "center" — see HtmxNewsEngine prod bug.
    """
    url = row.get("url")
    title = row.get("title")
    if not url or not title:
        return None

    bias_raw = row.get("political_bias_score")
    if bias_raw is None:
        return None  # saved before analysis ran — skip
    try:
        bias = float(bias_raw)
    except (ValueError, TypeError):
        return None

    # Preserve the legacy integer id so /article/<id> share URLs keep working.
    legacy_id = row.get("id")
    try:
        legacy_id = int(legacy_id) if legacy_id is not None else None
    except (ValueError, TypeError):
        legacy_id = None

    article = Article(
        title=title,
        url=url,
        summary=row.get("abstract") or "",
        source_name=row.get("source") or "Unknown",
        published=_to_iso(row.get("published_at") or row.get("created_at")),
        region="national",
        categories=[row.get("category")] if row.get("category") else [],
        legacy_id=legacy_id,
    )
    return article, bias, (row.get("variant_type") or None)


def _build_analyzed(original_row: dict, variants: dict[str, dict]
                    ) -> AnalyzedArticle | None:
    """Combine an original news_article row with its left/right variant rows
    into a single AnalyzedArticle ready for shard write."""
    comp = _row_to_components(original_row)
    if not comp:
        return None
    article, bias, _ = comp

    left_row = variants.get("left", {})
    right_row = variants.get("right", {})

    return AnalyzedArticle(
        original=article,
        bias_score=bias,
        bias_label=_label_for(bias),
        variant_left_title=left_row.get("title") or article.title,
        variant_left_summary=left_row.get("abstract") or "",
        variant_right_title=right_row.get("title") or article.title,
        variant_right_summary=right_row.get("abstract") or "",
        analysis_notes="Imported from HtmxNewsEngine PostgreSQL.",
    )


def import_from_db(db_url: str, *, limit: int = 0,
                   dry_run: bool = False, console=None) -> dict:
    """Read news_article rows from a live Postgres and write zeitghost shards.

    Returns a stats dict: {written, skipped_partial, skipped_known, total_rows}.
    """
    import psycopg2
    from psycopg2.extras import RealDictCursor

    log.info("Connecting to %s", db_url.split('@')[-1])  # don't log credentials
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM news_article ORDER BY id")
            rows = cur.fetchall()
    finally:
        conn.close()

    log.info("Loaded %d news_article rows", len(rows))
    if console:
        console.print(f"  Loaded [bold]{len(rows)}[/bold] news_article rows")

    # Group variants by their parent article id.
    # Variants have is_variant=True and variant_type ∈ {"left", "right"}.
    variants_for: dict[int, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        if not r.get("is_variant"):
            continue
        vtype = (r.get("variant_type") or "").lower()
        orig_id = r.get("original_article_id")
        if vtype in ("left", "right") and orig_id is not None:
            variants_for[orig_id][vtype] = r

    # Process originals
    store = init_store() if not dry_run else None
    known = known_url_entities(store) if store else set()
    written = 0
    skipped_partial = 0  # NULL bias — saved before analysis ran
    skipped_known = 0    # already in shard store
    for r in rows:
        if r.get("is_variant"):
            continue
        if r.get("political_bias_score") is None and r.get("url") and r.get("title"):
            skipped_partial += 1
            continue
        analyzed = _build_analyzed(r, variants_for.get(r["id"], {}))
        if analyzed is None:
            continue
        if store and is_known(analyzed.original.url, known):
            skipped_known += 1
            continue

        if not dry_run:
            article_to_internal_shard(analyzed, store)
            article_to_sw_shard(analyzed, store)

        written += 1
        if console and written % 500 == 0:
            console.print(f"  imported {written} so far...")
        if limit and written >= limit:
            break

    if console:
        if skipped_partial:
            console.print(f"  [yellow]Skipped {skipped_partial} rows with NULL "
                          f"bias_score (saved before async analysis completed)[/yellow]")
        if skipped_known:
            console.print(f"  [dim]Skipped {skipped_known} already in shard store[/dim]")

    return {
        "written": written,
        "skipped_partial": skipped_partial,
        "skipped_known": skipped_known,
        "total_rows": len(rows),
    }


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-url", required=True,
                   help="postgresql://user:pass@host:port/dbname")
    p.add_argument("--limit", "-n", type=int, default=0,
                   help="cap articles imported (0=all)")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be imported without writing shards")
    args = p.parse_args()
    stats = import_from_db(args.db_url, limit=args.limit, dry_run=args.dry_run)
    verb = "Would import" if args.dry_run else "Imported"
    print(f"{verb} {stats['written']} articles "
          f"({stats['skipped_partial']} skipped: null bias, "
          f"{stats['skipped_known']} skipped: already known, "
          f"of {stats['total_rows']} total rows)")
