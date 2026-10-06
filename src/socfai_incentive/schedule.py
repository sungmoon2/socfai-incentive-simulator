"""Deterministic FIFO event scheduler over a supplied state; no snapshot access."""

import heapq
from collections import Counter


def service_slot(arrival, earliest, duration, servers, intervals):
    """Earliest finish-safe slot; FIFO reservations by arrival/truck/job upstream."""
    candidates = []
    for i, free in enumerate(servers):
        for opening, closing in intervals:
            start = max(arrival, earliest, free, opening)
            if start + duration <= closing:
                candidates.append((start, i))
                break
    return min(candidates) if candidates else None


def simulate(state, routes):
    jobs = {j["job_id"]: j for j in state["jobs"]}
    trucks = {t["truck_id"]: t for t in state["trucks"]}
    if set(routes) - set(trucks):
        raise ValueError("routes: unknown truck")
    counts = Counter(j for route in routes.values() for j in route)
    if set(counts) - set(jobs) or any(n != 1 for n in counts.values()):
        raise ValueError("routes: unknown or duplicate job")
    failed = set()
    completed = set()
    servers = {n: [0] * v["server_capacity"] for n, v in state["locations"].items()}
    queue = []
    trace = []
    waiting = travel = 0
    horizon = state["horizon_minutes"]

    def next_job(tid, index, when, location):
        nonlocal travel
        route = routes.get(tid, [])
        if index < len(route):
            jid = route[index]
            distance = state["travel_times"][location][jobs[jid]["origin_id"]]
            travel += distance
            heapq.heappush(queue, (when + distance, tid, jid, 0, index))

    for tid in sorted(trucks):
        t = trucks[tid]
        if not t["enabled"]:
            failed.update(routes.get(tid, []))
        else:
            next_job(tid, 0, t["available_at"], t["start_location_id"])
    while queue:
        arrival, tid, jid, stage, index = heapq.heappop(queue)
        job = jobs[jid]
        loc = job["origin_id"] if stage == 0 else job["destination_id"]
        duration = job["service_minutes_origin"] if stage == 0 else job["service_minutes_destination"]
        earliest = max(job["ready_at"], job["window_start"]) if stage == 0 else job["window_start"]
        slot = service_slot(arrival, earliest, duration, servers[loc], state["locations"][loc]["open_intervals"])
        if slot is None or slot[0] + duration > min(horizon, job["window_end"]):
            failed.add(jid)
            trace.append(dict(truck_id=tid, job_id=jid, stage=stage, location_id=loc, arrival_time=arrival, status="failed"))
            next_job(tid, index + 1, arrival, loc)
            continue
        start, server = slot
        end = start + duration
        waiting += start - arrival
        servers[loc][server] = end
        trace.append(dict(truck_id=tid, job_id=jid, stage=stage, location_id=loc, arrival_time=arrival,
                          service_start=start, service_end=end, server=server, waiting_minutes=start-arrival, status="served"))
        if stage == 0:
            distance = state["travel_times"][loc][job["destination_id"]]
            travel += distance
            heapq.heappush(queue, (end + distance, tid, jid, 1, index))
        else:
            completed.add(jid)
            next_job(tid, index + 1, end, loc)
    return dict(total_waiting_minutes=waiting, total_travel_minutes=travel,
                total_operation_minutes=waiting + travel, failed_jobs=len(failed),
                unassigned_jobs=len(set(jobs) - set(counts)), completed_jobs=len(completed),
                trace=trace)
