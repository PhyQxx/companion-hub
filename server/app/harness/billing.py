"""Pure billing comparison. Never releases quota or verifies provider authenticity."""

import hashlib
import json
from collections import Counter, defaultdict

from app.schemas.billing import (
    BillingComparison,
    BillingEvidence,
    BillingReport,
    BillLine,
    ComparisonStatus,
    CostSnapshot,
    LedgerLine,
)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def compare_bill(ledger: CostSnapshot, bill: BillingEvidence) -> BillingReport:
    if (ledger.period_start, ledger.period_end) != (bill.period_start, bill.period_end):
        raise ValueError("billing_period_mismatch")
    recorded: dict[tuple[str, str], list[LedgerLine]] = defaultdict(list)
    invoiced: dict[tuple[str, str], list[BillLine]] = defaultdict(list)
    rows: list[BillingComparison] = []
    for line in ledger.lines:
        if line.provider_request_id is None:
            rows.append(
                BillingComparison(
                    status="missing_receipt",
                    call_id=line.call_id,
                    endpoint=line.endpoint,
                    ledger_currency=line.currency,
                    held_micros=line.charged_micros,
                )
            )
        else:
            recorded[(line.endpoint, line.provider_request_id)].append(line)
    for item in bill.lines:
        invoiced[(item.endpoint, item.provider_request_id)].append(item)
    for key in sorted(recorded.keys() | invoiced.keys()):
        local, external = recorded.get(key, []), invoiced.get(key, [])
        receipt_hash = _fingerprint(key)
        if len(local) > 1 or len(external) > 1:
            # Do not choose, sum, or deduplicate ambiguous provider receipts.
            for line in local:
                rows.append(
                    BillingComparison(
                        status="ambiguous_receipt",
                        call_id=line.call_id,
                        endpoint=key[0],
                        receipt_sha256=receipt_hash,
                        ledger_currency=line.currency,
                        held_micros=line.charged_micros,
                    )
                )
            for item in external:
                rows.append(
                    BillingComparison(
                        status="ambiguous_receipt",
                        endpoint=key[0],
                        receipt_sha256=receipt_hash,
                        bill_currency=item.currency,
                        billed_micros=item.billed_micros,
                    )
                )
            continue
        if not local:
            rows.append(
                BillingComparison(
                    status="bill_only",
                    endpoint=key[0],
                    receipt_sha256=receipt_hash,
                    bill_currency=external[0].currency,
                    billed_micros=external[0].billed_micros,
                )
            )
            continue
        if not external:
            rows.append(
                BillingComparison(
                    status="ledger_only",
                    call_id=local[0].call_id,
                    endpoint=key[0],
                    receipt_sha256=receipt_hash,
                    ledger_currency=local[0].currency,
                    held_micros=local[0].charged_micros,
                )
            )
            continue
        line, item = local[0], external[0]
        status: ComparisonStatus = (
            "ledger_unknown"
            if line.state != "estimated" or line.charged_micros is None or line.currency is None
            else "currency_mismatch"
            if line.currency != item.currency
            else "amount_mismatch"
            if line.charged_micros != item.billed_micros
            else "matched"
        )
        rows.append(
            BillingComparison(
                status=status,
                call_id=line.call_id,
                endpoint=key[0],
                receipt_sha256=receipt_hash,
                ledger_currency=line.currency,
                bill_currency=item.currency,
                held_micros=line.charged_micros,
                billed_micros=item.billed_micros,
            )
        )
    # Canonical input sorting makes content hashes independent of export order.
    canonical_ledger = ledger.model_dump(mode="json")
    canonical_ledger["lines"] = sorted(canonical_ledger["lines"], key=lambda item: item["call_id"])
    canonical_bill = bill.model_dump(mode="json")
    canonical_bill["lines"] = sorted(
        canonical_bill["lines"], key=lambda item: json.dumps(item, sort_keys=True)
    )
    rows.sort(
        key=lambda item: (
            item.endpoint,
            item.receipt_sha256 or "",
            str(item.call_id or ""),
            json.dumps(item.model_dump(mode="json"), sort_keys=True),
        )
    )
    return BillingReport(
        period_start=ledger.period_start,
        period_end=ledger.period_end,
        bill_declared_complete=bill.declared_complete,
        ledger_sha256=_fingerprint(canonical_ledger),
        bill_sha256=_fingerprint(canonical_bill),
        counts=dict(Counter(row.status for row in rows)),
        comparisons=rows,
    )
