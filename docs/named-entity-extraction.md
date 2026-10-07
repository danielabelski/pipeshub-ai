# Named entity extraction

Indexing can extract typed entities from a record and store them beside the
taxonomy graph. The Labs flag `ENABLE_NAMED_ENTITY_EXTRACTION` is off by
default. Turning it off does not delete nodes other records still mention;
it removes the mention edges for records indexed while the flag is off.

## What is stored

Entities live in the `namedEntities` collection (`NamedEntity` on Neo4j).
A record points at them with `mentionsEntity` (`MENTIONS_ENTITY`). These are
not `person`, `users`, or `organizations` nodes. A person mention requires
no email and is never written with `upsert_person_by_email`.

Value kinds carry a typed payload so a filter can use a range:

- dates are half-open epoch milliseconds (`startMs` inclusive, `endMs` exclusive)
- money is a decimal amount plus an ISO-4217 code
- quantities are stored in SI units
- percentages are fractions (`300 bps` is `0.03`)

A date with no year is marked ambiguous and has no range. A period covers
the whole period: `Q3 2026`, `FY2025`, `next week` (Monday to Monday),
`last month`, and `this year` are ranges anchored on the record's reference
time. Percent words (`percent`, `pct`, `bps`) and currency words (`dollars`,
`euros`, `yen`, `rupees`) are values too. `pounds` is not, because it is
also a weight.

Card numbers (Luhn-checked), IBANs (mod-97-checked), and national IDs (US
SSN, UK NINO, Aadhaar, PAN) are detected only so they are dropped. That
holds for model output as well: a card number submitted as a product is
rejected before it reaches the graph.

The graph key is `uuid5(orgId:kind:normKey)` in namespace
`c3a1e7b2-4d58-4f0a-9b6e-2a7d8c1f0e55`. Identity is the norm key. Two kinds
never share a node, and two orgs never share a node. Numeric keys are
canonical, so `$1,250` and `USD 1,250.00` are both `money:USD:1250` and land
on one node.

## How a record is extracted

The indexing pipeline runs extraction at the same time as document
classification. A failure is recorded on `entityExtractionStatus` and does
not change `indexingStatus`.

The deterministic recognizers (email, URL, dates, amounts, quantities) run
first on every record. The model then adds what they cannot find, picked in
this order:

1. A short read-only agent (`app/agent_loop_lib`) with per-record tools:
   read blocks, search the document, normalize a value, submit entities,
   and finish. The run is capped at 8 turns, 24 tool calls, and 120 seconds.
   Accepted entities are kept when a budget trips.
2. One structured call, when the model cannot call tools, the index LLM
   pool is full, or the agent fails before it accepts anything.
3. The deterministic results alone, when no semantic kind is enabled or
   the structured call also fails.

The model fills a flat JSON object: `text`, `block`, `kind`, `normalized`.
Typed values are built in code after the span is grounded in the block, so
a name the model invents is dropped.

`entityExtractionStatus` is `COMPLETED`, `PARTIAL` (a budget tripped or the
model failed after some entities were accepted), `SKIPPED` (no text), or
`FAILED`. A `FAILED` run keeps the record's existing mention edges, because
a transient model error must not erase the last good extraction.

The extraction service caps concurrent runs at
`MAX_CONCURRENT_ENTITY_EXTRACTIONS` (default 8) and returns 429 when full.

## Resolution

Semantic names are resolved in shadow mode. A possible merge is recorded
and is not applied, so the graph still gets a new node. Value kinds are
keyed only by their norm key. Organization keys drop legal suffixes, so
`Globex Corporation` and `Globex Corp.` share a node without resolution.
Production stays in shadow; apply mode is scored only by the offline eval
below.

## Retrieval

Search accepts `filters.entityFilters` (kinds, name, entity ids, a date
range, an amount, a quantity). The gateway keeps that object; retrieval
reads it before other filter keys are lowercased, intersects the hits with
records the caller can already access, and refuses a match set that hit
the graph cap. The name filter is a case-insensitive prefix. A filter that
matches more entities than the cap is reported as truncated and not
searched, rather than searched on an arbitrary subset.

Agent tools, when the flag is on:

- `search_entities` accepts `named_entity_kinds`
- `find_records_by_entity` accepts a named-entity id
- `search` can scope by those ids
- `find_records_by_value` filters by a typed value
- `fetch_record` lists entities on records it already returned

An entity is visible only when the caller can read a record that mentions it.
Records in the trash never come back from an entity filter; the entities on
a trashed record are still readable from the record itself.

## Configuration

`/services/namedEntities` overrides the timezone (default UTC), enabled
kinds, and budgets. Phone numbers and IP addresses are off unless enabled
there.

## Evaluation

The eval set is in `backend/python/tests/evals/ner/`: `golden.jsonl` holds
hand-labelled rows (surface, kind, normalized value, and for semantic rows
a recorded model reply), and `resolution.json` holds mentions with gold
clusters, including lookalikes that must stay apart (`Acme Robotics` and
`Acme Foods`, `Apple` the company and the product).

Metrics:

- span strict and partial F1, overall and per kind (`nervaluate`)
- value accuracy: predicted spans paired with gold by optimal assignment on
  overlap, then the normalized value must match exactly
- grounding rate: the stored surface equals the block text at its offsets
- schema validity: the extraction round-trips, and value kinds have a value
- secrets stored: must be zero
- B-cubed precision, recall, F1 and over-merge rate for resolution, run in
  apply mode against an in-memory graph
- latency p50/p95, tokens, turns, tool calls, and dollars per document

`test_gates.py` runs in CI with no model: the deterministic recognizers,
the agent loop over the recorded replies, and resolution. Floors are in
`thresholds.yaml`. Grounding and schema validity must be 1.0 and over-merge
must be 0.

Live runs need a model:

```bash
cd backend/python
python -m tests.evals.ner.run --strategy deterministic --out /tmp/ner.json
python -m tests.evals.ner.run --strategy agent --model openai:gpt-4.1-mini
python -m tests.evals.ner.run --strategy ab --model openai:gpt-4.1-mini
```

`ab` runs the agent and the single call on the same rows and applies
`ab_gate`: the agent must gain strict F1, lose no value accuracy, stay fully
grounded and valid, keep p95 under the ceiling, and cost at most the set
multiple of the single call. A model with no known price fails the gate.
