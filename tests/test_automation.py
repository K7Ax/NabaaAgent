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

    # Pinging around the clock costs ~744 instance-hours against Render's 750-hour free
    # cap, which suspended the service near month-end. Riyadh waking hours cost ~570.
    assert 'cron: "*/10 2-20 * * *"' in workflow
    assert '"${NABAA_API_URL%/}/health"' in workflow
    assert "TAVILY_API_KEY" not in workflow
    assert "scheduled_job.py" not in workflow


def test_keepalive_tolerates_a_cold_start_instead_of_emailing() -> None:
    workflow = Path(".github/workflows/keepalive.yml").read_text(encoding="utf-8")

    assert "--retry 5" in workflow
    assert "--retry-all-errors" in workflow
    assert "::warning::" in workflow  # a missed ping warns; it does not fail the run


def test_a_separate_job_reports_real_outages_twice_a_day() -> None:
    """Keepalive stays quiet, so something else has to be loud when production is down."""
    workflow = Path(".github/workflows/health-check.yml").read_text(encoding="utf-8")

    assert 'cron: "0 6,18 * * *"' in workflow
    assert '"${NABAA_API_URL%/}/readiness"' in workflow
    assert "--fail" in workflow


def test_container_starts_the_same_entry_point_as_local_development() -> None:
    """The port is read by __main__.main(), which is covered in tests/test_api.py.

    The container used to inline its own uvicorn command line, so the deployed process
    and the documented one could drift apart without any test noticing.
    """
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert 'CMD ["python", "-m", "opportunity_sentinel"]' in dockerfile
    assert "uvicorn" not in dockerfile
