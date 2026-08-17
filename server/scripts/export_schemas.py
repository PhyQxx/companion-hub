from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from pydantic.json_schema import models_json_schema  # noqa: E402

from app.schemas import SCHEMA_MODELS  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "contracts" / "jsonschema"


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    index: dict[str, Any] = {"schema_version": 1, "schemas": {}}
    expected: set[Path] = set()

    for model in SCHEMA_MODELS:
        name = model.__name__
        path = OUTPUT / f"{name}.schema.json"
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        path.write_text(json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        expected.add(path)
        index["schemas"][name] = path.name

    model_inputs = [(model, "validation") for model in SCHEMA_MODELS]
    model_refs, shared = models_json_schema(
        model_inputs,
        title="Aria Contracts",
        description="Canonical Aria input, output, delivery, and adapter contracts.",
        ref_template="#/$defs/{model}",
    )
    bundle = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "AriaContracts",
        "type": "object",
        "properties": {
            model.__name__: model_refs[(model, "validation")] for model in SCHEMA_MODELS
        },
        "$defs": shared.get("$defs", {}),
    }
    bundle_path = OUTPUT / "AriaContracts.schema.json"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    expected.add(bundle_path)
    index["bundle"] = bundle_path.name

    index_path = OUTPUT / "index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    expected.add(index_path)

    for stale in OUTPUT.glob("*.json"):
        if stale not in expected:
            stale.unlink()


if __name__ == "__main__":
    main()
