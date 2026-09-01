"""Generate the JSON Schema for an availability query from the Pydantic models.

Run this whenever `rquest_models` changes so `docs/availability-query.schema.json`
stays in lockstep with the code:

    uv run python scripts/generate_availability_schema.py

The emitted schema captures structure, types, enums, defaults, and required
fields. Cross-field rules enforced by Pydantic validators are *not* expressible
in JSON Schema and are documented in `docs/availability-query-schema.md`:
  - a query must carry at least one group or a `demographics` block;
  - a group must carry at least one rule or nested group;
  - group nesting may not exceed `MAX_GROUP_DEPTH`;
  - an age range needs at least one bound, and `min <= max`;
  - a demographics block must set at least one field.

`tests/unit/docs/test_doc_examples_validate.py` re-runs this generator and fails
if the committed schema has drifted from the models.
"""

import json
from pathlib import Path
from typing import Any

from hutch_bunny.core.rquest_models.availability import AvailabilityQuery

SCHEMA_ID = "https://hdruk.github.io/hutch-bunny/availability-query.schema.json"
OUTPUT = (
    Path(__file__).resolve().parent.parent / "docs" / "availability-query.schema.json"
)


#: Wire forms the models accept via `mode="before"` validators, which Pydantic
#: cannot infer from the field annotations alone. Each entry widens one property
#: to the full set of shapes a sender may legitimately put on the wire, so the
#: schema validates real payloads rather than only the canonical form.
INPUT_SHORTHANDS: dict[str, dict[str, dict[str, Any]]] = {
    "Rule": {
        # A TEXT rule may carry one concept id or a list of them.
        "value": {
            "anyOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": ["string", "integer"]}},
            ],
            "default": "",
            "title": "Value",
        },
        # Declared `list[int]`, but senders have always used strings here and
        # Pydantic coerces them (see tests/queries/availability/
        # secondary_modifiers.json), so the schema must accept both.
        "secondary_modifier": {
            "anyOf": [
                {"type": "array", "items": {"type": ["string", "integer"]}},
                {"type": "null"},
            ],
            "default": None,
            "title": "Secondary Modifier",
        },
    },
    "Demographics": {
        # An age range may be an object or the legacy "<min>|<max>" string.
        "age": {
            "anyOf": [
                {"$ref": "#/$defs/AgeRange"},
                {"type": "string"},
                {"type": "null"},
            ],
            "default": None,
        },
        # A concept filter may be an object or a bare list of concept ids.
        **{
            field: {
                "anyOf": [
                    {"$ref": "#/$defs/ConceptFilter"},
                    {"type": "array", "items": {"type": "integer"}, "minItems": 1},
                    {"type": "null"},
                ],
                "default": None,
            }
            for field in ("gender", "race", "ethnicity")
        },
    },
}


def build_schema() -> dict[str, Any]:
    """Build the schema document, including the metadata Pydantic omits.

    Returns:
        The complete JSON Schema document.
    """
    schema = AvailabilityQuery.model_json_schema()

    for definition, properties in INPUT_SHORTHANDS.items():
        schema["$defs"][definition]["properties"].update(properties)

    # `values` is derived in `model_post_init`, never sent by a caller.
    schema["$defs"]["Rule"]["properties"]["values"]["readOnly"] = True

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        **schema,
    }


def main() -> None:
    """Write the schema to `docs/availability-query.schema.json`."""
    OUTPUT.write_text(json.dumps(build_schema(), indent=2) + "\n")
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
