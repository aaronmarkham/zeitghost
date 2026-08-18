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

def test_legacy_importer_skips_null_bias_rows():
    """A row with NULL political_bias_score means HtmxNewsEngine's async
    analysis hadn't completed. The importer must skip these rows, not
    default them to 0.5 (which would silently mislabel an unanalyzed
    article as "center")."""
    from scripts.import_legacy_dump import _row_to_components

    # Analyzed article — should be parsed
    ok = _row_to_components({
        "title": "A", "url": "https://e.com/a", "abstract": "S", "source": "CNN",
        "category": "politics", "political_bias_score": 0.42,
        "is_variant": False, "variant_type": None,
        "published_at": None, "created_at": None,
    })
    assert ok is not None
    assert ok[1] == 0.42

    # In-flight — bias_score is NULL — must be skipped
    skipped = _row_to_components({
        "title": "B", "url": "https://e.com/b", "abstract": "S", "source": "FOX",
        "category": "politics", "political_bias_score": None,
        "is_variant": False, "variant_type": None,
        "published_at": None, "created_at": None,
    })
    assert skipped is None


def test_legacy_importer_skips_malformed_bias():
    """Garbage values in political_bias_score (non-numeric) should be skipped
    rather than silently coerced to a default."""
    from scripts.import_legacy_dump import _row_to_components

    skipped = _row_to_components({
        "title": "A", "url": "https://e.com/a", "abstract": "S", "source": "CNN",
        "category": "politics", "political_bias_score": "not-a-float",
        "is_variant": False, "variant_type": None,
        "published_at": None, "created_at": None,
    })
    assert skipped is None


def test_legacy_importer_pairs_originals_with_variants():
    """Originals are linked to their left/right variants via
    original_article_id. The combined AnalyzedArticle should carry both."""
    from scripts.import_legacy_dump import _build_analyzed

    original = {
        "id": 1, "title": "Bill passes", "url": "https://e.com/bill",
        "abstract": "A bill passed", "source": "AP", "category": "politics",
        "political_bias_score": 0.55, "is_variant": False, "variant_type": None,
        "published_at": None, "created_at": None,
    }
    variants = {
        "left": {"title": "Progressive bill passes",
                 "abstract": "A progressive bill passed"},
        "right": {"title": "Conservative bill passes",
                  "abstract": "A conservative bill passed"},
    }
    analyzed = _build_analyzed(original, variants)
    assert analyzed is not None
    assert analyzed.bias_score == 0.55
    assert analyzed.bias_label == "center"
    assert analyzed.variant_left_title == "Progressive bill passes"
    assert analyzed.variant_right_title == "Conservative bill passes"


def test_template_renders_with_full_data():
    """Sanity: the index template renders with normal article data."""
    from jinja2 import Environment, FileSystemLoader

    from zeitghost.bias import AnalyzedArticle, bias_lean_display
    from zeitghost.fetcher import Article

    env = Environment(
        loader=FileSystemLoader(str(REPO_ROOT / "templates")),
        autoescape=True,
    )
    env.filters["bias_lean"] = bias_lean_display
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


def test_landing_template_omits_version_chip_when_unknown():
    """If spiritwriter-core's installed version can't be resolved, the
    template must omit the version chip entirely rather than advertise a
    hardcoded fallback. (We previously had `or "0.6.0"` defaults that would
    silently advertise a stale version on a lookup miss.) Footer falls back
    to "local" when commit env var is also missing."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(str(REPO_ROOT / "templates")),
        autoescape=True,
    )
    rendered = env.get_template("landing.html").render(
        sw_core_version="",
        zeitghost_commit="",
    )
    # No literal version-string fallback anywhere in the rendered output —
    # the chip should be omitted, not filled with a stand-in.
    import re
    assert re.search(r"\bv?\d+\.\d+\.\d+\b", rendered) is None
    assert ">local<" in rendered


def test_landing_template_wires_commit_link_and_social_meta():
    """When a commit SHA is provided, the footer renders a link to the
    GitHub commit page with the full SHA in href and short SHA as the link
    text. Social-share meta tags must be present so link previews on
    Twitter / Slack / Discord aren't bare."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(str(REPO_ROOT / "templates")),
        autoescape=True,
    )
    rendered = env.get_template("landing.html").render(
        sw_core_version="x.y.z",
        zeitghost_commit="abc1234deadbeef",
    )
    # Full SHA in href, short SHA visible in link text
    assert "commit/abc1234deadbeef" in rendered
    assert ">abc1234<" in rendered
    # Social-share metadata wired up for link previews
    assert 'property="og:image"' in rendered
    assert 'name="twitter:card"' in rendered


def test_template_renders_with_empty_variants():
    """If analysis returned partial data (empty variant strings), the
    template must still render — slider just won't have anything new to show."""
    from jinja2 import Environment, FileSystemLoader

    from zeitghost.bias import AnalyzedArticle, bias_lean_display
    from zeitghost.fetcher import Article

    env = Environment(
        loader=FileSystemLoader(str(REPO_ROOT / "templates")),
        autoescape=True,
    )
    env.filters["bias_lean"] = bias_lean_display
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


def test_essay_template_renders_standalone():
    """The canonical-forms essay is a standalone page for spiritwriter.ai,
    like landing.html — it must not inherit the news masthead, and its
    interactive parts must survive Jinja rendering untouched."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(str(REPO_ROOT / "templates")),
        autoescape=True,
    )
    rendered = env.get_template("computed-not-assigned.html").render(
        sw_core_version="0.10.1",
        zeitghost_commit="abc1234deadbeef",
    )
    # A complete document, not a base.html fragment
    assert rendered.lstrip().startswith("<!doctype html>")
    assert "Daily Bias Index" not in rendered
    # The measured figures the essay argues from
    assert "114" in rendered and "200" in rendered
    # Precomputed scorer output must pass through autoescape intact — the
    # widget is wrong, not merely ugly, if these get mangled.
    assert '"01234":[1.0,1]' in rendered
    assert "leastRot" in rendered
    # Signature row wired to the live build
    assert "spiritwriter v0.10.1" in rendered
    assert "commit/abc1234deadbeef" in rendered
    assert ">abc1234<" in rendered
    # Social preview metadata, same as landing
    assert 'property="og:image"' in rendered
    assert 'name="twitter:card"' in rendered
    assert 'href="https://spiritwriter.ai/computed-not-assigned.html"' in rendered


def test_essay_template_degrades_without_build_metadata():
    """With no version or commit resolvable the signature row still renders,
    without advertising a stale stand-in version."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(str(REPO_ROOT / "templates")),
        autoescape=True,
    )
    rendered = env.get_template("computed-not-assigned.html").render(
        sw_core_version="",
        zeitghost_commit="",
    )
    assert "library · spiritwriter" in rendered
    assert "deploy · local" in rendered
    assert "spiritwriter v" not in rendered
