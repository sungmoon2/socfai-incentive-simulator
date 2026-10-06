"""Observed-only deterministic best insertion and relocate/swap local search."""

import copy
import time
from dataclasses import dataclass

from .data import Observed, validate_state
from .schedule import simulate

ALGORITHM_NAME = "greedy-insertion-local-search"
ALGORITHM_VERSION = "1.0.0"


@dataclass
class Plan:
    routes: dict
    status: str
    iterations: int
    runtime_ms: float
    estimated_objective: int


def optimize(observed):
    """Only Observed is accepted; no global truth/snapshot repository exists."""
    if type(observed) is not Observed:
        raise TypeError("optimize requires Observed")
    state = observed.state
    validate_state(state)
    opts = observed.options
    started = time.perf_counter()
    routes = {t["truck_id"]: [] for t in sorted(state["trucks"], key=lambda x: x["truck_id"]) if t["enabled"]}
    iterations = 0

    def elapsed():
        return time.perf_counter() - started

    def score(candidate):
        k = simulate(state, candidate)
        # Feasibility is lexicographic, never an invented time penalty.
        return (k["failed_jobs"], k["unassigned_jobs"], k["total_operation_minutes"])

    def finish(status):
        return Plan(routes, status, iterations, elapsed() * 1000, score(routes)[2])

    if opts["timeout_seconds"] <= 0:
        return finish("timeout")
    for job in sorted(state["jobs"], key=lambda j: (j["window_end"], -j["priority"], j["ready_at"], j["job_id"])):
        best = None
        for tid in routes:
            for pos in range(len(routes[tid]) + 1):
                if elapsed() >= opts["timeout_seconds"]:
                    return finish("timeout")
                candidate = copy.deepcopy(routes)
                candidate[tid].insert(pos, job["job_id"])
                key = (score(candidate), tid, pos, job["job_id"])
                if best is None or key < best[0]:
                    best = (key, candidate)
        if best:
            routes = best[1]
    if not routes and state["jobs"]:
        return finish("unassigned")
    for iteration in range(opts["max_iterations"]):
        current = score(routes)
        best = None
        tids = list(routes)
        # Ordered operation key is a fully documented secondary tie-break.
        for src in tids:
            for pos, jid in enumerate(routes[src]):
                removed = copy.deepcopy(routes)
                removed[src].pop(pos)
                for dst in tids:
                    for destpos in range(len(removed[dst]) + 1):
                        if elapsed() >= opts["timeout_seconds"]:
                            return finish("timeout")
                        candidate = copy.deepcopy(removed)
                        candidate[dst].insert(destpos, jid)
                        value = score(candidate)
                        key = (value, "relocate", src, pos, dst, destpos, jid)
                        if value < current and (best is None or key < best[0]):
                            best = (key, candidate)
        positions = [(t, i) for t in tids for i in range(len(routes[t]))]
        for a, (ta, ia) in enumerate(positions):
            for tb, ib in positions[a+1:]:
                if elapsed() >= opts["timeout_seconds"]:
                    return finish("timeout")
                candidate = copy.deepcopy(routes)
                candidate[ta][ia], candidate[tb][ib] = candidate[tb][ib], candidate[ta][ia]
                value = score(candidate)
                key = (value, "swap", ta, ia, tb, ib, "")
                if value < current and (best is None or key < best[0]):
                    best = (key, candidate)
        iterations = iteration + 1
        if best is None:
            return finish("success")
        routes = best[1]
    return finish("iteration_limit")
