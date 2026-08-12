import json

import httpx

from opportunity_sentinel.llm import ModelRouter, Provider, _strict_json_schema
from opportunity_sentinel.models import VerificationReport, VerificationStatus


def test_model_router_falls_back_to_second_provider() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            return httpx.Response(429, json={"error": "rate limited"})
        content = VerificationReport(
            status=VerificationStatus.VERIFIED,
            score=0.95,
            reasons=["fallback_worked"],
        ).model_dump_json()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    router = ModelRouter(
        [
            Provider("primary", "https://primary.test/v1", "secret", "model-a"),
            Provider("fallback", "https://fallback.test/v1", "secret", "model-b"),
        ]
    )
    router.client = httpx.Client(transport=httpx.MockTransport(handler))
    result = router.generate_json(
        system="Return verification JSON.",
        user=json.dumps({"candidate": "test"}),
        schema=VerificationReport,
        task="fallback_test",
    )
    assert result.status == VerificationStatus.VERIFIED
    assert calls == ["primary.test", "fallback.test"]


def test_schema_is_closed_for_strict_structured_output() -> None:
    schema = VerificationReport.model_json_schema()
    strict = _strict_json_schema(schema)

    assert strict["additionalProperties"] is False
    assert set(strict["required"]) == set(strict["properties"])
    for definition in strict.get("$defs", {}).values():
        if "properties" in definition:
            assert definition["additionalProperties"] is False
            assert set(definition["required"]) == set(definition["properties"])
