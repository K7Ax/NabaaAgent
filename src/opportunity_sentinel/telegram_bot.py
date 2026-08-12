from __future__ import annotations

import asyncio
import html
import uuid
from dataclasses import dataclass, field
from datetime import date

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from langgraph.types import Command

from opportunity_sentinel.agents import DiscoveryAgent, VerificationAgent
from opportunity_sentinel.config import Settings, get_settings
from opportunity_sentinel.graph import build_graph, thread_config
from opportunity_sentinel.llm import build_model_router
from opportunity_sentinel.logging import configure_logging, logger
from opportunity_sentinel.models import OpportunityCandidate, OpportunityType, StudentProfile
from opportunity_sentinel.repository import Repository
from opportunity_sentinel.tools import WebResearchTools

MAJORS = {
    "software": "هندسة البرمجيات",
    "cs": "علوم الحاسب",
    "ce": "هندسة الحاسب",
    "it": "تقنية المعلومات",
    "cyber": "الأمن السيبراني",
    "ai": "الذكاء الاصطناعي",
    "data": "علم البيانات",
}
TYPE_LABELS = {
    OpportunityType.INTERNSHIP: "تدريب صيفي",
    OpportunityType.COOP: "تدريب تعاوني",
    OpportunityType.COURSE: "دورات مجانية",
}


@dataclass
class PendingProfile:
    major: str | None = None
    graduation_year: int | None = None


@dataclass
class BotRuntime:
    settings: Settings
    repository: Repository
    graph: object
    pending: dict[int, PendingProfile] = field(default_factory=dict)


def create_runtime(settings: Settings | None = None) -> BotRuntime:
    settings = settings or get_settings()
    llm = build_model_router(
        groq_api_key=settings.groq_api_key,
        openrouter_api_key=settings.openrouter_api_key,
        groq_model=settings.groq_model,
        openrouter_model=settings.openrouter_model,
        timeout=settings.request_timeout_seconds,
    )
    tools = WebResearchTools(settings.search_max_results, settings.request_timeout_seconds)
    graph = build_graph(
        DiscoveryAgent(tools, llm),
        VerificationAgent(llm),
        settings.checkpoint_db_path,
        settings.max_research_attempts,
    )
    return BotRuntime(settings, Repository(settings.data_db_path), graph)


def build_router(runtime: BotRuntime) -> Router:
    router = Router(name="opportunity-sentinel")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if not message.from_user:
            return
        profile = runtime.repository.get_profile(message.from_user.id)
        if profile:
            await message.answer("مرحبًا بعودتك 👋", reply_markup=main_menu())
        else:
            runtime.pending[message.from_user.id] = PendingProfile()
            await message.answer(
                "أهلًا بك في Opportunity Sentinel. اختر تخصصك:",
                reply_markup=major_keyboard(),
            )

    @router.callback_query(F.data.startswith("major:"))
    async def choose_major(callback: CallbackQuery) -> None:
        await callback.answer()
        key = callback.data.split(":", 1)[1]
        pending = runtime.pending.setdefault(callback.from_user.id, PendingProfile())
        pending.major = MAJORS[key]
        await _edit(callback, "اختر سنة التخرج المتوقعة:", year_keyboard())

    @router.callback_query(F.data.startswith("year:"))
    async def choose_year(callback: CallbackQuery) -> None:
        await callback.answer()
        pending = runtime.pending.setdefault(callback.from_user.id, PendingProfile())
        pending.graduation_year = int(callback.data.split(":", 1)[1])
        await _edit(
            callback,
            "ما نوع الفرص الذي تريد متابعته؟",
            type_keyboard("onboard"),
        )

    @router.callback_query(F.data.startswith("onboard:"))
    async def finish_onboarding(callback: CallbackQuery) -> None:
        await callback.answer()
        pending = runtime.pending.get(callback.from_user.id)
        if not pending or not pending.major or not pending.graduation_year:
            await _edit(
                callback,
                "انتهت جلسة التسجيل. اضغط البدء من جديد.",
                restart_keyboard(),
            )
            return
        opportunity_type = OpportunityType(callback.data.split(":", 1)[1])
        runtime.repository.upsert_profile(
            StudentProfile(
                telegram_id=callback.from_user.id,
                major=pending.major,
                graduation_year=pending.graduation_year,
                preferred_types={opportunity_type},
            )
        )
        runtime.pending.pop(callback.from_user.id, None)
        await _edit(
            callback,
            "اكتمل ملفك ✅ يمكنك الآن البحث عن فرص موثقة.",
            main_menu(),
        )

    @router.callback_query(F.data == "menu:home")
    async def home(callback: CallbackQuery) -> None:
        await callback.answer()
        await _edit(callback, "القائمة الرئيسية", main_menu())

    @router.callback_query(F.data == "menu:profile")
    async def profile(callback: CallbackQuery) -> None:
        await callback.answer()
        student = runtime.repository.get_profile(callback.from_user.id)
        if not student:
            await _edit(callback, "لم يكتمل ملفك بعد.", restart_keyboard())
            return
        types = "، ".join(TYPE_LABELS[item] for item in student.preferred_types)
        text = (
            f"👤 التخصص: {html.escape(student.major)}\n"
            f"🎓 سنة التخرج: {student.graduation_year}\n"
            f"🎯 الفرص: {types}"
        )
        await _edit(callback, text, profile_keyboard())

    @router.callback_query(F.data == "profile:reset")
    async def reset_profile(callback: CallbackQuery) -> None:
        await callback.answer()
        runtime.pending[callback.from_user.id] = PendingProfile()
        await _edit(callback, "اختر تخصصك من جديد:", major_keyboard())

    @router.callback_query(F.data == "menu:find")
    async def find(callback: CallbackQuery, bot: Bot) -> None:
        await callback.answer("بدأ البحث والتحقق…")
        profile = runtime.repository.get_profile(callback.from_user.id)
        if not profile:
            await _edit(callback, "أكمل ملفك أولًا.", restart_keyboard())
            return
        await _edit(
            callback,
            "🔎 أبحث الآن في مصادر متعددة وأتحقق من الأدلة…",
            None,
        )
        thread_id = f"opp-{callback.from_user.id}-{uuid.uuid4().hex[:10]}"
        result = await asyncio.to_thread(
            runtime.graph.invoke,
            _initial_state(thread_id, profile),
            thread_config(thread_id),
        )
        if "__interrupt__" in result:
            await _send_admin_review(bot, runtime, thread_id, result)
            await callback.message.answer(
                "وجدت فرصة تحتاج مراجعة بشرية قبل عرضها. أرسلتها للمشرف.",
                reply_markup=main_menu(),
            )
            return
        await _deliver_graph_result(callback.message, runtime, result)

    @router.callback_query(F.data == "menu:saved")
    async def saved(callback: CallbackQuery) -> None:
        await callback.answer()
        items = runtime.repository.list_saved(callback.from_user.id)
        if not items:
            await _edit(
                callback,
                "لا توجد فرص محفوظة حتى الآن.",
                main_menu(),
            )
            return
        await _edit(callback, "فرصك المحفوظة:", main_menu())
        for identifier, candidate in items[:10]:
            await callback.message.answer(
                opportunity_text(candidate),
                reply_markup=opportunity_keyboard(identifier, candidate),
                disable_web_page_preview=True,
            )

    @router.callback_query(F.data.startswith("save:"))
    async def save(callback: CallbackQuery) -> None:
        identifier = callback.data.split(":", 1)[1]
        runtime.repository.save_for_student(callback.from_user.id, identifier)
        await callback.answer("تم حفظ الفرصة ✅")

    @router.callback_query(F.data.startswith("review:"))
    async def review(callback: CallbackQuery, bot: Bot) -> None:
        if callback.from_user.id != runtime.settings.telegram_admin_chat_id:
            await callback.answer("هذا الإجراء للمشرف فقط.", show_alert=True)
            return
        _, decision, thread_id = callback.data.split(":", 2)
        await callback.answer("تم تسجيل القرار")
        result = await asyncio.to_thread(
            runtime.graph.invoke,
            Command(resume={"decision": decision}),
            thread_config(thread_id),
        )
        logger.info("human_review", thread_id=thread_id, decision=decision)
        if "__interrupt__" in result:
            await _edit(callback, "أعيد البحث، والنتيجة الجديدة بانتظار مراجعتك.", None)
            await _send_admin_review(bot, runtime, thread_id, result)
            return

        await _edit(callback, f"اكتملت المراجعة: {decision}", None)
        user_id = user_id_from_thread(thread_id)
        if result.get("final_status") == "verified" and result.get("candidate"):
            candidate = OpportunityCandidate.model_validate(result["candidate"])
            score = result.get("verification", {}).get("score", 0.8)
            identifier = runtime.repository.save_opportunity(candidate, score)
            if user_id:
                await bot.send_message(
                    user_id,
                    "✅ اعتمد المشرف الفرصة بعد مراجعة الأدلة.\n\n"
                    + opportunity_text(candidate),
                    reply_markup=opportunity_keyboard(identifier, candidate),
                    disable_web_page_preview=True,
                )
                runtime.repository.mark_delivered(user_id, identifier)
        elif user_id:
            await bot.send_message(
                user_id,
                "لم تعتمد الفرصة بعد المراجعة؛ لذلك لن نعرضها لك.",
                reply_markup=main_menu(),
            )

    return router


async def _deliver_graph_result(
    message: Message,
    runtime: BotRuntime,
    result: dict,
) -> None:
    if result.get("final_status") != "verified" or not result.get("candidate"):
        await message.answer(
            "لم أعثر حاليًا على فرصة تستوفي التوثيق والأهلية.",
            reply_markup=main_menu(),
        )
        return
    candidate = OpportunityCandidate.model_validate(result["candidate"])
    score = result.get("verification", {}).get("score", 0.8)
    identifier = runtime.repository.save_opportunity(candidate, score)
    if message.chat:
        runtime.repository.mark_delivered(message.chat.id, identifier)
    await message.answer(
        opportunity_text(candidate),
        reply_markup=opportunity_keyboard(identifier, candidate),
        disable_web_page_preview=True,
    )
    await message.answer(
        "يمكنك البحث مجددًا أو مراجعة المحفوظات.",
        reply_markup=main_menu(),
    )


async def _send_admin_review(
    bot: Bot,
    runtime: BotRuntime,
    thread_id: str,
    result: dict,
) -> None:
    admin_id = runtime.settings.telegram_admin_chat_id
    if not admin_id:
        logger.warning("admin_review_unavailable", thread_id=thread_id)
        return
    candidate = result.get("candidate") or {}
    verification = result.get("verification") or {}
    text = (
        "⚠️ فرصة تحتاج مراجعة\n\n"
        f"العنوان: {html.escape(candidate.get('title', 'غير معروف'))}\n"
        f"الجهة: {html.escape(candidate.get('organization', 'غير معروفة'))}\n"
        f"الأسباب: {html.escape(', '.join(verification.get('reasons', [])))}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ اعتماد",
                    callback_data=f"review:approve:{thread_id}",
                ),
                InlineKeyboardButton(
                    text="❌ رفض",
                    callback_data=f"review:reject:{thread_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 إعادة البحث",
                    callback_data=f"review:research_again:{thread_id}",
                )
            ],
        ]
    )
    await bot.send_message(admin_id, text, reply_markup=keyboard)


def user_id_from_thread(thread_id: str) -> int | None:
    """Extract the authenticated Telegram user from our opaque workflow ID."""
    parts = thread_id.split("-", 2)
    if len(parts) != 3 or parts[0] not in {"opp", "alert"}:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _initial_state(thread_id: str, profile: StudentProfile) -> dict:
    preferred = " ".join(TYPE_LABELS[item] for item in profile.preferred_types)
    query = (
        f"{preferred} {profile.major} طلاب الرياض 2026 التقديم مفتوح "
        "site:gov.sa OR site:edu.sa OR site:org.sa"
    )
    return {
        "thread_id": thread_id,
        "search_query": query,
        "search_attempts": 0,
        "student_profile": profile.model_dump(mode="json"),
    }


def opportunity_text(candidate: OpportunityCandidate) -> str:
    deadline = candidate.deadline.isoformat() if candidate.deadline else "غير معلن"
    majors = "، ".join(candidate.accepted_majors) or "غير محددة"
    return (
        f"🎯 <b>{html.escape(candidate.title)}</b>\n\n"
        f"🏢 {html.escape(candidate.organization)}\n"
        f"📍 {html.escape(candidate.city or 'عن بُعد')}\n"
        f"🎓 {html.escape(majors)}\n"
        f"⏳ آخر موعد: {deadline}\n"
        f"🔗 المصدر: {html.escape(str(candidate.source_url))}\n\n"
        "✅ تم التحقق من الحقول الأساسية والأدلة."
    )


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 ابحث عن فرصة", callback_data="menu:find")],
            [
                InlineKeyboardButton(text="🔖 المحفوظات", callback_data="menu:saved"),
                InlineKeyboardButton(text="👤 ملفي", callback_data="menu:profile"),
            ],
        ]
    )


def major_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=value, callback_data=f"major:{key}")]
        for key, value in MAJORS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def year_keyboard() -> InlineKeyboardMarkup:
    first_year = max(2026, date.today().year)
    years = range(first_year, first_year + 6)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=str(year), callback_data=f"year:{year}")
                for year in years
            ]
        ]
    )


def type_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"{prefix}:{kind.value}")]
            for kind, label in TYPE_LABELS.items()
        ]
    )


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ تعديل الملف", callback_data="profile:reset")],
            [InlineKeyboardButton(text="⬅️ الرئيسية", callback_data="menu:home")],
        ]
    )


def restart_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="ابدأ التسجيل", callback_data="profile:reset")]
        ]
    )


def opportunity_keyboard(
    identifier: str,
    candidate: OpportunityCandidate,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 التقديم", url=str(candidate.application_url))],
            [InlineKeyboardButton(text="🔖 حفظ", callback_data=f"save:{identifier}")],
        ]
    )


async def _edit(
    callback: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> None:
    if callback.message:
        await callback.message.edit_text(text, reply_markup=keyboard)


async def run_bot() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required in .env")
    if not settings.telegram_admin_chat_id:
        raise RuntimeError("TELEGRAM_ADMIN_CHAT_ID is required in .env")
    if not settings.groq_api_key and not settings.openrouter_api_key:
        raise RuntimeError("GROQ_API_KEY or OPENROUTER_API_KEY is required in .env")
    configure_logging(settings.log_level)
    runtime = create_runtime(settings)
    bot = Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(runtime))
    logger.info("telegram_bot_started")
    notification_task = asyncio.create_task(notification_loop(bot, runtime))
    try:
        await dispatcher.start_polling(bot)
    finally:
        notification_task.cancel()
        await bot.session.close()


async def notification_loop(bot: Bot, runtime: BotRuntime) -> None:
    interval = runtime.settings.notification_interval_minutes * 60
    while True:
        await asyncio.sleep(interval)
        for profile in runtime.repository.list_profiles():
            thread_id = f"alert-{profile.telegram_id}-{uuid.uuid4().hex[:10]}"
            try:
                result = await asyncio.to_thread(
                    runtime.graph.invoke,
                    _initial_state(thread_id, profile),
                    thread_config(thread_id),
                )
                if "__interrupt__" in result:
                    await _send_admin_review(bot, runtime, thread_id, result)
                    continue
                if result.get("final_status") != "verified" or not result.get("candidate"):
                    continue
                candidate = OpportunityCandidate.model_validate(result["candidate"])
                score = result.get("verification", {}).get("score", 0.8)
                identifier = runtime.repository.save_opportunity(candidate, score)
                if runtime.repository.was_delivered(profile.telegram_id, identifier):
                    continue
                await bot.send_message(
                    profile.telegram_id,
                    "🔔 فرصة جديدة مطابقة لملفك\n\n" + opportunity_text(candidate),
                    reply_markup=opportunity_keyboard(identifier, candidate),
                    disable_web_page_preview=True,
                )
                runtime.repository.mark_delivered(profile.telegram_id, identifier)
            except Exception as exc:
                logger.exception(
                    "scheduled_notification_failed",
                    telegram_id=profile.telegram_id,
                    error=str(exc),
                )


def main() -> None:
    asyncio.run(run_bot())
