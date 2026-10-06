"""Explicit truth-only evaluator boundary."""

from .data import Truth, validate_state
from .schedule import simulate


def evaluate(actual, routes):
    if type(actual) is not Truth:
        raise TypeError("evaluate requires Truth")
    validate_state(actual.state)
    return simulate(actual.state, routes)
