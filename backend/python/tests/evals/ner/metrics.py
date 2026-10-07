"""Scores for named-entity extraction.

- Spans: nervaluate's scheme (SemEval-2013 task 9.1). ``strict`` needs the
  exact boundary and kind; ``partial`` gives half credit to an overlapping
  boundary and ignores the kind. Overall and per kind.
- Values: within one document and kind, gold and predicted spans are paired
  by the Hungarian algorithm on span overlap, so one long prediction cannot
  claim two gold spans. A pair is right when the predicted ``norm_key``
  equals the gold value; unpaired gold is wrong.
- Grounding: share of stored mentions whose surface is the block text at
  their offsets. Anything else would show the user text that is not there.
- Schema validity: share of extractions that round-trip through
  ``NamedEntityExtraction`` with a typed value on every value kind.
- Resolution: B-cubed, plus the over-merge rate: share of predicted nodes
  holding more than one gold entity. Over-merging is the error a user cannot
  undo from search results, so it is gated on its own.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nervaluate import Evaluator
from scipy.optimize import linear_sum_assignment

from app.modules.named_entities.domain.kinds import spec_for
from app.modules.named_entities.domain.models import NamedEntityExtraction
from tests.evals.cost import run_cost
from tests.evals.entity_resolution.metrics import bcubed

if TYPE_CHECKING:
    from collections.abc import Hashable, Mapping, Sequence


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: str
    value: str | None = None


def _nervaluate(spans: Sequence[Span]) -> list[dict]:
    # nervaluate ends are inclusive; ours are half-open.
    return [{"label": s.kind, "start": s.start, "end": s.end - 1} for s in spans]


def _prf(result) -> dict[str, float]:
    return {"precision": round(result.precision, 4), "recall": round(result.recall, 4), "f1": round(result.f1, 4)}


def span_scores(gold: Sequence[Sequence[Span]], predicted: Sequence[Sequence[Span]], kinds: Sequence[str]) -> dict:
    if len(gold) != len(predicted):
        raise ValueError("gold and predicted must cover the same documents")
    results = Evaluator(
        [_nervaluate(doc) for doc in gold], [_nervaluate(doc) for doc in predicted], tags=list(kinds),
    ).evaluate()
    overall = results["overall"]
    by_kind = {}
    for kind in kinds:
        scored = results["entities"].get(kind)
        if scored is None or (scored["strict"].possible == 0 and scored["strict"].actual == 0):
            continue
        strict = scored["strict"]
        by_kind[kind] = {
            "strict_f1": round(strict.f1, 4),
            "partial_f1": round(scored["partial"].f1, 4),
            "gold": strict.possible,
            "predicted": strict.actual,
        }
    return {"strict": _prf(overall["strict"]), "partial": _prf(overall["partial"]), "by_kind": by_kind}


def _overlap(left: Span, right: Span) -> float:
    inter = min(left.end, right.end) - max(left.start, right.start)
    if inter <= 0:
        return 0.0
    return inter / (max(left.end, right.end) - min(left.start, right.start))


def value_accuracy(gold: Sequence[Sequence[Span]], predicted: Sequence[Sequence[Span]]) -> dict:
    correct = total = 0
    for gold_doc, predicted_doc in zip(gold, predicted, strict=True):
        by_kind: dict[str, tuple[list[Span], list[Span]]] = defaultdict(lambda: ([], []))
        for span in gold_doc:
            if span.value is not None:
                by_kind[span.kind][0].append(span)
        for span in predicted_doc:
            by_kind[span.kind][1].append(span)
        for wanted, found in by_kind.values():
            total += len(wanted)
            if not wanted or not found:
                continue
            cost = [[-_overlap(w, f) for f in found] for w in wanted]
            for row, col in zip(*linear_sum_assignment(cost), strict=True):
                if cost[row][col] < 0 and found[col].value == wanted[row].value:
                    correct += 1
    return {"accuracy": round(correct / total, 4) if total else 1.0, "correct": correct, "gold": total}


def grounding_rate(runs: Sequence[tuple[Mapping[int, str], NamedEntityExtraction]]) -> dict:
    grounded = total = 0
    for blocks, extraction in runs:
        for entity in extraction.entities:
            for mention in entity.mentions:
                total += 1
                text = blocks.get(mention.block_index, "")
                grounded += text[mention.char_start:mention.char_end] == mention.surface
    return {"rate": round(grounded / total, 4) if total else 1.0, "grounded": grounded, "mentions": total}


def _valid(extraction: NamedEntityExtraction) -> bool:
    try:
        NamedEntityExtraction.model_validate_json(extraction.model_dump_json())
    except ValueError:
        return False
    for entity in extraction.entities:
        spec = spec_for(entity.kind)
        if spec is not None and not spec.resolvable and entity.value is None:
            return False
    return True


def schema_validity_rate(extractions: Sequence[NamedEntityExtraction]) -> float:
    if not extractions:
        return 1.0
    return round(sum(_valid(extraction) for extraction in extractions) / len(extractions), 4)


def over_merge_rate(gold: Mapping[Hashable, Hashable], predicted: Mapping[Hashable, Hashable]) -> float:
    members: dict[Hashable, set[Hashable]] = defaultdict(set)
    for mention, node in predicted.items():
        members[node].add(gold[mention])
    if not members:
        return 0.0
    return round(sum(len(entities) > 1 for entities in members.values()) / len(members), 4)


def resolution_scores(gold: Mapping[Hashable, Hashable], predicted: Mapping[Hashable, Hashable]) -> dict:
    return {"bcubed": bcubed(gold, predicted).as_dict(), "over_merge_rate": over_merge_rate(gold, predicted)}


@dataclass
class RunCosts:
    seconds: list[float] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    turns: int = 0
    tool_calls: int = 0

    def add(self, seconds: float, extraction: NamedEntityExtraction) -> None:
        self.seconds.append(seconds)
        self.tokens_in += extraction.stats.tokens_in
        self.tokens_out += extraction.stats.tokens_out
        self.turns += extraction.stats.turns
        self.tool_calls += extraction.stats.tool_calls

    def summary(self, model: str) -> dict:
        docs = len(self.seconds) or 1
        cost = run_cost(model, self.tokens_in, self.tokens_out)
        return {
            "docs": len(self.seconds),
            "p50_seconds": round(_percentile(self.seconds, 50), 3),
            "p95_seconds": round(_percentile(self.seconds, 95), 3),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "turns_per_doc": round(self.turns / docs, 2),
            "tool_calls_per_doc": round(self.tool_calls / docs, 2),
            "usd": cost.usd,
            "usd_per_doc": None if cost.usd is None else cost.usd / docs,
        }


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, math.ceil(pct / 100 * len(ordered)) - 1)
    return ordered[rank]


def ab_gate(agent: Mapping, single: Mapping, gate: Mapping) -> list[str]:
    """Why the agentic strategy should not replace single-call; empty means promote."""
    failures = []
    gain = agent["spans"]["strict"]["f1"] - single["spans"]["strict"]["f1"]
    if gain < gate["min_strict_f1_gain"]:
        failures.append(f"strict F1 gain {gain:+.3f} is below {gate['min_strict_f1_gain']:+.3f}")
    value_drop = single["values"]["accuracy"] - agent["values"]["accuracy"]
    if value_drop > gate["max_value_accuracy_drop"]:
        failures.append(f"value accuracy fell by {value_drop:.3f}")
    if agent["grounding"]["rate"] < gate["min_grounding"]:
        failures.append(f"grounding {agent['grounding']['rate']:.3f} is below {gate['min_grounding']}")
    if agent["schema_validity"] < gate["min_schema_validity"]:
        failures.append(f"schema validity {agent['schema_validity']:.3f} is below {gate['min_schema_validity']}")
    if agent["costs"]["p95_seconds"] > gate["max_p95_seconds"]:
        failures.append(f"p95 latency {agent['costs']['p95_seconds']}s is over {gate['max_p95_seconds']}s")
    agent_usd, single_usd = agent["costs"]["usd_per_doc"], single["costs"]["usd_per_doc"]
    if agent_usd is None or single_usd is None:
        failures.append("cost is unknown for one arm; price the model in tests/evals/cost.py")
    elif single_usd and agent_usd / single_usd > gate["max_cost_ratio"]:
        failures.append(f"cost ratio {agent_usd / single_usd:.2f}x is over {gate['max_cost_ratio']}x")
    return failures
