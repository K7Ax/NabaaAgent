"""Run central discovery/revalidation/delivery from GitHub Actions.

This process searches once for the whole platform. It never contains a student profile.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import multiprocessing
import os
import random
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import httpx

from opportunity_sentinel.agents import DiscoveryAgent, VerificationAgent
from opportunity_sentinel.config import get_settings
from opportunity_sentinel.connectors import (
    FinancialAcademyHackathonConnector,
    FutureSkillsConnector,
    KSUAlumniJobsConnector,
    KSUOfficialNewsConnector,
    MiskProgramsConnector,
    MonshaatAcademyConnector,
    PublicATSConnector,
    default_ats_boards,
    extract_linkedin_technical_training,
)
from opportunity_sentinel.lightweight_extractor import LightweightOpportunityExtractor
from opportunity_sentinel.llm import Provider
from opportunity_sentinel.models import OpportunityCandidate, VerificationStatus
from opportunity_sentinel.repository import Repository
from opportunity_sentinel.revalidation import revalidate_application
from opportunity_sentinel.tools import SourcePage, WebResearchTools

INGEST_BATCH_SIZE = 5
SOURCE_REPORT_BATCH_SIZE = 3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["fast", "deep", "deliver", "revalidate"])
    args = parser.parse_args()
    api_url = os.getenv("NABAA_API_URL", "").rstrip("/")
    secret = os.getenv("INTERNAL_API_SECRET", "")
    if api_url and secret:
        # Every mode below ends up posting to the API. Waking it first means the long
        # crawl is not thrown away because the instance was still cold when it finished.
        _wake(api_url)
    if args.mode in {"deliver", "revalidate"}:
        if not api_url or not secret:
            if args.mode == "revalidate":
                print(json.dumps(_revalidate_local(), ensure_ascii=False))
                return
            raise RuntimeError("NABAA_API_URL and INTERNAL_API_SECRET are required for delivery")
        response = _signed_post(api_url, f"/internal/{args.mode}", b"", secret)
        print(json.dumps(response))
        return

    if args.mode == "fast":
        candidates, source_reports, source_inventory = _fast_candidates()
        if not api_url or not secret:
            print(
                json.dumps(
                    _save_local(
                        candidates,
                        "official-connectors",
                        source_reports,
                        source_inventory,
                    ),
                    ensure_ascii=False,
                )
            )
            return
        response = _post_ingest_batches(
            api_url,
            secret,
            candidates,
            "official-connectors",
            source_reports,
            source_inventory,
        )
        print(json.dumps(response))
        return

    quota_guard = (
        (lambda units: _reserve_quota(api_url, secret, units)) if api_url and secret else None
    )
    queries = _queries(args.mode)
    local_quota_repository = None
    if quota_guard is None:
        local_quota_repository = Repository.from_settings(get_settings())

        def local_quota_guard(units: int) -> bool:
            return local_quota_repository.consume_quota(
                "tavily", units, get_settings().tavily_monthly_credit_limit
            )

        quota_guard = local_quota_guard
    try:
        candidates = _deep_candidates(queries, quota_guard)
    finally:
        if local_quota_repository:
            local_quota_repository.connection.close()
    if not api_url or not secret:
        print(json.dumps(_save_local(candidates, "web-discovery")))
        return
    response = _post_ingest_batches(api_url, secret, candidates, "web-discovery")
    print(json.dumps(response))


def _revalidate_local() -> dict[str, object]:
    settings = get_settings()
    repository = Repository.from_settings(settings)
    run_id = repository.start_crawl("revalidation")
    checked = opened = unverified = 0
    expired = repository.expire_stale()
    try:
        due = repository.due_revalidation(limit=settings.revalidation_batch_size)
        if due:
            with ThreadPoolExecutor(max_workers=min(8, len(due))) as executor:
                futures = {
                    executor.submit(
                        revalidate_application,
                        candidate,
                        settings.request_timeout_seconds,
                    ): identifier
                    for identifier, candidate in due
                }
                for future in as_completed(futures):
                    identifier = futures[future]
                    checked += 1
                    try:
                        result = future.result()
                    except Exception as exc:
                        repository.record_revalidation(
                            identifier, "unverified", f"{type(exc).__name__}: {exc}"
                        )
                        unverified += 1
                        continue
                    repository.record_revalidation(identifier, result.state, result.reason)
                    if result.state == "closed":
                        expired += 1
                    elif result.state == "open":
                        opened += 1
                    else:
                        unverified += 1
        repository.finish_crawl(run_id, checked, opened)
        return {
            "mode": "local",
            "checked": checked,
            "open": opened,
            "expired": expired,
            "unverified": unverified,
        }
    except Exception as exc:
        repository.finish_crawl(run_id, checked, opened, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        repository.connection.close()


def _queries(mode: str, slot: int | None = None) -> list[str]:
    year = datetime.now(UTC).year
    slot = datetime.now(UTC).hour // 8 if slot is None else slot % 3
    query_sets = [
        [
            f"تدريب تعاوني COOP تخصصات تقنية الرياض {year} التقديم مفتوح موقع رسمي",
            f"internship software data AI cybersecurity Riyadh students {year} official careers",
            f"تدريب صيفي تقنية طلاب الجامعات الرياض السعودية {year} التقديم مفتوح",
            f"برنامج خريجين تمهير تقنية الرياض {year} التقديم مفتوح موقع رسمي",
            f"تدريب تعاوني تدريب صيفي تقنية الرياض {year} التسجيل مفتوح site:x.com",
        ],
        [
            f"دوام جزئي part time تقنية برمجة الرياض {year} apply official careers",
            f"وظيفة مبتدئة junior entry level software data الرياض {year} official careers",
            f"هاكاثون تقني طلاب الجامعات الرياض السعودية {year} التسجيل مفتوح",
            f"مسابقة تحدي تقني برمجة ذكاء اصطناعي السعودية {year} التسجيل مفتوح",
            f"هاكاثون مسابقة فعالية تقنية الرياض {year} التسجيل مفتوح site:x.com",
        ],
        [
            f"منحة تقنية طلاب الجامعات السعودية {year} التقديم مفتوح",
            f"معسكر تقني برمجة بيانات ذكاء اصطناعي الرياض {year} التسجيل مفتوح",
            f"فعالية مؤتمر ورشة تقنية طلاب الرياض {year} التسجيل مفتوح",
            f"تطوع تقني برمجة بيانات طلاب الجامعات السعودية {year} رابط رسمي",
            f"معسكر منحة تطوع برنامج تقني السعودية {year} التسجيل مفتوح site:x.com",
        ],
    ]
    return query_sets[slot]


def _fast_candidates() -> tuple[
    dict[str, dict],
    dict[str, dict[str, object]],
    dict[str, list[str]],
]:
    settings = get_settings()
    tools = WebResearchTools(
        max_results=settings.search_max_results,
        timeout=settings.request_timeout_seconds,
        tavily_api_key=None,
    )
    discovery = DiscoveryAgent(tools)
    verifier = VerificationAgent()
    candidates: dict[str, dict] = {}
    source_reports: dict[str, dict[str, object]] = {}
    source_inventory: dict[str, list[str]] = {}

    try:
        pages, observation = tools.search_web("برامج مفتوحة site:tuwaiq.edu.sa")
        verified = 0
        for page in pages:
            candidate = discovery.extract(page.__dict__)
            if not candidate:
                continue
            report = verifier.verify(candidate)
            if report.status == VerificationStatus.VERIFIED:
                application_url = str(candidate.application_url)
                candidates[application_url] = candidate.model_dump(mode="json")
                source_inventory.setdefault("tuwaiq", []).append(application_url)
                verified += 1
        source_reports["tuwaiq"] = {
            "discovered": len(pages),
            "verified": verified,
            **({"error": observation.detail} if not observation.success else {}),
        }
    except Exception as exc:
        source_reports["tuwaiq"] = {"discovered": 0, "verified": 0, "error": str(exc)}

    connectors = [
        (
            "future-skills",
            FutureSkillsConnector(
                timeout=settings.request_timeout_seconds,
                max_courses=max(settings.search_max_results, 20),
            ),
        ),
        (
            "financial-academy",
            FinancialAcademyHackathonConnector(timeout=settings.request_timeout_seconds),
        ),
        ("misk", MiskProgramsConnector(timeout=settings.request_timeout_seconds)),
        (
            "monshaat-academy",
            MonshaatAcademyConnector(timeout=settings.request_timeout_seconds),
        ),
        ("ksu-main", KSUOfficialNewsConnector(timeout=settings.request_timeout_seconds)),
        ("ksu-alumni-gate", KSUAlumniJobsConnector(timeout=settings.request_timeout_seconds)),
    ]
    connectors.extend(
        (
            board.source_id or "public-ats",
            PublicATSConnector([board], timeout=settings.request_timeout_seconds),
        )
        for board in default_ats_boards()
    )
    for source_id, connector in connectors:
        try:
            collected = connector.collect()
            verified = 0
            for candidate in collected:
                report = verifier.verify(candidate)
                if report.status == VerificationStatus.VERIFIED:
                    application_url = str(candidate.application_url)
                    candidates[application_url] = candidate.model_dump(mode="json")
                    source_inventory.setdefault(source_id, []).append(application_url)
                    verified += 1
            source_reports[source_id] = {
                "discovered": len(collected),
                "verified": verified,
            }
        except Exception as exc:
            source_reports[source_id] = {
                "discovered": 0,
                "verified": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            connector.close()
    return candidates, source_reports, source_inventory


def _deep_candidates(queries: list[str], quota_guard: Callable[[int], bool]) -> dict[str, dict]:
    settings = get_settings()
    timeout = min(settings.request_timeout_seconds, 8)
    candidates = _linkedin_candidates(settings, quota_guard)
    tools = WebResearchTools(
        max_results=20,
        timeout=timeout,
        tavily_api_key=settings.tavily_api_key,
        tavily_quota_guard=quota_guard,
        tavily_search_depth="basic",
    )
    verifier = VerificationAgent()
    pages: dict[str, SourcePage] = {}
    social_link_budget = 12
    per_query_limit = 6
    official_page_limit = max(12, settings.search_max_results * 3)
    for query in queries:
        found, _ = tools.search_web(query)
        added = 0
        for page in found:
            linkedin_candidate = extract_linkedin_technical_training(page)
            if linkedin_candidate:
                report = verifier.verify(linkedin_candidate)
                if report.status == VerificationStatus.VERIFIED:
                    candidates[str(linkedin_candidate.application_url)] = (
                        linkedin_candidate.model_dump(mode="json")
                    )
                    added += 1
            if (
                page.official
                and page.url not in pages
                and len(pages) < official_page_limit
            ):
                pages.setdefault(page.url, page)
                added += 1
            elif _is_social_url(page.url) and social_link_budget > 0:
                for official_url in _social_outbound_urls(page.content):
                    social_link_budget -= 1
                    opened, _ = tools.open_page(official_url)
                    if opened and opened.official and opened.url not in pages:
                        pages.setdefault(opened.url, opened)
                        added += 1
                    if added >= per_query_limit:
                        break
                    if social_link_budget <= 0:
                        break
            if added >= per_query_limit:
                break
    if not pages or not (settings.groq_api_key or settings.openrouter_api_key):
        return candidates
    context = multiprocessing.get_context("spawn")
    workers = []
    for page in pages.values():
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_extract_worker,
            args=(page.__dict__, timeout, sender),
        )
        process.start()
        sender.close()
        workers.append((process, receiver))
    deadline = time.monotonic() + 40
    for process, receiver in workers:
        process.join(max(0, deadline - time.monotonic()))
        if process.is_alive():
            process.terminate()
            process.join(1)
        try:
            data = receiver.recv() if receiver.poll() else None
        except (BrokenPipeError, EOFError, OSError):
            data = None
        if data:
            candidate = OpportunityCandidate.model_validate(data)
            report = verifier.verify(candidate)
            if report.status == VerificationStatus.VERIFIED:
                candidates[str(candidate.application_url)] = candidate.model_dump(mode="json")
        receiver.close()
    return candidates


def _linkedin_candidates(settings, quota_guard: Callable[[int], bool]) -> dict[str, dict]:
    tools = WebResearchTools(
        max_results=20,
        timeout=min(settings.request_timeout_seconds, 12),
        tavily_api_key=settings.tavily_api_key,
        tavily_quota_guard=quota_guard,
        tavily_search_depth="basic",
    )
    verifier = VerificationAgent()
    candidates: dict[str, dict] = {}
    year = datetime.now(UTC).year
    queries = [
        f"COOP internship software computer IT data AI Riyadh {year} Apply site:linkedin.com",
        f"graduate program junior technical Riyadh Saudi Arabia {year} Apply site:linkedin.com",
    ]
    for query in queries:
        pages, _ = tools.search_web(query)
        for page in pages:
            candidate = extract_linkedin_technical_training(page)
            if not candidate:
                continue
            report = verifier.verify(candidate)
            if report.status == VerificationStatus.VERIFIED:
                candidates[str(candidate.application_url)] = candidate.model_dump(mode="json")
    return candidates


def _is_social_url(url: str) -> bool:
    folded = url.casefold()
    return any(host in folded for host in ("linkedin.com/", "x.com/", "twitter.com/"))


def _social_outbound_urls(content: str) -> list[str]:
    urls: list[str] = []
    for value in re.findall(r"https?://[^\s<>()\]\[\"']+", content):
        cleaned = value.rstrip(".,،؛:!?")
        if _is_social_url(cleaned) or cleaned in urls:
            continue
        urls.append(cleaned)
    return urls[:2]


def _extract_worker(page_data: dict, timeout: float, sender) -> None:
    """Isolate provider hangs; the parent enforces a hard wall-clock deadline."""
    try:
        settings = get_settings()
        providers: list[Provider] = []
        if settings.groq_api_key:
            providers.append(
                Provider(
                    "groq",
                    "https://api.groq.com/openai/v1",
                    settings.groq_api_key,
                    settings.groq_model,
                )
            )
        if settings.openrouter_api_key:
            providers.append(
                Provider(
                    "openrouter",
                    "https://openrouter.ai/api/v1",
                    settings.openrouter_api_key,
                    settings.openrouter_model,
                    {"HTTP-Referer": "https://github.com/K7Ax/NabaaAgent", "X-Title": "NabaaAgent"},
                )
            )
        page = SourcePage(**page_data)
        candidate = LightweightOpportunityExtractor(providers, timeout).extract(page)
        sender.send(candidate.model_dump(mode="json") if candidate else None)
    except Exception:
        sender.send(None)
    finally:
        sender.close()


def _payload(
    candidates: dict[str, dict],
    source_id: str,
    source_reports: dict[str, dict[str, object]] | None = None,
    source_inventory: dict[str, list[str]] | None = None,
) -> bytes:
    return json.dumps(
        {
            "source_id": source_id,
            "candidates": list(candidates.values()),
            "source_reports": source_reports or {},
            "source_inventory": source_inventory or {},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def _post_ingest_batches(
    api_url: str,
    secret: str,
    candidates: dict[str, dict],
    source_id: str,
    source_reports: dict[str, dict[str, object]] | None = None,
    source_inventory: dict[str, list[str]] | None = None,
    batch_size: int = INGEST_BATCH_SIZE,
    report_batch_size: int = SOURCE_REPORT_BATCH_SIZE,
) -> dict[str, int]:
    """Ingest bounded chunks so free hosts do not time out on large collections.

    Candidate and source-health batches are separate. This keeps both verification
    writes and authoritative reconciliation below strict free-host request limits.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if report_batch_size < 1:
        raise ValueError("report_batch_size must be positive")

    items = list(candidates.items())
    chunks = [items[index : index + batch_size] for index in range(0, len(items), batch_size)]
    if not chunks and not source_reports:
        chunks = [[]]

    totals = {"received": 0, "verified": 0, "withheld": 0}
    for chunk in chunks:
        chunk_candidates = dict(chunk)
        chunk_urls = set(chunk_candidates)
        chunk_inventory = {
            owner: [url for url in urls if url in chunk_urls]
            for owner, urls in (source_inventory or {}).items()
        }
        payload = _payload(
            chunk_candidates,
            source_id,
            None,
            chunk_inventory,
        )
        response = _signed_post(api_url, "/internal/ingest/batch", payload, secret)
        for key in totals:
            totals[key] += int(response.get(key) or 0)

    report_items = list((source_reports or {}).items())
    report_chunks = [
        report_items[index : index + report_batch_size]
        for index in range(0, len(report_items), report_batch_size)
    ]
    for report_chunk in report_chunks:
        reports = dict(report_chunk)
        inventory = {
            owner: (source_inventory or {}).get(owner, []) for owner in reports
        }
        payload = _payload({}, source_id, reports, inventory)
        _signed_post(api_url, "/internal/ingest/batch", payload, secret)
    return totals


def _save_local(
    candidates: dict[str, dict],
    source_id: str,
    source_reports: dict[str, dict[str, object]] | None = None,
    source_inventory: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    settings = get_settings()
    repository = Repository.from_settings(settings)
    run_id = repository.start_crawl(source_id)
    verifier = VerificationAgent()
    verified = withheld = 0
    titles: list[str] = []
    source_inventory = source_inventory or {}
    owners_by_url = {
        application_url: owner
        for owner, application_urls in source_inventory.items()
        for application_url in application_urls
    }
    try:
        for data in candidates.values():
            candidate = OpportunityCandidate.model_validate(data)
            repository.record_signal(
                source_id,
                str(candidate.source_url),
                candidate.title,
                candidate.model_dump(mode="json"),
            )
            report = verifier.verify(candidate)
            if report.status == VerificationStatus.VERIFIED:
                owner = owners_by_url.get(str(candidate.application_url))
                repository.save_opportunity(candidate, report.score, source_id=owner or source_id)
                verified += 1
                titles.append(candidate.title)
            else:
                withheld += 1
        repository.finish_crawl(run_id, len(candidates), verified)
        for report_source_id, source_report in (source_reports or {}).items():
            source_run_id = repository.start_crawl(report_source_id)
            repository.finish_crawl(
                source_run_id,
                int(source_report.get("discovered") or 0),
                int(source_report.get("verified") or 0),
                str(source_report["error"]) if source_report.get("error") else None,
            )
            if not source_report.get("error"):
                repository.reconcile_source(
                    report_source_id,
                    set(source_inventory.get(report_source_id, [])),
                )
        return {
            "mode": "local",
            "received": len(candidates),
            "verified": verified,
            "withheld": withheld,
            "titles": titles,
            "sources": source_reports or {},
        }
    except Exception as exc:
        repository.finish_crawl(run_id, len(candidates), verified, str(exc))
        raise
    finally:
        repository.connection.close()


def _reserve_quota(api_url: str, secret: str, units: int) -> bool:
    body = json.dumps({"provider": "tavily", "units": units}, separators=(",", ":")).encode()
    return bool(_signed_post(api_url, "/internal/quota/reserve", body, secret).get("granted"))


POST_ATTEMPTS = 4
POST_TIMEOUT_SECONDS = 60
WAKE_ATTEMPTS = 12
WAKE_INTERVAL_SECONDS = 10
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _wake(api_url: str) -> bool:
    """Poll readiness until the free-tier service has finished cold-starting.

    A suspended Render instance takes up to a minute to come back, and every job in
    this file used to fail on that first request. Waiting here keeps a cold start from
    looking like an outage.
    """
    if not api_url:
        return False
    for attempt in range(1, WAKE_ATTEMPTS + 1):
        try:
            response = httpx.get(api_url + "/readiness", timeout=15)
            if response.status_code == 200:
                if attempt > 1:
                    print(json.dumps({"event": "api_woke", "attempts": attempt}))
                return True
        except httpx.HTTPError:
            pass
        time.sleep(WAKE_INTERVAL_SECONDS)
    print(json.dumps({"event": "api_never_woke", "attempts": WAKE_ATTEMPTS}))
    return False


def _should_retry(exc: Exception) -> bool:
    if isinstance(exc, httpx.TimeoutException | httpx.ConnectError | httpx.ReadError):
        return True
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code in RETRYABLE_STATUS
    )


def _signed_post(api_url: str, path: str, body: bytes, secret: str) -> dict:
    """POST with an HMAC signature, retrying transient failures.

    The signature covers a timestamp the API only accepts inside a short window, so each
    attempt is signed again rather than replaying the first signature. Retries are safe
    because ingest upserts on application_url and delivery deduplicates; a 4xx is our own
    mistake and is never retried.
    """
    last: Exception | None = None
    for attempt in range(1, POST_ATTEMPTS + 1):
        timestamp = str(int(time.time()))
        signature = hmac.new(
            secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        try:
            response = httpx.post(
                api_url + path,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Nabaa-Timestamp": timestamp,
                    "X-Nabaa-Signature": signature,
                },
                timeout=POST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - re-raised below unless retryable
            last = exc
            if attempt == POST_ATTEMPTS or not _should_retry(exc):
                raise
            delay = 2**attempt + random.uniform(0, 1)
            print(
                json.dumps(
                    {
                        "event": "signed_post_retry",
                        "path": path,
                        "attempt": attempt,
                        "sleep": round(delay, 2),
                        "error": type(exc).__name__,
                    }
                )
            )
            time.sleep(delay)
    raise last  # pragma: no cover - the loop always returns or raises


if __name__ == "__main__":
    main()
