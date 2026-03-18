"""pipeline/update_crawl.py — Run all crawlers in the correct order and mode.

Execution order:
1. Parallel: clawhub, marketplace, skillhub (none use the GitHub search API quota)
2. Sequential: skillsmp then topic (both consume search API — must not race)
3. Chain: normalize → backfill_metadata → build_index (if --chain)

Usage:
    python pipeline/update_crawl.py --mode incremental [--sources clawhub,marketplace] [--chain]
    python pipeline/update_crawl.py --mode full --chain

Environment:
    GITHUB_TOKEN  GitHub PAT forwarded to each crawler subprocess.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)

ALL_SOURCES = ["clawhub", "marketplace", "skillhub", "skillsmp", "topic"]
PARALLEL_SOURCES = ["clawhub", "marketplace", "skillhub"]
SEQUENTIAL_SOURCES = ["skillsmp", "topic"]

_CRAWLER_MODULES = {
    "clawhub": "crawlers.clawhub_crawler",
    "marketplace": "crawlers.marketplace_crawler",
    "skillhub": "crawlers.skillhub_crawler",
    "skillsmp": "crawlers.skillsmp_crawler",
    "topic": "crawlers.topic_crawler",
}


def _run_crawler(
    source: str,
    mode: str,
    token: str | None,
    output_dir: str,
    extra_args: list[str] | None = None,
) -> int:
    """Run a single crawler as a subprocess.  Returns the exit code."""
    module = _CRAWLER_MODULES[source]
    output_path = Path(output_dir) / f"{source}.jsonl"
    log_path = Path("data/logs") / f"{source}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Build command as a list — never use string splitting (breaks on paths with spaces)
    cmd: list[str] = [
        sys.executable, "-m", module,
        "-o", str(output_path),
        "--mode", mode,
    ]
    if token:
        cmd += ["--token", token]
    if extra_args:
        cmd += extra_args

    logger.info("Starting crawler: %s (mode=%s)", source, mode)
    start = time.monotonic()
    with log_path.open("w") as log_fh:
        result = subprocess.run(cmd, stdout=log_fh, stderr=subprocess.STDOUT)
    elapsed = time.monotonic() - start

    if result.returncode == 0:
        record_count = (
            sum(1 for line in output_path.open(encoding="utf-8") if line.strip())
            if output_path.exists() else 0
        )
        logger.info(
            "Crawler %s finished OK: %d records in %.1fs (log: %s)",
            source, record_count, elapsed, log_path,
        )
    else:
        logger.error(
            "Crawler %s failed (exit %d) in %.1fs; see %s",
            source, result.returncode, elapsed, log_path,
        )
    return result.returncode


def run_all(
    mode: str,
    sources: list[str],
    token: str | None,
    output_dir: str,
    chain: bool,
) -> int:
    """Run all specified crawlers in the correct order.

    Returns:
        Number of failed steps (0 = success).
    """
    parallel = [s for s in PARALLEL_SOURCES if s in sources]
    sequential = [s for s in SEQUENTIAL_SOURCES if s in sources]
    errors = 0
    total_start = time.monotonic()

    # ------------------------------------------------------------------ phase 1
    # Crawlers that don't use GitHub Search API quota can run in parallel.
    if parallel:
        phase_start = time.monotonic()
        logger.info("Phase 1: parallel — %s", parallel)
        with ThreadPoolExecutor(max_workers=len(parallel)) as pool:
            futures = {
                pool.submit(_run_crawler, s, mode, token, output_dir): s
                for s in parallel
            }
            for fut in as_completed(futures):
                if fut.result() != 0:
                    errors += 1
        logger.info("Phase 1 done in %.1fs", time.monotonic() - phase_start)

    # ------------------------------------------------------------------ phase 2
    # skillsmp and topic both call the GitHub Search API — run sequentially to
    # avoid burning the shared 10 req/min search quota in parallel.
    if sequential:
        phase_start = time.monotonic()
    for source in sequential:
        logger.info("Phase 2: sequential — %s", source)
        if _run_crawler(source, mode, token, output_dir) != 0:
            errors += 1
    if sequential:
        logger.info("Phase 2 done in %.1fs", time.monotonic() - phase_start)

    # ------------------------------------------------------------------ phase 3
    # Optional pipeline chain: normalize → backfill → build index.
    # Only runs when there were no crawler errors.
    if chain and errors == 0:
        logger.info("Phase 3: running pipeline chain")
        raw_files = sorted(Path(output_dir).glob("*.jsonl"))
        raw_paths = [str(p) for p in raw_files]

        chain_steps: list[list[str]] = [
            [sys.executable, "pipeline/normalize.py"] + raw_paths + ["-o", "data/unified_skills.jsonl"],
            [sys.executable, "pipeline/backfill_metadata.py"] + raw_paths,
            [sys.executable, "pipeline/build_index.py"],
        ]
        for step_cmd in chain_steps:
            logger.info("Chain step: %s", " ".join(step_cmd[:4]) + " ...")
            result = subprocess.run(step_cmd)
            if result.returncode != 0:
                logger.error("Chain step failed: %s", step_cmd[0:4])
                errors += 1
                break

    logger.info("All phases done in %.1fs (%d errors)", time.monotonic() - total_start, errors)
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run all crawlers in the correct mode and order.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["full", "incremental", "metadata", "discover"],
        default="incremental",
        help="Crawl mode passed to every crawler subprocess",
    )
    parser.add_argument(
        "--sources",
        default=",".join(ALL_SOURCES),
        metavar="LIST",
        help="Comma-separated list of sources to run",
    )
    parser.add_argument(
        "--token",
        default=None,
        metavar="TOKEN",
        help="GitHub PAT (overrides GITHUB_TOKEN env var)",
    )
    parser.add_argument(
        "--output-dir",
        default="data/raw",
        metavar="DIR",
        help="Directory for raw crawler JSONL output",
    )
    parser.add_argument(
        "--chain",
        action="store_true",
        help="After crawling, run normalize → backfill_metadata → build_index",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    token = args.token or os.environ.get("GITHUB_TOKEN")
    sources = [s.strip() for s in args.sources.split(",") if s.strip() in ALL_SOURCES]

    if not sources:
        logger.error("No valid sources specified.  Choose from: %s", ALL_SOURCES)
        return 1

    errors = run_all(args.mode, sources, token, args.output_dir, args.chain)
    if errors:
        logger.error("%d step(s) failed", errors)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
