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
import base64
import json
import logging
import re
import sys
import time
from urllib.robotparser import RobotFileParser
from urllib.parse import parse_qs, urljoin, urlparse

import yaml

try:
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "beautifulsoup4 is required: pip install beautifulsoup4 lxml"
    ) from exc

from crawlers.base import (
    GITHUB_API,
    add_to_filter_cache,
    extract_github_url,
    fetch_repo_metadata,
    find_skill_md_paths,
    infer_platforms,
    load_filter_cache,
    make_session,
    write_jsonl,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILLHUB_BASE = "https://skillhub.club"
_SKILLS_LIST_PATH = "/skills"         # listing endpoint; ?category=X&page=N for pagination
_REQUEST_DELAY = 0.5                  # seconds between HTTP requests (polite crawl)
_USER_AGENT = "SkillFinder-Crawler/1.0 (+https://github.com/skillfinder/skillfinder)"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SKILL.md helpers
# ---------------------------------------------------------------------------

def _parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from a SKILL.md string."""
    if not content or not content.startswith("---"):
        return {}
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return {}
    yaml_text = content[3: end_match.start() + 3]
    try:
        fm = yaml.safe_load(yaml_text)
        if not isinstance(fm, dict):
            return {}
        return fm
    except yaml.YAMLError:
        return {}


def _fetch_skill_md(session, repo_full_name: str, path: str = "SKILL.md", default_branch: str = "main") -> str | None:
    """Fetch raw SKILL.md content from a GitHub repo via GitHub API."""
    url = f"{GITHUB_API}/repos/{repo_full_name}/contents/{path}"
    try:
        resp = session.get(url, params={"ref": default_branch}, timeout=30)
    except Exception as exc:
        log.debug("Network error fetching SKILL.md from %s: %s", repo_full_name, exc)
        return None
    if resp.status_code == 200:
        try:
            encoded = resp.json().get("content", "")
            return base64.b64decode(encoded.replace("\n", "")).decode("utf-8", errors="replace")
        except Exception:
            return None
    return None


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

def discover_categories(session) -> list[str]:
    """Fetch the SkillHub skills index page and return all category slugs.

    Parses ``?category=<slug>&page=1`` links from the pagination bar.
    Returns a deduplicated list of category slug strings, e.g.
    ["development", "devops", "testing", ...].  Returns an empty list
    on failure (caller falls back to uncategorised pagination).
    """
    try:
        time.sleep(_REQUEST_DELAY)
        resp = session.get(f"{SKILLHUB_BASE}{_SKILLS_LIST_PATH}", timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Could not fetch category list: %s", exc)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    cats: list[str] = []
    seen: set[str] = set()
    for a in soup.select("a[href*='category=']"):
        href = a.get("href", "")
        try:
            cat = href.split("category=")[1].split("&")[0]
        except IndexError:
            continue
        if cat and cat not in seen:
            seen.add(cat)
            cats.append(cat)
    log.info("Discovered %d SkillHub categories: %s", len(cats), cats)
    return cats


def get_skill_list_page(session, page: int = 1, category: str = "") -> tuple[list[dict], bool]:
    """Fetch one page of the SkillHub skill listing.

    Sends a GET request to ``{SKILLHUB_BASE}/skills?category={category}&page={page}``
    and parses the HTML to find skill cards.

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
    params: dict = {}
    if category:
        params["category"] = category
    if page > 1:
        params["page"] = page
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")

    skills: list[dict] = []

    # SkillHub (Next.js SSR): skill cards are <a class="block h-full group" href="/skills/...">
    # direct children of a <div class="grid ... gap-6"> container.
    grid = soup.find(
        "div",
        class_=lambda c: c and "grid" in c and "gap-6" in c if c else False,
    )
    cards = []
    if grid:
        cards = grid.find_all(
            "a",
            class_=lambda c: c and "group" in c if c else False,
            href=True,
        )
        log.debug("Found %d cards in skills grid on page %d", len(cards), page)

    for card in cards:
        # Card IS the <a> element — href is on the card itself
        href = card["href"]
        if not href.startswith("http"):
            href = urljoin(SKILLHUB_BASE, href)
        # Only follow links that are within skillhub.club
        if urlparse(href).netloc not in ("skillhub.club", "www.skillhub.club"):
            continue

        # --- extract name ---
        name_tag = card.find(["h1", "h2", "h3", "h4"])
        name = name_tag.get_text(strip=True) if name_tag else ""
        if not name:
            continue

        # --- extract description snippet ---
        desc_tag = card.find("p")
        description = desc_tag.get_text(strip=True) if desc_tag else ""

        # --- extract rank badge (class "rating-s", "rating-a", etc.) ---
        # BS4 class_ callbacks receive one class string at a time, so use direct class lookup.
        rank = ""
        for _grade in ("s", "a", "b", "c", "f"):
            rank_tag = card.find(class_=f"rating-{_grade}")
            if rank_tag:
                rank = rank_tag.get_text(strip=True)
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
        # Check for a page number link greater than current page.
        # Use urllib.parse to extract the `page` query param correctly —
        # naive split("page=")[-1] would pick up `per_page=12` as 12.
        page_links = soup.select("a[href*='page=']")
        for pl in page_links:
            href = pl.get("href", "")
            try:
                qs = parse_qs(urlparse(href).query)
                pnum = int(qs["page"][0])
                if pnum > page:
                    has_more = True
                    break
            except (KeyError, ValueError, IndexError):
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

    # --- full description (meta tags are most reliable) ---
    full_description = ""
    for meta_selector in [
        {"property": "og:description"},
        {"name": "description"},
    ]:
        meta_tag = soup.find("meta", attrs=meta_selector)
        if meta_tag and meta_tag.get("content", "").strip():
            full_description = meta_tag["content"].strip()
            break

    # --- rank: look for "Grade X" text in a font-pixel span ---
    rank = ""
    for span in soup.find_all("span", class_="font-pixel"):
        grade_match = re.match(r"^Grade\s+([SABCDF])$", span.get_text(strip=True))
        if grade_match:
            rank = grade_match.group(1)
            break

    # --- overall score: the large text-4xl font-pixel span ---
    overall_score: float = 0.0
    score_tag = soup.select_one("span.text-4xl.font-pixel")
    if score_tag:
        score_match = re.search(r"\b(\d+(?:\.\d+)?)\b", score_tag.get_text(strip=True))
        if score_match:
            try:
                overall_score = float(score_match.group(1))
            except ValueError:
                pass

    # --- dimension scores: each card pairs a font-pixel score with an uppercase label ---
    dimension_scores: dict[str, float] = {}
    for card in soup.select("div.text-center.p-3"):
        score_div = card.find("div", class_="font-pixel")
        label_div = card.find("div", class_="uppercase")
        if score_div and label_div:
            dim_name = label_div.get_text(strip=True).title()
            try:
                dimension_scores[dim_name] = float(score_div.get_text(strip=True))
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
            "stars": skill.get("stars", 0),
            "pushed_at": skill.get("pushed_at", ""),
            "skill_md_url": skill.get("skill_md_url", ""),
            "platforms": skill.get("platforms", ["claude_code"]),
        },
    }


def _match_skill_path(skill_name: str, skill_md_paths: dict[str, str]) -> str | None:
    """Pick the best SKILL.md path by matching the skill name to its parent directory.

    Normalises both the skill name and each path's parent directory to
    alphanumeric-only lowercase before comparing.  Falls back to the first
    result when no match is found.

    Args:
        skill_name:      Display name of the skill from SkillHub.
        skill_md_paths:  Dict of SKILL.md paths → blob SHAs from Trees API.

    Returns:
        Best-matching path, or None if skill_md_paths is empty.
    """
    if not skill_md_paths:
        return None

    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    name_norm = _norm(skill_name)

    for path in skill_md_paths:
        parts = path.split("/")
        if len(parts) >= 2:
            dir_norm = _norm(parts[-2])
            if name_norm == dir_norm or dir_norm in name_norm or name_norm in dir_norm:
                return path

    return next(iter(skill_md_paths))


def _iter_jsonl(path: str):
    """Yield parsed dicts from a JSONL file, silently skipping bad lines."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass


def load_existing_skillhub_urls(output_path: str) -> set[str]:
    """Load the set of skillhub_urls already present in an existing JSONL output file.

    Used in resume mode to skip detail-page fetches for skills we have already
    crawled, avoiding redundant HTTP requests to skillhub.club.
    """
    return {
        record["raw_metadata"]["skillhub_url"]
        for record in _iter_jsonl(output_path)
        if record.get("raw_metadata", {}).get("skillhub_url")
    }


def scrape_skill_listing(
    session=None,
    limit: int = None,
    token: str = None,
    filter_cache_path: str = None,
    skip_skillhub_urls: set | None = None,
):
    """Scrape all SkillHub skills, yielding unified parsed skill dicts one at a time.

    Handles pagination and detail-page fetching internally. Each yielded
    dict has keys: name, description, repo_url, rank, overall_score,
    dimension_scores, skillhub_url, stars, pushed_at, skill_md_url, platforms.

    Args:
        session:             requests.Session for SkillHub HTTP (creates one if None).
        limit:               Stop after this many skills (for testing).
        token:               GitHub personal access token for fetching repo metadata.
        filter_cache_path:   Path to shared filter cache JSONL file.
        skip_skillhub_urls:  Set of skillhub_urls to skip without fetching detail pages
                             (used in resume mode to avoid redundant HTTP requests).

    Yields:
        Unified skill dicts.
    """
    if session is None:
        session = make_session()
        session.headers.update({"User-Agent": _USER_AGENT})

    github_session = make_session(token=token) if token else None

    # Load filter cache
    filter_cache: set[str] = set()
    if filter_cache_path:
        filter_cache = load_filter_cache(filter_cache_path)
        log.info("Filter cache loaded: %d entries", len(filter_cache))

    rp = _load_robots(session)
    yielded = 0
    seen_urls: set[str] = set()  # dedup across categories by skillhub_url
    # Cache repo_full_name → (meta, default_branch, skill_md_paths) to avoid
    # repeated GitHub API calls for the same monorepo.
    repo_paths_cache: dict[str, tuple[dict, str, dict[str, str]]] = {}

    # Discover all categories; "" = uncategorised (catches skills not in any category)
    categories = discover_categories(session)
    crawl_targets = [""] + categories
    log.info("Crawling %d targets: uncategorised + %d categories", len(crawl_targets), len(categories))

    for category in crawl_targets:
        page = 1
        while True:
            list_url = (
                f"{SKILLHUB_BASE}{_SKILLS_LIST_PATH}?category={category}&page={page}"
                if category else
                f"{SKILLHUB_BASE}{_SKILLS_LIST_PATH}?page={page}"
            )
            if not _can_fetch(rp, list_url):
                log.warning("robots.txt disallows %s; stopping.", list_url)
                break

            try:
                skill_cards, has_more = get_skill_list_page(session, page=page, category=category)
            except Exception as exc:
                log.error("Failed to fetch listing page %d (category=%r): %s", page, category, exc)
                break

            if not skill_cards:
                break

            for basic in skill_cards:
                skillhub_url = basic.get("skillhub_url", "")

                # Dedup across categories — same skill may appear in multiple categories
                if skillhub_url in seen_urls:
                    continue
                seen_urls.add(skillhub_url)

                # Resume mode: skip detail fetch for already-crawled skills
                if skip_skillhub_urls and skillhub_url in skip_skillhub_urls:
                    log.debug("Skipping already-crawled skillhub URL: %s", skillhub_url)
                    continue

                detail_url = skillhub_url
                if not _can_fetch(rp, detail_url):
                    continue
                try:
                    detail = get_skill_detail(session, detail_url) or {}
                except Exception as exc:
                    log.warning("Failed detail page %s: %s", detail_url, exc)
                    detail = {}

                github_url = detail.get("github_url", "")
                stars = 0
                pushed_at = ""
                skill_md_url = ""
                platforms = infer_platforms({}, "skillhub")

                # Check filter cache before making GitHub API calls
                if github_url and github_url in filter_cache:
                    log.debug("Skipping %s: in filter cache", github_url)
                    continue

                if github_url and github_session:
                    full_name = github_url.removeprefix("https://github.com/")

                    if full_name in repo_paths_cache:
                        meta, default_branch, skill_md_paths = repo_paths_cache[full_name]
                    else:
                        meta = fetch_repo_metadata(github_session, full_name)
                        default_branch = meta.get("default_branch", "main")
                        skill_md_paths = find_skill_md_paths(github_session, full_name)
                        repo_paths_cache[full_name] = (meta, default_branch, skill_md_paths)
                        # Only write to filter cache when we're confident no SKILL.md exists.
                        # find_skill_md_paths returns {} for both "none found" and transient
                        # errors; we accept the risk of a false negative here rather than
                        # permanently blocking a repo on a rate-limit hiccup.
                        if not skill_md_paths and filter_cache_path:
                            add_to_filter_cache(filter_cache_path, github_url, "no_skill_md")
                            filter_cache.add(github_url)

                    stars = meta.get("stargazers_count", stars)
                    pushed_at = meta.get("pushed_at", pushed_at)

                    skill_path = _match_skill_path(basic["name"], skill_md_paths) if skill_md_paths else None

                    if skill_path:
                        skill_md_url = f"{github_url}/blob/{default_branch}/{skill_path}"
                        skill_content = _fetch_skill_md(
                            github_session, full_name, path=skill_path, default_branch=default_branch
                        )
                        if skill_content:
                            fm = _parse_frontmatter(skill_content)
                            platforms = infer_platforms(fm, "skillhub")

                unified = {
                    "name": basic["name"],
                    "description": detail.get("full_description") or basic.get("description", ""),
                    "repo_url": github_url,
                    "rank": detail.get("rank") or basic.get("rank", ""),
                    "overall_score": detail.get("overall_score", 0.0),
                    "dimension_scores": detail.get("dimension_scores", {}),
                    "skillhub_url": detail_url,
                    "stars": stars,
                    "pushed_at": pushed_at,
                    "skill_md_url": skill_md_url,
                    "platforms": platforms,
                }
                yield unified
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

            log.info(
                "category=%r page=%d  seen_urls=%d  yielded=%d",
                category or "(all)", page, len(seen_urls), yielded,
            )
            if not has_more:
                break
            page += 1


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def run(
    output_path: str,
    limit: int = None,
    resume: bool = False,
    token: str = None,
    filter_cache_path: str = None,
    mode: str = "full",
) -> int:
    """Run the SkillHub crawler.

    Args:
        output_path:       Path to output JSONL file.
        limit:             Stop after writing this many records (for testing).
        resume:            If True, skip repos already present in output_path.
        token:             GitHub personal access token for fetching repo metadata.
        filter_cache_path: Path to shared filter cache JSONL file.
        mode:              Crawl mode: full, incremental, metadata, or discover.
                           incremental behaves like resume=True.

    Returns:
        Number of new records written.
    """
    # Resolve mode: incremental aliases resume behaviour
    if mode == "incremental":
        resume = True
    session = make_session()
    session.headers.update({"User-Agent": _USER_AGENT})

    # Dedup key: skill_md_url when available (monorepo sub-skills share repo_url),
    # otherwise repo_url.  Using repo_url alone would block all sub-skills after
    # the first one from a given monorepo.
    existing_keys: set[str] = set()
    skip_skillhub_urls: set[str] = set()
    if resume:
        for line in _iter_jsonl(output_path):
            skill_md = line.get("raw_metadata", {}).get("skill_md_url", "")
            existing_keys.add(skill_md if skill_md else line.get("repo_url", ""))
        skip_skillhub_urls = load_existing_skillhub_urls(output_path)
        log.info(
            "Resume mode: %d keys already in output, %d skillhub URLs to skip",
            len(existing_keys), len(skip_skillhub_urls),
        )

    skills = scrape_skill_listing(
        session=session,
        limit=limit,
        token=token,
        filter_cache_path=filter_cache_path,
        skip_skillhub_urls=skip_skillhub_urls,
    )

    batch: list[dict] = []
    written = 0

    for skill in skills:
        if limit is not None and written + len(batch) >= limit:
            break

        record = build_raw_record(skill)
        if record is None:
            continue

        skill_md = record.get("raw_metadata", {}).get("skill_md_url", "")
        dedup_key = skill_md if skill_md else record["repo_url"]
        if dedup_key in existing_keys:
            log.debug("Skipping already-crawled skill: %s", dedup_key)
            continue

        batch.append(record)
        existing_keys.add(dedup_key)

        if len(batch) >= 50:
            written += write_jsonl(batch, output_path, append=(written > 0 or resume))
            log.info("Written %d records so far to %s", written, output_path)
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
        "--token",
        default=None,
        metavar="TOKEN",
        help="GitHub personal access token for fetching repo metadata (or set GITHUB_TOKEN env var)",
    )
    p.add_argument(
        "--mode",
        choices=["full", "incremental", "metadata", "discover"],
        default="full",
        help="Crawl mode: full=complete re-crawl, incremental=changed repos only, metadata=stars/ETags only, discover=new repos since last run",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="[Deprecated] Alias for --mode incremental",
    )
    p.add_argument(
        "--filter-cache",
        default="data/filter_cache.jsonl",
        metavar="PATH",
        help="Shared filter-cache JSONL file (pass empty string to disable)",
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

    import os as _os
    token = getattr(args, "token", None) or _os.environ.get("GITHUB_TOKEN")
    filter_cache_path = getattr(args, "filter_cache", None) or None

    # Deprecated flag aliases
    if args.resume:
        import warnings
        warnings.warn("--resume is deprecated, use --mode incremental", DeprecationWarning)
        if args.mode == "full":
            args.mode = "incremental"

    try:
        count = run(
            output_path=args.output,
            limit=args.limit,
            resume=args.resume,
            token=token,
            filter_cache_path=filter_cache_path,
            mode=args.mode,
        )
        print(f"Wrote {count} total records to {args.output}", file=sys.stderr)
        return 0
    except Exception as exc:
        log.error("SkillHub crawler failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
