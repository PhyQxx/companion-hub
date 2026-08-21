from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast


def _evaluate(report: dict[str, Any]) -> tuple[bool, list[str]]:
    script = Path(__file__).resolve().parents[1] / "scripts" / "p6_voice_m2_report.py"
    namespace = runpy.run_path(str(script), run_name="p6_voice_m2_report_test")
    evaluate = cast(
        Callable[[dict[str, Any]], tuple[bool, list[str]]],
        namespace["evaluate_report"],
    )
    return evaluate(report)


def test_p6_voice_m2_report_requires_all_acceptance_checks() -> None:
    passed, failed = _evaluate(
        {
            "acceptance": {
                "completed_turns_ready": True,
                "interrupt_samples_ready": True,
                "first_audio_p90_pass": True,
                "interrupt_p90_pass": True,
            }
        }
    )
    assert passed is True
    assert failed == []

    passed, failed = _evaluate(
        {
            "acceptance": {
                "completed_turns_ready": True,
                "interrupt_samples_ready": False,
                "first_audio_p90_pass": True,
                "interrupt_p90_pass": False,
            }
        }
    )
    assert passed is False
    assert failed == ["interrupt_samples_ready", "interrupt_p90_pass"]
