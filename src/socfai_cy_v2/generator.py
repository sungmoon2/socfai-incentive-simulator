"""Versioned independent RNG streams; candidate rejection is always journaled."""
import hashlib
import itertools
import random
from .contracts import (VERSIONS, STREAMS, AS_OF, UNTIL, DATA_GRADE, SEED_SET_ID,
                        SEED_LIST_SHA256, defaults)
from .schema import integer, require, seal, state_from, validate_snapshot
from .execution import simulate

class GenerationFailed(ValueError):
    def __init__(self, journal):
        super().__init__("generation_failed after 100 candidates")
        self.journal = journal

def stream_seed(master_seed, name):
    if name not in STREAMS:
        raise ValueError("unknown RNG stream")
    return int(hashlib.sha256(f"{master_seed}{name}".encode("ascii")).hexdigest(), 16)

def candidate(seed, streams):
    stacks = [dict(stack_id=f"B{b}-BAY{bay:02d}-R{row}", block_id=f"B{b}",
                   bay=bay, row=row, max_tier=4, stack_bottom_to_top=[], restricted=False)
              for b in (1, 2) for bay in range(1, 5) for row in (1, 2)]
    containers = [f"CT{i:03d}" for i in range(1, 33)]
    shuffled = list(containers)
    streams["layout"].shuffle(shuffled)
    for cid in shuffled:
        available = [s for s in stacks if len(s["stack_bottom_to_top"]) < 4]
        streams["layout"].choice(available)["stack_bottom_to_top"].append(cid)
    targets = sorted(streams["targets"].sample(containers, 6))
    for index in streams["restrictions"].sample(range(16), 2):
        stacks[index]["restricted"] = True
    jobs, trucks = [], []
    for index, cid in enumerate(targets, 1):
        ready = streams["cfs"].randint(0, 60)
        jobs.append(dict(job_id=f"J{index:02d}", container_id=cid, ready_at=ready,
                         window_start=0, window_end=480,
                         service_minutes=streams["cfs"].randint(5, 12),
                         priority=streams["cfs"].randint(1, 3),
                         work_status=streams["cfs"].choice(["ready", "planned"])))
        available = streams["carrier"].randint(0, 20)
        travel = streams["carrier"].randint(5, 30)
        trucks.append(dict(truck_id=f"T{index:02d}", target_container_id=cid,
                           current_location_id=f"SYNTH_LOC_{streams['carrier'].randint(1, 4)}",
                           available_at=available, estimated_arrival_at=available + travel + streams["carrier"].randint(0, 10),
                           enabled=True, travel_time_minutes=travel))
    scenario = f"CYV2-S{seed:03d}"
    return dict(VERSIONS, scenario_id=scenario, snapshot_id=scenario + "-INPUT",
                snapshot_sha256="", generated_at=AS_OF, observed_at=AS_OF, valid_until=UNTIL,
                data_grade=DATA_GRADE, master_seed=seed, seed_set_id=SEED_SET_ID,
                seed_list_sha256=SEED_LIST_SHA256, horizon_minutes=480,
                yard=dict(yard_id="SYNTH_CY", stacks=stacks,
                          equipment_open_intervals=[[0, 210], [230, 480]],
                          move_service_minutes=streams["layout"].randint(3, 6)),
                jobs=jobs, trucks=trucks, public_defaults=defaults())

def interaction(snapshot):
    """Require two successful retrieval orders with different relocation counts."""
    state = state_from(snapshot)
    costs = set()
    count = 0
    for order in itertools.permutations(sorted(j["job_id"] for j in state["jobs"])):
        result = simulate(state, order, checked=True)
        count += 1
        if result["status"] == "success":
            costs.add(result["total_rehandle_moves"])
        if len(costs) > 1:
            return True, count
    return False, count

def generate(seed, journal=None):
    integer(seed, "master_seed")
    require(seed < 100, "seed outside frozen set")
    journal = [] if journal is None else journal
    streams = {name: random.Random(stream_seed(seed, name)) for name in STREAMS}
    for attempt in range(1, 101):
        snapshot = candidate(seed, streams)
        targets = {j["container_id"] for j in snapshot["jobs"]}
        buried = sum(cid in targets for s in snapshot["yard"]["stacks"]
                     for cid in s["stack_bottom_to_top"][:-1])
        varying, orders = interaction(snapshot) if buried >= 3 else (False, 0)
        reasons = (["fewer_than_three_buried_targets"] if buried < 3 else
                   ["no_successful_order_interaction"] if not varying else [])
        journal.append(dict(event="generation_candidate", seed=seed, attempt=attempt,
                            buried_targets=buried, interaction_orders_checked=orders,
                            accepted=not reasons, rejection_reasons=reasons))
        if not reasons:
            snapshot["generation"] = dict(accepted_attempt=attempt,
                rng_stream_seeds={name: str(stream_seed(seed, name)) for name in STREAMS})
            seal(snapshot)
            validate_snapshot(snapshot, AS_OF)
            return snapshot
    raise GenerationFailed(list(journal))
