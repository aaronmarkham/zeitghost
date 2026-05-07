# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Zeitghost is a spiritwriter-powered static site generator for national bias-analyzed news. It fetches NewsAPI articles, analyzes political bias with Claude, generates BOTH left- and right-leaning rewrites per article, and builds a static site with a client-side bias slider that swaps which variant is visible.

Sister project to perseus-news (regional/enforcement focus, single right-leaning variant). Both share the `sw:article` shard format. Zeitghost serves `news.spiritwriter.ai` from us-ny1.

## Architecture

```
feeds/newsapi.yaml → fetcher.py → bias.py (Claude) → shards.py → generator.py → output/
(NewsAPI config)     (HTTP)        (analysis + L/R vars)  (sw:article)  (Jinja2 + slider JS)
```

## Commands

```bash
pip install -e ".[dev]"
zeitghost ingest                    # fetch + analyze + write shards
zeitghost build                     # render site from shards
zeitghost analytics                 # source-bias rollup page
zeitghost import-legacy <dump.sql>  # HtmxNewsEngine SQL → shards
```

## Key Files

- `feeds/newsapi.yaml` — NewsAPI categories, query filters, fetch limits
- `zeitghost/fetcher.py` — NewsAPI HTTP client, quota tracking
- `zeitghost/bias.py` — Claude prompt → bias_score + variant_left + variant_right
- `zeitghost/shards.py` — sw:article shard write/read (mirrors perseus convention)
- `zeitghost/analytics.py` — per-source bias rollups from shard scan
- `zeitghost/generator.py` — Jinja2 site builder, embeds all 3 variants per card
- `zeitghost/cli.py` — Click CLI
- `templates/` — base.html, index.html (with slider), source.html, analytics.html
- `static/css/style.css`, `static/js/slider.js` — client-side bias slider
- `scripts/import_legacy_dump.py` — read HtmxNewsEngine pg_dump → write shards

## Dependencies

- `spiritwriter-core>=0.3.0` — shards, LLM provider, secrets
- `anthropic>=0.40.0` — Claude API (used via spiritwriter LLM provider)
- `requests` — NewsAPI HTTP
- `jinja2`, `click`, `rich`, `pyyaml`

## Bias Slider (client-side, no server)

Each article in the rendered HTML carries three variants — left/original/right — plus its own analyzed `data-bias` score. The slider on the page changes which variant is visible:

- slider < 0.35 → show `.variant-left`
- 0.35 ≤ slider ≤ 0.65 → show `.variant-original`
- slider > 0.65 → show `.variant-right`

This is purely a presentation toggle; nothing hits the server. If "filter across the whole archive" becomes a requirement later, add a tiny FastAPI layer that reads from shards (mirror frio's web pattern).

## Shard Format

Two shard scopes per article (mirror perseus's pattern):

- `zeitghost:article` — internal, contains both variants and full analysis atoms.
- `sw:article` — consumer-agnostic, atom keys match `frio/src/shard_engine.py shard_from_article()` convention. Entity key: `article:{sha256(url)}` (full hash). Lineage via `parent_shard_id` on re-analysis.

## Deployment

us-ny1 (Ubuntu, Tailscale-meshed, NY). Docker stack: `builder` (loops `zeitghost ingest && zeitghost build`) + `nginx` (serves `output/`). Behind Cloudflare for TLS. CI/CD via GitHub Actions: PR runs tests; push-to-main builds spiritwriter-core wheel, deploys via Ansible-over-Tailscale.

## Robustness invariants (production-debug lessons)

Two real bugs hit HtmxNewsEngine prod on 2026-05-06 that we want to keep out of zeitghost:

1. **No partial-state data reaches renderers.** Articles enriched by async LLM analysis can sit in storage with NULL bias before analysis completes. zeitghost's ingest pipeline writes shards only after analysis succeeds — and the legacy-dump importer skips rows with NULL `political_bias_score` rather than defaulting them to 0.5 (which would silently mislabel an unanalyzed article as "center"). When adding new fields enriched by async work, follow the same rule: skip or guard, never default-fill.
2. **No file I/O at module-import time.** Logging, config loaders, and cache singletons must defer to `main()` / runtime, never run at import. Production containers are a clean slate — no `logs/`, no `output/`, no `.env`. CI runs `python -c "import zeitghost"` from a fresh empty cwd to catch regressions.

Both are also encoded in `tests/test_robustness.py`.

## Notes

- Initial article corpus came from HtmxNewsEngine's PostgreSQL dump (~46MB, ~tens of thousands of articles with prior OpenAI bias scores). Use `zeitghost import-legacy` to seed shards from that dump.
- HtmxNewsEngine's user/auth/onboarding/preferences sprawl is intentionally NOT carried over — zeitghost is identity-free, slider-only.
