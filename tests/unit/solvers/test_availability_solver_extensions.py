"""SQL-shape tests for the extended availability query spec.

Covers the three additions to the spec:

1. nested groups,
2. multiple concepts per rule, and
3. the top-level `demographics` block.

These assert on compiled SQL rather than counts, so they need no database. The
demographics tests are the important ones: they pin the JOIN-to-`person` shape
that the query-optimisation study found to be the win, and assert the absence of
the INTERSECT it replaces.
"""

from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from hutch_bunny.core.rquest_models.availability import AvailabilityQuery
from hutch_bunny.core.solvers.availability_solver import AvailabilitySolver

CLINICAL_TABLES = (
    "measurement",
    "observation",
    "condition_occurrence",
    "drug_exposure",
    "procedure_occurrence",
)


def _rule(value: Any, varcat: str = "Condition", **extra: Any) -> dict[str, Any]:
    return {
        "varname": "OMOP",
        "varcat": varcat,
        "type": "TEXT",
        "oper": "=",
        "value": value,
        **extra,
    }


def compile_sql(payload: dict[str, Any], rounding: int = 0, low_number: int = 0) -> str:
    """Build the final availability query and compile it to a SQL string."""
    engine = create_engine("postgresql+psycopg://user:pass@localhost/db")
    db_client = Mock()
    db_client.engine = engine

    query = AvailabilityQuery.model_validate(
        {
            "uuid": "unique_id",
            "owner": "user1",
            "collection": "collection_id",
            "protocol_version": "v2",
            "char_salt": "salt",
            **payload,
        }
    )
    solver = AvailabilitySolver(db_client, query)

    groups = query.cohort.groups if query.cohort is not None else []
    group_queries = [solver._build_group_query(group, {}) for group in groups]
    statement = solver._construct_final_query(group_queries, rounding, low_number)

    return str(statement.compile(engine, compile_kwargs={"literal_binds": True}))


# --------------------------------------------------------------------------
# 2. Multiple concepts per rule
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_multiple_concepts_become_one_in_clause_per_table() -> None:
    sql = compile_sql(
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [
                    {
                        "rules_oper": "AND",
                        "rules": [_rule(["201826", "4214962", "4181583"])],
                    }
                ],
            }
        }
    )

    for table in CLINICAL_TABLES:
        assert table in sql

    # One IN per table, not one query per concept.
    assert sql.count("IN (201826, 4214962, 4181583)") == len(CLINICAL_TABLES)
    assert "= 201826" not in sql


@pytest.mark.unit
def test_integer_concepts_are_accepted() -> None:
    sql = compile_sql(
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [{"rules_oper": "AND", "rules": [_rule([201826, 4214962])]}],
            }
        }
    )

    assert sql.count("IN (201826, 4214962)") == len(CLINICAL_TABLES)


@pytest.mark.unit
def test_a_single_concept_still_compiles_to_equality() -> None:
    """Regression guard: single-concept SQL must not change shape."""
    sql = compile_sql(
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [{"rules_oper": "AND", "rules": [_rule("201826")]}],
            }
        }
    )

    assert "condition_occurrence.condition_concept_id = 201826" in sql
    assert "IN (201826)" not in sql


# --------------------------------------------------------------------------
# 1. Nested groups
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_nested_group_is_combined_with_its_parents_operator() -> None:
    """`C AND (D OR E)` - the nested OR set intersects the parent's rule."""
    sql = compile_sql(
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [
                    {
                        "rules_oper": "AND",
                        "rules": [_rule("333", "Observation")],
                        "groups": [
                            {
                                "rules_oper": "OR",
                                "rules": [
                                    _rule("444", "Measurement"),
                                    _rule("555", "Procedure"),
                                ],
                            }
                        ],
                    }
                ],
            }
        }
    )

    assert "INTERSECT" in sql
    assert "444" in sql and "555" in sql and "333" in sql


@pytest.mark.unit
def test_three_level_nesting_compiles() -> None:
    sql = compile_sql(
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [
                    {
                        "rules_oper": "AND",
                        "groups": [
                            {
                                "rules_oper": "OR",
                                "groups": [
                                    {
                                        "rules_oper": "AND",
                                        "rules": [_rule("777", "Drug")],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        }
    )

    assert "777" in sql
    assert "drug_exposure" in sql


# --------------------------------------------------------------------------
# 3. The demographics block
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_demographics_block_joins_person_instead_of_intersecting() -> None:
    """The whole point of the block: a JOIN, not a second person_id set.

    The query-optimisation study found the INTERSECT of two multi-million-row
    person_id sets dominated runtime; joining `person` once and counting removes
    it.
    """
    sql = compile_sql(
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [{"rules_oper": "AND", "rules": [_rule("201826")]}],
            },
            "demographics": {"age": {"min": 40, "max": 70}, "gender": [8532]},
        }
    )

    assert "JOIN person ON person.person_id =" in sql
    assert "count(DISTINCT" in sql
    assert "INTERSECT" not in sql
    assert "person.gender_concept_id = 8532" in sql
    assert "person.year_of_birth >= 40" in sql
    assert "person.year_of_birth <= 70" in sql


@pytest.mark.unit
def test_demographics_only_query_scans_person_directly() -> None:
    sql = compile_sql({"demographics": {"age": "40|70", "gender": [8532]}})

    assert "FROM person" in sql
    assert "count(DISTINCT person.person_id)" in sql
    assert "final_group_0" not in sql
    assert "INTERSECT" not in sql


@pytest.mark.unit
def test_demographics_exclude_negates_only_that_field() -> None:
    sql = compile_sql(
        {
            "demographics": {
                "gender": [8532],
                "race": {"concepts": [8527, 8515], "exclude": True},
            }
        }
    )

    assert "person.gender_concept_id = 8532" in sql
    assert "person.race_concept_id NOT IN (8527, 8515)" in sql


@pytest.mark.unit
@pytest.mark.parametrize(
    "age,expected,not_expected",
    [
        ({"min": 40}, ">= 40", "<="),
        ({"max": 70}, "<= 70", ">="),
    ],
)
def test_open_ended_age_emits_one_bound_only(
    age: dict[str, int], expected: str, not_expected: str
) -> None:
    sql = compile_sql({"demographics": {"age": age}})

    assert f"person.year_of_birth {expected}" in sql
    assert f"person.year_of_birth {not_expected}" not in sql


@pytest.mark.unit
def test_obfuscation_is_applied_on_the_join_path() -> None:
    """Rounding and suppression must survive the rewrite."""
    sql = compile_sql(
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [{"rules_oper": "AND", "rules": [_rule("201826")]}],
            },
            "demographics": {"gender": [8532]},
        },
        rounding=10,
        low_number=10,
    )

    assert "round(" in sql
    assert "HAVING count(DISTINCT" in sql


@pytest.mark.unit
def test_query_without_demographics_is_unchanged() -> None:
    """No demographics block means the original counting path, untouched."""
    sql = compile_sql(
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [{"rules_oper": "AND", "rules": [_rule("201826")]}],
            }
        }
    )

    assert "JOIN person" not in sql
    assert "count(DISTINCT" not in sql
    assert "SELECT count(*)" in sql
