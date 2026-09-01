from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEMOGRAPHIC_CONCEPT_FIELDS = ("gender", "race", "ethnicity")
"""
Demographic fields that are filtered by a list of OMOP concept ids.

Each maps to a single `*_concept_id` column on the `person` table.
"""


class AgeRange(BaseModel):
    """
    An age range in years, inclusive at both ends.

    Either bound may be omitted for an open-ended range, but at least one must be
    present.
    """

    min: float | None = None
    """
    Lower bound in years, inclusive.
    """

    max: float | None = None
    """
    Upper bound in years, inclusive.
    """

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def accept_pipe_separated(cls, data: Any) -> Any:
        """
        Accept the pipe-separated form the existing `AGE` rule already uses, so
        `"age": "20|30"` and `"age": {"min": 20, "max": 30}` are equivalent.

        Args:
            data: The raw input, either a mapping or a `"min|max"` string.

        Returns:
            The input as a mapping.
        """
        if isinstance(data, str):
            parts = data.split("|")
            if len(parts) != 2:
                raise ValueError(
                    "an age range string must be '<min>|<max>', for example '20|30'"
                )
            lower, upper = parts
            return {
                "min": float(lower) if lower else None,
                "max": float(upper) if upper else None,
            }
        return data

    @model_validator(mode="after")
    def check_bounds(self) -> "AgeRange":
        """
        Check the range has at least one bound and is not inverted.

        Returns:
            AgeRange: The validated range.

        Raises:
            ValueError: If both bounds are missing, or `min` exceeds `max`.
        """
        if self.min is None and self.max is None:
            raise ValueError("an age range must specify at least one of 'min' or 'max'")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("an age range 'min' must be less than or equal to 'max'")
        return self


class ConceptFilter(BaseModel):
    """
    A filter on one `person` concept column, as a list of OMOP concept ids
    combined with `IN`.

    May be written as a bare list (`[8507, 8532]`) or in full
    (`{"concepts": [8507], "exclude": true}`).
    """

    concepts: list[int] = Field(min_length=1)
    """
    Concept ids to match, combined with `IN`.
    """

    exclude: bool = False
    """
    When true, the filter is negated - people *not* matching the concepts.
    """

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def accept_bare_list(cls, data: Any) -> Any:
        """
        Accept a bare list of concept ids as shorthand for an inclusive filter.

        Args:
            data: The raw input, either a mapping or a list of concept ids.

        Returns:
            The input as a mapping.
        """
        if isinstance(data, list):
            return {"concepts": data}
        return data


class Demographics(BaseModel):
    """
    A block of filters applied directly to the `person` table.

    Unlike a `Person` rule inside a group, this block is **always** ANDed with
    the whole cohort, which lets the solver resolve it as a single JOIN against
    `person` rather than as another `person_id` set to INTERSECT. See
    `AvailabilitySolver._construct_final_query`.

    Every field present is combined with AND.
    """

    age: AgeRange | None = None
    """
    Age in years at query time.
    """

    gender: ConceptFilter | None = None
    """
    Concept ids matched against `person.gender_concept_id`.
    """

    race: ConceptFilter | None = None
    """
    Concept ids matched against `person.race_concept_id`.
    """

    ethnicity: ConceptFilter | None = None
    """
    Concept ids matched against `person.ethnicity_concept_id`.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    @model_validator(mode="after")
    def check_not_empty(self) -> "Demographics":
        """
        Check the block filters on something.

        An empty block would silently widen the query to the whole population,
        which is exactly the mistake this block exists to avoid.

        Returns:
            Demographics: The validated block.

        Raises:
            ValueError: If no filter is set.
        """
        if not self.is_populated():
            raise ValueError(
                "a demographics block must set at least one of "
                "'age', 'gender', 'race' or 'ethnicity'"
            )
        return self

    def is_populated(self) -> bool:
        """
        Whether any filter is set on this block.

        Returns:
            bool: True if at least one field is set.
        """
        return any(
            getattr(self, field) is not None
            for field in ("age", *DEMOGRAPHIC_CONCEPT_FIELDS)
        )
