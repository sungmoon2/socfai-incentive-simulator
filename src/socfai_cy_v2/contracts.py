"""Frozen constants, byte encoding, and observed-only types."""
import hashlib
import json
from dataclasses import dataclass

PLAYERS = ("C", "Y", "R")
COALITIONS = ("NONE", "C", "Y", "R", "CY", "CR", "YR", "CYR")
PARTICIPANTS = {"C": "CFS", "Y": "CY operator", "R": "carrier"}
VERSIONS = {
    "schema_version": "cy-v2.0",
    "generator_version": "cy-gen-v1.0",
    "public_default_version": "cy-public-v1.0",
    "algorithm_name": "deterministic-exact-retrieval-order",
    "algorithm_version": "cy-opt-v1.0",
    "calculation_version": "cy-value-v1.0",
    "reward_rule_version": "cy-reward-v1.0",
}
SEED_SET_ID = "cy-v2-formal-0-99"
SEED_LIST_SHA256 = hashlib.sha256("".join(f"{i}\n" for i in range(100)).encode()).hexdigest()
AS_OF = "2026-09-28T00:00:00+00:00"
UNTIL = "2026-09-28T08:00:00+00:00"
DATA_GRADE = "synthetic_with_partner_schema_reference"
STREAMS = ("layout", "targets", "cfs", "carrier", "restrictions")
JOB_FIELDS = ("ready_at", "window_start", "window_end", "service_minutes", "priority", "work_status")
TRUCK_FIELDS = ("current_location_id", "available_at", "estimated_arrival_at", "enabled", "travel_time_minutes")

def canonical(value):
    """UTF-8 sorted compact JSON, no NaN, exactly one terminal LF."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")

def defaults():
    """New object on each call; never reads a snapshot or random stream."""
    return {
        "version": "cy-public-v1.0",
        "cfs": dict(ready_at=0, window_start=0, window_end=480,
                    service_minutes=10, priority=1, work_status="planned"),
        "yard": dict(expected_blockers_per_target=1, equipment_open_intervals=[[0, 480]],
                     move_service_minutes=5),
        "carrier": dict(current_location_id="PUBLIC_UNKNOWN", available_at=0,
                        estimated_arrival_at=0, enabled=True, travel_time_minutes=15),
    }

def members(coalition):
    if coalition not in COALITIONS:
        raise ValueError("unknown coalition")
    return () if coalition == "NONE" else tuple(p for p in PLAYERS if p in coalition)

@dataclass(frozen=True)
class Observed:
    coalition_id: str
    state_json: bytes

    @property
    def state(self):
        return json.loads(self.state_json)

@dataclass(frozen=True)
class Plan:
    order: tuple
    estimated_objective: tuple
    permutations_evaluated: int
    status: str
    runtime_ms: float
