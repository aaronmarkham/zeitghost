# Zeitghost

National news with bias analysis and dual left/right variants. A spiritwriter-powered static site generator that fetches NewsAPI articles, analyzes political bias with Claude, generates left- and right-leaning rewrites, and builds a static site with a client-side bias slider.

Sister project to [perseus-news](https://github.com/aaronmarkham/perseus-news) (regional enforcement focus). Both depend on [spiritwriter-core](https://github.com/aaronmarkham/spiritwriter-core) and share the same article shard format (`sw:article`).

## What it does

```
NewsAPI → fetcher.py → bias.py (Claude) → shards.py → generator.py → output/
                       │                                │
                       └─→ bias score 0..1, L/R variants  └─→ static HTML + slider JS
```

The bias slider is **client-side**: every article card embeds left, original, and right variants; JS swaps the visible variant based on the slider position.

## Commands

```bash
pip install -e ".[dev]"

zeitghost ingest                    # fetch newest from NewsAPI, analyze, write shards
zeitghost build                     # render static site from shard store
zeitghost analytics                 # regenerate source-bias rollup page
zeitghost import-legacy --db-url postgresql://...  # seed shards from a temp Postgres
                                    # (see `zeitghost import-legacy --help` for full
                                    # workflow with HtmxNewsEngine SQL dump)
```

## Configuration

- `feeds/newsapi.yaml` — NewsAPI categories + keyword filters
- `~/.zeitghost/shards/` — shard store (override with `ZEITGHOST_SHARD_STORE`)
- Secrets via `spiritwriter.secrets` keychain: `ANTHROPIC_API_KEY`, `NEWS_API_KEY`

## Deployment

`infra/ansible/inventories/us-ny1/hosts.yml` deploys to `news.spiritwriter.ai`. Mirrors the perseus-news pattern: Tailscale + Ansible + Docker Compose, behind nginx, TLS via Cloudflare.

```bash
./infra/docker/build-wheels.sh
cd infra/ansible
ansible-playbook deploy.yml -i inventories/us-ny1/hosts.yml
```

CI/CD via GitHub Actions — push to `main` runs tests, builds the spiritwriter-core wheel, and deploys via Tailscale.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
