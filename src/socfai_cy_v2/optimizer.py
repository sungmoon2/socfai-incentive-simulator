"""Enumerate all 720 orders from an Observed value only."""
import itertools
import time
from .contracts import Observed, Plan, defaults, members, JOB_FIELDS, TRUCK_FIELDS
from .execution import simulate, objective
from .schema import validate_state, require

def validate_observed(observed):
    state = observed.state
    shared = members(observed.coalition_id)
    require(set(state) == {"horizon_minutes", "layout_visible", "yard", "jobs", "trucks"}, "observed metadata leaked")
    require(state["layout_visible"] == ("Y" in shared), "observed visibility mismatch")
    public = defaults()
    for section, id_keys, provider, keys, fallback in (
            ("jobs", ("job_id", "container_id"), "C", JOB_FIELDS, "cfs"),
            ("trucks", ("truck_id", "target_container_id"), "R", TRUCK_FIELDS, "carrier")):
        for item in state[section]:
            require(set(item) == set(id_keys + keys), "observed extra provider fields")
            if provider not in shared:
                require(all(item[k] == public[fallback][k] for k in keys), "hidden provider values leaked")
    if "Y" not in shared:
        require(state["yard"] == public["yard"], "hidden yard values leaked")
    else:
        require(set(state["yard"]) == {"yard_id", "stacks", "equipment_open_intervals", "move_service_minutes"}, "extra yard fields")
        for stack in state["yard"]["stacks"]:
            require(set(stack) == {"stack_id", "block_id", "bay", "row", "max_tier",
                                  "stack_bottom_to_top", "restricted"}, "extra stack fields")
    validate_state(state)
    return state

def optimize(observed):
    if type(observed) is not Observed:
        raise TypeError("optimizer requires Observed, never Truth or snapshot")
    state = validate_observed(observed)
    if len(state["jobs"]) != 6:
        raise ValueError("exact optimizer requires six jobs")
    started = time.perf_counter()
    best, count = None, 0
    ids = sorted(j["job_id"] for j in state["jobs"])
    for order in itertools.permutations(ids):
        count += 1
        result = simulate(state, order, checked=True)
        key = objective(result, order)
        if best is None or key < best:
            best = key
    return Plan(best[-1], best, count, "success" if best[0] == 0 else "infeasible",
                (time.perf_counter() - started) * 1000)
