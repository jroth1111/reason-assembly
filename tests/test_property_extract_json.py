import json

from hypothesis import given, settings, strategies as st

from protocols import extract_json


@settings(max_examples=200, deadline=5000)
@given(st.text())
def test_extract_json_never_leaks_parser_exceptions(text):
    value = extract_json(text)
    if value is not None:
        json.dumps(value)


@settings(max_examples=100, deadline=5000)
@given(st.dictionaries(st.text(max_size=20), st.integers(), max_size=8))
def test_extract_json_round_trip(value):
    assert extract_json(json.dumps(value)) == value
