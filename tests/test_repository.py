from pathlib import Path

import libsql

from opportunity_sentinel.agents import DiscoveryAgent
from opportunity_sentinel.models import OpportunityType, StudentProfile
from opportunity_sentinel.repository import Repository
from opportunity_sentinel.tools import InMemoryResearchTools, SourcePage


def test_profile_opportunity_deduplication_and_delivery(
    tmp_path: Path, verified_page: SourcePage
) -> None:
    repository = Repository(tmp_path / "data.sqlite")
    profile = StudentProfile(
        telegram_id=123,
        major="Software Engineering",
        graduation_year=2027,
        preferred_types={OpportunityType.COOP},
    )
    repository.upsert_profile(profile)
    candidate = DiscoveryAgent(InMemoryResearchTools([verified_page])).extract(
        verified_page.__dict__
    )
    assert candidate is not None
    first_id = repository.save_opportunity(candidate, 0.95)
    second_id = repository.save_opportunity(candidate, 0.99)
    assert first_id == second_id
    assert repository.get_profile(123) == profile
    assert repository.list_matches(profile)[0][0] == first_id
    assert repository.was_delivered(123, first_id) is False
    repository.mark_delivered(123, first_id)
    assert repository.was_delivered(123, first_id) is True
    repository.save_for_student(123, first_id)
    assert repository.list_saved(123)[0][0] == first_id


def test_authoritative_source_reconciliation_expires_removed_inventory(
    tmp_path: Path, verified_page: SourcePage
) -> None:
    repository = Repository(tmp_path / "reconciliation.sqlite")
    candidate = DiscoveryAgent(InMemoryResearchTools([verified_page])).extract(
        verified_page.__dict__
    )
    assert candidate is not None
    identifier = repository.save_opportunity(
        candidate,
        0.99,
        source_id="ats-canonical",
    )

    assert repository.reconcile_source(
        "ats-canonical", {str(candidate.application_url)}
    ) == 0
    assert repository.reconcile_source("ats-canonical", set()) == 1

    row = repository.connection.execute(
        "SELECT status,lifecycle FROM opportunities WHERE id=?", (identifier,)
    ).fetchone()
    assert row["status"] == "expired"
    assert row["lifecycle"] == "expired"


def test_turso_backend_preserves_sqlite_row_contract(
    tmp_path: Path, monkeypatch, verified_page: SourcePage
) -> None:
    real_connect = libsql.connect
    remote_file = tmp_path / "remote-compatible.db"
    monkeypatch.setattr(
        libsql,
        "connect",
        lambda **_kwargs: real_connect(str(remote_file), _check_same_thread=False),
    )

    repository = Repository(
        tmp_path / "unused-local.db",
        database_url="libsql://nabaa-test.turso.io",
        auth_token="test-token",
    )
    profile = StudentProfile(
        telegram_id=991,
        major="هندسة البرمجيات",
        graduation_year=2027,
        preferred_types={OpportunityType.COURSE},
    )
    repository.upsert_profile(profile)

    assert repository.backend == "turso"
    assert repository.get_profile(991) == profile
    row = repository.connection.execute("SELECT 7 AS value").fetchone()
    assert row is not None
    assert dict(row) == {"value": 7}
