"""Explicit allowlist projection; hashes and private metadata never reach optimizer."""
import copy
from .contracts import Observed, canonical, defaults, members, JOB_FIELDS, TRUCK_FIELDS
from .schema import validate_snapshot, validate_state

def observe(snapshot, coalition, as_of):
    shared = members(coalition)
    validate_snapshot(snapshot, as_of)
    public = defaults()
    jobs = []
    for actual in sorted(snapshot["jobs"], key=lambda x: x["job_id"]):
        job = {key: actual[key] for key in ("job_id", "container_id")}
        source = actual if "C" in shared else public["cfs"]
        job.update({key: copy.deepcopy(source[key]) for key in JOB_FIELDS})
        jobs.append(job)
    trucks = []
    for actual in sorted(snapshot["trucks"], key=lambda x: x["truck_id"]):
        truck = {key: actual[key] for key in ("truck_id", "target_container_id")}
        source = actual if "R" in shared else public["carrier"]
        truck.update({key: copy.deepcopy(source[key]) for key in TRUCK_FIELDS})
        trucks.append(truck)
    if "Y" in shared:
        yard = {k: copy.deepcopy(snapshot["yard"][k]) for k in
                ("yard_id", "equipment_open_intervals", "move_service_minutes")}
        keys = ("stack_id", "block_id", "bay", "row", "max_tier", "stack_bottom_to_top", "restricted")
        yard["stacks"] = [{k: copy.deepcopy(s[k]) for k in keys}
                          for s in sorted(snapshot["yard"]["stacks"], key=lambda x: x["stack_id"])]
    else:
        yard = copy.deepcopy(public["yard"])
    state = dict(horizon_minutes=480, layout_visible="Y" in shared,
                 yard=yard, jobs=jobs, trucks=trucks)
    validate_state(state)
    return Observed(coalition, canonical(state))
