from fastapi.testclient import TestClient

from opportunity_sentinel.api import app


def test_health_endpoint() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "opportunity-sentinel"}


def test_readiness_never_exposes_secrets() -> None:
    with TestClient(app) as client:
        response = client.get("/readiness")
    assert response.status_code == 200
    assert "groq_api_key" not in response.text
    assert "openrouter_api_key" not in response.text
