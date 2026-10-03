"""Global source ownership survives deletion and competing PostgreSQL resolvers."""

import os
from pathlib import Path

import pytest
from test_observation_owner_binding import (
    test_backdated_new_account_cannot_replace_an_established_binding as check_backdated,
)
from test_observation_owner_binding import (
    test_deleted_original_observation_owner_is_not_reassigned as check_deleted,
)
from test_observation_owner_binding import (
    test_disabled_original_is_bound_before_activity_and_not_reassigned as check_disabled,
)
from test_observation_owner_binding import (
    test_inflight_owner_deletion_cannot_accept_data_for_a_successor as check_inflight,
)
from test_observation_owner_binding import (
    test_initial_binding_under_concurrent_resolvers as check_concurrent,
)
from test_observation_owner_binding import (
    test_reenabled_original_owner_keeps_its_binding as check_reenabled,
)

from scripts.benchmark_storage import open_storage


@pytest.mark.parametrize(
    "case", ["deleted", "disabled", "reenabled", "backdated", "concurrent", "inflight"]
)
async def test_postgres_observation_binding(case: str, tmp_path: Path) -> None:
    url = os.getenv("ARIA_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("ARIA_TEST_DATABASE_URL is not configured")
    storage = await open_storage(tmp_path / "unused.db", url)
    try:
        checks = {
            "deleted": check_deleted,
            "disabled": check_disabled,
            "reenabled": check_reenabled,
            "backdated": check_backdated,
            "concurrent": check_concurrent,
            "inflight": check_inflight,
        }
        await checks[case](storage.database)
    finally:
        await storage.close()
