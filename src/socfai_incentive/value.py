"""Scientific minutes and policy-only reward points, kept separate."""

import math
import statistics
from fractions import Fraction

from .data import COALITIONS, PROVIDERS, SEEDS


def metrics(baseline, cost):
    return {
        "value_minutes": baseline - cost,
        "time_reduction_pct": 100 * (baseline - cost) / baseline if baseline else None,
        "time_reduction_reason": None if baseline else "zero_baseline",
        "efficiency_index": 100 * baseline / cost if cost else None,
        "efficiency_index_reason": None if cost else "zero_cost",
    }


def close(a, b):
    return abs(a-b) <= max(1e-9, 1e-9 * abs(b))


def shapley(values):
    if set(values) != set(COALITIONS) or not close(values["NONE"], 0):
        raise ValueError("complete normalized 8-coalition game required")
    result = {}
    for letter, participant in PROVIDERS.items():
        amount = Fraction(0)
        for cid in COALITIONS:
            members = "" if cid == "NONE" else cid
            if letter in members:
                continue
            union = "".join(c for c in "CTR" if c in members + letter)
            n = len(members)
            weight = Fraction(math.factorial(n)*math.factorial(2-n), math.factorial(3))
            amount += weight * (Fraction(str(values[union])) - Fraction(str(values[cid])))
        result[participant] = float(amount)
    if not close(sum(result.values()), values["CTR"]):
        raise ValueError("Shapley efficiency failed")
    return result


def valid_seed(rows):
    return (len(rows) == 8 and {r["coalition_id"] for r in rows} == set(COALITIONS)
            and len({r["snapshot_sha256"] for r in rows}) == 1
            and len({r["seed"] for r in rows}) == 1
            and all(r["status"] == "success" and r["failed_jobs"] == 0 and r["unassigned_jobs"] == 0 for r in rows))


def formal_valid(rows):
    if len(rows) != 800 or {r["seed"] for r in rows} != set(SEEDS):
        return False
    return all(valid_seed([r for r in rows if r["seed"] == s]) for s in SEEDS)


def game(rows):
    if not valid_seed(rows):
        return None
    costs = {r["coalition_id"]: r["total_operation_minutes"] for r in rows}
    return {c: costs["NONE"]-costs[c] for c in COALITIONS}


def aggregate(games):
    if not games:
        raise ValueError("no valid games")
    phis = [shapley(g) for g in games]
    means = {p: statistics.mean(phi[p] for phi in phis) for p in PROVIDERS.values()}
    full = statistics.mean(g["CTR"] for g in games)
    if not close(sum(means.values()), full):
        raise ValueError("mean efficiency failed")
    return means, full


def rewards(mean_phi, full_value, formal_run_set_valid):
    if set(mean_phi) != set(PROVIDERS.values()):
        raise ValueError("three participants required")
    if not all(math.isfinite(v) for v in [full_value, *mean_phi.values()]):
        raise ValueError("finite values required")
    credit = {p: max(Fraction(str(v)), 0) for p, v in mean_phi.items()}
    total = sum(credit.values())
    reason = ("formal_run_set_incomplete" if not formal_run_set_valid else
              "nonpositive_full_value" if full_value <= 0 else
              "no_positive_credit" if total == 0 else None)
    active = reason is None
    raw = {p: 1000 * credit[p] / total if active else Fraction(0) for p in sorted(credit)}
    integer = {p: int(v) for p, v in raw.items()}
    rank = sorted(raw, key=lambda p: (-(raw[p]-integer[p]), p))
    for p in rank[:1000-sum(integer.values()) if active else 0]:
        integer[p] += 1
    return [dict(participant_id=p, raw_shapley_minutes=mean_phi[p], mean_shapley_minutes=mean_phi[p],
                 value_credit_minutes=float(credit[p]), contribution_share_pct=float(100*credit[p]/total) if total else 0,
                 full_value_minutes=full_value, reward_active=active, reward_inactive_reason=reason,
                 raw_reward_points=float(raw[p]), integer_reward_points=integer[p], rounding_rank=rank.index(p)+1,
                 reward_rule_version="1.0.0", calculation_version="exact-shapley-3-v1") for p in sorted(raw)]
