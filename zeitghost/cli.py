"""Zeitghost CLI — ingest, build, analytics, import-legacy."""

import asyncio
import logging
import os
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler

console = Console()

PROJECT_ROOT = Path(__file__).parent.parent


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def main(verbose: bool):
    """Zeitghost — national bias-aware news with dual L/R variants."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        handlers=[RichHandler(console=console, show_path=False)],
        format="%(message)s",
    )


@main.command()
@click.option("--feeds", "-f", type=click.Path(exists=True),
              default=str(PROJECT_ROOT / "feeds" / "newsapi.yaml"),
              help="NewsAPI source config")
@click.option("--limit", "-n", type=int, default=0,
              help="Cap total articles to fetch (0=use config)")
@click.option("--max-requests", type=int, default=None,
              help="Cap NewsAPI requests this run (default: remaining quota)")
@click.option("--dry-run", is_flag=True,
              help="Fetch + analyze but skip writing shards")
def ingest(feeds: str, limit: int, max_requests: int | None, dry_run: bool):
    """Fetch new articles from NewsAPI, analyze with Claude, write shards."""
    from zeitghost.fetcher import fetch_all, enrich_with_bodies
    from zeitghost.bias import analyze_batch
    from zeitghost.shards import (init_store, known_url_entities, is_known,
                                  article_to_internal_shard, article_to_sw_shard,
                                  build_lineage_index,
                                  SCOPE_INTERNAL, SCOPE_SW_ARTICLE)

    store = init_store()
    state_dir = Path(store.path).parent if hasattr(store, "path") else Path.home() / ".zeitghost"

    console.print(f"[bold]Fetching from {feeds}[/bold]")
    articles = fetch_all(Path(feeds), state_dir, limit=limit, max_requests=max_requests)
    console.print(f"  {len(articles)} articles fetched")

    if not articles:
        return

    known = known_url_entities(store)
    new = [a for a in articles if not is_known(a.url, known)]
    skipped = len(articles) - len(new)
    if skipped:
        console.print(f"  {skipped} already in shard store (skipped)")
    if not new:
        console.print("  Nothing new to analyze")
        return

    # Pull the actual article body via trafilatura so Claude has real source
    # text to work from instead of NewsAPI's 1-sentence description. Body is
    # used for analysis only — not stored in shards, not displayed in the UI.
    console.print(f"[bold]Fetching full bodies for {len(new)} articles...[/bold]")
    enrich_with_bodies(new)

    console.print(f"[bold]Analyzing {len(new)} new articles with Claude...[/bold]")
    analyzed = asyncio.run(analyze_batch(new))
    console.print(f"  {len(analyzed)} analyzed successfully")

    if dry_run:
        console.print("[yellow]Dry run — skipping shard writes[/yellow]")
        for a in analyzed[:5]:
            console.print(f"  [{a.bias_label}] {a.original.title[:70]}")
        return

    # Build lineage indexes once so re-analyses (rare with our skip-if-known
    # default, but possible if dedup is bypassed later) chain via parent_shard_id.
    internal_lineage = build_lineage_index(store, SCOPE_INTERNAL)
    sw_lineage = build_lineage_index(store, SCOPE_SW_ARTICLE)
    for a in analyzed:
        article_to_internal_shard(a, store, lineage_index=internal_lineage)
        article_to_sw_shard(a, store, lineage_index=sw_lineage)
    console.print(f"  {len(analyzed) * 2} shards written "
                  f"(internal + sw:article)")


@main.command()
@click.option("--output", "-o", type=click.Path(),
              default=str(PROJECT_ROOT / "output"),
              help="Output directory for the rendered site")
@click.option("--site-name", default="Zeitghost")
@click.option("--site-tagline", default="Bias-aware news, your slider's choice")
@click.option("--max-articles", type=int, default=500,
              help="Limit articles rendered on index page (client-side filters "
                   "narrow further by date range + bias slider)")
def build(output: str, site_name: str, site_tagline: str, max_articles: int):
    """Render the static site from existing shards (no API calls).

    Always renders — even with zero articles — so nginx never serves stale
    content (e.g. the nginx:alpine welcome page that ships in the named
    volume on first launch).
    """
    from zeitghost.shards import init_store, load_articles_from_shards
    from zeitghost.generator import generate_site

    store = init_store()
    articles = load_articles_from_shards(store)
    console.print(f"[bold]Loaded {len(articles)} articles from shard store[/bold]")
    if not articles:
        console.print("[yellow]Shard store empty — rendering placeholder page[/yellow]")

    out = generate_site(
        articles=articles,
        template_dir=PROJECT_ROOT / "templates",
        output_dir=Path(output),
        static_dir=PROJECT_ROOT / "static",
        site_name=site_name,
        site_tagline=site_tagline,
        max_articles=max_articles,
    )
    console.print(f"[green]Site generated at {out}[/green]")


@main.command()
@click.option("--output", "-o", type=click.Path(),
              default=str(PROJECT_ROOT / "output"),
              help="Output directory")
def analytics(output: str):
    """Regenerate the per-source bias analytics page."""
    from zeitghost.shards import init_store, load_articles_from_shards
    from zeitghost.generator import generate_analytics_only

    store = init_store()
    articles = load_articles_from_shards(store)
    console.print(f"[bold]Computing source stats over {len(articles)} articles[/bold]")
    if not articles:
        console.print("[yellow]No articles in shard store[/yellow]")
        return

    path = generate_analytics_only(
        articles=articles,
        template_dir=PROJECT_ROOT / "templates",
        output_dir=Path(output),
    )
    console.print(f"[green]Analytics page generated at {path}[/green]")


@main.command(name="import-legacy")
@click.option("--db-url", required=True,
              help="postgresql://user:pass@host:port/dbname (the temp pg "
                   "container that has the restored HtmxNewsEngine dump)")
@click.option("--limit", "-n", type=int, default=0,
              help="Cap articles imported (0=all)")
@click.option("--dry-run", is_flag=True,
              help="Report what would be imported without writing shards")
def import_legacy(db_url: str, limit: int, dry_run: bool):
    """One-shot: read HtmxNewsEngine articles from a temp Postgres → shards.

    Workflow (run on us-ny1 from the host shell):

    \b
      docker run -d --name tmp-pg --network=docker_default \\
          -e POSTGRES_PASSWORD=tmp -e POSTGRES_DB=legacy postgres:16
      sleep 5
      docker cp /path/to/dump.sql tmp-pg:/tmp/dump.sql
      docker exec -e PGPASSWORD=tmp tmp-pg \\
          psql -U postgres -d legacy -f /tmp/dump.sql
      docker exec zeitghost-builder zeitghost import-legacy \\
          --db-url postgresql://postgres:tmp@tmp-pg:5432/legacy --dry-run
      # if numbers look right, drop --dry-run
      docker stop tmp-pg && docker rm tmp-pg

    Pre-existing OpenAI bias scores and L/R variants from HtmxNewsEngine are
    preserved (no Claude re-analysis). Articles already in the shard store
    are skipped, as are rows with NULL bias_score (saved before HtmxNewsEngine's
    async analysis completed).
    """
    from scripts.import_legacy_dump import import_from_db
    stats = import_from_db(db_url, limit=limit, dry_run=dry_run, console=console)
    verb = "Would import" if dry_run else "Imported"
    console.print(
        f"[green]{verb} {stats['written']} articles[/green] "
        f"({stats['skipped_partial']} null-bias, "
        f"{stats['skipped_known']} already known, "
        f"{stats['total_rows']} total)"
    )


@main.command(name="migrate-tags")
@click.option("--dry-run", is_flag=True,
              help="Report what would be augmented without writing shards")
def migrate_tags(dry_run: bool):
    """One-time migration: backfill tags onto shards written before lineage+tags landed.

    \b
    For each existing shard without tags, writes a NEW child shard with
    the same atoms + computed tags + parent_shard_id back to the
    original. Old shards stay (immutable); new shards become the
    "latest revision" for each entity. Idempotent — already-tagged
    shards are skipped.

    Workflow on us-ny1:

    \b
        docker exec zeitghost-builder zeitghost migrate-tags --dry-run
        # if counts look right:
        docker exec zeitghost-builder zeitghost migrate-tags
        docker exec zeitghost-builder python -m zeitghost.cli build
    """
    from scripts.augment_shard_tags import run
    results = run(dry_run=dry_run, console=console)
    total = sum(r["augmented"] for r in results)
    skipped = sum(r["skipped_already_tagged"] for r in results)
    verb = "Would augment" if dry_run else "Augmented"
    console.print(
        f"\n[bold green]{verb} {total} shards[/bold green] across "
        f"{len(results)} scopes ({skipped} already tagged, skipped)"
    )


@main.command()
@click.option("--title", "-t", default="Senate passes bipartisan immigration bill")
@click.option("--summary", "-s",
              default="The Senate passed a bipartisan bill addressing border security and pathways to citizenship for long-term residents.")
@click.option("--source", default="Test Source")
def test_analysis(title: str, summary: str, source: str):
    """Smoke-test bias analysis on a single article (costs Claude API credits)."""
    from zeitghost.fetcher import Article
    from zeitghost.bias import analyze_article

    article = Article(
        title=title, url="https://example.com/test", summary=summary,
        source_name=source, published="2026-05-07T12:00:00+00:00",
        region="national", categories=["politics"],
    )
    console.print(f"[bold]Analyzing:[/bold] {title}")
    result = asyncio.run(analyze_article(article))
    if not result:
        console.print("[red]Analysis failed[/red]")
        return
    console.print(f"\n[green]Bias: {result.bias_score:.2f} ({result.bias_label})[/green]\n")
    console.print(f"[bold]Left variant:[/bold]")
    console.print(f"  {result.variant_left_title}")
    console.print(f"  {result.variant_left_summary}\n")
    console.print(f"[bold]Right variant:[/bold]")
    console.print(f"  {result.variant_right_title}")
    console.print(f"  {result.variant_right_summary}\n")
    if result.analysis_notes:
        console.print(f"[dim]Notes: {result.analysis_notes}[/dim]")


if __name__ == "__main__":
    main()
