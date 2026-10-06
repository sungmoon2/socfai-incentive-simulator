import copy
import csv
import hashlib
import itertools
import json
import math
import random
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch
from socfai_cy_v2 import contracts as c, schema, formal, formal_statistics as stats, runner
from socfai_cy_v2.generator import GenerationFailed

ROOT = Path(__file__).resolve().parents[1]
CONFIG_RAW = (ROOT / "configs/cy_v2_formal.json").read_bytes()

def snapshot(seed):
    """Handwritten fixture, never a call to the actual generator."""
    stacks = [dict(stack_id=f"B{block}-{bay}-{row}", block_id=f"B{block}",
                   bay=bay, row=row, max_tier=4,
                   stack_bottom_to_top=[f"X{2*i}", f"X{2*i+1}"], restricted=False)
              for i, (block, bay, row) in enumerate(itertools.product((1, 2), range(1, 5), (1, 2)))]
    jobs = [dict(job_id=f"J{i}", container_id=f"X{i}", ready_at=0, window_start=0,
                 window_end=480, service_minutes=0, priority=1, work_status="ready") for i in range(6)]
    trucks = [dict(truck_id=f"T{i}", target_container_id=f"X{i}", current_location_id="FIXTURE",
                   available_at=0, estimated_arrival_at=0, enabled=True, travel_time_minutes=0) for i in range(6)]
    return schema.seal(dict(c.VERSIONS, scenario_id=f"CYV2-S{seed:03d}",
        snapshot_id=f"CYV2-S{seed:03d}-INPUT", snapshot_sha256="", master_seed=seed,
        generated_at=c.AS_OF, observed_at=c.AS_OF, valid_until=c.UNTIL,
        data_grade=c.DATA_GRADE, seed_set_id=c.SEED_SET_ID, seed_list_sha256=c.SEED_LIST_SHA256,
        horizon_minutes=480, public_defaults=c.defaults(),
        yard=dict(yard_id="FIXTURE", stacks=stacks, equipment_open_intervals=[[0,480]], move_service_minutes=1),
        jobs=jobs, trucks=trucks))

def fabricated(zero=False):
    snapshots = [snapshot(seed) for seed in range(100)]
    rows = []
    for s in snapshots:
        seed = s["master_seed"]
        weights = dict(C=seed+1, Y=-2, R=3) if not zero else dict(C=0,Y=0,R=0)
        for cid in c.COALITIONS:
            cost = 300 - sum(weights[p] for p in c.members(cid))
            key = runner.run_key(s,cid)
            rows.append(dict(run_id=key, run_key=key, scenario_id=s["scenario_id"],
                snapshot_sha256=s["snapshot_sha256"], seed=seed, coalition_id=cid,
                participants=list(c.members(cid)), algorithm_name=c.VERSIONS["algorithm_name"],
                algorithm_version=c.VERSIONS["algorithm_version"], total_rehandle_moves=cost,
                total_moves=cost+6, completed_jobs=6, failed_jobs=0, total_waiting_minutes=20,
                total_travel_minutes=6, total_operation_minutes=42, runtime_ms=0.1, status="success",
                optimizer_status="success", permutations_evaluated=720))
    return dict(scenarios=snapshots, coalition_results=rows, generation_journal=[],
                plans=[], evaluations=[], failures=[], generator_calls=100,
                optimizer_calls=800, truth_evaluation_calls=800)

class FormalStatisticsTests(unittest.TestCase):
    def test_known_description_and_n_minus_one_std(self):
        got=stats.describe([1,2,3,4])
        self.assertEqual(got["n"],4)
        self.assertEqual(got["mean_exact"],"5/2")
        self.assertEqual(got["median"],2.5)
        self.assertAlmostEqual(got["sample_std"],math.sqrt(5/3))
        self.assertEqual((got["min"],got["max"]),(1,4))

    def test_linear_percentile_known_boundaries(self):
        self.assertEqual(stats.percentile([30,0,20,10],".25"),Fraction(15,2))
        self.assertEqual(stats.percentile([30,0,20,10],".975"),Fraction(117,4))
        self.assertEqual(stats.percentile([2,7],0),2)
        self.assertEqual(stats.percentile([2,7],1),7)
        with self.assertRaises(ValueError):stats.percentile([],0.5)

    def test_paired_bootstrap_independent_rng_indices_and_quantile(self):
        data={"full":[1,9,-2],"C":[2,18,-4],"Y":[0,0,0],"R":[-1,-9,2]}
        got=stats.paired_bootstrap(data)
        other=stats.paired_bootstrap(dict(reversed(list(data.items()))))
        self.assertEqual(got,other)
        rng=random.Random(20260928)
        digest=hashlib.sha256()
        sampled=[]
        for _ in range(10000):
            indices=[rng.randrange(3) for _ in range(3)]
            digest.update((json.dumps(indices,separators=(",",":"))+"\n").encode())
            sampled.append(Fraction(sum(data["full"][i] for i in indices),3))
        sampled.sort()
        def independent(p):
            position=Fraction(9999)*p
            i=position.numerator//position.denominator
            return sampled[i]+(sampled[min(i+1,9999)]-sampled[i])*(position-i)
        self.assertEqual(got["index_stream_sha256"],digest.hexdigest())
        self.assertEqual(got["intervals"]["full"]["lower_exact"],str(independent(Fraction(25,1000))))
        self.assertEqual(got["intervals"]["full"]["upper_exact"],str(independent(Fraction(975,1000))))
        self.assertEqual(Fraction(got["intervals"]["C"]["lower_exact"]),2*independent(Fraction(25,1000)))

    def test_statistics_reject_null_nonfinite_and_unpaired(self):
        for bad in ([1,None],[1,float("nan")],[1,float("inf")],[1,True],[1]):
            with self.assertRaises(ValueError):stats.describe(bad)
        with self.assertRaises(ValueError):stats.paired_bootstrap({"a":[1,2],"b":[1,2,3]})

class FormalAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records=fabricated()
        with patch.object(formal,"rewards",wraps=formal.rewards) as reward:
            cls.analysis=formal.analyze(copy.deepcopy(cls.records),formal.expected_config())
            cls.reward_count=reward.call_count
            cls.reward_args=reward.call_args.args

    def test_fixed_config_raw_pin_seed_coalition_and_bootstrap(self):
        cfg=formal.validate_raw_config(CONFIG_RAW)
        self.assertEqual(hashlib.sha256(CONFIG_RAW).hexdigest(),formal.CONFIG_SHA256)
        self.assertEqual(cfg["seeds"],list(range(100)))
        self.assertEqual(cfg["coalitions"],list(c.COALITIONS))
        self.assertEqual((cfg["bootstrap_seed"],cfg["bootstrap_replicates"]),(20260928,10000))
        with self.assertRaises(ValueError):formal.validate_raw_config(CONFIG_RAW+b" ")
        cfg["seeds"].pop()
        with self.assertRaises(ValueError):formal.validate_config(cfg)

    def test_fabricated_100_games_exact_mean_efficiency(self):
        s=self.analysis["summary"]
        self.assertTrue(s["formal_run_set_valid"])
        self.assertEqual(s["mean_shapley_exact"],dict(C="101/2",Y="-2",R="3"))
        self.assertEqual(s["mean_full_value_exact"],"103/2")
        self.assertEqual(sum(Fraction(x) for x in s["mean_shapley_exact"].values()),Fraction(s["mean_full_value_exact"]))
        self.assertEqual(len(self.analysis["seed_contributions"]),300)
        self.assertTrue(all(not r["diagnostic_only"] for r in self.analysis["seed_contributions"]))

    def test_reward_applied_once_to_means_not_seed_points(self):
        self.assertEqual(self.reward_count,1)
        self.assertEqual(self.reward_args,({"C":Fraction(101,2),"Y":Fraction(-2),"R":Fraction(3)},Fraction(103,2),True))
        self.assertEqual(self.analysis["summary"]["reward"]["points"],dict(C=944,Y=0,R=56))
        self.assertEqual(self.analysis["summary"]["integer_reward_total"],1000)

    def test_missing_duplicate_generation_infeasible_block_every_aggregate(self):
        mutations=[
            lambda d:d["coalition_results"].pop(),
            lambda d:d["coalition_results"].append(copy.deepcopy(d["coalition_results"][0])),
            lambda d:d["failures"].append(dict(seed=0,status="generation_failed",reason="fixture")),
            lambda d:d["coalition_results"][0].update(status="infeasible",failed_jobs=1),
            lambda d:d["coalition_results"][0].update(permutations_evaluated=719),
            lambda d:d["coalition_results"][0].update(total_rehandle_moves=None),
            lambda d:d["coalition_results"][0].update(total_waiting_minutes=float("nan")),
            lambda d:d["coalition_results"][0].update(runtime_ms=float("inf")),
            lambda d:d["coalition_results"][0].update(snapshot_sha256="0"*64),
            lambda d:d["coalition_results"][0].update(run_key=d["coalition_results"][1]["run_key"]),
            lambda d:d["scenarios"].pop(),
            lambda d:d.update(optimizer_calls=799)]
        for mutate in mutations:
            d=copy.deepcopy(self.records);mutate(d)
            with patch.object(formal,"paired_bootstrap") as boot,patch.object(formal,"rewards") as rew:
                got=formal.analyze(d,formal.expected_config())
            self.assertFalse(got["summary"]["formal_run_set_valid"])
            self.assertIsNone(got["summary"]["mean_full_value_moves"])
            self.assertFalse(got["descriptive_stats"]["available"])
            self.assertFalse(got["bootstrap_ci"]["available"])
            self.assertEqual(got["summary"]["integer_reward_total"],0)
            self.assertTrue(got["validation_report"]["issues"])
            self.assertTrue(all(r["diagnostic_only"] for r in got["seed_contributions"]))
            boot.assert_not_called();rew.assert_not_called()

    def test_shapley_efficiency_failure_blocks_statistics(self):
        with patch.object(formal,"exact_shapley",return_value=dict(C=0,Y=0,R=0)),patch.object(formal,"paired_bootstrap") as boot:
            got=formal.analyze(copy.deepcopy(self.records),formal.expected_config())
        self.assertFalse(got["summary"]["formal_run_set_valid"])
        boot.assert_not_called()

    def test_valid_zero_value_still_ci_but_no_reward_call(self):
        with patch.object(formal,"rewards") as reward:
            got=formal.analyze(fabricated(zero=True),formal.expected_config())
        self.assertTrue(got["summary"]["formal_run_set_valid"])
        self.assertTrue(got["bootstrap_ci"]["available"])
        self.assertFalse(got["summary"]["reward"]["active"])
        reward.assert_not_called()

class FormalCollectorTests(unittest.TestCase):
    def small(self,reverse=False,generation_failure=False):
        def gen(seed,journal):
            journal.append(dict(event="generation_candidate",seed=seed,attempt=1,accepted=not generation_failure))
            if generation_failure and seed==0:raise GenerationFailed(list(journal))
            return snapshot(seed)
        def plan(observed):
            return c.Plan(tuple(j["job_id"] for j in observed.state["jobs"]),(0,0,0,0,tuple(f"J{i}" for i in range(6))),720,"success",0.1)
        def evaluate(actual,plan):
            return dict(total_rehandle_moves=0,total_moves=6,completed_jobs=6,failed_jobs=0,
                        total_waiting_minutes=0,total_travel_minutes=0,total_operation_minutes=6,
                        status="success",trace=[],failures=[],final_stacks=[])
        with patch.object(formal,"generate",side_effect=gen) as g,patch.object(formal,"optimize",side_effect=plan) as o,patch.object(formal,"evaluate",side_effect=evaluate) as e:
            result=formal.collect(formal.expected_config(),tuple(reversed(c.COALITIONS)) if reverse else c.COALITIONS,fixture_seeds=[0,1])
        return result,(g.call_count,o.call_count,e.call_count)

    def test_fixture_counts_unique_keys_one_snapshot_each(self):
        result,calls=self.small()
        self.assertEqual(calls,(2,16,16))
        self.assertEqual(len({r["run_key"] for r in result["coalition_results"]}),16)
        for seed in [0,1]:
            self.assertEqual(len({r["snapshot_sha256"] for r in result["coalition_results"] if r["seed"]==seed}),1)

    def test_fixture_reverse_scientific_bytes_equal(self):
        a,_=self.small();b,_=self.small(True)
        self.assertEqual(c.canonical(formal.scientific(a,formal.expected_config())),
                         c.canonical(formal.scientific(b,formal.expected_config())))

    def test_generation_failure_preserved_without_seed_replacement(self):
        result,calls=self.small(generation_failure=True)
        self.assertEqual(calls,(2,8,8))
        self.assertEqual(result["failures"][0]["seed"],0)
        with patch.object(formal,"paired_bootstrap") as boot:
            got=formal.analyze(result,formal.expected_config())
        self.assertFalse(got["summary"]["formal_run_set_valid"])
        boot.assert_not_called()

    def test_duplicate_seed_and_coalition_fail_before_calls(self):
        with patch.object(formal,"generate") as gen:
            with self.assertRaises(ValueError):formal.collect(formal.expected_config(),fixture_seeds=[0,0])
            with self.assertRaises(ValueError):formal.collect(formal.expected_config(),order=["NONE"]*8)
            gen.assert_not_called()

class FormalOutputTests(unittest.TestCase):
    def test_fabricated_full_output_rows_sort_hashes_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(formal,"collect",return_value=fabricated()):
            output=Path(tmp)/"formal"
            result=formal.execute(CONFIG_RAW,output,"fixture")
            required=set("config.json source_context.json scenarios.jsonl generation_journal.jsonl plans.jsonl evaluation_traces.jsonl coalition_results.csv seed_contributions.csv aggregate_contributions.csv rewards.csv descriptive_stats.json bootstrap_ci.json summary.json validation_report.json scientific.json run.log SHA256SUMS.txt".split())
            self.assertEqual({p.name for p in output.iterdir()},required)
            for filename,count in [("coalition_results.csv",800),("seed_contributions.csv",300),("aggregate_contributions.csv",3),("rewards.csv",3)]:
                with (output/filename).open() as f:rows=list(csv.DictReader(f))
                self.assertEqual(len(rows),count)
            self.assertEqual(len((output/"scenarios.jsonl").read_bytes().splitlines()),100)
            for line in (output/"scenarios.jsonl").read_bytes().splitlines():
                self.assertEqual(c.canonical(json.loads(line)),line+b"\n")
            for line in (output/"SHA256SUMS.txt").read_text().splitlines():
                h,n=line.split("  ")
                self.assertEqual(hashlib.sha256((output/n).read_bytes()).hexdigest(),h)
            self.assertEqual((output/"scientific.json").read_bytes(),c.canonical(formal.scientific(result,formal.expected_config())))
            with self.assertRaises(FileExistsError):formal.execute(CONFIG_RAW,output,"fixture")

    def test_unexpected_failure_retains_partial_checkpoint_and_checksum(self):
        def abort(config,order,emit,checkpoint):
            checkpoint("fixture_before_failure",dict(seed=0))
            raise RuntimeError("injected fixture failure")
        with tempfile.TemporaryDirectory() as tmp,patch.object(formal,"collect",side_effect=abort):
            output=Path(tmp)/"failed"
            with self.assertRaisesRegex(RuntimeError,"injected"):formal.execute(CONFIG_RAW,output,"fixture")
            self.assertTrue((output/"progress.jsonl").exists())
            self.assertIn("injected fixture failure",(output/"failure.txt").read_text())
            self.assertFalse(json.loads((output/"summary.json").read_text())["formal_run_set_valid"])
            self.assertTrue((output/"SHA256SUMS.txt").exists())

    def test_source_guard_rejects_wrong_commit_branch_dirty_or_config(self):
        commit="a"*40
        bad_responses=[
            [b"wrong\n"],
            [(commit+"\n").encode(),b"wrong\n"],
            [(commit+"\n").encode(),b"main\n",b" M file\n"],
            [(commit+"\n").encode(),b"main\n",b"",CONFIG_RAW+b" "]]
        for responses in bad_responses:
            with patch.object(formal.subprocess,"check_output",side_effect=responses):
                with self.assertRaises(ValueError):formal.source_guard(commit)
        with self.assertRaises(ValueError):formal.source_guard("short")

    def test_source_guard_accepts_clean_main_and_pinned_config(self):
        commit="a"*40
        with patch.object(formal.subprocess,"check_output",side_effect=[
                (commit+"\n").encode(),b"main\n",b"",CONFIG_RAW]) as git:
            formal.source_guard(commit)
        self.assertEqual(git.call_args.args[0],
                         ["git","show",commit+":configs/cy_v2_formal.json"])

    def test_smoke_config_and_existing_scientific_pin_preserved(self):
        cfg=json.loads((ROOT/"configs/cy_v2_smoke.json").read_bytes())
        self.assertEqual(cfg,runner.expected_config())
        result=runner.run_seed(cfg)
        self.assertEqual(hashlib.sha256(c.canonical(runner.scientific(result))).hexdigest(),
                         "b84de3f307cbef158993bd19e052a5cf46512756aa3e1844db99029f5c54098f")

if __name__=="__main__":
    unittest.main()
