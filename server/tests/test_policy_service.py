import pytest

from app.privacy import EgressDestination
from app.privacy.service import PolicyService
from app.schemas import PrivacyLevel
from app.schemas.policy import PolicyCapability, PolicyContext


@pytest.mark.parametrize(
    "privacy,local,expected",
    [
        ("L1", False, "require_confirmation"),
        ("L2", False, "deny"),
        ("L2", True, "require_confirmation"),
        ("L3", True, "deny"),
    ],
)
def test_privacy_is_checked_before_confirmation(privacy: str, local: bool, expected: str) -> None:
    service = PolicyService()
    decision = service.evaluate(
        PolicyContext(privacy_level=PrivacyLevel(privacy)),
        PolicyCapability(
            name="test",
            runs_local=local,
            max_privacy_level=PrivacyLevel.L2,
            requires_confirmation=True,
        ),
        "action",
    )
    assert decision.action == expected and decision.policy_version == "policy-v1"


def test_prohibition_survives_confirmation_and_local_only_is_explicit() -> None:
    service = PolicyService()
    context = PolicyContext(privacy_level=PrivacyLevel.L2, confirmed=True)
    capability = PolicyCapability(
        name="test", runs_local=True, max_privacy_level=PrivacyLevel.L2, prohibited=True
    )
    assert service.evaluate(context, capability, "action").action == "deny"
    assert (
        service.authorize(
            "L2", EgressDestination("local", True, PrivacyLevel.L2), phase="model"
        ).action
        == "local_only"
    )
    assert service.rejection("daily_limit", phase="delivery").reason_code == "daily_limit"
    assert service.confirmation("always", preauthorized=True).action == "require_confirmation"
    assert (
        service.confirmation("preauthorized", preauthorized=False).action == "require_confirmation"
    )
    assert service.confirmation("preauthorized", preauthorized=True).action == "allow"
    assert service.confirmation("prohibited", preauthorized=True).action == "deny"
