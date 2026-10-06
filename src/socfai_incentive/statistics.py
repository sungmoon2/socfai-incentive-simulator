"""Fixed paired-seed statistics; no optimizer execution or sample selection."""

import hashlib
import math
import random
import statistics as std

from .data import canonical


def finite(values):
    return all(type(v) in (int, float) and math.isfinite(v) for v in values)


def describe(values):
    if len(values) < 2 or not finite(values):
        raise ValueError("at least two finite observations required")
    return dict(n=len(values), mean=std.mean(values), median=std.median(values),
                sample_std=std.stdev(values), min=min(values), max=max(values))


def percentile(values, p):
    if not values or not finite(values) or not 0 <= p <= 1:
        raise ValueError("finite observations and probability required")
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def paired_bootstrap(series, seed, replicates):
    """One randrange index vector per replicate, shared by EVERY series.

    Extra ratio-of-means is computed from the SAME resampled baseline/full costs.
    The index digest makes the exact random draw sequence independently auditable.
    """
    sizes = {len(v) for v in series.values()}
    if len(sizes) != 1 or not sizes or min(sizes) < 2:
        raise ValueError("equal paired series lengths >= 2 required")
    if not all(finite(v) for v in series.values()) or replicates < 2:
        raise ValueError("finite series and >= 2 replicates required")
    if "NONE.J" not in series or "CTR.J" not in series or any(v <= 0 for v in series["NONE.J"]):
        raise ValueError("positive baseline costs required")
    n = sizes.pop()
    rng = random.Random(seed)
    samples = {key: [] for key in series}
    ratio_key = "full_sharing.ratio_of_means_reduction_pct"
    samples[ratio_key] = []
    index_hash = hashlib.sha256()
    for _ in range(replicates):
        indices = [rng.randrange(n) for _ in range(n)]
        index_hash.update(canonical(indices) + b"\n")
        means = {key: std.mean(values[i] for i in indices) for key, values in series.items()}
        for key, value in means.items():
            samples[key].append(value)
        samples[ratio_key].append(100 * (means["NONE.J"] - means["CTR.J"]) / means["NONE.J"])
    points = {key: std.mean(values) for key, values in series.items()}
    points[ratio_key] = 100 * (points["NONE.J"] - points["CTR.J"]) / points["NONE.J"]
    if not finite(points.values()) or not all(finite(v) for v in samples.values()):
        raise ValueError("non-finite bootstrap result")
    return dict(available=True, bootstrap_seed=seed, replicates=replicates,
                rng="random.Random; randrange(n) repeated n times per replicate",
                resampling_unit="paired scenario seed", sample_size=n,
                same_indices_for_all_series=True, index_vectors_sha256=index_hash.hexdigest(),
                quantile_method="0-based linear interpolation at (B-1)*p", confidence_level=0.95,
                primary_targets=["CTR.time_reduction_pct", "CTR.V", "shapley.cfs", "shapley.terminal", "shapley.carrier"],
                auxiliary_target=ratio_key,
                intervals={key: dict(point_estimate=points[key], lower=percentile(values, .025),
                                     upper=percentile(values, .975)) for key, values in samples.items()})
