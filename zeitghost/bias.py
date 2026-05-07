"""Bias analysis and dual L/R variant generation using Claude."""

import json
import logging
from dataclasses import dataclass

from zeitghost.fetcher import Article

log = logging.getLogger(__name__)

# Lazy import — spiritwriter LLM provider
_provider = None


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


ANALYSIS_PROMPT = """\
You are analyzing a news article for political bias and producing two
opposite-leaning rewrites of it.

Article:
Title: {title}
Summary: {summary}
Source: {source_name}

Respond with ONLY a JSON object (no markdown, no code fences):
{{
    "bias_score": <float 0.0=far left, 0.5=center, 1.0=far right>,
    "bias_label": "<left|center-left|center|center-right|right>",
    "variant_left": {{
        "title": "<rewritten title with progressive/left-leaning framing>",
        "summary": "<2-3 sentence rewrite emphasizing systemic context, equity, marginalized perspectives>"
    }},
    "variant_right": {{
        "title": "<rewritten title with conservative/right-leaning framing>",
        "summary": "<2-3 sentence rewrite emphasizing personal responsibility, rule of law, traditional values>"
    }},
    "analysis_notes": "<brief reasoning for the bias score and what each variant changed>"
}}

Guidelines:
- Maintain factual accuracy in BOTH variants — do not fabricate events or statistics.
- The variants should be plausible as real-world opinion-page rewrites of the same factual story, not propaganda.
- Use active voice and natural news prose. Subtle framing wins; obvious framing reads as propaganda.
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
    prompt = ANALYSIS_PROMPT.format(
        title=article.title,
        summary=article.summary,
        source_name=article.source_name,
    )

    try:
        response = await provider.query(prompt)
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
