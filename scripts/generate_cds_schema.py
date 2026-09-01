"""Generate the JSON Schema for a CDS availability query from the Pydantic model.

Run this whenever `cds_models` changes so `docs/cds-query.schema.json` stays in
lockstep with the code:

    uv run python scripts/generate_cds_schema.py

The emitted schema captures structure, types, enums, defaults, and required
fields, plus the root "at least one of cohort / demographics" constraint that
Pydantic cannot express on its own (injected by `build_schema` below).

Cross-field rules enforced by Pydantic `model_validator`s are *not* expressible
in JSON Schema and are documented in `docs/cds-models-schema.md`:
  - Range: at least one of `min`/`max`; `min <= max` when both set.
  - Clinical rule: `domain` and `domains` are mutually exclusive; `value_range`
    requires every effective domain to have a value column.
  - Group: nesting depth <= MAX_GROUP_DEPTH.
  - AFTER group: >= 2 children; clinical/nested-AFTER children only; each
    clinical child resolves to exactly one domain; no child `exclude`; first
    child sets no `after_gap`.
  - Demographics: block non-empty; each sub-filter non-empty; `status: "alive"`
    may not be paired with `cause_concepts` / `age_at_death`.
"""

import json
from pathlib import Path

from hutch_bunny.core.cds_models.query import CdsQuery

SCHEMA_ID = "https://hdruk.github.io/hutch-bunny/cds-query.schema.json"
OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "cds-query.schema.json"

# `cohort` and `demographics` are each individually optional, but a query with
# neither has no body and would count the whole population. Pydantic enforces
# this with a model_validator; JSON Schema needs it spelled out so sender-side
# validation catches it too.
ROOT_ANY_OF = [{"required": ["cohort"]}, {"required": ["demographics"]}]


def build_schema() -> dict:
    schema = CdsQuery.model_json_schema()
    # Prepend standard JSON Schema metadata (Pydantic omits these by default).
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        **schema,
        "anyOf": ROOT_ANY_OF,
    }


def main() -> None:
    schema = build_schema()
    OUTPUT.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
