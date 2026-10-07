from app.modules.named_entities.domain.kinds import EntityKind
from app.modules.named_entities.domain.models import (
    Mention,
    NamedEntity,
    NamedEntityExtraction,
)
from tests.evals.ner.metrics import (
    RunCosts,
    Span,
    ab_gate,
    grounding_rate,
    over_merge_rate,
    schema_validity_rate,
    span_scores,
    value_accuracy,
)
from tests.evals.ner.run import load_thresholds


def test_strict_needs_the_exact_boundary_and_partial_does_not():
    gold = [[Span(0, 13, "organization")]]
    predicted = [[Span(0, 4, "organization")]]
    scores = span_scores(gold, predicted, ["organization"])
    assert scores["strict"]["f1"] == 0.0
    assert scores["partial"]["f1"] == 0.5


def test_a_wrong_kind_fails_strict():
    scores = span_scores([[Span(0, 5, "person")]], [[Span(0, 5, "organization")]], ["person", "organization"])
    assert scores["strict"]["f1"] == 0.0


def test_touching_spans_do_not_overlap():
    scores = span_scores([[Span(0, 5, "date")]], [[Span(5, 9, "date")]], ["date"])
    assert scores["partial"]["f1"] == 0.0


def test_values_are_paired_optimally_not_greedily():
    # Gold A overlaps the second prediction most, so a greedy pass gives it
    # that one and leaves gold B with only a span it does not touch.
    gold = [[Span(0, 10, "currency", "money:USD:1"), Span(9, 13, "currency", "money:USD:2")]]
    predicted = [[Span(2, 12, "currency", "money:USD:2"), Span(0, 6, "currency", "money:USD:1")]]
    assert value_accuracy(gold, predicted)["accuracy"] == 1.0


def test_a_wrong_value_counts_against_accuracy():
    gold = [[Span(0, 5, "currency", "money:USD:1250")]]
    predicted = [[Span(0, 5, "currency", "money:USD:1250.00")]]
    assert value_accuracy(gold, predicted) == {"accuracy": 0.0, "correct": 0, "gold": 1}


def _extraction(surface: str, start: int, kind=EntityKind.ORGANIZATION, value=None) -> NamedEntityExtraction:
    mention = Mention(block_index=0, char_start=start, char_end=start + len(surface), surface=surface, extractor="agent")
    entity = NamedEntity(kind=kind, display_name=surface, norm_key=surface.casefold(), value=value, mentions=[mention])
    return NamedEntityExtraction(entities=[entity])


def test_a_mention_off_its_offsets_is_ungrounded():
    text = {0: "Acme signed"}
    assert grounding_rate([(text, _extraction("Acme", 0))])["rate"] == 1.0
    assert grounding_rate([(text, _extraction("Acme", 1))])["rate"] == 0.0


def test_a_value_kind_without_a_value_is_invalid():
    assert schema_validity_rate([_extraction("Acme", 0)]) == 1.0
    assert schema_validity_rate([_extraction("$5", 0, kind=EntityKind.CURRENCY)]) == 0.0


def test_over_merge_counts_nodes_holding_two_entities():
    gold = {"a1": "acme-robotics", "a2": "acme-foods", "g1": "globex", "g2": "globex"}
    predicted = {"a1": "n1", "a2": "n1", "g1": "n2", "g2": "n3"}
    assert over_merge_rate(gold, predicted) == round(1 / 3, 4)


def _arm(f1: float, usd: float | None, p95: float = 5.0) -> dict:
    return {
        "spans": {"strict": {"f1": f1}},
        "values": {"accuracy": 0.95},
        "grounding": {"rate": 1.0},
        "schema_validity": 1.0,
        "costs": {"p95_seconds": p95, "usd_per_doc": usd},
    }


def test_ab_gate_promotes_only_a_gain_worth_its_cost():
    gate = load_thresholds()["ab_gate"]
    assert ab_gate(_arm(0.90, 0.002), _arm(0.85, 0.001), gate) == []
    assert any("gain" in f for f in ab_gate(_arm(0.86, 0.002), _arm(0.85, 0.001), gate))
    assert any("cost ratio" in f for f in ab_gate(_arm(0.95, 0.010), _arm(0.85, 0.001), gate))
    assert any("p95" in f for f in ab_gate(_arm(0.95, 0.002, p95=120), _arm(0.85, 0.001), gate))
    assert any("unknown" in f for f in ab_gate(_arm(0.95, None), _arm(0.85, 0.001), gate))


def test_costs_report_percentiles_and_unpriced_models():
    costs = RunCosts()
    for seconds in (1.0, 2.0, 3.0, 10.0):
        costs.add(seconds, NamedEntityExtraction())
    summary = costs.summary("not-a-listed-model")
    assert (summary["p50_seconds"], summary["p95_seconds"]) == (2.0, 10.0)
    assert summary["usd"] is None
