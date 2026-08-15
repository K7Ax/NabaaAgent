from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from opportunity_sentinel.models import (
    DeliveryMode,
    Evidence,
    OpportunityCandidate,
    OpportunityType,
)
from opportunity_sentinel.tools import SourcePage


class FutureSkillsConnector:
    """Deterministic first-party connector for MCIT Future Skills courses."""

    base_url = "https://futureskills.mcit.gov.sa"
    catalogue_url = f"{base_url}/ar/catalogue/all"
    faq_url = f"{base_url}/ar/seekers-faq"
    free_evidence = "كافة البرامج التدريبية المقدمة من مهارات المستقبل مجانية"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        timeout: float = 20,
        max_courses: int = 30,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "OpportunitySentinel/0.1 educational-research-bot"},
        )
        self.max_courses = max_courses

    def collect(self, today: date | None = None) -> list[OpportunityCandidate]:
        """Return only open, future, free courses backed by official-page evidence."""
        today = today or date.today()
        faq_response = self.client.get(self.faq_url)
        faq_response.raise_for_status()
        faq_text = _page_text(faq_response.text)
        if self.free_evidence not in faq_text:
            # Cost is a hard publication gate. A changed FAQ must fail closed.
            return []

        catalogue_response = self.client.get(self.catalogue_url)
        catalogue_response.raise_for_status()
        catalogue = BeautifulSoup(catalogue_response.text, "html.parser")
        urls = sorted(
            {
                urljoin(self.base_url, anchor["href"])
                for anchor in catalogue.select("a[href]")
                if re.fullmatch(r"/ar/group/\d+", anchor.get("href", ""))
            }
        )[: self.max_courses]
        if not urls:
            return []

        with ThreadPoolExecutor(max_workers=min(6, len(urls))) as executor:
            candidates = list(
                executor.map(lambda url: self._read_course(url, faq_text, today), urls)
            )
        return [candidate for candidate in candidates if candidate is not None]

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _read_course(
        self, url: str, faq_text: str, today: date
    ) -> OpportunityCandidate | None:
        response = self.client.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        text = _page_text(response.text)
        if "انتهت فترة التقديم" in text or "طلب انضمام" not in text:
            return None

        join_link = soup.select_one("a.join-link[href]")
        if not join_link:
            return None
        date_match = re.search(
            r"تبدأ\s*(\d{2}-\d{2}-\d{4})\s*إلى\s*(\d{2}-\d{2}-\d{4})",
            text,
        )
        if not date_match:
            return None
        start_date = _arabic_course_date(date_match.group(1))
        end_date = _arabic_course_date(date_match.group(2))
        if end_date < today:
            return None

        title = _course_title(soup, url)
        organization = _provider_name(soup) or "وزارة الاتصالات وتقنية المعلومات"
        requirements = _requirements(text)
        delivery_quote = _delivery_quote(text)
        if not delivery_quote:
            return None

        requirement_quote = "المتطلبات السابقة للتدريب: " + "، ".join(
            quote for _, quote in requirements
        )
        evidence = [
            Evidence(
                field_name="organization",
                value=organization,
                quote=f"مقدم من: {organization}",
                source_url=url,
                official_source=True,
            ),
            Evidence(
                field_name="registration_status",
                value="open",
                quote="طلب انضمام",
                source_url=url,
                official_source=True,
            ),
            Evidence(
                field_name="accepted_majors",
                value="جميع التخصصات",
                quote=requirement_quote,
                source_url=url,
                official_source=True,
            ),
            Evidence(
                field_name="cost",
                value="free",
                quote=self.free_evidence,
                source_url=self.faq_url,
                official_source=True,
            ),
            Evidence(
                field_name="delivery_mode",
                value="online",
                quote=delivery_quote,
                source_url=url,
                official_source=True,
            ),
            Evidence(
                field_name="start_date",
                value=start_date.isoformat(),
                quote=date_match.group(0),
                source_url=url,
                official_source=True,
            ),
        ]

        return OpportunityCandidate(
            title=title,
            organization=organization,
            opportunity_type=OpportunityType.COURSE,
            delivery_mode=DeliveryMode.ONLINE,
            accepted_majors=["جميع التخصصات"],
            registration_open=True,
            application_url=urljoin(url, str(join_link["href"])),
            source_url=url,
            evidence=evidence,
            start_date=start_date,
            end_date=end_date,
            requirements={key: expected for key, _, expected in _requirement_rules(requirements)},
            is_free=True,
            remote_allowed=True,
        )


class MonshaatAcademyConnector:
    """Collect open technical courses from Monsha'at Academy's official catalogue."""

    base_url = "https://academy.monshaat.gov.sa"
    catalogue_url = f"{base_url}/local/course/index.php?categoryid=0"
    listing_url = f"{base_url}/theme/lambda/layout/includes/course_listing_ajax.php"
    service_url = "https://www.monshaat.gov.sa/ar/node/5257"
    technical_category_id = "10"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        timeout: float = 20,
        max_courses: int = 30,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "OpportunitySentinel/0.1 educational-research-bot"},
        )
        self.max_courses = max_courses

    def collect(self, today: date | None = None) -> list[OpportunityCandidate]:
        del today  # Ongoing courses are gated by a live "register now" control.
        cost_quote = self._free_service_quote()
        response = self.client.post(
            self.listing_url,
            data={
                "courses_search": "",
                "category_list[]": self.technical_category_id,
                "location": "",
                "crs_type": "",
                "page": "1",
                "courses_type": "all",
                "sorting": "",
                "private": "false",
                "sign_language": "false",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("main"), str):
            return []
        soup = BeautifulSoup(payload["main"], "html.parser")
        cards = soup.select("div.new_card_container")[: self.max_courses]
        if not cards:
            return []
        with ThreadPoolExecutor(max_workers=min(6, len(cards))) as executor:
            candidates = list(
                executor.map(lambda card: self._candidate(card, cost_quote), cards)
            )
        return [candidate for candidate in candidates if candidate is not None]

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _free_service_quote(self) -> str | None:
        try:
            response = self.client.get(self.service_url)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        text = _page_text(response.text)
        return next(
            (
                marker
                for marker in ("خدمة مجانية", "البرامج التدريبية المجانية")
                if marker in text
            ),
            None,
        )

    def _candidate(
        self, card, cost_quote: str | None
    ) -> OpportunityCandidate | None:
        anchor = card.select_one(".new_card_container--title a[href]")
        title_node = card.select_one(".new_card_container--title h2")
        if anchor is None or title_node is None:
            return None
        source_url = urljoin(self.base_url, str(anchor.get("href") or ""))
        if "/local/course/enrol.php?id=" not in source_url:
            return None
        title = _clean_text(title_node)
        category_quotes = [_clean_text(node) for node in card.select(".new_card_tag")]
        if "التقنية والابتكار" not in category_quotes:
            return None
        program_type_node = card.select_one(".category-tag")
        program_type = _clean_text(program_type_node) if program_type_node else ""
        online_markers = (
            "برنامج إلكتروني مستمر",
            "برنامج مباشر افتراضي",
            "لقاء مسجل",
        )
        if not any(marker in program_type for marker in online_markers):
            # Local sessions require a separately proven city; do not guess it from the academy.
            return None

        detail = self.client.get(source_url)
        detail.raise_for_status()
        detail_text = _page_text(detail.text)
        if "سجل الآن" not in detail_text or any(
            marker in detail_text
            for marker in ("التسجيل مغلق", "انتهى التسجيل", "انتهت فترة التسجيل")
        ):
            return None
        evidence = [
            Evidence(
                field_name="organization",
                value="أكاديمية منشآت",
                quote="أكاديمية منشآت",
                source_url=source_url,
                official_source=True,
            ),
            Evidence(
                field_name="registration_status",
                value="open",
                quote="سجل الآن",
                source_url=source_url,
                official_source=True,
            ),
            Evidence(
                field_name="technical_focus",
                value="true",
                quote="التقنية والابتكار",
                source_url=source_url,
                official_source=True,
            ),
            Evidence(
                field_name="delivery_mode",
                value=DeliveryMode.ONLINE.value,
                quote=program_type,
                source_url=source_url,
                official_source=True,
            ),
        ]
        if cost_quote:
            evidence.append(
                Evidence(
                    field_name="cost",
                    value="free",
                    quote=cost_quote,
                    source_url=self.service_url,
                    official_source=True,
                )
            )
        return OpportunityCandidate(
            title=title[:300],
            organization="أكاديمية منشآت",
            opportunity_type=OpportunityType.COURSE,
            delivery_mode=DeliveryMode.ONLINE,
            technical_focus=True,
            registration_open=True,
            application_url=source_url,
            source_url=source_url,
            evidence=evidence,
            is_free=True if cost_quote else None,
            remote_allowed=True,
        )


class MiskProgramsConnector:
    """Deterministic connector for open programs in Misk Hub's official catalogue."""

    base_url = "https://hub.misk.org.sa"
    catalogue_url = f"{base_url}/ar/programs/"
    catalogue_api_url = f"{base_url}/api/RenderProgram/GetAllFilteredPrograms"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        timeout: float = 20,
        max_pages: int = 20,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "OpportunitySentinel/0.1 educational-research-bot"},
        )
        self.max_pages = max_pages

    def collect(self, today: date | None = None) -> list[OpportunityCandidate]:
        today = today or date.today()
        response = self.client.get(self.catalogue_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        candidates: dict[str, OpportunityCandidate] = {}
        for data_node in soup.select("input.listing-banner-program-data"):
            candidate = self._candidate(data_node, today)
            if candidate:
                candidates[str(candidate.source_url)] = candidate

        # The initial HTML contains only a small featured carousel. The complete
        # catalogue is loaded through Misk's first-party pagination endpoint.
        listing_nodes = self._catalogue_nodes()
        if listing_nodes:
            with ThreadPoolExecutor(max_workers=min(6, len(listing_nodes))) as executor:
                listed_candidates = executor.map(
                    lambda node: self._listing_candidate(node, today), listing_nodes
                )
            for candidate in listed_candidates:
                if candidate:
                    candidates[str(candidate.source_url)] = candidate
        return list(candidates.values())

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _catalogue_nodes(self) -> list[Any]:
        nodes: list[Any] = []
        seen_urls: set[str] = set()
        skip_count = 0
        for _ in range(self.max_pages):
            try:
                response = self.client.post(
                    self.catalogue_api_url,
                    data={
                        "SkipCount": skip_count,
                        "CurrentCulture": "ar-SA",
                        "CategoryId": 0,
                        "OrderBy": "",
                        "FilterBy": "",
                    },
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError, TypeError):
                # Keep the featured-card fallback working if Misk changes or
                # temporarily disables its catalogue endpoint.
                break
            if not isinstance(payload, dict):
                break
            fragment = BeautifulSoup(str(payload.get("stringObjectValues") or ""), "html.parser")
            for node in fragment.select("input.listing-card-program-data"):
                page_value = str(
                    node.get("data-program-url")
                    or node.get("data-current-page-url")
                    or ""
                ).strip()
                page_url = urljoin(self.base_url, page_value)
                if not page_value or page_url in seen_urls:
                    continue
                seen_urls.add(page_url)
                nodes.append(node)

            try:
                next_skip = int(payload.get("nextSkippedValue") or 0)
            except (TypeError, ValueError):
                break
            stop_value = str(payload.get("skippedloadmorebutton") or "").casefold()
            if stop_value in {"true", "404"} or next_skip <= skip_count:
                break
            skip_count = next_skip
        return nodes

    def _listing_candidate(self, data_node: Any, today: date) -> OpportunityCandidate | None:
        state = str(data_node.get("data-button-state") or "").strip().casefold()
        open_quote = str(data_node.get("data-button-title") or "").strip()
        if state != "open" or open_quote not in {"قدّم الآن", "انضم الآن", "سجّل الآن"}:
            return None

        card = data_node.find_parent(class_="article-content") or data_node.parent
        if card is None:
            return None
        title_node = card.select_one(".article-title-inner a span") or card.find(
            ["h2", "h3", "h4"]
        )
        title = _clean_text(title_node) if title_node else ""
        page_value = str(
            data_node.get("data-program-url")
            or data_node.get("data-current-page-url")
            or ""
        ).strip()
        if not title or not page_value or "/programs/" not in page_value:
            return None
        page_url = urljoin(self.base_url, page_value)

        application_value = str(
            data_node.get("data-button-url")
            or data_node.get("data-application-link")
            or data_node.get("data-external-application-form-url")
            or data_node.get("data-final-application-form-url")
            or page_url
        ).strip()
        if application_value.casefold().startswith("javascript:"):
            application_value = page_url
        application_url = urljoin(self.base_url, application_value)

        try:
            response = self.client.get(page_url)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        detail_soup = BeautifulSoup(response.text, "html.parser")
        detail_scopes = [
            scope
            for scope in (
                detail_soup.select_one("header.program-overview-v3"),
                detail_soup.select_one("section.content-detail"),
            )
            if scope is not None
        ]
        # Related programmes appear beneath the detail section and must not be
        # allowed to make an unrelated programme look technical.
        detail_text = " ".join(_clean_text(scope) for scope in detail_scopes)
        if not detail_text:
            detail_text = _page_text(response.text)
        combined_text = f"{_clean_text(card)} {detail_text}"
        return self._build_candidate(
            title=title,
            page_url=page_url,
            application_url=application_url,
            source_text=combined_text,
            open_quote=open_quote,
            today=today,
        )

    def _candidate(self, data_node, today: date) -> OpportunityCandidate | None:
        card = data_node.find_parent(class_=lambda value: value and "carousel-item" in value)
        if card is None:
            card = data_node.parent
        if card is None:
            return None
        title_node = card.find(["h2", "h3", "h4"])
        title = _clean_text(title_node) if title_node else ""
        card_text = _clean_text(card)
        page_path = str(data_node.get("data-current-page-url") or "").strip()
        page_url = urljoin(self.base_url, page_path)
        if not title or not page_path or "/programs/" not in page_path:
            return None

        apply_node = next(
            (
                anchor
                for anchor in card.select("a.js-program-url")
                if _clean_text(anchor) in {"قدّم الآن", "انضم الآن"}
            ),
            None,
        )
        if apply_node is None:
            return None
        application_value = str(
            apply_node.get("data-program-url")
            or data_node.get("data-external-application-form-url")
            or data_node.get("data-final-application-form-url")
            or ""
        ).strip()
        if not application_value or application_value.casefold().startswith("javascript:"):
            return None
        application_url = urljoin(self.base_url, application_value)

        return self._build_candidate(
            title=title,
            page_url=page_url,
            application_url=application_url,
            source_text=card_text,
            open_quote=_clean_text(apply_node),
            today=today,
        )

    def _build_candidate(
        self,
        *,
        title: str,
        page_url: str,
        application_url: str,
        source_text: str,
        open_quote: str,
        today: date,
    ) -> OpportunityCandidate | None:
        deadline, deadline_quote = _misk_deadline(source_text, today)
        if deadline and deadline < today:
            return None
        technical_quote = _misk_technical_quote(title, source_text)
        broad_major_quote = next(
            (
                marker
                for marker in ("جميع التخصصات التقنية", "التخصصات التقنية", "جميع التخصصات")
                if marker in source_text
            ),
            None,
        )
        if not technical_quote and not broad_major_quote:
            return None

        delivery_mode, city, delivery_quote = _misk_delivery(source_text)
        if delivery_mode is None or delivery_quote is None:
            return None
        if delivery_mode != DeliveryMode.ONLINE and not city:
            # An in-person opportunity without a stated city cannot be matched safely.
            return None

        opportunity_type = _misk_opportunity_type(title, source_text)
        accepted_majors = [broad_major_quote] if broad_major_quote else []
        evidence = [
            Evidence(
                field_name="organization",
                value="مؤسسة مسك",
                quote="مؤسسة مسك",
                source_url=page_url,
                official_source=True,
            ),
            Evidence(
                field_name="registration_status",
                value="open",
                quote=open_quote,
                source_url=page_url,
                official_source=True,
            ),
            Evidence(
                field_name="delivery_mode",
                value=delivery_mode.value,
                quote=delivery_quote,
                source_url=page_url,
                official_source=True,
            ),
        ]
        if technical_quote:
            evidence.append(
                Evidence(
                    field_name="technical_focus",
                    value="true",
                    quote=technical_quote,
                    source_url=page_url,
                    official_source=True,
                )
            )
        if broad_major_quote:
            evidence.append(
                Evidence(
                    field_name="accepted_majors",
                    value=broad_major_quote,
                    quote=broad_major_quote,
                    source_url=page_url,
                    official_source=True,
                )
            )
        if deadline and deadline_quote:
            evidence.append(
                Evidence(
                    field_name="deadline",
                    value=deadline.isoformat(),
                    quote=deadline_quote,
                    source_url=page_url,
                    official_source=True,
                )
            )
        if city:
            city_quote = "المملكة العربية السعودية" if city == "جميع مدن السعودية" else city
            evidence.append(
                Evidence(
                    field_name="city",
                    value=city,
                    quote=city_quote,
                    source_url=page_url,
                    official_source=True,
                )
            )
        return OpportunityCandidate(
            title=title,
            organization="مؤسسة مسك",
            opportunity_type=opportunity_type,
            city=city,
            delivery_mode=delivery_mode,
            accepted_majors=accepted_majors,
            deadline=deadline,
            registration_open=True,
            application_url=application_url,
            source_url=page_url,
            evidence=evidence,
            technical_focus=bool(technical_quote),
            remote_allowed=delivery_mode != DeliveryMode.IN_PERSON,
        )


class FinancialAcademyHackathonConnector:
    """First-party connector for the Financial Academy's current innovation hackathon."""

    page_url = (
        "https://fa.gov.sa/Services/ProgramDetails/"
        "ba1ee3c5-4255-407f-9e6e-b36d013b7df3"
    )

    def __init__(self, client: httpx.Client | None = None, *, timeout: float = 20) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "OpportunitySentinel/0.1 educational-research-bot"},
        )

    def collect(self, today: date | None = None) -> list[OpportunityCandidate]:
        today = today or date.today()
        response = self.client.get(self.page_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        text = _page_text(response.text)
        required_quotes = {
            "registration": "التسجيل",
            "cost": "مدعوم بالكامل",
            "city": "الرياض",
            "technical": "المطورين والمتخصصين في علم البيانات",
        }
        if any(quote not in text for quote in required_quotes.values()):
            return []
        if any(marker in text for marker in ("انتهى التسجيل", "التسجيل مغلق")):
            return []
        start_match = re.search(r"(\d{1,2})\s+سبتمبر\s+(\d{4})", text)
        if not start_match:
            return []
        start_date = date(int(start_match.group(2)), 9, int(start_match.group(1)))
        if start_date < today:
            return []
        registration_link = next(
            (
                anchor
                for anchor in soup.select("a[href]")
                if _clean_text(anchor) == "التسجيل"
            ),
            None,
        )
        if not registration_link:
            return []
        application_url = urljoin(self.page_url, str(registration_link["href"]))
        evidence = [
            Evidence(
                field_name=field,
                value=value,
                quote=quote,
                source_url=self.page_url,
                official_source=True,
            )
            for field, value, quote in [
                ("organization", "الأكاديمية المالية", "الأكاديمية المالية"),
                ("city", "الرياض", required_quotes["city"]),
                ("registration_status", "open", required_quotes["registration"]),
                ("cost", "free", required_quotes["cost"]),
                ("technical_focus", "true", required_quotes["technical"]),
            ]
        ]
        return [
            OpportunityCandidate(
                title="هاكاثون الابتكار في السوق المالية",
                organization="الأكاديمية المالية",
                opportunity_type=OpportunityType.HACKATHON,
                city="الرياض",
                delivery_mode=DeliveryMode.IN_PERSON,
                technical_focus=True,
                registration_open=True,
                application_url=application_url,
                source_url=self.page_url,
                evidence=evidence,
                start_date=start_date,
                is_free=True,
            )
        ]

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class KSUAlumniJobsConnector:
    """First-party connector for public jobs and training on KSU's alumni gate."""

    base_url = "https://alumnigate.ksu.edu.sa"
    listing_url = f"{base_url}/user/jobs"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        timeout: float = 20,
        max_pages: int = 20,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "OpportunitySentinel/0.1 educational-research-bot"},
        )
        self.max_pages = max_pages

    def collect(self, today: date | None = None) -> list[OpportunityCandidate]:
        today = today or date.today()
        urls = self._listing_urls()
        if not urls:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(urls))) as executor:
            candidates = list(executor.map(lambda url: self._read_job(url, today), urls))
        return [candidate for candidate in candidates if candidate is not None]

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _listing_urls(self) -> list[str]:
        urls: set[str] = set()
        for page_number in range(1, self.max_pages + 1):
            response = self.client.get(self.listing_url, params={"page": page_number})
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            page_urls = {
                urljoin(self.base_url, str(anchor["href"]))
                for anchor in soup.select('a[href*="/user/jobs/details/"]')
            }
            if not page_urls - urls:
                break
            urls.update(page_urls)
        return sorted(urls)

    def _read_job(self, url: str, today: date) -> OpportunityCandidate | None:
        response = self.client.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        main = soup.select_one("#ajax_table")
        if not main:
            return None

        status_node = main.select_one("header.card a.g-btn")
        status = _clean_text(status_node) if status_node else ""
        if status != "قدم الآن":
            return None
        title_node = main.select_one('a[href*="/user/jobs/details/"] span.fs-1')
        organization_node = main.select_one("header.card section.d-flex .m-0 a")
        if not title_node or not organization_node:
            return None
        title = _clean_text(title_node)
        organization = _clean_text(organization_node)
        description_node = main.select_one(".decription_details")
        description = _clean_text(description_node) if description_node else ""
        attributes = _ksu_job_attributes(main)
        majors = _normalized_ksu_majors(attributes.get("التخصص", ""))
        colleges = _split_values(attributes.get("الكلية", ""))
        opportunity_type = _ksu_opportunity_type(title, description)
        technical_quote = _ksu_technical_quote(
            title,
            description,
            colleges,
            majors,
            opportunity_type,
        )
        if not technical_quote:
            return None

        location_quote = attributes.get("الموقع", "")
        city, delivery_mode = _ksu_location(location_quote)
        if delivery_mode != DeliveryMode.ONLINE and city != "الرياض":
            return None
        publication_date = _ksu_publication_date(main)
        if publication_date and publication_date > today:
            return None

        evidence = [
            Evidence(
                field_name="organization",
                value=organization,
                quote=organization,
                source_url=url,
                official_source=True,
            ),
            Evidence(
                field_name="registration_status",
                value="open",
                quote=status,
                source_url=url,
                official_source=True,
            ),
            Evidence(
                field_name="technical_focus",
                value="true",
                quote=technical_quote,
                source_url=url,
                official_source=True,
            ),
        ]
        if city:
            evidence.append(
                Evidence(
                    field_name="city",
                    value=city,
                    quote=location_quote,
                    source_url=url,
                    official_source=True,
                )
            )
        if majors:
            evidence.append(
                Evidence(
                    field_name="accepted_majors",
                    value=", ".join(majors),
                    quote=attributes.get("التخصص", ""),
                    source_url=url,
                    official_source=True,
                )
            )
        if publication_date:
            evidence.append(
                Evidence(
                    field_name="publication_date",
                    value=publication_date.isoformat(),
                    quote=publication_date.isoformat(),
                    source_url=url,
                    official_source=True,
                )
            )
        return OpportunityCandidate(
            title=title,
            organization=organization,
            opportunity_type=opportunity_type,
            city=city,
            delivery_mode=delivery_mode,
            accepted_majors=majors,
            technical_focus=True,
            registration_open=True,
            application_url=url,
            source_url=url,
            evidence=evidence,
            publication_date=publication_date,
            remote_allowed=delivery_mode != DeliveryMode.IN_PERSON,
        )


class KSUOfficialNewsConnector:
    """Scan KSU's official news feed for still-applicable technical opportunities."""

    base_url = "https://news.ksu.edu.sa"
    listing_url = f"{base_url}/ar/node"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        timeout: float = 20,
        max_pages: int = 3,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "OpportunitySentinel/0.1 educational-research-bot"},
        )
        self.max_pages = max_pages

    def collect(self, today: date | None = None) -> list[OpportunityCandidate]:
        today = today or date.today()
        urls = self._listing_urls()
        if not urls:
            return []
        with ThreadPoolExecutor(max_workers=min(6, len(urls))) as executor:
            rows = list(executor.map(lambda url: self._read_page(url, today), urls))
        return [candidate for candidate in rows if candidate is not None]

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _listing_urls(self) -> list[str]:
        urls: set[str] = set()
        for page_number in range(self.max_pages):
            response = self.client.get(
                self.listing_url,
                params={"page": page_number, "quicktabs_2": 0},
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            found = {
                urljoin(self.base_url, str(anchor["href"]))
                for anchor in soup.select('a[href^="/ar/node/"]')
                if re.fullmatch(r"/ar/node/\d+", str(anchor.get("href") or ""))
            }
            if not found - urls:
                break
            urls.update(found)
        return sorted(urls)

    def _read_page(self, url: str, today: date) -> OpportunityCandidate | None:
        response = self.client.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        article = soup.find("article")
        if article is None:
            return None
        title_node = article.find("h1")
        title = _clean_text(title_node) if title_node else ""
        text = _clean_text(article)
        if not title or any(
            marker in text
            for marker in ("انتهى التسجيل", "التسجيل مغلق", "اختتمت", "أقيمت الفعالية")
        ):
            return None
        technical_quote = _technical_quote(title, text)
        if not technical_quote:
            return None
        application_link = next(
            (
                anchor
                for anchor in article.select("a[href]")
                if re.fullmatch(
                    r"(?:رابط\s+)?(?:التسجيل|التقديم|المشاركة)|"
                    r"(?:سجل|سجّل|قدّم|شارك)\s+الآن",
                    _clean_text(anchor),
                )
                and urljoin(url, str(anchor.get("href"))) != url
            ),
            None,
        )
        if application_link is None:
            return None
        open_quote = _clean_text(application_link)
        application_url = urljoin(url, str(application_link["href"]))

        dates = _arabic_text_dates(text)
        publication_date = dates[0] if dates else None
        future_dates = [value for value in dates if value >= today]
        if not future_dates and (
            publication_date is None or (today - publication_date).days > 30
        ):
            return None
        start_date = max(future_dates) if future_dates else None
        if "عن بعد" in text or "افتراضي" in text:
            delivery_mode, city = DeliveryMode.ONLINE, None
            location_quote = "عن بعد" if "عن بعد" in text else "افتراضي"
        elif "الرياض" in text or "جامعة الملك سعود" in text:
            delivery_mode, city = DeliveryMode.IN_PERSON, "الرياض"
            location_quote = "جامعة الملك سعود"
        else:
            delivery_mode, city = DeliveryMode.UNKNOWN, None
            location_quote = None
        broad_major_quote = next(
            (
                marker
                for marker in ("جميع التخصصات التقنية", "التخصصات التقنية", "جميع التخصصات")
                if marker in text
            ),
            None,
        )
        accepted_majors = [broad_major_quote] if broad_major_quote else []
        evidence = [
            Evidence(
                field_name="organization",
                value="جامعة الملك سعود",
                quote="جامعة الملك سعود",
                source_url=url,
                official_source=True,
            ),
            Evidence(
                field_name="registration_status",
                value="open",
                quote=open_quote,
                source_url=url,
                official_source=True,
            ),
            Evidence(
                field_name="technical_focus",
                value="true",
                quote=technical_quote,
                source_url=url,
                official_source=True,
            ),
        ]
        if city and location_quote:
            evidence.append(
                Evidence(
                    field_name="city",
                    value=city,
                    quote=location_quote,
                    source_url=url,
                    official_source=True,
                )
            )
        if broad_major_quote:
            evidence.append(
                Evidence(
                    field_name="accepted_majors",
                    value=broad_major_quote,
                    quote=broad_major_quote,
                    source_url=url,
                    official_source=True,
                )
            )
        return OpportunityCandidate(
            title=title,
            organization="جامعة الملك سعود",
            opportunity_type=_ksu_news_type(title, text),
            city=city,
            delivery_mode=delivery_mode,
            accepted_majors=accepted_majors,
            registration_open=True,
            application_url=application_url,
            source_url=url,
            evidence=evidence,
            publication_date=publication_date,
            start_date=start_date,
            technical_focus=True,
            remote_allowed=(True if delivery_mode == DeliveryMode.ONLINE else None),
        )


@dataclass(frozen=True)
class ATSBoard:
    """A public first-party applicant-tracking-system job board."""

    provider: str
    slug: str
    organization: str
    source_id: str | None = None


class PublicATSConnector:
    """Collect student and early-career technical roles from official ATS APIs.

    The connector talks directly to the same public JSON endpoints used by the
    employer's careers page. It does not scrape LinkedIn or bypass authentication.
    """

    def __init__(
        self,
        boards: list[ATSBoard],
        client: httpx.Client | None = None,
        *,
        timeout: float = 20,
    ) -> None:
        self.boards = boards
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "OpportunitySentinel/0.1 educational-research-bot"},
        )

    def collect(self) -> list[OpportunityCandidate]:
        candidates: dict[str, OpportunityCandidate] = {}
        for board in self.boards:
            try:
                rows = self._fetch_board(board)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                continue
            for row in rows:
                candidate = self._candidate(board, row)
                if candidate:
                    candidates[str(candidate.application_url)] = candidate
        return list(candidates.values())

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _fetch_board(self, board: ATSBoard) -> list[dict[str, Any]]:
        provider = board.provider.casefold()
        if provider == "ashby":
            response = self.client.get(
                f"https://api.ashbyhq.com/posting-api/job-board/{board.slug}"
            )
            response.raise_for_status()
            return [row for row in response.json().get("jobs", []) if row.get("isListed", True)]
        if provider == "lever":
            response = self.client.get(
                f"https://api.lever.co/v0/postings/{board.slug}",
                params={"mode": "json", "limit": 100},
            )
            response.raise_for_status()
            body = response.json()
            return body if isinstance(body, list) else []
        if provider == "greenhouse":
            response = self.client.get(
                f"https://boards-api.greenhouse.io/v1/boards/{board.slug}/jobs",
                params={"content": "true"},
            )
            response.raise_for_status()
            return response.json().get("jobs", [])
        return []

    def _candidate(
        self, board: ATSBoard, row: dict[str, Any]
    ) -> OpportunityCandidate | None:
        normalized = _normalized_ats_row(board, row)
        title = normalized["title"]
        body = normalized["description"]
        location = normalized["location"]
        application_url = normalized["application_url"]
        source_url = normalized["source_url"]
        if not all((title, application_url, source_url)):
            return None
        opportunity_type = _ats_opportunity_type(
            f"{title} {normalized['employment_type']}"
        ) or _ats_body_opportunity_type(body)
        if opportunity_type is None:
            return None
        technical_quote = _ats_technical_quote(title, body, normalized["department"])
        if not technical_quote:
            return None
        if any(
            marker in normalized["workplace_type"].casefold()
            for marker in ("remote", "عن بعد")
        ) and not _ats_remote_scope_allowed(location):
            return None
        city, delivery_mode = _ats_location(location, normalized["workplace_type"])
        if delivery_mode != DeliveryMode.ONLINE and city != "الرياض":
            return None
        status_quote = normalized["status_quote"]
        evidence = [
            Evidence(
                field_name="organization",
                value=board.organization,
                quote=board.organization,
                source_url=source_url,
                official_source=True,
            ),
            Evidence(
                field_name="registration_status",
                value="open",
                quote=status_quote,
                source_url=source_url,
                official_source=True,
            ),
            Evidence(
                field_name="technical_focus",
                value="true",
                quote=technical_quote,
                source_url=source_url,
                official_source=True,
            ),
        ]
        if city:
            evidence.append(
                Evidence(
                    field_name="city",
                    value=city,
                    quote=location or normalized["workplace_type"],
                    source_url=source_url,
                    official_source=True,
                )
            )
        publication_date = _iso_date(normalized["publication_date"])
        return OpportunityCandidate(
            title=title[:300],
            organization=board.organization,
            opportunity_type=opportunity_type,
            city=city,
            delivery_mode=delivery_mode,
            technical_focus=True,
            registration_open=True,
            application_url=application_url,
            source_url=source_url,
            evidence=evidence,
            publication_date=publication_date,
            remote_allowed=delivery_mode != DeliveryMode.IN_PERSON,
        )


def default_ats_boards() -> list[ATSBoard]:
    """Curated official boards with a Saudi/Riyadh presence."""
    return [
        ATSBoard("ashby", "sarjai", "Sarj.ai", "ats-sarjai"),
        ATSBoard("ashby", "LeanTech", "Lean Technologies", "ats-leantech"),
        ATSBoard("lever", "trendyol", "Trendyol", "ats-trendyol"),
        ATSBoard("lever", "infinitepl", "Infinite PL", "ats-infinitepl"),
        ATSBoard("greenhouse", "tamara", "Tamara", "ats-tamara"),
        ATSBoard("greenhouse", "hala", "HALA", "ats-hala"),
        ATSBoard("lever", "tsmg", "TSMG", "ats-tsmg"),
        ATSBoard("greenhouse", "canonical", "Canonical", "ats-canonical"),
        ATSBoard("greenhouse", "careem", "Careem", "ats-careem"),
    ]


def extract_linkedin_technical_training(page: SourcePage) -> OpportunityCandidate | None:
    """Validate an active employer-controlled public LinkedIn training listing."""
    folded_url = page.url.casefold()
    content = page.content
    folded = content.casefold()
    if "linkedin.com/jobs/view" not in folded_url:
        return None
    if any(
        marker in folded
        for marker in (
            "لم نعد نقبل طلبات التقدم",
            "no longer accepting applications",
            "job is no longer available",
        )
    ):
        return None
    open_quote = next(
        (
            marker
            for marker in ("Apply", "التقدم", "انضم للتقدم")
            if marker.casefold() in folded
        ),
        None,
    )
    technical_quote = next(
        (
            marker
            for marker in (
                "Computer Science",
                "Information Technology",
                "Software Engineering",
                "Data Science",
                "Artificial Intelligence",
                "Cybersecurity",
                "computer engineering",
            )
            if marker.casefold() in folded
        ),
        None,
    )
    training_marker = next(
        (marker for marker in ("COOP", "Co-op", "Internship") if marker.casefold() in folded),
        None,
    )
    city_quote = next(
        (marker for marker in ("Riyadh", "الرياض") if marker.casefold() in folded), None
    )
    if not all((open_quote, technical_quote, training_marker, city_quote)):
        return None
    organization, title = _linkedin_title_parts(page.title)
    if not organization or not title:
        return None
    opportunity_type = (
        OpportunityType.COOP
        if any(marker in folded for marker in ("coop", "co-op", "تعاوني"))
        else OpportunityType.INTERNSHIP
    )
    evidence = [
        Evidence(
            field_name=field,
            value=value,
            quote=quote,
            source_url=page.url,
            # LinkedIn job pages are controlled by the named employer and expose the
            # live application state. They are trusted recruitment-platform evidence.
            official_source=True,
        )
        for field, value, quote in [
            ("organization", organization, organization),
            ("city", "الرياض", city_quote),
            ("registration_status", "open", open_quote),
            ("technical_focus", "true", technical_quote),
        ]
    ]
    return OpportunityCandidate(
        title=title,
        organization=organization,
        opportunity_type=opportunity_type,
        city="الرياض",
        delivery_mode=DeliveryMode.IN_PERSON,
        technical_focus=True,
        registration_open=True,
        application_url=page.url,
        source_url=page.url,
        evidence=evidence,
    )


def _linkedin_title_parts(value: str) -> tuple[str | None, str | None]:
    normalized = value.replace("\u200f", "").replace("\u200e", "").replace("‏", "").strip()
    english = re.match(r"(.+?)\s+hiring\s+(.+?)\s+in\s+", normalized, re.I)
    if english:
        return english.group(1).strip(), english.group(2).strip()
    arabic = re.match(r"تقوم شركة\s+(.+?)\s+بالتوظيف لوظيفة\s+(.+?)\s+في\s+", normalized)
    if arabic:
        return arabic.group(1).strip(), arabic.group(2).strip()
    return None, None


def _normalized_ats_row(board: ATSBoard, row: dict[str, Any]) -> dict[str, str]:
    provider = board.provider.casefold()
    if provider == "ashby":
        locations = [str(row.get("location") or "")]
        locations.extend(
            str(item.get("location") or "")
            for item in row.get("secondaryLocations", [])
            if isinstance(item, dict)
        )
        return {
            "title": str(row.get("title") or "").strip(),
            "description": _plain_text(
                str(row.get("descriptionPlain") or row.get("descriptionHtml") or "")
            ),
            "location": ", ".join(item for item in locations if item),
            "department": " ".join(
                str(row.get(key) or "") for key in ("department", "team")
            ),
            "employment_type": str(row.get("employmentType") or ""),
            "workplace_type": str(row.get("workplaceType") or ""),
            "application_url": str(row.get("applyUrl") or row.get("jobUrl") or ""),
            "source_url": str(row.get("jobUrl") or row.get("applyUrl") or ""),
            "publication_date": str(row.get("publishedAt") or ""),
            "status_quote": "isListed: true",
        }
    if provider == "lever":
        categories = row.get("categories") if isinstance(row.get("categories"), dict) else {}
        list_text = " ".join(
            _plain_text(str(item.get("content") or ""))
            for item in row.get("lists", [])
            if isinstance(item, dict)
        )
        return {
            "title": str(row.get("text") or "").strip(),
            "description": _plain_text(
                " ".join(
                    [
                        str(row.get("descriptionPlain") or row.get("description") or ""),
                        list_text,
                    ]
                )
            ),
            "location": str(categories.get("location") or ""),
            "department": " ".join(
                str(categories.get(key) or "") for key in ("department", "team")
            ),
            "employment_type": str(categories.get("commitment") or ""),
            "workplace_type": str(row.get("workplaceType") or ""),
            "application_url": str(row.get("applyUrl") or row.get("hostedUrl") or ""),
            "source_url": str(row.get("hostedUrl") or row.get("applyUrl") or ""),
            "publication_date": "",
            "status_quote": "applyUrl: " + str(row.get("applyUrl") or "open"),
        }
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    return {
        "title": str(row.get("title") or "").strip(),
        "description": _plain_text(str(row.get("content") or "")),
        "location": str(location.get("name") or ""),
        "department": " ".join(
            str(item.get("name") or "")
            for item in row.get("departments", [])
            if isinstance(item, dict)
        ),
        "employment_type": "",
        "workplace_type": "",
        "application_url": str(row.get("absolute_url") or ""),
        "source_url": str(row.get("absolute_url") or ""),
        "publication_date": str(row.get("updated_at") or ""),
        "status_quote": "absolute_url: " + str(row.get("absolute_url") or "open"),
    }


def _plain_text(value: str) -> str:
    return " ".join(BeautifulSoup(unescape(value), "html.parser").get_text(" ", strip=True).split())


def _ats_opportunity_type(text: str) -> OpportunityType | None:
    folded = text.casefold()
    if any(marker in folded for marker in ("coop", "co-op", "cooperative training", "تعاوني")):
        return OpportunityType.COOP
    if any(marker in folded for marker in ("part-time", "part time", "دوام جزئي")):
        return OpportunityType.PART_TIME_JOB
    if any(
        marker in folded
        for marker in (
            "graduate program",
            "graduate programme",
            "graduate level",
            "new grad",
            "fresh graduate",
            "تمهير",
            "خريجين",
        )
    ):
        return OpportunityType.GRADUATE_PROGRAM
    internship_markers = ("internship", "intern ", "intern,", "intern-", "متدرب", "تدريب")
    if any(marker in folded for marker in internship_markers):
        return OpportunityType.INTERNSHIP
    if any(marker in folded for marker in ("entry level", "entry-level", "junior", "مبتدئ")):
        return OpportunityType.ENTRY_LEVEL_JOB
    return None


def _ats_body_opportunity_type(text: str) -> OpportunityType | None:
    """Use only unambiguous programme wording from the body.

    Employer boilerplate often mentions graduates or entry-level compensation on every
    posting. Broad words must therefore never classify an otherwise senior headline.
    """
    folded = text.casefold()
    if any(marker in folded for marker in ("co-op training", "coop training", "تدريب تعاوني")):
        return OpportunityType.COOP
    if any(marker in folded for marker in ("tamheer", "تمهير", "builders program")):
        return OpportunityType.GRADUATE_PROGRAM
    if any(
        marker in folded
        for marker in ("internship programme", "internship program", "summer internship")
    ):
        return OpportunityType.INTERNSHIP
    return None


def _ats_technical_quote(title: str, description: str, department: str) -> str | None:
    headline = f"{title} {department}"
    folded_headline = headline.casefold()
    headline_markers = (
        "software",
        "developer",
        "engineer",
        "quality assurance",
        "system operations",
        "data",
        "machine learning",
        "artificial intelligence",
        "cyber",
        "cloud",
        "linux",
        "ubuntu",
        "technical",
        "هندسة البرمجيات",
        "علوم الحاسب",
        "هندسة الحاسب",
        "تقنية المعلومات",
        "ذكاء اصطناعي",
        "علم البيانات",
        "أمن سيبراني",
        "برمجة",
    )
    marker = next((item for item in headline_markers if item in folded_headline), None)
    if marker:
        start = max(0, folded_headline.find(marker) - 80)
        return headline[start : start + 500]

    nontechnical_titles = (
        "human resources",
        "communications",
        "marketing",
        "campaign",
        "ads specialist",
        "financial controller",
        "accountant",
        "procurement",
        "customer care",
        "legal",
        "product manager",
        "project manager",
        "business services",
        "talent scientist",
        "موارد بشرية",
        "تسويق",
        "محاسب",
        "مشتريات",
    )
    if any(marker in title.casefold() for marker in nontechnical_titles):
        return None
    folded_body = description.casefold()
    body_markers = (
        "ai products",
        "ai-powered product",
        "software testing",
        "degree in computer science",
        "computer science or related",
        "computer engineering",
        "information technology",
        "data science",
        "data engineering",
        "machine learning",
        "artificial intelligence",
        "cybersecurity",
        "cyber security",
        "programming experience",
        "python",
        "sql",
        "علوم الحاسب",
        "هندسة الحاسب",
        "تقنية المعلومات",
        "علم البيانات",
        "ذكاء اصطناعي",
        "أمن سيبراني",
        "برمجة",
    )
    marker = next((item for item in body_markers if item in folded_body), None)
    if not marker:
        return None
    start = max(0, folded_body.find(marker) - 80)
    return description[start : start + 500]


def _ats_location(location: str, workplace_type: str) -> tuple[str | None, DeliveryMode]:
    text = f"{location} {workplace_type}".casefold()
    if any(marker in text for marker in ("remote", "home based", "home-based", "عن بعد")):
        return "عن بُعد", DeliveryMode.ONLINE
    if any(marker in text for marker in ("riyadh", "الرياض", "diriyah", "الدرعية")):
        return "الرياض", DeliveryMode.IN_PERSON
    return None, DeliveryMode.IN_PERSON


def _ats_remote_scope_allowed(location: str) -> bool:
    """Reject remote jobs that are explicitly restricted to a foreign market."""
    folded = location.casefold().strip()
    if not folded:
        return True
    return any(
        marker in folded
        for marker in (
            "remote",
            "anywhere",
            "worldwide",
            "global",
            "saudi",
            "ksa",
            "riyadh",
            "middle east",
            "mena",
            "عن بعد",
            "السعودية",
            "الرياض",
            "الشرق الأوسط",
        )
    )


def _iso_date(value: str) -> date | None:
    match = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _clean_text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def _ksu_job_attributes(main) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for row in main.select("div.d-flex.flex-stack"):
        label = row.select_one(":scope > div.text-gray-700")
        value = row.select_one(":scope > div.d-flex span")
        if label and value:
            attributes.setdefault(_clean_text(label), _clean_text(value))
    return attributes


def _split_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip() and item.strip() != "--"]


def _normalized_ksu_majors(value: str) -> list[str]:
    normalized: list[str] = []
    for raw in _split_values(value):
        folded = raw.casefold()
        if "هندسة البرمجيات" in folded:
            item = "هندسة البرمجيات"
        elif "هندسة الحاسب" in folded or "هندسة حاسب" in folded:
            item = "هندسة الحاسب"
        elif "الأمن السيبراني" in folded or "الامن السيبراني" in folded:
            item = "الأمن السيبراني"
        elif "تقنية المعلومات" in folded:
            item = "تقنية المعلومات"
        elif "علم البيانات" in folded:
            item = "علم البيانات"
        elif "الذكاء الاصطناعي" in folded:
            item = "الذكاء الاصطناعي"
        elif "علوم الحاسب" in folded and "نظم المعلومات" not in folded:
            item = "علوم الحاسب"
        else:
            item = raw
        if item not in normalized:
            normalized.append(item)
    return normalized


def _ksu_opportunity_type(title: str, description: str) -> OpportunityType:
    text = f"{title} {description}".casefold()
    if any(marker in text for marker in ("تدريب تعاوني", "coop", "co-op")):
        return OpportunityType.COOP
    if any(marker in text for marker in ("دوام جزئي", "part-time", "part time")):
        return OpportunityType.PART_TIME_JOB
    if any(
        marker in text
        for marker in ("تمهير", "برنامج خريجين", "تطوير الخريجين", "graduate program")
    ):
        return OpportunityType.GRADUATE_PROGRAM
    if any(marker in text for marker in ("internship", "intern ", "تدريب", "متدرب")):
        return OpportunityType.INTERNSHIP
    return OpportunityType.ENTRY_LEVEL_JOB


def _ksu_technical_quote(
    title: str,
    description: str,
    colleges: list[str],
    majors: list[str],
    opportunity_type: OpportunityType,
) -> str | None:
    folded_all = f"{title} {description}".casefold()
    non_computing_roles = (
        "مبيعات تقنية",
        "مبيعات تقنيه",
        "أنظمة انذار",
        "انظمة انذار",
        "إنذار الحريق",
        "انذار الحريق",
        "medical technology",
    )
    if any(marker in folded_all for marker in non_computing_roles):
        return None
    title_markers = (
        "برمجيات",
        "مبرمج",
        "علوم الحاسب",
        "هندسة الحاسب",
        "تقنية المعلومات",
        "نظم المعلومات",
        "حماية بيانات",
        "تحليل بيانات",
        "هندسة بيانات",
        "علم البيانات",
        "ذكاء اصطناعي",
        "سيبراني",
        "شبكات",
        "دعم فني",
        "إلكترون",
        "software",
        "computer",
        "information technology",
        "cyber",
        "machine learning",
        "artificial intelligence",
        "developer",
        "programmer",
        "network engineer",
        "data engineer",
        "data analyst",
    )
    if any(marker in title.casefold() for marker in title_markers):
        return title[:1000]

    description_markers = (
        "تطوير البرمجيات",
        "برمجة",
        "علوم الحاسب",
        "هندسة الحاسب",
        "تقنية المعلومات",
        "نظم المعلومات",
        "قواعد البيانات",
        "تحليل البيانات",
        "هندسة البيانات",
        "علم البيانات",
        "الذكاء الاصطناعي",
        "الأمن السيبراني",
        "الشبكات",
        "software engineering",
        "computer science",
        "information technology",
        "cybersecurity",
        "machine learning",
        "artificial intelligence",
        "data engineering",
        "data analysis",
    )
    if any(marker in description.casefold() for marker in description_markers):
        return description[:1000]

    technical_majors = [
        major
        for major in majors
        if any(marker in major.casefold() for marker in title_markers)
    ]
    training_types = {
        OpportunityType.INTERNSHIP,
        OpportunityType.COOP,
        OpportunityType.GRADUATE_PROGRAM,
    }
    if opportunity_type in training_types and technical_majors:
        ratio = len(technical_majors) / max(1, len(majors))
        technical_college = any("علوم الحاسب" in college for college in colleges)
        if ratio >= 0.75 or (technical_college and ratio >= 0.5):
            return ", ".join(technical_majors)[:1000]
    return None


_ARABIC_MONTHS = {
    "يناير": 1,
    "فبراير": 2,
    "مارس": 3,
    "أبريل": 4,
    "ابريل": 4,
    "مايو": 5,
    "يونيو": 6,
    "يوليو": 7,
    "أغسطس": 8,
    "اغسطس": 8,
    "سبتمبر": 9,
    "أكتوبر": 10,
    "اكتوبر": 10,
    "نوفمبر": 11,
    "ديسمبر": 12,
}


def _misk_deadline(text: str, today: date) -> tuple[date | None, str | None]:
    match = re.search(
        r"(?:إغلاق باب التقديم في|انتهاء التقديم)\s+(\d{1,2})\s+"
        r"(يناير|فبراير|مارس|أبريل|ابريل|مايو|يونيو|يوليو|أغسطس|اغسطس|"
        r"سبتمبر|أكتوبر|اكتوبر|نوفمبر|ديسمبر)(?:\s+(\d{4}))?",
        text,
    )
    if not match:
        return None, None
    year = int(match.group(3) or today.year)
    value = date(year, _ARABIC_MONTHS[match.group(2)], int(match.group(1)))
    # A year-less date in January after a December crawl belongs to next year.
    if not match.group(3) and value < today and today.month >= 10:
        value = value.replace(year=year + 1)
    return value, match.group(0)


def _technical_quote(title: str, text: str) -> str | None:
    markers = (
        "تقني",
        "برمج",
        "الذكاء الاصطناعي",
        "بيانات",
        "الأمن السيبراني",
        "الحوسبة",
        "سحابي",
        "تطوير التطبيقات",
        "تطوير الويب",
        "software",
        "artificial intelligence",
        "data",
        "cyber",
        "cloud",
    )
    for value in (title, text):
        folded = value.casefold()
        marker = next((item for item in markers if item in folded), None)
        if marker:
            start = max(0, folded.find(marker) - 200)
            return value[start : start + 1000]
    return None


def _misk_technical_quote(title: str, text: str) -> str | None:
    """Require a computing-specific phrase, not a generic use of technology or data."""
    title_markers = (
        "تقني",
        "برمج",
        "الذكاء الاصطناعي",
        "بيانات",
        "الأمن السيبراني",
        "الحوسبة السحابية",
        "software",
        "artificial intelligence",
        "data analyst",
        "data science",
        "cyber",
        "cloud",
    )
    folded_title = title.casefold()
    marker = next((item for item in title_markers if item in folded_title), None)
    if marker:
        start = max(0, folded_title.find(marker) - 200)
        return title[start : start + 1000]

    # A broad traineeship is technical when its own fields explicitly include
    # technology. Prefer this direct eligibility evidence over testimonials.
    training_fields = re.search(r"مجالات التدريب.{0,300}التكنولوجيا", text, re.DOTALL)
    if training_fields:
        return training_fields.group(0)[:1000]

    body_markers = (
        "المهارات التقنية",
        "مطور برمجيات",
        "هندسة البرمجيات",
        "علوم البيانات",
        "تحليل البيانات",
        "الأمن السيبراني",
        "الحوسبة السحابية",
        "الذكاء الاصطناعي",
        "software engineering",
        "software development",
        "data science",
        "data analysis",
        "cybersecurity",
        "cloud computing",
        "artificial intelligence",
    )
    folded_text = text.casefold()
    marker = next((item for item in body_markers if item in folded_text), None)
    if marker:
        start = max(0, folded_text.find(marker) - 200)
        return text[start : start + 1000]
    return None


def _misk_delivery(text: str) -> tuple[DeliveryMode | None, str | None, str | None]:
    city = next(
        (value for value in ("الرياض", "جدة", "الدمام", "الخبر") if value in text),
        None,
    )
    if city is None and any(
        marker in text for marker in ("المملكة العربية السعودية", "Saudi Arabia")
    ):
        city = "جميع مدن السعودية"
    if "التعليم المدمج" in text:
        return DeliveryMode.HYBRID, city, "التعليم المدمج"
    if "حضوري" in text:
        return DeliveryMode.IN_PERSON, city, "حضوري"
    if "عن بعد" in text:
        return DeliveryMode.ONLINE, None, "عن بعد"
    for marker in ("عبر الإنترنت", "أونلاين", "Online", "online"):
        if marker in text:
            return DeliveryMode.ONLINE, None, marker
    return None, None, None


def _misk_opportunity_type(title: str, _text: str) -> OpportunityType:
    folded_title = title.casefold()
    if "هاكاثون" in folded_title or "hackathon" in folded_title:
        return OpportunityType.HACKATHON
    if "معسكر" in folded_title or "bootcamp" in folded_title:
        return OpportunityType.BOOTCAMP
    if any(
        marker in folded_title
        for marker in ("تدريب على رأس العمل", "تدريب عملي", "internship", "traineeship")
    ):
        return OpportunityType.INTERNSHIP
    if any(marker in folded_title for marker in ("مسابقة", "تحدي")):
        return OpportunityType.COMPETITION
    if any(marker in folded_title for marker in ("فعالية", "ورشة", "event")):
        return OpportunityType.EVENT
    return OpportunityType.COURSE


def _arabic_text_dates(text: str) -> list[date]:
    month_pattern = "|".join(sorted(_ARABIC_MONTHS, key=len, reverse=True))
    found: list[date] = []
    for match in re.finditer(rf"(\d{{1,2}})\s+({month_pattern})\s+(\d{{4}})", text):
        try:
            value = date(int(match.group(3)), _ARABIC_MONTHS[match.group(2)], int(match.group(1)))
        except ValueError:
            continue
        if value not in found:
            found.append(value)
    for match in re.finditer(rf"({month_pattern})\s+(\d{{1,2}})[،,]?\s+(\d{{4}})", text):
        try:
            value = date(int(match.group(3)), _ARABIC_MONTHS[match.group(1)], int(match.group(2)))
        except ValueError:
            continue
        if value not in found:
            found.append(value)
    return found


def _ksu_news_type(title: str, text: str) -> OpportunityType:
    folded = f"{title} {text}".casefold()
    if "هاكاثون" in folded:
        return OpportunityType.HACKATHON
    if any(marker in folded for marker in ("مسابقة", "تحدي")):
        return OpportunityType.COMPETITION
    if any(marker in folded for marker in ("تدريب تعاوني", "coop")):
        return OpportunityType.COOP
    if any(marker in folded for marker in ("تدريب", "internship")):
        return OpportunityType.INTERNSHIP
    if any(marker in folded for marker in ("فعالية", "ورشة", "ملتقى", "معرض")):
        return OpportunityType.EVENT
    return OpportunityType.COURSE


def _ksu_location(value: str) -> tuple[str | None, DeliveryMode]:
    folded = value.casefold()
    if any(marker in folded for marker in ("remote", "عن بعد")):
        return "عن بُعد", DeliveryMode.ONLINE
    if any(marker in folded for marker in ("riyadh", "الرياض", "diriyah", "الدرعية")):
        return "الرياض", DeliveryMode.IN_PERSON
    return None, DeliveryMode.IN_PERSON


def _ksu_publication_date(main) -> date | None:
    match = re.search(r"تم النشر\s*(\d{4}-\d{2}-\d{2})", _clean_text(main))
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def _course_title(soup: BeautifulSoup, fallback: str) -> str:
    if not soup.title or not soup.title.string:
        return fallback
    return soup.title.string.split(" | ", 1)[0].strip()


def _provider_name(soup: BeautifulSoup) -> str | None:
    label = soup.find(string=lambda value: bool(value and "مقدم من:" in value))
    if not label:
        return None
    next_span = label.parent.find_next("span") if label.parent else None
    return next_span.get_text(" ", strip=True) if next_span else None


def _arabic_course_date(value: str) -> date:
    return datetime.strptime(value, "%d-%m-%Y").date()


def _delivery_quote(text: str) -> str | None:
    for marker in ("طريقة توصيل الدورة تفاعلية مباشرة", "تفاعلية مباشرة"):
        if marker in text:
            return marker
    return None


def _requirements(text: str) -> list[tuple[str, str]]:
    known = [
        ("saudi_national", "سعودي الجنسية"),
        ("degree_level", "دبلوم وما اعلى"),
        ("degree_level", "دبلوم وما أعلى"),
        ("english_level", "لغة انجليزية متوسطة"),
        ("english_level", "لغة إنجليزية متوسطة"),
        ("has_computer", "جهاز كومبيوتر"),
        ("has_computer", "جهاز كمبيوتر"),
    ]
    found: dict[str, str] = {}
    for key, quote in known:
        if quote in text:
            found.setdefault(key, quote)
    return list(found.items())


def _requirement_rules(
    requirements: list[tuple[str, str]],
) -> list[tuple[str, str, bool | list[str]]]:
    values: dict[str, bool | list[str]] = {
        "saudi_national": True,
        "degree_level": ["diploma", "bachelor", "graduate"],
        "english_level": ["intermediate", "advanced"],
        "has_computer": True,
    }
    return [(key, quote, values[key]) for key, quote in requirements]
