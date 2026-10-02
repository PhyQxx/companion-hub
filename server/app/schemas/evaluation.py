"""Explicit synthetic fixtures; persisted reports contain only ids and hashes."""

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .common import StrictModel


class FixtureCase(StrictModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    operation: str = Field(min_length=1, max_length=50)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    expected: dict[str, JsonValue] | None = None
    expected_reason: (
        Literal["operation_missing", "skill_arguments_invalid", "invalid_path_parameter"] | None
    ) = None

    @model_validator(mode="after")
    def one_expectation(self) -> "FixtureCase":
        if (self.expected is None) == (self.expected_reason is None):
            raise ValueError("fixture_requires_one_expectation")
        return self


class FixtureEvaluationRequest(StrictModel):
    data_class: Literal["synthetic"]
    cases: list[FixtureCase] = Field(max_length=50)

    @model_validator(mode="after")
    def bound_cases(self) -> "FixtureEvaluationRequest":
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate_fixture_id")
        if len(self.model_dump_json()) > 65536:
            raise ValueError("fixture_payload_too_large")
        return self
