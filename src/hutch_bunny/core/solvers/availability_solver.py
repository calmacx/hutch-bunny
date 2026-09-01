from logging import DEBUG
from typing import Any, TypedDict, Union, Literal
from sqlalchemy import (
    CompoundSelect,
    func,
    ColumnElement,
    select,
    Select,
    intersect,
    union,
    literal, 
    or_,
    distinct
)
from hutch_bunny.core.db import BaseDBClient
from hutch_bunny.core.db.entities import (
    Concept,
    Person
)
from tenacity import (
    retry,
    stop_after_attempt,
    wait_fixed,
    before_sleep_log,
    after_log,
)
from typing import Tuple 

from hutch_bunny.core.obfuscation import apply_filters
from hutch_bunny.core.rquest_models.group import Group
from hutch_bunny.core.rquest_models.availability import AvailabilityQuery
from hutch_bunny.core.logger import logger
from hutch_bunny.core.rquest_models.rule import Rule
from hutch_bunny.core.omop import Varcat
from hutch_bunny.core.solvers.rule_query_builders import (
    DemographicsConstraintBuilder,
    OMOPRuleQueryBuilder,
    PersonConstraintBuilder,
)
from hutch_bunny.core.db.utils import log_query
from hutch_bunny.core.settings import Settings


settings = Settings()


class ResultModifier(TypedDict):
    id: str
    threshold: int | None
    nearest: int | None

Key = Literal["threshold", "nearest"]


class RuleTableQuery(TypedDict):
    union_query: CompoundSelect
    inclusion: bool


class AvailabilitySolver():

    def __init__(self, db_client: BaseDBClient, query: AvailabilityQuery) -> None:
        self.db_client = db_client
        self.query = query
        self.person_constraint_builder = PersonConstraintBuilder(db_client)
        self.demographics_constraint_builder = DemographicsConstraintBuilder(db_client)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(60),
        before_sleep=before_sleep_log(logger, DEBUG),
        after=after_log(logger, DEBUG)
    )
    def solve_query(self, results_modifiers: list[ResultModifier]) -> int:
        """
        Solve the availability query by:
        1. Finding concepts and extracting modifiers
        2. Building queries for each group
        3. Combining groups with AND/OR logic
        4. Executing the final query and applying filters
        """
        groups = self.query.cohort.groups if self.query.cohort is not None else []
        concepts = self._find_concepts(groups)
        low_number = self._extract_modifier(results_modifiers, "Low Number Suppression", "threshold", 10)
        rounding = self._extract_modifier(results_modifiers, "Rounding", "nearest", 10)

        count = 0

        with self.db_client.engine.connect() as con:
            group_queries = []

            for group in groups:
                group_query = self._build_group_query(group, concepts)
                group_queries.append(group_query)

            final_query = self._construct_final_query(
                group_queries,
                rounding, 
                low_number
            )

            try:
                output = con.execute(final_query).fetchone()
                count = int(output[0]) if output is not None else 0
            except Exception as e:
                logger.error(str(e))

        return apply_filters(count, results_modifiers)

    def _find_concepts(self, groups: list[Group]) -> dict[str, str]:
        """Function that takes all the concept IDs in the cohort definition, looks them up in the OMOP database
        to extract the concept_id and domain and place this within a dictionary for lookup during other query building

        Although the query payload will tell you where the OMOP concept is from (based on the RQUEST OMOP version, this is
        a safer method as we know concepts can move between tables based on a vocab.

        Therefore, this helps to account for a difference between the Bunny vocab version and the RQUEST OMOP version.

        """
        concept_ids = set()
        for group in groups:
            # all_rules() walks nested groups, so concepts are found at any depth
            for rule in group.all_rules():
                # Guard for rules that carry no concept (e.g. Age, geo-radius)
                for value in rule.values:
                    concept_ids.add(int(value))

        concept_query = (
            # order must be .concept_id, .domain_id
            select(Concept.concept_id, Concept.domain_id)
            .where(Concept.concept_id.in_(concept_ids))
            .distinct()
        )
        with self.db_client.engine.connect() as con:
            result = con.execute(concept_query)
            concept_dict = {
                str(concept_id): domain_id for concept_id, domain_id in result
            }
        return concept_dict

    def _extract_modifier(
        self,
        results_modifiers: list[ResultModifier],
        result_id: str,
        key: Key,
        default_value: int = 10,
    ) -> int:
        for item in results_modifiers:
            if item["id"] == result_id:
                value = item.get(key)  # type: int | None
                return value if value is not None else default_value
        return default_value

    def _build_group_query(
        self,
        group: Group,
        concepts: dict[str, str]
    ) -> Union[Select[Tuple[int]], CompoundSelect]:
        """
        Build query for a single group - a nested SQL expression.

        Args:
            group: The group that contains the rules to be assembled
            concepts: a dictionary that maps the concepts IDs to the domains they belong

        Returns:
            Either a single Select statement or multiple joined by UNION or INTERSECT
            depending on the logic specified for the group
        """

        rule_table_queries: list[RuleTableQuery] = []
        person_constraints: list[ColumnElement[bool]] = []

        for rule in group.rules:
            inclusion_criteria = rule.operator == "="
            if rule.varcat == Varcat.PERSON:
                constraints = self.person_constraint_builder.build_constraints(rule, concepts)
                person_constraints.extend(constraints)
            else:
                rule_union = self._build_rule_query(rule)
                rule_table_queries.append({
                    'union_query': rule_union,
                    'inclusion': inclusion_criteria
                })

        # A nested group resolves to a person_id set exactly like a rule does, so
        # it joins its parent's children and is combined by the same operator.
        nested_group_queries = [
            self._build_group_query(nested_group, concepts)
            for nested_group in group.groups
        ]

        return self._construct_group_query(
            group, person_constraints, rule_table_queries, nested_group_queries
        )

    def _build_rule_query(self, rule: Rule) -> CompoundSelect:
        """Build query for a single non-Person rule."""
        builder = OMOPRuleQueryBuilder(
            self.db_client,
            include_specimen=settings.OMOP_SPECIMEN_ENABLED,
            include_location=settings.OMOP_LOCATION_ENABLED,
            include_death=settings.OMOP_DEATH_ENABLED,
            varcat=rule.varcat,
        )

        if rule.values:
            builder.add_concept_constraints([int(value) for value in rule.values])

        valid_time_constraint = rule.greater_than_value or rule.less_than_value

        if valid_time_constraint and rule.time_category == "AGE":
            builder.add_age_constraint(
                greater_than_value=rule.greater_than_value,
                less_than_value=rule.less_than_value
            )
        elif valid_time_constraint and rule.time_category == "TIME":
            builder.add_temporal_constraint(
                greater_than_time=rule.greater_than_value or "",
                less_than_time=rule.less_than_value or ""
            )

        if rule.min_value is not None and rule.max_value is not None:
            builder.add_numeric_range(rule.min_value, rule.max_value)

        if rule.secondary_modifier:
            builder.add_secondary_modifiers(rule.secondary_modifier)

        if (
            rule.center_lat is not None
            and rule.center_lon is not None
            and rule.geo_radius_meters is not None
        ):
            builder.add_haversine_radius_constraint(
                rule.center_lat, rule.center_lon, rule.geo_radius_meters
            )

        return builder.build()

    def _construct_group_query(
        self,
        current_group: Group,
        person_constraints_for_group: list[ColumnElement[bool]],
        rule_table_queries: list[RuleTableQuery],
        nested_group_queries: list[Union[Select[Tuple[int]], CompoundSelect[Tuple[int]]]] | None = None
    ) -> Union[Select[Tuple[int]], CompoundSelect]:
        """
        Construct the query for a single group by processing inclusion/exclusion rules.

        Args:
            current_group: The group to construct a query for
            person_constraints_for_group: Person-level constraints for this group
            rule_table_queries: List of rule table queries for this group
            nested_group_queries: Already-built queries for this group's nested
                subgroups. Combined with the group's own rules using the same
                `rules_operator`.

        Returns:
            The constructed group query
        """
        # Build the group query using UNION approach
        inclusion_queries: list[Union[Select[Tuple[int]], CompoundSelect]] = []
        exclusion_queries: list[Union[Select[Tuple[int]], CompoundSelect]] = []

        # Add person constraints as a separate query
        if person_constraints_for_group:
            if current_group.rules_operator == "OR" and len(person_constraints_for_group) > 1:
                # For OR logic, combine Person constraints with OR
                person_query = select(Person.person_id).where(or_(*person_constraints_for_group))
            else:
                # For AND logic or single constraint, use AND (default)
                person_query = select(Person.person_id).where(*person_constraints_for_group)
            inclusion_queries.append(person_query)

        # Nested subgroups are inclusion sets in their own right
        if nested_group_queries:
            inclusion_queries.extend(nested_group_queries)

        # Add table queries for each rule
        if rule_table_queries:
            logger.debug(f"Processing {len(rule_table_queries)} rule table queries")
            for i, rule_data in enumerate(rule_table_queries):
                union_query = rule_data['union_query']
                inclusion = rule_data['inclusion']
                logger.debug(f"Rule {i}: inclusion={inclusion}")

                if inclusion:
                    # For inclusion: add the union directly
                    inclusion_queries.append(union_query)
                    logger.debug(f"Added inclusion query for rule {i}")
                else:
                    # For exclusion: store the union query to exclude people who match
                    exclusion_queries.append(union_query)
                    logger.debug(f"Added exclusion query for rule {i}")
        else:
            logger.debug("No rule table queries found")

        # Create the final group query (without CTEs at this level)
        if inclusion_queries:
            if current_group.rules_operator == "AND":
                # For AND logic, use INTERSECT which is more efficient than joins
                group_query: Union[Select[Tuple[int]], CompoundSelect] = inclusion_queries[0]
                for query in inclusion_queries[1:]:
                    group_query = intersect(group_query, query)
            else:
                # For OR logic, use UNION
                group_query = union(*inclusion_queries)
        else:
            # Start with all people if no inclusion queries
            group_query = select(Person.person_id)

        # Handle exclusion queries - remove people who match exclusion criteria
        if exclusion_queries:
            logger.debug(f"Processing {len(exclusion_queries)} exclusion queries")
            try:
                # Union all exclusion queries
                exclusion_union = union(*exclusion_queries)
                logger.debug("Exclusion union created successfully")

                # Exclude people who match any exclusion criteria
                exclusion_query = select(Person.person_id).where(
                    ~Person.person_id.in_(select(exclusion_union.subquery()))
                )
                group_query = intersect(group_query, exclusion_query)

                logger.debug("Exclusion queries processed successfully")
            except Exception as e:
                logger.error(f"Error processing exclusion queries: {e}")
                raise

        return group_query

    def _build_demographic_constraints(self) -> list[ColumnElement[bool]]:
        """
        Build the WHERE constraints for the top-level `demographics` block.

        Returns:
            The constraints, or an empty list when the query has no demographics
            block.
        """
        if self.query.demographics is None:
            return []
        return self.demographics_constraint_builder.build_constraints(
            self.query.demographics
        )

    def _combine_group_queries(
        self,
        all_groups_queries: list[Union[Select[Tuple[int]], CompoundSelect[Tuple[int]]]]
    ) -> Union[Select[Tuple[int]], CompoundSelect[Tuple[int]]] | None:
        """
        Combine each group's query into a single statement yielding person_ids.

        Args:
            all_groups_queries: List of queries, one per top-level group.

        Returns:
            A UNION (for OR) or INTERSECT (for AND) over the group CTEs, or None
            when the query has no groups at all.
        """
        if not all_groups_queries:
            return None

        group_ctes = [
            query.cte(name=f"final_group_{index}")
            for index, query in enumerate(all_groups_queries)
        ]
        group_selects = [select(cte) for cte in group_ctes]

        groups_operator = (
            self.query.cohort.groups_operator if self.query.cohort is not None else "AND"
        )
        if groups_operator == "OR":
            return union(*group_selects)
        return intersect(*group_selects)

    @staticmethod
    def _apply_obfuscation(
        query: Select[Tuple[int]],
        count_expression: ColumnElement[Any],
        low_number: int
    ) -> Select[Tuple[int]]:
        """
        Apply low-number suppression to a count query.

        A suppressed count returns no row at all, which the caller reports as 0.

        Args:
            query: The count query to constrain.
            count_expression: The raw (un-rounded) count being suppressed.
            low_number: The suppression threshold; 0 disables suppression.

        Returns:
            The query, with a HAVING clause when suppression is enabled.
        """
        if low_number > 0:
            return query.having(count_expression >= low_number)
        return query

    @staticmethod
    def _round_count(
        count_expression: ColumnElement[Any], rounding: int
    ) -> ColumnElement[Any]:
        """
        Wrap a count in SQL-side rounding.

        Args:
            count_expression: The raw count.
            rounding: Round to the nearest this; 0 disables rounding.

        Returns:
            The rounded count expression, or the raw count when disabled.
        """
        if rounding > 0:
            return func.round((count_expression / rounding), 0) * rounding
        return count_expression

    def _construct_final_query(
        self,
        all_groups_queries: list[Union[Select[Tuple[int]], CompoundSelect]],
        rounding: int,
        low_number: int
    ) -> Select[Tuple[int]]:
        """
        Construct the final counting query.

        Groups are combined with OR/AND logic using CTEs, exactly as before. What
        changes when the query carries a top-level `demographics` block is the
        final step:

        - **Without demographics** the cohort set is counted directly, unchanged.
        - **With demographics** the cohort set is JOINed to `person` and the
          demographic filters are applied as WHERE clauses on that join.

        The JOIN matters. A `Person` rule inside a group becomes another
        `person_id` set that has to be INTERSECTed with the cohort set, and on a
        large collection that means sorting two multi-million-row sets and
        spilling to disk - it dominated the runtime in the query-optimisation
        study. Because a `demographics` block is guaranteed to be a conjunction,
        the same filter can instead ride along on a join to `person` and be
        counted once.

        Args:
            all_groups_queries: List of queries for each group
            rounding: Rounding factor for the final count
            low_number: Low number suppression threshold

        Returns:
            The final query that counts the results with appropriate rounding
        """
        demographic_constraints = self._build_demographic_constraints()
        combined_groups = self._combine_group_queries(all_groups_queries)

        if combined_groups is None:
            if not demographic_constraints:
                # Nothing to count. Model validation normally prevents this.
                full_query_all_groups = select(func.count()).where(literal(False))
            else:
                # Demographics-only query: `person` is the whole cohort.
                count_expression = func.count(distinct(Person.person_id))
                full_query_all_groups = (
                    select(self._round_count(count_expression, rounding))
                    .select_from(Person)
                    .where(*demographic_constraints)
                )
                full_query_all_groups = self._apply_obfuscation(
                    full_query_all_groups, count_expression, low_number
                )
        elif demographic_constraints:
            # Join the cohort set to `person` and filter there, rather than
            # INTERSECTing it with a second large person_id set.
            cohort_subquery = combined_groups.subquery()
            # Every group query selects a person_id column, whatever table it
            # came from, so the join column is always named.
            person_id_column = cohort_subquery.c.person_id
            count_expression = func.count(distinct(person_id_column))

            full_query_all_groups = (
                select(self._round_count(count_expression, rounding))
                .select_from(cohort_subquery)
                .join(Person, Person.person_id == person_id_column)
                .where(*demographic_constraints)
            )
            full_query_all_groups = self._apply_obfuscation(
                full_query_all_groups, count_expression, low_number
            )
        else:
            # No demographics block - count the cohort set directly, as before.
            full_query_all_groups = select(
                self._round_count(func.count(), rounding)
            ).select_from(combined_groups.subquery())
            full_query_all_groups = self._apply_obfuscation(
                full_query_all_groups, func.count(), low_number
            )

        log_query(
            full_query_all_groups, 
            self.db_client.engine
        )

        return full_query_all_groups
