"""Static site generator — builds HTML with embedded L/R variants per article."""

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from zeitghost.analytics import (
    SourceStats, compute_source_stats,
    compute_overall_stats, compute_bias_distribution, compute_category_stats,
    compute_monthly_stats, render_bias_sparkline, source_slug,
    top_leaning_sources,
)
from zeitghost.bias import AnalyzedArticle

log = logging.getLogger(__name__)


def _serialize_for_json(articles: list[AnalyzedArticle]) -> list[dict]:
    return [
        {
            "title": a.original.title,
            "summary": a.original.summary,
            "url": a.original.url,
            "permalink": a.permalink,
            "legacy_id": a.original.legacy_id,
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


def _render_source_pages(articles: list[AnalyzedArticle],
                         env: Environment,
                         output_dir: Path,
                         base_ctx: dict,
                         min_articles: int = 5,
                         max_articles_per_page: int = 500) -> int:
    """Emit /source/<slug>.html for each source with >=min_articles.

    Each page lists that source's articles (most recent first, capped) plus
    a monthly bias-drift table so users can see whether the source's mean
    bias has shifted over the timespan we have.
    """
    import statistics

    source_dir = output_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    for stale in source_dir.glob("*.html"):
        stale.unlink()

    by_source: dict[str, list[AnalyzedArticle]] = {}
    for a in articles:
        if a.original.source_name:
            by_source.setdefault(a.original.source_name, []).append(a)

    tmpl = env.get_template("source.html")
    written = 0
    seen_slugs: set[str] = set()

    for source_name, source_articles in by_source.items():
        if len(source_articles) < min_articles:
            continue

        slug = source_slug(source_name)
        # Disambiguate slug collisions (e.g. two source names slugify to the
        # same string) by suffixing -2, -3, ...
        original_slug = slug
        suffix = 2
        while slug in seen_slugs:
            slug = f"{original_slug}-{suffix}"
            suffix += 1
        seen_slugs.add(slug)

        source_articles.sort(key=lambda a: a.original.published, reverse=True)
        visible_articles = source_articles[:max_articles_per_page]

        scores = [a.bias_score for a in source_articles]
        avg = statistics.fmean(scores) if scores else 0.0
        # Coarse 3-band lean for the headline badge
        if avg < 0.48: overall_lean = "left"
        elif avg > 0.52: overall_lean = "right"
        else: overall_lean = "center"

        first_date = min((a.original.published for a in source_articles
                          if a.original.published), default="")[:10]
        last_date = max((a.original.published for a in source_articles
                         if a.original.published), default="")[:10]

        monthly = compute_monthly_stats(source_articles)
        ctx = {
            **base_ctx,
            "source_name": source_name,
            "source_slug": slug,
            "articles": visible_articles,
            "total_count": len(source_articles),
            "first_date": first_date,
            "last_date": last_date,
            "overall_avg": f"{avg:.2f}",
            "overall_lean": overall_lean,
            "monthly": monthly,
            "sparkline_svg": render_bias_sparkline(monthly),
        }
        (source_dir / f"{slug}.html").write_text(
            tmpl.render(**ctx),
            encoding="utf-8",
        )
        written += 1
    return written


def _render_article_pages(articles: list[AnalyzedArticle],
                          env: Environment,
                          output_dir: Path,
                          base_ctx: dict) -> int:
    """Emit output/article/<slug>.html for every article. Returns the count."""
    article_dir = output_dir / "article"
    article_dir.mkdir(parents=True, exist_ok=True)
    # Wipe stale files so deleted articles don't linger as 200s
    for stale in article_dir.glob("*.html"):
        stale.unlink()

    tmpl = env.get_template("article.html")
    written = 0
    seen_slugs: set[str] = set()
    for a in articles:
        slug = a.permalink_slug
        if slug in seen_slugs:
            log.warning("Duplicate permalink slug %r — skipping second occurrence "
                        "(url=%s)", slug, a.original.url)
            continue
        seen_slugs.add(slug)
        (article_dir / f"{slug}.html").write_text(
            tmpl.render(article=a, **base_ctx),
            encoding="utf-8",
        )
        written += 1
    return written


def generate_site(articles: list[AnalyzedArticle],
                  template_dir: Path,
                  output_dir: Path,
                  static_dir: Path | None = None,
                  site_name: str = "Zeitghost",
                  site_tagline: str = "Bias-aware news, your slider's choice",
                  max_articles: int = 500) -> Path:
    """Render the index page with the bias slider and a per-source stats page."""
    output_dir.mkdir(parents=True, exist_ok=True)

    for old in output_dir.glob("*.html"):
        old.unlink()

    env = Environment(loader=FileSystemLoader(str(template_dir)),
                      autoescape=True)

    articles = sorted(articles, key=lambda a: a.original.published, reverse=True)
    visible = articles[:max_articles]
    stats = compute_source_stats(articles)
    overall = compute_overall_stats(articles)
    bias_distribution = compute_bias_distribution(articles)
    category_stats = compute_category_stats(articles)
    left_sources = top_leaning_sources(stats, direction="left")
    right_sources = top_leaning_sources(stats, direction="right")
    now = datetime.now(timezone.utc).isoformat()

    # Commit SHA baked into the image at build time by ansible's
    # `docker compose build --build-arg COMMIT_SHA=...`. Falls back to ""
    # for local dev / hand-built images so the footer just omits the link.
    commit = os.environ.get("ZEITGHOST_COMMIT", "").strip()
    if commit in ("", "unknown"):
        commit = ""

    # Installed spiritwriter-core version. Useful in the footer because the
    # wheel is rebuilt per-deploy from whatever ref CI was pointed at — knowing
    # which release of the underlying lib generated this site closes the
    # provenance loop alongside the commit SHA.
    from importlib.metadata import version, PackageNotFoundError
    try:
        sw_core_version = version("spiritwriter-core")
    except PackageNotFoundError:
        sw_core_version = ""

    base_ctx = {
        "site_name": site_name,
        "site_tagline": site_tagline,
        "generated_at": now,
        "total_articles": len(articles),
        "source_count": len(stats),
        "zeitghost_commit": commit,
        "sw_core_version": sw_core_version,
    }

    # --- index.html with the bias slider ------------------------------------
    index_tmpl = env.get_template("index.html")
    (output_dir / "index.html").write_text(
        index_tmpl.render(articles=visible, **base_ctx),
        encoding="utf-8",
    )
    log.info("Generated index.html with %d visible articles (of %d total)",
             len(visible), len(articles))

    # --- per-article permalink pages ---------------------------------------
    n_articles = _render_article_pages(articles, env, output_dir, base_ctx)
    log.info("Generated %d /article/<slug>.html permalink pages", n_articles)

    # --- per-source pages with time-travel ---------------------------------
    n_sources = _render_source_pages(articles, env, output_dir, base_ctx)
    log.info("Generated %d /source/<slug>.html pages (sources with ≥5 articles)",
             n_sources)

    # --- spiritwriter.ai landing page --------------------------------------
    # nginx serves this at the root for spiritwriter.ai + www.spiritwriter.ai
    # (separate server block); news.spiritwriter.ai keeps the regular
    # index.html. Receives sw_core_version + zeitghost_commit so the footer
    # signature row reflects the live deploy.
    landing_tmpl = env.get_template("landing.html")
    (output_dir / "landing.html").write_text(
        landing_tmpl.render(**base_ctx),
        encoding="utf-8",
    )
    log.info("Generated landing.html")

    # --- about page — methodology, shard format, lineage explainer ---------
    about_tmpl = env.get_template("about.html")
    (output_dir / "about.html").write_text(
        about_tmpl.render(**base_ctx),
        encoding="utf-8",
    )
    log.info("Generated about.html")

    # --- analytics.html — overall + distribution + leaning + categories ---
    analytics_tmpl = env.get_template("analytics.html")
    (output_dir / "analytics.html").write_text(
        analytics_tmpl.render(
            stats=stats,
            overall=overall,
            bias_distribution=bias_distribution,
            category_stats=category_stats,
            left_sources=left_sources,
            right_sources=right_sources,
            **base_ctx,
        ),
        encoding="utf-8",
    )
    log.info("Generated analytics.html (%d sources, %d categories, %d-%d-%d L-C-R)",
             len(stats), len(category_stats),
             bias_distribution.left, bias_distribution.center, bias_distribution.right)

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
        "overall": compute_overall_stats(articles),
        "bias_distribution": compute_bias_distribution(articles),
        "category_stats": compute_category_stats(articles),
        "left_sources": top_leaning_sources(stats, direction="left"),
        "right_sources": top_leaning_sources(stats, direction="right"),
    }
    (output_dir / "analytics.html").write_text(
        env.get_template("analytics.html").render(**ctx),
        encoding="utf-8",
    )
    return output_dir / "analytics.html"


__all__ = ["generate_site", "generate_analytics_only", "SourceStats"]
