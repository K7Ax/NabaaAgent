from __future__ import annotations

import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from ipaddress import ip_address
from socket import getaddrinfo
from typing import Protocol
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

from opportunity_sentinel.logging import logger
from opportunity_sentinel.models import ToolObservation


@dataclass(frozen=True)
class SourcePage:
    url: str
    title: str
    content: str
    official: bool = False


class ResearchTools(Protocol):
    def search_web(self, query: str) -> tuple[list[SourcePage], ToolObservation]: ...

    def open_page(self, url: str) -> tuple[SourcePage | None, ToolObservation]: ...


class InMemoryResearchTools:
    """Deterministic tool implementation for development, tests, and rubric evidence."""

    def __init__(self, pages: list[SourcePage]) -> None:
        self.pages = {page.url: page for page in pages}
        self.search_calls = 0

    def search_web(self, query: str) -> tuple[list[SourcePage], ToolObservation]:
        started = time.perf_counter()
        self.search_calls += 1
        terms = {term.casefold() for term in query.split() if len(term) > 2}
        ranked = sorted(
            self.pages.values(),
            key=lambda page: len(terms.intersection(page.content.casefold().split())),
            reverse=True,
        )
        elapsed = (time.perf_counter() - started) * 1000
        observation = ToolObservation(
            tool="search_web",
            success=True,
            detail=f"Found {len(ranked)} candidate pages",
            latency_ms=elapsed,
            metadata={"query": query, "attempt": self.search_calls},
        )
        return ranked, observation

    def open_page(self, url: str) -> tuple[SourcePage | None, ToolObservation]:
        started = time.perf_counter()
        page = self.pages.get(url)
        elapsed = (time.perf_counter() - started) * 1000
        return page, ToolObservation(
            tool="open_page",
            success=page is not None,
            detail="Page opened" if page else "Page not found",
            latency_ms=elapsed,
            metadata={"url": url},
        )


class WebResearchTools:
    """Live web search and safe page retrieval tools used by the Discovery Agent."""

    def __init__(
        self,
        max_results: int = 5,
        timeout: float = 20,
        tavily_api_key: str | None = None,
        tavily_quota_guard: Callable[[int], bool] | None = None,
        tavily_search_depth: str = "advanced",
    ) -> None:
        self.max_results = max_results
        self.tavily_api_key = tavily_api_key
        self.tavily_quota_guard = tavily_quota_guard
        if tavily_search_depth not in {"basic", "advanced"}:
            raise ValueError("tavily_search_depth must be 'basic' or 'advanced'")
        self.tavily_search_depth = tavily_search_depth
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "OpportunitySentinel/0.1 educational-research-bot"},
        )

    def search_web(self, query: str) -> tuple[list[SourcePage], ToolObservation]:
        folded_query = query.casefold()
        if "site:tuwaiq.edu.sa" in folded_query and "-site:tuwaiq.edu.sa" not in folded_query:
            return self._search_tuwaiq(query)
        if self.tavily_api_key:
            pages, observation = self._search_tavily(query)
            if observation.success and pages:
                return pages, observation
        started = time.perf_counter()
        try:
            results = list(
                DDGS(timeout=min(8, int(self.client.timeout.read))).text(
                    query, max_results=self.max_results
                )
            )
            pages = [
                SourcePage(
                    url=item["href"],
                    title=item.get("title") or "Untitled opportunity page",
                    content=item.get("body") or "",
                    official=_looks_official(item["href"]),
                )
                for item in results
                if item.get("href") and _public_http_url(item["href"])
            ]
            success = True
            detail = f"Found {len(pages)} live search results"
        except Exception as exc:  # the search backend exposes heterogeneous failures
            pages = []
            success = False
            detail = f"Search failed: {type(exc).__name__}"
        elapsed = (time.perf_counter() - started) * 1000
        logger.info("tool_call", tool="search_web", success=success, latency_ms=elapsed)
        return pages, ToolObservation(
            tool="search_web",
            success=success,
            detail=detail,
            latency_ms=elapsed,
            metadata={"query": query, "result_count": len(pages)},
        )

    def _search_tavily(self, query: str) -> tuple[list[SourcePage], ToolObservation]:
        started = time.perf_counter()
        pages: list[SourcePage] = []
        credits = 0
        request_id = None
        reserved_credits = 1 if self.tavily_search_depth == "basic" else 2
        if self.tavily_quota_guard and not self.tavily_quota_guard(reserved_credits):
            return pages, ToolObservation(
                tool="tavily_search",
                success=False,
                detail="Monthly Tavily safety budget exhausted; using free fallback",
                latency_ms=0,
                metadata={"query": query, "credits": 0, "budget_blocked": True},
            )
        try:
            payload = {
                "query": query[:400],
                "search_depth": self.tavily_search_depth,
                "max_results": self.max_results,
                "include_raw_content": "markdown",
                "include_answer": False,
                "time_range": "year",
                "country": "saudi arabia",
            }
            if self.tavily_search_depth == "advanced":
                payload["chunks_per_source"] = 3
            explicit_domains = [
                domain.casefold()
                for domain in re.findall(r"(?<!-)site:([\w.-]+)", query, re.I)
            ]
            if explicit_domains:
                payload["include_domains"] = sorted(set(explicit_domains))
            if "-site:tuwaiq.edu.sa" in query.casefold():
                payload["include_domains"] = [
                    "athkax.sdaia.gov.sa",
                    "futurex.sa",
                    "hub.misk.org.sa",
                    "kacst.gov.sa",
                    "mcit.gov.sa",
                    "misk.org.sa",
                    "sdaia.gov.sa",
                    "spa.gov.sa",
                ]
            response = self.client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {self.tavily_api_key}"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            credits = body.get("usage", {}).get("credits", 0)
            request_id = body.get("request_id")
            for item in body.get("results", []):
                url = item.get("url")
                if not url or not _public_http_url(url):
                    continue
                content = item.get("raw_content") or item.get("content") or ""
                pages.append(
                    SourcePage(
                        url=url,
                        title=item.get("title") or "Untitled opportunity page",
                        content="TAVILY_EXTRACTED_SOURCE\n" + content[:60_000],
                        official=_looks_official(url),
                    )
                )
            success = True
            detail = f"Tavily returned {len(pages)} ranked and extracted results"
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            success = False
            detail = f"Tavily failed: {type(exc).__name__}"
        elapsed = (time.perf_counter() - started) * 1000
        logger.info(
            "tool_call",
            tool="tavily_search",
            success=success,
            latency_ms=elapsed,
            credits=credits,
            request_id=request_id,
        )
        return pages, ToolObservation(
            tool="tavily_search",
            success=success,
            detail=detail,
            latency_ms=elapsed,
            metadata={
                "query": query,
                "result_count": len(pages),
                "credits": credits,
                "request_id": request_id,
                "search_depth": self.tavily_search_depth,
            },
        )

    def _search_tuwaiq(self, query: str) -> tuple[list[SourcePage], ToolObservation]:
        """Use Tuwaiq's API, with a zero-cost official-page index fallback.

        Tuwaiq currently returns Cloudflare 403 responses to some server-side
        clients while the same catalogue remains public in browsers.  A failed
        API must therefore degrade to indexed *tuwaiq.edu.sa* detail pages
        instead of making the whole academy disappear from discovery.
        """
        started = time.perf_counter()
        pages: list[SourcePage] = []
        strategy = "official_api"
        try:
            eligible_rows = self._tuwaiq_open_rows()
            eligible_rows.sort(
                key=lambda row: (
                    str(row.get("registrationEndDate") or "9999-12-31"),
                    -_tuwaiq_relevance(row, query),
                )
            )

            # Detail pages contain the exact requirements and evidence needed by the
            # verifier. Fetch them concurrently so complete coverage does not turn the
            # fast official scan into a several-minute sequential crawl.
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(eligible_rows)))) as executor:
                details = executor.map(self._tuwaiq_detail, eligible_rows)
            for row, detail in zip(eligible_rows, details, strict=True):
                slug = row.get("slug")
                if not slug or not detail:
                    continue
                majors = _majors_from_requirements(detail.get("requirements") or [])
                city, mode = _location_from_tuwaiq(detail)
                deadline = (detail.get("registrationEndDate") or "")[:10]
                application_url = f"https://tuwaiq.edu.sa/bootcamp/{slug}/view"
                opportunity_type = _tuwaiq_opportunity_type(detail)
                technical_quote = _tuwaiq_technical_quote(detail)
                content = (
                    "OPPORTUNITY_SENTINEL_STRUCTURED_SOURCE\n"
                    "organization: أكاديمية طويق\n"
                    f"type: {opportunity_type}\n"
                    f"city: {city}\n"
                    f"mode: {mode}\n"
                    f"majors: {majors}\n"
                    f"deadline: {deadline}\n"
                    "registration_status: open\n"
                    "cost: free\n"
                    f"apply: {application_url}\n"
                    + ("technical_focus: true\n" if technical_quote else "")
                    + (f"technical_evidence: {technical_quote}\n" if technical_quote else "")
                    + "requirements: "
                    + " | ".join(detail.get("requirements") or [])
                )
                pages.append(
                    SourcePage(
                        url=application_url,
                        title=detail.get("title") or row.get("title") or "برنامج تقني",
                        content=content,
                        official=True,
                    )
                )
            success = True
            detail_text = f"Found {len(pages)} open programs from Tuwaiq official API"
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            strategy = "official_telegram_channel"
            try:
                pages = self._tuwaiq_channel_fallback()
            except (httpx.HTTPError, ValueError, KeyError):
                pages = []
            if not pages:
                strategy = "official_index_fallback"
                pages = self._tuwaiq_index_fallback(query)
            success = bool(pages)
            detail_text = (
                f"Tuwaiq API failed ({type(exc).__name__}); "
                f"{strategy} found {len(pages)} open programs"
            )
        elapsed = (time.perf_counter() - started) * 1000
        logger.info(
            "tool_call",
            tool=(
                "tuwaiq_official_api"
                if strategy == "official_api"
                else "tuwaiq_official_fallback"
            ),
            success=success,
            latency_ms=elapsed,
        )
        return pages, ToolObservation(
            tool=(
                "tuwaiq_official_api"
                if strategy == "official_api"
                else "tuwaiq_official_fallback"
            ),
            success=success,
            detail=detail_text,
            latency_ms=elapsed,
            metadata={
                "result_count": len(pages),
                "source": "tuwaiq.edu.sa",
                "strategy": strategy,
            },
        )

    def _tuwaiq_index_fallback(self, query: str) -> list[SourcePage]:
        """Recover public official detail pages when Cloudflare blocks the API.

        Search is discovery only.  Every returned record still points to an
        official Tuwaiq detail page and must carry an explicit open marker from
        that page's indexed text.  Closed/ambiguous snippets fail closed.
        """
        searches = [
            'site:tuwaiq.edu.sa/bootcamp "متاح التسجيل" معسكر برنامج تقني',
            'site:tuwaiq.edu.sa/bootcamp "ينتهي التسجيل" برمجة ذكاء اصطناعي',
            'site:tuwaiq.edu.sa/bootcamp "Register Now" cybersecurity cloud data',
        ]
        if query.strip():
            searches.insert(0, f"{query} /bootcamp/ متاح التسجيل")
        unique: dict[str, SourcePage] = {}
        for search in searches:
            try:
                results = DDGS(timeout=min(10, int(self.client.timeout.read))).text(
                    search,
                    max_results=max(20, self.max_results * 4),
                )
                for item in results:
                    page = _tuwaiq_index_page(item)
                    if page:
                        unique[page.url] = page
            except Exception:  # DDGS exposes heterogeneous backend failures
                continue
        return list(unique.values())

    def _tuwaiq_channel_fallback(self) -> list[SourcePage]:
        """Read recent registration posts from Tuwaiq's official public channel."""
        response = self.client.get("https://t.me/s/TuwaiqAcademy")
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        pages: dict[str, SourcePage] = {}
        for post in soup.select(".tgme_widget_message"):
            page = _tuwaiq_channel_page(post)
            if page:
                pages[page.url] = page
        return list(pages.values())

    def _tuwaiq_open_rows(self) -> list[dict]:
        """Paginate the official catalogue until it reaches past registrations."""
        unique: dict[str, dict] = {}
        page_number = 1
        while page_number <= 100:
            response = self.client.get(
                "https://tuwaiq.edu.sa/api/"
                f"GetInitiativePublishesShorten/20/{page_number}?type=NORMAL"
            )
            response.raise_for_status()
            body = response.json()
            rows = body.get("data", [])
            if not rows:
                break
            for row in rows:
                if (
                    row.get("isOpen")
                    and row.get("isRegistrationOpen")
                    and not row.get("isRegistrationClosed")
                    and _future_or_today(row.get("registrationEndDate"))
                ):
                    identifier = str(row.get("id") or row.get("slug") or "")
                    if identifier:
                        unique[identifier] = row
            # The official catalogue is ordered from current/future initiatives into
            # historical ones. Once an entire page has no future deadline, later pages
            # cannot contain a currently applicable registration.
            if not any(_future_or_today(row.get("registrationEndDate")) for row in rows):
                break
            total_pages = int(body.get("pagination", {}).get("totalPages") or 0)
            if total_pages and page_number >= total_pages:
                break
            page_number += 1
        return list(unique.values())

    def _tuwaiq_detail(self, row: dict) -> dict | None:
        slug = row.get("slug")
        if not slug:
            return None
        try:
            response = self.client.get(
                f"https://tuwaiq.edu.sa/api/GetInitiativePublishBySlug/{slug}"
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            return None

    def open_page(self, url: str) -> tuple[SourcePage | None, ToolObservation]:
        started = time.perf_counter()
        page: SourcePage | None = None
        detail = "Page retrieval failed"
        try:
            current_url = url
            response: httpx.Response | None = None
            for _ in range(4):
                if not _public_http_url(current_url):
                    raise ValueError("Blocked non-public URL")
                response = self.client.get(current_url)
                if not response.is_redirect:
                    break
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Redirect is missing a location")
                current_url = urljoin(current_url, location)
            else:
                raise ValueError("Too many redirects")
            if response is None or response.is_redirect:
                raise ValueError("Redirect could not be resolved safely")
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type and "text/plain" not in content_type:
                raise ValueError(f"Unsupported content type: {content_type}")
            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.decompose()
            text = "\n".join(
                line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
            )
            page = SourcePage(
                url=str(response.url),
                title=(soup.title.string.strip() if soup.title and soup.title.string else url),
                content=text[:60_000],
                official=_looks_official(str(response.url)),
            )
            detail = "Page opened and sanitized"
        except (httpx.HTTPError, ValueError) as exc:
            detail = f"Page failed: {type(exc).__name__}: {exc}"
        elapsed = (time.perf_counter() - started) * 1000
        logger.info(
            "tool_call",
            tool="open_page",
            success=page is not None,
            latency_ms=elapsed,
            url=_ascii_safe(url),
        )
        return page, ToolObservation(
            tool="open_page",
            success=page is not None,
            detail=detail,
            latency_ms=elapsed,
            metadata={"url": url},
        )


def _public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.hostname.casefold() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        addresses = {info[4][0] for info in getaddrinfo(parsed.hostname, parsed.port or 443)}
        return all(_is_public_address(address) for address in addresses)
    except OSError:
        return False


def _is_public_address(address: str) -> bool:
    parsed = ip_address(address)
    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def _looks_official(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    trusted_roots = {
        "aramco.com",
        "ksu.edu.sa",
        "alumni.ksu.edu.sa",
        "dar.ksu.edu.sa",
        "futurex.sa",
        "elm.sa",
        "misk.org.sa",
        "neom.com",
        "riyadh.sa",
        "sabic.com",
        "spa.gov.sa",
        "stc.com.sa",
    }
    return (
        any(host == root or host.endswith(f".{root}") for root in trusted_roots)
        or host.endswith(".gov.sa")
        or host.endswith(".edu.sa")
        or host.endswith(".org.sa")
    )


def _ascii_safe(value: str) -> str:
    return value.encode("ascii", "backslashreplace").decode("ascii")


def _future_or_today(value: str | None) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value).date() >= date.today()
    except ValueError:
        return False


def _tuwaiq_relevance(row: dict, query: str) -> int:
    text = " ".join(
        str(row.get(field) or "")
        for field in ("title", "initiativeScopeName", "initiativeCategoryName")
    ).casefold()
    terms = {term.casefold() for term in query.split() if len(term) > 3}
    technical = (
        "البرمجيات",
        "الذكاء الاصطناعي",
        "الحوسبة",
        "الأمن السيبراني",
        "تقني",
        "بيانات",
        "برمجة",
    )
    return 10 * len(terms.intersection(text.split())) + 20 * sum(x in text for x in technical)


def _tuwaiq_index_page(item: dict) -> SourcePage | None:
    """Convert one indexed result only when the official snippet proves it is open."""
    url = str(item.get("href") or "").strip()
    parsed = urlparse(url)
    if parsed.hostname != "tuwaiq.edu.sa" or not re.fullmatch(
        r"/bootcamp/[^/]+/view/?", parsed.path
    ):
        return None
    title = str(item.get("title") or "").strip()
    snippet = str(item.get("body") or "").strip()
    text = f"{title}\n{snippet}"
    folded = text.casefold()
    if any(
        marker in folded
        for marker in ("للناشئين", "أبناءك", "أبنائكم", "من 14 إلى 17", "من 6 إلى 12")
    ):
        return None
    if any(
        marker in folded
        for marker in (
            "انتهى التسجيل",
            "انتهي التسجيل",
            "التسجيل مغلق",
            "closed",
        )
    ):
        return None
    open_quote = next(
        (
            marker
            for marker in (
                "متاح التسجيل",
                "ينتهي التسجيل",
                "بادر بالتسجيل",
                "سجل الآن",
                "register now",
            )
            if marker in folded
        ),
        None,
    )
    technical_quote = _tuwaiq_technical_quote({"title": title, "description": snippet})
    if not open_quote or not technical_quote:
        return None

    if "عن بعد" in text:
        city, mode = "عن بعد", "online"
    elif "الرياض" in text:
        city, mode = "الرياض", "in_person"
    else:
        # Location is a hard delivery gate.  Never turn an omitted location into
        # a guessed Riyadh or remote programme.
        return None
    opportunity_type = "bootcamp" if "معسكر" in title else "course"
    majors = "التخصصات التقنية" if any(
        marker in folded for marker in ("التخصصات التقنية", "تخصص تقني")
    ) else ""
    structured = (
        "OPPORTUNITY_SENTINEL_STRUCTURED_SOURCE\n"
        "organization: أكاديمية طويق\n"
        f"type: {opportunity_type}\n"
        f"city: {city}\n"
        f"mode: {mode}\n"
        f"majors: {majors}\n"
        "registration_status: open\n"
        f"apply: {url}\n"
        "technical_focus: true\n"
        f"technical_evidence: {technical_quote[:1000]}\n"
        f"indexed_open_evidence: {open_quote}\n"
    )
    return SourcePage(
        url=url,
        title=title or "برنامج تقني من أكاديمية طويق",
        content=structured,
        official=True,
    )


def _tuwaiq_channel_page(post) -> SourcePage | None:
    """Convert a recent official-channel registration post into structured evidence."""
    message = post.select_one(".tgme_widget_message_text")
    time_node = post.select_one("time[datetime]")
    post_id = str(post.get("data-post") or "").strip()
    if message is None or time_node is None or not post_id.startswith("TuwaiqAcademy/"):
        return None
    try:
        published = datetime.fromisoformat(str(time_node["datetime"])).date()
    except (TypeError, ValueError):
        return None
    age_days = (date.today() - published).days
    if age_days < 0 or age_days > 21:
        return None

    text = "\n".join(
        line.strip() for line in message.get_text("\n").splitlines() if line.strip()
    )
    folded = text.casefold()
    if any(
        marker in folded
        for marker in ("للناشئين", "أبناءك", "أبنائكم", "من 14 إلى 17", "من 6 إلى 12")
    ):
        return None
    open_quote = next(
        (marker for marker in ("سجّل الآن", "سجل الآن", "التسجيل مفتوح") if marker in text),
        None,
    )
    if not open_quote or any(marker in folded for marker in ("انتهى التسجيل", "التسجيل مغلق")):
        return None
    application_url = next(
        (
            str(anchor.get("href"))
            for anchor in message.select("a[href]")
            if re.fullmatch(
                r"https?://(?:www\.)?tuwaiq\.edu\.sa/bootcamp/[^/]+/view/?",
                str(anchor.get("href") or ""),
                re.I,
            )
        ),
        None,
    )
    if not application_url:
        return None
    technical_quote = _tuwaiq_technical_quote({"title": text, "description": text})
    if not technical_quote:
        return None

    title_match = re.search(
        r"((?:معسكر|برنامج|دبلوم|ورشة|تحدي|مسابقة)\s+[^\n.!؟]{3,180})",
        text,
    )
    raw_title = title_match.group(1) if title_match else text.splitlines()[0]
    title = re.sub(r"\s+", " ", raw_title).strip(" :،؛\"'")
    if "عن بعد" in text:
        city, mode = "", "online"
    elif "الرياض" in text:
        city, mode = "الرياض", "in_person"
    else:
        city, mode = "", "unknown"
    if any(marker in title for marker in ("تحدي", "مسابقة")):
        opportunity_type = "competition"
    elif "ورشة" in title:
        opportunity_type = "event"
    elif any(marker in title for marker in ("معسكر", "دبلوم")):
        opportunity_type = "bootcamp"
    else:
        opportunity_type = "course"
    majors = next(
        (
            marker
            for marker in ("جميع التخصصات التقنية", "التخصصات التقنية", "تخصص تقني")
            if marker in text
        ),
        "",
    )
    source_url = f"https://t.me/{post_id}"
    structured = (
        "OPPORTUNITY_SENTINEL_STRUCTURED_SOURCE\n"
        "organization: أكاديمية طويق\n"
        f"type: {opportunity_type}\n"
        + (f"city: {city}\n" if city else "")
        + f"mode: {mode}\n"
        + f"majors: {majors}\n"
        + "registration_status: open\n"
        + f"publication_date: {published.isoformat()}\n"
        + f"apply: {application_url}\n"
        + "technical_focus: true\n"
        + f"technical_evidence: {technical_quote[:1000]}\n"
        + f"channel_open_evidence: {open_quote}\n"
    )
    return SourcePage(url=source_url, title=title[:300], content=structured, official=True)


def _tuwaiq_opportunity_type(detail: dict) -> str:
    category = str(detail.get("initiativeCategoryName") or "").strip().casefold()
    if "معسكر" in category or "bootcamp" in category:
        return "bootcamp"
    if any(marker in category for marker in ("لقاء", "ويبينار", "ندوة", "event", "webinar")):
        return "event"
    return "course"


def _tuwaiq_technical_quote(detail: dict) -> str | None:
    """Return an exact first-party quote proving that the initiative is technical."""
    values: list[str] = []
    for field in ("title", "description"):
        value = str(detail.get(field) or "").strip()
        if value:
            values.append(value)
    for field in ("goals", "features", "requirements"):
        value = detail.get(field) or []
        if isinstance(value, list):
            values.extend(str(item).strip() for item in value if str(item).strip())

    technical_markers = (
        "تقني",
        "تقنية",
        "برمج",
        "الذكاء الاصطناعي",
        "بيانات",
        "الأمن السيبراني",
        "الحوسبة",
        "سحابي",
        "شبكات",
        "قواعد البيانات",
        "sql",
        "python",
        "java",
        "javascript",
        "تطوير الويب",
        "تطوير التطبيقات",
        "الواقع الافتراضي",
        "الواقع المعزز",
        "تجربة المستخدم",
        "واجهات",
        "روبوت",
        "إنترنت الأشياء",
        "blockchain",
        "cloud",
        "software",
        "cyber",
        "data",
        "machine learning",
        "artificial intelligence",
    )
    for value in values:
        folded = value.casefold()
        if any(marker in folded for marker in technical_markers):
            return value[:1000]
    return None


def _majors_from_requirements(requirements: list[str]) -> str:
    joined = " ".join(requirements).casefold()
    if "تخصص تقني" in joined or "التخصصات التقنية" in joined:
        return "التخصصات التقنية"
    return ""


def _location_from_tuwaiq(detail: dict) -> tuple[str, str]:
    location = str(detail.get("locationName") or detail.get("locationText") or "")
    if not location:
        return "عن بعد", "online"
    if "عن بعد" in location and "الرياض" in location:
        return "الرياض", "hybrid"
    if "عن بعد" in location:
        return "عن بعد", "online"
    return "الرياض" if "الرياض" in location else location, "in_person"
