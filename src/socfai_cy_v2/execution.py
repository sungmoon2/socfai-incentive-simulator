"""Pure single-equipment simulation over supplied state, with no truth lookup."""
import copy
from .schema import validate_state

def service_slot(earliest, duration, intervals):
    for opening, closing in intervals:
        start = max(earliest, opening)
        if start + duration <= closing:
            return start
    return None

def destination(stacks, source):
    allowed = [s for s in stacks if s["stack_id"] != source["stack_id"]
               and not s["restricted"] and len(s["stack_bottom_to_top"]) < s["max_tier"]]
    return min(allowed, key=lambda s: (-(s["max_tier"] - len(s["stack_bottom_to_top"])),
                                      s["stack_id"]), default=None)

def simulate(state, order, *, checked=False):
    if not checked:
        validate_state(state)
    jobs = {j["job_id"]: j for j in state["jobs"]}
    if len(order) != len(jobs) or len(set(order)) != len(order) or set(order) != set(jobs):
        raise ValueError("order must contain each known job exactly once")
    yard = copy.deepcopy(state["yard"])
    trucks = {t["target_container_id"]: t for t in state["trucks"]}
    clock = rehandles = retrieves = waiting = travel = completed = 0
    trace, failures = [], []
    for jid in order:
        job = jobs[jid]
        truck = trucks[job["container_id"]]
        if not truck["enabled"]:
            failures.append(dict(job_id=jid, reason="disabled_truck"))
            continue
        travel += truck["travel_time_minutes"]
        arrival = max(truck["estimated_arrival_at"], truck["available_at"] + truck["travel_time_minutes"])
        if job["work_status"] == "blocked":
            failures.append(dict(job_id=jid, reason="blocked_job"))
            continue
        earliest = max(clock, arrival, job["ready_at"], job["window_start"])
        limit = min(job["window_end"], state["horizon_minutes"])
        if earliest > limit:
            failures.append(dict(job_id=jid, reason="window_or_horizon"))
            continue
        waiting += earliest - arrival
        clock = earliest
        if clock + job["service_minutes"] > limit:
            failures.append(dict(job_id=jid, reason="service_exceeds_window"))
            continue
        clock += job["service_minutes"]
        if state["layout_visible"]:
            source = next(s for s in yard["stacks"] if job["container_id"] in s["stack_bottom_to_top"])
            blocker_count = len(source["stack_bottom_to_top"]) - 1 - source["stack_bottom_to_top"].index(job["container_id"])
        else:
            source = None
            blocker_count = yard["expected_blockers_per_target"]
        failure = None
        for move in range(blocker_count + 1):
            relocation = move < blocker_count
            dest = destination(yard["stacks"], source) if relocation and source is not None else None
            if relocation and source is not None and dest is None:
                failure = "no_relocation_capacity"
                break
            start = service_slot(clock, yard["move_service_minutes"], yard["equipment_open_intervals"])
            if start is None or start + yard["move_service_minutes"] > limit:
                failure = "equipment_or_window"
                break
            waiting += start - clock
            clock = start + yard["move_service_minutes"]
            container = source["stack_bottom_to_top"].pop() if source is not None else (
                "PUBLIC_EXPECTED_BLOCKER" if relocation else job["container_id"])
            if relocation:
                rehandles += 1
                if dest is not None:
                    dest["stack_bottom_to_top"].append(container)
            else:
                retrieves += 1
                completed += 1
            trace.append(dict(job_id=jid, container_id=container,
                              kind="relocation" if relocation else "retrieval",
                              source_stack=source["stack_id"] if source else None,
                              destination_stack=dest["stack_id"] if dest else None,
                              start_minute=start, end_minute=clock))
        if failure:
            failures.append(dict(job_id=jid, reason=failure))
    failed = len(jobs) - completed
    return dict(total_rehandle_moves=rehandles, total_moves=rehandles + retrieves,
                completed_jobs=completed, failed_jobs=failed,
                total_waiting_minutes=waiting, total_travel_minutes=travel,
                total_operation_minutes=clock, status="success" if failed == 0 else "infeasible",
                trace=trace, failures=failures, final_stacks=yard.get("stacks"))

def objective(result, order):
    return (result["failed_jobs"], result["total_rehandle_moves"],
            result["total_waiting_minutes"], result["total_operation_minutes"], tuple(order))
