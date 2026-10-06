"""Truth replay never supplies feedback to the optimizer."""
from .contracts import Plan
from .schema import Truth
from .execution import simulate

def evaluate(actual, plan):
    if type(actual) is not Truth or type(plan) is not Plan:
        raise TypeError("evaluator requires Truth and Plan")
    return simulate(actual.state, plan.order)
