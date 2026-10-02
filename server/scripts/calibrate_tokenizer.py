"""Inspect declared synthetic usage locally; never invoke a model or download."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.harness.calibration import CalibrationCorpus, calibrate
from app.llm.contracts import ContextTokenizer

MAX_CORPUS_BYTES = 256 * 1024


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--tokenizer-id", required=True)
    parser.add_argument("--tokenizer-sha256", required=True)
    parser.add_argument("--safety-multiplier", type=float, default=1.25)
    parser.add_argument("--protocol-reserve-tokens", type=int, default=256)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        with args.corpus.open("rb") as source:
            payload = source.read(MAX_CORPUS_BYTES + 1)
        if len(payload) > MAX_CORPUS_BYTES:
            raise ValueError("calibration_corpus_too_large")
        corpus = CalibrationCorpus.model_validate_json(payload)
        spec = ContextTokenizer(
            id=args.tokenizer_id,
            sha256=args.tokenizer_sha256,
            safety_multiplier=args.safety_multiplier,
            protocol_reserve_tokens=args.protocol_reserve_tokens,
        )
        report = calibrate(corpus, spec)
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.report:
            args.report.write_text(encoded)
        else:
            print(encoded, end="")
        return 0 if report["all_cases_within_estimate"] else 1
    except Exception as error:
        # Validation errors may echo input text; emit only the class identifier.
        print(json.dumps({"ok": False, "reason": type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
