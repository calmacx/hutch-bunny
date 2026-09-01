"""Guard the availability spec doc and its JSON Schema against drift.

Three things can fall out of step with each other:

1. the Pydantic models,
2. `docs/availability-query.schema.json`, generated from them, and
3. `docs/availability-query-schema.md`, written by hand.

This module ties all three together. The schema is regenerated and compared
against the committed file, and every fenced ```json block in the doc is parsed
and — when it is a full query — validated against that schema.

```jsonc blocks are deliberately skipped: they carry `//` comments and are
illustrative fragments rather than payloads.
"""

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = REPO_ROOT / "docs"
SPEC_DOC = DOCS / "availability-query-schema.md"
SCHEMA_FILE = DOCS / "availability-query.schema.json"
GENERATOR = REPO_ROOT / "scripts" / "generate_availability_schema.py"

# Fenced blocks tagged exactly `json` (not `jsonc`, `sql`, `bash`, ...).
JSON_BLOCK = re.compile(r"^```json\n(.*?)^```", re.DOTALL | re.MULTILINE)

# Canary: if the extraction regex or the doc structure breaks, the test must
# fail rather than silently pass on an empty list.
MIN_FULL_QUERY_EXAMPLES = 3


def _json_blocks() -> list[tuple[int, str]]:
    """Return (1-indexed block number, raw text) for each ```json block."""
    text = SPEC_DOC.read_text()
    return [(i, m.group(1)) for i, m in enumerate(JSON_BLOCK.finditer(text), start=1)]


def _load_generator() -> Any:
    """Import the schema generator, which lives outside the package."""
    spec = importlib.util.spec_from_file_location(
        "generate_availability_schema", GENERATOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
def test_committed_schema_matches_the_models() -> None:
    """The schema is generated - regenerating it must be a no-op."""
    expected = _load_generator().build_schema()
    committed = json.loads(SCHEMA_FILE.read_text())

    assert committed == expected, (
        "docs/availability-query.schema.json is out of date with the models. "
        "Run: uv run python scripts/generate_availability_schema.py"
    )


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
def test_existing_query_fixtures_still_validate(
    validator: Draft202012Validator,
) -> None:
    """Every payload the test suite already uses must pass the schema.

    The spec changes are meant to be additive, so a fixture written before them
    failing here would mean a genuine break in backwards compatibility.
    """
    fixtures = sorted((REPO_ROOT / "tests" / "queries" / "availability").glob("*.json"))
    assert fixtures, "no availability fixtures found"

    for fixture in fixtures:
        payload = json.loads(fixture.read_text())
        errors = sorted(validator.iter_errors(payload), key=lambda e: e.path)
        assert not errors, "{} fails the schema:\n{}".format(
            fixture.name,
            "\n".join(f"  {list(e.path)}: {e.message}" for e in errors),
        )


@pytest.mark.unit
def test_schema_accepts_a_list_of_concepts(validator: Draft202012Validator) -> None:
    payload = {
        "uuid": "unique_id",
        "owner": "user1",
        "collection": "collection_id",
        "protocol_version": "v2",
        "char_salt": "salt",
        "cohort": {
            "groups_oper": "AND",
            "groups": [
                {
                    "rules_oper": "AND",
                    "rules": [
                        {
                            "varname": "OMOP",
                            "varcat": "Condition",
                            "type": "TEXT",
                            "oper": "=",
                            "value": ["201826", "4214962"],
                        }
                    ],
                }
            ],
        },
    }
    assert not list(validator.iter_errors(payload))


@pytest.mark.unit
def test_schema_accepts_nested_groups(validator: Draft202012Validator) -> None:
    payload = {
        "uuid": "unique_id",
        "owner": "user1",
        "collection": "collection_id",
        "protocol_version": "v2",
        "char_salt": "salt",
        "cohort": {
            "groups_oper": "AND",
            "groups": [
                {
                    "rules_oper": "AND",
                    "groups": [
                        {
                            "rules_oper": "OR",
                            "rules": [
                                {"varcat": "Measurement", "value": "444"},
                                {"varcat": "Procedure", "value": "555"},
                            ],
                        }
                    ],
                }
            ],
        },
    }
    assert not list(validator.iter_errors(payload))


@pytest.mark.unit
@pytest.mark.parametrize(
    "demographics",
    [
        {"age": {"min": 40, "max": 70}},
        {"age": "40|70"},
        {"gender": [8532]},
        {"race": {"concepts": [8527], "exclude": True}},
    ],
)
def test_schema_accepts_demographics_shorthands(
    validator: Draft202012Validator, demographics: dict[str, Any]
) -> None:
    """The generator widens these; the schema must accept what Pydantic does."""
    payload = {
        "uuid": "unique_id",
        "owner": "user1",
        "collection": "collection_id",
        "protocol_version": "v2",
        "char_salt": "salt",
        "demographics": demographics,
    }
    assert not list(validator.iter_errors(payload))
