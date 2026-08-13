"""Deterministic 5,000-student matching acceptance test."""

from __future__ import annotations

import json
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

from opportunity_sentinel.models import (
    DeliveryMode,
    Evidence,
    OpportunityCandidate,
    OpportunityType,
    StudentProfile,
)
from opportunity_sentinel.repository import Repository


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nabaa-load-") as directory:
        repository = Repository(Path(directory) / "load.sqlite")
        now = date.today().isoformat()
        rows = []
        for telegram_id in range(1, 5001):
            profile = StudentProfile(
                telegram_id=telegram_id,
                major="هندسة البرمجيات",
                graduation_year=2027,
                preferred_types={OpportunityType.INTERNSHIP},
            )
            rows.append(
                (
                    telegram_id,
                    profile.major,
                    profile.graduation_year,
                    '["internship"]',
                    1,
                    now,
                    profile.model_dump_json(),
                )
            )
        repository.connection.executemany(
            """INSERT INTO students
               (telegram_id,major,graduation_year,preferred_types,accepts_online,
                updated_at,profile_json) VALUES (?,?,?,?,?,?,?)""",
            rows,
        )
        repository.connection.commit()
        deadline = date.today() + timedelta(days=15)
        url = "https://ksu.edu.sa/load-test-opportunity"
        evidence = [
            Evidence(
                field_name=field,
                value=value,
                quote=f"Official {field}: {value}",
                source_url=url,
                official_source=True,
            )
            for field, value in (
                ("organization", "KSU"),
                ("city", "الرياض"),
                ("accepted_majors", "جميع التخصصات التقنية"),
                ("deadline", deadline.isoformat()),
            )
        ]
        candidate = OpportunityCandidate(
            title="KSU load test internship",
            organization="KSU",
            opportunity_type=OpportunityType.INTERNSHIP,
            city="الرياض",
            delivery_mode=DeliveryMode.IN_PERSON,
            accepted_majors=["جميع التخصصات التقنية"],
            deadline=deadline,
            registration_open=True,
            application_url=url,
            source_url=url,
            evidence=evidence,
        )
        started = time.perf_counter()
        identifier = repository.save_opportunity(candidate, 1.0)
        elapsed = time.perf_counter() - started
        matches = repository.connection.execute(
            "SELECT COUNT(*) FROM matches WHERE opportunity_id=? AND state='eligible'",
            (identifier,),
        ).fetchone()[0]
        result = {"students": 5000, "eligible_matches": matches, "seconds": round(elapsed, 3)}
        print(json.dumps(result))
        repository.connection.close()
        if matches != 5000 or elapsed >= 60:
            raise SystemExit("5,000-student acceptance target failed")


if __name__ == "__main__":
    main()
