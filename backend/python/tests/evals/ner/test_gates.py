"""CI gates for named-entity extraction. No model calls, no network."""

from __future__ import annotations

import json

import pytest

from tests.evals.ner.run import (
    RESOLUTION,
    SEMANTIC,
    evaluate,
    evaluate_resolution,
    load_golden,
    load_thresholds,
)

THRESHOLDS = load_thresholds()


@pytest.fixture(scope="module")
def rows():
    return load_golden()


def test_golden_spans_point_at_their_text(rows):
    for row in rows:
        for span in row["spans"]:
            assert 0 <= span["start"] < span["end"] <= len(row["text"]), row["id"]
        for item in row.get("model", []):
            assert set(item) == {"text", "kind", "normalized"}, row["id"]


async def test_deterministic_recognizers_meet_the_gate(rows):
    gate = THRESHOLDS["deterministic"]
    report = await evaluate(rows, "deterministic")
    assert report["spans"]["strict"]["f1"] >= gate["strict_f1"], report["spans"]
    assert report["spans"]["partial"]["f1"] >= gate["partial_f1"], report["spans"]
    weak = {k: v for k, v in report["spans"]["by_kind"].items() if v["strict_f1"] < gate["per_kind_strict_f1"]}
    assert not weak, weak
    assert report["values"]["accuracy"] >= gate["value_accuracy"], report["values"]
    assert report["secrets_stored"] == 0
    assert not set(report["spans"]["by_kind"]) & SEMANTIC


async def test_agent_loop_over_replayed_model_output_meets_the_gate(rows):
    gate = THRESHOLDS["replay"]
    report = await evaluate(rows, "replay")
    assert report["grounding"]["rate"] >= gate["grounding"], report["grounding"]
    assert report["schema_validity"] >= gate["schema_validity"]
    assert report["spans"]["strict"]["f1"] >= gate["strict_f1"], report["spans"]
    assert report["values"]["accuracy"] >= gate["value_accuracy"], report["values"]
    assert report["secrets_stored"] == 0
    assert report["termination_reasons"] == {"finish_ok": len(rows)}


async def test_resolution_never_over_merges():
    gate = THRESHOLDS["resolution"]
    report = await evaluate_resolution(json.loads(RESOLUTION.read_text()))
    assert report["over_merge_rate"] <= gate["max_over_merge_rate"], report
    assert report["bcubed"]["f1"] >= gate["bcubed_f1"], report
