from __future__ import annotations

import itertools
import json

import httpx
import pytest

from scripts import scheduled_job


def test_post_ingest_batches_bounds_candidates_and_source_reports(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_post(api_url: str, path: str, body: bytes, secret: str) -> dict[str, int]:
        assert api_url == "https://example.test"
        assert path == "/internal/ingest/batch"
        assert secret == "secret"
        payload = json.loads(body)
        calls.append(payload)
        return {
            "received": len(payload["candidates"]),
            "verified": len(payload["candidates"]),
            "withheld": 0,
        }

    monkeypatch.setattr(scheduled_job, "_signed_post", fake_post)
    candidates = {
        f"https://example.test/{index}": {"title": f"Opportunity {index}"}
        for index in range(45)
    }
    inventory = {
        f"source-{index}": list(candidates)[index * 9 : (index + 1) * 9]
        for index in range(5)
    }
    reports = {
        source_id: {"discovered": len(urls), "verified": len(urls)}
        for source_id, urls in inventory.items()
    }

    result = scheduled_job._post_ingest_batches(
        "https://example.test",
        "secret",
        candidates,
        "official-connectors",
        reports,
        inventory,
    )

    candidate_calls = [call for call in calls if call["candidates"]]
    report_calls = [call for call in calls if call["source_reports"]]
    assert [len(call["candidates"]) for call in candidate_calls] == [5] * 9
    assert all(call["source_reports"] == {} for call in candidate_calls)
    assert [len(call["source_reports"]) for call in report_calls] == [3, 2]
    assert set().union(*(call["source_reports"] for call in report_calls)) == set(reports)
    assert set().union(*(call["source_inventory"] for call in report_calls)) == set(inventory)
    assert result == {"received": 45, "verified": 45, "withheld": 0}


def test_post_ingest_batches_sends_empty_finalization(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_post(*args) -> dict[str, int]:
        calls.append(json.loads(args[2]))
        return {"received": 0, "verified": 0, "withheld": 0}

    monkeypatch.setattr(scheduled_job, "_signed_post", fake_post)

    result = scheduled_job._post_ingest_batches(
        "https://example.test",
        "secret",
        {},
        "official-connectors",
        {"source-a": {"discovered": 0, "verified": 0}},
        {"source-a": []},
    )

    assert len(calls) == 1
    assert calls[0]["candidates"] == []
    assert calls[0]["source_reports"]["source-a"]["discovered"] == 0
    assert result["received"] == 0


def test_post_ingest_batches_rejects_invalid_batch_size() -> None:
    with pytest.raises(ValueError, match="positive"):
        scheduled_job._post_ingest_batches("url", "secret", {}, "source", batch_size=0)

    with pytest.raises(ValueError, match="report_batch_size"):
        scheduled_job._post_ingest_batches(
            "url", "secret", {}, "source", report_batch_size=0
        )


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "https://api.example/internal"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict:
        return self._payload


def test_a_cold_start_is_retried_instead_of_failing_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Render free instance waking up returned 503 and killed the whole run."""
    responses = [
        httpx.ReadTimeout("cold start"),
        _Response(503),
        _Response(200, {"ingested": 3}),
    ]
    sent: list[dict[str, str]] = []
    clock = itertools.count(1_700_000_000, 7)

    def fake_post(url, **kwargs):  # noqa: ANN001, ARG001
        sent.append(kwargs["headers"])
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(scheduled_job.httpx, "post", fake_post)
    monkeypatch.setattr(scheduled_job.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(scheduled_job.time, "time", lambda: next(clock))

    result = scheduled_job._signed_post("https://api.example", "/internal/deliver", b"", "s3cret")

    assert result == {"ingested": 3}
    assert len(sent) == 3
    # Each attempt signs a fresh timestamp; the API only accepts a recent one, so
    # replaying the first signature would be rejected once the retries take a while.
    timestamps = [headers["X-Nabaa-Timestamp"] for headers in sent]
    assert timestamps == ["1700000000", "1700000007", "1700000014"]
    assert len({headers["X-Nabaa-Signature"] for headers in sent}) == 3


def test_a_client_error_is_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 is our own bug; retrying it just delays the failure four times over."""
    calls = []

    def fake_post(url, **kwargs):  # noqa: ANN001, ARG001
        calls.append(url)
        return _Response(401)

    monkeypatch.setattr(scheduled_job.httpx, "post", fake_post)
    monkeypatch.setattr(scheduled_job.time, "sleep", lambda _seconds: None)

    with pytest.raises(httpx.HTTPStatusError):
        scheduled_job._signed_post("https://api.example", "/internal/deliver", b"", "s3cret")

    assert len(calls) == 1


def test_retries_give_up_after_the_last_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_post(url, **kwargs):  # noqa: ANN001, ARG001
        calls.append(url)
        raise httpx.ConnectError("down")

    monkeypatch.setattr(scheduled_job.httpx, "post", fake_post)
    monkeypatch.setattr(scheduled_job.time, "sleep", lambda _seconds: None)

    with pytest.raises(httpx.ConnectError):
        scheduled_job._signed_post("https://api.example", "/internal/deliver", b"", "s3cret")

    assert len(calls) == scheduled_job.POST_ATTEMPTS


def test_wake_polls_until_the_service_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = [httpx.ConnectError("suspended"), _Response(503), _Response(200)]
    monkeypatch.setattr(scheduled_job.time, "sleep", lambda _seconds: None)

    def fake_get(url, **kwargs):  # noqa: ANN001, ARG001
        assert url.endswith("/readiness")
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(scheduled_job.httpx, "get", fake_get)

    assert scheduled_job._wake("https://api.example") is True


def test_wake_gives_up_rather_than_hanging_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduled_job.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        scheduled_job.httpx, "get", lambda *_args, **_kwargs: _Response(503)
    )

    assert scheduled_job._wake("https://api.example") is False
    assert scheduled_job._wake("") is False
