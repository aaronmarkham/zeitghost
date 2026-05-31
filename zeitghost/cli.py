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
@click.option("--require-signing", is_flag=True,
              help="Fail if no valid signing key is configured, instead of "
                   "writing unsigned shards. Also enabled by "
                   "ZEITGHOST_REQUIRE_SIGNING=1 (prod sets this; local/CI leave "
                   "it off). See `zeitghost gen-signing-key`.")
def ingest(feeds: str, limit: int, max_requests: int | None, dry_run: bool,
           require_signing: bool):
    """Fetch new articles from NewsAPI, analyze with Claude, write shards."""
    from zeitghost.fetcher import fetch_all, enrich_with_bodies
    from zeitghost.bias import analyze_batch
    from zeitghost.shards import (init_store, known_url_entities, is_known,
                                  article_to_internal_shard, article_to_sw_shard,
                                  build_lineage_index, resolve_signing_seed,
                                  signing_required, SIGNING_KEY_NAME,
                                  init_trace_emitter,
                                  SCOPE_INTERNAL, SCOPE_SW_ARTICLE)

    store = init_store()

    # Resolve the signing key up front so a "signing required but unconfigured"
    # deploy fails fast — before spending any NewsAPI quota or Claude calls —
    # rather than after fetching and analyzing a whole batch.
    seed = resolve_signing_seed()
    if seed is None and signing_required(require_signing):
        raise click.ClickException(
            f"Signing is required but no valid {SIGNING_KEY_NAME} is configured. "
            f"Provision the key (see `zeitghost gen-signing-key`), or drop "
            f"--require-signing / unset ZEITGHOST_REQUIRE_SIGNING for unsigned runs."
        )
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
    # `seed` was resolved up front (for the fail-fast require check). Signing is
    # opt-in: when it's None the shards are written unsigned.
    emitter = trace_path = None
    if analyzed:
        console.print("  Signing shards (ZEITGHOST_SIGNING_KEY configured)"
                      if seed else "  [dim]No signing key — writing unsigned shards[/dim]")
        # One hash-chained trace log per ingest run; each shard's trace_ref
        # points back at its shard_created event in this file.
        emitter, trace_path = init_trace_emitter(store)
    for a in analyzed:
        article_to_internal_shard(a, store, lineage_index=internal_lineage,
                                  signing_seed=seed, emitter=emitter)
        article_to_sw_shard(a, store, lineage_index=sw_lineage,
                            signing_seed=seed, emitter=emitter)
    console.print(f"  {len(analyzed) * 2} shards written "
                  f"(internal + sw:article)")
    if emitter is not None:
        from spiritwriter.fabric.emitter import verify_chain
        events = emitter.get_events()
        console.print(f"  Trace: {len(events)} events recorded → "
                      f"traces/{trace_path.name}")
        # Sanity self-check only — verifying a chain we just wrote always
        # passes barring disk corruption. Real provenance auditing of an old
        # run is a future `verify-trace <run_id>` command, not this line.
        if not verify_chain(events):
            console.print("  [red]Warning: trace chain failed self-verification[/red]")


@main.command()
@click.option("--source", help="Only re-analyze articles from this source "
                                "(name or slug, e.g. 'Fox News' or 'fox-news')")
@click.option("--since", help="Only articles published on/after this date "
                              "(YYYY-MM-DD)")
@click.option("--limit", "-n", type=int, default=0,
              help="Cap how many articles to re-analyze (newest first). "
                   "Required when no --source/--since filter is given, to "
                   "avoid re-scoring the whole corpus.")
@click.option("--model", default=None,
              help="Claude model for re-analysis (default: bias.DEFAULT_MODEL). "
                   "Recorded on the new shard so lineage shows which model "
                   "produced each revision.")
@click.option("--dry-run", is_flag=True,
              help="Select + report only — no Claude calls, no writes.")
@click.option("--require-signing", is_flag=True,
              help="Fail if no valid signing key is configured (see ingest).")
def reanalyze(source: str | None, since: str | None, limit: int,
              model: str | None, dry_run: bool, require_signing: bool):
    """Re-score existing articles with Claude, writing them as new revisions.

    Unlike `ingest` (which dedups and skips known articles), `reanalyze`
    deliberately re-processes articles already in the store — re-running bias
    analysis and writing a NEW shard that chains onto the prior one via
    parent_shard_id. This is the workflow that exercises lineage: re-score a
    window with a newer model and keep the old revision in the chain.

    Bounded by design: pass --limit and/or --source/--since. Re-analysis costs
    one Claude call per article, so a bare `reanalyze` (whole corpus) is
    refused.
    """
    import asyncio
    from zeitghost.bias import analyze_batch, DEFAULT_MODEL
    from zeitghost.shards import (init_store, load_articles_from_shards,
                                  select_for_reanalysis, build_lineage_index,
                                  article_to_internal_shard, article_to_sw_shard,
                                  resolve_signing_seed, signing_required,
                                  SIGNING_KEY_NAME, init_trace_emitter,
                                  SCOPE_INTERNAL, SCOPE_SW_ARTICLE)

    if not source and not since and limit <= 0:
        raise click.ClickException(
            "Refusing to re-analyze the entire corpus (one Claude call each). "
            "Narrow it with --limit and/or --source/--since."
        )
    if since:
        from datetime import datetime
        try:
            datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            raise click.ClickException(
                f"--since must be YYYY-MM-DD (got {since!r}). Date comparison is "
                f"lexicographic on the ISO prefix, so a malformed date silently "
                f"matches everything or nothing."
            )

    model = model or DEFAULT_MODEL
    store = init_store()

    # Fail fast on a required-but-missing signing key, before any Claude spend.
    seed = resolve_signing_seed()
    if seed is None and signing_required(require_signing):
        raise click.ClickException(
            f"Signing is required but no valid {SIGNING_KEY_NAME} is configured. "
            f"Provision the key (see `zeitghost gen-signing-key`), or drop "
            f"--require-signing / unset ZEITGHOST_REQUIRE_SIGNING for unsigned runs."
        )

    selected = select_for_reanalysis(
        load_articles_from_shards(store),
        source=source, since=since, limit=limit,
    )
    console.print(f"[bold]{len(selected)} articles selected for re-analysis[/bold] "
                  f"(model: {model})")
    if not selected:
        return

    if dry_run:
        console.print("[yellow]Dry run — no Claude calls, no writes[/yellow]")
        for a in selected[:10]:
            console.print(f"  [{a.bias_label} {a.bias_score:.2f}] "
                          f"{a.original.source_name}: {a.original.title[:60]}")
        if len(selected) > 10:
            console.print(f"  … and {len(selected) - 10} more")
        return

    console.print(f"[bold]Re-analyzing {len(selected)} articles with Claude...[/bold]")
    # analyze_batch stamps each result's .model with the model used, so the
    # revision shard records which model produced it.
    results = asyncio.run(analyze_batch([a.original for a in selected], model=model))
    skipped = len(selected) - len(results)
    console.print(f"  {len(results)} re-analyzed, {skipped} skipped (analysis failed)")
    if not results:
        return

    # Lineage indexes resolve each entity's current head → the new shards chain
    # onto it as revisions (every selected article already exists, so all chain).
    internal_lineage = build_lineage_index(store, SCOPE_INTERNAL)
    sw_lineage = build_lineage_index(store, SCOPE_SW_ARTICLE)
    console.print("  Signing shards (ZEITGHOST_SIGNING_KEY configured)"
                  if seed else "  [dim]No signing key — writing unsigned shards[/dim]")
    emitter, trace_path = init_trace_emitter(store)
    for r in results:
        article_to_internal_shard(r, store, lineage_index=internal_lineage,
                                  signing_seed=seed, emitter=emitter)
        article_to_sw_shard(r, store, lineage_index=sw_lineage,
                            signing_seed=seed, emitter=emitter)
    console.print(f"  {len(results) * 2} revision shards written (chained via "
                  f"parent_shard_id)")
    from spiritwriter.fabric.emitter import verify_chain
    events = emitter.get_events()
    console.print(f"  Trace: {len(events)} events recorded → traces/{trace_path.name}")
    if not verify_chain(events):
        console.print("  [red]Warning: trace chain failed self-verification[/red]")


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


@main.command(name="gen-signing-key")
@click.option("--store/--no-store", "store_key", default=True,
              help="Store the key in the OS keychain (default). --no-store "
                   "only prints it, for manual provisioning on a headless host.")
@click.option("--print-seed", is_flag=True,
              help="Also echo the secret seed after a successful keychain store "
                   "(for mirroring to the prod env var). --no-store always "
                   "prints it; otherwise the seed stays off-screen by default.")
def gen_signing_key(store_key: bool, print_seed: bool):
    """Generate an Ed25519 key for signing shards' provenance.

    Shards written by `zeitghost ingest` are signed whenever ZEITGHOST_SIGNING_KEY
    is resolvable (OS keychain or env var), stamping each with a verifiable
    signature + `created_by` thumbprint. This mints a fresh 32-byte seed,
    stores it in the keychain (unless --no-store), and prints the public-key
    thumbprint — the signer identity `MemoryShard.verify()` checks against.

    Record the thumbprint somewhere durable. The seed itself is secret and is
    NOT echoed by default after a keychain store — pass --print-seed (or use
    --no-store) to reveal it when you need to mirror the identity onto the
    headless us-ny1 builder via a ZEITGHOST_SIGNING_KEY env var.
    """
    import os as _os
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from spiritwriter.fabric.shard import pubkey_thumbprint
    from zeitghost.shards import SIGNING_KEY_NAME

    seed = _os.urandom(32)  # a 32-byte Ed25519 seed
    pub = (Ed25519PrivateKey.from_private_bytes(seed).public_key()
           .public_bytes(encoding=serialization.Encoding.Raw,
                         format=serialization.PublicFormat.Raw))
    seed_hex = seed.hex()
    console.print(f"[bold]Signer thumbprint:[/bold] {pubkey_thumbprint(pub)}")

    stored = False
    if store_key:
        from spiritwriter.secrets import configure, set_api_key
        configure(service_name="zeitghost")
        stored = set_api_key(SIGNING_KEY_NAME, seed_hex)

    if stored:
        console.print(f"[green]Stored {SIGNING_KEY_NAME} in the OS keychain — "
                      f"the next `zeitghost ingest` will sign its shards.[/green]")
        if print_seed:
            console.print(f"[dim]seed (secret; for the prod env var): {seed_hex}[/dim]")
        else:
            console.print("[dim]Seed kept off-screen. Re-run with --print-seed "
                          "to reveal it for mirroring to prod.[/dim]")
    else:
        # Not stored (--no-store, or keychain unavailable): the printed seed is
        # the only copy, so it must be shown regardless of --print-seed.
        if store_key:
            console.print("[yellow]Keychain unavailable — key NOT stored.[/yellow]")
        console.print("Provision it yourself (e.g. on the us-ny1 builder):")
        console.print(f"  [bold]export {SIGNING_KEY_NAME}={seed_hex}[/bold]")


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
