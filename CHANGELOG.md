# Changelog

All notable changes to `zeitghost` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project follows pre-1.0 SemVer — **minor** for breaking changes, **patch** for additive/non-breaking changes.

## [0.1.0] — 2026-05-31

Initial public release. Apache-2.0, companion to [spiritwriter](https://github.com/aaronmarkham/spiritwriter-core).

### Added
- Static-site pipeline: NewsAPI fetch → Claude bias analysis → dual left/right rewrites → `sw:article` shards → Jinja2 site with a client-side bias slider.
- `zeitghost` CLI: `ingest`, `reanalyze`, `build`, `analytics`, `import-legacy`, `gen-signing-key`.
- Two shard scopes per article: `zeitghost:article` (internal, full analysis + variants) and `sw:article` (consumer-agnostic, atom keys matching the shared spiritwriter convention). Re-analysis chains revisions via `parent_shard_id` lineage.
- Optional Ed25519 shard signing (opt-in; fail-open until a key is provisioned).
- Source-bias analytics rollup with bias-drift sparklines.
- Legacy importer that seeds shards from a restored HtmxNewsEngine Postgres dump, skipping rows with NULL bias rather than default-filling them to "center".
- Robustness invariants enforced in `tests/test_robustness.py`: no partial-state data reaches renderers, and no file I/O at module-import time.
- Deployment infra: Docker Compose (builder + nginx), Ansible-over-Tailscale playbook, and GitHub Actions CI/CD.
