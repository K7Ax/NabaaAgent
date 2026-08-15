from pathlib import Path


def test_discovery_schedule_is_central_and_fails_closed_without_api() -> None:
    workflow = Path(".github/workflows/discovery.yml").read_text(encoding="utf-8")

    assert 'cron: "17,47 * * * *"' in workflow
    assert "Discovery skipped safely" in workflow
    assert 'modes="fast"' in workflow
    assert 'modes="$modes deep"' in workflow
    assert 'modes="$modes revalidate"' in workflow
    assert "python scripts/scheduled_job.py deliver" in workflow
