from datetime import date

import httpx

from opportunity_sentinel.agents import VerificationAgent
from opportunity_sentinel.connectors import (
    ATSBoard,
    FinancialAcademyHackathonConnector,
    FutureSkillsConnector,
    KSUAlumniJobsConnector,
    KSUOfficialNewsConnector,
    MiskProgramsConnector,
    PublicATSConnector,
    _ats_location,
    _ats_opportunity_type,
    _iso_date,
    extract_linkedin_technical_training,
)
from opportunity_sentinel.models import DeliveryMode, OpportunityType, VerificationStatus
from opportunity_sentinel.tools import SourcePage
from scripts.scheduled_job import _queries


def _client(*, free_proof: bool = True) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ar/seekers-faq":
            proof = (
                "كافة البرامج التدريبية المقدمة من مهارات المستقبل مجانية"
                if free_proof
                else "معلومات البرامج التدريبية"
            )
            return httpx.Response(200, text=f"<html><body>{proof}</body></html>")
        if request.url.path == "/ar/catalogue/all":
            return httpx.Response(
                200,
                text=(
                    '<a href="/ar/group/13051">open</a>'
                    '<a href="/ar/group/12000">closed</a>'
                ),
            )
        if request.url.path == "/ar/group/12000":
            return httpx.Response(200, text="<html><body>انتهت فترة التقديم</body></html>")
        if request.url.path == "/ar/group/13051":
            return httpx.Response(
                200,
                text="""
                <html><head><title>أساسيات السحابة | بوابة مهارات المستقبل</title></head>
                <body>
                  <span>مقدم من:</span><span>مكان التعلم</span>
                  <h5>المتطلبات السابقة للتدريب</h5>
                  <ul>
                    <li>سعودي الجنسية</li><li>دبلوم وما أعلى</li>
                    <li>لغة إنجليزية متوسطة</li><li>جهاز كمبيوتر</li>
                  </ul>
                  <span>طريقة توصيل الدورة</span><span>تفاعلية مباشرة</span>
                  <span>موعد البرنامج تبدأ 16-08-2026 إلى 18-08-2026 لمدة 12 ساعات</span>
                  <a class="join-link" href="/ar/user/login?destination=group/13051">
                    طلب انضمام
                  </a>
                </body></html>
                """,
            )
        return httpx.Response(404)

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://futureskills.mcit.gov.sa",
    )


def test_future_skills_connector_publishes_only_open_free_courses() -> None:
    client = _client()
    connector = FutureSkillsConnector(client=client)

    candidates = connector.collect(today=date(2026, 8, 13))

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.title == "أساسيات السحابة"
    assert candidate.is_free is True
    assert candidate.registration_open is True
    assert candidate.accepted_majors == ["جميع التخصصات"]
    assert candidate.requirements == {
        "saudi_national": True,
        "degree_level": ["diploma", "bachelor", "graduate"],
        "english_level": ["intermediate", "advanced"],
        "has_computer": True,
    }
    assert VerificationAgent().verify(candidate).status == VerificationStatus.VERIFIED
    client.close()


def test_future_skills_connector_fails_closed_without_official_cost_proof() -> None:
    client = _client(free_proof=False)
    connector = FutureSkillsConnector(client=client)

    assert connector.collect(today=date(2026, 8, 13)) == []
    client.close()


def test_financial_academy_connector_extracts_current_hackathon() -> None:
    html = """
    <html><head><title>الأكاديمية المالية</title></head><body>
    <a href="/Services/ProgramDetailsAuth/id"><span>التسجيل</span></a>
    <p>الرسوم 9,200.00 مدعوم بالكامل</p><p>تدريب حضوري الرياض</p>
    <p>هاكاثون يجمع المطورين والمتخصصين في علم البيانات</p>
    <p>التاريخ 6 سبتمبر 2026</p>
    </body></html>
    """
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html)))
    connector = FinancialAcademyHackathonConnector(client=client)

    candidates = connector.collect(today=date(2026, 8, 13))

    assert len(candidates) == 1
    assert candidates[0].opportunity_type == OpportunityType.HACKATHON
    assert VerificationAgent().verify(candidates[0]).status == VerificationStatus.VERIFIED
    client.close()


def test_misk_connector_collects_only_open_technical_programs() -> None:
    html = """
    <div class="carousel-item">
      <div class="links-bar-wrapper">إغلاق باب التقديم في 30 نوفمبر 2026</div>
      <h2>برنامج هندسة الذكاء الاصطناعي</h2>
      <p>برنامج تقني لتطوير نماذج الذكاء الاصطناعي لجميع التخصصات التقنية</p>
      <div class="highlighter-box-wrapper"><span>عن بعد</span></div>
      <a class="js-program-url" data-program-url="/ar/apply/ai">قدّم الآن</a>
      <input class="listing-banner-program-data"
        data-current-page-url="/ar/programs/skills/ai-engineering/" />
    </div>
    <div class="carousel-item">
      <h2>برنامج مغلق في الأمن السيبراني</h2>
      <span>عن بعد</span><a>إرسال إشعار</a>
      <input class="listing-banner-program-data"
        data-current-page-url="/ar/programs/skills/closed-cyber/" />
    </div>
    """
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html))
    )
    connector = MiskProgramsConnector(client=client)

    candidates = connector.collect(today=date(2026, 8, 15))

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.title == "برنامج هندسة الذكاء الاصطناعي"
    assert candidate.deadline == date(2026, 11, 30)
    assert candidate.delivery_mode == DeliveryMode.ONLINE
    assert candidate.accepted_majors == ["جميع التخصصات التقنية"]
    assert candidate.technical_focus is True
    assert VerificationAgent().verify(candidate).status == VerificationStatus.VERIFIED
    client.close()


def test_linkedin_parser_accepts_active_technical_job_and_rejects_closed() -> None:
    page = SourcePage(
        url="https://www.linkedin.com/jobs/view/4409882454/",
        title="Hewlett Packard Enterprise hiring College Intern (COOP) in Riyadh",
        content="Apply Riyadh Degrees: Computer Science, Information Technology COOP",
    )

    candidate = extract_linkedin_technical_training(page)

    assert candidate is not None
    assert candidate.opportunity_type == OpportunityType.COOP
    assert VerificationAgent().verify(candidate).status == VerificationStatus.VERIFIED
    assert (
        extract_linkedin_technical_training(
            page.__class__(
                **{**page.__dict__, "content": page.content + " no longer accepting applications"}
            )
        )
        is None
    )


def test_rotating_deep_query_matrix_covers_every_priority_category() -> None:
    shards = [_queries("deep", slot=slot) for slot in range(3)]
    combined = " ".join(query for shard in shards for query in shard).casefold()

    assert all(len(shard) == 5 for shard in shards)
    assert all(any("site:x.com" in query for query in shard) for shard in shards)
    for marker in (
        "coop",
        "internship",
        "تدريب صيفي",
        "دوام جزئي",
        "وظيفة مبتدئة",
        "هاكاثون",
        "مسابقة",
        "منحة",
        "معسكر",
        "فعالية",
        "تطوع",
    ):
        assert marker.casefold() in combined


def test_ksu_alumni_connector_collects_only_open_technical_opportunities() -> None:
    def detail(
        identifier: str,
        title: str,
        status: str,
        description: str,
        majors: str,
    ) -> str:
        return f"""
        <div id="ajax_table">
          <header class="card">
            <section class="d-flex"><div class="m-0"><a>شركة تقنية</a></div></section>
            <a href="/user/jobs/details/{identifier}"><span class="fs-1">{title}</span></a>
            <a class="g-btn">{status}</a>
            <span>تم النشر</span><span>2026-08-12</span>
          </header>
          <div class="decription_details">{description}</div>
          <div class="d-flex flex-stack">
            <div class="text-gray-700">الكلية</div>
            <div class="d-flex"><span>علوم الحاسب والمعلومات,</span></div>
          </div>
          <div class="d-flex flex-stack">
            <div class="text-gray-700">التخصص</div>
            <div class="d-flex"><span>{majors}</span></div>
          </div>
          <div class="d-flex flex-stack">
            <div class="text-gray-700">الموقع</div>
            <div class="d-flex"><span>Riyadh Saudi Arabia</span></div>
          </div>
        </div>
        """

    listing = """
    <a href="/user/jobs/details/open-tech">tech</a>
    <a href="/user/jobs/details/closed-tech">closed</a>
    <a href="/user/jobs/details/open-accounting">accounting</a>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/user/jobs":
            return httpx.Response(200, text=listing)
        if path.endswith("/open-tech"):
            return httpx.Response(
                200,
                text=detail(
                    "open-tech",
                    "Software Engineering COOP",
                    "قدم الآن",
                    "تدريب تعاوني في تطوير البرمجيات",
                    "هندسة الحاسب, هندسة البرمجيات,",
                ),
            )
        if path.endswith("/closed-tech"):
            return httpx.Response(
                200,
                text=detail(
                    "closed-tech",
                    "Cybersecurity Internship",
                    "انتهى التقديم على الوظيفة",
                    "Cybersecurity training",
                    "الأمن السيبراني,",
                ),
            )
        return httpx.Response(
            200,
            text=detail(
                "open-accounting",
                "محاسب",
                "قدم الآن",
                "إعداد القوائم المالية",
                "المحاسبة,",
            ),
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://alumnigate.ksu.edu.sa",
    )
    connector = KSUAlumniJobsConnector(client=client, max_pages=2)

    candidates = connector.collect(today=date(2026, 8, 13))

    assert len(candidates) == 1
    assert candidates[0].title == "Software Engineering COOP"
    assert candidates[0].opportunity_type == OpportunityType.COOP
    assert "هندسة الحاسب" in candidates[0].accepted_majors
    assert VerificationAgent().verify(candidates[0]).status == VerificationStatus.VERIFIED
    client.close()


def test_ksu_news_connector_requires_current_open_technical_application() -> None:
    listing = '<a href="/ar/node/2026">فرصة</a><a href="/ar/node/old">قديم</a>'
    detail = """
    <html><body><article>
      <h1>هاكاثون تطبيقات الذكاء الاصطناعي</h1>
      <p>10 أغسطس 2026</p>
      <p>تدعو جامعة الملك سعود طلاب الجامعة لبناء حلول برمجية وتقنية.</p>
      <p>يقام الهاكاثون في جامعة الملك سعود بالرياض يوم 20 أغسطس 2026.</p>
      <a href="https://forms.ksu.edu.sa/ai-hackathon">سجّل الآن</a>
    </article></body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ar/node":
            return httpx.Response(200, text=listing)
        if request.url.path == "/ar/node/2026":
            return httpx.Response(200, text=detail)
        return httpx.Response(404)

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://news.ksu.edu.sa",
    )
    connector = KSUOfficialNewsConnector(client=client, max_pages=2)

    candidates = connector.collect(today=date(2026, 8, 15))

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.opportunity_type == OpportunityType.HACKATHON
    assert candidate.city == "الرياض"
    assert candidate.start_date == date(2026, 8, 20)
    assert candidate.technical_focus is True
    assert VerificationAgent().verify(candidate).status == VerificationStatus.VERIFIED
    client.close()


def test_public_ats_connector_normalizes_three_official_providers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "ashbyhq.com" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "title": "Agent Engineering Intern",
                            "location": "Riyadh, Saudi Arabia",
                            "department": "Engineering",
                            "descriptionPlain": "Build artificial intelligence software agents.",
                            "employmentType": "Intern",
                            "workplaceType": "On-site",
                            "isListed": True,
                            "publishedAt": "2026-08-10T00:00:00Z",
                            "jobUrl": "https://jobs.ashbyhq.com/company/job-1",
                            "applyUrl": "https://jobs.ashbyhq.com/company/job-1/application",
                        }
                    ]
                },
            )
        if "lever.co" in request.url.host:
            return httpx.Response(
                200,
                json=[
                    {
                        "text": "Data Engineering Internship",
                        "descriptionPlain": "Work with software and data engineering teams.",
                        "categories": {
                            "location": "Riyadh",
                            "department": "Technology",
                            "commitment": "Internship",
                        },
                        "hostedUrl": "https://jobs.lever.co/company/job-2",
                        "applyUrl": "https://jobs.lever.co/company/job-2/apply",
                        "workplaceType": "on-site",
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "title": "Cybersecurity Intern",
                        "location": {"name": "Riyadh"},
                        "content": "Computer Science students; cybersecurity internship.",
                        "departments": [{"name": "Information Technology"}],
                        "absolute_url": "https://boards.greenhouse.io/company/jobs/3",
                        "updated_at": "2026-08-11T00:00:00Z",
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    connector = PublicATSConnector(
        [
            ATSBoard("ashby", "company", "Ashby Company"),
            ATSBoard("lever", "company", "Lever Company"),
            ATSBoard("greenhouse", "company", "Greenhouse Company"),
        ],
        client=client,
    )

    candidates = connector.collect()

    assert len(candidates) == 3
    assert {candidate.organization for candidate in candidates} == {
        "Ashby Company",
        "Lever Company",
        "Greenhouse Company",
    }
    assert all(
        VerificationAgent().verify(candidate).status == VerificationStatus.VERIFIED
        for candidate in candidates
    )
    client.close()


def test_ats_taxonomy_covers_early_career_types_and_locations() -> None:
    assert _ats_opportunity_type("Software CO-OP") == OpportunityType.COOP
    assert _ats_opportunity_type("Developer part-time") == OpportunityType.PART_TIME_JOB
    assert _ats_opportunity_type("Technology graduate program") == OpportunityType.GRADUATE_PROGRAM
    assert _ats_opportunity_type("Junior software engineer") == OpportunityType.ENTRY_LEVEL_JOB
    assert _ats_opportunity_type("Senior accountant") is None
    assert _ats_location("Saudi Arabia", "remote") == ("عن بُعد", DeliveryMode.ONLINE)
    assert _ats_location("Jeddah", "on-site") == (None, DeliveryMode.IN_PERSON)
    assert _iso_date("2026-08-13T12:00:00Z") == date(2026, 8, 13)
    assert _iso_date("not-a-date") is None
