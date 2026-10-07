"""Run deterministic recognizers, then the selected model strategy."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.modules.named_entities.domain.config import NamedEntityBudgets
from app.modules.named_entities.domain.kinds import EntityKind, default_enabled_kinds
from app.modules.named_entities.domain.models import (
    ExtractionStats,
    NamedEntityExtraction,
    TerminationReason,
)
from app.modules.named_entities.guardrails import apply_caps
from app.modules.named_entities.mentions import RawMention
from app.modules.named_entities.merge import mentions_to_entities
from app.modules.named_entities.normalizers import normalize_value
from app.modules.named_entities.normalizers.dates import NormalizationContext
from app.modules.named_entities.recognizers.pattern import PatternRecognizer
from app.modules.named_entities.recognizers.values import ValueCandidateRecognizer
from app.modules.named_entities.strategies.agentic import AgenticExtractionStrategy
from app.modules.named_entities.strategies.base import (
    ExtractionContext,
    ExtractionDocument,
    StrategyResult,
)
from app.modules.named_entities.strategies.selector import (
    ExtractionStrategySelector,
    remember_tool_capability,
)
from app.modules.named_entities.strategies.single_call import (
    SingleCallExtractionStrategy,
)
from app.modules.named_entities.text import TextUnit, text_units_from_blocks
from app.telemetry.modules.named_entity_metrics import record_fallback

_MAX_RECORD_CHARS = 1_000_000
_COMPLETED = {"deterministic", "finish_ok", "single_call", "empty"}


@dataclass
class NamedEntityRequest:
    blocks: list = field(default_factory=list)
    org_id: str = ""
    record_name: str = ""
    record_type: str = ""
    reference_time_ms: int | None = None
    tz: str = "UTC"
    enabled: frozenset[EntityKind] | None = None
    budgets: NamedEntityBudgets | None = None
    llm: object | None = None
    transport: object | None = None
    model_name: str = "indexing"
    provider_name: str = "indexing"
    supports_tools: bool | None = None
    under_pressure: bool = False


def _cap_units(units: list[TextUnit]) -> list[TextUnit]:
    kept: list[TextUnit] = []
    total = 0
    for unit in units:
        if total >= _MAX_RECORD_CHARS:
            break
        room = _MAX_RECORD_CHARS - total
        text = unit.text if len(unit.text) <= room else unit.text[:room]
        kept.append(TextUnit(unit.block_index, unit.block_id, text))
        total += len(text)
    return kept


def _deterministic(units: list[TextUnit], enabled: frozenset[EntityKind], norm: NormalizationContext) -> list[RawMention]:
    found = PatternRecognizer(enabled).recognize(units) + ValueCandidateRecognizer(enabled).recognize(units)
    kept: list[RawMention] = []
    for mention in found:
        if mention.extractor == "value" and normalize_value(mention.kind, mention.surface, norm) is None:
            continue
        kept.append(mention)
    return kept


class NamedEntityExtractor:
    def __init__(
        self,
        *,
        agent: AgenticExtractionStrategy | None = None,
        single: SingleCallExtractionStrategy | None = None,
        selector: ExtractionStrategySelector | None = None,
    ) -> None:
        self._agent = agent or AgenticExtractionStrategy()
        self._single = single or SingleCallExtractionStrategy()
        self._selector = selector or ExtractionStrategySelector()

    async def extract(self, request: NamedEntityRequest) -> NamedEntityExtraction:
        units = _cap_units(text_units_from_blocks(request.blocks))
        enabled = request.enabled if request.enabled is not None else default_enabled_kinds()
        budgets = request.budgets or NamedEntityBudgets()
        norm = NormalizationContext(reference_time_ms=request.reference_time_ms, tz=request.tz or "UTC")
        if not units:
            return NamedEntityExtraction(
                strategy="deterministic",
                termination_reason="empty",
                status="SKIPPED",
            )
        seed = await asyncio.to_thread(_deterministic, units, enabled, norm)
        doc = ExtractionDocument(
            units=units,
            record_name=request.record_name,
            record_type=request.record_type,
            token_estimate=max(1, sum(len(unit.text) for unit in units) // 4),
        )
        ctx = ExtractionContext(
            org_id=request.org_id,
            norm=norm,
            enabled=enabled,
            budgets=budgets,
            llm=request.llm,
            transport=request.transport,
            model_name=request.model_name,
            provider_name=request.provider_name,
            supports_tools=request.supports_tools,
            under_pressure=request.under_pressure,
        )
        choice = self._selector.choose(doc, ctx)
        model = StrategyResult()
        strategy = "deterministic"
        reason: TerminationReason = "deterministic"
        if choice == "agent":
            model = await self._agent.extract(doc, seed, ctx)
            if model.tools_unsupported:
                remember_tool_capability(ctx.provider_name, ctx.model_name, False)
            if model.termination_reason == "llm_error" and not model.mentions:
                model = await self._single.extract(doc, seed, ctx)
                strategy, reason = _single_outcome(model)
                record_fallback(strategy, "llm_error")
            else:
                strategy = "agent"
                reason = model.termination_reason
        elif choice == "single_call":
            model = await self._single.extract(doc, seed, ctx)
            strategy, reason = _single_outcome(model)
        entities, dropped = apply_caps(mentions_to_entities(seed + model.mentions, norm), budgets, enabled)
        status = "COMPLETED" if reason in _COMPLETED else "PARTIAL"
        if reason == "llm_error":
            status = "PARTIAL" if entities else "FAILED"
        return NamedEntityExtraction(
            strategy=strategy,
            termination_reason=reason,
            status=status,
            entities=entities,
            stats=ExtractionStats(
                units=len(units),
                deterministic=len(seed),
                submitted=model.submitted,
                accepted=len(model.mentions),
                rejected=model.rejected,
                dropped_cap=dropped,
                turns=model.turns,
                tool_calls=model.tool_calls,
                tokens_in=model.tokens_in,
                tokens_out=model.tokens_out,
            ),
        )


def _single_outcome(model: StrategyResult) -> tuple[str, TerminationReason]:
    if model.termination_reason == "llm_error" and not model.mentions:
        return "deterministic", "deterministic"
    return "single_call", "single_call"


async def indexing_llm(config_service):
    """The indexing-role model, wrapped so each agent turn takes a slot."""
    from app.agents.agent_loop.langchain_transport import LangChainTransport
    from app.modules.named_entities.strategies.agent.transport import SlottedTransport
    from app.utils.llm import get_llm_for_role

    llm, config = await get_llm_for_role(config_service, "indexing", reasoning_effort="low")
    provider = str((config or {}).get("provider") or "indexing")
    configuration = (config or {}).get("configuration") or {}
    model = str(configuration.get("model") or (config or {}).get("modelKey") or "indexing")
    return llm, SlottedTransport(LangChainTransport(llm, model_name=model)), provider, model
