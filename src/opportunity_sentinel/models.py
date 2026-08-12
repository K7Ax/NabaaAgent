from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, model_validator


class OpportunityType(StrEnum):
    INTERNSHIP = "internship"
    COOP = "coop"
    COURSE = "course"


class DeliveryMode(StrEnum):
    IN_PERSON = "in_person"
    ONLINE = "online"
    HYBRID = "hybrid"


class VerificationStatus(StrEnum):
    DISCOVERED = "discovered"
    NEEDS_RESEARCH = "needs_research"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    VERIFIED = "verified"
    REJECTED = "rejected"


class Evidence(BaseModel):
    field_name: str
    value: str
    quote: str = Field(min_length=3, max_length=1000)
    source_url: HttpUrl
    official_source: bool = False
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OpportunityCandidate(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    organization: str = Field(min_length=2, max_length=200)
    opportunity_type: OpportunityType
    city: str | None = None
    delivery_mode: DeliveryMode
    accepted_majors: list[str] = Field(default_factory=list)
    accepted_graduation_years: list[int] = Field(default_factory=list)
    deadline: date | None = None
    registration_open: bool | None = None
    application_url: HttpUrl
    source_url: HttpUrl
    evidence: list[Evidence] = Field(default_factory=list)

    def evidence_for(self, field_name: str) -> list[Evidence]:
        return [item for item in self.evidence if item.field_name == field_name]

    @model_validator(mode="after")
    def require_location(self) -> "OpportunityCandidate":
        if self.delivery_mode != DeliveryMode.ONLINE and not self.city:
            raise ValueError("city is required for in-person and hybrid opportunities")
        return self


class VerificationReport(BaseModel):
    status: VerificationStatus
    score: float = Field(ge=0, le=1)
    missing_fields: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    requires_human_review: bool = False


class StudentProfile(BaseModel):
    telegram_id: int
    major: str
    graduation_year: int
    preferred_types: set[OpportunityType] = Field(default_factory=set)
    accepts_online: bool = True


class EligibilityDecision(BaseModel):
    eligible: bool
    reasons: list[str] = Field(default_factory=list)


class ToolObservation(BaseModel):
    tool: str
    success: bool
    detail: str
    latency_ms: float = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
