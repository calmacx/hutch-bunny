from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from hutch_bunny.core.rquest_models.rule import Rule

MAX_GROUP_DEPTH = 20
"""
Maximum nesting depth of a group tree.

Groups nest to arbitrary depth, but a payload arrives from upstream and is not
necessarily trusted, so the depth is bounded. Twenty is far beyond anything a
cohort-builder UI produces; the cap exists so a pathological payload fails
validation with a clear message instead of exhausting the stack deep inside the
solver.
"""


class Group(BaseModel):
    """
    A group of rules, and optionally nested groups, with an operator to combine
    them.

    A group holds `rules` (leaf search criteria), `groups` (nested subgroups), or
    both. `rules_operator` combines **all** of a group's children — its rules and
    its nested groups alike — so there is exactly one operator per level, as
    there has always been.

    Nesting is optional and additive: a payload with no `groups` key behaves
    exactly as it did before nesting was supported.
    """

    rules: list[Rule] = Field(default_factory=list)
    """
    Rules of the group.
    """

    groups: list["Group"] = Field(default_factory=list)
    """
    Nested subgroups of the group.

    Each is combined with this group's `rules` using `rules_operator`.
    """

    rules_operator: Literal["AND", "OR"] = Field(alias="rules_oper")
    """
    Operator to combine the rules and nested groups.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    @field_validator("rules", mode="before")
    @classmethod
    def validate_rules(cls, v: list[dict[str, str | float | bool]]) -> list[Rule]:
        """
        Validate and convert the list of rule dictionaries to `Rule` objects.

        Args:
            v (list[dict]): List of rule dictionaries to validate.

        Returns:
            list[Rule]: List of validated `Rule` objects.
        """
        if isinstance(v, list):
            return [Rule.model_validate(r) for r in v]
        return v

    @field_validator("groups", mode="before")
    @classmethod
    def validate_groups(cls, v: list[Any]) -> list["Group"]:
        """
        Validate and convert the list of nested group dictionaries to `Group`
        objects, recursing through the whole tree.

        Args:
            v (list[dict]): List of group dictionaries to validate.

        Returns:
            list[Group]: List of validated `Group` objects.
        """
        if isinstance(v, list):
            return [cls.model_validate(g) for g in v]
        return v

    @model_validator(mode="after")
    def validate_group_tree(self) -> "Group":
        """
        Check the group carries something to search for, and that the tree below
        it is not nested beyond `MAX_GROUP_DEPTH`.

        Returns:
            Group: The validated group.

        Raises:
            ValueError: If the group is empty or nested too deeply.
        """
        if not self.rules and not self.groups:
            raise ValueError("a group must contain at least one rule or nested group")

        depth = self.depth()
        if depth > MAX_GROUP_DEPTH:
            raise ValueError(
                f"group nesting depth {depth} exceeds the maximum of {MAX_GROUP_DEPTH}"
            )
        return self

    def depth(self) -> int:
        """
        Depth of this group's tree, counting itself as one.

        Returns:
            int: 1 for a group with no nested groups, otherwise 1 plus the depth
                of its deepest child.
        """
        return 1 + max((group.depth() for group in self.groups), default=0)

    def all_rules(self) -> list[Rule]:
        """
        Every rule in this group and, recursively, in its nested groups.

        Returns:
            list[Rule]: The flattened rules, in tree order.
        """
        rules = list(self.rules)
        for group in self.groups:
            rules.extend(group.all_rules())
        return rules


Group.model_rebuild()
