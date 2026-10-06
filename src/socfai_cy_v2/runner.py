"""Seed-0 smoke, exclusive outputs, raw journals and stable scientific bytes."""
import argparse
import copy
import csv
import hashlib
import io
import json
import sys
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from .contracts import (AS_OF, COALITIONS, PARTICIPANTS, PLAYERS, VERSIONS, canonical, members)
from .generator import generate
from .schema import validate_snapshot, truth
from .observations import observe
from .optimizer import optimize
from .evaluator import evaluate
from .value import analyze

def expected_config():
    return {k: VERSIONS[k] for k in ("schema_version", "generator_version",
            "public_default_version", "algorithm_version", "calculation_version", "reward_rule_version")} | {
            "mode": "smoke", "seeds": [0], "freshness_as_of": AS_OF}

def validate_config(config):
    if canonical(config) != canonical(expected_config()):
        raise ValueError("only frozen seed-0 smoke config is authorized; formal execution disabled")

def run_key(snapshot, coalition):
    members(coalition)
    fields = ("schema_version", "scenario_id", "snapshot_sha256", "master_seed")
    values = [str(snapshot[k]) for k in fields] + [coalition] + [
        snapshot[k] for k in ("algorithm_version", "calculation_version", "reward_rule_version")]
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()

def run_seed(config, order=COALITIONS, snapshot=None, journal=None, on_completed=None):
    validate_config(config)
    order = tuple(order)
    if len(order) != 8 or set(order) != set(COALITIONS):
        raise ValueError("duplicate run_key or incomplete coalition order")
    emit = journal if journal is not None else lambda event: None
    generation = []
    if snapshot is None:
        try:
            snapshot = generate(0, generation)
        finally:
            for event in generation:
                emit(event)
    else:
        snapshot = copy.deepcopy(snapshot)
    validate_snapshot(snapshot, config["freshness_as_of"])
    if snapshot["master_seed"] != 0:
        raise ValueError("smoke snapshot seed mismatch")
    actual = truth(snapshot, config["freshness_as_of"])
    emit(dict(event="snapshot_validated", snapshot_sha256=snapshot["snapshot_sha256"]))
    seen, rows, plans, evaluations = set(), [], [], []
    for coalition in order:
        key = run_key(snapshot, coalition)
        if key in seen:
            raise ValueError("duplicate run_key")
        seen.add(key)
        observed = observe(snapshot, coalition, config["freshness_as_of"])
        emit(dict(event="optimizer_call", coalition_id=coalition, run_key=key))
        plan = optimize(observed)
        emit(dict(event="truth_evaluation_call", coalition_id=coalition, run_key=key))
        result = evaluate(actual, plan)
        metrics = {k: result[k] for k in ("total_rehandle_moves", "total_moves", "completed_jobs",
                   "failed_jobs", "total_waiting_minutes", "total_travel_minutes", "total_operation_minutes", "status")}
        row = dict(run_id=key, run_key=key, scenario_id=snapshot["scenario_id"],
                   snapshot_sha256=snapshot["snapshot_sha256"], seed=0, coalition_id=coalition,
                   participants=list(members(coalition)), algorithm_name=VERSIONS["algorithm_name"],
                   algorithm_version=VERSIONS["algorithm_version"], **metrics, runtime_ms=plan.runtime_ms,
                   optimizer_status=plan.status, permutations_evaluated=plan.permutations_evaluated)
        p = dict(coalition_id=coalition, run_key=key, **asdict(plan))
        evaluation = dict(coalition_id=coalition, run_key=key, **result)
        rows.append(row)
        plans.append(p)
        evaluations.append(evaluation)
        if on_completed:
            on_completed(row, p, evaluation)
        emit(dict(event="run_complete", coalition_id=coalition, run_key=key,
                  status=result["status"], permutations_evaluated=plan.permutations_evaluated))
    rows.sort(key=lambda r: COALITIONS.index(r["coalition_id"]))
    plans.sort(key=lambda r: COALITIONS.index(r["coalition_id"]))
    evaluations.sort(key=lambda r: COALITIONS.index(r["coalition_id"]))
    summary = analyze(rows)
    for row in rows:
        row["value_moves"] = summary["values"][row["coalition_id"]] if summary["valid"] else None
    summary.update(mode="smoke", seed_count=1, coalition_count=8, data_grade=snapshot["data_grade"],
                   snapshot_sha256=snapshot["snapshot_sha256"], reward_rule_version=VERSIONS["reward_rule_version"])
    contributions = [dict(participant_id=p, participant_name=PARTICIPANTS[p],
                          shapley_moves=summary["shapley_moves"][p] if summary["valid"] else None,
                          shapley_exact=summary["shapley_exact"][p] if summary["valid"] else None,
                          unit="move", aggregation="single_seed_smoke") for p in PLAYERS]
    rewards = [dict(participant_id=p, active=summary["reward"]["active"],
                    integer_reward_points=summary["reward"]["points"][p],
                    inactive_reason=summary["reward"]["inactive_reason"]) for p in PLAYERS]
    validation = dict(valid=summary["valid"], status=summary["status"], single_snapshot=True,
                      unique_run_keys=len(seen), all_720=all(r["permutations_evaluated"] == 720 for r in rows),
                      shapley_efficiency=summary["valid"], formal_run_set_valid=False,
                      issues=[] if summary["valid"] else ["invalid_seed: at least one coalition infeasible"])
    return dict(input_snapshot=snapshot, config=copy.deepcopy(config), generation_journal=generation,
                coalition_results=rows, plans=plans, evaluations=evaluations, contributions=contributions,
                rewards=rewards, summary=summary, validation_report=validation)

def scientific(result):
    output = copy.deepcopy(result)
    for field in ("generated_at", "observed_at", "valid_until"):
        output["input_snapshot"].pop(field, None)
    output["config"].pop("freshness_as_of", None)
    for section in ("coalition_results", "plans"):
        for row in output[section]:
            row.pop("runtime_ms", None)
    return output

def write_json(path, value):
    with path.open("xb") as stream:
        stream.write(canonical(value))

def write_csv(path, rows):
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, separators=(",", ":"), ensure_ascii=False)
                             if isinstance(v, (dict, list, tuple)) else v for k, v in row.items()})

def execute(config, destination, reverse=False):
    validate_config(config)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    write_json(destination / "config.json", config)
    def emit(event):
        with (destination / "run.log").open("ab") as f:
            f.write(canonical(dict(event, utc=datetime.now(timezone.utc).isoformat())))
    def completed(row, plan, evaluation):
        for name, value in (("completed_runs.jsonl", row), ("plans.jsonl", plan), ("evaluation_traces.jsonl", evaluation)):
            with (destination / name).open("ab") as f:
                f.write(canonical(value))
    try:
        emit(dict(event="start", mode="smoke", seed=0, reverse=bool(reverse)))
        result = run_seed(config, tuple(reversed(COALITIONS)) if reverse else COALITIONS,
                          journal=emit, on_completed=completed)
        for name, value in (("input_snapshot", result["input_snapshot"]),
                            ("public_defaults", result["input_snapshot"]["public_defaults"]),
                            ("summary", result["summary"]), ("validation_report", result["validation_report"]),
                            ("scientific", scientific(result)), ("generation_journal", result["generation_journal"])):
            write_json(destination / (name + ".json"), value)
        for name in ("coalition_results", "contributions", "rewards"):
            write_csv(destination / (name + ".csv"), result[name])
        emit(dict(event="complete", status=result["summary"]["status"]))
        lines = []
        for file in sorted(destination.iterdir()):
            if file.is_file():
                lines.append(hashlib.sha256(file.read_bytes()).hexdigest() + "  " + file.name)
        with (destination / "SHA256SUMS.txt").open("x", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return result
    except Exception:
        with (destination / "failure.txt").open("x", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        emit(dict(event="aborted", status="failed"))
        raise

def main():
    parser = argparse.ArgumentParser(description="Synthetic CY v2 seed-0 smoke only")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--reverse", action="store_true")
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_bytes())
        result = execute(config, args.output, args.reverse)
        print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
        return 0 if result["summary"]["valid"] else 1
    except Exception:
        traceback.print_exc()
        return 1
