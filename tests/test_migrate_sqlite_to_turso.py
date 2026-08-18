from pathlib import Path

from opportunity_sentinel.models import OpportunityType, StudentProfile
from opportunity_sentinel.repository import Repository
from scripts.migrate_sqlite_to_turso import copy_database


def test_copy_database_preserves_student_profile(tmp_path: Path) -> None:
    source = Repository(tmp_path / "source.sqlite")
    destination = Repository(tmp_path / "destination.sqlite")
    profile = StudentProfile(
        telegram_id=12345,
        major="هندسة البرمجيات",
        graduation_year=2027,
        preferred_types={OpportunityType.COOP, OpportunityType.INTERNSHIP},
        accepts_online=True,
    )
    source.upsert_profile(profile)

    copied = copy_database(source.connection, destination.connection)

    assert copied["students"] == 1
    assert destination.get_profile(12345) == profile


def test_copy_database_is_idempotent(tmp_path: Path) -> None:
    source = Repository(tmp_path / "source.sqlite")
    destination = Repository(tmp_path / "destination.sqlite")

    first = copy_database(source.connection, destination.connection)
    second = copy_database(source.connection, destination.connection)

    assert second == first
    assert destination.metrics()["sources"] == source.metrics()["sources"]
