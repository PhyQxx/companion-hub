from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def evaluate_report(report: dict[str, Any]) -> tuple[bool, list[str]]:
    acceptance = report.get("acceptance")
    if not isinstance(acceptance, dict):
        return False, ["missing_acceptance"]

    checks = {
        "completed_turns_ready": acceptance.get("completed_turns_ready") is True,
        "interrupt_samples_ready": acceptance.get("interrupt_samples_ready") is True,
        "first_audio_p90_pass": acceptance.get("first_audio_p90_pass") is True,
        "interrupt_p90_pass": acceptance.get("interrupt_p90_pass") is True,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return not failed, failed


def fetch_report(url: str, *, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("voice latency endpoint did not return an object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate P6 M2 voice latency acceptance.")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000/api/v1/meta/voice/latency",
        help="Running Hub voice latency endpoint.",
    )
    parser.add_argument("--report", type=Path, help="Optional JSON output path.")
    args = parser.parse_args()

    try:
        report = fetch_report(args.url)
    except (OSError, ValueError, urllib.error.URLError) as error:
        print(json.dumps({"ok": False, "reason": type(error).__name__}, ensure_ascii=False))
        return 2

    passed, failed = evaluate_report(report)
    result = {"ok": passed, "failed_checks": failed, "report": report}
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
