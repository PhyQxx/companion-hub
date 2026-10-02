"""Compare two local normalized JSON files without network or database writes."""

import argparse
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.harness.billing import compare_bill
from app.schemas.billing import BillingEvidence, CostSnapshot


def read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 2 * 1024 * 1024:
            raise ValueError("billing_input_invalid")
        value = source.read(2 * 1024 * 1024 + 1)
    if len(value) > 2 * 1024 * 1024:
        raise ValueError("billing_input_too_large")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--bill", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = compare_bill(
            CostSnapshot.model_validate_json(read(args.ledger)),
            BillingEvidence.model_validate_json(read(args.bill)),
        )
    except Exception as error:
        # Validation details can echo receipt identifiers. Print type only.
        print(type(error).__name__, file=sys.stderr)
        return 2
    print(report.model_dump_json(indent=2))
    return (
        0
        if report.comparisons
        and report.bill_declared_complete
        and all(status == "matched" for status in report.counts)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
