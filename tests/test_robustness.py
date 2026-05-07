"""Robustness tests — codify two production-debug lessons from HtmxNewsEngine
(2026-05-06):

1. Async pipelines mean some rows are mid-enrichment. Renderers must not
   assume enrichment fields are populated. Defaulting NULL bias to 0.5
   silently mislabels unanalyzed articles as "center"; we skip them instead.

2. Module-import-time file I/O breaks in fresh production containers.
   Anything that touches the filesystem at import must create-or-fall-back.
   Test by importing from an empty cwd via subprocess.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Lesson 1: partial-state data must not silently get a default value.
# ---------------------------------------------------------------------------

def test_legacy_importer_skips_null_bias_rows(tmp_path: Path):
    """A row with NULL political_bias_score means analysis hadn't completed.
    The importer must skip such rows, not default them to 0.5 (which would
    silently mislabel an unanalyzed article as "center")."""
    from scripts.import_legacy_dump import import_dump

    dump = tmp_path / "with_null_bias.sql"
    dump.write_text(
        "COPY public.news_article (id, title, url, abstract, source, "
        "category, political_bias_score, is_variant, variant_type, "
        "original_article_id, published_at, created_at) FROM stdin;\n"
        # Analyzed article — should be imported
        "1\tAnalyzed\thttps://e.com/a\tSummary A\tCNN\tpolitics\t"
        "0.42\tf\t\\N\t\\N\t2026-05-07 12:00:00\t2026-05-07 12:00:00\n"
        # In-flight article — saved but bias_score is NULL — should be SKIPPED
        "2\tInflight\thttps://e.com/b\tSummary B\tFOX\tpolitics\t"
        "\\N\tf\t\\N\t\\N\t2026-05-07 12:00:00\t2026-05-07 12:00:00\n"
        "\\.\n",
        encoding="utf-8",
    )
    written = import_dump(dump, dry_run=True)
    assert written == 1, "expected the analyzed row imported, the NULL-bias row skipped"


def test_legacy_importer_skips_malformed_bias(tmp_path: Path):
    """Bias columns containing garbage strings should be skipped, not
    silently coerced to a default."""
    from scripts.import_legacy_dump import import_dump

    dump = tmp_path / "garbage_bias.sql"
    dump.write_text(
        "COPY public.news_article (id, title, url, abstract, source, "
        "category, political_bias_score, is_variant, variant_type, "
        "original_article_id, published_at, created_at) FROM stdin;\n"
        "1\tA\thttps://e.com/a\tS\tCNN\tpolitics\t"
        "not-a-float\tf\t\\N\t\\N\t2026-05-07 12:00:00\t2026-05-07 12:00:00\n"
        "\\.\n",
        encoding="utf-8",
    )
    assert import_dump(dump, dry_run=True) == 0


def test_template_renders_with_full_data():
    """Sanity: the index template renders with normal article data."""
    from jinja2 import Environment, FileSystemLoader

    from zeitghost.bias import AnalyzedArticle
    from zeitghost.fetcher import Article

    env = Environment(
        loader=FileSystemLoader(str(REPO_ROOT / "templates")),
        autoescape=True,
    )
    article = AnalyzedArticle(
        original=Article(
            title="Senate passes bill", url="https://example.com/a",
            summary="A bill passed", source_name="Test Source",
            published="2026-05-07T00:00:00+00:00",
        ),
        bias_score=0.42, bias_label="center-left",
        variant_left_title="Senate passes progressive bill",
        variant_left_summary="A progressive bill passed",
        variant_right_title="Senate passes conservative bill",
        variant_right_summary="A conservative bill passed",
    )
    rendered = env.get_template("index.html").render(
        articles=[article], site_name="Test", site_tagline="t",
        generated_at="2026-05-07T00:00:00", total_articles=1, source_count=1,
    )
    assert "Senate passes bill" in rendered           # original variant
    assert "progressive bill" in rendered             # left variant embedded
    assert "conservative bill" in rendered            # right variant embedded
    assert "0.42" in rendered                         # bias score formatted
    assert 'data-bias="0.42"' in rendered             # slider data attr


def test_build_renders_with_empty_shards(tmp_path: Path):
    """Build must always emit index.html even when shards are empty.

    Otherwise nginx falls through to whatever was in /usr/share/nginx/html
    when the named volume was first created — which on nginx:alpine is
    the welcome page. Caught on us-ny1's first deploy.
    """
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(str(REPO_ROOT / "templates")),
        autoescape=True,
    )
    rendered = env.get_template("index.html").render(
        articles=[], site_name="Test", site_tagline="t",
        generated_at="2026-05-07T00:00:00", total_articles=0, source_count=0,
    )
    # The empty-state placeholder must be present so visitors don't see a
    # blank page or stale welcome content
    assert "No articles ingested yet" in rendered
    # Slider markup is still there even with no articles
    assert "bias-slider" in rendered


def test_template_renders_with_empty_variants():
    """If analysis returned partial data (empty variant strings), the
    template must still render — slider just won't have anything new to show."""
    from jinja2 import Environment, FileSystemLoader

    from zeitghost.bias import AnalyzedArticle
    from zeitghost.fetcher import Article

    env = Environment(
        loader=FileSystemLoader(str(REPO_ROOT / "templates")),
        autoescape=True,
    )
    article = AnalyzedArticle(
        original=Article(
            title="Some title", url="https://example.com/x",
            summary="Some summary", source_name="Source",
            published="2026-05-07T00:00:00+00:00",
        ),
        bias_score=0.5, bias_label="center",
        variant_left_title="", variant_left_summary="",
        variant_right_title="", variant_right_summary="",
    )
    # Should NOT raise:
    rendered = env.get_template("index.html").render(
        articles=[article], site_name="Test", site_tagline="t",
        generated_at="2026-05-07T00:00:00", total_articles=1, source_count=1,
    )
    assert "Some title" in rendered


# ---------------------------------------------------------------------------
# Lesson 2: importing the package must not require any cwd-relative state.
# Production containers are a clean slate — no logs/ directory, no .env, etc.
# ---------------------------------------------------------------------------

def test_package_imports_from_clean_cwd(tmp_path: Path):
    """Run `python -c 'import zeitghost; from zeitghost.cli import main'`
    from an empty directory. If any module-level code tries to open
    cwd-relative files (logs/, output/, etc.), this surfaces the bug
    before it lands in a fresh container."""
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import zeitghost; from zeitghost.cli import main; "
            "from zeitghost.fetcher import Article; "
            "from zeitghost.bias import AnalyzedArticle; "
            "from zeitghost.shards import init_store; "
            "from zeitghost.generator import generate_site; "
            "from zeitghost.analytics import compute_source_stats; "
            "print('ok')",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "Clean-cwd import failed — something in zeitghost is doing file I/O at "
        f"import time. This breaks in production containers.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "ok" in result.stdout
