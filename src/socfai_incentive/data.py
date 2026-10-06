"""Validated truth snapshots and provider-authorized observed projections."""

import copy
import hashlib
import json
import random
from dataclasses import dataclass
from datetime import datetime

COALITIONS = ("NONE", "C", "T", "R", "CT", "CR", "TR", "CTR")
PROVIDERS = {"C": "cfs", "T": "terminal", "R": "carrier"}
SEEDS = list(range(100))
GENERATOR_VERSION = "1.0.0"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def seed_hash(seeds):
    return hashlib.sha256("".join(f"{n}\n" for n in seeds).encode()).hexdigest()


def stream_seed(seed, name):
    return int(digest([seed, name, GENERATOR_VERSION]), 16)


def snapshot_hash(snapshot):
    return digest({k: v for k, v in snapshot.items() if k != "snapshot_sha256"})


def seal(snapshot):
    snapshot["snapshot_sha256"] = snapshot_hash(snapshot)
    return snapshot


@dataclass(frozen=True)
class Truth:
    state: dict


@dataclass(frozen=True)
class Observed:
    state: dict
    options: dict


def require(ok, path, reason):
    if not ok:
        raise ValueError(f"{path}: {reason}")


def integer(x, path, minimum=0):
    require(type(x) is int and x >= minimum, path, "expected nonnegative integer")


def timestamp(value, path):
    require(isinstance(value, str), path, "expected ISO 8601 string")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{path}: invalid ISO 8601") from exc
    require(result.tzinfo is not None, path, "timezone required")
    return result


def fields(obj, names, path):
    require(isinstance(obj, dict), path, "expected object")
    for name in names.split():
        require(name in obj, f"{path}.{name}", "missing field")


def validate_state(state):
    fields(state, "locations jobs trucks travel_times horizon_minutes", "state")
    integer(state["horizon_minutes"], "horizon_minutes", 1)
    locs = state["locations"]
    require(isinstance(locs, dict) and bool(locs), "locations", "nonempty mapping required")
    for loc, spec in locs.items():
        require(isinstance(loc, str) and bool(loc), "location_id", "string required")
        fields(spec, "server_capacity open_intervals", f"locations.{loc}")
        integer(spec["server_capacity"], f"{loc}.server_capacity", 1)
        intervals = spec["open_intervals"]
        require(isinstance(intervals, list), f"{loc}.open_intervals", "list required")
        previous = -1
        for interval in intervals:
            require(isinstance(interval, list) and len(interval) == 2, loc, "interval pair required")
            a, b = interval
            integer(a, loc); integer(b, loc)
            require(previous <= a < b <= state["horizon_minutes"], loc, "unordered/overlapping interval")
            previous = b
    for group, identity, required in [
        ("jobs", "job_id", "job_id origin_id destination_id ready_at service_minutes_origin service_minutes_destination window_start window_end priority"),
        ("trucks", "truck_id", "truck_id start_location_id available_at enabled"),
    ]:
        require(isinstance(state[group], list), group, "list required")
        seen = set()
        for item in state[group]:
            fields(item, required, group)
            key = item[identity]
            require(isinstance(key, str) and bool(key), identity, "string required")
            require(key not in seen, identity, "duplicate ID")
            seen.add(key)
            for field, val in item.items():
                if field in ("origin_id", "destination_id", "start_location_id"):
                    require(val in locs, field, "unknown location")
            if group == "jobs":
                for field in ("ready_at", "service_minutes_origin", "service_minutes_destination", "window_start", "window_end", "priority"):
                    integer(item[field], f"{key}.{field}")
                require(item["window_start"] <= item["window_end"] <= state["horizon_minutes"], key, "invalid window")
            else:
                integer(item["available_at"], f"{key}.available_at")
                require(type(item["enabled"]) is bool, f"{key}.enabled", "boolean required")
    matrix = state["travel_times"]
    require(isinstance(matrix, dict) and set(matrix) == set(locs), "travel_times", "location rows mismatch")
    for a, row in matrix.items():
        require(isinstance(row, dict) and set(row) == set(locs), a, "location columns mismatch")
        for b, minutes in row.items():
            integer(minutes, f"travel_times.{a}.{b}")
            require(a != b or minutes == 0, a, "diagonal must be zero")


def provider_views(state):
    return {
        "cfs": {
            "jobs": {j["job_id"]: {f: j[f] for f in ("ready_at", "service_minutes_origin")} for j in state["jobs"]},
            "locations": {k: copy.deepcopy(v) for k, v in state["locations"].items() if k.startswith("CFS_")},
        },
        "terminal": {
            "jobs": {j["job_id"]: {"service_minutes_destination": j["service_minutes_destination"]} for j in state["jobs"]},
            "locations": {k: copy.deepcopy(v) for k, v in state["locations"].items() if k.startswith("TML_")},
        },
        "carrier": {"trucks": copy.deepcopy(state["trucks"]), "travel_times": copy.deepcopy(state["travel_times"])},
    }


def validate_config(cfg):
    fields(cfg, "scenario_id generated_at freshness_as_of valid_until horizon_minutes job_count seeds seed_list_sha256 formal_seeds public_defaults optimizer", "config")
    require(cfg["formal_seeds"] == SEEDS, "formal_seeds", "must be exactly 0..99")
    require(cfg["seed_list_sha256"] == seed_hash(SEEDS), "seed_list_sha256", "mismatch")
    require(isinstance(cfg["seeds"], list) and len(set(cfg["seeds"])) == len(cfg["seeds"]), "seeds", "unique list required")
    for n in cfg["seeds"]:
        integer(n, "seed"); require(n in SEEDS, "seed", "outside frozen set")
    integer(cfg["horizon_minutes"], "horizon_minutes", 1)
    integer(cfg["job_count"], "job_count")
    require(timestamp(cfg["generated_at"], "generated_at") <= timestamp(cfg["freshness_as_of"], "freshness_as_of") <= timestamp(cfg["valid_until"], "valid_until"), "config", "stale_input")
    opt = cfg["optimizer"]
    fields(opt, "max_iterations timeout_seconds", "optimizer")
    integer(opt["max_iterations"], "max_iterations")
    require(opt["max_iterations"] <= 100, "max_iterations", "maximum is 100")
    require(type(opt["timeout_seconds"]) in (int, float) and 0 <= opt["timeout_seconds"] < float("inf"), "timeout_seconds", "finite nonnegative number required")


def generate(cfg, seed):
    validate_config(cfg)
    integer(seed, "seed")
    require(seed in SEEDS, "seed", "outside frozen set")
    rng = random.Random(stream_seed(seed, "input"))
    horizon = cfg["horizon_minutes"]
    names = ["CFS_A", "CFS_B", "TML_1", "TML_2", "TML_3"]
    locations = {n: {"server_capacity": 1 if n.startswith("CFS") else 2,
                     "open_intervals": [[rng.randrange(0, 21), horizon]]} for n in names}
    jobs = []
    for i in range(cfg["job_count"]):
        jobs.append(dict(job_id=f"JOB_{i+1:02d}", origin_id=names[i % 2],
                         destination_id=names[2 + rng.randrange(3)], ready_at=rng.randrange(0, 91),
                         service_minutes_origin=rng.randrange(5, 16), service_minutes_destination=rng.randrange(5, 16),
                         window_start=0, window_end=horizon, priority=1 + i % 3))
    trucks = [dict(truck_id=f"TRK_{i+1:02d}", start_location_id=names[rng.randrange(5)],
                   available_at=rng.randrange(0, 21), enabled=True) for i in range(6)]
    travel = {a: {b: 0 if a == b else rng.randrange(8, 26) for b in names} for a in names}
    state = dict(locations=locations, jobs=jobs, trucks=trucks, travel_times=travel, horizon_minutes=horizon)
    s = dict(schema_version="1.0.0", scenario_id=cfg["scenario_id"], snapshot_id=f"synthetic-{seed:03d}",
             generated_at=cfg["generated_at"], valid_until=cfg["valid_until"], data_grade="synthetic",
             master_seed=seed, generator_version=GENERATOR_VERSION, seed_set_id="fixed-0-99-v1",
             seed_list_sha256=cfg["seed_list_sha256"], public_default_version="1.0.0", horizon_minutes=horizon,
             truth=state, public_defaults=copy.deepcopy(cfg["public_defaults"]), provider_snapshots=provider_views(state),
             rng_streams={name: str(stream_seed(seed, name)) for name in ("input", "optimizer", "evaluation")})
    seal(s)
    validate_snapshot(s, cfg["freshness_as_of"])
    return s


def validate_snapshot(s, as_of):
    fields(s, "schema_version scenario_id snapshot_id snapshot_sha256 generated_at data_grade master_seed generator_version seed_set_id seed_list_sha256 public_default_version horizon_minutes valid_until truth public_defaults provider_snapshots rng_streams", "snapshot")
    for f in ("schema_version", "scenario_id", "snapshot_id", "generator_version", "seed_set_id", "public_default_version"):
        require(isinstance(s[f], str) and bool(s[f]), f, "nonempty string required")
    require(s["schema_version"] == "1.0.0", "schema_version", "unsupported")
    require(s["data_grade"] == "synthetic", "data_grade", "only synthetic input authorized")
    integer(s["master_seed"], "master_seed")
    require(s["seed_list_sha256"] == seed_hash(SEEDS), "seed_list_sha256", "mismatch")
    require(timestamp(s["generated_at"], "generated_at") <= timestamp(as_of, "as_of") <= timestamp(s["valid_until"], "valid_until"), "snapshot", "stale_input")
    require(s["snapshot_sha256"] == snapshot_hash(s), "snapshot_sha256", "snapshot mismatch")
    validate_state(s["truth"])
    require(s["horizon_minutes"] == s["truth"]["horizon_minutes"], "horizon_minutes", "inconsistent")
    require(s["provider_snapshots"] == provider_views(s["truth"]), "provider_snapshots", "snapshot mismatch")
    # Validate public defaults by projecting them, never averaging hidden truth.
    validate_state(observed(s, "NONE", {"max_iterations": 100, "timeout_seconds": 120}).state)


def observed(snapshot, coalition, options):
    require(coalition in COALITIONS, "coalition_id", "unknown coalition")
    d = snapshot["public_defaults"]
    fields(d, "ready_at service_minutes_origin service_minutes_destination server_capacity open_intervals start_location_id available_at enabled travel_minutes", "public_defaults")
    truth = snapshot["truth"]
    # Only public task/resource identities and task contract are copied here.
    state = {
        "horizon_minutes": snapshot["horizon_minutes"],
        "locations": {n: {"server_capacity": d["server_capacity"], "open_intervals": copy.deepcopy(d["open_intervals"])} for n in truth["locations"]},
        "jobs": [{**{f: j[f] for f in ("job_id", "origin_id", "destination_id", "window_start", "window_end", "priority")},
                  **{f: d[f] for f in ("ready_at", "service_minutes_origin", "service_minutes_destination")}} for j in truth["jobs"]],
        "trucks": [{"truck_id": t["truck_id"], **{f: d[f] for f in ("start_location_id", "available_at", "enabled")}} for t in truth["trucks"]],
        "travel_times": {a: {b: 0 if a == b else d["travel_minutes"] for b in truth["locations"]} for a in truth["locations"]},
    }
    members = "" if coalition == "NONE" else coalition
    for letter in members:
        view = snapshot["provider_snapshots"][PROVIDERS[letter]]
        if letter == "R":
            state["trucks"] = copy.deepcopy(view["trucks"])
            state["travel_times"] = copy.deepcopy(view["travel_times"])
        else:
            state["locations"].update(copy.deepcopy(view["locations"]))
            for job in state["jobs"]:
                job.update(copy.deepcopy(view["jobs"][job["job_id"]]))
    return Observed(state, copy.deepcopy(options))


def truth(snapshot):
    return Truth(copy.deepcopy(snapshot["truth"]))
