"""Tests for nested groups on the availability query model."""

import pytest
from pydantic import ValidationError

from hutch_bunny.core.rquest_models.availability import AvailabilityQuery
from hutch_bunny.core.rquest_models.group import MAX_GROUP_DEPTH, Group


def _rule(value: str = "111", varcat: str = "Condition") -> dict[str, str]:
    return {
        "varname": "OMOP",
        "varcat": varcat,
        "type": "TEXT",
        "oper": "=",
        "value": value,
    }


def _envelope(**extra: object) -> dict[str, object]:
    return {
        "uuid": "unique_id",
        "owner": "user1",
        "collection": "collection_id",
        "protocol_version": "v2",
        "char_salt": "salt",
        **extra,
    }


@pytest.mark.unit
def test_group_without_nested_groups_is_unchanged() -> None:
    """A payload predating nesting must behave exactly as before."""
    group = Group.model_validate({"rules": [_rule()], "rules_oper": "AND"})

    assert group.groups == []
    assert group.depth() == 1
    assert len(group.rules) == 1


@pytest.mark.unit
def test_group_nests_to_arbitrary_depth() -> None:
    group = Group.model_validate(
        {
            "rules": [_rule("111")],
            "rules_oper": "AND",
            "groups": [
                {
                    "rules": [_rule("222", "Drug")],
                    "rules_oper": "OR",
                    "groups": [
                        {"rules": [_rule("333", "Observation")], "rules_oper": "AND"}
                    ],
                }
            ],
        }
    )

    assert group.depth() == 3
    assert [rule.value for rule in group.all_rules()] == ["111", "222", "333"]


@pytest.mark.unit
def test_group_may_contain_only_nested_groups() -> None:
    """A pure container group carries no rules of its own."""
    group = Group.model_validate(
        {"rules_oper": "OR", "groups": [{"rules": [_rule()], "rules_oper": "AND"}]}
    )

    assert group.rules == []
    assert group.depth() == 2


@pytest.mark.unit
def test_empty_group_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least one rule or nested group"):
        Group.model_validate({"rules": [], "rules_oper": "AND"})


@pytest.mark.unit
def test_nesting_beyond_the_maximum_depth_is_rejected() -> None:
    payload: dict[str, object] = {"rules": [_rule()], "rules_oper": "AND"}
    for _ in range(MAX_GROUP_DEPTH):
        payload = {"rules_oper": "AND", "groups": [payload]}

    with pytest.raises(ValidationError, match="exceeds the maximum"):
        Group.model_validate(payload)


@pytest.mark.unit
def test_nesting_at_the_maximum_depth_is_accepted() -> None:
    payload: dict[str, object] = {"rules": [_rule()], "rules_oper": "AND"}
    for _ in range(MAX_GROUP_DEPTH - 1):
        payload = {"rules_oper": "AND", "groups": [payload]}

    assert Group.model_validate(payload).depth() == MAX_GROUP_DEPTH


@pytest.mark.unit
def test_query_with_no_groups_and_no_demographics_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least one group"):
        AvailabilityQuery.model_validate(
            _envelope(cohort={"groups": [], "groups_oper": "AND"})
        )
