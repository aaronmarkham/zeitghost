"""Bias analysis and dual L/R variant generation using Claude."""

import hashlib
import json
import logging
from dataclasses import dataclass, field

from zeitghost.fetcher import Article

log = logging.getLogger(__name__)

# Lazy import — spiritwriter LLM provider
_provider = None

# Haiku is well-suited to this task: structured JSON output, mechanical
# rewrite-with-X-framing pattern, no deep analysis required. ~5x cheaper
# than Sonnet at acceptable quality for prompted opinion-page rewrites.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def _get_provider():
    global _provider
    if _provider is None:
        from spiritwriter.secrets import configure
        configure(service_name="zeitghost")
        from spiritwriter.llm.anthropic import AnthropicProvider
        _provider = AnthropicProvider()
    return _provider


@dataclass
class AnalyzedArticle:
    """Article with bias score and BOTH left- and right-leaning rewrites."""
    original: Article
    bias_score: float          # 0.0 (far left) .. 1.0 (far right)
    bias_label: str            # "left" | "center-left" | "center" | "center-right" | "right"
    variant_left_title: str    # left-leaning rewrite of title
    variant_left_summary: str  # left-leaning rewrite of summary
    variant_right_title: str   # right-leaning rewrite of title
    variant_right_summary: str
    analysis_notes: str = ""

    # Shard-substrate provenance — populated by `_shard_to_article` when an
    # article is loaded back from the store, used by the card's flip panel
    # to show what physically backs this analysis. Default-empty so freshly-
    # constructed articles (e.g. in the legacy importer) round-trip cleanly.
    shard_id: str = ""
    parent_shard_id: str | None = None
    shard_created_at: str = ""
    shard_scope: str = ""
    shard_decay: str = ""
    shard_tags: list[str] = field(default_factory=list)
    shard_atom_kinds: dict[str, int] = field(default_factory=dict)
    model: str = ""            # LLM model that produced bias + variants
    agent: str = ""            # zeitghost/<ver> sw_core/<ver>

    @property
    def permalink_slug(self) -> str:
        """Stable slug used for the per-article page filename.

        Imported HtmxNewsEngine articles use their original integer id so old
        share URLs (/article/46274) keep working. New articles get a short
        SHA-256 prefix of their source URL.
        """
        if self.original.legacy_id is not None:
            return str(self.original.legacy_id)
        return hashlib.sha256(self.original.url.encode()).hexdigest()[:10]

    @property
    def permalink(self) -> str:
        """Relative URL of the article's permalink page (used in templates)."""
        return f"article/{self.permalink_slug}.html"

    @property
    def _bias_rgb(self) -> tuple[int, int, int]:
        """Interpolated RGB tuple for this article's bias score.

        Endpoints match the CSS palette so inline styles and stylesheet
        bias-* class colors line up:
        - --left  #2C5274 (RGB 44, 82, 116)   — oxford blue
        - --right #B0463A (RGB 176, 70, 58)   — terracotta (sibling to
                                                landing's wax-seal accent)
        """
        r = round(44 + (176 - 44) * self.bias_score)
        g = round(82 + (70 - 82) * self.bias_score)
        b = round(116 + (58 - 116) * self.bias_score)
        return (r, g, b)

    @property
    def bias_tint(self) -> str:
        """CSS rgb() string for the article's bias color."""
        r, g, b = self._bias_rgb
        return f"rgb({r}, {g}, {b})"

    @property
    def bias_tint_inline(self) -> str:
        """Inline style fragment exposing --bias-r/g/b CSS custom properties.

        The stylesheet uses these in `rgb()` and `rgba()` to derive the card's
        left border, background tint, and bias-bar marker — keeping the color
        math in Python so templates stay declarative.
        """
        r, g, b = self._bias_rgb
        return f"--bias-r: {r}; --bias-g: {g}; --bias-b: {b};"


def bias_lean_display(score: float, center_tolerance: float = 0.025) -> str:
    """Render a 0..1 bias score as a neutral direction + magnitude.

    Mirror in JS: `biasLeanDisplay()` in static/js/slider.js. Keep the
    formula (|tilt| × 200) and the `center_tolerance` default in sync —
    if you retune the band here, retune there too.


    The internal storage convention (0 = left, 0.5 = center, 1 = right)
    is unit-interval math that's convenient for interpolation but reads
    politically ("100% bias = right") when shown raw. This helper
    renders any score as a symmetric tilt off center, with magnitude
    scaled so each side runs 0–100% (i.e. magnitude = |tilt| × 200,
    so an article at score 0.0 reads as "L · 100%" — full left).

        0.50 → "CENTER"
        0.74 → "R · 48%"
        0.32 → "L · 36%"
        0.00 → "L · 100%"
        1.00 → "R · 100%"

    Values within `center_tolerance` of 0.5 render as "CENTER" without
    a magnitude — the slider's true mid-point is a verbal label, not a
    near-zero number.
    """
    tilt = score - 0.5
    if abs(tilt) < center_tolerance:
        return "CENTER"
    direction = "R" if tilt > 0 else "L"
    mag_pct = int(round(abs(tilt) * 200))
    return f"{direction} · {mag_pct}%"


ANALYSIS_PROMPT = """\
You are analyzing a news article for political bias and producing two
opposite-leaning rewrites of it.

Article:
Title: {title}
Source: {source_name}
Content:
{content}

Respond with ONLY a JSON object (no markdown, no code fences):
{{
    "bias_score": <float 0.0=far left, 0.5=center, 1.0=far right>,
    "bias_label": "<left|center-left|center|center-right|right>",
    "variant_left": {{
        "title": "<rewritten title with progressive/left-leaning framing>",
        "summary": "<3-4 sentence rewrite emphasizing systemic context, equity, marginalized perspectives>"
    }},
    "variant_right": {{
        "title": "<rewritten title with conservative/right-leaning framing>",
        "summary": "<3-4 sentence rewrite emphasizing personal responsibility, rule of law, traditional values>"
    }},
    "analysis_notes": "<brief reasoning for the bias score and what each variant changed>"
}}

Guidelines:
- Use ONLY facts present in the Content above. Do not invent events,
  statistics, quotes, or context that aren't in the source.
- The variants should be plausible as real-world opinion-page rewrites of
  the same factual story, not propaganda. Subtle framing wins.
- Use active voice and natural news prose.
- bias_score reflects the ORIGINAL article's lean, not the variants'.
"""


def _extract_json(text: str) -> dict | None:
    """Extract first valid JSON object from LLM response.

    Handles markdown fences, trailing text, and nested braces.
    """
    text = text.strip()
    if "```" in text:
        lines = text.split("\n")
        inside = False
        json_lines = []
        for line in lines:
            if line.strip().startswith("```"):
                inside = not inside
                continue
            if inside or not json_lines:
                json_lines.append(line)
        text = "\n".join(json_lines).strip()

    start = text.find("{")
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text, start)
        return obj
    except json.JSONDecodeError:
        return None


async def analyze_article(article: Article) -> AnalyzedArticle | None:
    """Analyze one article — compute bias and generate both variants."""
    provider = _get_provider()
    # Prefer the trafilatura-extracted body when present (richer context for
    # Claude); fall back to NewsAPI's terse description if body fetch failed.
    content = article.body if article.body else article.summary
    prompt = ANALYSIS_PROMPT.format(
        title=article.title,
        content=content,
        source_name=article.source_name,
    )

    try:
        response = await provider.query(prompt, model=DEFAULT_MODEL)
        data = _extract_json(response)
        if data is None:
            log.warning("No valid JSON in response for '%s'", article.title[:50])
            log.debug("Raw response: %s", response[:500])
            return None

        left = data.get("variant_left", {}) or {}
        right = data.get("variant_right", {}) or {}
        return AnalyzedArticle(
            original=article,
            bias_score=float(data.get("bias_score", 0.5)),
            bias_label=data.get("bias_label", "center"),
            variant_left_title=left.get("title", article.title),
            variant_left_summary=left.get("summary", article.summary),
            variant_right_title=right.get("title", article.title),
            variant_right_summary=right.get("summary", article.summary),
            analysis_notes=data.get("analysis_notes", ""),
        )
    except (KeyError, ValueError) as e:
        log.warning("Analysis failed for '%s': %s", article.title[:50], e)
        return None
    except Exception as e:
        log.error("LLM error analyzing '%s': %s", article.title[:50], e)
        return None


async def analyze_batch(articles: list[Article]) -> list[AnalyzedArticle]:
    """Analyze a batch of articles. Skips any that fail to parse."""
    results = []
    for article in articles:
        analyzed = await analyze_article(article)
        if analyzed is not None:
            results.append(analyzed)
    log.info("Analyzed %d articles, %d succeeded", len(articles), len(results))
    return results
