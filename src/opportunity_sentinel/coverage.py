"""Independent benchmark models and production recall measurement."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

from opportunity_sentinel.models import OpportunityType
from opportunity_sentinel.repository import Repository

RIYADH = timezone(timedelta(hours=3), name="Asia/Riyadh")


class ReviewedSource(BaseModel):
    source_id: str = Field(pattern=r"^[a-z0-9_-]{2,60}$")
    catalogue_url: HttpUrl
    review_status: Literal["complete", "blocked"]
    notes: str | None = Field(default=None, max_length=500)


class GoldOpportunity(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    application_url: HttpUrl
    source_id: str = Field(pattern=r"^[a-z0-9_-]{2,60}$")
    opportunity_type: OpportunityType
    expected_open: bool = True
    evidence_quote: str = Field(min_length=3, max_length=500)
    reviewed_at: date


class CoverageBenchmark(BaseModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    as_of: date
    scope: str = Field(min_length=10, max_length=1000)
    method: str = Field(min_length=10, max_length=1000)
    reviewed_sources: list[ReviewedSource] = Field(min_length=1, max_length=200)
    opportunities: list[GoldOpportunity] = Field(max_length=2000)

    @model_validator(mode="after")
    def validate_independent_audit(self) -> CoverageBenchmark:
        if self.as_of > _riyadh_today():
            raise ValueError("benchmark date cannot be in the future")
        source_ids = [item.source_id for item in self.reviewed_sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("reviewed source IDs must be unique")
        source_status = {item.source_id: item.review_status for item in self.reviewed_sources}
        urls = [str(item.application_url).strip().rstrip("/") for item in self.opportunities]
        if len(urls) != len(set(urls)):
            raise ValueError("gold opportunity URLs must be unique")
        for item in self.opportunities:
            if source_status.get(item.source_id) != "complete":
                raise ValueError("gold opportunities require a completely reviewed source")
            if item.reviewed_at > self.as_of:
                raise ValueError("opportunity review date cannot be after benchmark date")
        return self


def evaluate_repository_coverage(
    repository: Repository, benchmark: CoverageBenchmark
) -> dict[str, Any]:
    """Compare an independent benchmark with verified production inventory."""
    report = repository.coverage_snapshot()
    verified_urls = {
        str(row["application_url"]).strip().rstrip("/")
        for row in repository.connection.execute(
            "SELECT application_url FROM opportunities WHERE status='verified'"
        ).fetchall()
    }
    expected = [item for item in benchmark.opportunities if item.expected_open]
    missed = [
        item
        for item in expected
        if str(item.application_url).strip().rstrip("/") not in verified_urls
    ]
    detected = len(expected) - len(missed)
    complete_sources = [
        item.source_id for item in benchmark.reviewed_sources if item.review_status == "complete"
    ]
    blocked_sources = [
        item.source_id for item in benchmark.reviewed_sources if item.review_status == "blocked"
    ]
    by_source: dict[str, dict[str, int | float | str | None]] = {}
    for source_id in complete_sources:
        source_expected = [item for item in expected if item.source_id == source_id]
        source_missed = [item for item in missed if item.source_id == source_id]
        source_detected = len(source_expected) - len(source_missed)
        by_source[source_id] = {
            "status": "measured" if source_expected else "no_open_opportunities",
            "expected_open": len(source_expected),
            "detected": source_detected,
            "missed": len(source_missed),
            "recall_percent": (
                round(100 * source_detected / len(source_expected), 2)
                if source_expected
                else None
            ),
        }
    report["recall"] = {
        "status": "measured" if expected else "not_measured",
        "gold_as_of": benchmark.as_of.isoformat(),
        "benchmark_age_days": (_riyadh_today() - benchmark.as_of).days,
        "scope": benchmark.scope,
        "reviewed_sources": complete_sources,
        "blocked_sources": blocked_sources,
        "expected_open": len(expected),
        "detected": detected,
        "missed": [item.model_dump(mode="json") for item in missed],
        "recall_percent": round(100 * detected / len(expected), 2) if expected else None,
        "by_source": by_source,
    }
    return report


def _riyadh_today() -> date:
    return datetime.now(RIYADH).date()
