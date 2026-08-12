from __future__ import annotations

import json
import uuid
from pathlib import Path

from opportunity_sentinel.config import Settings
from opportunity_sentinel.graph import thread_config
from opportunity_sentinel.telegram_bot import _initial_state, create_runtime


def main() -> None:
    runtime = create_runtime(
        Settings(
            checkpoint_db_path=Path(
                f"artifacts/live-workflow-{uuid.uuid4().hex[:10]}.sqlite"
            )
        )
    )
    profiles = runtime.repository.list_profiles()
    if not profiles:
        raise RuntimeError("No onboarded Telegram profile is available")
    profile = profiles[0]
    thread_id = f"smoke-{uuid.uuid4().hex[:12]}"
    result = runtime.graph.invoke(
        _initial_state(thread_id, profile),
        thread_config(thread_id),
    )
    candidate = result.get("candidate") or {}
    collected = result.get("verified_candidates") or []
    observations = result.get("observations") or []
    report = {
        "final_status": result.get("final_status"),
        "paused_for_human_review": "__interrupt__" in result,
        "search_attempts": result.get("search_attempts"),
        "sources_examined": sum(
            item.get("tool") == "open_page" for item in result.get("observations", [])
        ),
        "title": candidate.get("title"),
        "organization": candidate.get("organization"),
        "source_url": candidate.get("source_url"),
        "verification": result.get("verification"),
        "eligibility": result.get("eligibility"),
        "verified_result_count": len(collected),
        "verified_sources": [
            item.get("candidate", {}).get("source_url") for item in collected
        ],
        "tools_used": [item.get("tool") for item in observations],
        "tool_observations": observations,
    }
    evidence_path = Path("artifacts/live-workflow-evidence.json")
    evidence_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
