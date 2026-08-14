"""Report honest source coverage and optional recall against an independent gold set."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from opportunity_sentinel.config import get_settings
from opportunity_sentinel.repository import Repository


def build_report(repository: Repository, gold_path: Path | None = None) -> dict[str, Any]:
    report = repository.coverage_snapshot()
    if gold_path is None or not gold_path.exists():
        return report
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    expected = [item for item in gold.get("opportunities", []) if item.get("expected_open", True)]
    if not expected:
        return report
    verified_urls = {
        str(row["application_url"]).rstrip("/")
        for row in repository.connection.execute(
            "SELECT application_url FROM opportunities WHERE status='verified'"
        ).fetchall()
    }
    missed = [
        item
        for item in expected
        if str(item.get("application_url", "")).strip().rstrip("/") not in verified_urls
    ]
    detected = len(expected) - len(missed)
    report["recall"] = {
        "status": "measured",
        "gold_as_of": gold.get("as_of"),
        "expected_open": len(expected),
        "detected": detected,
        "missed": missed,
        "recall_percent": round(100 * detected / len(expected), 2),
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=get_settings().data_db_path)
    parser.add_argument(
        "--gold",
        type=Path,
        default=Path("benchmarks/gold_opportunities.json"),
    )
    args = parser.parse_args()
    repository = Repository(args.db)
    try:
        report = build_report(repository, args.gold)
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        repository.connection.close()


if __name__ == "__main__":
    main()
