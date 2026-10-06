"""Seed-0 execution, run-key isolation and reproducible evidence packages."""

import copy
import csv
import hashlib
import json
import platform
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .data import COALITIONS, digest, generate, observed, truth, validate_config, validate_snapshot
from .evaluator import evaluate
from .optimizer import ALGORITHM_NAME, ALGORITHM_VERSION, optimize
from .value import aggregate, formal_valid, game, metrics, rewards


def run_key(snapshot, coalition):
    return digest([snapshot["scenario_id"], snapshot["snapshot_sha256"], ALGORITHM_VERSION,
                   coalition, snapshot["master_seed"]])


def run_seed(config, seed=0, order=COALITIONS, snapshot=None):
    validate_config(config)
    snapshot = generate(config, seed) if snapshot is None else copy.deepcopy(snapshot)
    validate_snapshot(snapshot, config["freshness_as_of"])
    if snapshot["master_seed"] != seed or snapshot["scenario_id"] != config["scenario_id"]:
        raise ValueError("snapshot identity mismatch")
    seen = set()
    rows, plans, evaluations = [], {}, {}
    for cid in order:
        key = run_key(snapshot, cid)
        if key in seen:
            raise ValueError("duplicate run_key; overwrite forbidden")
        seen.add(key)
        plan = optimize(observed(snapshot, cid, config["optimizer"]))
        actual = evaluate(truth(snapshot), plan.routes)
        status = plan.status
        if actual["failed_jobs"] or actual["unassigned_jobs"]:
            status = "invalid_assignment" if status != "timeout" else status
        rows.append(dict(run_id=key, scenario_id=snapshot["scenario_id"], snapshot_sha256=snapshot["snapshot_sha256"],
                         seed=seed, coalition_id=cid, algorithm_name=ALGORITHM_NAME, algorithm_version=ALGORITHM_VERSION,
                         **{k: v for k, v in actual.items() if k != "trace"}, runtime_ms=plan.runtime_ms, status=status,
                         optimizer_iterations=plan.iterations, optimizer_status=plan.status))
        plans[cid] = asdict(plan)
        evaluations[cid] = actual
    rows.sort(key=lambda r: COALITIONS.index(r["coalition_id"]))
    values = game(rows)
    for row in rows:
        if values is None:
            row.update(value_minutes=None, time_reduction_pct=None, efficiency_index=None,
                       time_reduction_reason="invalid_run_set", efficiency_index_reason="invalid_run_set")
        else:
            row.update(metrics(rows[0]["total_operation_minutes"], row["total_operation_minutes"]))
    # Scientific values are withheld, not silently replaced by zero, on invalid sets.
    mean_phi, full = aggregate([values]) if values is not None else (None, None)
    reward_rows = rewards(mean_phi, full, formal_valid(rows)) if values is not None else [
        dict(participant_id=p, raw_shapley_minutes=None, mean_shapley_minutes=None,
             value_credit_minutes=None, contribution_share_pct=None, full_value_minutes=None,
             reward_active=False, reward_inactive_reason="invalid_run_set", raw_reward_points=0,
             integer_reward_points=0, rounding_rank=None, reward_rule_version="1.0.0", calculation_version="exact-shapley-3-v1")
        for p in ("carrier", "cfs", "terminal")]
    summary = dict(scenario_id=snapshot["scenario_id"], snapshot_sha256=snapshot["snapshot_sha256"], seed=seed,
                   seed_list_sha256=snapshot["seed_list_sha256"], data_grade="synthetic",
                   formal_run_set_valid=False, full_value_minutes=full, shapley_minutes=mean_phi,
                   reward_active=False, reward_inactive_reason="formal_run_set_incomplete" if values is not None else "invalid_run_set",
                   valid=values is not None, formal_experiment_executed=False)
    return dict(snapshot=snapshot, coalition_results=rows, plans=plans, evaluations=evaluations,
                rewards=reward_rows, summary=summary)


def scientific(result):
    output = copy.deepcopy(result)
    for row in output["coalition_results"]:
        row.pop("runtime_ms", None)
    for plan in output["plans"].values():
        plan.pop("runtime_ms", None)
    return output


def json_bytes(obj):
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+"\n").encode()


COALITION_COLUMNS = "run_id scenario_id snapshot_sha256 seed coalition_id algorithm_name algorithm_version total_waiting_minutes total_travel_minutes total_operation_minutes failed_jobs unassigned_jobs time_reduction_pct efficiency_index runtime_ms status value_minutes time_reduction_reason efficiency_index_reason completed_jobs optimizer_iterations optimizer_status".split()
CONTRIBUTION_COLUMNS = "scenario_id aggregation_scope participant_id raw_shapley_minutes mean_shapley_minutes value_credit_minutes contribution_share_pct full_value_minutes calculation_version".split()
REWARD_COLUMNS = "scenario_id participant_id reward_active reward_inactive_reason raw_reward_points integer_reward_points rounding_rank reward_rule_version".split()


def write_package(destination, config, result, command):
    dest = Path(destination)
    # mkdir is atomic: prior runs and run packages cannot be overwritten.
    dest.mkdir(parents=True, exist_ok=False)
    now = datetime.now(timezone.utc).isoformat()
    jsons = {"config.json": config, "scenario.json": result["snapshot"],
             "input_manifest.json": {"snapshot_sha256": result["snapshot"]["snapshot_sha256"],
                                     "hash_exclusion": "snapshot_sha256 only", "seed_list_sha256": config["seed_list_sha256"],
                                     "scenario_file_sha256": hashlib.sha256(json_bytes(result["snapshot"])).hexdigest()},
             "summary.json": result["summary"],
             "validation_report.json": {"valid": result["summary"]["valid"],
                                        "shapley_efficiency_pass": result["summary"]["valid"],
                                        "formal_run_set_valid": False, "executed_at": now},
             "plans.json": result["plans"], "evaluation_trace.json": result["evaluations"],
             "scientific.json": scientific(result)}
    for name, obj in jsons.items():
        (dest/name).write_bytes(json_bytes(obj))
    enriched = [{**r, "scenario_id": result["snapshot"]["scenario_id"], "aggregation_scope": "smoke_seed_0"} for r in result["rewards"]]
    for name, columns, rows in [("coalition_results.csv", COALITION_COLUMNS, result["coalition_results"]),
                                ("contributions.csv", CONTRIBUTION_COLUMNS, enriched), ("rewards.csv", REWARD_COLUMNS, enriched)]:
        with (dest/name).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
            writer.writeheader(); writer.writerows(rows)
    (dest/"run.log").write_text(f"{now} seed=0 coalitions=8 valid={result['summary']['valid']}\n", encoding="utf-8")
    (dest/"environment.txt").write_text(f"Python {platform.python_version()}\n{platform.system()} {platform.release()} {platform.machine()}\n", encoding="utf-8")
    (dest/"commands.txt").write_text(command+"\n", encoding="utf-8")
    (dest/"SHA256SUMS").write_text("".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in sorted(dest.iterdir()) if p.is_file()), encoding="utf-8")


def main():
    import argparse
    import shlex
    parser = argparse.ArgumentParser(description="Dispatch seed-0 smoke run")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--reverse", action="store_true")
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    validate_config(cfg)
    if cfg["seeds"] != [0] or cfg.get("mode") != "smoke":
        parser.error("Use the formal reproduction mode for formal runs")
    if Path(args.output).exists():
        parser.error("output already exists; overwrite forbidden")
    result = run_seed(cfg, order=tuple(reversed(COALITIONS)) if args.reverse else COALITIONS)
    write_package(args.output, cfg, result, "PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -B -m socfai_incentive " + shlex.join(sys.argv[1:]))
    print(json.dumps(result["summary"], sort_keys=True))
    return 0 if result["summary"]["valid"] else 1
