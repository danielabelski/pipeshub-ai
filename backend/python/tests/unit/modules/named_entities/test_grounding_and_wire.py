import json

from app.modules.named_entities.domain.kinds import EntityKind
from app.modules.named_entities.grounding import Grounder
from app.modules.named_entities.normalizers.dates import NormalizationContext
from app.modules.named_entities.strategies.agent.tools import (
    ExtractionSession,
    FinishTool,
    stage_wire_entity,
)
from app.modules.named_entities.text import TextUnit
from app.modules.named_entities.wire import EntitySubmission, WireEntity


def test_grounder_exact_casefold_fuzzy_and_reject():
    grounder = Grounder([TextUnit(0, "b", "Acme Corp signed the agreement")])
    exact = grounder.align(0, "Acme Corp")
    assert exact is not None and exact[3] == "exact"
    folded = grounder.align(0, "acme corp")
    assert folded is not None and folded[3] == "exact"
    fuzzy = grounder.align(0, "Acme Corps")
    assert fuzzy is not None and fuzzy[3] == "fuzzy"
    assert grounder.align(0, "totally missing phrase") is None
    assert "Acme" in grounder.nearest(0, "Acmee Corp")


def test_wire_schema_has_no_unions_or_optional_fields():
    schema = EntitySubmission.model_json_schema()
    blob = json.dumps(schema)
    assert "anyOf" not in blob
    assert "oneOf" not in blob
    entity = schema["$defs"]["WireEntity"]
    assert set(entity["required"]) == {"text", "block", "kind", "normalized"}
    WireEntity.model_validate({"text": "Acme", "block": 0, "kind": "organization", "normalized": ""})
    from anthropic import transform_schema
    from openai.lib._pydantic import to_strict_json_schema

    for converted in (to_strict_json_schema(EntitySubmission), transform_schema(schema)):
        text = json.dumps(converted)
        assert "anyOf" not in text
        assert "oneOf" not in text


def test_submit_is_idempotent_and_rejects_a_secret():
    session = ExtractionSession(
        [TextUnit(0, "b", "Acme owes 4111111111111111")],
        enabled=frozenset({EntityKind.ORGANIZATION}),
        norm_ctx=NormalizationContext(),
        seed_count=0,
    )
    item = WireEntity(text="Acme", block=0, kind="organization", normalized="")
    first = stage_wire_entity(session, 0, item)
    second = stage_wire_entity(session, 1, item)
    assert first["status"] == "accepted"
    assert second["status"] == "accepted"
    assert len(session.staging) == 1
    secret = WireEntity(text="4111111111111111", block=0, kind="organization", normalized="")
    assert stage_wire_entity(session, 2, secret)["status"] == "rejected"


async def test_finish_refuses_unread_blocks():
    units = [TextUnit(index, "b", "x") for index in range(3)]
    session = ExtractionSession(
        units,
        enabled=frozenset({EntityKind.ORGANIZATION}),
        norm_ctx=NormalizationContext(),
        seed_count=0,
    )
    session.read.add(0)
    refused = await FinishTool(session).execute(reason="done")
    assert refused.success is False
    session.read.update({1, 2})
    accepted = await FinishTool(session).execute(reason="done")
    assert accepted.success is True
