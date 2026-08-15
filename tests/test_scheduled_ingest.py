from __future__ import annotations

import json

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
