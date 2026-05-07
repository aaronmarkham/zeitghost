"""Static site generator — builds HTML with embedded L/R variants per article."""

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from zeitghost.analytics import SourceStats, compute_source_stats
from zeitghost.bias import AnalyzedArticle

log = logging.getLogger(__name__)


def _serialize_for_json(articles: list[AnalyzedArticle]) -> list[dict]:
    return [
        {
            "title": a.original.title,
            "summary": a.original.summary,
            "url": a.original.url,
            "source": a.original.source_name,
            "published": a.original.published,
            "categories": a.original.categories,
            "bias_score": a.bias_score,
            "bias_label": a.bias_label,
            "variant_left": {
                "title": a.variant_left_title,
                "summary": a.variant_left_summary,
            },
            "variant_right": {
                "title": a.variant_right_title,
                "summary": a.variant_right_summary,
            },
        }
        for a in articles
    ]


def generate_site(articles: list[AnalyzedArticle],
                  template_dir: Path,
                  output_dir: Path,
                  static_dir: Path | None = None,
                  site_name: str = "Zeitghost",
                  site_tagline: str = "Bias-aware news, your slider's choice",
                  max_articles: int = 100) -> Path:
    """Render the index page with the bias slider and a per-source stats page."""
    output_dir.mkdir(parents=True, exist_ok=True)

    for old in output_dir.glob("*.html"):
        old.unlink()

    env = Environment(loader=FileSystemLoader(str(template_dir)),
                      autoescape=True)

    articles = sorted(articles, key=lambda a: a.original.published, reverse=True)
    visible = articles[:max_articles]
    stats = compute_source_stats(articles)
    now = datetime.now(timezone.utc).isoformat()

    base_ctx = {
        "site_name": site_name,
        "site_tagline": site_tagline,
        "generated_at": now,
        "total_articles": len(articles),
        "source_count": len(stats),
    }

    # --- index.html with the bias slider ------------------------------------
    index_tmpl = env.get_template("index.html")
    (output_dir / "index.html").write_text(
        index_tmpl.render(articles=visible, **base_ctx),
        encoding="utf-8",
    )
    log.info("Generated index.html with %d visible articles (of %d total)",
             len(visible), len(articles))

    # --- analytics.html — per-source bias rollup ----------------------------
    analytics_tmpl = env.get_template("analytics.html")
    (output_dir / "analytics.html").write_text(
        analytics_tmpl.render(stats=stats, **base_ctx),
        encoding="utf-8",
    )
    log.info("Generated analytics.html with %d sources", len(stats))

    # --- articles.json (consumer feed) --------------------------------------
    (output_dir / "articles.json").write_text(
        json.dumps(_serialize_for_json(articles), indent=2),
        encoding="utf-8",
    )

    # --- static assets ------------------------------------------------------
    if static_dir and static_dir.exists():
        dest = output_dir / "static"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(static_dir, dest)
        log.info("Copied static assets")

    log.info("Site generated at %s", output_dir)
    return output_dir


def generate_analytics_only(articles: list[AnalyzedArticle],
                            template_dir: Path,
                            output_dir: Path,
                            site_name: str = "Zeitghost",
                            site_tagline: str = "Bias-aware news, your slider's choice"
                            ) -> Path:
    """Regenerate just the analytics page — used when only the rollup needs updating."""
    output_dir.mkdir(parents=True, exist_ok=True)
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    stats = compute_source_stats(articles)
    ctx = {
        "site_name": site_name,
        "site_tagline": site_tagline,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_articles": len(articles),
        "source_count": len(stats),
        "stats": stats,
    }
    (output_dir / "analytics.html").write_text(
        env.get_template("analytics.html").render(**ctx),
        encoding="utf-8",
    )
    return output_dir / "analytics.html"


__all__ = ["generate_site", "generate_analytics_only", "SourceStats"]
