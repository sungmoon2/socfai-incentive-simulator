"""Strict synthetic input validation and truth boundary."""
import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from .contracts import (VERSIONS, DATA_GRADE, SEED_SET_ID, SEED_LIST_SHA256,
                        JOB_FIELDS, TRUCK_FIELDS, canonical, defaults)

def require(ok, message):
    if not ok:
        raise ValueError(message)

def integer(value, name, minimum=0):
    require(type(value) is int and value >= minimum, f"invalid {name}")

def identifier(value, name):
    require(isinstance(value, str) and bool(value) and "|" not in value, f"invalid {name}")

def fields(obj, names, label):
    require(isinstance(obj, dict), f"invalid {label}")
    for key in names:
        require(key in obj, f"missing {label}.{key}")

def timestamp(value):
    require(isinstance(value, str), "invalid timestamp")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    require(dt.tzinfo is not None and dt.utcoffset() is not None, "timestamp requires timezone")
    return dt

def intervals(items, horizon):
    require(isinstance(items, list), "invalid equipment intervals")
    previous = -1
    for pair in items:
        require(isinstance(pair, list) and len(pair) == 2, "invalid equipment interval")
        a, b = pair
        integer(a, "interval start")
        integer(b, "interval end")
        require(previous <= a <= b <= horizon, "invalid or overlapping equipment intervals")
        previous = b

def validate_state(state):
    fields(state, ("horizon_minutes", "layout_visible", "yard", "jobs", "trucks"), "state")
    horizon = state["horizon_minutes"]
    integer(horizon, "horizon_minutes", 1)
    require(type(state["layout_visible"]) is bool, "invalid layout visibility")
    yard = state["yard"]
    fields(yard, ("equipment_open_intervals", "move_service_minutes"), "yard")
    intervals(yard["equipment_open_intervals"], horizon)
    integer(yard["move_service_minutes"], "move_service_minutes")
    containers = set()
    if state["layout_visible"]:
        fields(yard, ("yard_id", "stacks"), "yard")
        identifier(yard["yard_id"], "yard_id")
        require(isinstance(yard["stacks"], list) and yard["stacks"], "empty yard")
        coords, ids = set(), set()
        for stack in yard["stacks"]:
            fields(stack, ("stack_id", "block_id", "bay", "row", "max_tier",
                           "stack_bottom_to_top", "restricted"), "stack")
            identifier(stack["stack_id"], "stack_id")
            identifier(stack["block_id"], "block_id")
            integer(stack["bay"], "bay", 1)
            integer(stack["row"], "row", 1)
            integer(stack["max_tier"], "max_tier", 1)
            require(type(stack["restricted"]) is bool, "invalid restricted")
            coord = (stack["block_id"], stack["bay"], stack["row"])
            require(coord not in coords and stack["stack_id"] not in ids, "duplicate stack coordinate or ID")
            coords.add(coord)
            ids.add(stack["stack_id"])
            contents = stack["stack_bottom_to_top"]
            require(isinstance(contents, list), "invalid stack contents")
            require(len(contents) <= stack["max_tier"], "capacity exceeded")
            for container in contents:
                identifier(container, "container_id")
                require(container not in containers, "duplicate container")
                containers.add(container)
    else:
        require(set(yard) == {"expected_blockers_per_target", "equipment_open_intervals",
                              "move_service_minutes"}, "hidden yard fields leaked")
        integer(yard["expected_blockers_per_target"], "expected_blockers_per_target")
    require(isinstance(state["jobs"], list) and isinstance(state["trucks"], list), "invalid jobs/trucks")
    jobs, targets = set(), set()
    for job in state["jobs"]:
        fields(job, ("job_id", "container_id") + JOB_FIELDS, "job")
        identifier(job["job_id"], "job_id")
        identifier(job["container_id"], "container_id")
        require(job["job_id"] not in jobs and job["container_id"] not in targets, "duplicate job or target")
        jobs.add(job["job_id"])
        targets.add(job["container_id"])
        if state["layout_visible"]:
            require(job["container_id"] in containers, "unknown container reference")
        for key in JOB_FIELDS[:-1]:
            integer(job[key], key)
        require(job["window_start"] <= job["ready_at"] <= job["window_end"] <= horizon, "invalid job window")
        require(job["work_status"] in ("ready", "planned", "blocked"), "invalid work_status")
    truck_ids, truck_targets = set(), set()
    for truck in state["trucks"]:
        fields(truck, ("truck_id", "target_container_id") + TRUCK_FIELDS, "truck")
        identifier(truck["truck_id"], "truck_id")
        identifier(truck["current_location_id"], "current_location_id")
        require(truck["truck_id"] not in truck_ids, "duplicate truck")
        target = truck["target_container_id"]
        require(target in targets, "unknown truck target")
        require(target not in truck_targets, "duplicate truck target")
        truck_ids.add(truck["truck_id"])
        truck_targets.add(target)
        for key in ("available_at", "estimated_arrival_at", "travel_time_minutes"):
            integer(truck[key], key)
        require(truck["estimated_arrival_at"] >= truck["available_at"], "invalid arrival")
        require(type(truck["enabled"]) is bool, "invalid enabled")
    require(truck_targets == targets, "missing truck reference")

def snapshot_hash(snapshot):
    value = copy.deepcopy(snapshot)
    value["snapshot_sha256"] = ""
    return hashlib.sha256(canonical(value)).hexdigest()

def seal(snapshot):
    snapshot["snapshot_sha256"] = snapshot_hash(snapshot)
    return snapshot

def state_from(snapshot):
    return copy.deepcopy({k: snapshot[k] for k in ("horizon_minutes", "yard", "jobs", "trucks")}) | {"layout_visible": True}

def validate_snapshot(snapshot, as_of):
    required = tuple(VERSIONS) + (
        "scenario_id", "snapshot_id", "snapshot_sha256", "generated_at", "observed_at",
        "valid_until", "data_grade", "master_seed", "seed_set_id", "seed_list_sha256",
        "horizon_minutes", "yard", "jobs", "trucks", "public_defaults")
    fields(snapshot, required, "snapshot")
    for key, value in VERSIONS.items():
        require(snapshot[key] == value, f"unsupported {key}")
    integer(snapshot["master_seed"], "master_seed")
    require(snapshot["master_seed"] < 100, "seed outside frozen set")
    expected = f"CYV2-S{snapshot['master_seed']:03d}"
    require(snapshot["scenario_id"] == expected and snapshot["snapshot_id"] == expected + "-INPUT", "identity mismatch")
    require(snapshot["seed_set_id"] == SEED_SET_ID and snapshot["seed_list_sha256"] == SEED_LIST_SHA256, "seed set mismatch")
    require(snapshot["data_grade"] == DATA_GRADE, "invalid data_grade")
    require(snapshot["public_defaults"] == defaults(), "public defaults mismatch")
    times = [timestamp(snapshot[k]) for k in ("generated_at", "observed_at", "valid_until")]
    require(times[0] <= times[1] <= times[2], "invalid timestamp order")
    require(times[1] <= timestamp(as_of) <= times[2], "stale_input")
    require(snapshot["snapshot_sha256"] == snapshot_hash(snapshot), "snapshot hash mismatch")
    validate_state(state_from(snapshot))
    require(snapshot["horizon_minutes"] == 480, "invalid production horizon")
    require(len(snapshot["jobs"]) == len(snapshot["trucks"]) == 6, "requires six jobs and trucks")
    require(all(t["enabled"] for t in snapshot["trucks"]), "retrieval requires enabled truck")
    stacks = snapshot["yard"]["stacks"]
    required_coords = {(f"B{b}", bay, row) for b in (1, 2) for bay in range(1, 5) for row in (1, 2)}
    require({(s["block_id"], s["bay"], s["row"]) for s in stacks} == required_coords, "invalid production topology")
    require(all(s["max_tier"] == 4 for s in stacks), "invalid production max tier")
    require(sum(len(s["stack_bottom_to_top"]) for s in stacks) == 32, "requires 32 containers")

@dataclass(frozen=True)
class Truth:
    state_json: bytes

    @property
    def state(self):
        return json.loads(self.state_json)

def truth(snapshot, as_of):
    validate_snapshot(snapshot, as_of)
    return Truth(canonical(state_from(snapshot)))
