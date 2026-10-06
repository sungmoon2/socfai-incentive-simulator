"""Frozen-source CY formal runner, fail-closed analysis and replay."""
import argparse
import copy
import csv
import hashlib
import json
import math
import re
import subprocess
import sys
import traceback
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from .contracts import AS_OF, COALITIONS, PLAYERS, PARTICIPANTS, VERSIONS, canonical, members
from .generator import generate, GenerationFailed
from .schema import validate_snapshot, truth
from .observations import observe
from .optimizer import optimize
from .evaluator import evaluate
from .runner import run_key, write_json
from .value import exact_shapley, rewards
from .formal_statistics import describe, paired_bootstrap

BASELINE = "211bc608a5eaf886cba73b92dc44de1cf65cb9f9"
CONFIG_SHA256 = "7346bd73001b477765534604f9f09c827e7afff1087fa87bfd10e035e8166184"
METRICS = ("total_rehandle_moves", "total_moves", "completed_jobs", "failed_jobs",
           "total_waiting_minutes", "total_travel_minutes", "total_operation_minutes")
ROW_FIELDS = ("run_id", "run_key", "scenario_id", "snapshot_sha256", "seed", "coalition_id",
              "participants", "algorithm_name", "algorithm_version") + METRICS + (
              "runtime_ms", "status", "optimizer_status", "permutations_evaluated", "value_moves")
SEED_FIELDS = ("seed", "participant_id", "participant_name", "shapley_moves", "shapley_exact", "diagnostic_only")
AGG_FIELDS = ("participant_id", "participant_name", "n", "mean_shapley_moves", "mean_shapley_exact",
              "ci_lower", "ci_upper", "available")
REWARD_FIELDS = ("participant_id", "active", "integer_reward_points", "inactive_reason")
ROOT = Path(__file__).resolve().parents[2]

def expected_config():
    return dict(schema_version="cy-v2.0", mode="formal", seeds=list(range(100)),
                coalitions=list(COALITIONS), freshness_as_of=AS_OF, bootstrap_seed=20260928,
                bootstrap_replicates=10000, **{key: VERSIONS[key] for key in (
                    "generator_version", "public_default_version", "algorithm_version",
                    "calculation_version", "reward_rule_version")})

def validate_config(config):
    if canonical(config) != canonical(expected_config()):
        raise ValueError("formal config is frozen")

def validate_raw_config(raw):
    if hashlib.sha256(raw).hexdigest() != CONFIG_SHA256:
        raise ValueError("formal config raw-byte pin mismatch")
    config = json.loads(raw)
    validate_config(config)
    return config

def collect(config, order=COALITIONS, *, fixture_seeds=None, emit=None, checkpoint=None):
    validate_config(config)
    order = tuple(order)
    if len(order) != 8 or set(order) != set(COALITIONS):
        raise ValueError("duplicate or missing coalition")
    seeds = config["seeds"] if fixture_seeds is None else list(fixture_seeds)
    if len(seeds) != len(set(seeds)) or any(type(s) is not int or s not in range(100) for s in seeds):
        raise ValueError("duplicate or invalid seed")
    emit = emit or (lambda event: None)
    checkpoint = checkpoint or (lambda kind, value: None)
    result = dict(scenarios=[], generation_journal=[], plans=[], evaluations=[], coalition_results=[],
                  failures=[], generator_calls=0, optimizer_calls=0, truth_evaluation_calls=0)
    seen = set()
    for seed in seeds:
        journal = []
        result["generator_calls"] += 1
        emit(dict(event="generator_call", seed=seed))
        try:
            snapshot = generate(seed, journal)
        except GenerationFailed as exc:
            failure = dict(seed=seed, status="generation_failed", reason=str(exc))
            result["failures"].append(failure)
            checkpoint("failure", failure)
            emit(dict(event="seed_failed", **failure))
            continue
        finally:
            for event in journal:
                result["generation_journal"].append(event)
                checkpoint("generation_candidate", event)
                emit(event)
        validate_snapshot(snapshot, config["freshness_as_of"])
        if snapshot["master_seed"] != seed:
            raise ValueError("generated seed identity mismatch")
        result["scenarios"].append(snapshot)
        checkpoint("scenario", snapshot)
        actual = truth(snapshot, config["freshness_as_of"])
        for coalition in order:
            key = run_key(snapshot, coalition)
            if key in seen:
                raise ValueError("duplicate run_key")
            seen.add(key)
            observed = observe(snapshot, coalition, config["freshness_as_of"])
            result["optimizer_calls"] += 1
            emit(dict(event="optimizer_call", seed=seed, coalition_id=coalition, run_key=key))
            plan = optimize(observed)
            result["truth_evaluation_calls"] += 1
            emit(dict(event="truth_evaluation_call", seed=seed, coalition_id=coalition, run_key=key))
            evaluation = evaluate(actual, plan)
            row = dict(run_id=key, run_key=key, scenario_id=snapshot["scenario_id"],
                       snapshot_sha256=snapshot["snapshot_sha256"], seed=seed,
                       coalition_id=coalition, participants=list(members(coalition)),
                       algorithm_name=VERSIONS["algorithm_name"], algorithm_version=VERSIONS["algorithm_version"],
                       **{k: evaluation[k] for k in METRICS}, runtime_ms=plan.runtime_ms,
                       status=evaluation["status"], optimizer_status=plan.status,
                       permutations_evaluated=plan.permutations_evaluated)
            plan_record = dict(seed=seed, coalition_id=coalition, run_key=key, **asdict(plan))
            trace = dict(seed=seed, coalition_id=coalition, run_key=key, **evaluation)
            result["coalition_results"].append(row)
            result["plans"].append(plan_record)
            result["evaluations"].append(trace)
            checkpoint("run_complete", dict(row=row, plan=plan_record, evaluation=trace))
            emit(dict(event="run_complete", seed=seed, coalition_id=coalition, run_key=key,
                      status=evaluation["status"], permutations_evaluated=plan.permutations_evaluated))
        emit(dict(event="seed_complete", seed=seed))
    for section in ("coalition_results", "plans", "evaluations"):
        result[section].sort(key=lambda r: (r["seed"], COALITIONS.index(r["coalition_id"])))
    result["scenarios"].sort(key=lambda s: s["master_seed"])
    result["failures"].sort(key=lambda r: r["seed"])
    return result

def analyze(collected, config):
    """Never compute official subset means/CI/rewards; retain all failure reasons."""
    validate_config(config)
    rows = collected["coalition_results"]
    issues, invalid = [], set()
    by_seed = {s: [] for s in range(100)}
    snapshots = {}
    for snapshot in collected["scenarios"]:
        seed = snapshot.get("master_seed")
        if type(seed) is not int or seed not in range(100):
            issues.append("snapshot has unknown seed")
            continue
        if seed in snapshots:
            issues.append(f"seed {seed}: duplicate snapshot")
            invalid.add(seed)
        snapshots[seed] = snapshot
        try:
            validate_snapshot(snapshot, config["freshness_as_of"])
        except (ValueError, TypeError, KeyError) as exc:
            issues.append(f"seed {seed}: snapshot validation: {exc}")
            invalid.add(seed)
    keys = set()
    for row in rows:
        seed = row.get("seed")
        if type(seed) is not int or seed not in range(100):
            issues.append("row has unknown seed")
            continue
        by_seed[seed].append(row)
        errors = []
        cid = row.get("coalition_id")
        if cid not in COALITIONS:
            errors.append("unknown coalition")
        snapshot = snapshots.get(seed)
        if snapshot is None:
            errors.append("missing snapshot")
        elif cid in COALITIONS:
            for key in ("scenario_id", "snapshot_sha256"):
                if row.get(key) != snapshot[key]:
                    errors.append(key + " mismatch")
            expected = run_key(snapshot, cid)
            if row.get("run_key") != expected or row.get("run_id") != expected:
                errors.append("run identity mismatch")
            if row.get("participants") != list(members(cid)):
                errors.append("participant mismatch")
        key = row.get("run_key")
        if not isinstance(key, str) or not re.fullmatch("[0-9a-f]{64}", key) or key in keys:
            errors.append("missing/duplicate run_key")
        if isinstance(key, str):
            keys.add(key)
        for metric in METRICS:
            if type(row.get(metric)) is not int or row[metric] < 0:
                errors.append("invalid metric " + metric)
        if not errors and row["total_moves"] != row["total_rehandle_moves"] + row["completed_jobs"]:
            errors.append("move identity")
        if row.get("status") != "success" or row.get("optimizer_status") != "success":
            errors.append("unsuccessful run")
        if row.get("completed_jobs") != 6 or row.get("failed_jobs") != 0:
            errors.append("incomplete jobs")
        if type(row.get("permutations_evaluated")) is not int or row["permutations_evaluated"] != 720:
            errors.append("not 720 permutations")
        runtime = row.get("runtime_ms")
        if type(runtime) not in (int, float) or not math.isfinite(runtime) or runtime < 0:
            errors.append("invalid runtime")
        for k in ("algorithm_name", "algorithm_version"):
            if row.get(k) != VERSIONS[k]:
                errors.append(k + " mismatch")
        if errors:
            invalid.add(seed)
            issues.extend(f"seed {seed}/{cid}: {e}" for e in errors)
    if len(rows) != 800 or len(keys) != 800:
        issues.append("requires 800 rows and unique run keys")
    for key, expected in (("generator_calls", 100), ("optimizer_calls", 800), ("truth_evaluation_calls", 800)):
        if collected.get(key) != expected:
            issues.append(key + " count mismatch")
    for failure in collected["failures"]:
        issues.append(f"collection failure: {failure}")
        if failure.get("seed") in by_seed:
            invalid.add(failure["seed"])
    games, contributions, seed_rows = {}, {}, []
    for seed in range(100):
        group = by_seed[seed]
        if seed not in snapshots or len(group) != 8 or Counter(r.get("coalition_id") for r in group) != Counter(COALITIONS):
            issues.append(f"seed {seed}: missing/duplicate snapshot or coalition")
            invalid.add(seed)
        if seed in invalid:
            continue
        costs = {r["coalition_id"]: r["total_rehandle_moves"] for r in group}
        game = {cid: costs["NONE"] - costs[cid] for cid in COALITIONS}
        try:
            phi = exact_shapley(game)
            if game["NONE"] != 0 or sum(phi.values()) != game["CYR"]:
                raise ValueError("exact Shapley efficiency")
        except (ValueError, TypeError, ArithmeticError) as exc:
            issues.append(f"seed {seed}: {exc}")
            invalid.add(seed)
            continue
        games[seed], contributions[seed] = game, phi
        seed_rows.extend(dict(seed=seed, participant_id=p, participant_name=PARTICIPANTS[p],
                              shapley_moves=float(phi[p]), shapley_exact=str(phi[p]), diagnostic_only=False)
                         for p in PLAYERS)
    valid = not issues and len(games) == 100 and not invalid
    for row in seed_rows:
        row["diagnostic_only"] = not valid
    # Values for valid individual seeds remain diagnostic if the formal gate fails.
    for row in rows:
        seed, cid = row.get("seed"), row.get("coalition_id")
        row["value_moves"] = games[seed][cid] if seed in games and cid in COALITIONS else None
    aggregate = [dict(participant_id=p, participant_name=PARTICIPANTS[p], n=0,
                      mean_shapley_moves=None, mean_shapley_exact=None, ci_lower=None, ci_upper=None, available=False)
                 for p in PLAYERS]
    reward = dict(active=False, inactive_reason="invalid_formal_run_set", points={p: 0 for p in PLAYERS})
    descriptive = dict(available=False, reason="invalid_formal_run_set", series={})
    bootstrap = dict(available=False, reason="invalid_formal_run_set")
    mean_phi = mean_full = None
    reward_calls = 0
    if valid:
        series = {}
        for cid in COALITIONS:
            series[cid + ".J"] = [next(r["total_rehandle_moves"] for r in by_seed[s] if r["coalition_id"] == cid) for s in range(100)]
            series[cid + ".V"] = [games[s][cid] for s in range(100)]
        for p in PLAYERS:
            series[p + ".phi"] = [contributions[s][p] for s in range(100)]
        mean_phi = {p: sum(series[p + ".phi"]) / 100 for p in PLAYERS}
        mean_full = Fraction(sum(series["CYR.V"]), 100)
        if sum(mean_phi.values()) != mean_full:
            raise ValueError("aggregate exact efficiency failure")
        descriptive = dict(available=True, series={name: describe(v) for name, v in sorted(series.items())},
                           full_value=describe(series["CYR.V"]), sample_std_definition="n-1")
        bootstrap = paired_bootstrap({name: series[name] for name in ("CYR.V", "C.phi", "Y.phi", "R.phi")},
                                     config["bootstrap_seed"], config["bootstrap_replicates"])
        # Policy is applied once to the mean, and only at the positive valid gate.
        if mean_full > 0:
            reward = rewards(mean_phi, mean_full, True)
            reward_calls = 1
        else:
            reward = dict(active=False, inactive_reason="nonpositive_full_value", points={p: 0 for p in PLAYERS})
        aggregate = [dict(participant_id=p, participant_name=PARTICIPANTS[p], n=100,
                          mean_shapley_moves=float(mean_phi[p]), mean_shapley_exact=str(mean_phi[p]),
                          ci_lower=bootstrap["intervals"][p + ".phi"]["lower"],
                          ci_upper=bootstrap["intervals"][p + ".phi"]["upper"], available=True) for p in PLAYERS]
    summary = dict(formal_run_set_valid=valid, mode="formal", expected_seed_count=100,
                   valid_seed_count=len(games), invalid_seed_count=100-len(games),
                   statistics_sample_size=100 if valid else 0, run_count=len(rows),
                   generator_calls=collected["generator_calls"], optimizer_calls=collected["optimizer_calls"],
                   truth_evaluation_calls=collected["truth_evaluation_calls"], unique_run_keys=len(keys),
                   mean_full_value_moves=float(mean_full) if mean_full is not None else None,
                   mean_full_value_exact=str(mean_full) if mean_full is not None else None,
                   mean_shapley_moves={p: float(v) for p, v in mean_phi.items()} if mean_phi else None,
                   mean_shapley_exact={p: str(v) for p, v in mean_phi.items()} if mean_phi else None,
                   reward=reward, reward_calls=reward_calls, integer_reward_total=sum(reward["points"].values()))
    validation = dict(formal_run_set_valid=valid, issues=issues, invalid_seeds=sorted(invalid),
                      valid_seed_count=len(games), invalid_seed_count=100-len(games),
                      mean_shapley_efficiency_pass=valid, required_run_count=800)
    paid = [dict(participant_id=p, active=reward["active"], integer_reward_points=reward["points"][p],
                 inactive_reason=reward["inactive_reason"]) for p in PLAYERS]
    return dict(seed_contributions=seed_rows, aggregate_contributions=aggregate, rewards=paid,
                descriptive_stats=descriptive, bootstrap_ci=bootstrap, summary=summary, validation_report=validation)

def scientific(result, config):
    normalized = copy.deepcopy(result)
    for snapshot in normalized["scenarios"]:
        for key in ("generated_at", "observed_at", "valid_until"):
            snapshot.pop(key, None)
    for section in ("coalition_results", "plans"):
        for row in normalized[section]:
            row.pop("runtime_ms", None)
        normalized[section].sort(key=lambda r: (r["seed"], COALITIONS.index(r["coalition_id"])))
    normalized["evaluations"].sort(key=lambda r: (r["seed"], COALITIONS.index(r["coalition_id"])))
    normalized["scenarios"].sort(key=lambda s: s["master_seed"])
    normalized["config"] = {k: v for k, v in config.items() if k != "freshness_as_of"}
    return normalized

def write_csv(path, rows, fields):
    with path.open("x", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(row[key], separators=(",", ":"))
                             if isinstance(row[key], (list, dict)) else row[key] for key in fields})

def execute(config_raw, output, source_commit, reverse=False):
    config = validate_raw_config(config_raw)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "config.json").write_bytes(config_raw)
    write_json(output / "source_context.json", dict(source_commit=source_commit, config_sha256=CONFIG_SHA256))
    def emit(event):
        with (output / "run.log").open("ab") as f:
            f.write(canonical(dict(event, utc=datetime.now(timezone.utc).isoformat())))
    def checkpoint(kind, data):
        with (output / "progress.jsonl").open("ab") as f:
            f.write(canonical(dict(kind=kind, data=data)))
    try:
        emit(dict(event="start", source_commit=source_commit, reverse=bool(reverse)))
        collected = collect(config, tuple(reversed(COALITIONS)) if reverse else COALITIONS,
                            emit=emit, checkpoint=checkpoint)
        analysis = analyze(collected, config)
        result = dict(collected, **analysis)
        for name, section in (("scenarios", "scenarios"), ("generation_journal", "generation_journal"),
                              ("plans", "plans"), ("evaluation_traces", "evaluations")):
            with (output / (name + ".jsonl")).open("xb") as f:
                for row in result[section]:
                    f.write(canonical(row))
        for name, fields in (("coalition_results", ROW_FIELDS), ("seed_contributions", SEED_FIELDS),
                             ("aggregate_contributions", AGG_FIELDS), ("rewards", REWARD_FIELDS)):
            write_csv(output / (name + ".csv"), result[name], fields)
        for name in ("descriptive_stats", "bootstrap_ci", "summary", "validation_report"):
            write_json(output / (name + ".json"), result[name])
        write_json(output / "scientific.json", scientific(result, config))
        emit(dict(event="complete", formal_run_set_valid=result["summary"]["formal_run_set_valid"]))
        return result
    except Exception:
        (output / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        if not (output / "summary.json").exists():
            write_json(output / "summary.json", dict(formal_run_set_valid=False, status="aborted",
                       reward_active=False, integer_reward_total=0))
        emit(dict(event="aborted", formal_run_set_valid=False))
        raise
    finally:
        with (output / "SHA256SUMS.txt").open("x", encoding="utf-8") as f:
            for path in sorted(output.iterdir()):
                if path.is_file() and path.name != "SHA256SUMS.txt":
                    f.write(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n")

def source_guard(expected_commit):
    if not re.fullmatch("[0-9a-f]{40}", expected_commit):
        raise ValueError("expected commit must be full SHA")
    if sys.version_info[:2] != (3, 12):
        raise ValueError("requires Python 3.12")
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT).decode()
    if git("rev-parse", "HEAD").strip() != expected_commit:
        raise ValueError("source commit mismatch")
    if git("branch", "--show-current").strip() != "main":
        raise ValueError("formal branch mismatch")
    if git("status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("source tree dirty")
    blob = subprocess.check_output(["git", "show", expected_commit + ":configs/cy_v2_formal.json"], cwd=ROOT)
    validate_raw_config(blob)

def main():
    parser = argparse.ArgumentParser(description="Frozen CY v2 formal 100-seed run")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--reverse", action="store_true")
    args = parser.parse_args()
    try:
        raw = Path(args.config).read_bytes()
        validate_raw_config(raw)
        source_guard(args.expected_commit)
        result = execute(raw, args.output, args.expected_commit, args.reverse)
        print(json.dumps(result["summary"], sort_keys=True))
        return 0 if result["summary"]["formal_run_set_valid"] else 1
    except Exception:
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
