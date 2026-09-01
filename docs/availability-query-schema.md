# Availability Query Schema

An **availability query** asks a collection how many people match a cohort
definition. It is the query Bunny receives most, and it resolves to a single
obfuscated person count.

- Models: `src/hutch_bunny/core/rquest_models/` (`availability.py`, `cohort.py`,
  `group.py`, `rule.py`, `demographics.py`)
- Solver: `src/hutch_bunny/core/solvers/availability_solver.py`
- Machine-readable schema: [`docs/availability-query.schema.json`](./availability-query.schema.json)

---

## What changed in this revision

Three additions, all **backwards compatible** — every payload that worked before
works unchanged, and produces byte-identical SQL.

| # | Addition | Summary |
|---|---|---|
| 1 | [Nested groups](#nested-groups) | A group may contain `groups` as well as `rules`, to arbitrary depth. Previously the tree was pinned at exactly `cohort → group → rule`. |
| 2 | [Multiple concepts per rule](#multiple-concepts-per-rule) | A rule's `value` may be a list of concept ids, resolved as one `IN (...)` per table instead of one query per concept. |
| 3 | [The `demographics` block](#the-demographics-block) | A new top-level key that filters `person` directly. Because it is always ANDed, the solver applies it as a **JOIN to `person`** rather than as another `person_id` set to `INTERSECT`. |

Addition 3 is the one that matters for speed. The query-optimisation study found
that the dominant cost of a large availability query was not the number of tables
searched (narrowing that saved only 3–7%) but the final combine step: an
`INTERSECT` of two multi-million-row `person_id` sets, which has to sort both and
spill to disk. That single step was ~5 of 7 seconds on a 23-concept query.
Replacing it with a join to `person` and one count took the same query from 140s
to 49s on partner-grade hardware — the regime where the ~3–5 minute
Cohort Discovery timeout actually bites.

Note what has **not** changed: a concept is still searched across all five
clinical tables. The same study measured that narrowing to the concept's declared
domain saves 3–7% but silently misses ~6% of concept-scans (4.93% sitting in a
different domain than the central vocabulary expects, plus 0.96% absent from it
entirely), 97% of that being Condition↔Observation drift. That is a bad trade, so
the fan-out stays.

---

## Top-level structure

```json
{
  "task_id": "job-2026-09-01-09:00:54-RQ-6bdd4145",
  "project": "project_id",
  "uuid": "unique_id",
  "owner": "user1",
  "collection": "collection_id",
  "protocol_version": "v2",
  "char_salt": "salt",
  "cohort": {
    "groups_oper": "AND",
    "groups": [
      {
        "rules_oper": "AND",
        "rules": [
          { "varname": "OMOP", "varcat": "Condition", "type": "TEXT", "oper": "=", "value": "260139" }
        ]
      }
    ]
  },
  "demographics": { "age": { "min": 40, "max": 70 }, "gender": [8532] }
}
```

### `AvailabilityQuery`

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `uuid` | `string` | **yes** | — | Unique identifier of the query; echoed back in the result. |
| `owner` | `string` | **yes** | — | Owner (connection id) of the query. |
| `collection` | `string` | **yes** | — | The collection being queried; echoed back as `collection_id`. |
| `protocol_version` | `string` | **yes** | — | Protocol version, for example `v2`. |
| `char_salt` | `string` | **yes** | — | Salt used for hashing. |
| `cohort` | `Cohort` \| `null` | no | `null` | The groups and rules to search for. |
| `demographics` | `Demographics` \| `null` | no | `null` | Filters applied directly to `person` — see [below](#the-demographics-block). |

`cohort` is optional so that a query may filter on `demographics` alone. **A query
must carry at least one group or a `demographics` block**; one with neither is
rejected rather than answered, since it would count the whole population.

Unknown top-level keys are ignored, so envelope fields such as `task_id` and
`project` are harmless.

### `Cohort`

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `groups` | `list[Group]` | no | `[]` | The groups to combine. |
| `groups_oper` | `"AND"` \| `"OR"` | **yes** | — | How the top-level groups combine. |

---

## Nested groups

A `Group` holds `rules`, `groups`, or both.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `rules` | `list[Rule]` | no | `[]` | Leaf search criteria. |
| `groups` | `list[Group]` | no | `[]` | Nested subgroups. |
| `rules_oper` | `"AND"` \| `"OR"` | **yes** | — | How **all** of this group's children combine — its rules and its nested groups alike. |

There is exactly **one operator per level**, as there always has been:
`rules_oper` combines a group's rules *and* its nested subgroups. A nested group
resolves to a set of `person_id`s in precisely the same way a rule does, so the
two mix freely.

```json
{
  "uuid": "unique_id",
  "owner": "user1",
  "collection": "collection_id",
  "protocol_version": "v2",
  "char_salt": "salt",
  "cohort": {
    "groups_oper": "AND",
    "groups": [
      {
        "rules_oper": "AND",
        "rules": [
          { "varname": "OMOP", "varcat": "Observation", "type": "TEXT", "oper": "=", "value": "333" }
        ],
        "groups": [
          {
            "rules_oper": "OR",
            "rules": [
              { "varname": "OMOP", "varcat": "Measurement", "type": "TEXT", "oper": "=", "value": "444" },
              { "varname": "OMOP", "varcat": "Procedure", "type": "TEXT", "oper": "=", "value": "555" }
            ]
          }
        ]
      }
    ]
  }
}
```

reads as `333 AND (444 OR 555)` and compiles to
`(444-set UNION 555-set) INTERSECT 333-set`.

Rules:

- A group must contain **at least one** rule or nested group. An empty group is
  rejected.
- A group may contain *only* nested groups, acting as a pure container.
- Nesting is capped at `MAX_GROUP_DEPTH` (**20**). A payload arrives from
  upstream and is not necessarily trusted, so the depth is bounded; exceeding it
  is a validation error rather than a stack overflow inside the solver. Twenty is
  far beyond anything a cohort-builder UI produces.
- Omitting `groups` reproduces the previous behaviour exactly.

---

## Multiple concepts per rule

A `TEXT` rule's `value` may be a single concept id or a **list** of them:

```jsonc
// one concept - unchanged
{ "varname": "OMOP", "varcat": "Condition", "type": "TEXT", "oper": "=", "value": "260139" }

// several concepts - one IN (...) per table
{ "varname": "OMOP", "varcat": "Condition", "type": "TEXT", "oper": "=",
  "value": ["201826", "4214962", "4181583"] }
```

Ids may be sent as strings or integers. A list of concepts is a **disjunction**:
the rule matches a person with *any* of them.

Previously the only way to express "any of these 23 cancers" was 23 separate
rules in an `OR` group, which built 23 separate five-table unions. A list builds
one, with a single `IN (...)` per table — the same shape the optimisation study
used for its rewritten queries.

`oper: "!="` still inverts the whole rule, so a list with `!=` excludes people
matching *any* of the concepts.

### `Rule`

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `varcat` | `Varcat` | **yes** | — | Which table to search: `Person`, `Condition`, `Observation`, `Drug`, `Measurement`, `Medication`, `Procedure`, `Specimen`, `Location`, `Death`. |
| `varname` | `string` | no | `""` | `OMOP`, `AGE`, or `OMOP=<concept_id>` for measurement-style searches. |
| `type` | `"TEXT"` \| `"NUM"` \| `"GEO_RADIUS"` | no | `"TEXT"` | Kind of value. |
| `oper` | `"="` \| `"!="` | no | `"="` | Inclusion or exclusion. |
| `value` | `string` \| `list` | no | `""` | Concept id, list of concept ids, numeric range, or `lat\|lon\|metres`. |
| `time` | `string` \| `null` | no | `null` | Age-at-event or relative-time window, e.g. `"50\|:AGE:Y"`. |
| `secondary_modifier` | `list[int]` \| `null` | no | `null` | Provenance concepts on `condition_occurrence`. |

A derived `values` field holds every concept on the rule, always as a list: a
single-valued rule yields one element, and an empty `value` (such as the "any
death record" sentinel) yields none. Solver code reads `values`; `value` is kept
as the first element so existing callers and payloads are unaffected. `values` is
derived, never sent.

Lists apply to `TEXT` rules. `NUM` and `GEO_RADIUS` rules rewrite `value` during
parsing (a numeric range, or `lat|lon|metres`), so they carry exactly one concept
as they always have.

---

## The `demographics` block

A top-level block of filters applied directly to the `person` table. Every field
present is combined with `AND`, and the block as a whole is `AND`ed with the
cohort.

```json
{
  "uuid": "unique_id",
  "owner": "user1",
  "collection": "collection_id",
  "protocol_version": "v2",
  "char_salt": "salt",
  "demographics": {
    "age": { "min": 40, "max": 70 },
    "gender": [8532],
    "race": { "concepts": [8527, 8515], "exclude": true },
    "ethnicity": [38003564]
  }
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `age` | `AgeRange` \| `string` \| `null` | no | `null` | Age in years at query time. |
| `gender` | `ConceptFilter` \| `list[int]` \| `null` | no | `null` | Filters `person.gender_concept_id`. |
| `race` | `ConceptFilter` \| `list[int]` \| `null` | no | `null` | Filters `person.race_concept_id`. |
| `ethnicity` | `ConceptFilter` \| `list[int]` \| `null` | no | `null` | Filters `person.ethnicity_concept_id`. |

The block must set at least one field. An empty block is rejected, because it
would silently widen the query to everybody — exactly the mistake the block
exists to avoid.

### `ConceptFilter`

Write a bare list for the common case, or the object form to negate:

```jsonc
"gender": [8532]                                    // shorthand
"gender": { "concepts": [8532], "exclude": false }  // identical
"race":   { "concepts": [8527], "exclude": true }   // NOT IN (8527)
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `concepts` | `list[int]` | **yes** (≥1) | — | Concept ids, combined with `IN`. |
| `exclude` | `boolean` | no | `false` | Negates **only this field**. |

`exclude` negating only its own field is a deliberate difference from a `Person`
rule, where `oper: "!="` negates the demographic *and* any age qualifier attached
to it — so `!=` on a gender rule with an age window also matches people of that
gender outside the window.

### `AgeRange`

An inclusive range in years. Accepts the object form or the same pipe-separated
string the existing `AGE` rule uses, so `"age": "40|70"` and
`"age": {"min": 40, "max": 70}` are equivalent.

| Field | Type | Description |
|---|---|---|
| `min` | `float` \| `null` | Lower bound, inclusive. |
| `max` | `float` \| `null` | Upper bound, inclusive. |

At least one bound is required, and `min` may not exceed `max`. Open-ended forms
(`"40|"`, `"|70"`, `{"min": 40}`) emit a single comparison.

### Relationship to `Person` rules

`varcat: "Person"` rules still work and are unchanged. The difference is what the
solver may assume:

| | `Person` rule | `demographics` block |
|---|---|---|
| Where it sits | Inside a group | Top level |
| Combining | May sit under an `OR` | Always `AND` |
| Which column | Resolved at runtime from `concept.domain_id` | Named explicitly in the payload |
| Compiles to | Another `person_id` set to `INTERSECT` | A `JOIN` to `person` with `WHERE` clauses |

Because a block is guaranteed to be a conjunction, the solver can put it on a
join instead of building a second large `person_id` set. A rule cannot be treated
that way — under an `OR` it is genuinely a set union, so the guarantee does not
hold.

Naming the column also removes a vocabulary lookup: a `Person` rule has to fetch
`concept.domain_id` to discover whether `8507` means gender, race or ethnicity,
and **silently contributes no constraint** if the concept resolves to none of
them. A block says which field it means.

Location and death filtering continue to use `varcat: "Location"` and
`varcat: "Death"` rules; they are not part of the block.

---

## SQL translation

Each rule becomes a `SELECT person_id` over the five clinical tables, `UNION`ed
together. Rules and nested groups within a group combine with `INTERSECT` (`AND`)
or `UNION` (`OR`). Groups become CTEs named `final_group_<n>` and combine the same
way. That much is unchanged.

What changes is the final step.

### Without a `demographics` block — unchanged

```sql
WITH final_group_0 AS (...)
SELECT count(*)
FROM (SELECT final_group_0.person_id FROM final_group_0) AS anon_1
```

### With a `demographics` block — join `person`

```sql
WITH final_group_0 AS (
  SELECT measurement.person_id FROM measurement
   WHERE measurement.measurement_concept_id IN (201826, 4214962)
  UNION SELECT observation.person_id FROM observation
   WHERE observation.observation_concept_id IN (201826, 4214962)
  UNION SELECT condition_occurrence.person_id FROM condition_occurrence
   WHERE condition_occurrence.condition_concept_id IN (201826, 4214962)
  UNION SELECT drug_exposure.person_id FROM drug_exposure
   WHERE drug_exposure.drug_concept_id IN (201826, 4214962)
  UNION SELECT procedure_occurrence.person_id FROM procedure_occurrence
   WHERE procedure_occurrence.procedure_concept_id IN (201826, 4214962)
)
SELECT count(DISTINCT anon_1.person_id)
FROM (SELECT final_group_0.person_id FROM final_group_0) AS anon_1
JOIN person ON person.person_id = anon_1.person_id
WHERE date_part('year', CURRENT_TIMESTAMP) - person.year_of_birth >= 40.0
  AND date_part('year', CURRENT_TIMESTAMP) - person.year_of_birth <= 70.0
  AND person.gender_concept_id = 8532
```

There is **no `INTERSECT` against a demographic set** — that is the whole point.
The equivalent query written with a `Person` rule instead has to materialise
every person aged 40–70 who is female (a quarter of the population on a 50M-person
collection) and intersect it with the concept set.

### Demographics only

With no groups, `person` *is* the cohort, so there is no subquery at all:

```sql
SELECT count(DISTINCT person.person_id)
FROM person
WHERE date_part('year', CURRENT_TIMESTAMP) - person.year_of_birth >= 40.0
  AND date_part('year', CURRENT_TIMESTAMP) - person.year_of_birth <= 70.0
  AND person.gender_concept_id = 8532
```

### Age arithmetic

Age uses the same year-difference expression as the existing `AGE` rule
(`SQLDialectHandler.get_year_difference`), so a `demographics` block and the
equivalent `AGE` rule return **identical counts**. It is deliberately not
rewritten into a sargable `year_of_birth` comparison: the optimisation study found
the demographic filter matches roughly a quarter of the population, where a full
read is genuinely the fastest plan, and matching the existing expression keeps the
two paths verifiably equal.

---

## Obfuscation

Unchanged, and applied on every path. Low-number suppression becomes a `HAVING`
clause and rounding wraps the count:

```sql
SELECT round(count(DISTINCT anon_1.person_id) / 10, 0) * 10
...
HAVING count(DISTINCT anon_1.person_id) >= 10
```

A suppressed count returns no row, which is reported as `0`. Both are also
re-applied in Python by `apply_filters`, and both default to 10. Modifiers are
supplied in the standard form:

```json
[
  { "id": "Low Number Suppression", "threshold": 10 },
  { "id": "Rounding", "nearest": 10 }
]
```

---

## Backwards compatibility

Every change is additive:

- A payload with no `groups` key on a group parses as before — `groups` defaults
  to empty.
- A payload with no `demographics` key takes the original counting path and
  produces byte-identical SQL.
- A `value` string behaves exactly as before, and a single concept still compiles
  to `= <id>` rather than `IN (<id>)`.
- `Person`, `Location` and `Death` rules are untouched.

The two relaxations are `Cohort.groups` and `AvailabilityQuery.cohort` becoming
optional. Both previously required, so nothing that validated before fails now.

---

## JSON Schema

A machine-readable **JSON Schema (Draft 2020-12)** is generated from the Pydantic
models:

- Schema file: [`docs/availability-query.schema.json`](./availability-query.schema.json)
- Generator: `scripts/generate_availability_schema.py`

```bash
uv run python scripts/generate_availability_schema.py
```

`tests/unit/docs/test_doc_examples_validate.py` re-runs the generator and fails if
the committed schema has drifted, so it cannot silently go stale.

The generator widens a few properties beyond what the annotations say, because
the models accept more than the field types express. Those widenings are declared
in `INPUT_SHORTHANDS` in the generator:

- `Rule.value` — a single concept id or a list of them.
- `Demographics.gender` / `race` / `ethnicity` — an object or a bare concept list.
- `Demographics.age` — an object or a `"<min>|<max>"` string.
- `Rule.secondary_modifier` — declared `list[int]`, but senders have always put
  strings here (see `tests/queries/availability/secondary_modifiers.json`) and
  Pydantic coerces them.

### What the schema does not cover

Cross-field rules are enforced by Pydantic at parse time and are not expressible
in JSON Schema:

- a query must carry at least one group or a `demographics` block;
- a group must carry at least one rule or nested group;
- nesting may not exceed `MAX_GROUP_DEPTH`;
- an age range needs at least one bound, and `min <= max`;
- a demographics block must set at least one field.

Treat the JSON Schema as a first-pass structural gate and the Pydantic models as
the authority.

---

## Validation summary

| Rule | Raised by |
|---|---|
| At least one group or a `demographics` block | `AvailabilityQuery.check_has_a_query_body` |
| A group has at least one rule or nested group | `Group.validate_group_tree` |
| Nesting depth ≤ `MAX_GROUP_DEPTH` (20) | `Group.validate_group_tree` |
| Age range has a bound, and `min <= max` | `AgeRange.check_bounds` |
| Age range string is `"<min>\|<max>"` | `AgeRange.accept_pipe_separated` |
| `concepts` is non-empty | `ConceptFilter` |
| Demographics block is non-empty | `Demographics.check_not_empty` |
