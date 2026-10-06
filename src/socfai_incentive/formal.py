"""Frozen dispatch formal execution, fixed statistics and immutable packages.

The separate CLI preserves the previously verified smoke API and its guard.
Core generator, masking, optimizer, evaluator and value rules are imported unchanged.
"""

import argparse
import copy
import csv
import hashlib
import json
import platform
import shlex
import subprocess
import sys
import traceback
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .data import COALITIONS, PROVIDERS, SEEDS, canonical, generate, observed, truth, validate_config, validate_snapshot
from .evaluator import evaluate
from .optimizer import ALGORITHM_NAME, ALGORITHM_VERSION, optimize
from .runner import COALITION_COLUMNS, CONTRIBUTION_COLUMNS, REWARD_COLUMNS, json_bytes, run_key
from .statistics import describe, finite, paired_bootstrap
from .value import aggregate, close, game, metrics, rewards, shapley

FORMAL_SHA256 = "3a97cd5ee136cbc14431a759edae7e365e2011a060c05afa2bc6a424af321520"
PARTICIPANTS = tuple(sorted(PROVIDERS.values()))


def collect(config, order=COALITIONS, progress=None):
    """Collect all configured seeds without computing any rewards.

    Tests use a small smoke-derived fixture; production CLI pins the full config.
    Each attempted optimizer/truth evaluation call is journaled before invocation.
    """
    validate_config(config)
    if config["seeds"] != sorted(set(config["seeds"])):
        raise ValueError("unique ascending seeds required")
    if len(order) != 8 or set(order) != set(COALITIONS):
        raise ValueError("exactly eight unique coalitions required")
    result = dict(scenarios=[], coalition_results=[], plans=[], evaluations=[],
                  optimizer_calls=0, truth_evaluation_calls=0)
    seen = set()
    for seed in config["seeds"]:
        snapshot = generate(config, seed)
        validate_snapshot(snapshot, config["freshness_as_of"])
        result["scenarios"].append(snapshot)
        if progress:
            progress(dict(event="scenario_generated", seed=seed, snapshot=snapshot))
        for cid in order:
            key = run_key(snapshot, cid)
            if key in seen:
                raise ValueError("duplicate run_key")
            seen.add(key)
            identity = dict(seed=seed, coalition_id=cid, run_id=key)
            result["optimizer_calls"] += 1
            if progress:
                progress(dict(event="optimizer_call", **identity))
            plan = optimize(observed(snapshot, cid, config["optimizer"]))
            result["truth_evaluation_calls"] += 1
            if progress:
                progress(dict(event="truth_evaluation_call", **identity))
            actual = evaluate(truth(snapshot), plan.routes)
            status = plan.status
            if actual["failed_jobs"] or actual["unassigned_jobs"]:
                status = "invalid_assignment" if status != "timeout" else status
            row = dict(**identity, scenario_id=snapshot["scenario_id"],
                       snapshot_sha256=snapshot["snapshot_sha256"],
                       algorithm_name=ALGORITHM_NAME, algorithm_version=ALGORITHM_VERSION,
                       optimizer_options=copy.deepcopy(config["optimizer"]),
                       **{k: v for k, v in actual.items() if k != "trace"},
                       runtime_ms=plan.runtime_ms, status=status,
                       optimizer_iterations=plan.iterations, optimizer_status=plan.status)
            result["coalition_results"].append(row)
            plan_row = dict(**identity, plan=asdict(plan))
            eval_row = dict(**identity, evaluation=actual)
            result["plans"].append(plan_row)
            result["evaluations"].append(eval_row)
            if progress:
                progress(dict(event="run_complete", row=row, plan=plan_row, evaluation=eval_row))
        if progress:
            progress(dict(event="seed_complete", seed=seed, snapshot=snapshot))
    for key in ("coalition_results", "plans", "evaluations"):
        result[key].sort(key=lambda r: (r["seed"], COALITIONS.index(r["coalition_id"])))
    return result


def analyze(rows, config):
    """Fail closed on any required seed, run identity, metric or efficiency gap."""
    issues, seed_values, seed_phis = [], {}, {}
    def issue(seed, coalition, reason):
        issues.append(dict(seed=seed, coalition_id=coalition, reason=reason))
    if len(rows) != 800:
        issue(None, None, f"run_count:{len(rows)}!=800")
    if {r.get("seed") for r in rows} != set(SEEDS):
        issue(None, None, "seed_set_not_0_to_99")
    counts = Counter(r.get("run_id") for r in rows)
    for row in rows:
        if not row.get("run_id") or counts[row.get("run_id")] != 1:
            issue(row.get("seed"), row.get("coalition_id"), "missing_or_duplicate_run_key")
        if row.get("seed") not in SEEDS:
            issue(row.get("seed"), row.get("coalition_id"), "unexpected_seed")
    for seed in SEEDS:
        group = [r for r in rows if r.get("seed") == seed]
        start = len(issues)
        ids = Counter(r.get("coalition_id") for r in group)
        for cid in COALITIONS:
            if ids[cid] != 1:
                issue(seed, cid, f"coalition_count:{ids[cid]}!=1")
        if len(group) != 8 or set(ids) != set(COALITIONS):
            issue(seed, None, "incomplete_or_extra_coalitions")
        hashes = {r.get("snapshot_sha256") for r in group}
        if len(hashes) != 1 or not all(isinstance(h, str) and len(h) == 64 for h in hashes):
            issue(seed, None, "snapshot_hash_mismatch_or_missing")
        for row in group:
            cid = row.get("coalition_id")
            if row.get("status") != "success" or row.get("optimizer_status") != "success":
                issue(seed, cid, f"status:{row.get('status')};optimizer:{row.get('optimizer_status')}")
            if row.get("failed_jobs") != 0 or row.get("unassigned_jobs") != 0:
                issue(seed, cid, "failed_or_unassigned_jobs")
            if (row.get("algorithm_name") != ALGORITHM_NAME or row.get("algorithm_version") != ALGORITHM_VERSION
                    or row.get("optimizer_options") != config["optimizer"]):
                issue(seed, cid, "algorithm_or_options_mismatch")
            if row.get("scenario_id") != config["scenario_id"]:
                issue(seed, cid, "scenario_identity_mismatch")
            elif row.get("run_id") != run_key(dict(scenario_id=row["scenario_id"],
                    snapshot_sha256=row.get("snapshot_sha256"), master_seed=seed), cid):
                issue(seed, cid, "run_key_identity_mismatch")
            kpis = [row.get(k) for k in ("total_waiting_minutes", "total_travel_minutes", "total_operation_minutes")]
            if not finite(kpis) or any(v < 0 for v in kpis):
                issue(seed, cid, "nonfinite_null_or_negative_KPI")
            elif not close(kpis[0] + kpis[1], kpis[2]):
                issue(seed, cid, "J_not_waiting_plus_travel")
        if len(issues) == start and not any(i["seed"] == seed for i in issues):
            try:
                values = game(group)
                phi = shapley(values)
                baseline = next(r["total_operation_minutes"] for r in group if r["coalition_id"] == "NONE")
                for row in group:
                    metric = metrics(baseline, row["total_operation_minutes"])
                    row.update(metric)
                    if not finite(metric[k] for k in ("value_minutes", "time_reduction_pct", "efficiency_index")):
                        issue(seed, row["coalition_id"], "nonfinite_or_null_metric:" + str(metric))
                # Scientific Shapley for feasible games is retained even if a ratio is undefined.
                seed_values[seed], seed_phis[seed] = values, phi
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                issue(seed, None, "game_or_shapley:" + str(exc))
        for row in group:
            if "value_minutes" not in row:
                row.update(value_minutes=None, time_reduction_pct=None, efficiency_index=None,
                           time_reduction_reason="invalid_seed", efficiency_index_reason="invalid_seed")
    valid = not issues and len(seed_phis) == 100
    mean_phi = full = None
    descriptive = dict(available=False, reason="invalid_formal_run_set", series={})
    bootstrap = dict(available=False, reason="invalid_formal_run_set", intervals={})
    if valid:
        try:
            mean_phi, full = aggregate([seed_values[s] for s in SEEDS])
            series = {}
            for cid in COALITIONS:
                group = sorted((r for r in rows if r["coalition_id"] == cid), key=lambda r: r["seed"])
                for label, field in [("J", "total_operation_minutes"), ("V", "value_minutes"),
                                     ("time_reduction_pct", "time_reduction_pct"), ("efficiency_index", "efficiency_index")]:
                    series[f"{cid}.{label}"] = [r[field] for r in group]
            for p in PARTICIPANTS:
                series[f"shapley.{p}"] = [seed_phis[s][p] for s in SEEDS]
            bootstrap = paired_bootstrap(series, config["bootstrap_seed"], config["bootstrap_replicates"])
            descriptive = dict(available=True, sample_std_definition="statistics.stdev; denominator n-1",
                               series={key: describe(values) for key, values in series.items()},
                               full_sharing_aliases={"value_minutes": "CTR.V", "seed_reduction_pct": "CTR.time_reduction_pct"},
                               ratio_of_means_reduction_pct=bootstrap["intervals"][bootstrap["auxiliary_target"]]["point_estimate"])
        except (ValueError, TypeError, OverflowError) as exc:
            issue(None, None, "formal_analysis:" + str(exc))
            valid = False
            mean_phi = full = None
            descriptive = dict(available=False, reason="formal_analysis_failure", series={})
            bootstrap = dict(available=False, reason="formal_analysis_failure", intervals={})
    # Exactly one policy invocation for a valid aggregate; never per-seed points.
    if valid:
        reward_rows = rewards(mean_phi, full, True)
        reward_calls = 1
    else:
        reward_calls = 0
        reward_rows = [dict(participant_id=p, raw_shapley_minutes=None, mean_shapley_minutes=None,
                           value_credit_minutes=None, contribution_share_pct=None, full_value_minutes=None,
                           reward_active=False, reward_inactive_reason="invalid_formal_run_set",
                           raw_reward_points=0, integer_reward_points=0, rounding_rank=None,
                           reward_rule_version="1.0.0", calculation_version="exact-shapley-3-v1") for p in PARTICIPANTS]
    for row in reward_rows:
        row.update(scenario_id=config["scenario_id"], aggregation_scope="formal_100_paired_seeds")
    invalid_seeds = sorted({i["seed"] for i in issues if type(i["seed"]) is int})
    seed_rows = [dict(scenario_id=config["scenario_id"], seed=s, participant_id=p,
                      raw_shapley_minutes=seed_phis[s][p], full_value_minutes=seed_values[s]["CTR"],
                      calculation_version="exact-shapley-3-v1") for s in SEEDS if s in seed_phis for p in PARTICIPANTS]
    validation = dict(formal_run_set_valid=valid, run_count=len(rows), unique_run_keys=len(counts),
                      required_seed_count=100, observed_seeds=sorted({r["seed"] for r in rows}),
                      valid_seed_count=sum(s in seed_phis and s not in invalid_seeds for s in SEEDS),
                      valid_run_count=sum(r.get("status") == "success" and r.get("failed_jobs") == 0
                                          and r.get("unassigned_jobs") == 0 for r in rows),
                      invalid_seeds=invalid_seeds, issues=issues,
                      seed_shapley_efficiency={str(s): close(sum(seed_phis[s].values()), seed_values[s]["CTR"]) for s in seed_phis},
                      mean_shapley_efficiency_pass=bool(valid and close(sum(mean_phi.values()), full)),
                      reward_rule_calls=reward_calls, statistics_sample_size=100 if valid else 0,
                      replay_pooled_into_primary=False)
    summary = dict(scenario_id=config["scenario_id"], data_grade="synthetic", formal_run_set_valid=valid,
                   full_value_minutes=full, mean_phi=mean_phi, reward_active=all(r["reward_active"] for r in reward_rows),
                   reward_inactive_reason=reward_rows[0]["reward_inactive_reason"],
                   integer_reward_total=sum(r["integer_reward_points"] for r in reward_rows),
                   valid_seed_count=validation["valid_seed_count"], statistics_sample_size=100 if valid else 0)
    return dict(seed_contributions=seed_rows, aggregate_contributions=reward_rows, rewards=reward_rows,
                descriptive_stats=descriptive, bootstrap_ci=bootstrap, validation_report=validation, summary=summary)


def scientific(result):
    normalized = copy.deepcopy(result)
    for row in normalized["coalition_results"]:
        row.pop("runtime_ms", None)
    for row in normalized["plans"]:
        row["plan"].pop("runtime_ms", None)
    return normalized


def write_package(dest, config_raw, result, command, commit, journal):
    """Write into an already atomically reserved NEW output directory."""
    dest = Path(dest)
    (dest / "config.json").write_bytes(config_raw)
    for name, key in [("scenarios", "scenarios"), ("plans", "plans"), ("evaluation_traces", "evaluations")]:
        (dest / f"{name}.jsonl").write_bytes(b"".join(canonical(row) + b"\n" for row in result[key]))
    for name in ("descriptive_stats", "bootstrap_ci", "summary", "validation_report"):
        (dest / f"{name}.json").write_bytes(json_bytes(result[name]))
    (dest / "scientific.json").write_bytes(canonical(scientific(result)) + b"\n")
    seed_columns = "scenario_id seed participant_id raw_shapley_minutes full_value_minutes calculation_version".split()
    for name, columns in [("coalition_results", COALITION_COLUMNS + ["optimizer_options"]),
                          ("seed_contributions", seed_columns), ("aggregate_contributions", CONTRIBUTION_COLUMNS),
                          ("rewards", REWARD_COLUMNS)]:
        with (dest / f"{name}.csv").open("w", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            for row in result[name]:
                record = dict(row)
                if "optimizer_options" in record:
                    record["optimizer_options"] = canonical(record["optimizer_options"]).decode()
                writer.writerow(record)
    (dest / "environment.txt").write_text(f"Python {platform.python_version()}\n{platform.platform()}\nhost={platform.node()}\ncommit={commit}\n", encoding="utf-8")
    (dest / "commands.txt").write_text(command + "\n", encoding="utf-8")
    journal(dict(event="package_complete", optimizer_calls=result["optimizer_calls"],
                 truth_evaluation_calls=result["truth_evaluation_calls"]))
    (dest / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
                                               for p in sorted(dest.iterdir()) if p.is_file() and p.name != "SHA256SUMS"), encoding="utf-8")


def execute(config_raw, destination, command, commit, order=COALITIONS):
    config = json.loads(config_raw)
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=False)
    def journal(event):
        event = dict(utc=datetime.now(timezone.utc).isoformat(), **event)
        with (dest / "run.log").open("ab") as out:
            out.write(canonical(event) + b"\n")
    journal(dict(event="start", commit=commit, command=command, coalition_execution_order=order))
    try:
        result = collect(config, order, journal)
        result.update(analyze(result["coalition_results"], config))
        result["validation_report"].update(optimizer_calls=result["optimizer_calls"],
                                            truth_evaluation_calls=result["truth_evaluation_calls"])
        write_package(dest, config_raw, result, command, commit, journal)
        return result
    except Exception:
        (dest / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        journal(dict(event="aborted", reason="see failure.txt; no retry or exclusion"))
        raise


def main():
    parser = argparse.ArgumentParser(description="Frozen dispatch formal experiment")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--reverse", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    raw = Path(args.config).read_bytes()
    if hashlib.sha256(raw).hexdigest() != FORMAL_SHA256:
        parser.error("frozen formal config raw-byte hash mismatch")
    if sys.version_info[:2] != (3, 12):
        parser.error("Python 3.12 required")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    if commit != args.expected_commit or subprocess.check_output(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=root):
        parser.error("expected clean committed code required before results")
    output = Path(args.output).resolve()
    if not output.is_relative_to(root / "outputs") or output == root / "outputs":
        parser.error("new output directory under repository outputs/ required")
    if output.exists():
        parser.error("output exists; overwrite forbidden")
    command = "PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -B -m socfai_incentive.formal " + shlex.join(sys.argv[1:])
    result = execute(raw, output, command, commit, tuple(reversed(COALITIONS)) if args.reverse else COALITIONS)
    print(json.dumps(result["summary"], sort_keys=True))
    print(f"optimizer_calls={result['optimizer_calls']} truth_evaluation_calls={result['truth_evaluation_calls']}")
    return 0 if result["summary"]["formal_run_set_valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
