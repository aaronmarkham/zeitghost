"""Fetch articles from NewsAPI.

NewsAPI has a 100 req/day limit on the developer tier. We track per-day usage
in a small JSON file alongside the shard store and refuse to over-fetch.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from spiritwriter.secrets import keychain

log = logging.getLogger(__name__)

NEWSAPI_BASE = "https://newsapi.org/v2"
FETCH_TIMEOUT = 15
DEFAULT_DAILY_QUOTA = 100


@dataclass
class Article:
    """A fetched article before bias analysis."""
    title: str
    url: str
    summary: str
    source_name: str
    published: str  # ISO 8601
    region: str = "national"
    categories: list[str] = field(default_factory=list)
    author: str = ""


def load_feed_config(config_path: Path) -> dict:
    """Load a YAML feed configuration file."""
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _quota_path(state_dir: Path) -> Path:
    return state_dir / "newsapi_quota.json"


def _load_quota(state_dir: Path) -> tuple[str, int]:
    """Return (date_str, count_used) for today, resetting on date change."""
    p = _quota_path(state_dir)
    today = datetime.now(timezone.utc).date().isoformat()
    if not p.exists():
        return today, 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("date") == today:
            return today, int(data.get("used", 0))
    except (json.JSONDecodeError, ValueError):
        pass
    return today, 0


def _save_quota(state_dir: Path, used: int) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    _quota_path(state_dir).write_text(
        json.dumps({"date": today, "used": used}), encoding="utf-8"
    )


def _api_key() -> str:
    """Resolve the NewsAPI key.

    Env var wins (works in any environment including Docker containers
    without a keyring backend). Falls back to spiritwriter's keychain for
    local-dev convenience.
    """
    key = os.environ.get("NEWS_API_KEY")
    if not key:
        try:
            key = keychain.get_api_key("newsapi")
        except Exception as e:
            log.debug("Keychain lookup failed: %s", e)
            key = None
    if not key:
        raise RuntimeError(
            "NewsAPI key not found. Either set the NEWS_API_KEY env var "
            "(production) or configure via "
            "`spiritwriter.secrets.keychain.set_api_key('newsapi', '...')` "
            "(local dev)."
        )
    return key


def _request(endpoint: str, params: dict) -> dict:
    """Single GET against NewsAPI. Returns parsed JSON."""
    headers = {"X-Api-Key": _api_key()}
    resp = requests.get(f"{NEWSAPI_BASE}/{endpoint}",
                        params=params, headers=headers, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _normalize(item: dict, source_fallback: str = "",
               categories: list[str] | None = None) -> Article | None:
    """Convert one NewsAPI article object to our Article dataclass."""
    title = (item.get("title") or "").strip()
    url = item.get("url") or ""
    if not title or not url:
        return None
    src = (item.get("source") or {}).get("name") or source_fallback or "Unknown"
    summary = item.get("description") or item.get("content") or ""
    return Article(
        title=title,
        url=url,
        summary=summary[:500],
        source_name=src,
        published=item.get("publishedAt") or datetime.now(timezone.utc).isoformat(),
        region="national",
        categories=list(categories or []),
        author=item.get("author") or "",
    )


def fetch_top_headlines(category: str, country: str = "us",
                        page_size: int = 50) -> list[Article]:
    """Fetch top headlines for a NewsAPI category.

    Costs 1 request from the daily quota.
    """
    log.info("Fetching top-headlines (category=%s, country=%s)", category, country)
    data = _request("top-headlines", {
        "category": category,
        "country": country,
        "pageSize": min(page_size, 100),
    })
    items = data.get("articles", []) or []
    out = []
    for item in items:
        a = _normalize(item, categories=[category])
        if a:
            out.append(a)
    log.info("  %d headlines from %s", len(out), category)
    return out


def fetch_query(query: str, language: str = "en",
                page_size: int = 50,
                categories: list[str] | None = None) -> list[Article]:
    """Fetch articles matching a free-text query.

    Costs 1 request from the daily quota.
    """
    log.info("Fetching everything (q=%r)", query)
    data = _request("everything", {
        "q": query,
        "language": language,
        "sortBy": "publishedAt",
        "pageSize": min(page_size, 100),
    })
    items = data.get("articles", []) or []
    out = []
    for item in items:
        a = _normalize(item, categories=categories)
        if a:
            out.append(a)
    log.info("  %d hits for %r", len(out), query)
    return out


def fetch_all(config_path: Path, state_dir: Path,
              limit: int = 0,
              max_requests: int | None = None) -> list[Article]:
    """Fetch articles from all configured NewsAPI endpoints.

    Args:
        config_path: feeds/newsapi.yaml — defines categories, queries, quota.
        state_dir: where to persist daily quota tracking.
        limit: cap total articles returned across all sources (0=no cap).
        max_requests: cap requests this run (default: from config or quota remaining).
    """
    cfg = load_feed_config(config_path)
    quota = int(cfg.get("daily_quota", DEFAULT_DAILY_QUOTA))
    reserve = int(cfg.get("reserve_for_manual", 0))

    today, used = _load_quota(state_dir)
    remaining = max(0, quota - used - reserve)
    budget = min(remaining, max_requests) if max_requests else remaining
    log.info("NewsAPI quota: %d used today, %d remaining (budget this run: %d)",
             used, remaining, budget)

    if budget <= 0:
        log.warning("No quota remaining; returning empty list")
        return []

    all_articles: list[Article] = []
    spent = 0

    for entry in cfg.get("sources", []):
        if spent >= budget:
            log.info("Budget exhausted, stopping fetch")
            break
        kind = entry.get("kind", "category")
        try:
            if kind == "category":
                cat = entry["category"]
                articles = fetch_top_headlines(
                    cat,
                    country=entry.get("country", "us"),
                    page_size=entry.get("page_size", 50),
                )
            elif kind == "query":
                articles = fetch_query(
                    entry["query"],
                    language=entry.get("language", "en"),
                    page_size=entry.get("page_size", 50),
                    categories=entry.get("categories"),
                )
            else:
                log.warning("Unknown source kind %r in feed config", kind)
                continue
            spent += 1
            all_articles.extend(articles)
        except requests.HTTPError as e:
            log.warning("NewsAPI request failed for %r: %s", entry, e)
        except requests.RequestException as e:
            log.warning("Network error for %r: %s", entry, e)

    _save_quota(state_dir, used + spent)

    if limit:
        all_articles = all_articles[:limit]

    log.info("Total: %d articles, %d requests spent", len(all_articles), spent)
    return all_articles
