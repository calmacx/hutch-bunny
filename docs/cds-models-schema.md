# CDS Query Schema

`cds_models` is an alternative cohort-query model for Bunny, giving fine-grained
control over how an availability query is translated into SQL. It runs
**alongside** the legacy `rquest_models` path — Bunny auto-detects which model a
payload uses from its structure (see [Detection](#detection)) and routes
accordingly. Both produce the same result shape (`RquestResult`).

A CDS query resolves to a single **cohort person count** against an OMOP CDM
database. Everything is driven from the `person` table: demographics are plain
predicates on `person` columns, and each clinical rule becomes a correlated
`EXISTS`. There is no `INTERSECT` of large `person_id` sets.

- Models: `src/hutch_bunny/core/cds_models/` (`query.py`, `nodes.py`,
  `demographics.py`, `domains.py`)
- Solver: `src/hutch_bunny/core/solvers/cds_availability_solver.py`

---

## Revision history

### Revision 2 — nesting, multi-domain, demographics block

Three changes, driven by the BUNNY optimisation & data-diversity report:

1. **Nesting** — arbitrary group depth was already the design; this revision
   documents the `exclude` semantics precisely and adds a **depth cap**
   (`MAX_GROUP_DEPTH`, 20) so a pathological payload cannot recurse unguarded.
2. **Multi-domain clinical rules** — `domain` (a single required string) becomes
   `domains` (an **optional** list). Omitting it fans the concepts out across all
   five clinical tables, which is Bunny's long-standing safe default; supplying it
   narrows the search deliberately. See [Domains](#domains).
3. **Top-level `demographics` block** — demographics move out of the cohort tree
   into their own top-level key that is always `AND`ed. This guarantees the person
   filter is a conjunctive prefix the query planner can lead with. See
   [Demographics](#demographics).

Why (2) is a *fallback*, not a *trust*: the report measured a partner's declared
domain disagreeing with the central vocabulary on **~6%** of concept-scans, 97% of
it Condition/Observation drift. Narrowing to one table saved only **3–7%** of query
time. Paying a 6% miss rate for a 3–7% speed-up is a bad trade, so all-five stays
the default and narrowing is opt-in for callers who know what they hold.

Why (3) matters: the report found the dominant cost was not the number of tables
but the final combine step — an `INTERSECT` of two multi-million-row `person_id`
sets, ~5 of 7 seconds on a 23-concept query. Joining straight to `person` and
counting once took that query from 140s to 49s on partner-grade hardware.

**Breaking change:** `kind: "demographic"` nodes are no longer accepted inside the
cohort tree. See [Demographics](#demographics) for the migration and the trade-off.

---

## Top-level structure

```jsonc
{
  "uuid": "unique_id",
  "collection": "collection_id",
  "owner": "user1",
  "protocol_version": "v2",
  "char_salt": "salt",
  "cohort": { /* a group node — see below */ },
  "demographics": { /* a person-table filter block — see below */ }
}
```

### `CdsQuery` (envelope)

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `cohort` | `CdsGroup` \| `null` | conditional | `null` | Root of the clinical query tree. Must be a **group** node. |
| `demographics` | `CdsDemographics` \| `null` | conditional | `null` | Person-table filter block, always `AND`ed with `cohort`. |
| `uuid` | `string` | **yes** | — | Unique identifier of the query; echoed back in the result. |
| `collection` | `string` | **yes** | — | Collection the query runs against; echoed back as `collection_id`. |
| `owner` | `string` | no | `"user1"` | Owner / connection id. |
| `protocol_version` | `string` | no | `"v2"` | Protocol version. |
| `char_salt` | `string` | no | `""` | Hashing salt (unused by the solver; carried for parity). |

**At least one of `cohort` / `demographics` must be present.** A query with only
`demographics` is valid and useful — "how many women aged 40–70 are there" needs no
clinical tree. A query with neither is rejected, since it would count the whole
population.

Unknown top-level keys are ignored (Pydantic default), so upstream envelope
fields such as `task_id` or `project` are harmless.

Defined in `cds_models/query.py`.

---

## Nodes

The `cohort` is a recursive tree of **nodes**. Every node has a `kind`
discriminator, so the tree is a Pydantic *discriminated union* and is
self-describing. There are two node kinds:

| `kind` | Model | Role |
|---|---|---|
| `"group"` | `CdsGroup` | Combines child nodes with a boolean or temporal operator. |
| `"clinical"` | `CdsClinicalRule` | A concept search against one or more OMOP clinical domains. |

Defined in `cds_models/nodes.py`.

Every node compiles to a boolean predicate over `person.person_id`, which is why
groups can nest to arbitrary depth — a group is just a boolean/temporal
combination of its children's predicates.

> `kind: "demographic"` was a third node kind in revision 1. It has been removed;
> demographics now live in the top-level [`demographics`](#demographics) block. A
> payload still using it is rejected with a message pointing there.

### `CdsGroup` (`kind: "group"`)

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `kind` | `"group"` | no | `"group"` | Node discriminator. |
| `operator` | `"AND"` \| `"OR"` \| `"AFTER"` | **yes** | — | How children combine (see [Operators](#operators)). |
| `children` | `list[node]` | **yes** (≥1) | — | Child nodes: any mix of groups and clinical rules (restricted for `AFTER`). |
| `exclude` | `boolean` | no | `false` | Negates the whole group's predicate (`NOT (...)`). |
| `after_gap` | `Range` \| `null` | no | `null` | Gap (days) to the previous step when this group is a non-first child of an `AFTER` group. Ignored otherwise. |

Notes:

- **A one-child group is legal** and compiles to its child's predicate unchanged
  (negated if `exclude` is set). Senders may wrap freely without cost.
- **`exclude` negates the combined predicate, not each child.** On an `OR` group,
  `exclude: true` means "matches **none** of the children" — `NOT (A OR B)` — not
  "matches not-all-of". On an `AND` group it means `NOT (A AND B)`, which still
  matches people who satisfy one child but not the other. This trips people up;
  if you want "has A but not B", use an `AND` group with `exclude` on the B child.
- **Depth is capped** at `MAX_GROUP_DEPTH` (20) nested groups. Exceeding it is a
  validation error rather than a recursion failure deep in the solver. Twenty is
  far beyond any UI-authored query; the cap exists to bound untrusted input.

### `CdsClinicalRule` (`kind: "clinical"`)

A concept search against one or more OMOP domain tables. Concepts are always
resolved with a single `WHERE <concept_col> IN (...)` per table.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `kind` | `"clinical"` | no | `"clinical"` | Node discriminator. |
| `concepts` | `list[int]` | **yes** (≥1) | — | Concept ids, combined with `IN`. |
| `domains` | `list[Domain]` \| `null` | no | `null` | Which OMOP domains to search. **Omit to fan out across all five clinical tables** (see [Domains](#domains)). |
| `domain` | `Domain` | no | — | Shorthand for a single-element `domains`. Supplying **both** `domain` and `domains` is a validation error. |
| `exclude` | `boolean` | no | `false` | `true` → the rule matches people *without* the concept. |
| `value_range` | `Range` \| `null` | no | `null` | Numeric `value_as_number` range. **Measurement / Observation only.** |
| `age_at_event` | `Range` \| `null` | no | `null` | Age (years) at the time of the event. |
| `after_gap` | `Range` \| `null` | no | `null` | Gap (days) to the previous step in an `AFTER` sequence. Ignored otherwise. |

```jsonc
// safe default — searches all five clinical tables
{ "kind": "clinical", "concepts": [201826, 4214962] }

// deliberate narrowing — Condition + Observation only
{ "kind": "clinical", "domains": ["Condition", "Observation"], "concepts": [201826] }

// single-domain shorthand
{ "kind": "clinical", "domain": "Measurement", "concepts": [3004249] }
```

---

## `Range`

A reusable double-sided numeric range, used by `value_range`, `age_at_event`,
demographic `age` / `age_at_death`, and `after_gap`.

```json
{ "min": 1.0, "max": 3.0 }
```

| Field | Type | Description |
|---|---|---|
| `min` | `float` \| `null` | Lower bound (inclusive). |
| `max` | `float` \| `null` | Upper bound (inclusive). |

Rules:

- At least one of `min` / `max` must be present.
- If both are present, `min <= max`.
- Both present → SQL `BETWEEN min AND max`.
- Only `min` → `column >= min`.
- Only `max` → `column <= max`.

Units depend on context: **years** for age, **days** for `after_gap`, and the
raw measurement/observation value for `value_range`.

---

## Domains

A `Domain` is one of the keys below. Each maps to exactly one OMOP table and its
columns (`cds_models/domains.py`).

| `Domain` | Table | Concept column | Event date column | Value column | In default fan-out |
|---|---|---|---|---|---|
| `Condition` | `condition_occurrence` | `condition_concept_id` | `condition_start_date` | — | ✅ |
| `Observation` | `observation` | `observation_concept_id` | `observation_date` | `value_as_number` | ✅ |
| `Measurement` | `measurement` | `measurement_concept_id` | `measurement_date` | `value_as_number` | ✅ |
| `Drug` | `drug_exposure` | `drug_concept_id` | `drug_exposure_start_date` | — | ✅ |
| `Procedure` | `procedure_occurrence` | `procedure_concept_id` | `procedure_date` | — | ✅ |
| `Medication` | `drug_exposure` | `drug_concept_id` | `drug_exposure_start_date` | — | — (alias of `Drug`) |
| `Specimen` | `specimen` | `specimen_concept_id` | `specimen_date` | — | — (must be named) |

### Resolving a rule's domains

| Case | Effective domains |
|---|---|
| `domains` omitted, no `value_range` | `["Condition", "Observation", "Measurement", "Drug", "Procedure"]` |
| `domains` omitted, `value_range` set | `["Measurement", "Observation"]` — the only domains with a value column |
| `domains` given | Exactly those, deduplicated |
| `domain` given | `[domain]` |

- **`Specimen` is never in the default fan-out.** It is gated by
  `OMOP_SPECIMEN_ENABLED`, and it was outside the vocabulary-drift analysis that
  justifies the default, so it must be named explicitly.
- **`Medication` is an alias for `Drug`.** If both appear in `domains` they
  collapse to one `drug_exposure` branch — the rule is not evaluated twice.
- **`value_range` on a domain with no value column is an error.** If `domains`
  explicitly names e.g. `Condition` alongside a `value_range`, the query is
  rejected rather than silently matching everyone with the bare concept. (The
  legacy path has exactly that bug: `add_numeric_range` constrains only the
  measurement and observation legs and leaves the other three unfiltered.)
- **An `AFTER` step must resolve to exactly one domain**, since ordering needs a
  single event date column. More than one is a validation error.

### Why the default is all five

Bunny has always fanned each concept across every clinical table, because a
concept's domain can move between vocabulary releases and Bunny cannot assume
where a partner's records sit. The optimisation report quantified both sides of
dropping that:

| Outcome | Share of concept-scans |
|---|---|
| Central vocabulary agrees with the partner's placement | 94.11% |
| Disagrees — a single-table search looks in the **wrong** table | 4.93% |
| Concept absent from the central vocabulary entirely | 0.96% |

So trusting a declared domain unconditionally would silently miss ~6% of concepts,
97% of that being Condition↔Observation drift — while saving only 3–7% of query
time even on a 23-concept query. Hence: fan out by default, narrow on request.

If you *do* narrow, the report's safe pattern is to keep `Condition` and
`Observation` together, use a single domain for `Drug` / `Measurement` /
`Procedure`, and omit `domains` whenever unsure. That holds the miss rate to
~0.5–1% instead of ~6%.

---

## Operators

A `CdsGroup.operator` determines how the group's children combine.

### `AND`

All children must match. Each child's predicate is combined with SQL `AND`.

### `OR`

Any child may match. Children are combined with SQL `OR`.

`AND` / `OR` children may be **any** node kind (clinical rules or nested groups),
to any depth up to `MAX_GROUP_DEPTH`.

### `AFTER`

A **temporal sequence** (a patient pathway): the children are ordered clinical
events, and each event must occur **strictly after** the previous one. See
[The `AFTER` operator](#the-after-operator).

---

## Demographics

`demographics` is a top-level block of filters on the `person` table. It sits
**outside** the cohort tree and is always `AND`ed with it.

A complete demographics-only query (no clinical tree — see
[Top-level structure](#top-level-structure)):

```json
{
  "uuid": "unique_id",
  "collection": "collection_id",
  "demographics": {
    "age":       { "min": 40, "max": 70 },
    "gender":    [8532],
    "race":      { "concepts": [8527], "exclude": true },
    "ethnicity": [38003564],
    "location":  {
      "geo_radius": { "latitude": 55.9533, "longitude": -3.1883, "radius_metres": 10000 },
      "country_concepts": [4330435],
      "source_values": ["GBR"]
    },
    "death": {
      "status": "deceased",
      "cause_concepts": [4329847],
      "age_at_death": { "min": 70, "max": 95 }
    }
  }
}
```

### `CdsDemographics`

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `age` | `Range` \| `null` | no | `null` | Age in **years** at query time. |
| `gender` | `ConceptFilter` \| `null` | no | `null` | Filters `person.gender_concept_id`. |
| `race` | `ConceptFilter` \| `null` | no | `null` | Filters `person.race_concept_id`. |
| `ethnicity` | `ConceptFilter` \| `null` | no | `null` | Filters `person.ethnicity_concept_id`. |
| `location` | `CdsLocationFilter` \| `null` | no | `null` | Filters via `person.location_id` → `location`. |
| `death` | `CdsDeathFilter` \| `null` | no | `null` | Filters via the `death` table. |

**Every key present is `AND`ed**, both with each other and with the cohort. The
block must contain at least one key; an empty `demographics: {}` is rejected.

Defined in `cds_models/demographics.py`.

### `ConceptFilter`

`gender` / `race` / `ethnicity` accept either a bare array of concept ids
(shorthand for `exclude: false`) or the full object form:

```jsonc
"gender": [8532]                                  // shorthand
"gender": { "concepts": [8532], "exclude": false } // equivalent long form
"race":   { "concepts": [8527], "exclude": true }  // NOT IN (8527)
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `concepts` | `list[int]` | **yes** (≥1) | — | Concept ids, combined with `IN`. |
| `exclude` | `boolean` | no | `false` | `true` → `NOT (column IN (...))`. |

Note that `exclude` negates **only that field**, unlike the legacy path where
negating a gender rule that also carried an age qualifier negated the conjunction
of both.

### `CdsLocationFilter`

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `geo_radius` | `GeoRadius` \| `null` | no | `null` | Circle around a point; see below. |
| `country_concepts` | `list[int]` \| `null` | no | `null` | Filters `location.country_concept_id` with `IN`. |
| `source_values` | `list[str]` \| `null` | no | `null` | Filters `location.location_source_value` with `IN` (e.g. `["GBR"]`). |
| `exclude` | `boolean` | no | `false` | Negates the whole location predicate. |

`GeoRadius`:

| Field | Type | Required | Description |
|---|---|---|---|
| `latitude` | `float` | **yes** | Centre latitude, decimal degrees, −90 to 90. |
| `longitude` | `float` | **yes** | Centre longitude, decimal degrees, −180 to 180. |
| `radius_metres` | `float` | **yes** | Radius in **metres**, must be > 0. |

- At least one of the three filter keys must be present.
- Multiple keys are `AND`ed — `country_concepts` **and** `geo_radius` together
  means "in this country *and* within this circle". This replaces the legacy
  restriction that the two location filter types were mutually exclusive per rule
  and had to be split across two `AND`ed rules.
- Rows with a `NULL` latitude or longitude never satisfy `geo_radius`.
- Distance uses the haversine formula via
  `SQLDialectHandler.get_haversine_distance`, which is implemented for
  PostgreSQL, DuckDB, MSSQL and Snowflake.

### `CdsDeathFilter`

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `status` | `"deceased"` \| `"alive"` \| `null` | no | `null` | Whether the person has a `death` row at all. |
| `cause_concepts` | `list[int]` \| `null` | no | `null` | Filters `death.cause_concept_id` with `IN`. |
| `age_at_death` | `Range` \| `null` | no | `null` | Age in **years** at `death.death_date`. |

- At least one key must be present.
- `status: "alive"` may not be combined with `cause_concepts` or `age_at_death` —
  a person with no death row has no cause and no age at death. This is a
  validation error rather than a silently empty result.
- `cause_concepts` or `age_at_death` without a `status` implies
  `status: "deceased"`.

### Feature flags

`location` and `death` are gated by the same settings as the legacy path
(`core/settings.py`), and **both default to disabled**:

| Block key | Setting | When disabled |
|---|---|---|
| `location` | `OMOP_LOCATION_ENABLED` | The filter matches **nobody** (or everybody, if `exclude: true`). |
| `death` | `OMOP_DEATH_ENABLED` | `status: "deceased"`, `cause_concepts`, `age_at_death` match **nobody**; `status: "alive"` matches **everybody**. |

This mirrors the legacy fallback exactly, so counts do not change when a partner
migrates to the CDS model. Be aware of the consequence: **the same query returns
different counts at partners with different flags**, with no error to distinguish
"nobody matched" from "this partner has not enabled the table". Where that
distinction matters, check the collection's declared capabilities upstream rather
than inferring it from a zero count.

### Why a separate block, and what it costs

The whole query is already `SELECT count(*) FROM person WHERE …`, so `age`,
`gender`, `race` and `ethnicity` compile to plain sargable predicates directly on
`person` columns — no join, no subquery. Hoisting them out of the tree guarantees
they are a **conjunctive prefix**: the planner can always lead with the person
filter and probe the clinical tables for the survivors. That is precisely the
rewrite the optimisation report measured at 140s → 49s on partner-grade hardware.

A demographic *leaf node* could not guarantee that, because it might sit under an
`OR` — and `EXISTS(...) OR person.gender_concept_id = 8532` forces the planner to
consider the whole population regardless.

**The cost:** you can no longer express a demographic in a disjunction, e.g.
"has diabetes **OR** is over 80". That is deliberate — it is exactly the shape
that forfeits the optimisation. If a genuine use case appears, re-introducing a
`kind: "demographic"` leaf is purely additive and would not break any payload
written against this revision.

**Migration from revision 1:** lift each `kind: "demographic"` node out of the
tree and into the block.

```jsonc
// revision 1
{ "cohort": { "kind": "group", "operator": "AND", "children": [
    { "kind": "clinical", "domain": "Observation", "concepts": [4062643] },
    { "kind": "demographic", "field": "age", "range": { "min": 57, "max": 93 } },
    { "kind": "demographic", "field": "gender", "concepts": [8507] }
] } }

// revision 2
{ "cohort": { "kind": "group", "operator": "AND", "children": [
    { "kind": "clinical", "domains": ["Observation"], "concepts": [4062643] }
] },
  "demographics": { "age": { "min": 57, "max": 93 }, "gender": [8507] } }
```

Because demographics were only ever `AND`ed in practice, this is a mechanical
rewrite for every query the Daphne API currently emits.

---

## SQL translation

The whole query becomes a single `person`-driven count:

```sql
SELECT <count expression>
FROM person
WHERE <demographics predicate> AND <cohort predicate>
```

### Clinical rule

One correlated `EXISTS` per effective domain, `OR`ed together:

```sql
(  EXISTS (SELECT 1 FROM condition_occurrence c
           WHERE c.person_id = person.person_id
             AND c.condition_concept_id IN (...))
OR EXISTS (SELECT 1 FROM observation o
           WHERE o.person_id = person.person_id
             AND o.observation_concept_id IN (...)
             [ AND o.value_as_number BETWEEN <min> AND <max> ]          -- value_range
             [ AND (year(o.observation_date) - person.year_of_birth)
                   BETWEEN <min> AND <max> ])                            -- age_at_event
OR ... )                                                                 -- one branch per domain
```

`exclude: true` wraps the disjunction in `NOT (...)`, which is equivalent to
requiring `NOT EXISTS` on every branch. With a single effective domain the whole
thing collapses to one `EXISTS`, so a narrowed rule costs exactly what it did in
revision 1.

`value_range` and `age_at_event` are applied **per branch**, against that domain's
own value and date columns.

### Demographics

- `gender` / `race` / `ethnicity` → `person.<x>_concept_id IN (...)`, wrapped in
  `NOT (...)` when `exclude` is set.
- `age` → a sargable `year_of_birth` range. `age BETWEEN min AND max` is
  equivalent to `year_of_birth BETWEEN (current_year - max) AND (current_year - min)`,
  which keeps the column bare so an index on it remains usable.
- `location` → a correlated `EXISTS` through `person.location_id`, so the outer
  `FROM person` shape is preserved rather than turning into a join.
- `death` → a correlated `EXISTS` (or `NOT EXISTS` for `status: "alive"`) on the
  `death` table, which is at most one row per person.

```sql
-- location
EXISTS (SELECT 1 FROM location l
        WHERE l.location_id = person.location_id
          AND l.country_concept_id IN (4330435)
          AND l.location_source_value IN ('GBR')
          AND l.latitude IS NOT NULL AND l.longitude IS NOT NULL
          AND <haversine(l.latitude, l.longitude)> <= 10000)

-- death, status "deceased"
EXISTS (SELECT 1 FROM death d
        WHERE d.person_id = person.person_id
          AND d.cause_concept_id IN (4329847)
          AND (year(d.death_date) - person.year_of_birth) BETWEEN 70 AND 95)

-- death, status "alive"
NOT EXISTS (SELECT 1 FROM death d WHERE d.person_id = person.person_id)
```

### Group

`AND` / `OR` (or `AFTER`) of its children's predicates, negated if `exclude`.

The count is then wrapped with **obfuscation baked into the SQL** (see
[Obfuscation](#obfuscation)).

`EXISTS` compiles to a semi-join in PostgreSQL, so with the concept-id /
person-id indices (`scripts/omop_indices.sql`) this stays efficient and avoids
materialising large `person_id` sets.

### Worked example

```json
{
  "uuid": "unique_id",
  "collection": "collection_id",
  "cohort": {
    "kind": "group",
    "operator": "AND",
    "children": [
      {
        "kind": "clinical",
        "domains": ["Observation"],
        "concepts": [4062643, 4064912, 4166438]
      }
    ]
  },
  "demographics": {
    "age": { "min": 57, "max": 93 },
    "gender": [8507]
  }
}
```

compiles to (PostgreSQL, rounding/suppression at defaults of 10):

```sql
SELECT round(count(*) / CAST(10 AS NUMERIC), 0) * 10
FROM person
WHERE person.year_of_birth >= date_part('year', CURRENT_TIMESTAMP) - 93
  AND person.year_of_birth <= date_part('year', CURRENT_TIMESTAMP) - 57
  AND person.gender_concept_id IN (8507)
  AND EXISTS (
        SELECT 1 FROM observation
        WHERE observation.person_id = person.person_id
          AND observation.observation_concept_id IN (4062643, 4064912, 4166438)
      )
HAVING count(*) >= 10;
```

Dropping `"domains"` from that rule would replace the single `EXISTS` with a
five-branch `OR` — the safe default — at a measured cost of 3–7%.

---

## The `AFTER` operator

An `AFTER` group expresses an **ordered temporal sequence** of clinical events:
child *i+1*'s event must occur strictly after child *i*'s event
(a *consecutive chain* — for `[A, B, C]`: `B.date > A.date` **and** `C.date > B.date`).

### What `AFTER` children may be

`AFTER` children are restricted (they must have a single event date to order by):

- ✅ `CdsClinicalRule` **resolving to exactly one domain** — ordered by that
  domain's event date column. A rule with no `domains` (or more than one) is
  rejected inside an `AFTER` group, since there would be no single date to order by.
- ✅ a nested `CdsGroup` whose `operator` is also `"AFTER"` (a sub-sequence).
- ❌ `AND` / `OR` groups (no single event date).

Additional validation for an `AFTER` group:

- Must have **at least two** children.
- **No** child may set `exclude: true` (a `NOT EXISTS` would break the date chain).
- The **first** child may not set `after_gap` (it has no predecessor).

Demographics are not a concern here — they live in the top-level block and are
`AND`ed around the whole sequence.

### `after_gap` — optional gap between steps

A non-first child may carry `after_gap`, a `Range` in **days** constraining the
gap from the previous step's event:

```json
{ "kind": "clinical", "domain": "Measurement", "concepts": [3004249],
  "after_gap": { "min": 30, "max": 365 } }
```

means "at least 30 and at most 365 days after the previous event". It compiles
to a day-difference `BETWEEN` alongside the strict `>` ordering:

```sql
AND b.measurement_date > a.condition_start_date
AND (b.measurement_date - a.condition_start_date) BETWEEN 30 AND 365
```

Day-difference is dialect-aware: date subtraction on PostgreSQL / DuckDB,
`DATEDIFF(day, …)` on MSSQL / Snowflake.

### Nested `AFTER` flattens

With consecutive-chain semantics, a nested `AFTER` sub-sequence is equivalent to
inlining its steps. `AFTER[X, AFTER[P, Q], Z]` imposes `P>X`, `Q>P`, `Z>Q` —
exactly the flat chain `X<P<Q<Z`. The boundary gap on the sub-sequence node
(`after_gap` on the nested group) becomes the gap on its first inlined step;
internal gaps are preserved. The solver flattens the tree into a linear list of
steps and emits **one** chain of correlated `EXISTS`, so nested `AFTER` is purely
an authoring convenience.

### `AFTER` example

```json
{
  "uuid": "unique_id",
  "collection": "collection_id",
  "cohort": {
    "kind": "group",
    "operator": "AFTER",
    "children": [
      { "kind": "clinical", "domain": "Condition", "concepts": [201826] },
      { "kind": "clinical", "domain": "Measurement", "concepts": [3004249],
        "after_gap": { "min": 30, "max": 365 } },
      { "kind": "clinical", "domain": "Drug", "concepts": [1503297, 1503327] }
    ]
  }
}
```

compiles to a nested correlated `EXISTS` chain:

```sql
SELECT round(count(*) / CAST(10 AS NUMERIC), 0) * 10
FROM person
WHERE EXISTS (                                         -- step A (earliest)
  SELECT 1 FROM condition_occurrence a
  WHERE a.person_id = person.person_id
    AND a.condition_concept_id IN (201826)
    AND EXISTS (                                       -- step B, correlated to A
      SELECT 1 FROM measurement b
      WHERE b.person_id = person.person_id
        AND b.measurement_concept_id IN (3004249)
        AND b.measurement_date > a.condition_start_date
        AND (b.measurement_date - a.condition_start_date) BETWEEN 30 AND 365
        AND EXISTS (                                   -- step C, correlated to B
          SELECT 1 FROM drug_exposure c
          WHERE c.person_id = person.person_id
            AND c.drug_concept_id IN (1503297, 1503327)
            AND c.drug_exposure_start_date > b.measurement_date
        )
    )
)
HAVING count(*) >= 10;
```

An `AFTER` group is itself just a predicate over `person`, so it composes
normally as a child of an `AND` / `OR` group.

---

## Detection

Bunny picks the CDS model purely from JSON structure (`execute_query.py`,
`_is_cds_query`). Dispatch order:

1. `"analysis"` key present → **distribution** query (legacy path).
2. A top-level `"demographics"` key is present, **or** `cohort` is an object with
   `"kind": "group"` or a `"children"` key → **CDS** query.
3. Otherwise → legacy **availability** query.

The `demographics` clause is needed because `cohort` is now optional, and it is
unambiguous because the legacy envelope has no such key. The legacy availability
`cohort` uses `groups` / `groups_oper`, so the two remain distinguishable. No env
flag or explicit discriminator is required.

---

## Obfuscation

The CDS solver applies **low-number suppression** and **rounding** in SQL, so a
raw count is never emitted:

- Rounding → `round(count(*) / nearest, 0) * nearest`.
- Low-number suppression → `HAVING count(*) >= threshold` (a suppressed count
  returns no row, reported as `0`).

Both are driven by the results modifiers passed with the task and **default to
10** when the corresponding modifier is absent. Modifiers are supplied per the
standard `results_modifiers()` helper:

```json
[
  { "id": "Low Number Suppression", "threshold": 10 },
  { "id": "Rounding", "nearest": 10 }
]
```

Setting `nearest` or `threshold` to `0` disables that step.

---

## JSON Schema

A machine-readable **JSON Schema (Draft 2020-12)** is generated directly from the
Pydantic models, so it never drifts from the code:

- Schema file: [`docs/cds-query.schema.json`](./cds-query.schema.json)
- Generator: `scripts/generate_cds_schema.py`

Regenerate it whenever `cds_models` changes:

```bash
uv run python scripts/generate_cds_schema.py
```

> **Revision 2 status:** the schema file is currently **hand-maintained**, because
> this branch carries the spec ahead of the models. Once the revision-2 changes
> land in `cds_models`, regenerate and diff against the committed file — the
> generator is the source of truth from that point on, and any divergence is a
> bug in the models or in this doc.

Use it to validate query payloads on the sender side (e.g. the Daphne API), or in
an editor for autocomplete. Structurally it is a single root object with a
recursive `$defs/CdsGroup`; the `children` array is a discriminated `oneOf` keyed
on `kind`:

```jsonc
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://hdruk.github.io/hutch-bunny/cds-query.schema.json",
  "type": "object",
  "required": ["uuid", "collection"],
  "anyOf": [{ "required": ["cohort"] }, { "required": ["demographics"] }],
  "properties": {
    "cohort": { "anyOf": [{ "$ref": "#/$defs/CdsGroup" }, { "type": "null" }] },
    "demographics": { "anyOf": [{ "$ref": "#/$defs/CdsDemographics" }, { "type": "null" }] },
    "owner": { "type": "string", "default": "user1" }
    // ... uuid, collection, protocol_version, char_salt
  },
  "$defs": {
    "CdsGroup": {
      "properties": {
        "operator": { "enum": ["AND", "OR", "AFTER"] },
        "children": {
          "type": "array", "minItems": 1,
          "items": {
            "discriminator": { "propertyName": "kind" },
            "oneOf": [
              { "$ref": "#/$defs/CdsGroup" },
              { "$ref": "#/$defs/CdsClinicalRule" }
            ]
          }
        }
        // ... exclude, after_gap
      }
    }
    // ... CdsClinicalRule, CdsDemographics, ConceptFilter,
    //     CdsLocationFilter, GeoRadius, CdsDeathFilter, Range
  }
}
```

Example (validate a payload with the Python `jsonschema` package):

```python
import json
from jsonschema import Draft202012Validator

schema = json.load(open("docs/cds-query.schema.json"))
Draft202012Validator(schema).validate(json.load(open("my-query.json")))
```

### What the JSON Schema does and does not cover

The generated schema captures **structure**: node kinds and their discriminator,
field types, `enum` values (domains, death status, operators), `minItems`
(non-empty `concepts` / `children`), required fields, defaults, and the
"at least one of `cohort` / `demographics`" root constraint.

It does **not** capture the cross-field rules enforced by Pydantic
`model_validator`s at parse time — these are runtime-only (see
[Validation summary](#validation-summary)):

- `Range`: at least one of `min`/`max`; `min <= max` when both set.
- `CdsClinicalRule`: `domain` and `domains` are mutually exclusive; `value_range`
  requires every effective domain to have a value column.
- `CdsGroup`: nesting depth ≤ `MAX_GROUP_DEPTH`.
- `AFTER` group: ≥2 children; clinical or nested-`AFTER` children only; each
  clinical child resolves to exactly one domain; no child sets `exclude`; the
  first child sets no `after_gap`.
- `CdsDemographics` and each sub-filter: non-empty.
- `CdsDeathFilter`: `status: "alive"` excludes `cause_concepts` / `age_at_death`.

A document that passes the JSON Schema but violates one of these is still
rejected by Bunny with a `ValueError`. Treat the JSON Schema as a first-pass
structural gate, and the Pydantic models as the authority.

---

## Validation summary

Enforced by Pydantic on `model_validate`:

- **Envelope** — at least one of `cohort` / `demographics`.
- **Range** — at least one bound; `min <= max` when both set.
- **Clinical rule** — `concepts` non-empty; `domain` / `domains` mutually
  exclusive; every named domain is a known one; `value_range` only where a value
  column exists.
- **Group** — `children` non-empty; nesting depth ≤ 20.
- **`AFTER` group** — ≥2 children; children are clinical rules or nested `AFTER`
  groups only; every clinical child resolves to exactly one domain; no child sets
  `exclude`; the first child sets no `after_gap`.
- **Demographics** — block non-empty; each `ConceptFilter` has non-empty
  `concepts`; `location` has at least one filter key and a valid `GeoRadius`
  (latitude −90..90, longitude −180..180, radius > 0); `death` has at least one
  key and does not pair `status: "alive"` with cause or age-at-death.
- **Removed node kind** — `kind: "demographic"` is rejected with a message
  directing the sender to the top-level block.

---

## Scope & extensibility

Current scope is **availability (cohort count)** queries. Distribution and
demographics-distribution queries continue to use the legacy solvers.

Not yet modelled (available on the legacy path): secondary modifiers
(condition-type provenance) and relative-time windows (`time: "1|:TIME:M"`).
Also deferred: an absolute `death_date` window on `CdsDeathFilter`, and a `unit`
on `after_gap` for months/years.

The node schema is additive — new leaf `kind`s or fields can be introduced
without breaking payloads written against this revision.
