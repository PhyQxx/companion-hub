import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.harness.billing import compare_bill
from app.ids import uuid7
from app.schemas.billing import BillingEvidence, BillLine, CostSnapshot, LedgerLine

START, END = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC)


def ledger(**changes: object) -> LedgerLine:
    return LedgerLine.model_validate(
        {
            "call_id": str(uuid7()),
            "endpoint": "fixture",
            "provider_request_id": "receipt-private",
            "currency": "CNY",
            "charged_micros": "9007199254740993",
            "state": "estimated",
            **changes,
        }
    )


def invoice(**changes: object) -> BillLine:
    return BillLine.model_validate(
        {
            "endpoint": "fixture",
            "provider_request_id": "receipt-private",
            "currency": "CNY",
            "billed_micros": "9007199254740993",
            **changes,
        }
    )


def snapshot(lines: list[LedgerLine]) -> CostSnapshot:
    return CostSnapshot(period_start=START, period_end=END, lines=lines)


def evidence(lines: list[BillLine], **changes: object) -> BillingEvidence:
    return BillingEvidence.model_validate(
        {
            "period_start": START,
            "period_end": END,
            "declared_complete": True,
            "lines": [line.model_dump() for line in lines],
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes,status",
    [
        ({}, "matched"),
        ({"charged_micros": "9007199254740992"}, "amount_mismatch"),
        ({"currency": "USD"}, "currency_mismatch"),
        ({"currency": None}, "ledger_unknown"),
        ({"state": "reserved"}, "ledger_unknown"),
        ({"state": "unknown"}, "ledger_unknown"),
        ({"charged_micros": None}, "ledger_unknown"),
    ],
)
def test_compare_preserves_unknown_and_currency(changes: dict[str, object], status: str) -> None:
    local, bill = snapshot([ledger(**changes)]), evidence([invoice()])
    before = local.model_dump_json()
    report = compare_bill(local, bill)
    assert report.counts == {status: 1}
    assert report.budget_effect == "none" and report.validation_level == "V2"
    assert report.evidence == "operator_supplied_bill_unverified"
    assert local.model_dump_json() == before
    assert "receipt-private" not in report.model_dump_json()
    assert report.comparisons[0].billed_micros == "9007199254740993"


@pytest.mark.parametrize("side", ["ledger", "bill"])
def test_duplicate_receipts_are_ambiguous(side: str) -> None:
    local, external = [ledger()], [invoice()]
    if side == "ledger":
        local.append(ledger())
    else:
        external.append(invoice(billed_micros="1"))
    report = compare_bill(snapshot(local), evidence(external))
    assert report.counts == {"ambiguous_receipt": 3}


def test_missing_unmatched_and_endpoint_scope() -> None:
    report = compare_bill(
        snapshot([ledger(provider_request_id=None), ledger(endpoint="other")]),
        evidence([invoice()]),
    )
    assert report.counts == {"missing_receipt": 1, "ledger_only": 1, "bill_only": 1}


def test_canonical_report_is_independent_of_export_order() -> None:
    local = [ledger(), ledger(provider_request_id="second")]
    external = [invoice(), invoice(provider_request_id="second")]
    first = compare_bill(snapshot(local), evidence(external))
    second = compare_bill(snapshot(list(reversed(local))), evidence(list(reversed(external))))
    assert first == second


@pytest.mark.parametrize(
    "start,end",
    [(START.replace(tzinfo=None), END), (END, START), (START, END + timedelta(days=366))],
)
def test_invalid_periods_are_rejected(start: datetime, end: datetime) -> None:
    with pytest.raises(ValidationError):
        CostSnapshot(period_start=start, period_end=end, lines=[])


def test_mismatched_period_and_duplicate_calls_are_rejected() -> None:
    local = ledger()
    with pytest.raises(ValidationError, match="duplicate_ledger_call"):
        snapshot([local, local])
    with pytest.raises(ValueError, match="billing_period_mismatch"):
        compare_bill(snapshot([local]), evidence([invoice()], period_end=END + timedelta(days=1)))


@pytest.mark.parametrize("amount", ["-1", "1.5", "NaN", "01", "10000000000000000000"])
def test_invalid_billed_amounts_are_rejected(amount: str) -> None:
    with pytest.raises(ValidationError):
        invoice(billed_micros=amount)


def test_cli_readonly_and_redacted_failures(tmp_path: Path) -> None:
    left, right = tmp_path / "ledger.json", tmp_path / "bill.json"
    left.write_text(snapshot([ledger()]).model_dump_json())
    right.write_text(evidence([invoice()]).model_dump_json())
    root = Path(__file__).resolve().parents[2]
    command = [
        str(root / ".venv/bin/python"),
        str(root / "server/scripts/reconcile_model_bill.py"),
        "--ledger",
        str(left),
        "--bill",
        str(right),
    ]
    before = left.read_bytes(), right.read_bytes()
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout)["counts"] == {"matched": 1}
    assert "receipt-private" not in result.stdout
    assert before == (left.read_bytes(), right.read_bytes())
    right.write_text(evidence([invoice(billed_micros="1")]).model_dump_json())
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    right.write_text('{"private synthetic invalid body": true}')
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and result.stderr.strip() == "ValidationError"
    assert "private" not in result.stderr and result.stdout == ""


def test_ambiguous_report_order_is_canonical() -> None:
    local = snapshot([ledger()])
    external = [invoice(), invoice(billed_micros="1")]
    assert compare_bill(local, evidence(external)) == compare_bill(
        local, evidence(list(reversed(external)))
    )


def test_missing_complete_claim_does_not_pass_cli(tmp_path: Path) -> None:
    left, right = tmp_path / "ledger.json", tmp_path / "bill.json"
    left.write_text(snapshot([]).model_dump_json())
    right.write_text(evidence([], declared_complete=False).model_dump_json())
    root = Path(__file__).resolve().parents[2]
    command = [
        str(root / ".venv/bin/python"),
        str(root / "server/scripts/reconcile_model_bill.py"),
        "--ledger",
        str(left),
        "--bill",
        str(right),
    ]
    assert subprocess.run(command, capture_output=True, timeout=10).returncode == 1
    import os

    right.unlink()
    os.mkfifo(right)
    result = subprocess.run(command, capture_output=True, timeout=10)
    assert result.returncode == 2 and result.stderr.strip() == b"ValueError"
