from __future__ import annotations

from datetime import date, timedelta

from opportunity_sentinel.models import (
    DeliveryMode,
    EligibilityDecision,
    OpportunityCandidate,
    StudentProfile,
)

ALL_MAJORS = {"all majors", "all disciplines", "جميع التخصصات"}
TECHNICAL = {
    "all technical majors",
    "technical majors",
    "computer related majors",
    "جميع التخصصات التقنية",
    "التخصصات التقنية",
}
TECH_ALIASES = {
    "هندسة البرمجيات",
    "علوم الحاسب",
    "هندسة الحاسب",
    "تقنية المعلومات",
    "الأمن السيبراني",
    "الذكاء الاصطناعي",
    "علم البيانات",
    "software engineering",
    "computer science",
    "computer engineering",
    "information technology",
    "cybersecurity",
    "artificial intelligence",
    "data science",
}
def match_opportunity(
    candidate: OpportunityCandidate, profile: StudentProfile, today: date | None = None
) -> EligibilityDecision:
    today = today or date.today()
    reasons: list[str] = []
    unknown: list[str] = []
    if candidate.registration_open is False or (candidate.deadline and candidate.deadline < today):
        return EligibilityDecision(eligible=False, reasons=["الفرصة مغلقة أو منتهية"], score=0)
    if profile.preferred_types and candidate.opportunity_type not in profile.preferred_types:
        return EligibilityDecision(eligible=False, reasons=["نوع الفرصة غير مطلوب"], score=0)

    accepted = {value.casefold().strip() for value in candidate.accepted_majors}
    student_major = profile.major.casefold().strip()
    is_technical = student_major in TECH_ALIASES
    major_match = (
        student_major in accepted
        or bool(accepted & ALL_MAJORS)
        or (is_technical and bool(accepted & TECHNICAL))
        or (is_technical and not accepted and candidate.technical_focus is True)
    )
    if not accepted:
        if not major_match:
            unknown.append("accepted_majors")
    elif not major_match:
        return EligibilityDecision(eligible=False, reasons=["التخصص غير مقبول"], score=0)

    if (
        candidate.accepted_graduation_years
        and profile.graduation_year not in candidate.accepted_graduation_years
    ):
        return EligibilityDecision(eligible=False, reasons=["سنة التخرج غير مقبولة"], score=0)
    if not profile.accepts_online and candidate.delivery_mode == DeliveryMode.ONLINE:
        return EligibilityDecision(eligible=False, reasons=["الطالب لا يقبل الفرص عن بُعد"], score=0)
    if candidate.delivery_mode != DeliveryMode.ONLINE and candidate.city:
        cities = {city.casefold() for city in profile.preferred_cities}
        all_saudi = "جميع مدن السعودية" in profile.preferred_cities
        if not all_saudi and candidate.city.casefold() not in cities:
            return EligibilityDecision(eligible=False, reasons=["المدينة غير مقبولة"], score=0)

    # Keep provider requirements in the candidate record for transparency, but do not
    # hide a verified technical opportunity because of them. Discovery completeness is
    # the publication rule; the official application page remains the source of terms.

    if unknown:
        return EligibilityDecision(
            eligible=False,
            reasons=["نحتاج تأكيد بعض متطلبات الأهلية"],
            unknown_requirements=sorted(set(unknown)),
            score=0,
        )

    score = 35 + 20
    reasons.extend(["التخصص مطابق", "نوع الفرصة مطلوب"])
    if candidate.delivery_mode == DeliveryMode.ONLINE or candidate.city:
        score += 15
        reasons.append("الموقع أو نمط الحضور مناسب")
    if (
        not candidate.accepted_graduation_years
        or profile.graduation_year in candidate.accepted_graduation_years
    ):
        score += 10
    profile_skills = str(profile.profile_answers.get("skills", "")).casefold()
    if not candidate.skills or any(
        skill.casefold() in profile_skills for skill in candidate.skills
    ):
        score += 10
    if candidate.deadline and candidate.deadline <= today + timedelta(days=7):
        score += 10
        reasons.append("موعد الإغلاق قريب")
    elif candidate.publication_date and candidate.publication_date >= today - timedelta(days=14):
        score += 10
        reasons.append("فرصة حديثة")
    else:
        score += 5
    return EligibilityDecision(eligible=True, reasons=reasons, score=min(score, 100))
