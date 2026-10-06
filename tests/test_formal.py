import copy
import csv
import hashlib
import json
import math
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from socfai_incentive import data, formal, statistics as stats, value

ROOT = Path(__file__).resolve().parents[1]


def cfg():
    # Only fabricated games use this config; no formal optimization in tests.
    c = json.loads((ROOT / "configs/formal.json").read_bytes())
    c["bootstrap_replicates"] = 13
    return c


def fabricated():
    config = cfg()
    rows = []
    for s in range(100):
        # Varying, signed additive contributions distinguish aggregate allocation
        # from the mean of seed-specific allocations.
        weights = dict(C=1 + s, T=-2, R=3)
        snapshot = dict(scenario_id=config["scenario_id"], master_seed=s,
                        snapshot_sha256=hashlib.sha256(str(s).encode()).hexdigest())
        for cid in data.COALITIONS:
            cost = 200 + s - sum(weights.get(c, 0) for c in (cid if cid != "NONE" else ""))
            rows.append(dict(seed=s, coalition_id=cid, run_id=formal.run_key(snapshot, cid),
                             scenario_id=config["scenario_id"], snapshot_sha256=snapshot["snapshot_sha256"],
                             algorithm_name=formal.ALGORITHM_NAME, algorithm_version=formal.ALGORITHM_VERSION,
                             optimizer_options=copy.deepcopy(config["optimizer"]), optimizer_status="success",
                             status="success", failed_jobs=0, unassigned_jobs=0, completed_jobs=12,
                             total_waiting_minutes=0, total_travel_minutes=cost, total_operation_minutes=cost,
                             runtime_ms=1, optimizer_iterations=1))
    return rows


class StatisticsTests(unittest.TestCase):
    def test_known_description_sample_std(self):
        got = stats.describe([1, 2, 3, 4])
        self.assertEqual({k: v for k, v in got.items() if k != "sample_std"},
                         dict(n=4, mean=2.5, median=2.5, min=1, max=4))
        self.assertAlmostEqual(got["sample_std"], math.sqrt(5 / 3))

    def test_known_linear_percentile(self):
        self.assertEqual(stats.percentile([30, 0, 20, 10], .25), 7.5)
        self.assertEqual(stats.percentile([30, 0, 20, 10], .975), 29.25)
        self.assertEqual(stats.percentile([1], .5), 1)
        self.assertEqual(stats.percentile([1, 2], 0), 1)
        self.assertEqual(stats.percentile([1, 2], 1), 2)

    def test_paired_indices_and_fixed_random_reproduction(self):
        series = {"NONE.J": [10, 30, 20], "CTR.J": [5, 10, 10], "x": [1, 9, -2], "twice_x": [2, 18, -4]}
        got = stats.paired_bootstrap(series, 20260910, 10000)
        self.assertEqual(got, stats.paired_bootstrap(series, 20260910, 10000))
        # Independent direct RNG loop, hand arithmetic and interpolation.
        rng = random.Random(20260910)
        xs, ratios = [], []
        h = hashlib.sha256()
        for _ in range(10000):
            indices = [rng.randrange(3) for _ in range(3)]
            h.update(data.canonical(indices) + b"\n")
            xs.append(sum(series["x"][i] for i in indices) / 3)
            a = sum(series["NONE.J"][i] for i in indices) / 3
            b = sum(series["CTR.J"][i] for i in indices) / 3
            ratios.append(100 * (a - b) / a)
        self.assertEqual(got["index_vectors_sha256"], h.hexdigest())
        def bound(xs, p):
            ordered = sorted(xs); pos = 9999 * p; i = int(pos)
            return ordered[i] * (1 - (pos - i)) + ordered[i + 1] * (pos - i)
        for p, key in [(.025, "lower"), (.975, "upper")]:
            self.assertAlmostEqual(got["intervals"]["x"][key], bound(xs, p))
            self.assertAlmostEqual(got["intervals"]["twice_x"][key], 2 * bound(xs, p))
            self.assertAlmostEqual(got["intervals"][got["auxiliary_target"]][key], bound(ratios, p))

    def test_statistics_reject_nonfinite_missing_and_unpaired(self):
        for bad in ([1, None], [1, float("nan")], [1, float("inf")], [1]):
            with self.assertRaises(ValueError):
                stats.describe(bad)
        for series in ({"NONE.J": [1, 2], "CTR.J": [1]},
                       {"NONE.J": [1, None], "CTR.J": [1, 2]},
                       {"NONE.J": [0, 1], "CTR.J": [1, 2]}):
            with self.assertRaises(ValueError):
                stats.paired_bootstrap(series, 1, 10)


class FormalAnalysisTests(unittest.TestCase):
    def test_formal_config_and_smoke_raw_pins(self):
        self.assertEqual(hashlib.sha256((ROOT / "configs/formal.json").read_bytes()).hexdigest(), formal.FORMAL_SHA256)
        self.assertEqual(hashlib.sha256((ROOT / "configs/smoke.json").read_bytes()).hexdigest(),
                         "9dcfdf5d41a4cf18784a4e3760b3dbf9273ca1c66ce85f86c90c073d4c392141")
        c = json.loads((ROOT / "configs/formal.json").read_bytes())
        self.assertEqual(c["seeds"], list(range(100)))
        self.assertEqual(c["bootstrap_seed"], 20260910)
        self.assertEqual(c["bootstrap_replicates"], 10000)

    def test_100_fabricated_games_mean_efficiency_and_single_reward(self):
        with patch.object(formal, "rewards", wraps=value.rewards) as call:
            result = formal.analyze(fabricated(), cfg())
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args, ({"cfs": 50.5, "terminal": -2.0, "carrier": 3.0}, 51.5, True))
        self.assertTrue(result["validation_report"]["mean_shapley_efficiency_pass"])
        self.assertEqual(len(result["seed_contributions"]), 300)
        self.assertEqual(result["summary"]["statistics_sample_size"], 100)
        self.assertEqual(result["summary"]["integer_reward_total"], 1000)
        paid = {r["participant_id"]: r["integer_reward_points"] for r in result["rewards"]}
        self.assertEqual(paid, dict(cfs=944, terminal=0, carrier=56))
        self.assertAlmostEqual(result["descriptive_stats"]["series"]["CTR.V"]["mean"], 51.5)

    def test_ratio_of_means_distinct_from_mean_seed_percent(self):
        result = formal.analyze(fabricated(), cfg())
        descriptive = result["descriptive_stats"]
        self.assertAlmostEqual(descriptive["ratio_of_means_reduction_pct"], 100 * 51.5 / 249.5)
        self.assertNotAlmostEqual(descriptive["ratio_of_means_reduction_pct"], descriptive["series"]["CTR.time_reduction_pct"]["mean"])

    def test_missing_duplicate_invalid_timeout_iteration_and_null_block(self):
        mutations = [lambda r: r.pop(), lambda r: r.append(copy.deepcopy(r[0])),
                     lambda r: r[0].update(status="timeout", optimizer_status="timeout"),
                     lambda r: r[0].update(status="iteration_limit", optimizer_status="iteration_limit"),
                     lambda r: r[0].update(failed_jobs=1), lambda r: r[0].update(unassigned_jobs=1),
                     lambda r: r[0].update(total_operation_minutes=None),
                     lambda r: r[0].update(total_operation_minutes=float("nan")),
                     lambda r: r[0].update(snapshot_sha256="0" * 64),
                     lambda r: r[0].update(optimizer_options={}),
                     lambda r: r[0].update(run_id=r[1]["run_id"])]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                rows = fabricated(); mutate(rows)
                with patch.object(formal, "paired_bootstrap") as bootstrap, patch.object(formal, "rewards") as reward:
                    result = formal.analyze(rows, cfg())
                self.assertFalse(result["summary"]["formal_run_set_valid"])
                self.assertTrue(result["validation_report"]["issues"])
                self.assertFalse(result["bootstrap_ci"]["available"])
                self.assertFalse(result["descriptive_stats"]["available"])
                self.assertIsNone(result["summary"]["mean_phi"])
                self.assertEqual(result["summary"]["integer_reward_total"], 0)
                bootstrap.assert_not_called(); reward.assert_not_called()

    def test_zero_denominator_blocks_analysis(self):
        rows = fabricated()
        for row in rows[:8]:
            row.update(total_operation_minutes=0, total_travel_minutes=0)
        result = formal.analyze(rows, cfg())
        self.assertFalse(result["summary"]["formal_run_set_valid"])
        self.assertEqual(result["validation_report"]["valid_seed_count"], 99)
        self.assertFalse(result["bootstrap_ci"]["available"])


class FormalRunnerTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "outputs").mkdir(parents=True, exist_ok=True)

    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "configs/smoke.json").read_bytes())
        cls.config.update(job_count=2, seeds=[0, 1])
        with patch.object(formal, "optimize", wraps=formal.optimize) as opt, patch.object(formal, "evaluate", wraps=formal.evaluate) as ev, patch.object(formal, "generate", wraps=formal.generate) as gen, patch.object(formal, "rewards") as rew:
            cls.forward = formal.collect(cls.config)
            cls.call_counts = (opt.call_count, ev.call_count, gen.call_count, rew.call_count)
        cls.reverse = formal.collect(cls.config, tuple(reversed(data.COALITIONS)))

    def test_small_fixture_call_counts_and_run_keys(self):
        self.assertEqual(self.call_counts, (16, 16, 2, 0))
        self.assertEqual(len({r["run_id"] for r in self.forward["coalition_results"]}), 16)
        self.assertEqual(self.forward["optimizer_calls"], 16)
        self.assertEqual(self.forward["truth_evaluation_calls"], 16)
        for s in [0, 1]:
            self.assertEqual(len({r["snapshot_sha256"] for r in self.forward["coalition_results"] if r["seed"] == s}), 1)

    def test_reverse_order_normalized_scientific_equality(self):
        self.assertEqual(data.canonical(formal.scientific(self.forward)), data.canonical(formal.scientific(self.reverse)))

    def test_no_duplicate_seed_or_coalition_execution(self):
        with patch.object(formal, "optimize") as opt:
            with self.assertRaises(ValueError):
                formal.collect(self.config, ["NONE"] * 8)
            c = copy.deepcopy(self.config); c["seeds"] = [0, 0]
            with self.assertRaises(ValueError):
                formal.collect(c)
            opt.assert_not_called()

    def test_output_contract_csv_jsonl_hashes_and_no_overwrite(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "outputs") as tmp:
            dest = Path(tmp) / "fixture"
            result = formal.execute(data.canonical(self.config), dest, "small fixture", "test-fixture")
            required = set("config.json scenarios.jsonl plans.jsonl evaluation_traces.jsonl coalition_results.csv seed_contributions.csv aggregate_contributions.csv rewards.csv descriptive_stats.json bootstrap_ci.json summary.json validation_report.json scientific.json run.log environment.txt commands.txt SHA256SUMS".split())
            self.assertEqual({p.name for p in dest.iterdir()}, required)
            hashes = {}
            for line in (dest / "SHA256SUMS").read_text().splitlines():
                h, name = line.split("  "); hashes[name] = h
                self.assertEqual(hashlib.sha256((dest / name).read_bytes()).hexdigest(), h)
            self.assertEqual(set(hashes), required - {"SHA256SUMS"})
            for name, count in [("scenarios", 2), ("plans", 16), ("evaluation_traces", 16)]:
                lines = (dest / f"{name}.jsonl").read_bytes().splitlines()
                self.assertEqual(len(lines), count)
                for line in lines:
                    self.assertEqual(data.canonical(json.loads(line)), line)
            for name, count in [("coalition_results", 16), ("seed_contributions", 6), ("aggregate_contributions", 3), ("rewards", 3)]:
                with (dest / f"{name}.csv").open() as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), count)
                self.assertTrue(all(None not in row for row in rows))
            events = [json.loads(line) for line in (dest / "run.log").read_bytes().splitlines()]
            self.assertEqual(sum(e["event"] == "optimizer_call" for e in events), 16)
            self.assertEqual(sum(e["event"] == "truth_evaluation_call" for e in events), 16)
            self.assertEqual(json.loads((dest / "scientific.json").read_bytes()), formal.scientific(result))
            with patch.object(formal, "optimize") as opt:
                with self.assertRaises(FileExistsError):
                    formal.execute(data.canonical(self.config), dest, "repeat", "test-fixture")
                opt.assert_not_called()

    def test_failure_preserves_partial_journal(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "outputs") as tmp:
            dest = Path(tmp) / "failed"
            with patch.object(formal, "optimize", side_effect=RuntimeError("fixture error")):
                with self.assertRaises(RuntimeError):
                    formal.execute(data.canonical(self.config), dest, "failure fixture", "test-fixture")
            self.assertIn("fixture error", (dest / "failure.txt").read_text())
            events = [json.loads(line) for line in (dest / "run.log").read_bytes().splitlines()]
            self.assertEqual([e["event"] for e in events], ["start", "scenario_generated", "optimizer_call", "aborted"])

    def test_fabricated_complete_output_schema(self):
        config = cfg()
        fake = dict(scenarios=[], plans=[], evaluations=[], coalition_results=fabricated(),
                    optimizer_calls=800, truth_evaluation_calls=800)
        with tempfile.TemporaryDirectory(dir=ROOT / "outputs") as tmp, patch.object(formal, "collect", return_value=fake):
            dest = Path(tmp) / "fabricated"
            formal.execute(data.canonical(config), dest, "fabricated; zero actual optimizer calls", "test-fixture")
            for name, count in [("coalition_results", 800), ("seed_contributions", 300), ("aggregate_contributions", 3), ("rewards", 3)]:
                with (dest / f"{name}.csv").open() as f:
                    self.assertEqual(len(list(csv.DictReader(f))), count)

    def test_cli_requires_pinned_config_expected_commit_and_clean_tree(self):
        from contextlib import redirect_stderr
        from io import StringIO
        argv = ["formal", "--config", str(ROOT / "configs/formal.json"),
                "--output", str(ROOT / "outputs/forbidden-test"), "--expected-commit", "fixture"]
        with patch("sys.argv", argv), patch.object(formal, "execute") as execute, redirect_stderr(StringIO()):
            for responses in [[b"other\n"], [b"fixture\n", b" M README.md\n"]]:
                with patch.object(formal.subprocess, "check_output", side_effect=responses):
                    with self.assertRaises(SystemExit) as exc:
                        formal.main()
                    self.assertEqual(exc.exception.code, 2)
            argv[2] = str(ROOT / "configs/smoke.json")
            with self.assertRaises(SystemExit):
                formal.main()
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
