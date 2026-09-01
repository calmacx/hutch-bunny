"""Equivalence tests for the extended availability query spec.

Each new form is run against a real database alongside the legacy payload that
expresses the same question, and the two counts must match. This is what proves
the additions are safe: nesting, concept lists and the `demographics` block are
meant to change how a query is *written* and *executed*, never what it *means*.

The demographics cases matter most. They swap an `INTERSECT` of two large
`person_id` sets for a `JOIN` to `person`, which is the rewrite the
query-optimisation study measured; these tests are the guarantee that the faster
plan is also the same plan semantically.

Concepts are discovered from the database rather than hard-coded, so the tests
run against any OMOP collection.
"""

from typing import Any

import pytest
from sqlalchemy import func, select

from hutch_bunny.core.db import BaseDBClient
from hutch_bunny.core.db.entities import ConditionOccurrence, Person
from hutch_bunny.core.rquest_models.availability import AvailabilityQuery
from hutch_bunny.core.solvers.availability_solver import AvailabilitySolver

ENVELOPE = {
    "uuid": "unique_id",
    "owner": "user1",
    "collection": "collection_id",
    "protocol_version": "v2",
    "char_salt": "salt",
}

# Counts are compared directly, so obfuscation is switched off.
NO_OBFUSCATION: list[Any] = [
    {"id": "Rounding", "nearest": 0},
    {"id": "Low Number Suppression", "threshold": 0},
]


@pytest.fixture
def condition_concepts(db_client: BaseDBClient) -> list[str]:
    """The three most common condition concepts in this collection."""
    statement = (
        select(ConditionOccurrence.condition_concept_id)
        .group_by(ConditionOccurrence.condition_concept_id)
        .order_by(func.count().desc())
        .limit(3)
    )
    with db_client.engine.connect() as connection:
        concepts = [str(row[0]) for row in connection.execute(statement)]

    if len(concepts) < 3:
        pytest.skip("collection has fewer than three condition concepts")
    return concepts


@pytest.fixture
def gender_concept(db_client: BaseDBClient) -> str:
    """A gender concept present in this collection."""
    statement = (
        select(Person.gender_concept_id)
        .group_by(Person.gender_concept_id)
        .order_by(func.count().desc())
        .limit(1)
    )
    with db_client.engine.connect() as connection:
        row = connection.execute(statement).fetchone()

    if row is None:
        pytest.skip("collection has no people")
    return str(row[0])


def count_for(db_client: BaseDBClient, payload: dict[str, Any]) -> int:
    """Resolve a query payload to an un-obfuscated count."""
    query = AvailabilityQuery.model_validate({**ENVELOPE, **payload})
    return AvailabilitySolver(db_client, query).solve_query(NO_OBFUSCATION)


def rule(value: Any, varcat: str = "Condition", oper: str = "=") -> dict[str, Any]:
    return {
        "varname": "OMOP",
        "varcat": varcat,
        "type": "TEXT",
        "oper": oper,
        "value": value,
    }


def age_rule(value: str) -> dict[str, Any]:
    return {
        "varname": "AGE",
        "varcat": "Person",
        "type": "NUM",
        "oper": "=",
        "value": value,
    }


@pytest.mark.integration
def test_concept_list_matches_separate_or_rules(
    db_client: BaseDBClient, condition_concepts: list[str]
) -> None:
    """`value: [A, B]` must equal two rules OR'd together."""
    first, second, _ = condition_concepts

    combined = count_for(
        db_client,
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [{"rules_oper": "OR", "rules": [rule([first, second])]}],
            }
        },
    )
    separate = count_for(
        db_client,
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [{"rules_oper": "OR", "rules": [rule(first), rule(second)]}],
            }
        },
    )

    assert combined == separate
    assert combined > 0


@pytest.mark.integration
def test_nested_group_matches_equivalent_flat_groups(
    db_client: BaseDBClient, condition_concepts: list[str]
) -> None:
    """`A AND (B OR C)` nested must equal the same thing as two top-level groups."""
    first, second, third = condition_concepts

    nested = count_for(
        db_client,
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [
                    {
                        "rules_oper": "AND",
                        "rules": [rule(first)],
                        "groups": [
                            {"rules_oper": "OR", "rules": [rule(second), rule(third)]}
                        ],
                    }
                ],
            }
        },
    )
    flat = count_for(
        db_client,
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [
                    {"rules_oper": "AND", "rules": [rule(first)]},
                    {"rules_oper": "OR", "rules": [rule(second), rule(third)]},
                ],
            }
        },
    )

    assert nested == flat


@pytest.mark.integration
def test_demographics_gender_matches_person_rule(
    db_client: BaseDBClient, condition_concepts: list[str], gender_concept: str
) -> None:
    """The JOIN path must agree with the INTERSECT path it replaces."""
    first = condition_concepts[0]
    cohort = {
        "groups_oper": "AND",
        "groups": [{"rules_oper": "AND", "rules": [rule(first)]}],
    }

    with_block = count_for(
        db_client, {"cohort": cohort, "demographics": {"gender": [int(gender_concept)]}}
    )
    with_person_rule = count_for(
        db_client,
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [
                    {"rules_oper": "AND", "rules": [rule(first)]},
                    {
                        "rules_oper": "AND",
                        "rules": [rule(gender_concept, "Person")],
                    },
                ],
            }
        },
    )

    assert with_block == with_person_rule


@pytest.mark.integration
def test_demographics_age_matches_age_rule(
    db_client: BaseDBClient, condition_concepts: list[str]
) -> None:
    """A block age range must agree with the existing `AGE` rule exactly."""
    first = condition_concepts[0]

    with_block = count_for(
        db_client,
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [{"rules_oper": "AND", "rules": [rule(first)]}],
            },
            "demographics": {"age": {"min": 40, "max": 70}},
        },
    )
    with_age_rule = count_for(
        db_client,
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [
                    {"rules_oper": "AND", "rules": [rule(first)]},
                    {"rules_oper": "AND", "rules": [age_rule("40|70")]},
                ],
            }
        },
    )

    assert with_block == with_age_rule


@pytest.mark.integration
def test_demographics_age_and_gender_match_person_rules(
    db_client: BaseDBClient, condition_concepts: list[str], gender_concept: str
) -> None:
    first = condition_concepts[0]

    with_block = count_for(
        db_client,
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [{"rules_oper": "AND", "rules": [rule(first)]}],
            },
            "demographics": {"age": "40|70", "gender": [int(gender_concept)]},
        },
    )
    with_person_rules = count_for(
        db_client,
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [
                    {"rules_oper": "AND", "rules": [rule(first)]},
                    {
                        "rules_oper": "AND",
                        "rules": [age_rule("40|70"), rule(gender_concept, "Person")],
                    },
                ],
            }
        },
    )

    assert with_block == with_person_rules


@pytest.mark.integration
def test_demographics_only_query_matches_person_rule_cohort(
    db_client: BaseDBClient, gender_concept: str
) -> None:
    """A block with no cohort must equal a cohort of just that Person rule."""
    with_block = count_for(
        db_client, {"demographics": {"gender": [int(gender_concept)]}}
    )
    with_person_rule = count_for(
        db_client,
        {
            "cohort": {
                "groups_oper": "AND",
                "groups": [
                    {"rules_oper": "AND", "rules": [rule(gender_concept, "Person")]}
                ],
            }
        },
    )

    assert with_block == with_person_rule
    assert with_block > 0


@pytest.mark.integration
def test_demographics_exclude_is_the_complement(
    db_client: BaseDBClient, gender_concept: str
) -> None:
    """Including and excluding the same concept must partition the population."""
    included = count_for(db_client, {"demographics": {"gender": [int(gender_concept)]}})
    excluded = count_for(
        db_client,
        {
            "demographics": {
                "gender": {"concepts": [int(gender_concept)], "exclude": True}
            }
        },
    )

    with db_client.engine.connect() as connection:
        total = connection.execute(select(func.count(Person.person_id))).scalar_one()

    assert included + excluded == total
