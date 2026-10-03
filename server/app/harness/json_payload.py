"""Pure parsing shared by structured model-output policies."""

import json


def load_json_object(raw: str) -> dict[str, object]:
    """Extract an object, retaining the existing Markdown fence behavior."""
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.split("\n", 1)[-1]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in extractor output")
    payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("extractor output is not a JSON object")
    return payload
