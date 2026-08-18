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


def test_readiness_fails_when_production_data_would_be_lost(monkeypatch) -> None:
    """A production instance on ephemeral SQLite loses every row on restart.

    It used to report itself healthy anyway, so the platform kept serving it and the
    loss stayed invisible until students' saved opportunities disappeared.
    """
    with TestClient(app) as client:
        state = app.state.service
        monkeypatch.setattr(state.settings, "app_env", "production")
        monkeypatch.setattr(state.repository, "backend", "sqlite", raising=False)
        response = client.get("/readiness")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["durable_storage"] is False
