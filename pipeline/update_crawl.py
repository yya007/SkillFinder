"""pipeline/update_crawl.py — Run all crawlers in the correct order and mode.

Execution order:
1. Parallel: clawhub, marketplace, skillhub (no search quota)
2. Sequential: skillsmp then topic (both use search API)
3. Chain: normalize → backfill_metadata → build_index (if --chain)

Usage:
    python pipeline/update_crawl.py --mode incremental [--sources clawhub,marketplace] [--chain]
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)

ALL_SOURCES = ["clawhub", "marketplace", "skillhub", "skillsmp", "topic"]
PARALLEL_SOURCES = ["clawhub", "marketplace", "skillhub"]
SEQUENTIAL_SOURCES = ["skillsmp", "topic"]


def _run_crawler(source: str, mode: str, token: str | None, output_dir: str, extra_args: list[str] = None) -> int:
    """Run a single crawler subprocess. Returns exit code."""
    crawler_map = {
        "clawhub": "crawlers.clawhub_crawler",
        "marketplace": "crawlers.marketplace_crawler",
        "skillhub": "crawlers.skillhub_crawler",
        "skillsmp": "crawlers.skillsmp_crawler",
        "topic": "crawlers.topic_crawler",
    }
    module = crawler_map[source]
    output_path = str(Path(output_dir) / f"{source}.jsonl")
    log_path = str(Path("data/logs") / f"{source}.log")

    Path("data/logs").mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, "-m", module, "-o", output_path, "--mode", mode]
    if token:
        cmd += ["--token", token]
    if source == "topic":
        cmd += ["--data-dir", output_dir]
    if extra_args:
        cmd += extra_args

    logger.info("Starting crawler: %s (mode=%s)", source, mode)
    with open(log_path, "w") as log_fh:
        result = subprocess.run(cmd, stdout=log_fh, stderr=subprocess.STDOUT)

    if result.returncode == 0:
        logger.info("Crawler %s finished OK", source)
    else:
        logger.error("Crawler %s failed (exit %d); see %s", source, result.returncode, log_path)
    return result.returncode


def run_all(mode: str, sources: list[str], token: str | None, output_dir: str, chain: bool) -> int:
    """Run all specified crawlers in the correct order."""
    parallel = [s for s in PARALLEL_SOURCES if s in sources]
    sequential = [s for s in SEQUENTIAL_SOURCES if s in sources]

    errors = 0

    # Phase 1: parallel crawlers
    if parallel:
        logger.info("Phase 1: running %s in parallel", parallel)
        with ThreadPoolExecutor(max_workers=len(parallel)) as pool:
            futures = {pool.submit(_run_crawler, s, mode, token, output_dir): s for s in parallel}
            for fut in as_completed(futures):
                if fut.result() != 0:
                    errors += 1

    # Phase 2: sequential search-API crawlers
    for source in sequential:
        logger.info("Phase 2: running %s sequentially", source)
        if _run_crawler(source, mode, token, output_dir) != 0:
            errors += 1

    # Phase 3: optional chain
    if chain and errors == 0:
        logger.info("Phase 3: running pipeline chain")
        raw_files = list(Path(output_dir).glob("*.jsonl"))
        raw_paths = " ".join(str(p) for p in raw_files)
        chain_cmds = [
            f"{sys.executable} pipeline/normalize.py {raw_paths} -o data/unified_skills.jsonl",
            f"{sys.executable} pipeline/backfill_metadata.py {raw_paths}",
            f"{sys.executable} pipeline/build_index.py",
        ]
        for cmd_str in chain_cmds:
            result = subprocess.run(cmd_str.split())
            if result.returncode != 0:
                logger.error("Chain step failed: %s", cmd_str)
                errors += 1
                break

    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run all crawlers in the correct mode and order.")
    parser.add_argument("--mode", choices=["full", "incremental", "metadata", "discover"], default="incremental")
    parser.add_argument("--sources", default=",".join(ALL_SOURCES),
                        help="Comma-separated list of sources to run (default: all)")
    parser.add_argument("--token", default=None)
    parser.add_argument("--output-dir", default="data/raw", metavar="DIR")
    parser.add_argument("--chain", action="store_true", help="Run normalize/backfill/build after crawling")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    token = args.token or os.environ.get("GITHUB_TOKEN")
    sources = [s.strip() for s in args.sources.split(",") if s.strip() in ALL_SOURCES]

    if not sources:
        logger.error("No valid sources specified")
        return 1

    errors = run_all(args.mode, sources, token, args.output_dir, args.chain)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
