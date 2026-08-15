"""Report honest source coverage and optional recall against an independent gold set."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from opportunity_sentinel.config import get_settings
from opportunity_sentinel.coverage import CoverageBenchmark, evaluate_repository_coverage
from opportunity_sentinel.repository import Repository


def build_report(repository: Repository, gold_path: Path | None = None) -> dict[str, Any]:
    report = repository.coverage_snapshot()
    if gold_path is None or not gold_path.exists():
        return report
    benchmark = CoverageBenchmark.model_validate_json(gold_path.read_text(encoding="utf-8"))
    return evaluate_repository_coverage(repository, benchmark)


def fetch_production_report(
    api_url: str, secret: str, benchmark: CoverageBenchmark
) -> dict[str, Any]:
    body = benchmark.model_dump_json().encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    response = httpx.post(
        f"{api_url.rstrip('/')}/internal/coverage/evaluate",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Nabaa-Timestamp": timestamp,
            "X-Nabaa-Signature": signature,
        },
        timeout=90,
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=get_settings().data_db_path)
    parser.add_argument(
        "--gold",
        type=Path,
        default=Path("benchmarks/gold_opportunities.json"),
    )
    parser.add_argument("--api-url", default=os.getenv("NABAA_API_URL"))
    parser.add_argument("--target-recall", type=float, default=90.0)
    parser.add_argument("--max-age-days", type=int, default=8)
    parser.add_argument("--fail-below-target", action="store_true")
    args = parser.parse_args()
    benchmark = CoverageBenchmark.model_validate_json(args.gold.read_text(encoding="utf-8"))
    secret = os.getenv("INTERNAL_API_SECRET", "")
    if args.api_url:
        if not secret:
            raise SystemExit("INTERNAL_API_SECRET is required with --api-url")
        report = fetch_production_report(args.api_url, secret, benchmark)
    else:
        repository = Repository(args.db)
        try:
            report = evaluate_repository_coverage(repository, benchmark)
        finally:
            repository.connection.close()
    recall = report["recall"]
    age = int(recall.get("benchmark_age_days") or 0)
    score = recall.get("recall_percent")
    below_target = score is None or float(score) < args.target_recall
    stale = age > args.max_age_days
    recall["target_percent"] = args.target_recall
    recall["target_status"] = "below_target" if below_target else "met"
    recall["freshness_status"] = "stale" if stale else "fresh"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.api_url and below_target:
        print(
            f"::warning title=Coverage below target::Production recall is {score}% "
            f"against a target of {args.target_recall}%.",
            file=sys.stderr,
        )
    if stale:
        print(
            f"::warning title=Coverage benchmark is stale::Gold set age is {age} days; "
            f"maximum is {args.max_age_days}.",
            file=sys.stderr,
        )
    if (below_target or stale) and args.fail_below_target:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
