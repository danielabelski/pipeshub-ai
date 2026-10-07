"""Named-entity extraction evaluation.

Runs ``golden.jsonl`` through the production ``NamedEntityExtractor`` and
``resolution.json`` through the production ``NamedEntityResolver``, then
scores them with ``metrics.py``.

Strategies:

- ``deterministic``: recognizers and normalizers only; scored on value and
  pattern kinds. Free, and the CI gate.
- ``replay``: the real agent loop over a scripted model that submits each
  row's ``model`` items (one invented name, one card number among them).
  Free; checks that grounding and the guardrails hold whatever a model says.
- ``agent`` / ``single_call``: a real model (``--model openai:<name>``,
  needs ``OPENAI_API_KEY``). Costs money.
- ``ab``: both live arms on the same rows, then the gate in
  ``thresholds.yaml`` decides whether the agent is worth its cost.

Resolution is scored as APPLY would merge, so the over-merge rate is known
before anyone switches it on; production stays in SHADOW.

  cd backend/python
  python -m tests.evals.ner.run --strategy deterministic
  python -m tests.evals.ner.run --strategy ab --model openai:gpt-4.1-mini --out ner.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.agent_loop_lib.core.messages import AssistantMessage, ToolCall
from app.agent_loop_lib.core.responses import StopReason
from app.models.blocks import Block, BlockType
from app.modules.entity_resolution.models import ResolutionMode
from app.modules.named_entities.domain.kinds import (
    EntityKind,
    default_enabled_kinds,
    semantic_kinds,
)
from app.modules.named_entities.domain.models import NamedEntityExtraction
from app.modules.named_entities.extractor import (
    NamedEntityExtractor,
    NamedEntityRequest,
)
from app.modules.named_entities.mentions import RawMention
from app.modules.named_entities.merge import mentions_to_entities
from app.modules.named_entities.normalizers.dates import NormalizationContext
from app.modules.named_entities.resolution import NamedEntityResolver
from app.modules.named_entities.strategies.agent.transport import SlottedTransport
from tests.evals.ner.metrics import (
    RunCosts,
    Span,
    ab_gate,
    grounding_rate,
    resolution_scores,
    schema_validity_rate,
    span_scores,
    value_accuracy,
)

HERE = Path(__file__).parent
GOLDEN = HERE / "golden.jsonl"
RESOLUTION = HERE / "resolution.json"
THRESHOLDS = HERE / "thresholds.yaml"
# Tuesday. Relative phrases in the golden set are written against it.
REFERENCE_MS = int(datetime(2026, 3, 10, tzinfo=UTC).timestamp() * 1000)
ENABLED = default_enabled_kinds()
SEMANTIC = frozenset(kind.value for kind in semantic_kinds(ENABLED))
KINDS = sorted(kind.value for kind in ENABLED)


def load_golden(path: Path = GOLDEN) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_thresholds(path: Path = THRESHOLDS) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


class _Fixed:
    def __init__(self, choice: str) -> None:
        self._choice = choice

    def choose(self, doc, ctx) -> str:
        return self._choice


def _replay_transport(row: dict[str, Any]):
    from tests.unit.agents.adapter.support.scripted_transport import (
        ScriptedStep,
        ScriptedTransport,
    )

    def call(name: str, arguments: dict, call_id: str) -> ScriptedStep:
        return ScriptedStep(
            message=AssistantMessage(content="", tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)]),
            stop_reason=StopReason.TOOL_USE,
        )

    steps = []
    if row.get("model"):
        entities = [{**item, "block": 0} for item in row["model"]]
        steps.append(call("submit_entities", {"entities": entities}, "submit"))
    steps.append(call("finish", {"reason": "done"}, "finish"))
    return SlottedTransport(ScriptedTransport(steps))


def _live_model(spec: str):
    provider, _, name = spec.partition(":")
    if provider != "openai" or not name:
        raise SystemExit(f"unknown model {spec!r}; use openai:<model>")
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set")
    from langchain_openai import ChatOpenAI

    from app.agents.agent_loop.langchain_transport import LangChainTransport

    llm = ChatOpenAI(model=name, temperature=0)
    return llm, SlottedTransport(LangChainTransport(llm, model_name=name)), name


async def _extract(row: dict[str, Any], strategy: str, live) -> NamedEntityExtraction:
    llm = transport = None
    model = "eval"
    choice = {"replay": "agent"}.get(strategy, strategy)
    if strategy == "replay":
        transport = _replay_transport(row)
    elif strategy in ("agent", "single_call"):
        llm, transport, model = live
    block = Block(type=BlockType.TEXT, data=row["text"], index=0)
    request = NamedEntityRequest(
        blocks=[block], org_id="org-ner-eval", record_name=row["id"], reference_time_ms=REFERENCE_MS,
        enabled=ENABLED, llm=llm, transport=transport, provider_name="eval", model_name=model,
        supports_tools=strategy != "single_call",
    )
    return await NamedEntityExtractor(selector=_Fixed(choice)).extract(request)


def _predicted_spans(extraction: NamedEntityExtraction) -> list[Span]:
    return [
        Span(m.char_start, m.char_end, entity.kind.value, entity.norm_key)
        for entity in extraction.entities
        for m in entity.mentions
        if m.block_index == 0
    ]


async def evaluate(rows: list[dict[str, Any]], strategy: str, *, live=None) -> dict[str, Any]:
    scored_kinds = [k for k in KINDS if k not in SEMANTIC] if strategy == "deterministic" else KINDS
    gold_docs, predicted_docs, runs, extractions = [], [], [], []
    costs = RunCosts()
    reasons: dict[str, int] = {}
    leaked = 0
    for row in rows:
        started = time.perf_counter()
        extraction = await _extract(row, strategy, live)
        costs.add(time.perf_counter() - started, extraction)
        reasons[extraction.termination_reason] = reasons.get(extraction.termination_reason, 0) + 1
        gold_docs.append([Span(**s) for s in row["spans"] if s["kind"] in scored_kinds])
        predicted_docs.append([s for s in _predicted_spans(extraction) if s.kind in scored_kinds])
        runs.append(({0: row["text"]}, extraction))
        extractions.append(extraction)
        if row.get("secret") and extraction.entities:
            leaked += 1
        leaked += any("4111" in m.surface for e in extraction.entities for m in e.mentions)
    return {
        "strategy": strategy,
        "rows": len(rows),
        "spans": span_scores(gold_docs, predicted_docs, scored_kinds),
        "values": value_accuracy(gold_docs, predicted_docs),
        "grounding": grounding_rate(runs),
        "schema_validity": schema_validity_rate(extractions),
        "secrets_stored": leaked,
        "termination_reasons": reasons,
        "costs": costs.summary(live[2] if live else "none"),
    }


class _MemoryGraph:
    """Nodes as the writer would store them, for one eval run."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}

    async def find_named_entities(self, org_id: str, kind: str, keys: list[str]) -> list[dict]:
        wanted = set(keys)
        return [
            node for node in self.nodes.values()
            if node["kind"] == kind and (node["normKey"] in wanted or wanted & set(node["normalizedAliases"]))
        ]

    async def candidates(self, org_id: str, kind: str, name: str) -> list[dict]:
        return [
            {"id": key, "name": node["name"], "orgId": org_id, "kind": kind}
            for key, node in self.nodes.items() if node["kind"] == kind
        ]

    def store(self, graph_key: str, entity) -> None:
        node = self.nodes.setdefault(graph_key, {
            "_key": graph_key, "kind": entity.kind.value, "name": entity.display_name,
            "normKey": entity.norm_key, "normalizedAliases": [],
        })
        if entity.norm_key != node["normKey"] and entity.norm_key not in node["normalizedAliases"]:
            node["normalizedAliases"].append(entity.norm_key)


async def evaluate_resolution(dataset: dict[str, Any]) -> dict[str, Any]:
    graph = _MemoryGraph()
    resolver = NamedEntityResolver(graph, vector_search=graph.candidates, mode=ResolutionMode.APPLY)
    org = dataset["org"]
    gold: dict[tuple[str, int], str] = {}
    predicted: dict[tuple[str, int], str] = {}
    for record in dataset["records"]:
        for index, item in enumerate(record["entities"]):
            raw = RawMention(
                kind=EntityKind(item["kind"]), surface=item["name"], block_index=0, block_id="b",
                char_start=0, char_end=len(item["name"]), extractor="agent",
            )
            (entity,) = mentions_to_entities([raw], NormalizationContext())
            (resolved,) = await resolver.resolve(org, [entity])
            graph.store(resolved.graph_key, entity)
            gold[(record["id"], index)] = item["gold"]
            predicted[(record["id"], index)] = resolved.graph_key
    return {
        "mentions": len(gold),
        "gold_entities": len(set(gold.values())),
        "predicted_nodes": len(set(predicted.values())),
        **resolution_scores(gold, predicted),
    }


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    rows = load_golden(args.golden)
    report: dict[str, Any] = {"resolution": await evaluate_resolution(json.loads(args.resolution.read_text()))}
    if args.strategy in ("deterministic", "replay"):
        report["extraction"] = await evaluate(rows, args.strategy)
        return report
    live = _live_model(args.model)
    if args.strategy != "ab":
        report["extraction"] = await evaluate(rows, args.strategy, live=live)
        return report
    agent = await evaluate(rows, "agent", live=live)
    single = await evaluate(rows, "single_call", live=live)
    failures = ab_gate(agent, single, load_thresholds()["ab_gate"])
    report.update(agent=agent, single_call=single, ab_gate={"promote_agent": not failures, "failures": failures})
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--strategy", default="deterministic",
                        choices=["deterministic", "replay", "agent", "single_call", "ab"])
    parser.add_argument("--model", default="", help="openai:<model> for the live strategies")
    parser.add_argument("--golden", type=Path, default=GOLDEN)
    parser.add_argument("--resolution", type=Path, default=RESOLUTION)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = asyncio.run(_main(args))
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
