import copy
import hashlib
import inspect
import itertools
import json
import tempfile
import unittest
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from socfai_cy_v2 import contracts as c, schema, generator, observations, optimizer, evaluator, execution, value, runner

ROOT = Path(__file__).resolve().parents[1]

def toy(stacks=None, targets=("A",)):
    if stacks is None:
        stacks = [("S1", ["A", "B", "C"], False, 4),
                  ("S2", [], False, 4), ("S3", [], False, 4)]
    yard = dict(yard_id="TOY", equipment_open_intervals=[[0, 480]], move_service_minutes=2,
                stacks=[dict(stack_id=sid, block_id="B1", bay=i, row=1, max_tier=cap,
                             stack_bottom_to_top=list(items), restricted=restricted)
                        for i, (sid, items, restricted, cap) in enumerate(stacks, 1)])
    jobs = [dict(job_id=f"J{i:02d}", container_id=target, ready_at=0, window_start=0,
                 window_end=480, service_minutes=0, priority=1, work_status="ready")
            for i, target in enumerate(targets, 1)]
    trucks = [dict(truck_id=f"T{i:02d}", target_container_id=target, current_location_id="L",
                   available_at=0, estimated_arrival_at=0, enabled=True, travel_time_minutes=0)
              for i, target in enumerate(targets, 1)]
    return dict(horizon_minutes=480, layout_visible=True, yard=yard, jobs=jobs, trucks=trucks)

def game(weights):
    return {cid: sum(weights.get(p, 0) for p in c.members(cid)) for cid in c.COALITIONS}

class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = generator.generate(0)

    def setUp(self):
        self.s = copy.deepcopy(self.baseline)

    def reject(self, regex):
        schema.seal(self.s)
        with self.assertRaisesRegex(ValueError, regex):
            schema.validate_snapshot(self.s, c.AS_OF)

    def test_canonical_encoding_known_bytes(self):
        self.assertEqual(c.canonical({"z": 1, "a": "한"}), '{"a":"한","z":1}\n'.encode())
        self.assertEqual(c.canonical({"a": 2, "b": 1}), c.canonical({"b": 1, "a": 2}))
        with self.assertRaises(ValueError):
            c.canonical({"a": float("nan")})

    def test_snapshot_hash_blank_self_includes_metadata(self):
        original = self.s["snapshot_sha256"]
        hand = copy.deepcopy(self.s)
        hand["snapshot_sha256"] = ""
        raw = (json.dumps(hand, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        self.assertEqual(original, hashlib.sha256(raw).hexdigest())
        self.s["snapshot_sha256"] = "f" * 64
        self.assertEqual(schema.snapshot_hash(self.s), original)
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            schema.validate_snapshot(self.s, c.AS_OF)
        self.s["yard"]["move_service_minutes"] += 1
        self.assertNotEqual(schema.snapshot_hash(self.s), original)

    def test_required_fields_and_versions(self):
        for key in list(c.VERSIONS) + ["scenario_id", "jobs", "public_defaults", "snapshot_sha256"]:
            with self.subTest(key=key):
                invalid = copy.deepcopy(self.s)
                del invalid[key]
                with self.assertRaises(ValueError):
                    schema.validate_snapshot(invalid, c.AS_OF)
        self.s["schema_version"] = "v1"
        self.reject("schema_version")

    def test_duplicate_container(self):
        nonempty = [s for s in self.s["yard"]["stacks"] if s["stack_bottom_to_top"]]
        nonempty[1]["stack_bottom_to_top"][0] = nonempty[0]["stack_bottom_to_top"][0]
        self.reject("duplicate container")

    def test_duplicate_stack_coordinate_and_id(self):
        for field_group in [("stack_id",), ("block_id", "bay", "row")]:
            self.s = copy.deepcopy(self.baseline)
            first, second = self.s["yard"]["stacks"][:2]
            for key in field_group:
                second[key] = first[key]
            self.reject("duplicate stack")

    def test_duplicate_jobs_trucks_and_targets(self):
        for group, key in [("jobs", "job_id"), ("jobs", "container_id"),
                           ("trucks", "truck_id"), ("trucks", "target_container_id")]:
            self.s = copy.deepcopy(self.baseline)
            self.s[group][1][key] = self.s[group][0][key]
            self.reject("duplicate")

    def test_unknown_and_missing_references(self):
        self.s["jobs"][0]["container_id"] = "UNKNOWN"
        self.reject("unknown container")
        self.s = copy.deepcopy(self.baseline)
        self.s["trucks"][0]["target_container_id"] = "UNKNOWN"
        self.reject("unknown truck")
        self.s = copy.deepcopy(self.baseline)
        self.s["trucks"].pop()
        self.reject("missing truck")

    def test_negative_time_bool_float_and_invalid_status(self):
        for group, field, bad in [("jobs", "ready_at", -1), ("jobs", "service_minutes", -1),
            ("jobs", "priority", True), ("jobs", "window_end", 1.5),
            ("trucks", "available_at", -1), ("trucks", "travel_time_minutes", -1),
            ("trucks", "enabled", 1), ("jobs", "work_status", "unknown")]:
            self.s = copy.deepcopy(self.baseline)
            self.s[group][0][field] = bad
            self.reject("invalid")

    def test_windows_arrivals_and_disabled_truck(self):
        self.s["jobs"][0]["window_start"] = 479
        self.reject("window")
        self.s = copy.deepcopy(self.baseline)
        self.s["trucks"][0].update(available_at=100, estimated_arrival_at=0)
        self.reject("arrival")
        self.s = copy.deepcopy(self.baseline)
        self.s["trucks"][0]["enabled"] = False
        self.reject("enabled truck")

    def test_capacity_topology_and_restricted_type(self):
        self.s["yard"]["stacks"][0]["max_tier"] = 0
        self.reject("max_tier")
        self.s = copy.deepcopy(self.baseline)
        self.s["yard"]["stacks"][0]["stack_bottom_to_top"] = [f"X{i}" for i in range(5)]
        self.reject("capacity")
        self.s = copy.deepcopy(self.baseline)
        self.s["yard"]["stacks"][0]["restricted"] = "false"
        self.reject("restricted")
        self.s = copy.deepcopy(self.baseline)
        self.s["yard"]["stacks"][0]["bay"] = 10
        self.reject("topology")

    def test_intervals_horizon_and_move_duration(self):
        for key, bad in [("equipment_open_intervals", [[10, 9]]),
                         ("equipment_open_intervals", [[0, 30], [20, 50]]),
                         ("move_service_minutes", -1)]:
            self.s = copy.deepcopy(self.baseline)
            self.s["yard"][key] = bad
            self.reject("invalid")

    def test_stale_future_timezone_and_time_order(self):
        for as_of in ("2026-09-29T00:00:00+00:00", "2026-09-27T00:00:00+00:00"):
            with self.assertRaisesRegex(ValueError, "stale_input"):
                schema.validate_snapshot(self.s, as_of)
        self.s["observed_at"] = "2026-09-28"
        self.reject("timezone")
        self.s = copy.deepcopy(self.baseline)
        self.s["generated_at"] = c.UNTIL
        self.reject("timestamp order")

    def test_identity_data_grade_and_public_defaults(self):
        for key, bad in [("scenario_id", "WRONG"), ("data_grade", "real"),
                         ("seed_list_sha256", "0" * 64), ("master_seed", True)]:
            self.s = copy.deepcopy(self.baseline)
            self.s[key] = bad
            self.reject("invalid|mismatch")
        self.s = copy.deepcopy(self.baseline)
        self.s["public_defaults"]["cfs"]["ready_at"] = 1
        self.reject("public defaults")

class GenerationTests(unittest.TestCase):
    def test_deterministic_stream_seeds_and_independence(self):
        seeds = [generator.stream_seed(0, name) for name in c.STREAMS]
        self.assertEqual(len(set(seeds)), 5)
        self.assertEqual(seeds[0], int(hashlib.sha256(b"0layout").hexdigest(), 16))
        first, second = [], []
        a, b = generator.generate(0, first), generator.generate(0, second)
        self.assertEqual(c.canonical(a), c.canonical(b))
        self.assertEqual(first, second)
        self.assertEqual(a["public_defaults"], generator.generate(1)["public_defaults"])

    def test_required_scale_burial_interaction_and_journal(self):
        journal = []
        s = generator.generate(0, journal)
        self.assertEqual(len(s["yard"]["stacks"]), 16)
        self.assertEqual(sum(len(x["stack_bottom_to_top"]) for x in s["yard"]["stacks"]), 32)
        self.assertEqual(len(s["jobs"]), 6)
        self.assertGreaterEqual(journal[-1]["buried_targets"], 3)
        self.assertTrue(journal[-1]["accepted"])
        self.assertTrue(generator.interaction(s)[0])
        self.assertEqual([x["attempt"] for x in journal], list(range(1, len(journal) + 1)))
        self.assertTrue(all(x["rejection_reasons"] for x in journal[:-1]))

    def test_generation_failed_retains_all_100_attempts(self):
        journal = []
        with patch.object(generator, "interaction", return_value=(False, 720)):
            with self.assertRaises(generator.GenerationFailed) as error:
                generator.generate(0, journal)
        self.assertEqual(len(journal), 100)
        self.assertEqual(error.exception.journal, journal)
        self.assertTrue(all(not e["accepted"] and e["rejection_reasons"] for e in journal))

    def test_seed_set_and_unknown_stream_rejection(self):
        for seed in (-1, 100, True, 1.5):
            with self.assertRaises(ValueError):
                generator.generate(seed)
        with self.assertRaises(ValueError):
            generator.stream_seed(0, "other")

class MaskAndOptimizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = generator.generate(0)

    def test_provider_allowlist_all_eight(self):
        for cid in c.COALITIONS:
            state = observations.observe(self.s, cid, c.AS_OF).state
            self.assertEqual(len(state["jobs"]), 6)
            self.assertEqual(len(state["trucks"]), 6)
            self.assertEqual(set(state), {"horizon_minutes", "layout_visible", "yard", "jobs", "trucks"})
            for actual, masked in zip(self.s["jobs"], state["jobs"]):
                for key in c.JOB_FIELDS:
                    self.assertEqual(masked[key], actual[key] if "C" in c.members(cid) else c.defaults()["cfs"][key])
            for actual, masked in zip(self.s["trucks"], state["trucks"]):
                for key in c.TRUCK_FIELDS:
                    self.assertEqual(masked[key], actual[key] if "R" in c.members(cid) else c.defaults()["carrier"][key])
            if "Y" in c.members(cid):
                self.assertEqual(state["yard"], self.s["yard"])
            else:
                self.assertEqual(state["yard"], c.defaults()["yard"])
                self.assertNotIn("stacks", state["yard"])

    def test_projection_ignores_private_extras_and_is_detached(self):
        s = copy.deepcopy(self.s)
        s["private_metadata"] = "SENTINEL"
        s["jobs"][0]["private_extra"] = "SENTINEL"
        s["trucks"][0]["private_extra"] = "SENTINEL"
        s["yard"]["private_extra"] = "SENTINEL"
        schema.seal(s)
        observed = observations.observe(s, "CYR", c.AS_OF)
        self.assertNotIn(b"SENTINEL", observed.state_json)
        state = observed.state
        state["jobs"][0]["ready_at"] += 1
        self.assertNotEqual(state, observed.state)
        self.assertEqual(observed.state["jobs"][0]["ready_at"], s["jobs"][0]["ready_at"])

    def test_hidden_truth_perturbation_leaves_observation_and_plan_identical(self):
        original = observations.observe(self.s, "NONE", c.AS_OF)
        s = copy.deepcopy(self.s)
        s["jobs"][0]["ready_at"] += 10
        s["trucks"][0]["travel_time_minutes"] += 20
        s["trucks"][0]["current_location_id"] = "SECRET_LOCATION"
        s["yard"]["move_service_minutes"] += 2
        s["yard"]["stacks"].reverse()
        schema.seal(s)
        hidden = observations.observe(s, "NONE", c.AS_OF)
        self.assertEqual(original.state_json, hidden.state_json)
        a, b = optimizer.optimize(original), optimizer.optimize(hidden)
        self.assertEqual(a.order, b.order)
        self.assertEqual(a.estimated_objective, b.estimated_objective)

    def test_truth_objects_dicts_and_callbacks_rejected(self):
        for wrong in (self.s, schema.truth(self.s, c.AS_OF), lambda: self.s):
            with self.assertRaises(TypeError):
                optimizer.optimize(wrong)
        self.assertEqual(tuple(inspect.signature(optimizer.optimize).parameters), ("observed",))
        with self.assertRaises(TypeError):
            evaluator.evaluate(observations.observe(self.s, "Y", c.AS_OF), None)

    def test_forged_observed_provider_leaks_rejected(self):
        observed = observations.observe(self.s, "NONE", c.AS_OF)
        for change in (lambda s: s.update(snapshot_sha256="hidden"),
                       lambda s: s["jobs"][0].update(ready_at=7),
                       lambda s: s["trucks"][0].update(current_location_id="hidden"),
                       lambda s: s["yard"].update(move_service_minutes=7)):
            s = observed.state
            change(s)
            with self.assertRaises(ValueError):
                optimizer.optimize(c.Observed("NONE", c.canonical(s)))

    def test_720_permutations_and_lexicographic_tie(self):
        observed = observations.observe(self.s, "NONE", c.AS_OF)
        with patch.object(optimizer, "simulate", wraps=execution.simulate) as calls:
            plan = optimizer.optimize(observed)
        orders = [tuple(call.args[1]) for call in calls.call_args_list]
        self.assertEqual(len(orders), 720)
        self.assertEqual(set(orders), set(itertools.permutations(sorted(plan.order))))
        self.assertEqual(plan.permutations_evaluated, 720)
        self.assertEqual(plan.order, tuple(sorted(plan.order)))
        self.assertEqual(plan.status, "success")

    def test_full_observed_estimate_equals_truth_replay(self):
        plan = optimizer.optimize(observations.observe(self.s, "CYR", c.AS_OF))
        actual = evaluator.evaluate(schema.truth(self.s, c.AS_OF), plan)
        self.assertEqual(plan.estimated_objective, execution.objective(actual, plan.order))

    def test_objective_priority_and_unknown_coalition(self):
        r = dict(failed_jobs=0, total_rehandle_moves=1, total_waiting_minutes=999, total_operation_minutes=999)
        other = dict(r, total_rehandle_moves=2, total_waiting_minutes=0, total_operation_minutes=0)
        self.assertLess(execution.objective(r, ("J",)), execution.objective(other, ("J",)))
        self.assertLess(execution.objective(other, ("J",)), execution.objective(dict(r, failed_jobs=1), ("J",)))
        with self.assertRaises(ValueError):
            observations.observe(self.s, "T", c.AS_OF)

class ExecutionTests(unittest.TestCase):
    def test_known_stack_two_rehandles_and_capacity_tie(self):
        result = execution.simulate(toy(), ("J01",))
        self.assertEqual(result["total_rehandle_moves"], 2)
        self.assertEqual(result["total_moves"], 3)
        self.assertEqual(result["completed_jobs"], 1)
        self.assertEqual(result["total_operation_minutes"], 6)
        self.assertEqual([x["container_id"] for x in result["trace"]], ["C", "B", "A"])
        self.assertEqual([x["destination_stack"] for x in result["trace"]], ["S2", "S3", None])

    def test_remaining_capacity_precedes_stack_id(self):
        state = toy([("S1", ["A", "B"], False, 4), ("S2", ["X"], False, 4), ("S3", [], False, 4)])
        r = execution.simulate(state, ("J01",))
        self.assertEqual(r["trace"][0]["destination_stack"], "S3")

    def test_order_changes_rehandles_known_toy(self):
        state = toy([("S1", ["A", "B"], False, 4), ("S2", [], False, 4)], ("A", "B"))
        a = execution.simulate(state, ("J01", "J02"))
        b = execution.simulate(state, ("J02", "J01"))
        self.assertEqual((a["total_rehandle_moves"], b["total_rehandle_moves"]), (1, 0))

    def test_restricted_occupied_source_allowed_destination_forbidden(self):
        state = toy([("S1", ["A", "B"], True, 4), ("S2", [], True, 4), ("S3", [], False, 4)])
        r = execution.simulate(state, ("J01",))
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["trace"][0]["destination_stack"], "S3")
        state["yard"]["stacks"][2]["restricted"] = True
        failed = execution.simulate(state, ("J01",))
        self.assertEqual(failed["status"], "infeasible")
        self.assertEqual(failed["failures"][0]["reason"], "no_relocation_capacity")

    def test_full_yard_infeasible_and_partial_moves_preserved(self):
        state = toy([("S1", ["A", "B", "C"], False, 3), ("S2", ["X"], False, 2)])
        result = execution.simulate(state, ("J01",))
        self.assertEqual(result["status"], "infeasible")
        self.assertEqual(result["total_rehandle_moves"], 1)
        self.assertEqual(result["completed_jobs"], 0)
        self.assertEqual(result["failed_jobs"], 1)
        self.assertEqual(result["final_stacks"][1]["stack_bottom_to_top"], ["X", "C"])

    def test_known_ready_arrival_wait_travel_and_makespan(self):
        state = toy([("S1", ["A"], False, 4)])
        state["jobs"][0].update(ready_at=20, service_minutes=5)
        state["trucks"][0].update(available_at=3, travel_time_minutes=7, estimated_arrival_at=12)
        r = execution.simulate(state, ("J01",))
        self.assertEqual((r["total_waiting_minutes"], r["total_travel_minutes"],
                          r["total_operation_minutes"]), (8, 7, 27))

    def test_equipment_gap_and_no_straddling(self):
        state = toy([("S1", ["A"], False, 4)])
        state["yard"]["equipment_open_intervals"] = [[0, 1], [10, 20]]
        r = execution.simulate(state, ("J01",))
        self.assertEqual((r["total_waiting_minutes"], r["total_operation_minutes"]), (10, 12))
        self.assertEqual(execution.service_slot(1, 0, [[0, 1]]), 1)
        self.assertIsNone(execution.service_slot(1, 1, [[0, 1]]))

    def test_window_blocked_disabled_closed_and_horizon_failures(self):
        for change in (lambda s: s["jobs"][0].update(window_end=1),
                       lambda s: s["jobs"][0].update(work_status="blocked"),
                       lambda s: s["trucks"][0].update(enabled=False),
                       lambda s: s["yard"].update(equipment_open_intervals=[]),
                       lambda s: s["jobs"][0].update(service_minutes=481)):
            state = toy()
            change(state)
            self.assertEqual(execution.simulate(state, ("J01",))["status"], "infeasible")

    def test_input_immutability_and_unknown_duplicate_orders(self):
        state = toy(targets=("A", "B"))
        before = c.canonical(state)
        execution.simulate(state, ("J01", "J02"))
        self.assertEqual(c.canonical(state), before)
        for bad in [("J01", "J01"), ("UNKNOWN", "J02"), ("J01",)]:
            with self.assertRaises(ValueError):
                execution.simulate(state, bad)

class ValueTests(unittest.TestCase):
    def test_exact_additive_signed_dummy_game(self):
        self.assertEqual(value.exact_shapley(game({"C": 3, "Y": -2, "R": 0})),
                         {"C": Fraction(3), "Y": Fraction(-2), "R": Fraction(0)})

    def test_symmetry_unanimity_and_efficiency(self):
        unanimity = {cid: int(cid == "CYR") for cid in c.COALITIONS}
        phi = value.exact_shapley(unanimity)
        self.assertEqual(phi, {p: Fraction(1, 3) for p in c.PLAYERS})
        self.assertEqual(sum(phi.values()), 1)

    def test_shapley_matches_independent_permutation_marginals(self):
        g = dict(zip(c.COALITIONS, [0, 2, -1, 3, 7, 1, 9, 11]))
        hand = {p: Fraction(0) for p in c.PLAYERS}
        for order in itertools.permutations(c.PLAYERS):
            prefix = []
            for p in order:
                before = value.coalition_id(prefix)
                prefix.append(p)
                hand[p] += Fraction(g[value.coalition_id(prefix)] - g[before], 6)
        self.assertEqual(value.exact_shapley(dict(reversed(list(g.items())))), hand)
        self.assertEqual(sum(hand.values()), 11)

    def test_missing_game_nonzero_baseline_invalid(self):
        with self.assertRaises(ValueError):
            value.exact_shapley({"NONE": 0})
        g = game({})
        g["NONE"] = 1
        with self.assertRaises(ValueError):
            value.exact_shapley(g)

    def test_reward_active_largest_remainder_and_id_tie(self):
        result = value.rewards({p: 1 for p in c.PLAYERS}, 3, True)
        self.assertTrue(result["active"])
        self.assertEqual(result["points"], {"C": 334, "Y": 333, "R": 333})
        self.assertEqual(sum(result["points"].values()), 1000)

    def test_reward_negative_and_zero_preserved_without_penalty(self):
        phi = dict(C=-1, Y=2, R=0)
        self.assertEqual(value.rewards(phi, 1, True)["points"], dict(C=0, Y=1000, R=0))
        self.assertEqual(phi["C"], -1)

    def test_reward_inactive_zero_negative_and_incomplete(self):
        for phi, full, valid in [(dict(C=0, Y=0, R=0), 0, True),
                                 (dict(C=-2, Y=1, R=0), -1, True),
                                 (dict(C=1, Y=1, R=1), 3, False)]:
            r = value.rewards(phi, full, valid)
            self.assertFalse(r["active"])
            self.assertEqual(sum(r["points"].values()), 0)
        with self.assertRaises(ValueError):
            value.rewards(dict(C=1, Y=1, R=1), 4, True)

class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = runner.expected_config()
        cls.forward = runner.run_seed(cls.cfg)
        cls.reverse = runner.run_seed(cls.cfg, tuple(reversed(c.COALITIONS)))

    def test_eight_success_one_snapshot_unique_run_keys(self):
        rows = self.forward["coalition_results"]
        self.assertEqual(len(rows), 8)
        self.assertEqual(len({r["run_key"] for r in rows}), 8)
        self.assertEqual(len({r["snapshot_sha256"] for r in rows}), 1)
        self.assertTrue(all(r["status"] == "success" and r["completed_jobs"] == 6
                            and r["failed_jobs"] == 0 and r["permutations_evaluated"] == 720 for r in rows))
        self.assertEqual({r["coalition_id"] for r in rows}, set(c.COALITIONS))

    def test_value_shapley_efficiency_and_smoke_reward_inactive(self):
        result = self.forward
        baseline = result["coalition_results"][0]["total_rehandle_moves"]
        for r in result["coalition_results"]:
            self.assertEqual(r["value_moves"], baseline - r["total_rehandle_moves"])
        summary = result["summary"]
        self.assertEqual(sum(Fraction(x) for x in summary["shapley_exact"].values()), summary["full_value_moves"])
        self.assertFalse(summary["reward"]["active"])
        self.assertEqual(summary["reward"]["inactive_reason"], "formal_run_set_incomplete")
        self.assertEqual(sum(r["integer_reward_points"] for r in result["rewards"]), 0)

    def test_reverse_scientific_bytes_and_runtime_normalization(self):
        a, b = c.canonical(runner.scientific(self.forward)), c.canonical(runner.scientific(self.reverse))
        self.assertEqual(a, b)
        self.assertEqual(hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest())
        scientific = runner.scientific(self.forward)
        self.assertNotIn(b'"runtime_ms":', c.canonical(scientific))
        self.assertNotIn(b'"generated_at":', c.canonical(scientific))

    def test_run_key_exact_specification_and_version_identity(self):
        s = self.forward["input_snapshot"]
        text = "|".join(str(x) for x in [s["schema_version"], s["scenario_id"],
            s["snapshot_sha256"], 0, "CY", s["algorithm_version"], s["calculation_version"], s["reward_rule_version"]])
        self.assertEqual(runner.run_key(s, "CY"), hashlib.sha256(text.encode()).hexdigest())
        modified = dict(s, reward_rule_version="other")
        self.assertNotEqual(runner.run_key(s, "CY"), runner.run_key(modified, "CY"))

    def test_duplicate_and_incomplete_prevent_optimizer_calls(self):
        with patch.object(runner, "optimize") as call:
            for order in [("NONE",) * 8, c.COALITIONS[:-1]]:
                with self.assertRaises(ValueError):
                    runner.run_seed(self.cfg, order)
            call.assert_not_called()

    def test_invalid_seed_blocks_all_values_contributions_and_rewards(self):
        s = copy.deepcopy(self.forward["input_snapshot"])
        s["jobs"][0]["work_status"] = "blocked"
        schema.seal(s)
        result = runner.run_seed(self.cfg, snapshot=s)
        self.assertEqual(result["summary"]["status"], "invalid_seed")
        self.assertIsNone(result["summary"]["shapley_exact"])
        self.assertTrue(all(r["value_moves"] is None for r in result["coalition_results"]))
        self.assertEqual(sum(r["integer_reward_points"] for r in result["rewards"]), 0)

    def test_mixed_snapshots_duplicate_keys_and_invalid_metrics_rejected(self):
        for change in (lambda r: r[0].update(snapshot_sha256="0" * 64),
                       lambda r: r[0].update(run_key=r[1]["run_key"]),
                       lambda r: r[0].update(total_rehandle_moves=-1)):
            rows = copy.deepcopy(self.forward["coalition_results"])
            change(rows)
            with self.assertRaises(ValueError):
                value.analyze(rows)

    def test_formal_config_guard_before_output_and_optimizer(self):
        cfg = runner.expected_config()
        cfg["seeds"] = list(range(100))
        with tempfile.TemporaryDirectory() as temp, patch.object(runner, "optimize") as call:
            output = Path(temp) / "forbidden"
            with self.assertRaises(ValueError):
                runner.execute(cfg, output)
            self.assertFalse(output.exists())
            call.assert_not_called()

    def test_new_package_contract_checksums_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "smoke"
            result = runner.execute(self.cfg, output)
            expected = {"config.json", "input_snapshot.json", "public_defaults.json", "summary.json",
                        "validation_report.json", "scientific.json", "generation_journal.json",
                        "coalition_results.csv", "contributions.csv", "rewards.csv", "run.log",
                        "completed_runs.jsonl", "plans.jsonl", "evaluation_traces.jsonl", "SHA256SUMS.txt"}
            self.assertEqual({p.name for p in output.iterdir()}, expected)
            for line in (output / "SHA256SUMS.txt").read_text().splitlines():
                h, name = line.split("  ")
                self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), h)
            self.assertEqual((output / "scientific.json").read_bytes(), c.canonical(runner.scientific(result)))
            events = [json.loads(line) for line in (output / "run.log").read_bytes().splitlines()]
            self.assertEqual(sum(e["event"] == "optimizer_call" for e in events), 8)
            self.assertEqual(sum(e["event"] == "truth_evaluation_call" for e in events), 8)
            with patch.object(runner, "optimize") as call:
                with self.assertRaises(FileExistsError):
                    runner.execute(self.cfg, output)
                call.assert_not_called()

    def test_unexpected_failure_retains_partial_runs_and_traceback(self):
        real = runner.optimize
        calls = 0
        def fail_second(observed):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic injected failure")
            return real(observed)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "partial"
            with patch.object(runner, "optimize", side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    runner.execute(self.cfg, output)
            self.assertEqual(len((output / "completed_runs.jsonl").read_bytes().splitlines()), 1)
            self.assertIn("synthetic injected failure", (output / "failure.txt").read_text())
            self.assertEqual(json.loads((output / "run.log").read_bytes().splitlines()[-1])["event"], "aborted")

    def test_generation_failure_journal_preserved_in_package(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(generator, "interaction", return_value=(False, 720)):
            output = Path(temp) / "failed_generation"
            with self.assertRaises(generator.GenerationFailed):
                runner.execute(self.cfg, output)
            events = [json.loads(line) for line in (output / "run.log").read_bytes().splitlines()]
            self.assertEqual(sum(e["event"] == "generation_candidate" for e in events), 100)
            self.assertIn("generation_failed", (output / "failure.txt").read_text())

if __name__ == "__main__":
    unittest.main()
