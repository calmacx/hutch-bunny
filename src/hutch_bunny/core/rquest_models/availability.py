from typing import Any
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from hutch_bunny.core.rquest_models.cohort import Cohort
from hutch_bunny.core.rquest_models.demographics import Demographics


class AvailabilityQuery(BaseModel):
    """
    The top-level structure of an Availability Query request.

    Enables the user to query the availability of a cohort in a collection.
    """

    cohort: Cohort | None = None
    """
    Cohort of the query, which contains the query groups and their rules.

    Optional, so a query may filter on `demographics` alone.
    """

    demographics: Demographics | None = None
    """
    Optional block of filters applied directly to the `person` table.

    Always ANDed with the cohort. Because it is guaranteed to be a conjunction,
    the solver resolves it as a single JOIN against `person` instead of another
    `person_id` set to INTERSECT - see `AvailabilitySolver._construct_final_query`.
    """

    uuid: str
    """
    Unique identifier of the query.
    """

    owner: str
    """
    Owner of the query. Not the user itself, but the ID of the connection - default is `user1`.
    """

    collection: str
    """
    Collection of the query. This is the unqiue collection that the query is being run on.
    """

    protocol_version: str
    """
    Protocol version of the query, for example `v2`.
    """

    char_salt: str
    """
    Char salt of the query used for hashing.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    @field_validator("cohort", mode="before")
    @classmethod
    def validate_cohort(cls, v: Any) -> Any:
        """Validate and convert the cohort dictionary to a Cohort object.
        This ensures proper nested validation of the entire query structure.

        Args:
            v: The cohort dictionary to validate, containing groups and their rules

        Returns:
            The validated Cohort object with all nested structures validated
        """
        if isinstance(v, dict):
            return Cohort.model_validate(v)
        return v

    @model_validator(mode="after")
    def check_has_a_query_body(self) -> "AvailabilityQuery":
        """
        Check the query actually constrains something.

        A query with neither groups nor demographics would count the whole
        population, so it is rejected rather than answered.

        Returns:
            AvailabilityQuery: The validated query.

        Raises:
            ValueError: If the query has no groups and no demographics block.
        """
        has_groups = self.cohort is not None and bool(self.cohort.groups)
        if not has_groups and self.demographics is None:
            raise ValueError(
                "an availability query must contain at least one group "
                "or a 'demographics' block"
            )
        return self
