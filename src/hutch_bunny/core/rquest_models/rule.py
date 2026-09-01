import re
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hutch_bunny.core.omop import Varcat


class Rule(BaseModel):
    """
    A rule in a group of rules.

    Specifies the search criteria for a rule.
    """

    varname: str = ""
    """
    Variable name to search for.

    Either:
    - `OMOP`: For OMOP searches
    - `AGE`: For AGE searches
    - `OMOP=21490742`: For Measurement searches
    """

    varcat: Varcat
    """
    Table to search in.
    """

    type_: Literal["NUM", "TEXT", "GEO_RADIUS"] = Field(default="TEXT", alias="type")
    """
    Type of value to search for.

    - `TEXT`: For OMOP concept_id searches
    - `NUM`: For AGE or Measurement searches

    RQUEST supports `ALT`, `SET`, `BOOLEAN` also - but Bunny does not.
    """

    operator: Literal["=", "!="] = Field(default="=", alias="oper")
    """
    Operator to use in the search

    = for inclusion

    != for exclusion
    """

    value: str = ""
    """
    Value to search for.

    TEXT searches have a OMOP concept_id (for example `8507`)

    NUM searches have a range value split by `|` (for example 1.0|3.0)

    TEXT searches may also send a **list** of concept_ids (for example
    `["8507", "8532"]`), which is resolved with a single `IN (...)` per table
    rather than one query per concept. When a list is sent, this field holds the
    first element for backwards compatibility and `values` holds them all.
    """

    values: list[str] = Field(default_factory=list)
    """
    All values to search for, derived from `value`.

    Always populated: a single-valued rule yields a one-element list, and an
    empty `value` (such as the "any death record" sentinel) yields an empty one.
    Prefer this over `value` when building queries.
    """

    time: str | None = None
    """
    Time to search for.

    A time is a number followed by a colon and a unit.

    If the `|` is on the left of the value it was less than or equal the number.

    If the `|` is on the right of the value it was greater than or equal the number.

    For example:
    - 10|:AGE:Y (greater than or equal to 10 years)
    - 10|:TIME:M (greater than or equal to 10 months)
    - |10:TIME:M (less than or equal to 10 months)
    - |10:AGE:Y (less than or equal to 10 years)
    """

    secondary_modifier: list[int] | None = None
    """
    Secondary modifier to use in the search.

    This is used to on the provenance of the data on `ConditionOccurence`.

    A list of concept_ids, for example `[32020]`.
    """

    """
    Used to store parsed numeric values from range strings.
    """
    raw_range: str | None = None
    min_value: float | None = None
    max_value: float | None = None

    # Parsed geo-radius values (populated when type_ == "GEO_RADIUS")
    center_lat: float | None = None
    center_lon: float | None = None
    geo_radius_meters: float | None = None

    # Parsed time values
    time_value: str | None = None
    time_category: str | None = None
    time_unit: str | None = None
    greater_than_value: str | None = None
    less_than_value: str | None = None

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _normalise_value_list(cls, data: Any) -> Any:
        """
        Accept a list of concept ids in `value` and split it across `value` and
        `values`.

        `value` is typed `str` and is read by the NUM / GEO_RADIUS parsing below,
        so a list is unpacked here rather than widening the field: `value` keeps
        the first element (what single-concept callers and older code expect) and
        `values` carries the full list.

        Args:
            data: The raw input to the model, usually the decoded rule dictionary.

        Returns:
            The input, with a list `value` split into `value` + `values`.
        """
        if isinstance(data, dict) and isinstance(data.get("value"), list):
            raw = [str(item) for item in data["value"]]
            return {**data, "value": raw[0] if raw else "", "values": raw}
        return data

    def model_post_init(self, __context: Any) -> None:
        """
        Initialize numeric values and parse time values after model creation

        Args:
            __context: The context of the model creation.

        Returns:
            None
        """
        # Parse geo-radius values for GEO_RADIUS type rules
        if self.type_ == "GEO_RADIUS":
            raw = self.value
            self.value = ""  # clear so concept lookups don't try int(value)
            parts = raw.split("|")
            if len(parts) == 3:
                try:
                    self.center_lat = float(parts[0])
                    self.center_lon = float(parts[1])
                    self.geo_radius_meters = float(parts[2])
                except ValueError:
                    pass

        # Parse numeric values for NUM type rules
        elif self.type_ == "NUM":
            # For NUM type rules, the value might be in range format (1.0..3.0) 
            # or pipe-separated format (1.0|3.0)
            if ".." in self.value:
                self.min_value, self.max_value = self._parse_numeric(self.value)
            else:
                # Handle pipe-separated format (1.0|3.0)
                self.min_value, self.max_value = self._parse_pipe_separated(self.value)
            
            parts = self.varname.split("=")
            v = parts[1] if len(parts) > 1 else None
            self.raw_range = self.value
            self.value = v or ""
        else:
            # For non-NUM rules, parse range from raw_range if provided
            if self.raw_range and self.raw_range != "":
                self.min_value, self.max_value = self._parse_pipe_separated(self.raw_range)
            else:
                self.min_value, self.max_value = None, None

        # Parse time values if time is provided
        if self.time:
            self._parse_time()

        # Settle `values` last: NUM and GEO_RADIUS rewrite `value` above, so a
        # multi-concept list is only meaningful for TEXT rules. Everything else
        # collapses to the (possibly rewritten, possibly empty) single value.
        if self.type_ == "TEXT" and self.values:
            self.values = [value for value in self.values if value != ""]
        else:
            self.values = [self.value] if self.value else []

    @staticmethod
    def _parse_numeric(value: str) -> tuple[float | None, float | None]:
        """
        Parse numeric values from range strings.

        Args:
            value (str): The value to parse.

        Returns:
            tuple[float | None, float | None]: The parsed numeric values.
        """
        pattern = re.compile(r"(-?\d*\.\d+|\d+|null)\.\.(-?\d*\.\d+|null)")
        if match := re.search(pattern, value):
            lower, upper = match.groups()
            try:
                min_value = float(lower)
            except ValueError:
                min_value = None
            try:
                max_value = float(upper)
            except ValueError:
                max_value = None
            return min_value, max_value
        return None, None

    @staticmethod
    def _parse_pipe_separated(value: str) -> tuple[float | None, float | None]:
        """
        Parse pipe-separated numeric values (e.g., "1.0|3.0").

        Args:
            value (str): The value to parse.

        Returns:
            tuple[float | None, float | None]: The parsed numeric values.
        """
        try:
            min_str, max_str = value.split("|")
            min_value = float(min_str) if min_str else None
            max_value = float(max_str) if max_str else None
            return min_value, max_value
        except (ValueError, AttributeError):
            return None, None

    def _parse_time(self) -> None:
        """
        Parse time string into components.
        
        Time format: "value|:CATEGORY:UNIT" or "|value:CATEGORY:UNIT"
        Examples:
        - "10|:AGE:Y" (greater than or equal to 10 years)
        - "|10:TIME:M" (less than or equal to 10 months)
        """
        if not self.time:
            return
            
        try:
            time_value, time_category, time_unit = self.time.split(":")
            self.time_value = time_value
            self.time_category = time_category
            self.time_unit = time_unit
            
            # Parse left and right values from time_value
            if "|" in time_value:
                left_value, right_value = time_value.split("|")
                self.greater_than_value = left_value if left_value else ""
                self.less_than_value = right_value if right_value else ""
            else:
                self.greater_than_value = time_value
                self.less_than_value = ""
        except (ValueError, AttributeError):
            # If parsing fails, set all values to None
            self.time_value = None
            self.time_category = None
            self.time_unit = None
            self.greater_than_value = None
            self.less_than_value = None
