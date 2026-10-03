"""Durable proactive quota acceptance on isolated PostgreSQL storage."""

import os
from pathlib import Path

import pytest
from test_proactive_quota_acceptance import (
    test_competing_event_keys_cannot_exceed_one_daily_slot as check_competing,
)
from test_proactive_quota_acceptance import (
    test_critical_event_bypasses_cap_and_consumes_a_count as check_critical,
)
from test_proactive_quota_acceptance import (
    test_deleting_decision_cannot_reset_spent_daily_quota as check_deletion,
)
from test_proactive_quota_acceptance import (
    test_failed_acceptance_rolls_back_quota_entry as check_rollback,
)
from test_proactive_quota_acceptance import (
    test_ignored_and_recorded_events_do_not_spend_a_slot as check_ignored,
)
from test_proactive_quota_acceptance import (
    test_legacy_decisions_count_once_without_backfilling_body as check_legacy,
)
from test_proactive_quota_acceptance import (
    test_provider_timestamp_cannot_backdate_quota_consumption as check_backdated,
)
from test_proactive_quota_acceptance import (
    test_separate_owners_have_independent_daily_quota as check_owners,
)

from scripts.benchmark_storage import open_storage


@pytest.mark.parametrize(
    "case",
    ["competing", "critical", "deletion", "rollback", "ignored", "legacy", "backdated", "owners"],
)
async def test_postgres_proactive_quota(case: str, tmp_path: Path) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "quota.db", url)
    try:
        checks = {
            "competing": check_competing,
            "critical": check_critical,
            "deletion": check_deletion,
            "rollback": check_rollback,
            "ignored": check_ignored,
            "legacy": check_legacy,
            "backdated": check_backdated,
            "owners": check_owners,
        }
        await checks[case](storage.database)
    finally:
        await storage.close()
