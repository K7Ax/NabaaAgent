from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from opportunity_sentinel.models import OpportunityCandidate, OpportunityType, StudentProfile


class Repository:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._setup()

    def _setup(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS students (
                telegram_id INTEGER PRIMARY KEY,
                major TEXT NOT NULL,
                graduation_year INTEGER NOT NULL,
                preferred_types TEXT NOT NULL,
                accepts_online INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS opportunities (
                id TEXT PRIMARY KEY,
                application_url TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                verification_score REAL NOT NULL,
                status TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_verified_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saved_opportunities (
                telegram_id INTEGER NOT NULL,
                opportunity_id TEXT NOT NULL,
                saved_at TEXT NOT NULL,
                PRIMARY KEY (telegram_id, opportunity_id)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                telegram_id INTEGER NOT NULL,
                opportunity_id TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                PRIMARY KEY (telegram_id, opportunity_id)
            );
            """
        )
        self.connection.commit()

    def upsert_profile(self, profile: StudentProfile) -> None:
        self.connection.execute(
            """
            INSERT INTO students VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
              major=excluded.major,
              graduation_year=excluded.graduation_year,
              preferred_types=excluded.preferred_types,
              accepts_online=excluded.accepts_online,
              updated_at=excluded.updated_at
            """,
            (
                profile.telegram_id,
                profile.major,
                profile.graduation_year,
                json.dumps(sorted(item.value for item in profile.preferred_types)),
                int(profile.accepts_online),
                _now(),
            ),
        )
        self.connection.commit()

    def get_profile(self, telegram_id: int) -> StudentProfile | None:
        row = self.connection.execute(
            "SELECT * FROM students WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if not row:
            return None
        return StudentProfile(
            telegram_id=row["telegram_id"],
            major=row["major"],
            graduation_year=row["graduation_year"],
            preferred_types={OpportunityType(item) for item in json.loads(row["preferred_types"])},
            accepts_online=bool(row["accepts_online"]),
        )

    def list_profiles(self) -> list[StudentProfile]:
        rows = self.connection.execute("SELECT telegram_id FROM students").fetchall()
        return [profile for row in rows if (profile := self.get_profile(row["telegram_id"]))]

    def save_opportunity(
        self, candidate: OpportunityCandidate, score: float, status: str = "verified"
    ) -> str:
        identifier = hashlib.sha256(str(candidate.application_url).encode()).hexdigest()[:16]
        now = _now()
        self.connection.execute(
            """
            INSERT INTO opportunities VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(application_url) DO UPDATE SET
              payload=excluded.payload,
              verification_score=excluded.verification_score,
              status=excluded.status,
              last_verified_at=excluded.last_verified_at
            """,
            (
                identifier,
                str(candidate.application_url),
                candidate.model_dump_json(),
                score,
                status,
                now,
                now,
            ),
        )
        self.connection.commit()
        return identifier

    def get_opportunity(self, identifier: str) -> OpportunityCandidate | None:
        row = self.connection.execute(
            "SELECT payload FROM opportunities WHERE id = ?", (identifier,)
        ).fetchone()
        return OpportunityCandidate.model_validate_json(row["payload"]) if row else None

    def list_matches(
        self, profile: StudentProfile, limit: int = 10
    ) -> list[tuple[str, OpportunityCandidate]]:
        rows = self.connection.execute(
            "SELECT id, payload FROM opportunities WHERE status = 'verified' "
            "ORDER BY last_verified_at DESC"
        ).fetchall()
        matches: list[tuple[str, OpportunityCandidate]] = []
        for row in rows:
            candidate = OpportunityCandidate.model_validate_json(row["payload"])
            if (
                profile.preferred_types
                and candidate.opportunity_type not in profile.preferred_types
            ):
                continue
            accepted = {major.casefold() for major in candidate.accepted_majors}
            broad = {"all technical majors", "technical majors", "جميع التخصصات التقنية"}
            broad.update({"all majors", "all disciplines", "جميع التخصصات"})
            if (
                accepted
                and profile.major.casefold() not in accepted
                and not accepted.intersection(broad)
            ):
                continue
            if not profile.accepts_online and candidate.delivery_mode.value == "online":
                continue
            matches.append((row["id"], candidate))
            if len(matches) >= limit:
                break
        return matches

    def save_for_student(self, telegram_id: int, opportunity_id: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO saved_opportunities VALUES (?, ?, ?)",
            (telegram_id, opportunity_id, _now()),
        )
        self.connection.commit()

    def was_delivered(self, telegram_id: int, opportunity_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM deliveries WHERE telegram_id = ? AND opportunity_id = ?",
                (telegram_id, opportunity_id),
            ).fetchone()
            is not None
        )

    def mark_delivered(self, telegram_id: int, opportunity_id: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?)",
            (telegram_id, opportunity_id, _now()),
        )
        self.connection.commit()

    def list_saved(self, telegram_id: int) -> list[tuple[str, OpportunityCandidate]]:
        rows = self.connection.execute(
            """
            SELECT o.id, o.payload FROM opportunities o
            JOIN saved_opportunities s ON s.opportunity_id = o.id
            WHERE s.telegram_id = ? ORDER BY s.saved_at DESC
            """,
            (telegram_id,),
        ).fetchall()
        return [
            (row["id"], OpportunityCandidate.model_validate_json(row["payload"]))
            for row in rows
        ]


def _now() -> str:
    return datetime.now(UTC).isoformat()
