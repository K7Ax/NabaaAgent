from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from opportunity_sentinel.models import (
    OpportunityCandidate,
    OpportunityLifecycle,
    OpportunityType,
    StudentProfile,
)
from opportunity_sentinel.taxonomy import KSU_TAXONOMY, KSU_TAXONOMY_VERSION


class DatabaseRow:
    """Small sqlite3.Row-compatible value used by the remote libSQL driver."""

    def __init__(self, columns: Sequence[str], values: Sequence[Any]) -> None:
        self._columns = tuple(columns)
        self._values = tuple(values)
        self._index = {name: index for index, name in enumerate(self._columns)}

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> list[str]:
        return list(self._columns)


class MappingCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self._columns = tuple(item[0] for item in (cursor.description or ()))

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def _row(self, value: Sequence[Any] | None) -> DatabaseRow | None:
        return DatabaseRow(self._columns, value) if value is not None else None

    def fetchone(self) -> DatabaseRow | None:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[DatabaseRow]:
        return [DatabaseRow(self._columns, row) for row in self._cursor.fetchall()]

    def __iter__(self) -> Iterator[DatabaseRow]:
        for row in self._cursor.fetchall():
            yield DatabaseRow(self._columns, row)


class MappingConnection:
    """Normalize libSQL tuple rows to the mapping API used by Repository."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> MappingCursor:
        return MappingCursor(self._connection.execute(sql, parameters))

    def executemany(
        self, sql: str, parameters: Iterable[Sequence[Any]]
    ) -> MappingCursor:
        return MappingCursor(self._connection.executemany(sql, parameters))

    def executescript(self, sql: str) -> None:
        self._connection.executescript(sql)

    def commit(self) -> None:
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()


class Repository:
    """SQLite system of record with additive migrations for the original capstone DB."""

    def __init__(
        self,
        path: Path,
        database_url: str | None = None,
        auth_token: str | None = None,
    ) -> None:
        if database_url and database_url.startswith("libsql://"):
            if not auth_token:
                raise RuntimeError("TURSO_AUTH_TOKEN is required with a remote DATABASE_URL")
            try:
                import libsql
            except ImportError as exc:  # pragma: no cover - deployment dependency guard
                raise RuntimeError("Install the libsql package for remote DATABASE_URL") from exc
            raw_connection = libsql.connect(
                database=database_url,
                auth_token=auth_token,
                timeout=30,
                _check_same_thread=False,
            )
            self.connection: Any = MappingConnection(raw_connection)
            self.backend = "turso"
        else:
            connection = sqlite3.connect(path, check_same_thread=False, timeout=30)
            connection.row_factory = sqlite3.Row
            self.connection = connection
            self.backend = "sqlite"
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._setup()

    @classmethod
    def from_settings(cls, settings: Any) -> Repository:
        return cls(
            settings.data_db_path,
            database_url=settings.database_url,
            auth_token=settings.turso_auth_token,
        )

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
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL UNIQUE,
                adapter_type TEXT NOT NULL,
                trust_tier TEXT NOT NULL CHECK (trust_tier IN ('A','B','C','D')),
                categories TEXT NOT NULL DEFAULT '[]',
                cadence_minutes INTEGER NOT NULL DEFAULT 1440,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_success_at TEXT,
                last_error_at TEXT,
                last_error TEXT,
                content_hash TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS crawl_runs (
                id TEXT PRIMARY KEY,
                source_id TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                discovered_count INTEGER NOT NULL DEFAULT 0,
                verified_count INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                FOREIGN KEY(source_id) REFERENCES sources(id)
            );
            CREATE TABLE IF NOT EXISTS raw_signals (
                id TEXT PRIMARY KEY,
                source_id TEXT,
                source_url TEXT NOT NULL,
                title TEXT NOT NULL,
                payload TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                discovered_at TEXT NOT NULL,
                processed_at TEXT,
                disposition TEXT NOT NULL DEFAULT 'signal',
                UNIQUE(source_url, content_hash)
            );
            CREATE TABLE IF NOT EXISTS opportunity_versions (
                opportunity_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                payload TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                PRIMARY KEY(opportunity_id, version)
            );
            CREATE TABLE IF NOT EXISTS opportunity_sources (
                opportunity_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(opportunity_id, source_id),
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id),
                FOREIGN KEY(source_id) REFERENCES sources(id)
            );
            CREATE TABLE IF NOT EXISTS evidence_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id TEXT NOT NULL,
                field_name TEXT NOT NULL,
                value TEXT NOT NULL,
                quote TEXT NOT NULL,
                source_url TEXT NOT NULL,
                official_source INTEGER NOT NULL,
                captured_at TEXT NOT NULL,
                UNIQUE(opportunity_id, field_name, source_url, quote)
            );
            CREATE TABLE IF NOT EXISTS matches (
                telegram_id INTEGER NOT NULL,
                opportunity_id TEXT NOT NULL,
                score INTEGER NOT NULL,
                reasons TEXT NOT NULL,
                unknown_requirements TEXT NOT NULL DEFAULT '[]',
                state TEXT NOT NULL,
                calculated_at TEXT NOT NULL,
                PRIMARY KEY(telegram_id, opportunity_id)
            );
            CREATE TABLE IF NOT EXISTS delivery_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                opportunity_id TEXT NOT NULL,
                delivery_kind TEXT NOT NULL,
                due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(telegram_id, opportunity_id, delivery_kind)
            );
            CREATE TABLE IF NOT EXISTS admin_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id TEXT,
                reason TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                resolution TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS quota_usage (
                provider TEXT NOT NULL,
                usage_date TEXT NOT NULL,
                units INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(provider, usage_date)
            );
            CREATE TABLE IF NOT EXISTS health_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                component TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS taxonomy (
                version TEXT NOT NULL,
                university TEXT NOT NULL,
                college TEXT NOT NULL,
                major TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT '[]',
                active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(version, university, college, major)
            );
            CREATE TABLE IF NOT EXISTS profile_answers (
                telegram_id INTEGER NOT NULL,
                requirement_key TEXT NOT NULL,
                answer_json TEXT NOT NULL,
                answered_at TEXT NOT NULL,
                PRIMARY KEY(telegram_id, requirement_key)
            );
            CREATE TABLE IF NOT EXISTS digests (
                telegram_id INTEGER NOT NULL,
                digest_date TEXT NOT NULL,
                opportunity_ids TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                PRIMARY KEY(telegram_id, digest_date)
            );
            CREATE INDEX IF NOT EXISTS idx_opportunities_status_verified
              ON opportunities(status, last_verified_at);
            CREATE INDEX IF NOT EXISTS idx_queue_due ON delivery_queue(status, due_at);
            CREATE INDEX IF NOT EXISTS idx_signals_disposition ON raw_signals(disposition);
            CREATE INDEX IF NOT EXISTS idx_opportunity_sources_active
              ON opportunity_sources(source_id, active);
            """
        )
        self._add_column("students", "profile_json", "TEXT")
        self._add_column("opportunities", "lifecycle", "TEXT NOT NULL DEFAULT 'verified_open'")
        self._add_column("opportunities", "content_hash", "TEXT")
        self._add_column("opportunities", "version", "INTEGER NOT NULL DEFAULT 1")
        self._add_column("admin_reviews", "candidate_json", "TEXT")
        self._add_column("admin_reviews", "fingerprint", "TEXT")
        self._add_column(
            "sources", "implementation_status", "TEXT NOT NULL DEFAULT 'planned'"
        )
        self._add_column("sources", "coverage_notes", "TEXT")
        self.connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_review_fingerprint
               ON admin_reviews(fingerprint) WHERE fingerprint IS NOT NULL"""
        )
        self._seed_sources()
        self._backfill_source_ownership()
        self._seed_taxonomy()
        self._audit_legacy_integrity()
        self.connection.commit()

    def _add_column(self, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _seed_sources(self) -> None:
        seeds = [
            (
                "ksu-admission",
                "جامعة الملك سعود - دليل البرامج",
                "https://dar.ksu.edu.sa",
                "html",
                "A",
            ),
            (
                "ksu-alumni",
                "جامعة الملك سعود - مركز الخريجين",
                "https://alumni.ksu.edu.sa",
                "html",
                "A",
            ),
            (
                "ksu-alumni-gate",
                "جامعة الملك سعود - بوابة الخريجين والوظائف",
                "https://alumnigate.ksu.edu.sa",
                "html_catalogue",
                "A",
            ),
            (
                "ksu-main",
                "جامعة الملك سعود - الأخبار والفعاليات",
                "https://ksu.edu.sa",
                "sitemap",
                "A",
            ),
            (
                "ksu-alumni-coop",
                "جامعة الملك سعود - وحدة التدريب التعاوني",
                "https://alumni.ksu.edu.sa/ar/node/2668",
                "html",
                "A",
            ),
            (
                "ksu-cba-coop",
                "كلية إدارة الأعمال - التدريب التعاوني",
                "https://cba.ksu.edu.sa/ar/Registration-Steps",
                "html",
                "A",
            ),
            ("tuwaiq", "أكاديمية طويق", "https://tuwaiq.edu.sa", "json_api", "A"),
            (
                "future-skills",
                "مهارات المستقبل - وزارة الاتصالات",
                "https://futureskills.mcit.gov.sa",
                "html_catalogue",
                "A",
            ),
            (
                "financial-academy",
                "الأكاديمية المالية",
                "https://fa.gov.sa",
                "html_catalogue",
                "A",
            ),
            (
                "public-ats",
                "لوحات التوظيف الرسمية للشركات",
                "https://jobs.ashbyhq.com",
                "public_json_api",
                "A",
            ),
            (
                "ats-sarjai",
                "وظائف Sarj.ai الرسمية",
                "https://jobs.ashbyhq.com/sarjai",
                "public_json_api",
                "A",
            ),
            (
                "ats-leantech",
                "وظائف Lean Technologies الرسمية",
                "https://jobs.ashbyhq.com/LeanTech",
                "public_json_api",
                "A",
            ),
            (
                "ats-trendyol",
                "وظائف Trendyol الرسمية",
                "https://jobs.lever.co/trendyol",
                "public_json_api",
                "A",
            ),
            (
                "ats-infinitepl",
                "وظائف Infinite PL الرسمية",
                "https://jobs.lever.co/infinitepl",
                "public_json_api",
                "A",
            ),
            (
                "ats-tamara",
                "وظائف Tamara الرسمية",
                "https://job-boards.greenhouse.io/tamara",
                "public_json_api",
                "A",
            ),
            (
                "ats-hala",
                "وظائف HALA الرسمية",
                "https://job-boards.greenhouse.io/hala",
                "public_json_api",
                "A",
            ),
            (
                "ats-tsmg",
                "وظائف TSMG الرسمية",
                "https://jobs.lever.co/tsmg",
                "public_json_api",
                "A",
            ),
            (
                "ats-canonical",
                "وظائف Canonical الرسمية",
                "https://job-boards.greenhouse.io/canonicaljobs",
                "public_json_api",
                "A",
            ),
            (
                "ats-careem",
                "وظائف Careem الرسمية",
                "https://job-boards.greenhouse.io/careem",
                "public_json_api",
                "A",
            ),
            ("misk", "مؤسسة مسك", "https://hub.misk.org.sa", "html", "A"),
            ("futurex", "FutureX", "https://futurex.sa", "html", "A"),
            ("mcit", "وزارة الاتصالات", "https://mcit.gov.sa", "html", "A"),
            ("sdaia", "سدايا", "https://sdaia.gov.sa", "html", "A"),
            ("kacst", "كاكست", "https://kacst.gov.sa", "html", "A"),
            (
                "monshaat-academy",
                "أكاديمية منشآت",
                "https://academy.monshaat.gov.sa",
                "html_catalogue",
                "A",
            ),
            ("dga", "هيئة الحكومة الرقمية", "https://dga.gov.sa", "html", "A"),
            ("web-discovery", "البحث المركزي الدوري", "https://search.nabaa.local", "search", "C"),
            (
                "linkedin-signals",
                "LinkedIn public signals",
                "https://linkedin.com",
                "search_signal",
                "B",
            ),
            ("x-signals", "X official-account signals", "https://x.com", "search_signal", "B"),
            (
                "official-connectors",
                "الموصلات الرسمية السريعة",
                "https://connectors.nabaa.local",
                "connector_batch",
                "A",
            ),
            (
                "revalidation",
                "إعادة التحقق من الروابط والمواعيد",
                "https://revalidation.nabaa.local",
                "lifecycle_job",
                "A",
            ),
        ]
        now = _now()
        self.connection.executemany(
            """INSERT INTO sources
               (id,name,base_url,adapter_type,trust_tier,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
               base_url=excluded.base_url,adapter_type=excluded.adapter_type,
               trust_tier=excluded.trust_tier,updated_at=excluded.updated_at""",
            [(*seed, now, now) for seed in seeds],
        )
        source_states = {
            "tuwaiq": (
                "active",
                "Official API with official-channel and zero-cost page-index fallbacks",
            ),
            "future-skills": ("active", "Deterministic official catalogue connector"),
            "financial-academy": ("active", "Single official campaign connector"),
            "misk": ("active", "Deterministic official programs catalogue connector"),
            "monshaat-academy": (
                "active",
                "Official technical-category catalogue with per-course registration checks",
            ),
            "ksu-main": ("active", "Official news feed opportunity connector"),
            "ksu-alumni-gate": ("active", "Official public jobs catalogue connector"),
            "public-ats": ("meta", "Aggregate label for curated employer boards"),
            "ats-sarjai": ("active", "Independent official Ashby board"),
            "ats-leantech": ("active", "Independent official Ashby board"),
            "ats-trendyol": ("active", "Independent official Lever board"),
            "ats-infinitepl": ("active", "Independent official Lever board"),
            "ats-tamara": ("active", "Independent official Greenhouse board"),
            "ats-hala": ("active", "Independent official Greenhouse board"),
            "ats-tsmg": ("active", "Independent official Lever board"),
            "ats-canonical": ("active", "Independent official Greenhouse board"),
            "ats-careem": ("active", "Independent official Greenhouse board"),
            "web-discovery": ("active", "Bounded Tavily/DDGS category discovery"),
            "linkedin-signals": ("signal_only", "Indexed public signal; not a direct API"),
            "x-signals": ("signal_only", "Indexed public signal; not a direct API"),
            "official-connectors": ("meta", "Aggregate health for the fast collector"),
            "revalidation": ("meta", "Daily lifecycle and application-link checks"),
        }
        for source_id, (implementation_status, notes) in source_states.items():
            self.connection.execute(
                """UPDATE sources SET implementation_status=?,coverage_notes=?,enabled=1,
                   updated_at=? WHERE id=?""",
                (implementation_status, notes, now, source_id),
            )
        active_ids = tuple(source_states)
        placeholders = ",".join("?" for _ in active_ids)
        self.connection.execute(
            f"""UPDATE sources SET implementation_status='planned',enabled=0,
                coverage_notes=COALESCE(coverage_notes,'Connector not implemented yet'),
                updated_at=? WHERE id NOT IN ({placeholders})""",
            (now, *active_ids),
        )
        self.connection.execute(
            """DELETE FROM sources WHERE id='monshaat'
               AND NOT EXISTS (SELECT 1 FROM crawl_runs WHERE source_id='monshaat')
               AND NOT EXISTS (
                 SELECT 1 FROM opportunity_sources WHERE source_id='monshaat'
               )"""
        )

    def _seed_taxonomy(self) -> None:
        rows = [
            (KSU_TAXONOMY_VERSION, "King Saud University", college, major)
            for college, majors in KSU_TAXONOMY.items()
            for major in majors
        ]
        self.connection.executemany(
            """INSERT OR IGNORE INTO taxonomy
               (version,university,college,major) VALUES (?,?,?,?)""",
            rows,
        )

    def _audit_legacy_integrity(self) -> None:
        """Withhold records created by the historical multiline major parser bug."""
        rows = self.connection.execute(
            "SELECT id,payload FROM opportunities WHERE status='verified'"
        ).fetchall()
        invalid_prefixes = ("deadline:", "apply:", "registration_status:")
        for row in rows:
            candidate = OpportunityCandidate.model_validate_json(row["payload"])
            if any(
                major.casefold().startswith(invalid_prefixes) for major in candidate.accepted_majors
            ):
                self.connection.execute(
                    "UPDATE opportunities SET status='rejected',lifecycle='rejected' WHERE id=?",
                    (row["id"],),
                )
                self.connection.execute(
                    """UPDATE delivery_queue SET status='cancelled'
                       WHERE opportunity_id=? AND status='pending'""",
                    (row["id"],),
                )

    def _backfill_source_ownership(self) -> None:
        """Attach pre-migration opportunities to deterministic connector identities."""
        organization_sources = {
            "Sarj.ai": "ats-sarjai",
            "Lean Technologies": "ats-leantech",
            "Trendyol": "ats-trendyol",
            "Infinite PL": "ats-infinitepl",
            "Tamara": "ats-tamara",
            "HALA": "ats-hala",
            "TSMG": "ats-tsmg",
            "Canonical": "ats-canonical",
            "Careem": "ats-careem",
            "أكاديمية منشآت": "monshaat-academy",
            "مؤسسة مسك": "misk",
        }
        rows = self.connection.execute(
            """SELECT o.id,o.payload FROM opportunities o
               WHERE NOT EXISTS (
                 SELECT 1 FROM opportunity_sources os WHERE os.opportunity_id=o.id
               )"""
        ).fetchall()
        now = _now()
        for row in rows:
            candidate = OpportunityCandidate.model_validate_json(row["payload"])
            source_id = organization_sources.get(candidate.organization)
            if source_id:
                self.connection.execute(
                    """INSERT OR IGNORE INTO opportunity_sources
                       (opportunity_id,source_id,last_seen_at,active) VALUES (?,?,?,1)""",
                    (row["id"], source_id, now),
                )

    def upsert_profile(self, profile: StudentProfile) -> None:
        self.connection.execute(
            """INSERT INTO students
               (telegram_id,major,graduation_year,preferred_types,accepts_online,updated_at,profile_json)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                 major=excluded.major, graduation_year=excluded.graduation_year,
                 preferred_types=excluded.preferred_types, accepts_online=excluded.accepts_online,
                 updated_at=excluded.updated_at, profile_json=excluded.profile_json""",
            (
                profile.telegram_id,
                profile.major,
                profile.graduation_year,
                json.dumps(sorted(item.value for item in profile.preferred_types)),
                int(profile.accepts_online),
                _now(),
                profile.model_dump_json(),
            ),
        )
        self.connection.commit()

    def get_profile(self, telegram_id: int) -> StudentProfile | None:
        row = self.connection.execute(
            "SELECT * FROM students WHERE telegram_id=?", (telegram_id,)
        ).fetchone()
        if not row:
            return None
        if row["profile_json"]:
            return StudentProfile.model_validate_json(row["profile_json"])
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
        self,
        candidate: OpportunityCandidate,
        score: float,
        status: str = "verified",
        source_id: str | None = None,
    ) -> str:
        canonical_url = _canonical_url(str(candidate.application_url))
        identifier = hashlib.sha256(canonical_url.encode()).hexdigest()[:16]
        payload = candidate.model_dump_json()
        content_hash = hashlib.sha256(payload.encode()).hexdigest()
        existing = self.connection.execute(
            """SELECT id,content_hash,version,first_seen_at FROM opportunities
               WHERE application_url=?""",
            (canonical_url,),
        ).fetchone()
        now = _now()
        lifecycle = _lifecycle(candidate, status)
        version = int(existing["version"] or 1) if existing else 1
        if existing and existing["content_hash"] != content_hash:
            version += 1
        self.connection.execute(
            """INSERT INTO opportunities
               (id,application_url,payload,verification_score,status,first_seen_at,
                last_verified_at,lifecycle,content_hash,version)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(application_url) DO UPDATE SET payload=excluded.payload,
                verification_score=excluded.verification_score,status=excluded.status,
                last_verified_at=excluded.last_verified_at,lifecycle=excluded.lifecycle,
                content_hash=excluded.content_hash,version=excluded.version""",
            (
                identifier,
                canonical_url,
                payload,
                score,
                status,
                now,
                now,
                lifecycle.value,
                content_hash,
                version,
            ),
        )
        if not existing or existing["content_hash"] != content_hash:
            self.connection.execute(
                "INSERT OR IGNORE INTO opportunity_versions VALUES (?,?,?,?,?)",
                (identifier, version, payload, content_hash, now),
            )
        for evidence in candidate.evidence:
            self.connection.execute(
                """INSERT OR IGNORE INTO evidence_records
                   (opportunity_id,field_name,value,quote,source_url,official_source,captured_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    identifier,
                    evidence.field_name,
                    evidence.value,
                    evidence.quote,
                    str(evidence.source_url),
                    int(evidence.official_source),
                    evidence.captured_at.isoformat(),
                ),
            )
        if source_id:
            self.link_opportunity_source(identifier, source_id)
        self.connection.commit()
        if status == "verified":
            self.recompute_matches(identifier)
        return identifier

    def link_opportunity_source(self, opportunity_id: str, source_id: str) -> None:
        self.connection.execute(
            """INSERT INTO opportunity_sources
               (opportunity_id,source_id,last_seen_at,active) VALUES (?,?,?,1)
               ON CONFLICT(opportunity_id,source_id) DO UPDATE SET
               last_seen_at=excluded.last_seen_at,active=1""",
            (opportunity_id, source_id, _now()),
        )

    def reconcile_source(self, source_id: str, current_application_urls: set[str]) -> int:
        """Expire records removed by a successful authoritative connector crawl."""
        canonical_urls = {_canonical_url(url) for url in current_application_urls}
        self.connection.execute(
            "UPDATE opportunity_sources SET active=0 WHERE source_id=?", (source_id,)
        )
        if canonical_urls:
            placeholders = ",".join("?" for _ in canonical_urls)
            self.connection.execute(
                f"""UPDATE opportunity_sources SET active=1,last_seen_at=?
                    WHERE source_id=? AND opportunity_id IN (
                      SELECT id FROM opportunities WHERE application_url IN ({placeholders})
                    )""",
                (_now(), source_id, *canonical_urls),
            )
        stale_rows = self.connection.execute(
            """SELECT os.opportunity_id FROM opportunity_sources os
               JOIN opportunities o ON o.id=os.opportunity_id
               WHERE os.source_id=? AND os.active=0 AND o.status='verified'
               AND NOT EXISTS (
                 SELECT 1 FROM opportunity_sources other
                 WHERE other.opportunity_id=os.opportunity_id AND other.active=1
               )""",
            (source_id,),
        ).fetchall()
        stale_ids = [str(row["opportunity_id"]) for row in stale_rows]
        if stale_ids:
            placeholders = ",".join("?" for _ in stale_ids)
            self.connection.execute(
                f"""UPDATE opportunities SET status='expired',lifecycle='expired'
                    WHERE id IN ({placeholders}) AND status='verified'""",
                stale_ids,
            )
            self.connection.execute(
                f"""UPDATE delivery_queue SET status='cancelled'
                    WHERE opportunity_id IN ({placeholders}) AND status='pending'""",
                stale_ids,
            )
        self.connection.commit()
        return len(stale_ids)

    def get_opportunity(self, identifier: str) -> OpportunityCandidate | None:
        row = self.connection.execute(
            "SELECT payload FROM opportunities WHERE id=?", (identifier,)
        ).fetchone()
        return OpportunityCandidate.model_validate_json(row["payload"]) if row else None

    def list_matches(
        self, profile: StudentProfile, limit: int = 10
    ) -> list[tuple[str, OpportunityCandidate]]:
        self.recompute_matches_for_profile(profile)
        rows = self.connection.execute(
            """SELECT o.id,o.payload FROM matches m JOIN opportunities o ON o.id=m.opportunity_id
               WHERE m.telegram_id=? AND m.state='eligible' AND o.status='verified'
               ORDER BY m.score DESC,o.last_verified_at DESC LIMIT ?""",
            (profile.telegram_id, limit),
        ).fetchall()
        return [
            (row["id"], OpportunityCandidate.model_validate_json(row["payload"])) for row in rows
        ]

    def list_scored_matches(
        self, profile: StudentProfile, limit: int = 10
    ) -> list[tuple[str, OpportunityCandidate, int, list[str]]]:
        self.recompute_matches_for_profile(profile)
        rows = self.connection.execute(
            """SELECT o.id,o.payload,m.score,m.reasons FROM matches m
               JOIN opportunities o ON o.id=m.opportunity_id
               WHERE m.telegram_id=? AND m.state='eligible' AND o.status='verified'
               ORDER BY m.score DESC,o.last_verified_at DESC LIMIT ?""",
            (profile.telegram_id, limit),
        ).fetchall()
        return [
            (
                row["id"],
                OpportunityCandidate.model_validate_json(row["payload"]),
                row["score"],
                json.loads(row["reasons"]),
            )
            for row in rows
        ]

    def recompute_matches(self, opportunity_id: str) -> int:
        candidate = self.get_opportunity(opportunity_id)
        if not candidate:
            return 0
        count = 0
        for profile in self.list_profiles():
            self._store_match(profile, opportunity_id, candidate)
            count += 1
        self.connection.commit()
        return count

    def list_verified_candidates(self, limit: int = 200) -> list[OpportunityCandidate]:
        """Return verified opportunities, newest first, for the retrieval corpus."""
        rows = self.connection.execute(
            """SELECT payload FROM opportunities WHERE status='verified'
               ORDER BY last_verified_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [OpportunityCandidate.model_validate_json(row["payload"]) for row in rows]

    def recompute_matches_for_profile(self, profile: StudentProfile) -> None:
        rows = self.connection.execute(
            "SELECT id,payload FROM opportunities WHERE status='verified'"
        ).fetchall()
        for row in rows:
            self._store_match(
                profile, row["id"], OpportunityCandidate.model_validate_json(row["payload"])
            )
        self.connection.commit()

    def next_unknown_requirement(self, telegram_id: int) -> str | None:
        rows = self.connection.execute(
            """SELECT unknown_requirements FROM matches WHERE telegram_id=?
               AND state='needs_confirmation' ORDER BY calculated_at DESC""",
            (telegram_id,),
        ).fetchall()
        for row in rows:
            values = json.loads(row["unknown_requirements"])
            if values:
                return str(values[0])
        return None

    def save_profile_answer(self, telegram_id: int, key: str, value: str | bool | float) -> bool:
        profile = self.get_profile(telegram_id)
        if not profile:
            return False
        profile.profile_answers[key] = value
        self.connection.execute(
            """INSERT INTO profile_answers VALUES (?,?,?,?)
               ON CONFLICT(telegram_id,requirement_key) DO UPDATE SET
               answer_json=excluded.answer_json,answered_at=excluded.answered_at""",
            (telegram_id, key, json.dumps(value, ensure_ascii=False), _now()),
        )
        self.upsert_profile(profile)
        self.recompute_matches_for_profile(profile)
        return True

    def dismiss_match(self, telegram_id: int, opportunity_id: str) -> None:
        self.connection.execute(
            "UPDATE matches SET state='dismissed' WHERE telegram_id=? AND opportunity_id=?",
            (telegram_id, opportunity_id),
        )
        self.connection.execute(
            """UPDATE delivery_queue SET status='cancelled' WHERE telegram_id=?
               AND opportunity_id=? AND status='pending'""",
            (telegram_id, opportunity_id),
        )
        self.connection.commit()

    def _store_match(
        self, profile: StudentProfile, opportunity_id: str, candidate: OpportunityCandidate
    ) -> None:
        from opportunity_sentinel.matching import match_opportunity

        decision = match_opportunity(candidate, profile)
        state = (
            "eligible"
            if decision.eligible
            else ("needs_confirmation" if decision.unknown_requirements else "ineligible")
        )
        self.connection.execute(
            """INSERT INTO matches VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(telegram_id,opportunity_id) DO UPDATE SET score=excluded.score,
               reasons=excluded.reasons,unknown_requirements=excluded.unknown_requirements,
               state=excluded.state,calculated_at=excluded.calculated_at""",
            (
                profile.telegram_id,
                opportunity_id,
                decision.score,
                json.dumps(decision.reasons, ensure_ascii=False),
                json.dumps(decision.unknown_requirements, ensure_ascii=False),
                state,
                _now(),
            ),
        )
        if decision.eligible and decision.score >= 60:
            kind = "immediate" if decision.score >= 80 or _closes_within(candidate, 2) else "digest"
            due = _now() if kind == "immediate" else _next_digest()
            self.connection.execute(
                """INSERT OR IGNORE INTO delivery_queue
                   (telegram_id,opportunity_id,delivery_kind,due_at,created_at)
                   VALUES (?,?,?,?,?)""",
                (profile.telegram_id, opportunity_id, kind, due, _now()),
            )

    def due_deliveries(self, limit: int = 100) -> list[Any]:
        return self.connection.execute(
            """SELECT q.*,o.payload,m.score,m.reasons FROM delivery_queue q
               JOIN opportunities o ON o.id=q.opportunity_id
               JOIN matches m ON m.telegram_id=q.telegram_id AND m.opportunity_id=q.opportunity_id
               WHERE q.status='pending' AND q.due_at<=? AND o.status='verified'
               ORDER BY q.due_at,q.id LIMIT ?""",
            (_now(), limit),
        ).fetchall()

    def complete_delivery(self, queue_id: int, telegram_id: int, opportunity_id: str) -> None:
        now = _now()
        self.connection.execute("UPDATE delivery_queue SET status='sent' WHERE id=?", (queue_id,))
        self.connection.execute(
            "INSERT OR IGNORE INTO deliveries VALUES (?,?,?)", (telegram_id, opportunity_id, now)
        )
        self.connection.commit()

    def fail_delivery(self, queue_id: int, error: str) -> None:
        self.connection.execute(
            "UPDATE delivery_queue SET attempts=attempts+1,last_error=? WHERE id=?",
            (error[:500], queue_id),
        )
        self.connection.commit()

    def save_for_student(self, telegram_id: int, opportunity_id: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO saved_opportunities VALUES (?,?,?)",
            (telegram_id, opportunity_id, _now()),
        )
        self.connection.commit()

    def was_delivered(self, telegram_id: int, opportunity_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM deliveries WHERE telegram_id=? AND opportunity_id=?",
                (telegram_id, opportunity_id),
            ).fetchone()
            is not None
        )

    def mark_delivered(self, telegram_id: int, opportunity_id: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO deliveries VALUES (?,?,?)",
            (telegram_id, opportunity_id, _now()),
        )
        self.connection.commit()

    def list_saved(self, telegram_id: int) -> list[tuple[str, OpportunityCandidate]]:
        rows = self.connection.execute(
            """SELECT o.id,o.payload FROM opportunities o JOIN saved_opportunities s
               ON s.opportunity_id=o.id WHERE s.telegram_id=? ORDER BY s.saved_at DESC""",
            (telegram_id,),
        ).fetchall()
        return [
            (row["id"], OpportunityCandidate.model_validate_json(row["payload"])) for row in rows
        ]

    def record_signal(
        self, source_id: str | None, source_url: str, title: str, payload: dict[str, Any]
    ) -> str:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        identifier = hashlib.sha256(f"{source_url}:{digest}".encode()).hexdigest()[:24]
        self.connection.execute(
            """INSERT OR IGNORE INTO raw_signals
               (id,source_id,source_url,title,payload,content_hash,discovered_at)
               VALUES (?,?,?,?,?,?,?)""",
            (identifier, source_id, source_url, title, raw, digest, _now()),
        )
        self.connection.commit()
        return identifier

    def start_crawl(self, source_id: str | None) -> str:
        identifier = uuid.uuid4().hex[:24]
        self.connection.execute(
            "INSERT INTO crawl_runs (id,source_id,started_at,status) VALUES (?,?,?,'running')",
            (identifier, source_id, _now()),
        )
        self.connection.commit()
        return identifier

    def finish_crawl(
        self, run_id: str, discovered: int, verified: int, error: str | None = None
    ) -> None:
        status = "failed" if error else "succeeded"
        row = self.connection.execute(
            "SELECT source_id FROM crawl_runs WHERE id=?", (run_id,)
        ).fetchone()
        self.connection.execute(
            """UPDATE crawl_runs SET completed_at=?,status=?,discovered_count=?,
               verified_count=?,error=? WHERE id=?""",
            (_now(), status, discovered, verified, error, run_id),
        )
        if row and row["source_id"]:
            if error:
                self.connection.execute(
                    """UPDATE sources SET last_error_at=?,last_error=?,
                       consecutive_failures=consecutive_failures+1,updated_at=? WHERE id=?""",
                    (_now(), error[:500], _now(), row["source_id"]),
                )
            else:
                self.connection.execute(
                    """UPDATE sources SET last_success_at=?,last_error=NULL,
                       consecutive_failures=0,updated_at=? WHERE id=?""",
                    (_now(), _now(), row["source_id"]),
                )
        self.connection.commit()

    def queue_admin_review(self, candidate: OpportunityCandidate, reason: str) -> int | None:
        payload = candidate.model_dump_json()
        fingerprint = hashlib.sha256(f"{candidate.application_url}:{reason}".encode()).hexdigest()
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO admin_reviews
               (reason,state,created_at,candidate_json,fingerprint)
               VALUES (?,'pending',?,?,?)""",
            (reason[:500], _now(), payload, fingerprint),
        )
        self.connection.commit()
        return int(cursor.lastrowid) if cursor.rowcount else None

    def source_health(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT id,name,base_url,adapter_type,trust_tier,enabled,
               implementation_status,coverage_notes,last_success_at,last_error_at,
               last_error,consecutive_failures FROM sources ORDER BY trust_tier,name"""
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            source = dict(row)
            implementation_status = str(source["implementation_status"])
            if implementation_status == "planned":
                operational_status = "planned"
            elif implementation_status == "signal_only":
                operational_status = "signal_only"
            elif int(source["consecutive_failures"]) >= 2:
                operational_status = "degraded"
            elif source["last_success_at"]:
                operational_status = "healthy"
            else:
                operational_status = "untested"
            source["operational_status"] = operational_status
            result.append(source)
        return result

    def coverage_snapshot(self, today: date | None = None) -> dict[str, Any]:
        """Return measurable inventory and connector coverage without claiming web recall."""
        today = today or date.today()
        status_rows = self.connection.execute(
            "SELECT status,COUNT(*) AS count FROM opportunities GROUP BY status"
        ).fetchall()
        type_rows = self.connection.execute(
            """SELECT json_extract(payload,'$.opportunity_type') AS opportunity_type,
               COUNT(*) AS count FROM opportunities WHERE status='verified'
               GROUP BY opportunity_type ORDER BY count DESC"""
        ).fetchall()
        stale_verified = 0
        verified_rows = self.connection.execute(
            "SELECT payload FROM opportunities WHERE status='verified'"
        ).fetchall()
        for row in verified_rows:
            candidate = OpportunityCandidate.model_validate_json(row["payload"])
            if (candidate.deadline and candidate.deadline < today) or (
                candidate.deadline is None
                and candidate.start_date is not None
                and candidate.start_date < today
            ):
                stale_verified += 1
        sources = self.source_health()
        implementation_counts: dict[str, int] = {}
        operational_counts: dict[str, int] = {}
        for source in sources:
            implementation = str(source["implementation_status"])
            operational = str(source["operational_status"])
            implementation_counts[implementation] = implementation_counts.get(implementation, 0) + 1
            operational_counts[operational] = operational_counts.get(operational, 0) + 1
        return {
            "generated_at": _now(),
            "inventory": {
                "by_status": {str(row["status"]): int(row["count"]) for row in status_rows},
                "verified_by_type": {
                    str(row["opportunity_type"]): int(row["count"]) for row in type_rows
                },
                "stale_verified": stale_verified,
            },
            "sources": {
                "total": len(sources),
                "by_implementation": implementation_counts,
                "by_operational_status": operational_counts,
                "items": sources,
            },
            "recall": {
                "status": "not_measured",
                "reason": "An independently curated gold set is required",
            },
        }

    def latest_crawl(self, source_id: str | None = None) -> dict[str, Any] | None:
        if source_id is None:
            row = self.connection.execute(
                """SELECT * FROM crawl_runs ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()
        else:
            row = self.connection.execute(
                """SELECT * FROM crawl_runs WHERE source_id=?
                   ORDER BY started_at DESC LIMIT 1""",
                (source_id,),
            ).fetchone()
        return dict(row) if row else None

    def verified_opportunity_ids(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT id FROM opportunities WHERE status='verified'"
        ).fetchall()
        return {str(row["id"]) for row in rows}

    def consume_quota(self, provider: str, units: int, monthly_limit: int) -> bool:
        month = date.today().isoformat()[:7]
        used = self.connection.execute(
            "SELECT COALESCE(SUM(units),0) FROM quota_usage WHERE provider=? AND usage_date LIKE ?",
            (provider, f"{month}%"),
        ).fetchone()[0]
        if used + units > monthly_limit:
            return False
        today = date.today().isoformat()
        self.connection.execute(
            """INSERT INTO quota_usage VALUES (?,?,?) ON CONFLICT(provider,usage_date)
               DO UPDATE SET units=units+excluded.units""",
            (provider, today, units),
        )
        self.connection.commit()
        return True

    def expire_stale(self, today: date | None = None) -> int:
        today = today or date.today()
        rows = self.connection.execute(
            "SELECT id,payload FROM opportunities WHERE status='verified'"
        ).fetchall()
        expired = 0
        for row in rows:
            candidate = OpportunityCandidate.model_validate_json(row["payload"])
            if (candidate.deadline and candidate.deadline < today) or (
                candidate.deadline is None
                and candidate.start_date is not None
                and candidate.start_date < today
            ):
                self.connection.execute(
                    "UPDATE opportunities SET status='expired',lifecycle='expired' WHERE id=?",
                    (row["id"],),
                )
                self.connection.execute(
                    """UPDATE delivery_queue SET status='cancelled'
                       WHERE opportunity_id=? AND status='pending'""",
                    (row["id"],),
                )
                expired += 1
        self.connection.commit()
        return expired

    def due_revalidation(self, limit: int = 100) -> list[tuple[str, OpportunityCandidate]]:
        rows = self.connection.execute(
            """SELECT id,payload FROM opportunities WHERE status='verified'
               ORDER BY CASE WHEN lifecycle='closing_soon' THEN 0 ELSE 1 END,
               last_verified_at LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            (row["id"], OpportunityCandidate.model_validate_json(row["payload"])) for row in rows
        ]

    def record_revalidation(self, opportunity_id: str, state: str, reason: str) -> None:
        if state == "closed":
            self.connection.execute(
                """UPDATE opportunities SET status='expired',lifecycle='expired',
                   last_verified_at=? WHERE id=?""",
                (_now(), opportunity_id),
            )
            self.connection.execute(
                """UPDATE delivery_queue SET status='cancelled' WHERE opportunity_id=?
                   AND status='pending'""",
                (opportunity_id,),
            )
        elif state == "open":
            self.connection.execute(
                "UPDATE opportunities SET last_verified_at=? WHERE id=?",
                (_now(), opportunity_id),
            )
        else:
            self.connection.execute(
                """INSERT INTO health_events (component,severity,message,metadata,created_at)
                   VALUES ('revalidation','warning',?,?,?)""",
                (reason[:500], json.dumps({"opportunity_id": opportunity_id}), _now()),
            )
        self.connection.commit()

    def metrics(self) -> dict[str, int]:
        def count(table: str) -> int:
            return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

        return {
            "students": count("students"),
            "opportunities": count("opportunities"),
            "sources": count("sources"),
            "signals": count("raw_signals"),
            "pending_deliveries": int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM delivery_queue WHERE status='pending'"
                ).fetchone()[0]
            ),
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_url(url: str) -> str:
    return url.strip().rstrip("/")


def _closes_within(candidate: OpportunityCandidate, days: int) -> bool:
    return bool(candidate.deadline and candidate.deadline <= date.today() + timedelta(days=days))


def _lifecycle(candidate: OpportunityCandidate, status: str) -> OpportunityLifecycle:
    if status == "rejected":
        return OpportunityLifecycle.REJECTED
    if status == "expired" or (candidate.deadline and candidate.deadline < date.today()):
        return OpportunityLifecycle.EXPIRED
    if _closes_within(candidate, 3):
        return OpportunityLifecycle.CLOSING_SOON
    return (
        OpportunityLifecycle.VERIFIED_OPEN
        if status == "verified"
        else OpportunityLifecycle.NEEDS_EVIDENCE
    )


def _next_digest() -> str:
    now = datetime.now(UTC)
    # 18:00 Asia/Riyadh == 15:00 UTC.
    digest = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if digest <= now:
        digest += timedelta(days=1)
    return digest.isoformat()
