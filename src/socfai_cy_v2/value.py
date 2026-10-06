"""Exact three-player values and an independently gated virtual-points rule."""
import itertools
import math
from fractions import Fraction
from .contracts import COALITIONS, PLAYERS, members

def coalition_id(players):
    players = set(players)
    if not players <= set(PLAYERS):
        raise ValueError("unknown participant")
    return "".join(p for p in PLAYERS if p in players) or "NONE"

def exact_shapley(game):
    if set(game) != set(COALITIONS):
        raise ValueError("requires all eight coalitions")
    values = {c: Fraction(v) for c, v in game.items()}
    if values["NONE"] != 0:
        raise ValueError("V(NONE) must be zero")
    phi = {}
    for player in PLAYERS:
        others = [p for p in PLAYERS if p != player]
        contribution = Fraction(0)
        for size in range(3):
            weight = Fraction(math.factorial(size) * math.factorial(2 - size), 6)
            for subset in itertools.combinations(others, size):
                contribution += weight * (values[coalition_id((*subset, player))] - values[coalition_id(subset)])
        phi[player] = contribution
    if sum(phi.values()) != values["CYR"]:
        raise ValueError("Shapley efficiency failed")
    return phi

def rewards(mean_phi, mean_full_value, formal_run_set_valid):
    if type(formal_run_set_valid) is not bool:
        raise ValueError("formal validity must be boolean")
    if set(mean_phi) != set(PLAYERS):
        raise ValueError("requires three mean contributions")
    phi = {p: Fraction(v) for p, v in mean_phi.items()}
    full = Fraction(mean_full_value)
    if sum(phi.values()) != full:
        raise ValueError("mean Shapley efficiency failed")
    reason = ("formal_run_set_incomplete" if not formal_run_set_valid else
              "nonpositive_full_value" if full <= 0 else None)
    points = {p: 0 for p in PLAYERS}
    if reason is None:
        positive = {p: max(v, Fraction(0)) for p, v in phi.items()}
        total = sum(positive.values())
        quotas = {p: 1000 * positive[p] / total for p in PLAYERS}
        points = {p: quotas[p].numerator // quotas[p].denominator for p in PLAYERS}
        remaining = 1000 - sum(points.values())
        ranked = sorted(PLAYERS, key=lambda p: (-(quotas[p] - points[p]), p))
        for player in ranked[:remaining]:
            points[player] += 1
        if sum(points.values()) != 1000:
            raise ValueError("reward total failed")
    return dict(active=reason is None, inactive_reason=reason, points=points)

def analyze(rows):
    if len(rows) != 8 or {r["coalition_id"] for r in rows} != set(COALITIONS):
        raise ValueError("incomplete or duplicate coalition set")
    if len({r["run_key"] for r in rows}) != 8:
        raise ValueError("duplicate run_key")
    if len({(r["seed"], r["scenario_id"], r["snapshot_sha256"]) for r in rows}) != 1:
        raise ValueError("mixed snapshot identity")
    valid = all(r["status"] == r["optimizer_status"] == "success"
                and r["completed_jobs"] == 6 and r["failed_jobs"] == 0
                and r["permutations_evaluated"] == 720 for r in rows)
    for r in rows:
        for field in ("total_rehandle_moves", "total_moves", "completed_jobs", "failed_jobs",
                      "total_waiting_minutes", "total_travel_minutes", "total_operation_minutes"):
            if type(r[field]) is not int or r[field] < 0:
                raise ValueError(f"invalid metric {field}")
        if r["total_moves"] != r["total_rehandle_moves"] + r["completed_jobs"]:
            raise ValueError("move identity failed")
    if not valid:
        return dict(valid=False, status="invalid_seed", values=None, shapley_moves=None,
                    shapley_exact=None, full_value_moves=None, formal_run_set_valid=False,
                    reward=dict(active=False, inactive_reason="invalid_seed", points={p: 0 for p in PLAYERS}))
    costs = {r["coalition_id"]: r["total_rehandle_moves"] for r in rows}
    game = {c: costs["NONE"] - costs[c] for c in COALITIONS}
    phi = exact_shapley(game)
    # A smoke seed is not the complete formal 0..99 set. Never pay seed points.
    reward = rewards(phi, game["CYR"], False)
    return dict(valid=True, status="success", values=game,
                shapley_moves={p: float(v) for p, v in phi.items()},
                shapley_exact={p: str(v) for p, v in phi.items()},
                full_value_moves=game["CYR"], formal_run_set_valid=False, reward=reward)
