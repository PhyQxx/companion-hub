from __future__ import annotations

import runpy
from pathlib import Path


def test_p5_real_model_regression_registry_has_unique_20_plus_cases() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "p5_real_model_regression.py"
    namespace = runpy.run_path(str(script), run_name="p5_real_model_regression_test")
    case_ids = namespace["_all_case_ids"]()

    assert len(case_ids) >= 20
    assert len(case_ids) == len(set(case_ids))
    assert {
        "assistant-profile-bootstrap",
        "conflict-active-wins",
        "deletion-does-not-resurrect",
        "l2-isolation",
        "conversation-extraction",
        "timeline-recent",
        "timeline-yesterday-evening",
        "timeline-no-evidence",
    }.issubset(case_ids)
