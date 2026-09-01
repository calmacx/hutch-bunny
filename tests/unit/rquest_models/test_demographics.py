"""Tests for the top-level demographics block."""

import pytest
from pydantic import ValidationError

from hutch_bunny.core.rquest_models.availability import AvailabilityQuery
from hutch_bunny.core.rquest_models.demographics import (
    AgeRange,
    ConceptFilter,
    Demographics,
)


@pytest.mark.unit
def test_concept_filter_accepts_a_bare_list() -> None:
    """`"gender": [8532]` is shorthand for an inclusive filter."""
    assert ConceptFilter.model_validate([8532]) == ConceptFilter(
        concepts=[8532], exclude=False
    )


@pytest.mark.unit
def test_concept_filter_accepts_the_full_form() -> None:
    concept_filter = ConceptFilter.model_validate(
        {"concepts": [8527, 8515], "exclude": True}
    )

    assert concept_filter.concepts == [8527, 8515]
    assert concept_filter.exclude is True


@pytest.mark.unit
def test_concept_filter_rejects_an_empty_concept_list() -> None:
    with pytest.raises(ValidationError):
        ConceptFilter.model_validate([])


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload,expected_min,expected_max",
    [
        ({"min": 40, "max": 70}, 40.0, 70.0),
        ("40|70", 40.0, 70.0),
        ("|70", None, 70.0),
        ("40|", 40.0, None),
        ({"min": 40}, 40.0, None),
    ],
)
def test_age_range_accepts_object_and_pipe_forms(
    payload: object, expected_min: float | None, expected_max: float | None
) -> None:
    """The pipe form matches the syntax the existing `AGE` rule already uses."""
    age = AgeRange.model_validate(payload)

    assert age.min == expected_min
    assert age.max == expected_max


@pytest.mark.unit
@pytest.mark.parametrize("payload", [{}, {"min": None, "max": None}, "|"])
def test_age_range_requires_a_bound(payload: object) -> None:
    with pytest.raises(ValidationError, match="at least one of 'min' or 'max'"):
        AgeRange.model_validate(payload)


@pytest.mark.unit
def test_age_range_rejects_inverted_bounds() -> None:
    with pytest.raises(ValidationError, match="less than or equal to"):
        AgeRange.model_validate({"min": 70, "max": 40})


@pytest.mark.unit
def test_age_range_rejects_a_malformed_string() -> None:
    with pytest.raises(ValidationError, match="'<min>\\|<max>'"):
        AgeRange.model_validate("40")


@pytest.mark.unit
def test_demographics_combines_every_field() -> None:
    demographics = Demographics.model_validate(
        {
            "age": {"min": 40, "max": 70},
            "gender": [8532],
            "race": {"concepts": [8527], "exclude": True},
            "ethnicity": [38003564],
        }
    )

    assert demographics.age is not None and demographics.age.max == 70.0
    assert demographics.gender is not None and demographics.gender.concepts == [8532]
    assert demographics.race is not None and demographics.race.exclude is True
    assert demographics.ethnicity is not None


@pytest.mark.unit
def test_empty_demographics_block_is_rejected() -> None:
    """An empty block would silently widen the query to everybody."""
    with pytest.raises(ValidationError, match="at least one of"):
        Demographics.model_validate({})


@pytest.mark.unit
def test_query_may_carry_demographics_without_a_cohort() -> None:
    query = AvailabilityQuery.model_validate(
        {
            "uuid": "unique_id",
            "owner": "user1",
            "collection": "collection_id",
            "protocol_version": "v2",
            "char_salt": "salt",
            "demographics": {"age": "40|70"},
        }
    )

    assert query.cohort is None
    assert query.demographics is not None
