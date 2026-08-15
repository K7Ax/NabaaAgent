from pathlib import Path


def test_discovery_schedule_is_central_and_fails_closed_without_api() -> None:
    workflow = Path(".github/workflows/discovery.yml").read_text(encoding="utf-8")

    assert 'cron: "17,47 * * * *"' in workflow
    assert "Discovery skipped safely" in workflow
    assert 'modes="fast"' in workflow
    assert 'modes="$modes deep"' in workflow
    assert 'modes="$modes revalidate"' in workflow
    assert "python scripts/scheduled_job.py deliver" in workflow


def test_keepalive_wakes_health_without_spending_search_credits() -> None:
    workflow = Path(".github/workflows/keepalive.yml").read_text(encoding="utf-8")

    assert 'cron: "3,13,23,33,43,53 * * * *"' in workflow
    assert '"${NABAA_API_URL%/}/health"' in workflow
    assert "TAVILY_API_KEY" not in workflow
    assert "scheduled_job.py" not in workflow


def test_container_uses_host_port_from_platform() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "${PORT:-8000}" in dockerfile
