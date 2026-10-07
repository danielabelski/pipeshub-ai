import json
from types import SimpleNamespace

import pytest

from app.agent_loop_lib.core.exceptions import TransportError
from app.modules.named_entities.domain.config import NamedEntityBudgets
from app.modules.named_entities.domain.kinds import EntityKind
from app.modules.named_entities.extractor import (
    NamedEntityExtractor,
    NamedEntityRequest,
)
from app.modules.named_entities.normalizers.dates import NormalizationContext
from app.modules.named_entities.strategies import single_call
from app.modules.named_entities.strategies.base import (
    ExtractionContext,
    ExtractionDocument,
)
from app.modules.named_entities.strategies.selector import (
    ExtractionStrategySelector,
    clear_tool_capability,
    model_supports_tools,
)
from app.modules.named_entities.strategies.single_call import (
    SingleCallExtractionStrategy,
)
from app.modules.named_entities.text import TextUnit
from app.utils.concurrency import indexing_llm_slots_remaining
from tests.unit.agents.adapter.support.scripted_transport import (
    ScriptedStep,
    ScriptedTransport,
)

_ENABLED = frozenset({EntityKind.ORGANIZATION, EntityKind.PERSON})


@pytest.fixture(autouse=True)
def _fresh_caches():
    clear_tool_capability()
    single_call._STEP_DOWN.clear()
    yield
    clear_tool_capability()
    single_call._STEP_DOWN.clear()


def _ctx(**overrides) -> ExtractionContext:
    values = {
        "org_id": "org",
        "norm": NormalizationContext(),
        "enabled": _ENABLED,
        "budgets": NamedEntityBudgets(),
        "provider_name": "p",
        "model_name": "m",
    }
    values.update(overrides)
    return ExtractionContext(**values)


class _NoTools:
    def bind_tools(self, tools):
        raise NotImplementedError("tools unsupported")


def test_bind_tools_failure_selects_single_call_and_is_cached():
    doc = ExtractionDocument(units=[TextUnit(0, "b", "Acme hired Priya Shah")])
    ctx = _ctx(llm=_NoTools())
    assert ExtractionStrategySelector().choose(doc, ctx) == "single_call"
    assert model_supports_tools(None, "p", "m", None) is False


async def test_agent_bind_tools_error_falls_back_and_remembers():
    transport = ScriptedTransport([ScriptedStep(error=TransportError("bind_tools() failed for 5 tool(s)"))])

    async def complete(system, prompt):
        return json.dumps({"entities": [{"text": "Acme", "block": 0, "kind": "organization", "normalized": ""}]})

    extractor = NamedEntityExtractor(single=SingleCallExtractionStrategy(complete=complete))
    result = await extractor.extract(NamedEntityRequest(
        blocks=[SimpleNamespace(index=0, id="b", type="text", data="Acme hired Priya Shah")],
        org_id="org",
        enabled=_ENABLED,
        transport=transport,
        provider_name="p",
        model_name="m",
        supports_tools=True,
    ))
    assert result.strategy == "single_call"
    assert [entity.display_name for entity in result.entities] == ["Acme"]
    assert model_supports_tools(None, "p", "m", None) is False


async def test_single_call_accepts_items_one_by_one():
    payload = json.dumps({"entities": [
        {"text": "Acme", "block": 0, "kind": "organization", "normalized": ""},
        {"text": "Priya Shah", "block": "zero", "kind": "person", "normalized": ""},
        {"text": "Globex", "block": 0, "kind": "organization", "normalized": ""},
        {"text": "Priya Shah", "block": 0, "kind": "person", "normalized": ""},
    ]})

    async def complete(system, prompt):
        return payload

    doc = ExtractionDocument(units=[TextUnit(0, "b", "Acme hired Priya Shah")])
    result = await SingleCallExtractionStrategy(complete=complete).extract(doc, [], _ctx())
    assert sorted(m.surface for m in result.mentions) == ["Acme", "Priya Shah"]
    assert result.rejected == 1


async def test_step_down_is_remembered_per_model(monkeypatch):
    calls: list[str] = []

    async def strict(llm, prompt):
        calls.append("strict")
        return None

    async def function_call(llm, prompt):
        calls.append("tool")
        raise ValueError("schema rejected")

    class _Llm:
        async def ainvoke(self, messages):
            calls.append("prompt")
            remaining.append(indexing_llm_slots_remaining())
            return type("R", (), {"content": '{"entities": []}'})()

    remaining: list[int] = []
    baseline = indexing_llm_slots_remaining()
    monkeypatch.setattr(single_call, "_strict", strict)
    monkeypatch.setattr(single_call, "_function_call", function_call)
    strategy = SingleCallExtractionStrategy()
    ctx = _ctx(llm=_Llm())
    doc = ExtractionDocument(units=[TextUnit(0, "b", "Acme")])
    await strategy.extract(doc, [], ctx)
    await strategy.extract(doc, [], ctx)
    assert calls == ["strict", "tool", "prompt", "prompt"]
    assert single_call._STEP_DOWN[("p", "m")] == "prompt"
    assert remaining == [baseline - 1, baseline - 1]
