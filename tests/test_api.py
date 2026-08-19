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


def test_the_module_entry_point_serves_the_api_on_the_platform_port(monkeypatch) -> None:
    """Render assigns the port through $PORT; a hardcoded 8000 fails its health check."""
    import opportunity_sentinel.__main__ as entry

    served: dict[str, object] = {}
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: served.update(app=app, **kw))
    monkeypatch.setenv("PORT", "10000")

    entry.main()

    assert served["app"] is app
    assert served["port"] == 10000
    assert served["host"] == "0.0.0.0"
