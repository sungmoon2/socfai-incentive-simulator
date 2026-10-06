import copy
import hashlib
import inspect
import json
import tempfile
import unittest
from pathlib import Path

from socfai_incentive import data, evaluator, optimizer, runner, value
from socfai_incentive.schedule import simulate, service_slot

ROOT = Path(__file__).resolve().parents[1]


def config():
    return json.loads((ROOT / "configs/smoke.json").read_text())


def small_state(capacity=1):
    return dict(horizon_minutes=100,
                locations={"CFS_A": {"server_capacity": capacity, "open_intervals": [[0, 100]]},
                           "TML_1": {"server_capacity": capacity, "open_intervals": [[0, 100]]}},
                jobs=[dict(job_id="J1", origin_id="CFS_A", destination_id="TML_1", ready_at=0,
                           service_minutes_origin=5, service_minutes_destination=5,
                           window_start=0, window_end=100, priority=1)],
                trucks=[dict(truck_id="T1", start_location_id="CFS_A", available_at=0, enabled=True)],
                travel_times={"CFS_A": {"CFS_A": 0, "TML_1": 10}, "TML_1": {"CFS_A": 10, "TML_1": 0}})


class InputTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.s = data.generate(self.cfg, 0)

    def reject(self, s, regex):
        with self.assertRaisesRegex(ValueError, regex):
            data.validate_snapshot(s, self.cfg["freshness_as_of"])

    def test_required_metadata(self):
        for field in ("scenario_id", "generated_at", "truth", "public_defaults", "provider_snapshots"):
            with self.subTest(field=field):
                s = copy.deepcopy(self.s); del s[field]
                self.reject(s, field)

    def test_hash_and_deterministic_generation(self):
        self.assertEqual(self.s, data.generate(self.cfg, 0))
        self.assertEqual(self.s["snapshot_sha256"], data.snapshot_hash(self.s))
        self.s["truth"]["jobs"][0]["ready_at"] += 1
        self.reject(self.s, "snapshot mismatch")

    def test_hash_covers_defaults_providers_and_metadata(self):
        for section in ("public_defaults", "provider_snapshots", "rng_streams"):
            s = copy.deepcopy(self.s); s[section]["tamper"] = 1
            self.reject(s, "snapshot mismatch")

    def test_missing_job_field(self):
        del self.s["truth"]["jobs"][0]["ready_at"]
        data.seal(self.s)
        self.reject(self.s, "ready_at")

    def test_negative_times_and_types(self):
        for field, invalid in [("ready_at", -1), ("service_minutes_origin", -1), ("service_minutes_destination", 1.5), ("priority", True)]:
            with self.subTest(field=field):
                s = copy.deepcopy(self.s); s["truth"]["jobs"][0][field] = invalid; data.seal(s)
                self.reject(s, field)

    def test_negative_travel(self):
        self.s["truth"]["travel_times"]["CFS_A"]["TML_1"] = -1
        data.seal(self.s); self.reject(self.s, "travel_times")

    def test_duplicate_ids(self):
        for group, key in [("jobs", "job_id"), ("trucks", "truck_id")]:
            s = copy.deepcopy(self.s); s["truth"][group][1][key] = s["truth"][group][0][key]
            data.seal(s); self.reject(s, "duplicate ID")

    def test_unknown_location(self):
        self.s["truth"]["jobs"][0]["destination_id"] = "UNKNOWN"
        data.seal(self.s); self.reject(self.s, "unknown location")

    def test_stale_and_timezone(self):
        with self.assertRaisesRegex(ValueError, "stale_input"):
            data.validate_snapshot(self.s, "2028-01-01T00:00:00+09:00")
        self.s["generated_at"] = "2026-09-10"
        data.seal(self.s); self.reject(self.s, "timezone")

    def test_provider_snapshot_mismatch(self):
        self.s["provider_snapshots"]["cfs"]["jobs"]["JOB_01"]["ready_at"] += 1
        data.seal(self.s); self.reject(self.s, "provider_snapshots")

    def test_invalid_capacity_intervals(self):
        for field, bad in [("server_capacity", 0), ("open_intervals", [[10, 5]]), ("open_intervals", [[0, 50], [40, 60]])]:
            s = copy.deepcopy(self.s); s["truth"]["locations"]["CFS_A"][field] = bad
            data.seal(s); self.reject(s, "CFS_A")

    def test_formal_seed_list_and_hash(self):
        cfg = json.loads((ROOT/"configs/formal.json").read_text())
        data.validate_config(cfg)
        self.assertEqual(cfg["seeds"], list(range(100)))
        self.assertEqual(cfg["seed_list_sha256"], hashlib.sha256("".join(f"{i}\n" for i in range(100)).encode()).hexdigest())
        cfg["formal_seeds"].pop()
        with self.assertRaisesRegex(ValueError, "formal_seeds"):
            data.validate_config(cfg)

    def test_defaults_are_frozen_across_seeds(self):
        # Input-generation check only, no optimization of a formal seed set.
        other = data.generate(self.cfg, 1)
        self.assertEqual(self.s["public_defaults"], other["public_defaults"])
        self.assertNotEqual(self.s["rng_streams"]["input"], self.s["rng_streams"]["optimizer"])
        self.assertEqual(self.s["public_defaults"], self.cfg["public_defaults"])


class MaskTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config(); self.s = data.generate(self.cfg, 0)

    def test_provider_mask_only_authorized_fields(self):
        baseline = data.observed(self.s, "NONE", self.cfg["optimizer"]).state
        for cid in data.COALITIONS:
            observed = data.observed(self.s, cid, self.cfg["optimizer"]).state
            letters = "" if cid == "NONE" else cid
            self.assertEqual(len(observed["jobs"]), 12); self.assertEqual(len(observed["trucks"]), 6)
            for i, job in enumerate(observed["jobs"]):
                for field in ("job_id", "origin_id", "destination_id", "window_start", "window_end", "priority"):
                    self.assertEqual(job[field], baseline["jobs"][i][field])
                for field, owner in [("ready_at", "C"), ("service_minutes_origin", "C"), ("service_minutes_destination", "T")]:
                    expected = self.s["truth"]["jobs"][i][field] if owner in letters else self.cfg["public_defaults"][field]
                    self.assertEqual(job[field], expected)
            for loc in baseline["locations"]:
                owner = "C" if loc.startswith("CFS") else "T"
                self.assertEqual(observed["locations"][loc], self.s["truth"]["locations"][loc] if owner in letters else baseline["locations"][loc])
            for field in ("trucks", "travel_times"):
                self.assertEqual(observed[field], self.s["truth"][field] if "R" in letters else baseline[field])

    def test_full_observed_equals_truth_but_is_detached(self):
        o = data.observed(self.s, "CTR", self.cfg["optimizer"])
        self.assertEqual(o.state, self.s["truth"])
        o.state["jobs"][0]["ready_at"] = 999
        self.assertNotEqual(o.state, self.s["truth"])

    def test_optimizer_hidden_truth_change_does_not_change_plan(self):
        o1 = data.observed(self.s, "NONE", self.cfg["optimizer"])
        self.s["truth"]["jobs"][0]["ready_at"] = 100
        self.s["provider_snapshots"] = data.provider_views(self.s["truth"])
        data.seal(self.s)
        o2 = data.observed(self.s, "NONE", self.cfg["optimizer"])
        self.assertEqual(o1, o2)
        self.assertEqual(optimizer.optimize(o1).routes, optimizer.optimize(o2).routes)
        self.assertEqual(list(inspect.signature(optimizer.optimize).parameters), ["observed"])
        self.assertNotIn("truth", optimizer.__dict__)
        with self.assertRaises(TypeError):
            optimizer.optimize(data.truth(self.s))
        with self.assertRaises(TypeError):
            evaluator.evaluate(o1, {})

    def test_unknown_coalition(self):
        with self.assertRaises(ValueError):
            data.observed(self.s, "BAD", self.cfg["optimizer"])


class SchedulerTests(unittest.TestCase):
    def test_known_wait_travel_operation(self):
        s = small_state(); s["jobs"][0]["ready_at"] = 10
        result = evaluator.evaluate(data.Truth(s), {"T1": ["J1"]})
        self.assertEqual((result["total_waiting_minutes"], result["total_travel_minutes"], result["total_operation_minutes"]), (10, 10, 20))
        self.assertEqual(result["completed_jobs"], 1)
        self.assertEqual(result["trace"][-1]["service_end"], 30)

    def test_capacity_and_arrival_tie_break(self):
        s = small_state()
        s["jobs"].append({**s["jobs"][0], "job_id": "J2"})
        s["trucks"].append({**s["trucks"][0], "truck_id": "T2"})
        result = simulate(s, {"T2": ["J2"], "T1": ["J1"]})
        origin = [e for e in result["trace"] if e["stage"] == 0]
        self.assertEqual([(e["truck_id"], e["service_start"]) for e in origin], [("T1", 0), ("T2", 5)])
        s["locations"]["CFS_A"]["server_capacity"] = 2
        result = simulate(s, {"T2": ["J2"], "T1": ["J1"]})
        self.assertEqual([e["service_start"] for e in result["trace"] if e["stage"] == 0], [0, 0])

    def test_open_intervals_no_straddle(self):
        self.assertEqual(service_slot(8, 0, 5, [0], [[0, 10], [20, 40]]), (20, 0))
        s = small_state(); s["locations"]["CFS_A"]["open_intervals"] = [[20, 100]]
        result = simulate(s, {"T1": ["J1"]})
        self.assertEqual(result["total_waiting_minutes"], 20)

    def test_hard_window_failure_without_time_penalty(self):
        s = small_state(); s["jobs"][0]["window_end"] = 16
        result = simulate(s, {"T1": ["J1"]})
        self.assertEqual(result["failed_jobs"], 1)
        self.assertEqual(result["completed_jobs"], 0)
        self.assertEqual(result["total_operation_minutes"], 10)

    def test_horizon_and_closed_location(self):
        s = small_state(); s["locations"]["TML_1"]["open_intervals"] = []
        self.assertEqual(simulate(s, {"T1": ["J1"]})["failed_jobs"], 1)
        s = small_state(); s["trucks"][0]["available_at"] = 99
        self.assertEqual(simulate(s, {"T1": ["J1"]})["failed_jobs"], 1)

    def test_duplicate_and_unknown_assignment(self):
        s = small_state()
        for routes in ({"T1": ["J1", "J1"]}, {"T1": ["BAD"]}, {"BAD": ["J1"]}):
            with self.assertRaises(ValueError):
                simulate(s, routes)

    def test_all_disabled_and_zero_jobs(self):
        s = small_state(); s["trucks"][0]["enabled"] = False
        plan = optimizer.optimize(data.Observed(s, config()["optimizer"]))
        self.assertEqual(plan.status, "unassigned")
        self.assertEqual(simulate(s, plan.routes)["unassigned_jobs"], 1)
        self.assertEqual(simulate(s, {"T1": ["J1"]})["failed_jobs"], 1)
        s["jobs"] = []
        plan = optimizer.optimize(data.Observed(s, config()["optimizer"]))
        self.assertEqual(plan.status, "success")
        self.assertEqual(simulate(s, plan.routes)["total_operation_minutes"], 0)

    def test_zero_travel(self):
        s = small_state()
        s["travel_times"] = {a: {b: 0 for b in s["locations"]} for a in s["locations"]}
        self.assertEqual(simulate(s, {"T1": ["J1"]})["total_operation_minutes"], 0)

    def test_timeout_and_iteration_status(self):
        s = small_state(); opts = config()["optimizer"]
        opts["timeout_seconds"] = 0
        self.assertEqual(optimizer.optimize(data.Observed(s, opts)).status, "timeout")
        opts.update(timeout_seconds=120, max_iterations=0)
        self.assertEqual(optimizer.optimize(data.Observed(s, opts)).status, "iteration_limit")

    def test_deterministic_insertion_tie(self):
        s = small_state(); s["trucks"].append({**s["trucks"][0], "truck_id": "T2"})
        p = optimizer.optimize(data.Observed(s, config()["optimizer"]))
        self.assertEqual(p.routes, {"T1": ["J1"], "T2": []})


class ValueTests(unittest.TestCase):
    def test_improvement_ratios_and_zero_division(self):
        self.assertEqual(value.metrics(100, 80)["time_reduction_pct"], 20)
        self.assertEqual(value.metrics(100, 80)["efficiency_index"], 125)
        self.assertIsNone(value.metrics(0, 0)["time_reduction_pct"])
        self.assertIsNone(value.metrics(0, 0)["efficiency_index"])
        self.assertEqual(value.metrics(0, 1)["time_reduction_reason"], "zero_baseline")
        self.assertEqual(value.metrics(1, 0)["efficiency_index_reason"], "zero_cost")

    def test_exact_toy_additive_dummy(self):
        g = {c: sum({"C": 6, "T": 3, "R": 0}[p] for p in ("" if c == "NONE" else c)) for c in data.COALITIONS}
        self.assertEqual(value.shapley(g), {"cfs": 6, "terminal": 3, "carrier": 0})

    def test_symmetry_unanimity_and_efficiency(self):
        g = {c: 9 if c == "CTR" else 0 for c in data.COALITIONS}
        self.assertEqual(value.shapley(g), dict(cfs=3, terminal=3, carrier=3))
        g["CR"] = -7
        self.assertTrue(value.close(sum(value.shapley(g).values()), 9))

    def test_mean_shapley_efficiency(self):
        games = [{c: (i+1)*len("" if c == "NONE" else c) for c in data.COALITIONS} for i in range(3)]
        means, full = value.aggregate(games)
        self.assertEqual(means, dict(cfs=2, terminal=2, carrier=2))
        self.assertEqual(sum(means.values()), full)

    def test_reward_activation_and_largest_remainder_tie(self):
        rows = value.rewards(dict(cfs=1, terminal=1, carrier=1), 3, True)
        self.assertEqual(sum(r["integer_reward_points"] for r in rows), 1000)
        self.assertEqual({r["participant_id"]: r["integer_reward_points"] for r in rows}, dict(carrier=334, cfs=333, terminal=333))
        self.assertTrue(all(r["reward_active"] for r in rows))

    def test_reward_negative_participant_preserved(self):
        rows = value.rewards(dict(cfs=-2, terminal=3, carrier=1), 2, True)
        cfs = next(r for r in rows if r["participant_id"] == "cfs")
        self.assertEqual(cfs["raw_shapley_minutes"], -2)
        self.assertEqual(cfs["integer_reward_points"], 0)
        self.assertEqual(sum(r["integer_reward_points"] for r in rows), 1000)

    def test_reward_inactive_cases(self):
        for phi, full, valid, reason in [(dict(cfs=1, terminal=1, carrier=1), 3, False, "formal_run_set_incomplete"),
                                         (dict(cfs=0, terminal=0, carrier=0), 0, True, "nonpositive_full_value"),
                                         (dict(cfs=-1, terminal=-1, carrier=-1), -3, True, "nonpositive_full_value"),
                                         (dict(cfs=0, terminal=0, carrier=0), 1, True, "no_positive_credit")]:
            rows = value.rewards(phi, full, valid)
            self.assertTrue(all(not r["reward_active"] and r["integer_reward_points"] == 0 and r["raw_reward_points"] == 0 for r in rows))
            self.assertEqual(rows[0]["reward_inactive_reason"], reason)

    def test_worsening_value_and_reward(self):
        rows = [dict(seed=0, snapshot_sha256="same", coalition_id=c, status="success", failed_jobs=0, unassigned_jobs=0,
                     total_operation_minutes=10 if c == "NONE" else 20) for c in data.COALITIONS]
        g = value.game(rows)
        self.assertEqual(g["CTR"], -10)
        phi = value.shapley(g)
        self.assertTrue(value.close(sum(phi.values()), -10))
        self.assertTrue(all(r["integer_reward_points"] == 0 for r in value.rewards(phi, -10, True)))

    def test_failure_unassignment_timeout_block_game(self):
        rows = [dict(seed=0, snapshot_sha256="same", coalition_id=c, status="success", failed_jobs=0,
                     unassigned_jobs=0, total_operation_minutes=10) for c in data.COALITIONS]
        for field, bad in [("failed_jobs", 1), ("unassigned_jobs", 1), ("status", "timeout"), ("snapshot_sha256", "different")]:
            changed = copy.deepcopy(rows); changed[-1][field] = bad; changed[-1]["total_operation_minutes"] = 0
            self.assertIsNone(value.game(changed))
        self.assertIsNone(value.game(rows[:-1]))

    def test_formal_run_set_validator_with_records_only(self):
        # 800 fabricated metadata records; ZERO optimization runs here.
        rows = [dict(seed=s, snapshot_sha256=f"hash-{s}", coalition_id=c, status="success", failed_jobs=0, unassigned_jobs=0) for s in range(100) for c in data.COALITIONS]
        self.assertTrue(value.formal_valid(rows))
        self.assertFalse(value.formal_valid(rows[:-1]))
        rows[-1]["failed_jobs"] = 1
        self.assertFalse(value.formal_valid(rows))


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = config()
        cls.forward = runner.run_seed(cls.cfg)
        cls.reverse = runner.run_seed(cls.cfg, order=tuple(reversed(data.COALITIONS)))

    def test_all_eight_success_complete_same_hash(self):
        rows = self.forward["coalition_results"]
        self.assertEqual(len(rows), 8)
        self.assertEqual({r["snapshot_sha256"] for r in rows}, {self.forward["snapshot"]["snapshot_sha256"]})
        self.assertTrue(all(r["status"] == "success" and r["failed_jobs"] == 0 and r["unassigned_jobs"] == 0 and r["completed_jobs"] == 12 for r in rows))
        for r in rows:
            self.assertEqual(r["total_operation_minutes"], r["total_waiting_minutes"] + r["total_travel_minutes"])

    def test_reverse_order_scientific_equality(self):
        self.assertEqual(data.canonical(runner.scientific(self.forward)), data.canonical(runner.scientific(self.reverse)))

    def test_smoke_reward_inactive_efficiency(self):
        summary = self.forward["summary"]
        self.assertFalse(summary["formal_run_set_valid"])
        self.assertEqual(summary["reward_inactive_reason"], "formal_run_set_incomplete")
        self.assertTrue(value.close(sum(summary["shapley_minutes"].values()), summary["full_value_minutes"]))
        self.assertEqual(sum(r["integer_reward_points"] for r in self.forward["rewards"]), 0)

    def test_duplicate_run_key(self):
        s = data.generate(self.cfg, 0)
        self.assertEqual(runner.run_key(s, "C"), runner.run_key(s, "C"))
        self.assertNotEqual(runner.run_key(s, "C"), runner.run_key(s, "T"))
        with self.assertRaisesRegex(ValueError, "duplicate run_key"):
            runner.run_seed(self.cfg, order=("NONE", "NONE"))

    def test_timeout_invalidates_whole_smoke(self):
        cfg = config(); cfg["optimizer"]["timeout_seconds"] = 0
        result = runner.run_seed(cfg)
        self.assertFalse(result["summary"]["valid"])
        self.assertIsNone(result["summary"]["shapley_minutes"])
        self.assertTrue(all(r["value_minutes"] is None for r in result["coalition_results"]))
        self.assertEqual(sum(r["integer_reward_points"] for r in result["rewards"]), 0)

    def test_disabled_truth_and_empty_jobs_pipeline(self):
        cfg = config()
        snapshot = data.generate(cfg, 0)
        for truck in snapshot["truth"]["trucks"]:
            truck["enabled"] = False
        snapshot["provider_snapshots"] = data.provider_views(snapshot["truth"])
        data.seal(snapshot)
        result = runner.run_seed(cfg, snapshot=snapshot)
        self.assertFalse(result["summary"]["valid"])
        self.assertIsNone(result["summary"]["full_value_minutes"])
        cfg["job_count"] = 0
        empty = runner.run_seed(cfg)
        self.assertTrue(empty["summary"]["valid"])
        self.assertEqual(empty["summary"]["full_value_minutes"], 0)
        self.assertTrue(all(r["time_reduction_pct"] is None and r["efficiency_index"] is None for r in empty["coalition_results"]))

    def test_snapshot_seed_identity_and_formal_cli_guard(self):
        from unittest.mock import patch
        from contextlib import redirect_stderr
        from io import StringIO
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            runner.run_seed(self.cfg, seed=1, snapshot=self.forward["snapshot"])
        with patch("sys.argv", ["socfai_incentive", "--config", str(ROOT/"configs/formal.json"),
                                "--output", str(ROOT/"outputs/must_not_exist")]), redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as exc:
                runner.main()
        self.assertEqual(exc.exception.code, 2)
        self.assertFalse((ROOT/"outputs/must_not_exist").exists())

    def test_package_contract_hashes_and_no_overwrite(self):
        output_root = ROOT / "outputs"
        output_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output_root) as temp:
            dest = Path(temp)/"run"
            runner.write_package(dest, self.cfg, self.forward, "unit fixture")
            required = "config.json scenario.json input_manifest.json coalition_results.csv contributions.csv rewards.csv summary.json validation_report.json run.log environment.txt commands.txt SHA256SUMS".split()
            self.assertTrue(all((dest/f).is_file() for f in required))
            for line in (dest/"SHA256SUMS").read_text().splitlines():
                h, n = line.split()
                self.assertEqual(hashlib.sha256((dest/n).read_bytes()).hexdigest(), h)
            with self.assertRaises(FileExistsError):
                runner.write_package(dest, self.cfg, self.forward, "do not overwrite")


if __name__ == "__main__":
    unittest.main()
