from opportunity_sentinel.supervisor import (
    DISCOVERY_ROUTES,
    RouteDecision,
    Supervisor,
    refine_query,
)


class FakeRunnable:
    """Stands in for a chat model configured with structured output."""

    def __init__(self, decision: RouteDecision | dict) -> None:
        self.decision = decision
        self.calls: list[list] = []

    def invoke(self, messages, **_kwargs):
        self.calls.append(messages)
        return self.decision


def test_supervisor_returns_the_model_choice_not_a_keyword_match() -> None:
    # The message contains no course keyword at all; only the model's classification
    # decides the route.
    decision = RouteDecision(
        route="find_courses_bootcamps",
        search_query="معسكر الذكاء الاصطناعي",
        reason="student wants training",
    )
    supervisor = Supervisor(FakeRunnable(decision))

    result = supervisor.classify("ودي أطور نفسي في الذكاء الاصطناعي")

    assert result.route == "find_courses_bootcamps"
    assert result.search_query == "معسكر الذكاء الاصطناعي"


def test_supervisor_validates_a_plain_dict_response() -> None:
    supervisor = Supervisor(FakeRunnable({"route": "ask_knowledge", "reason": "question"}))

    result = supervisor.classify("هل دورات طويق مجانية؟")

    assert isinstance(result, RouteDecision)
    assert result.route == "ask_knowledge"
    assert result.extracted_facts == []


def test_supervisor_passes_the_message_to_the_model() -> None:
    runnable = FakeRunnable(RouteDecision(route="update_profile"))
    supervisor = Supervisor(runnable)

    supervisor.classify("أفضل العمل عن بعد")

    system, human = runnable.calls[0]
    assert system[0] == "system"
    assert human == ("human", "أفضل العمل عن بعد")


def test_every_discovery_route_has_a_query_hint() -> None:
    for route in DISCOVERY_ROUTES:
        decision = RouteDecision(route=route, search_query="فرصة")
        assert refine_query(decision) != "فرصة"


def test_refine_query_appends_missing_evidence_on_a_second_attempt() -> None:
    decision = RouteDecision(route="find_scholarships", search_query="منحة")

    first = refine_query(decision)
    second = refine_query(decision, missing_fields=["deadline", "apply_url"])

    assert "official deadline apply_url" in second
    assert "official" not in first


def test_ask_knowledge_is_not_a_discovery_route() -> None:
    assert "ask_knowledge" not in DISCOVERY_ROUTES
    assert "update_profile" not in DISCOVERY_ROUTES
