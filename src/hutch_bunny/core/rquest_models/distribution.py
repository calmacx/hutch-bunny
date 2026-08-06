from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, field_validator, model_validator


class LocationScanType(str, Enum):
    """Fields that a `LOCATION` distribution query can group/count over."""

    SOURCE_VALUE = "SOURCE_VALUE"
    CONCEPT_CODE = "CONCEPT_CODE"
    LAT_LONG = "LAT_LONG"


class DistributionQueryType(str, Enum):
    """Types of distribution queries."""

    DEMOGRAPHICS = "DEMOGRAPHICS"
    GENERIC = "GENERIC"
    ICD_MAIN = "ICD-MAIN"
    LOCATION = "LOCATION"

    @property
    def file_name(self) -> Literal["demographics.distribution", "code.distribution", "location.distribution"]:
        """Get the corresponding file name for this distribution type."""
        mapping = {
            DistributionQueryType.DEMOGRAPHICS: "demographics.distribution",
            DistributionQueryType.GENERIC: "code.distribution",
            DistributionQueryType.LOCATION: "location.distribution",
        }
        if self not in mapping:
            raise ValueError(f"No file name mapping for query type: {self}")
        return mapping[self]  # type: ignore


class DistributionQuery(BaseModel):
    """
    The top-level structure of a distribution query request.
    """

    owner: str
    """
    Owner of the query. Not the user itself, but the ID of the connection - default is `user1`.
    """

    code: DistributionQueryType
    """
    Code of the query. This is the type of distribution query to run.
    """

    analysis: Literal["DISTRIBUTION"]
    """
    Analysis of the query. Currently only `DISTRIBUTION` is supported.
    """

    uuid: str
    """
    Unique identifier of the query.
    """

    collection: str
    """
    Collection of the query. This is the unique collection that the query is being run on.
    """

    location_scan_type: Optional[LocationScanType] = None
    """
    Field to group/count over for `LOCATION` distribution queries. Required when
    `code` is `LOCATION`; one of `SOURCE_VALUE`, `CONCEPT_CODE`, or `LAT_LONG`.
    """

    @field_validator("code", mode="before")
    @classmethod
    def validate_code(cls, v: str) -> DistributionQueryType:
        """Validate that the code is a valid distribution query type.

        Args:
            v (str): The code value to validate

        Raises:
            ValueError: If the code is not a valid distribution query type

        Returns:
            DistributionQueryType: The validated enum value
        """
        try:
            return DistributionQueryType(v)
        except ValueError:
            valid_values = [t.value for t in DistributionQueryType]
            raise ValueError(
                f"'{v}' is not a valid distribution query type. Valid values are: {', '.join(repr(v) for v in valid_values)}"
            )

    @field_validator("location_scan_type", mode="before")
    @classmethod
    def validate_location_scan_type(cls, v: str | None) -> LocationScanType | None:
        """Validate that the location scan type is a valid option.

        Args:
            v (str | None): The location scan type value to validate

        Raises:
            ValueError: If the value is not a valid location scan type

        Returns:
            LocationScanType | None: The validated enum value
        """
        if v is None:
            return None
        try:
            return LocationScanType(v)
        except ValueError:
            valid_values = [t.value for t in LocationScanType]
            raise ValueError(
                f"'{v}' is not a valid location scan type. Valid values are: {', '.join(repr(v) for v in valid_values)}"
            )

    @model_validator(mode="after")
    def validate_location_scan_type_required(self) -> "DistributionQuery":
        """Require `location_scan_type` whenever `code` is `LOCATION`.

        Raises:
            ValueError: If `code` is `LOCATION` and `location_scan_type` is not set
        """
        if self.code == DistributionQueryType.LOCATION and self.location_scan_type is None:
            valid_values = [t.value for t in LocationScanType]
            raise ValueError(
                f"'location_scan_type' is required when code is 'LOCATION'. Valid values are: {', '.join(repr(v) for v in valid_values)}"
            )
        return self
