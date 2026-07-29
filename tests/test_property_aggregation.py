from hypothesis import given, settings, strategies as st

from reason_assembly.protocol.judgment import aggregate_ballots


@settings(max_examples=200, deadline=5000)
@given(st.lists(st.sampled_from(["a", "b", "c", "unknown"]), max_size=100))
def test_aggregation_is_deterministic_and_bounded(ballots):
    candidates = ["a", "b", "c"]
    first = aggregate_ballots(ballots, candidates)
    assert first == aggregate_ballots(ballots, candidates)
    assert first is None or first in candidates
