from __future__ import annotations

import asyncio
import contextlib
import html
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

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
from opportunity_sentinel.taxonomy import KSU_TAXONOMY
from opportunity_sentinel.tools import WebResearchTools

MAJORS = {
    "software": "هندسة البرمجيات",
    "cs": "علوم الحاسب",
    "ce": "هندسة الحاسب",
    "it": "تقنية المعلومات",
    "cyber": "الأمن السيبراني",
    "ai": "الذكاء الاصطناعي",
    "data": "علم البيانات",
    "business": "إدارة الأعمال",
    "accounting": "المحاسبة",
    "finance": "المالية",
    "law": "القانون",
    "medicine": "الطب والجراحة",
    "dentistry": "طب الأسنان",
    "pharmacy": "الصيدلة",
    "nursing": "التمريض",
    "architecture": "العمارة والتخطيط",
    "civil": "الهندسة المدنية",
    "electrical": "الهندسة الكهربائية",
    "mechanical": "الهندسة الميكانيكية",
    "science": "العلوم",
    "arts": "الآداب والعلوم الإنسانية",
    "education": "التربية",
    "languages": "اللغات وعلومها",
    "other": "تخصص آخر ضمن الكلية",
}
TYPE_LABELS = {
    OpportunityType.INTERNSHIP: "تدريب صيفي",
    OpportunityType.COOP: "تدريب تعاوني",
    OpportunityType.COURSE: "دورات مجانية",
    OpportunityType.BOOTCAMP: "معسكرات",
    OpportunityType.GRADUATE_PROGRAM: "برامج خريجين",
    OpportunityType.PART_TIME_JOB: "وظائف جزئية",
    OpportunityType.ENTRY_LEVEL_JOB: "وظائف للمبتدئين",
    OpportunityType.SCHOLARSHIP: "منح",
    OpportunityType.COMPETITION: "مسابقات",
    OpportunityType.HACKATHON: "هاكاثونات",
    OpportunityType.EVENT: "فعاليات",
    OpportunityType.VOLUNTEERING: "تطوع",
}

COLLEGES = {
    "ccis": "علوم الحاسب والمعلومات",
    "engineering": "الهندسة",
    "business": "إدارة الأعمال",
    "medicine": "الطب",
    "dentistry": "طب الأسنان",
    "pharmacy": "الصيدلة",
    "nursing": "التمريض",
    "science": "العلوم",
    "architecture": "العمارة والتخطيط",
    "law": "الحقوق والعلوم السياسية",
    "arts": "الآداب",
    "education": "التربية",
    "languages": "اللغات وعلومها",
    "other": "كلية أخرى",
}

MAJOR_COLLEGES: dict[str, str] = {}
for taxonomy_college, taxonomy_majors in KSU_TAXONOMY.items():
    for taxonomy_major in taxonomy_majors:
        existing_key = next((key for key, value in MAJORS.items() if value == taxonomy_major), None)
        major_key = existing_key or f"ksu{len(MAJORS)}"
        MAJORS.setdefault(major_key, taxonomy_major)
        MAJOR_COLLEGES[major_key] = taxonomy_college


@dataclass
class PendingProfile:
    university: str = "King Saud University"
    college: str | None = None
    major: str | None = None
    graduation_year: int | None = None
    preferred_types: set[OpportunityType] = field(default_factory=set)


@dataclass
class BotRuntime:
    settings: Settings
    repository: Repository
    graph: object
    pending: dict[int, PendingProfile] = field(default_factory=dict)
    collection_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_heartbeat_at: datetime | None = None


def create_runtime(
    settings: Settings | None = None,
    repository: Repository | None = None,
    tavily_quota_guard: Callable[[int], bool] | None = None,
) -> BotRuntime:
    settings = settings or get_settings()
    repository = repository or Repository(settings.data_db_path)
    llm = build_model_router(
        groq_api_key=settings.groq_api_key,
        openrouter_api_key=settings.openrouter_api_key,
        groq_model=settings.groq_model,
        openrouter_model=settings.openrouter_model,
        timeout=settings.request_timeout_seconds,
    )
    tools = WebResearchTools(
        settings.search_max_results,
        settings.request_timeout_seconds,
        settings.tavily_api_key,
        tavily_quota_guard=tavily_quota_guard
        or (
            lambda units: repository.consume_quota(
                "tavily", units, settings.tavily_monthly_credit_limit
            )
        ),
    )
    graph = build_graph(
        DiscoveryAgent(tools, llm),
        VerificationAgent(llm),
        settings.checkpoint_db_path,
        settings.max_research_attempts,
    )
    return BotRuntime(settings, repository, graph)


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
                "أهلًا بك في نبأ. اختر جامعتك:",
                reply_markup=university_keyboard(),
            )

    @router.callback_query(F.data.startswith("university:"))
    async def choose_university(callback: CallbackQuery) -> None:
        await callback.answer()
        pending = runtime.pending.setdefault(callback.from_user.id, PendingProfile())
        pending.university = (
            "King Saud University" if callback.data.endswith(":ksu") else "Other University"
        )
        await _edit(callback, "اختر كليتك:", college_keyboard())

    @router.callback_query(F.data.startswith("college:"))
    async def choose_college(callback: CallbackQuery) -> None:
        await callback.answer()
        key = callback.data.split(":", 1)[1]
        pending = runtime.pending.setdefault(callback.from_user.id, PendingProfile())
        pending.college = COLLEGES.get(key, "كلية أخرى")
        await _edit(callback, "اختر تخصصك:", major_keyboard(pending.college))

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
        selected = callback.data.split(":", 1)[1]
        if selected != "done":
            opportunity_type = OpportunityType(selected)
            if opportunity_type in pending.preferred_types:
                pending.preferred_types.remove(opportunity_type)
            else:
                pending.preferred_types.add(opportunity_type)
            await _edit(
                callback,
                "اختر كل أنواع الفرص التي تهمك، ثم اضغط تم:",
                type_keyboard("onboard", pending.preferred_types),
            )
            return
        if not pending.preferred_types:
            await callback.answer("اختر نوعًا واحدًا على الأقل", show_alert=True)
            return
        runtime.repository.upsert_profile(
            StudentProfile(
                telegram_id=callback.from_user.id,
                major=pending.major,
                graduation_year=pending.graduation_year,
                preferred_types=pending.preferred_types,
                university=pending.university,
                college=pending.college,
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
        await _edit(callback, "اختر جامعتك من جديد:", university_keyboard())

    @router.callback_query(F.data == "menu:find")
    async def find(callback: CallbackQuery, bot: Bot) -> None:
        await callback.answer("بدأت دورة بحث حقيقية…")
        profile = runtime.repository.get_profile(callback.from_user.id)
        if not profile:
            await _edit(callback, "أكمل ملفك أولًا.", restart_keyboard())
            return
        cycle_result: dict[str, object] | None = None
        if runtime.collection_lock.locked():
            await _edit(
                callback,
                "🔎 الباحث يعمل الآن في الخلفية. سأعرض المخزون الموثق الحالي، "
                "وأرسل الجديد فور اكتمال الدورة.",
                main_menu(),
            )
        else:
            await _edit(
                callback,
                "🔎 أبحث الآن في المصادر الرسمية وLinkedIn وX عبر Tavily…\n"
                "قد تستغرق الدورة نحو دقيقة، وسأعرض النتيجة عند اكتمالها.",
                None,
            )
            cycle_result = await run_collection_cycle(runtime, ("fast", "deep"))
            await notify_new_opportunities(
                bot, runtime, cycle_result["new_ids"], exclude={callback.from_user.id}
            )
        matches = runtime.repository.list_scored_matches(profile, limit=500)
        if not matches:
            missing_requirement = runtime.repository.next_unknown_requirement(
                callback.from_user.id
            )
            if missing_requirement:
                await _edit(
                    callback,
                    "وجدت فرصًا موثقة، لكن لن أعرض رابط التقديم قبل أن أتأكد من "
                    "استيفائك للشروط. اضغط تأكيد أهليتي للإجابة بالأزرار.",
                    eligibility_prompt_keyboard(),
                )
                return
            await _edit(
                callback,
                "لا توجد الآن فرصة موثقة تطابق ملفك. سأرسل لك تلقائيًا عند اكتشاف واحدة.",
                main_menu(),
            )
            return
        cycle_summary = _cycle_summary(cycle_result) if cycle_result else ""
        await _edit(
            callback,
            f"اكتملت المراجعة. وجدت <b>{len(matches)}</b> فرصة موثقة تناسب ملفك."
            f"{cycle_summary}\n\nاختر الفئة، ثم تنقّل بين جميع الفرص بالأسهم:",
            results_category_keyboard(matches),
        )

    @router.callback_query(F.data.startswith("results:"))
    async def browse_results(callback: CallbackQuery) -> None:
        await callback.answer()
        profile = runtime.repository.get_profile(callback.from_user.id)
        if not profile:
            await _edit(callback, "أكمل ملفك أولًا.", restart_keyboard())
            return
        parts = callback.data.split(":")
        if len(parts) != 3:
            await _edit(callback, "تعذر فتح هذه الصفحة.", main_menu())
            return
        kind = parts[1]
        try:
            index = max(0, int(parts[2]))
        except ValueError:
            index = 0
        all_matches = runtime.repository.list_scored_matches(profile, limit=500)
        matches = [
            item
            for item in all_matches
            if kind == "all" or item[1].opportunity_type.value == kind
        ]
        if not matches:
            await _edit(
                callback,
                "لا توجد فرص في هذه الفئة الآن.",
                results_category_keyboard(all_matches),
            )
            return
        index = min(index, len(matches) - 1)
        identifier, candidate, score, reasons = matches[index]
        why = "، ".join(reasons[:3])
        await _edit(
            callback,
            f"<b>{index + 1} من {len(matches)}</b>\n"
            f"⭐ المطابقة: {score}/100\n"
            f"لماذا تناسبك؟ {html.escape(why)}\n\n"
            + opportunity_text(candidate),
            result_carousel_keyboard(kind, index, len(matches), identifier, candidate),
        )

    @router.callback_query(F.data == "menu:results")
    async def results_menu(callback: CallbackQuery) -> None:
        await callback.answer()
        profile = runtime.repository.get_profile(callback.from_user.id)
        if not profile:
            await _edit(callback, "أكمل ملفك أولًا.", restart_keyboard())
            return
        matches = runtime.repository.list_scored_matches(profile, limit=500)
        await _edit(
            callback,
            f"لديك <b>{len(matches)}</b> فرصة موثقة. اختر الفئة:",
            results_category_keyboard(matches),
        )

    @router.callback_query(F.data == "menu:search_status")
    async def search_status(callback: CallbackQuery) -> None:
        await callback.answer()
        await _edit(callback, collection_status_text(runtime), main_menu())

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

    @router.callback_query(F.data.startswith("dismiss:"))
    async def dismiss(callback: CallbackQuery) -> None:
        identifier = callback.data.split(":", 1)[1]
        runtime.repository.dismiss_match(callback.from_user.id, identifier)
        await callback.answer("لن نعرض هذه الفرصة لك مجددًا")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(F.data == "menu:confirm")
    async def confirm_eligibility(callback: CallbackQuery) -> None:
        await callback.answer()
        key = runtime.repository.next_unknown_requirement(callback.from_user.id)
        if not key:
            await _edit(callback, "لا توجد متطلبات ناقصة الآن ✅", main_menu())
            return
        await _edit(callback, requirement_question(key), requirement_keyboard(key))

    @router.callback_query(F.data.startswith("answer:"))
    async def save_answer(callback: CallbackQuery) -> None:
        _, key, value = callback.data.split(":", 2)
        parsed: str | bool | float = {"yes": True, "no": False}.get(value, value)
        if key == "gpa":
            parsed = float(value)
        runtime.repository.save_profile_answer(callback.from_user.id, key, parsed)
        await callback.answer("تم حفظ الإجابة")
        next_key = runtime.repository.next_unknown_requirement(callback.from_user.id)
        if next_key:
            await _edit(callback, requirement_question(next_key), requirement_keyboard(next_key))
        else:
            await _edit(callback, "اكتملت معلومات الأهلية المطلوبة ✅", main_menu())

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
                    "✅ اعتمد المشرف الفرصة بعد مراجعة الأدلة.\n\n" + opportunity_text(candidate),
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
    collected = result.get("verified_candidates") or []
    if not collected and result.get("candidate") and result.get("final_status") == "verified":
        collected = [
            {
                "candidate": result["candidate"],
                "verification": result.get("verification") or {},
            }
        ]
    if result.get("final_status") != "verified" or not collected:
        await message.answer(
            "لم أعثر حاليًا على فرصة تستوفي التوثيق والأهلية.",
            reply_markup=main_menu(),
        )
        return
    unique: dict[str, dict] = {}
    for item in collected:
        candidate_data = item.get("candidate") or {}
        source_url = str(candidate_data.get("source_url") or "")
        if source_url:
            unique.setdefault(source_url, item)
    delivered = 0
    for item in list(unique.values())[:5]:
        candidate = OpportunityCandidate.model_validate(item["candidate"])
        score = item.get("verification", {}).get("score", 0.8)
        identifier = runtime.repository.save_opportunity(candidate, score)
        if message.chat:
            runtime.repository.mark_delivered(message.chat.id, identifier)
        await message.answer(
            opportunity_text(candidate),
            reply_markup=opportunity_keyboard(identifier, candidate),
            disable_web_page_preview=True,
        )
        delivered += 1
    await message.answer(
        f"✅ تحققت من {delivered} فرص مناسبة. يمكنك البحث مجددًا أو مراجعة المحفوظات.",
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
    month_names = [
        "يناير",
        "فبراير",
        "مارس",
        "أبريل",
        "مايو",
        "يونيو",
        "يوليو",
        "أغسطس",
        "سبتمبر",
        "أكتوبر",
        "نوفمبر",
        "ديسمبر",
    ]
    today = date.today()
    query = (
        f"{preferred} {profile.major} طلاب الرياض "
        f"{month_names[today.month - 1]} {today.year} التسجيل مفتوح"
    )
    return {
        "thread_id": thread_id,
        "search_query": query,
        "search_attempts": 0,
        "student_profile": profile.model_dump(mode="json"),
    }


def opportunity_text(candidate: OpportunityCandidate) -> str:
    deadline = candidate.deadline.isoformat() if candidate.deadline else "مفتوح وفق الصفحة الرسمية"
    majors = "، ".join(candidate.accepted_majors) or (
        "فرصة تقنية — راجع الشروط الرسمية" if candidate.technical_focus else "غير محددة"
    )
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
            [InlineKeyboardButton(text="🔄 ابحث وحدّث الآن", callback_data="menu:find")],
            [InlineKeyboardButton(text="📡 حالة الباحث", callback_data="menu:search_status")],
            [
                InlineKeyboardButton(text="🔖 المحفوظات", callback_data="menu:saved"),
                InlineKeyboardButton(text="👤 ملفي", callback_data="menu:profile"),
            ],
        ]
    )


def eligibility_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ تأكيد أهليتي", callback_data="menu:confirm")],
            [InlineKeyboardButton(text="⬅️ الرئيسية", callback_data="menu:home")],
        ]
    )


def major_keyboard(college: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=value, callback_data=f"major:{key}")]
        for key, value in MAJORS.items()
        if not college or college == "كلية أخرى" or MAJOR_COLLEGES.get(key) == college
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def university_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="جامعة الملك سعود", callback_data="university:ksu")],
            [InlineKeyboardButton(text="جامعة أخرى", callback_data="university:other")],
        ]
    )


def college_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=value, callback_data=f"college:{key}")]
            for key, value in COLLEGES.items()
        ]
    )


def year_keyboard() -> InlineKeyboardMarkup:
    first_year = max(2026, date.today().year)
    years = range(first_year, first_year + 6)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=str(year), callback_data=f"year:{year}") for year in years]
        ]
    )


def type_keyboard(
    prefix: str, selected: set[OpportunityType] | None = None
) -> InlineKeyboardMarkup:
    selected = selected or set()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("✅ " if kind in selected else "") + label,
                    callback_data=f"{prefix}:{kind.value}",
                )
            ]
            for kind, label in TYPE_LABELS.items()
        ]
        + [[InlineKeyboardButton(text="تم الاختيار", callback_data=f"{prefix}:done")]]
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
        inline_keyboard=[[InlineKeyboardButton(text="ابدأ التسجيل", callback_data="profile:reset")]]
    )


def opportunity_keyboard(
    identifier: str,
    candidate: OpportunityCandidate,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 التقديم", url=str(candidate.application_url))],
            [
                InlineKeyboardButton(text="🔖 حفظ", callback_data=f"save:{identifier}"),
                InlineKeyboardButton(text="🙈 غير مهتم", callback_data=f"dismiss:{identifier}"),
            ],
        ]
    )


def results_category_keyboard(
    matches: list[tuple[str, OpportunityCandidate, int, list[str]]],
) -> InlineKeyboardMarkup:
    counts: dict[OpportunityType, int] = {}
    for _, candidate, _, _ in matches:
        counts[candidate.opportunity_type] = counts.get(candidate.opportunity_type, 0) + 1
    rows = [
        [InlineKeyboardButton(text=f"كل الفرص ({len(matches)})", callback_data="results:all:0")]
    ]
    rows.extend(
        [
            InlineKeyboardButton(
                text=f"{TYPE_LABELS[kind]} ({count})",
                callback_data=f"results:{kind.value}:0",
            )
        ]
        for kind, count in counts.items()
    )
    rows.append([InlineKeyboardButton(text="⬅️ الرئيسية", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def result_carousel_keyboard(
    kind: str,
    index: int,
    total: int,
    identifier: str,
    candidate: OpportunityCandidate,
) -> InlineKeyboardMarkup:
    navigation: list[InlineKeyboardButton] = []
    if index > 0:
        navigation.append(
            InlineKeyboardButton(text="◀️ السابق", callback_data=f"results:{kind}:{index - 1}")
        )
    if index + 1 < total:
        navigation.append(
            InlineKeyboardButton(text="التالي ▶️", callback_data=f"results:{kind}:{index + 1}")
        )
    rows = [
        [InlineKeyboardButton(text="🚀 التقديم", url=str(candidate.application_url))],
        [
            InlineKeyboardButton(text="🔖 حفظ", callback_data=f"save:{identifier}"),
            InlineKeyboardButton(text="🙈 غير مهتم", callback_data=f"dismiss:{identifier}"),
        ],
    ]
    if navigation:
        rows.append(navigation)
    rows.extend(
        [
            [InlineKeyboardButton(text="📚 جميع التصنيفات", callback_data="menu:results")],
            [InlineKeyboardButton(text="⬅️ الرئيسية", callback_data="menu:home")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def requirement_question(key: str) -> str:
    return {
        "saudi_national": "هل تستوفي شرط الجنسية السعودية؟",
        "gender": "ما الفئة المطابقة لك عند وجود شرط رسمي؟",
        "gpa": "ما نطاق معدلك؟ لا نحتاج المعدل الدقيق.",
        "english_level": "ما مستوى اللغة الإنجليزية؟",
        "degree_level": "ما درجتك العلمية الحالية؟",
    }.get(key, f"هل تستوفي المتطلب الرسمي التالي؟ {html.escape(key)}")


def requirement_keyboard(key: str) -> InlineKeyboardMarkup:
    choices = {
        "gender": [("طالب", "male"), ("طالبة", "female")],
        "gpa": [("4.5 فأعلى", "4.5"), ("4.0 فأعلى", "4.0"), ("3.0 فأعلى", "3.0")],
        "english_level": [("مبتدئ", "beginner"), ("متوسط", "intermediate"), ("متقدم", "advanced")],
        "degree_level": [
            ("دبلوم", "diploma"),
            ("بكالوريوس", "bachelor"),
            ("دراسات عليا", "graduate"),
        ],
    }.get(key, [("نعم", "yes"), ("لا", "no")])
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"answer:{key}:{value}")]
            for label, value in choices
        ]
    )


async def _edit(
    callback: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> None:
    if callback.message:
        await callback.message.edit_text(text, reply_markup=keyboard)


async def run_collection_cycle(
    runtime: BotRuntime, modes: tuple[str, ...]
) -> dict[str, object]:
    """Run the same real collectors used by automation and report DB-level changes."""
    if runtime.collection_lock.locked():
        return {"status": "busy", "new_ids": set(), "runs": []}
    async with runtime.collection_lock:
        before = runtime.repository.verified_opportunity_ids()
        runs: list[dict[str, object]] = []
        for mode in modes:
            runs.append(await _run_collector_process(runtime.settings, mode))
        runtime.repository.expire_stale()
        after = runtime.repository.verified_opportunity_ids()
        return {
            "status": "completed",
            "new_ids": after - before,
            "runs": runs,
        }


async def _run_collector_process(settings: Settings, mode: str) -> dict[str, object]:
    project_root = Path(__file__).resolve().parents[2]
    script = project_root / "scripts" / "scheduled_job.py"
    if not script.exists():
        return {"mode": mode, "ok": False, "error": "collector_script_missing"}
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        mode,
        cwd=str(project_root),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=settings.discovery_cycle_timeout_seconds
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        logger.error("collector_timeout", mode=mode)
        return {"mode": mode, "ok": False, "error": "timeout"}
    output = stdout.decode("utf-8", "replace").strip().splitlines()
    parsed: dict[str, object] = {}
    for line in reversed(output):
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                parsed = value
                break
        except json.JSONDecodeError:
            continue
    if process.returncode != 0:
        error = stderr.decode("utf-8", "replace")[-500:]
        logger.error("collector_failed", mode=mode, error=error)
        return {"mode": mode, "ok": False, "error": error or "nonzero_exit"}
    return {**parsed, "collector_mode": mode, "ok": True}


async def notify_new_opportunities(
    bot: Bot,
    runtime: BotRuntime,
    new_ids: set[str],
    exclude: set[int] | None = None,
) -> int:
    if not new_ids:
        return 0
    exclude = exclude or set()
    sent = 0
    for profile in runtime.repository.list_profiles():
        if profile.telegram_id in exclude:
            continue
        all_matches = runtime.repository.list_scored_matches(profile, limit=500)
        new_matches = [
            item
            for item in all_matches
            if item[0] in new_ids
            and not runtime.repository.was_delivered(profile.telegram_id, item[0])
        ]
        if len(new_matches) > 3:
            await bot.send_message(
                profile.telegram_id,
                f"🔔 اكتشف الباحث <b>{len(new_matches)}</b> فرصة جديدة موثقة.\n"
                "يمكنك استعراضها مع بقية الفرص حسب التصنيف، دون عشرات الرسائل:",
                reply_markup=results_category_keyboard(all_matches),
            )
            for identifier, _, _, _ in new_matches:
                runtime.repository.mark_delivered(profile.telegram_id, identifier)
            sent += len(new_matches)
            continue
        for identifier, candidate, score, _ in new_matches:
            await bot.send_message(
                profile.telegram_id,
                f"🔔 فرصة جديدة اكتشفها الباحث الآن\n⭐ المطابقة: {score}/100\n\n"
                + opportunity_text(candidate),
                reply_markup=opportunity_keyboard(identifier, candidate),
                disable_web_page_preview=True,
            )
            runtime.repository.mark_delivered(profile.telegram_id, identifier)
            sent += 1
    return sent


def collection_status_text(runtime: BotRuntime) -> str:
    fast = runtime.repository.latest_crawl("official-connectors")
    deep = runtime.repository.latest_crawl("web-discovery")
    connected_source_ids = {
        "tuwaiq",
        "future-skills",
        "financial-academy",
        "ksu-alumni-gate",
        "public-ats",
    }
    connected_sources = [
        source
        for source in runtime.repository.source_health()
        if source["id"] in connected_source_ids
    ]
    healthy_sources = sum(bool(source["last_success_at"]) for source in connected_sources)
    failing_sources = sum(int(source["consecutive_failures"] > 0) for source in connected_sources)
    verified_count = len(runtime.repository.verified_opportunity_ids())
    running = (
        "يعمل الآن 🔎"
        if runtime.collection_lock.locked()
        else "يعمل وينتظر الدورة القادمة 🟢"
    )
    return (
        f"📡 <b>حالة الباحث</b>\n\n"
        f"الحالة: {running}\n"
        f"المخزون الموثق: {verified_count} فرصة\n"
        f"تغطية الموصلات الحية: {healthy_sources}/{len(connected_source_ids)}"
        f" | المتعثرة: {failing_sources}\n"
        f"آخر فحص رسمي: {_crawl_line(fast)}\n"
        f"آخر بحث عميق: {_crawl_line(deep)}\n"
        "الفحص الرسمي القادم: خلال "
        f"{runtime.settings.official_scan_interval_minutes} دقيقة كحد أقصى\n"
        f"البحث العميق: كل {runtime.settings.notification_interval_minutes // 60} ساعات\n\n"
        "LinkedIn وX مصادر اكتشاف، ولا تُنشر الفرصة إلا بعد إثبات أنها تقنية ومفتوحة."
    )


def _cycle_summary(result: dict[str, object]) -> str:
    runs = result.get("runs") or []
    received = verified = failed = 0
    if isinstance(runs, list):
        for run in runs:
            if not isinstance(run, dict):
                continue
            received += int(run.get("received") or 0)
            verified += int(run.get("verified") or 0)
            failed += int(not bool(run.get("ok")))
    new_ids = result.get("new_ids") or set()
    new_count = len(new_ids) if isinstance(new_ids, (set, list, tuple)) else 0
    failure_note = f" | قنوات فشلت: {failed}" if failed else ""
    return (
        f"\n\n📊 هذه الدورة: فُحص {received}، وُثّق {verified}، "
        f"الجديد {new_count}{failure_note}."
    )


def _crawl_line(run: dict[str, object] | None) -> str:
    if not run:
        return "لم يعمل بعد"
    timestamp = str(run.get("completed_at") or run.get("started_at") or "")
    try:
        value = datetime.fromisoformat(timestamp).astimezone()
        shown = value.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        shown = timestamp[:16]
    return (
        f"{shown} | اكتشف {run.get('discovered_count', 0)} | "
        f"وثّق {run.get('verified_count', 0)} | {run.get('status', 'unknown')}"
    )


def _crawl_due(run: dict[str, object] | None, interval: timedelta) -> bool:
    if not run:
        return True
    timestamp = str(run.get("completed_at") or run.get("started_at") or "")
    try:
        return datetime.now(UTC) - datetime.fromisoformat(timestamp) >= interval
    except ValueError:
        return True


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
    collector_task = asyncio.create_task(collection_loop(bot, runtime))
    logger.info("telegram_bot_started")
    try:
        await dispatcher.start_polling(bot)
    finally:
        collector_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await collector_task
        await bot.session.close()


async def collection_loop(bot: Bot, runtime: BotRuntime) -> None:
    """Continuously collect centrally; never run one expensive search per student."""
    await asyncio.sleep(10)
    while True:
        fast = runtime.repository.latest_crawl("official-connectors")
        deep = runtime.repository.latest_crawl("web-discovery")
        modes: list[str] = []
        if _crawl_due(
            fast, timedelta(minutes=runtime.settings.official_scan_interval_minutes)
        ):
            modes.append("fast")
        if _crawl_due(
            deep, timedelta(minutes=runtime.settings.notification_interval_minutes)
        ):
            modes.append("deep")
        if modes:
            try:
                result = await run_collection_cycle(runtime, tuple(modes))
                await notify_new_opportunities(bot, runtime, result["new_ids"])
            except Exception as exc:
                logger.exception("background_collection_failed", error=str(exc))
        heartbeat_interval = timedelta(hours=runtime.settings.search_heartbeat_hours)
        if runtime.last_heartbeat_at is None:
            runtime.last_heartbeat_at = datetime.now(UTC)
        elif datetime.now(UTC) - runtime.last_heartbeat_at >= heartbeat_interval:
            for profile in runtime.repository.list_profiles():
                await bot.send_message(
                    profile.telegram_id,
                    "🟢 نبض الباحث اليومي\n\n" + collection_status_text(runtime),
                    reply_markup=main_menu(),
                )
            runtime.last_heartbeat_at = datetime.now(UTC)
        await asyncio.sleep(60)


def main() -> None:
    asyncio.run(run_bot())
