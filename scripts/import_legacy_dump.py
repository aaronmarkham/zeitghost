"""One-shot importer: HtmxNewsEngine pg_dump SQL backup → zeitghost shards.

Reads a plain-text pg_dump file, finds COPY blocks for the article tables, and
maps each row into an AnalyzedArticle then writes both internal and sw:article
shards.

Why parse the dump file directly instead of restoring to Postgres first?
Two reasons:
1. Avoids requiring a live Postgres for the migration.
2. The dump is the authoritative pinned-in-time source; we want to translate
   it into shards once and never look back.

The HtmxNewsEngine NewsArticle table has these relevant columns:
    id, title, abstract, url, source, city, category,
    political_bias_score, is_variant, variant_type, original_article_id,
    published_at, created_at, ...

Variants are linked to originals via `original_article_id` and tagged with
`variant_type` ("left" or "right"). We pair each original with its two
variants on import.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from zeitghost.bias import AnalyzedArticle
from zeitghost.fetcher import Article
from zeitghost.shards import (
    init_store, known_url_entities, is_known,
    article_to_internal_shard, article_to_sw_shard,
)

log = logging.getLogger(__name__)

# pg_dump COPY ... FROM stdin format: tab-separated, \N for NULL, \\ for backslash
NULL = r"\N"


def _parse_copy_value(raw: str) -> str | None:
    if raw == NULL:
        return None
    # pg_dump escapes: \\, \t, \n, \r — decode in correct order
    return (raw
            .replace(r"\t", "\t")
            .replace(r"\n", "\n")
            .replace(r"\r", "\r")
            .replace(r"\\", "\\"))


def _iter_copy_block(lines: Iterator[str], columns: list[str]) -> Iterator[dict]:
    """Yield dict-per-row from a COPY ... FROM stdin block in a pg_dump."""
    for line in lines:
        if line.rstrip("\n") == r"\.":
            return
        fields = line.rstrip("\n").split("\t")
        if len(fields) != len(columns):
            log.debug("Skipping malformed row (%d fields, expected %d)",
                      len(fields), len(columns))
            continue
        yield {col: _parse_copy_value(v) for col, v in zip(columns, fields)}


COPY_HEADER_RE = re.compile(
    r'^COPY\s+(?:public\.)?(?P<table>\w+)\s*\((?P<cols>[^)]+)\)\s+FROM\s+stdin;'
)


def _find_table_blocks(dump_path: Path,
                       table_name: str) -> Iterator[dict]:
    """Stream rows from every COPY block matching `table_name`."""
    with dump_path.open(encoding="utf-8", errors="replace") as f:
        line_iter = iter(f)
        for line in line_iter:
            m = COPY_HEADER_RE.match(line)
            if not m or m.group("table") != table_name:
                continue
            columns = [c.strip().strip('"') for c in m.group("cols").split(",")]
            yield from _iter_copy_block(line_iter, columns)


def _to_iso(raw: str | None) -> str:
    """Normalize pg timestamp to ISO-8601, defaulting to now if missing."""
    if not raw:
        return datetime.now(timezone.utc).isoformat()
    raw = raw.strip()
    # pg_dump emits "2025-01-12 17:43:21.123456" or with timezone — try a few
    for fmt in ("%Y-%m-%d %H:%M:%S.%f%z",
                "%Y-%m-%d %H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    return raw  # leave as-is rather than fail


def _row_to_components(row: dict) -> tuple[Article, float, str | None] | None:
    """Map a NewsArticle row to (Article, bias_score, variant_type).

    Returns None for rows that were saved before async bias analysis finished
    (NULL political_bias_score). Defaulting these to 0.5 would silently
    mislabel unanalyzed articles as "center" — see HtmxNewsEngine prod bug
    where this exact pattern caused page-load 500s.
    """
    url = row.get("url")
    title = row.get("title")
    if not url or not title:
        return None

    bias_raw = row.get("political_bias_score")
    if bias_raw is None or bias_raw == "":
        return None  # row was in-flight: saved but not yet analyzed
    try:
        bias = float(bias_raw)
    except ValueError:
        return None

    article = Article(
        title=title,
        url=url,
        summary=row.get("abstract") or "",
        source_name=row.get("source") or "Unknown",
        published=_to_iso(row.get("published_at") or row.get("created_at")),
        region="national",
        categories=[row.get("category")] if row.get("category") else [],
    )
    return article, bias, (row.get("variant_type") or None)


def _label_for(score: float) -> str:
    if score < 0.2: return "left"
    if score < 0.4: return "center-left"
    if score < 0.6: return "center"
    if score < 0.8: return "center-right"
    return "right"


def import_dump(dump_path: Path, *, limit: int = 0,
                dry_run: bool = False, console=None) -> int:
    """Read NewsArticle rows from the dump and write shards. Returns count."""
    log.info("Reading %s", dump_path)

    # Pass 1: collect all rows (we need to pair originals with variants)
    rows = list(_find_table_blocks(dump_path, "news_article"))
    if not rows:
        # Fallback to pluralized name
        rows = list(_find_table_blocks(dump_path, "news_articles"))
    log.info("Found %d news_article rows", len(rows))

    # Index rows by id for variant lookup
    by_id = {}
    for r in rows:
        rid = r.get("id")
        if rid:
            by_id[rid] = r

    # Group: id of original → {"left": row, "right": row}
    variants_for: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        is_variant = (r.get("is_variant") or "").lower() in ("t", "true", "1")
        vtype = (r.get("variant_type") or "").lower()
        orig_id = r.get("original_article_id")
        if is_variant and vtype in ("left", "right") and orig_id:
            variants_for[orig_id][vtype] = r

    # Build AnalyzedArticles from originals only
    store = init_store() if not dry_run else None
    known = known_url_entities(store) if store else set()
    written = 0
    skipped_partial = 0  # rows with NULL bias — saved before analysis ran
    for r in rows:
        is_variant = (r.get("is_variant") or "").lower() in ("t", "true", "1")
        if is_variant:
            continue
        comp = _row_to_components(r)
        if not comp:
            # Track rows skipped for missing/null bias specifically
            if r.get("url") and r.get("title") and not r.get("political_bias_score"):
                skipped_partial += 1
            continue
        original, bias, _ = comp
        if store and is_known(original.url, known):
            continue

        vmap = variants_for.get(r.get("id") or "", {})
        left_row = vmap.get("left", {})
        right_row = vmap.get("right", {})

        analyzed = AnalyzedArticle(
            original=original,
            bias_score=bias,
            bias_label=_label_for(bias),
            variant_left_title=left_row.get("title") or original.title,
            variant_left_summary=left_row.get("abstract") or "",
            variant_right_title=right_row.get("title") or original.title,
            variant_right_summary=right_row.get("abstract") or "",
            analysis_notes="Imported from HtmxNewsEngine PostgreSQL dump.",
        )

        if not dry_run:
            article_to_internal_shard(analyzed, store)
            article_to_sw_shard(analyzed, store)

        written += 1
        if console and written % 500 == 0:
            console.print(f"  imported {written} so far...")
        if limit and written >= limit:
            break

    if skipped_partial:
        msg = (f"Skipped {skipped_partial} rows with NULL bias_score "
               f"(saved before async analysis completed)")
        log.info(msg)
        if console:
            console.print(f"  [yellow]{msg}[/yellow]")

    return written


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python -m scripts.import_legacy_dump <dump.sql> [--limit N] [--dry-run]")
        sys.exit(2)
    args = sys.argv[1:]
    limit = 0
    dry = "--dry-run" in args
    if "--limit" in args:
        i = args.index("--limit")
        limit = int(args[i + 1])
    n = import_dump(Path(args[0]), limit=limit, dry_run=dry)
    print(f"{'Would import' if dry else 'Imported'} {n} articles")
