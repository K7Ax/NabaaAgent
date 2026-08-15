from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from opportunity_sentinel.agents import VerificationAgent
from opportunity_sentinel.config import Settings, get_settings
from opportunity_sentinel.logging import configure_logging, logger
from opportunity_sentinel.models import OpportunityCandidate, VerificationStatus
from opportunity_sentinel.repository import Repository
from opportunity_sentinel.revalidation import revalidate_application
from opportunity_sentinel.telegram_bot import BotRuntime, build_router, create_runtime


@dataclass
class ServiceState:
    settings: Settings
    repository: Repository
    runtime: BotRuntime | None = None
    bot: Bot | None = None
    dispatcher: Dispatcher | None = None


class IngestBatch(BaseModel):
    source_id: str | None = None
    candidates: list[OpportunityCandidate] = Field(max_length=200)
    source_reports: dict[str, dict[str, int | str]] = Field(default_factory=dict)
    source_inventory: dict[str, list[str]] = Field(default_factory=dict)


class QuotaReservation(BaseModel):
    provider: str = Field(pattern=r"^[a-z0-9_-]{2,30}$")
    units: int = Field(ge=1, le=100)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    repository = Repository(settings.data_db_path)
    state = ServiceState(settings=settings, repository=repository)
    if settings.telegram_bot_token:
        runtime = create_runtime(settings, repository=repository)
        bot = Bot(
            settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode="HTML"),
        )
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(runtime))
        state.runtime, state.bot, state.dispatcher = runtime, bot, dispatcher
        if settings.public_base_url and settings.telegram_webhook_secret:
            await bot.set_webhook(
                f"{settings.public_base_url.rstrip('/')}/telegram/webhook",
                secret_token=settings.telegram_webhook_secret,
                allowed_updates=["message", "callback_query"],
            )
            logger.info("telegram_webhook_configured")
    app.state.service = state
    yield
    if state.bot:
        await state.bot.session.close()
    repository.connection.close()


app = FastAPI(title="NabaaAgent", version="0.2.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "opportunity-sentinel"}


@app.get("/readiness")
def readiness(request: Request) -> dict[str, object]:
    state = _state(request)
    try:
        state.repository.connection.execute("SELECT 1").fetchone()
        database_ready = True
    except Exception:
        database_ready = False
    return {
        "status": "ready" if database_ready else "not_ready",
        "database_ready": database_ready,
        "telegram_configured": bool(state.settings.telegram_bot_token),
        "webhook_configured": bool(
            state.settings.public_base_url and state.settings.telegram_webhook_secret
        ),
        "llm_providers_configured": [
            name
            for name, configured in (
                ("groq", state.settings.groq_api_key),
                ("openrouter", state.settings.openrouter_api_key),
            )
            if configured
        ],
        "tavily_configured": bool(state.settings.tavily_api_key),
        "metrics": state.repository.metrics(),
    }


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    state = _state(request)
    expected = state.settings.telegram_webhook_secret
    if not expected or not hmac.compare_digest(x_telegram_bot_api_secret_token or "", expected):
        raise HTTPException(status_code=401, detail="invalid webhook secret")
    if not state.bot or not state.dispatcher:
        raise HTTPException(status_code=503, detail="telegram is not configured")
    update = Update.model_validate(await request.json(), context={"bot": state.bot})
    await state.dispatcher.feed_update(state.bot, update)
    return {"ok": True}


@app.post("/internal/ingest/batch")
async def ingest_batch(request: Request) -> dict[str, int]:
    raw = await request.body()
    _verify_internal_request(request, raw)
    batch = IngestBatch.model_validate_json(raw)
    state = _state(request)
    run_id = state.repository.start_crawl(batch.source_id)
    verifier = VerificationAgent()
    owners_by_url = {
        application_url: owner
        for owner, application_urls in batch.source_inventory.items()
        for application_url in application_urls
    }
    verified = 0
    rejected = 0
    try:
        for candidate in batch.candidates:
            state.repository.record_signal(
                batch.source_id,
                str(candidate.source_url),
                candidate.title,
                candidate.model_dump(mode="json"),
            )
            report = verifier.verify(candidate)
            if report.status == VerificationStatus.VERIFIED:
                owner = owners_by_url.get(str(candidate.application_url))
                state.repository.save_opportunity(
                    candidate,
                    report.score,
                    source_id=owner or batch.source_id,
                )
                verified += 1
            else:
                rejected += 1
                if report.status != VerificationStatus.REJECTED and any(
                    evidence.official_source for evidence in candidate.evidence
                ):
                    review_id = state.repository.queue_admin_review(
                        candidate, ", ".join(report.missing_fields or report.reasons)
                    )
                    if review_id and state.bot and state.settings.telegram_admin_chat_id:
                        await state.bot.send_message(
                            state.settings.telegram_admin_chat_id,
                            "⚠️ فرصة محتجزة تحتاج استكمال دليل\n\n"
                            f"{html.escape(candidate.title)}\n"
                            f"مراجعة رقم: {review_id}",
                        )
        state.repository.finish_crawl(run_id, len(batch.candidates), verified)
        for source_id, report in batch.source_reports.items():
            source_run_id = state.repository.start_crawl(source_id)
            state.repository.finish_crawl(
                source_run_id,
                int(report.get("discovered") or 0),
                int(report.get("verified") or 0),
                str(report["error"]) if report.get("error") else None,
            )
            if not report.get("error"):
                state.repository.reconcile_source(
                    source_id,
                    set(batch.source_inventory.get(source_id, [])),
                )
    except Exception as exc:
        state.repository.finish_crawl(
            run_id, len(batch.candidates), verified, f"{type(exc).__name__}: {exc}"
        )
        raise
    return {"received": len(batch.candidates), "verified": verified, "withheld": rejected}


@app.post("/internal/revalidate")
async def revalidate(request: Request) -> dict[str, int]:
    raw = await request.body()
    _verify_internal_request(request, raw)
    state = _state(request)
    run_id = state.repository.start_crawl("revalidation")
    expired = state.repository.expire_stale()
    checked = opened = unavailable = 0
    try:
        due = state.repository.due_revalidation(limit=state.settings.revalidation_batch_size)

        async def check_application(identifier: str, candidate: OpportunityCandidate):
            try:
                result = await asyncio.to_thread(
                    revalidate_application,
                    candidate,
                    state.settings.request_timeout_seconds,
                )
                return identifier, result, None
            except Exception as exc:
                return identifier, None, f"{type(exc).__name__}: {exc}"

        results = await asyncio.gather(
            *(check_application(identifier, candidate) for identifier, candidate in due),
        )
        for identifier, result, error in results:
            checked += 1
            if error:
                state.repository.record_revalidation(identifier, "unavailable", error)
                unavailable += 1
                continue
            assert result is not None
            state.repository.record_revalidation(identifier, result.state, result.reason)
            if result.state == "closed":
                expired += 1
            elif result.state == "open":
                opened += 1
            else:
                unavailable += 1
        state.repository.finish_crawl(run_id, checked, opened)
    except Exception as exc:
        state.repository.finish_crawl(
            run_id, checked, opened, f"{type(exc).__name__}: {exc}"
        )
        raise
    return {"open": opened, "expired": expired, "unavailable": unavailable}


@app.post("/internal/quota/reserve")
async def reserve_quota(request: Request) -> dict[str, bool]:
    raw = await request.body()
    _verify_internal_request(request, raw)
    reservation = QuotaReservation.model_validate_json(raw)
    state = _state(request)
    limit = state.settings.tavily_monthly_credit_limit if reservation.provider == "tavily" else 0
    return {
        "granted": bool(limit)
        and state.repository.consume_quota(reservation.provider, reservation.units, limit)
    }


@app.post("/internal/deliver")
async def deliver(request: Request) -> dict[str, int]:
    raw = await request.body()
    _verify_internal_request(request, raw)
    state = _state(request)
    if not state.bot:
        raise HTTPException(status_code=503, detail="telegram is not configured")
    rows = state.repository.due_deliveries(state.settings.delivery_batch_size)
    sent = failed = 0
    checked: dict[str, str] = {}
    digest_groups: dict[int, list] = {}
    for row in rows:
        if row["delivery_kind"] == "digest":
            digest_groups.setdefault(row["telegram_id"], []).append(row)
            continue
        candidate = OpportunityCandidate.model_validate_json(row["payload"])
        if state.repository.was_delivered(row["telegram_id"], row["opportunity_id"]):
            state.repository.complete_delivery(row["id"], row["telegram_id"], row["opportunity_id"])
            continue
        validation_state = checked.get(row["opportunity_id"])
        if validation_state is None:
            validation = await asyncio.to_thread(
                revalidate_application,
                candidate,
                state.settings.request_timeout_seconds,
            )
            validation_state = validation.state
            checked[row["opportunity_id"]] = validation_state
            state.repository.record_revalidation(
                row["opportunity_id"], validation.state, validation.reason
            )
        if validation_state == "closed":
            continue
        if validation_state != "open":
            state.repository.fail_delivery(row["id"], "application page unavailable")
            failed += 1
            continue
        try:
            from opportunity_sentinel.telegram_bot import opportunity_keyboard, opportunity_text

            await state.bot.send_message(
                row["telegram_id"],
                f"🔔 فرصة مطابقة لملفك — {row['score']}/100\n\n" + opportunity_text(candidate),
                reply_markup=opportunity_keyboard(row["opportunity_id"], candidate),
                disable_web_page_preview=True,
            )
            state.repository.complete_delivery(row["id"], row["telegram_id"], row["opportunity_id"])
            sent += 1
        except Exception as exc:
            state.repository.fail_delivery(row["id"], f"{type(exc).__name__}: {exc}")
            failed += 1
    for telegram_id, items in digest_groups.items():
        valid: list[tuple[object, OpportunityCandidate]] = []
        for row in items[:10]:
            candidate = OpportunityCandidate.model_validate_json(row["payload"])
            if state.repository.was_delivered(telegram_id, row["opportunity_id"]):
                state.repository.complete_delivery(row["id"], telegram_id, row["opportunity_id"])
                continue
            validation_state = checked.get(row["opportunity_id"])
            if validation_state is None:
                validation = await asyncio.to_thread(
                    revalidate_application,
                    candidate,
                    state.settings.request_timeout_seconds,
                )
                validation_state = validation.state
                checked[row["opportunity_id"]] = validation_state
                state.repository.record_revalidation(
                    row["opportunity_id"], validation.state, validation.reason
                )
            if validation_state == "open":
                valid.append((row, candidate))
            elif validation_state == "unavailable":
                state.repository.fail_delivery(row["id"], "application page unavailable")
                failed += 1
        if not valid:
            continue
        lines = ["🌙 <b>ملخص فرصك اليومي</b>", ""]
        for index, (row, candidate) in enumerate(valid, start=1):
            lines.append(
                f'{index}. <a href="{html.escape(str(candidate.application_url))}">'
                f"{html.escape(candidate.title)}</a> — {row['score']}/100"
            )
        try:
            await state.bot.send_message(
                telegram_id, "\n".join(lines), disable_web_page_preview=True
            )
            for row, _ in valid:
                state.repository.complete_delivery(row["id"], telegram_id, row["opportunity_id"])
            sent += len(valid)
        except Exception as exc:
            for row, _ in valid:
                state.repository.fail_delivery(row["id"], f"{type(exc).__name__}: {exc}")
            failed += len(valid)
    return {"sent": sent, "failed": failed}


@app.get("/internal/metrics")
async def metrics(request: Request) -> dict[str, int]:
    _verify_internal_request(request, b"")
    return _state(request).repository.metrics()


@app.get("/internal/sources")
async def sources(request: Request) -> list[dict[str, object]]:
    _verify_internal_request(request, b"")
    return _state(request).repository.source_health()


@app.get("/internal/coverage")
async def coverage(request: Request) -> dict[str, object]:
    _verify_internal_request(request, b"")
    return _state(request).repository.coverage_snapshot()


def _verify_internal_request(request: Request, body: bytes) -> None:
    secret = _state(request).settings.internal_api_secret
    timestamp = request.headers.get("X-Nabaa-Timestamp", "")
    supplied = request.headers.get("X-Nabaa-Signature", "")
    if not secret:
        raise HTTPException(status_code=503, detail="internal API secret is not configured")
    try:
        if abs(time.time() - int(timestamp)) > 300:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="stale request") from exc
    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid signature")


def _state(request: Request) -> ServiceState:
    return request.app.state.service
