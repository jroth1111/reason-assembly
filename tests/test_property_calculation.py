from hypothesis import given, settings, strategies as st

from verification import calculate


@settings(max_examples=100, deadline=5000)
@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz_.'()", min_size=1, max_size=80))
def test_calculation_never_executes_names(expression):
    try:
        result = calculate(expression)
    except (RuntimeError, ValueError, SyntaxError, TypeError, ZeroDivisionError):
        return
    assert "module" not in result.lower()
