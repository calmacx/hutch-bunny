"""Guard the CDS spec doc against drifting from the JSON Schema.

`docs/cds-models-schema.md` is the human-readable contract and
`docs/cds-query.schema.json` is the machine-readable one. It is easy to edit one
and forget the other, so this test extracts every fenced ```json block from the
doc and checks that:

1. it parses as JSON at all, and
2. if it looks like a full query envelope (it has a `uuid`), it validates
   against the schema.

```jsonc blocks are deliberately skipped: they carry `// ...` elisions and are
illustrative fragments rather than payloads.
"""

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

DOCS = Path(__file__).resolve().parents[3] / "docs"
SPEC_DOC = DOCS / "cds-models-schema.md"
SCHEMA_FILE = DOCS / "cds-query.schema.json"

# Fenced blocks tagged exactly `json` (not `jsonc`, `sql`, `bash`, ...).
JSON_BLOCK = re.compile(r"^```json\n(.*?)^```", re.DOTALL | re.MULTILINE)

# Full-query examples we expect to find, as a canary: if the extraction regex
# or the doc structure breaks, the test must fail rather than silently pass on
# an empty list.
MIN_FULL_QUERY_EXAMPLES = 3


def _json_blocks() -> list[tuple[int, str]]:
    """Return (1-indexed block number, raw text) for each ```json block."""
    text = SPEC_DOC.read_text()
    return [(i, m.group(1)) for i, m in enumerate(JSON_BLOCK.finditer(text), start=1)]


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_FILE.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.unit
def test_spec_doc_and_schema_exist() -> None:
    assert SPEC_DOC.is_file(), f"missing {SPEC_DOC}"
    assert SCHEMA_FILE.is_file(), f"missing {SCHEMA_FILE}"


@pytest.mark.unit
@pytest.mark.parametrize("block_number,raw", _json_blocks())
def test_doc_json_block_parses(block_number: int, raw: str) -> None:
    """Every ```json block in the spec doc must be valid JSON."""
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"```json block #{block_number} in {SPEC_DOC.name} is not valid JSON: {exc}"
        )


@pytest.mark.unit
def test_full_query_examples_validate(validator: Draft202012Validator) -> None:
    """Every full query example in the doc must satisfy the JSON Schema."""
    full_queries = [
        (number, payload)
        for number, raw in _json_blocks()
        if isinstance(payload := json.loads(raw), dict) and "uuid" in payload
    ]

    assert len(full_queries) >= MIN_FULL_QUERY_EXAMPLES, (
        f"expected at least {MIN_FULL_QUERY_EXAMPLES} full query examples in "
        f"{SPEC_DOC.name}, found {len(full_queries)} — has the doc structure "
        "or the extraction regex changed?"
    )

    for number, payload in full_queries:
        errors = sorted(validator.iter_errors(payload), key=lambda e: e.path)
        assert not errors, "```json block #{} fails the schema:\n{}".format(
            number,
            "\n".join(f"  {list(e.path)}: {e.message}" for e in errors),
        )


@pytest.mark.unit
def test_schema_rejects_a_query_with_neither_cohort_nor_demographics(
    validator: Draft202012Validator,
) -> None:
    """The `anyOf` on the root must actually bite — an envelope carrying no
    query body would otherwise count the whole population."""
    empty = {"uuid": "unique_id", "collection": "collection_id"}
    assert list(validator.iter_errors(empty)), (
        "schema accepted a query with neither 'cohort' nor 'demographics'"
    )


@pytest.mark.unit
def test_schema_rejects_removed_demographic_node(
    validator: Draft202012Validator,
) -> None:
    """`kind: "demographic"` was removed from the node union in revision 2;
    demographics live in the top-level block instead."""
    payload = {
        "uuid": "unique_id",
        "collection": "collection_id",
        "cohort": {
            "kind": "group",
            "operator": "AND",
            "children": [
                {"kind": "demographic", "field": "gender", "concepts": [8507]}
            ],
        },
    }
    assert list(validator.iter_errors(payload)), (
        "schema still accepts a 'demographic' node inside the cohort tree"
    )


@pytest.mark.unit
def test_schema_accepts_clinical_rule_without_domains(
    validator: Draft202012Validator,
) -> None:
    """Omitting `domains` is the safe default (fan out across all five clinical
    tables), so it must not be a structural error."""
    payload = {
        "uuid": "unique_id",
        "collection": "collection_id",
        "cohort": {
            "kind": "group",
            "operator": "AND",
            "children": [{"kind": "clinical", "concepts": [201826, 4214962]}],
        },
    }
    assert not list(validator.iter_errors(payload))


@pytest.mark.unit
def test_schema_accepts_demographics_only_query(
    validator: Draft202012Validator,
) -> None:
    """A demographics-only count needs no clinical tree."""
    payload = {
        "uuid": "unique_id",
        "collection": "collection_id",
        "demographics": {"age": {"min": 40, "max": 70}, "gender": [8532]},
    }
    assert not list(validator.iter_errors(payload))
