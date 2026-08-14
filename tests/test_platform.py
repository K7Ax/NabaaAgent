from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from opportunity_sentinel.config import Settings
from opportunity_sentinel.matching import match_opportunity
from opportunity_sentinel.models import (
    DeliveryMode,
    Evidence,
    OpportunityCandidate,
    OpportunityType,
    StudentProfile,
)
from opportunity_sentinel.repository import Repository
from opportunity_sentinel.tools import SourcePage
from scripts.coverage_report import build_report


def _candidate(**changes: object) -> OpportunityCandidate:
    deadline = date.today() + timedelta(days=10)
    source = "https://ksu.edu.sa/opportunity"
    data: dict[str, object] = {
        "title": "KSU Student Program",
        "organization": "King Saud University",
        "opportunity_type": OpportunityType.INTERNSHIP,
        "city": "الرياض",
        "delivery_mode": DeliveryMode.IN_PERSON,
        "accepted_majors": ["جميع التخصصات التقنية"],
        "deadline": deadline,
        "registration_open": True,
        "application_url": source,
        "source_url": source,
        "evidence": [
            Evidence(
                field_name=field,
                value=value,
                quote=f"Official {field}: {value}",
                source_url=source,
                official_source=True,
            )
            for field, value in (
                ("organization", "King Saud University"),
                ("city", "الرياض"),
                ("accepted_majors", "جميع التخصصات التقنية"),
                ("deadline", deadline.isoformat()),
            )
        ],
    }
    data.update(changes)
    return OpportunityCandidate.model_validate(data)


def test_additive_migration_preserves_legacy_student(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE students (telegram_id INTEGER PRIMARY KEY,major TEXT NOT NULL,
        graduation_year INTEGER NOT NULL,preferred_types TEXT NOT NULL,
        accepts_online INTEGER NOT NULL,updated_at TEXT NOT NULL)"""
    )
    connection.execute(
        "INSERT INTO students VALUES (1,'هندسة البرمجيات',2027,'[\"internship\"]',1,?)",
        (datetime.now(UTC).isoformat(),),
    )
    connection.commit()
    connection.close()

    repository = Repository(path)

    profile = repository.get_profile(1)
    assert profile is not None
    assert profile.major == "هندسة البرمجيات"
    assert repository.metrics()["sources"] >= 5


def test_central_matching_and_progressive_requirement(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "platform.sqlite")
    profile = StudentProfile(
        telegram_id=7,
        major="هندسة البرمجيات",
        graduation_year=2027,
        preferred_types={OpportunityType.INTERNSHIP},
    )
    repository.upsert_profile(profile)
    candidate = _candidate(requirements={"saudi_national": True})
    identifier = repository.save_opportunity(candidate, 1.0)

    assert repository.next_unknown_requirement(7) is None
    assert repository.list_matches(profile)[0][0] == identifier
    assert repository.due_deliveries()


def test_matcher_rejects_narrower_incompatible_major() -> None:
    profile = StudentProfile(
        telegram_id=9,
        major="هندسة البرمجيات",
        graduation_year=2027,
        preferred_types={OpportunityType.INTERNSHIP},
    )
    accepted = match_opportunity(_candidate(), profile)
    rejected = match_opportunity(_candidate(accepted_majors=["المحاسبة"]), profile)
    assert accepted.eligible is True
    assert accepted.score >= 60
    assert rejected.eligible is False


def test_matcher_keeps_proven_technical_opportunity_without_major_list() -> None:
    profile = StudentProfile(
        telegram_id=10,
        major="هندسة الحاسب",
        graduation_year=2027,
        preferred_types={OpportunityType.INTERNSHIP},
    )
    candidate = _candidate(accepted_majors=[], technical_focus=True)

    assert match_opportunity(candidate, profile).eligible is True


def test_signed_ingestion_accepts_verified_and_rejects_bad_signature(
    tmp_path: Path, monkeypatch
) -> None:
    import opportunity_sentinel.api as api_module

    secret = "test-internal-secret"
    settings = Settings(
        data_db_path=tmp_path / "api.sqlite",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite",
        internal_api_secret=secret,
        telegram_bot_token=None,
    )
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    body = json.dumps(
        {"source_id": "web-discovery", "candidates": [_candidate().model_dump(mode="json")]},
        default=str,
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()

    with TestClient(api_module.app) as client:
        denied = client.post(
            "/internal/ingest/batch",
            content=body,
            headers={"X-Nabaa-Timestamp": timestamp, "X-Nabaa-Signature": "wrong"},
        )
        accepted = client.post(
            "/internal/ingest/batch",
            content=body,
            headers={"X-Nabaa-Timestamp": timestamp, "X-Nabaa-Signature": signature},
        )
        quota_body = b'{"provider":"tavily","units":2}'
        quota_signature = hmac.new(
            secret.encode(), timestamp.encode() + b"." + quota_body, hashlib.sha256
        ).hexdigest()
        quota = client.post(
            "/internal/quota/reserve",
            content=quota_body,
            headers={
                "X-Nabaa-Timestamp": timestamp,
                "X-Nabaa-Signature": quota_signature,
            },
        )
        empty_signature = hmac.new(
            secret.encode(), timestamp.encode() + b".", hashlib.sha256
        ).hexdigest()
        sources = client.get(
            "/internal/sources",
            headers={
                "X-Nabaa-Timestamp": timestamp,
                "X-Nabaa-Signature": empty_signature,
            },
        )
        coverage = client.get(
            "/internal/coverage",
            headers={
                "X-Nabaa-Timestamp": timestamp,
                "X-Nabaa-Signature": empty_signature,
            },
        )
        no_telegram = client.post(
            "/internal/deliver",
            content=b"",
            headers={
                "X-Nabaa-Timestamp": timestamp,
                "X-Nabaa-Signature": empty_signature,
            },
        )

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["verified"] == 1
    assert quota.json() == {"granted": True}
    assert sources.status_code == 200
    assert any(item["id"] == "ksu-main" for item in sources.json())
    assert coverage.status_code == 200
    assert coverage.json()["recall"]["status"] == "not_measured"
    assert no_telegram.status_code == 503


def test_monthly_quota_fails_closed(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "quota.sqlite")
    assert repository.consume_quota("tavily", 2, 3) is True
    assert repository.consume_quota("tavily", 2, 3) is False


def test_revalidation_open_closed_and_unavailable(monkeypatch) -> None:
    import opportunity_sentinel.revalidation as module

    candidate = _candidate()

    class FakeTools:
        page: SourcePage | None = None

        def __init__(self, **_: object) -> None:
            pass

        def open_page(self, _: str):
            return self.page, SimpleNamespace(detail="network unavailable")

    monkeypatch.setattr(module, "WebResearchTools", FakeTools)
    FakeTools.page = None
    assert module.revalidate_application(candidate).state == "unavailable"
    FakeTools.page = SourcePage(
        url=str(candidate.application_url),
        title="Closed",
        content="انتهى التسجيل",
        official=True,
    )
    assert module.revalidate_application(candidate).state == "closed"
    FakeTools.page = SourcePage(
        url=str(candidate.application_url),
        title="Open",
        content="التقديم متاح",
        official=True,
    )
    assert module.revalidate_application(candidate).state == "open"
    FakeTools.page = SourcePage(
        url=str(candidate.application_url),
        title="Ambiguous",
        content="نبذة عامة عن البرنامج",
        official=True,
    )
    assert module.revalidate_application(candidate).state == "unavailable"


def test_review_queue_source_health_and_revalidation_state(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "operations.sqlite")
    candidate = _candidate()
    identifier = repository.save_opportunity(candidate, 1.0)
    review_id = repository.queue_admin_review(candidate, "missing secondary evidence")
    duplicate = repository.queue_admin_review(candidate, "missing secondary evidence")

    assert review_id is not None
    assert duplicate is None
    assert any(source["id"] == "ksu-alumni" for source in repository.source_health())
    repository.record_revalidation(identifier, "unavailable", "temporary outage")
    repository.record_revalidation(identifier, "open", "reachable")
    repository.record_revalidation(identifier, "closed", "registration closed")
    row = repository.connection.execute(
        "SELECT status FROM opportunities WHERE id=?", (identifier,)
    ).fetchone()
    assert row["status"] == "expired"


def test_source_registry_and_coverage_report_are_honest(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "coverage.sqlite")
    candidate = _candidate()
    repository.save_opportunity(candidate, 1.0)

    sources = {source["id"]: source for source in repository.source_health()}
    assert sources["future-skills"]["implementation_status"] == "active"
    assert sources["misk"]["implementation_status"] == "active"
    assert sources["misk"]["enabled"] == 1
    assert sources["ksu-main"]["implementation_status"] == "active"
    assert sources["x-signals"]["operational_status"] == "signal_only"

    for _ in range(2):
        run_id = repository.start_crawl("future-skills")
        repository.finish_crawl(run_id, 0, 0, "source unavailable")
    degraded = {source["id"]: source for source in repository.source_health()}
    assert degraded["future-skills"]["operational_status"] == "degraded"
    recovered_run = repository.start_crawl("future-skills")
    repository.finish_crawl(recovered_run, 1, 1)
    recovered = {source["id"]: source for source in repository.source_health()}
    assert recovered["future-skills"]["operational_status"] == "healthy"

    without_gold = repository.coverage_snapshot()
    assert without_gold["inventory"]["verified_by_type"]["internship"] == 1
    assert without_gold["sources"]["by_implementation"]["planned"] >= 1
    assert without_gold["recall"]["status"] == "not_measured"

    gold_path = tmp_path / "gold.json"
    gold_path.write_text(
        json.dumps(
            {
                "as_of": date.today().isoformat(),
                "opportunities": [
                    {
                        "application_url": str(candidate.application_url),
                        "source_id": "ksu-main",
                        "opportunity_type": "internship",
                        "expected_open": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    measured = build_report(repository, gold_path)
    assert measured["recall"]["status"] == "measured"
    assert measured["recall"]["recall_percent"] == 100.0
