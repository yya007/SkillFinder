"""
crawlers/skillhub_crawler.py — SkillHub curated registry crawler.

Strategy: scrape skillhub.club listing pages and individual skill detail pages.
Every SkillHub entry points to an underlying GitHub repository.

CRITICAL dedup invariant: repo_url in every output record must be the GitHub
repo URL (normalised via extract_github_url), never the SkillHub page URL.
If no GitHub URL can be found on a detail page, the record is skipped.

Output: data/raw/skillhub.jsonl
Each record conforms to the raw record schema defined in PRD-001.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from typing import Iterator
from urllib.robotparser import RobotFileParser
from urllib.parse import urljoin, urlparse

try:
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "beautifulsoup4 is required: pip install beautifulsoup4 lxml"
    ) from exc

from crawlers.base import (
    extract_github_url,
    load_existing_urls,
    make_session,
    write_jsonl,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILLHUB_BASE = "https://skillhub.club"
_SKILLS_LIST_PATH = "/skills"         # listing endpoint; ?page=N for pagination
_REQUEST_DELAY = 1.5                  # seconds between HTTP requests (polite crawl)
_USER_AGENT = "SkillFinder-Crawler/1.0 (+https://github.com/skillfinder/skillfinder)"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# robots.txt enforcement
# ---------------------------------------------------------------------------

def _load_robots(session) -> RobotFileParser:
    """Fetch and parse SkillHub's robots.txt."""
    robots_url = f"{SKILLHUB_BASE}/robots.txt"
    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        resp = session.get(robots_url, timeout=15)
        resp.raise_for_status()
        rp.parse(resp.text.splitlines())
        log.debug("Loaded robots.txt from %s", robots_url)
    except Exception as exc:
        log.warning("Could not fetch robots.txt (%s); proceeding without restrictions.", exc)
    return rp


def _can_fetch(rp: RobotFileParser, url: str) -> bool:
    """Return True if robots.txt allows fetching url for our user-agent."""
    return rp.can_fetch(_USER_AGENT, url)


# ---------------------------------------------------------------------------
# Listing page scraper
# ---------------------------------------------------------------------------

def get_skill_list_page(session, page: int = 1) -> tuple[list[dict], bool]:
    """Fetch one page of the SkillHub skill listing.

    Sends a GET request to ``{SKILLHUB_BASE}/skills?page={page}`` and parses
    the HTML to find skill cards.

    Each partial skill dict has at minimum:
        name          (str)  — skill display name
        skillhub_url  (str)  — absolute URL to the SkillHub detail page
        description   (str)  — snippet / short description from the card
        rank          (str)  — S/A/B/C badge text (empty string if not found)

    Returns:
        (skills, has_more) where has_more is True when a "next page" link exists.

    Adds a _REQUEST_DELAY second delay before making the request.
    """
    time.sleep(_REQUEST_DELAY)

    url = f"{SKILLHUB_BASE}{_SKILLS_LIST_PATH}"
    params = {"page": page} if page > 1 else {}
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")

    skills: list[dict] = []

    # SkillHub renders skill cards; we look for common patterns.
    # Attempt 1: look for <article> or <div> elements with a "skill" class variant.
    card_selectors = [
        "article.skill-card",
        "div.skill-card",
        "li.skill-item",
        "div.skill",
        "article",           # broad fallback — filtered further below
    ]

    cards = []
    for selector in card_selectors:
        cards = soup.select(selector)
        if cards:
            log.debug("Found %d cards with selector '%s' on page %d", len(cards), selector, page)
            break

    for card in cards:
        # --- extract link to detail page ---
        link_tag = card.find("a", href=True)
        if link_tag is None:
            continue
        href = link_tag["href"]
        if not href.startswith("http"):
            href = urljoin(SKILLHUB_BASE, href)
        # Only follow links that are within skillhub.club
        if urlparse(href).netloc not in ("skillhub.club", "www.skillhub.club"):
            continue

        # --- extract name ---
        # Prefer heading tags, fall back to link text
        name_tag = card.find(["h1", "h2", "h3", "h4"])
        name = name_tag.get_text(strip=True) if name_tag else link_tag.get_text(strip=True)
        if not name:
            continue

        # --- extract description snippet ---
        # Typically a <p> inside the card
        desc_tag = card.find("p")
        description = desc_tag.get_text(strip=True) if desc_tag else ""

        # --- extract rank badge ---
        rank = ""
        rank_tag = card.find(class_=lambda c: c and "rank" in c.lower() if c else False)
        if rank_tag:
            rank_text = rank_tag.get_text(strip=True)
            # Keep only the letter grade (S/A/B/C)
            for grade in ("S", "A", "B", "C"):
                if grade in rank_text:
                    rank = grade
                    break

        skills.append(
            {
                "name": name,
                "skillhub_url": href,
                "description": description,
                "rank": rank,
            }
        )

    # --- detect next page ---
    has_more = False
    # Look for a "next" link or pagination element
    next_link = soup.find("a", string=lambda s: s and ("next" in s.lower() or "›" in s or "»" in s))
    if next_link is None:
        # Check rel="next"
        next_link = soup.find("a", rel=lambda r: r and "next" in r)
    if next_link is None:
        # Check for a page number link greater than current page
        page_links = soup.select("a[href*='page=']")
        for pl in page_links:
            href = pl.get("href", "")
            try:
                pnum = int(href.split("page=")[-1].split("&")[0])
                if pnum > page:
                    has_more = True
                    break
            except (ValueError, IndexError):
                continue
    else:
        has_more = True

    log.info("Page %d: found %d skill cards, has_more=%s", page, len(skills), has_more)
    return skills, has_more


# ---------------------------------------------------------------------------
# Detail page scraper
# ---------------------------------------------------------------------------

def get_skill_detail(session, skillhub_url: str) -> dict | None:
    """Fetch and parse a SkillHub skill detail page.

    Extracts:
        full_description  (str)   — complete description text
        github_url        (str)   — GitHub repo URL (REQUIRED for dedup)
        rank              (str)   — S/A/B/C
        overall_score     (float) — 0–10
        dimension_scores  (dict)  — dimension name → float score

    Returns None if the GitHub URL cannot be found on the page.
    Adds a _REQUEST_DELAY second delay before making the request.
    """
    time.sleep(_REQUEST_DELAY)

    resp = session.get(skillhub_url, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")

    # --- find GitHub URL ---
    github_url: str | None = None

    # Strategy 1: look for <a href="https://github.com/...">
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        candidate = extract_github_url(href)
        if candidate:
            github_url = candidate
            break

    if github_url is None:
        log.debug("No GitHub URL found on detail page: %s", skillhub_url)
        return None

    # --- full description ---
    full_description = ""
    desc_candidates = [
        soup.find("div", class_=lambda c: c and "description" in c.lower() if c else False),
        soup.find("section", class_=lambda c: c and "description" in c.lower() if c else False),
        soup.find("div", class_=lambda c: c and "content" in c.lower() if c else False),
        soup.find("main"),
        soup.find("article"),
    ]
    for candidate in desc_candidates:
        if candidate:
            text = candidate.get_text(separator=" ", strip=True)
            if len(text) > len(full_description):
                full_description = text
    # Fallback: meta description
    if not full_description:
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            full_description = meta_desc["content"].strip()

    # --- rank ---
    rank = ""
    rank_tag = soup.find(class_=lambda c: c and "rank" in c.lower() if c else False)
    if rank_tag:
        rank_text = rank_tag.get_text(strip=True)
        for grade in ("S", "A", "B", "C"):
            if grade in rank_text:
                rank = grade
                break

    # --- overall score ---
    overall_score: float = 0.0
    score_tag = soup.find(
        class_=lambda c: c and ("overall" in c.lower() or "score" in c.lower()) if c else False
    )
    if score_tag:
        score_match = re.search(r"\b(\d+(?:\.\d+)?)\b", score_tag.get_text())
        if score_match:
            try:
                overall_score = float(score_match.group(1))
            except ValueError:
                pass

    # --- dimension scores ---
    dimension_scores: dict[str, float] = {}
    _DIMENSIONS = ("Practicality", "Clarity", "Automation", "Quality", "Impact")
    for dim in _DIMENSIONS:
        # Look for a tag whose text contains the dimension name
        dim_tag = soup.find(
            lambda tag, d=dim: tag.get_text and d.lower() in tag.get_text().lower()
            and tag.name not in ("html", "body", "head", "script", "style")
        )
        if dim_tag:
            score_match = re.search(r"\b(\d+(?:\.\d+)?)\b", dim_tag.get_text())
            if score_match:
                try:
                    dimension_scores[dim] = float(score_match.group(1))
                except ValueError:
                    pass

    return {
        "full_description": full_description,
        "github_url": github_url,
        "rank": rank,
        "overall_score": overall_score,
        "dimension_scores": dimension_scores,
    }


# ---------------------------------------------------------------------------
# Record builder
# ---------------------------------------------------------------------------

def build_raw_record(skill: dict) -> dict | None:
    """Build a raw record from a unified parsed skill dict.

    The input dict has keys: name, description, repo_url, rank,
    overall_score, dimension_scores. This is the format returned by
    scrape_skill_listing() and also used directly in tests.

    CRITICAL: repo_url must be a valid GitHub URL for cross-source dedup.
    If extract_github_url() returns None, returns None (record is skipped).

    Args:
        skill: Unified skill dict from scrape_skill_listing().

    Returns:
        Raw record dict or None if no valid GitHub URL.
    """
    repo_url = extract_github_url(skill.get("repo_url", ""))
    if repo_url is None:
        log.debug("Cannot build record: no valid GitHub URL in %r", skill.get("repo_url"))
        return None

    return {
        "repo_url": repo_url,
        "name": skill["name"],
        "description": skill.get("description", ""),
        "source": "skillhub",
        "raw_metadata": {
            "rank": skill.get("rank", ""),
            "overall_score": skill.get("overall_score", 0.0),
            "dimension_scores": skill.get("dimension_scores", {}),
            "skillhub_url": skill.get("skillhub_url", ""),
        },
    }


def scrape_skill_listing(session=None, limit: int = None) -> list[dict]:
    """Scrape all SkillHub skills and return unified parsed skill dicts.

    Handles pagination and detail-page fetching internally. Each returned
    dict has keys: name, description, repo_url, rank, overall_score,
    dimension_scores, skillhub_url.

    Args:
        session: requests.Session (creates one if None).
        limit:   Stop after this many skills (for testing).

    Returns:
        List of unified skill dicts.
    """
    if session is None:
        session = make_session()
        session.headers.update({"User-Agent": _USER_AGENT})

    rp = _load_robots(session)
    results: list[dict] = []
    page = 1

    while True:
        list_url = f"{SKILLHUB_BASE}{_SKILLS_LIST_PATH}?page={page}"
        if not _can_fetch(rp, list_url):
            log.warning("robots.txt disallows %s; stopping.", list_url)
            break

        try:
            skill_cards, has_more = get_skill_list_page(session, page=page)
        except Exception as exc:
            log.error("Failed to fetch listing page %d: %s", page, exc)
            break

        if not skill_cards:
            break

        for basic in skill_cards:
            detail_url = basic.get("skillhub_url", "")
            if not _can_fetch(rp, detail_url):
                continue
            try:
                detail = get_skill_detail(session, detail_url) or {}
            except Exception as exc:
                log.warning("Failed detail page %s: %s", detail_url, exc)
                detail = {}

            unified = {
                "name": basic["name"],
                "description": detail.get("full_description") or basic.get("description", ""),
                "repo_url": detail.get("github_url", ""),
                "rank": detail.get("rank") or basic.get("rank", ""),
                "overall_score": detail.get("overall_score", 0.0),
                "dimension_scores": detail.get("dimension_scores", {}),
                "skillhub_url": detail_url,
            }
            results.append(unified)

            if limit is not None and len(results) >= limit:
                return results

        if not has_more:
            break
        page += 1

    return results


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def run(output_path: str, limit: int = None, resume: bool = False) -> int:
    """Run the SkillHub crawler.

    Args:
        output_path: Path to output JSONL file.
        limit:       Stop after writing this many records (for testing).
        resume:      If True, skip repos already present in output_path.

    Returns:
        Number of new records written.
    """
    session = make_session()
    session.headers.update({"User-Agent": _USER_AGENT})

    existing_urls: set[str] = set()
    if resume:
        existing_urls = load_existing_urls(output_path)
        log.info("Resume mode: %d repos already in output", len(existing_urls))

    skills = scrape_skill_listing(session=session, limit=limit)

    batch: list[dict] = []
    written = 0

    for skill in skills:
        if limit is not None and written + len(batch) >= limit:
            break

        record = build_raw_record(skill)
        if record is None:
            continue

        if record["repo_url"] in existing_urls:
            log.debug("Skipping already-crawled repo: %s", record["repo_url"])
            continue

        batch.append(record)
        existing_urls.add(record["repo_url"])

        if len(batch) >= 50:
            written += write_jsonl(batch, output_path, append=(written > 0 or resume))
            batch = []

    if batch:
        written += write_jsonl(batch, output_path, append=(written > 0 or resume))

    log.info("SkillHub crawler done. %d records written to %s", written, output_path)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Crawl skillhub.club and emit raw JSONL records.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "-o", "--output",
        required=True,
        metavar="PATH",
        help="Output JSONL file path (e.g. data/raw/skillhub.jsonl)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after writing N records (for testing)",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Skip repos already present in the output file",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        stream=sys.stderr,
    )

    try:
        count = run(
            output_path=args.output,
            limit=args.limit,
            resume=args.resume,
        )
        print(f"Wrote {count} total records to {args.output}", file=sys.stderr)
        return 0
    except Exception as exc:
        log.error("SkillHub crawler failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
