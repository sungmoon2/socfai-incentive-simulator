"""Paired seed bootstrap, exact rational means, and specified linear quantiles."""
import hashlib
import math
import random
import statistics
from fractions import Fraction
from .contracts import canonical

def numbers(values):
    result = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
            raise ValueError("non-numeric or null statistic input")
        if not math.isfinite(value):
            raise ValueError("non-finite statistic input")
        result.append(Fraction(value))
    if len(result) < 2:
        raise ValueError("sample needs at least two observations")
    return result

def describe(values):
    values = numbers(values)
    mean = sum(values) / len(values)
    return dict(n=len(values), mean=float(mean), mean_exact=str(mean),
                median=float(statistics.median(values)), sample_std=statistics.stdev(values),
                min=float(min(values)), max=float(max(values)))

def percentile(values, probability):
    if not values:
        raise ValueError("empty quantile")
    p = Fraction(str(probability))
    if not 0 <= p <= 1:
        raise ValueError("invalid quantile probability")
    ordered = sorted(Fraction(v) for v in values)
    position = (len(ordered) - 1) * p
    lower = position.numerator // position.denominator
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

def paired_bootstrap(series, seed=20260928, replicates=10000):
    if type(seed) is not int or type(replicates) is not int or replicates < 2:
        raise ValueError("invalid bootstrap configuration")
    if not series:
        raise ValueError("empty paired series")
    values = {name: numbers(data) for name, data in sorted(series.items())}
    sizes = {len(v) for v in values.values()}
    if len(sizes) != 1:
        raise ValueError("unpaired series")
    n = sizes.pop()
    rng = random.Random(seed)
    digest = hashlib.sha256()
    sampled = {name: [] for name in values}
    # Integer numerators with a common denominator avoid accumulated float error.
    units = {}
    for name, data in values.items():
        denominator = math.lcm(*(v.denominator for v in data))
        units[name] = ([int(v * denominator) for v in data], denominator)
    for _ in range(replicates):
        indices = [rng.randrange(n) for _ in range(n)]
        digest.update(canonical(indices))  # Exactly one LF per vector.
        for name, (integers, denominator) in units.items():
            sampled[name].append(Fraction(sum(integers[i] for i in indices), n * denominator))
    intervals = {}
    for name in values:
        lower, upper = (percentile(sampled[name], p) for p in ("0.025", "0.975"))
        point = sum(values[name]) / n
        intervals[name] = dict(point_estimate=float(point), point_estimate_exact=str(point),
                               lower=float(lower), upper=float(upper),
                               lower_exact=str(lower), upper_exact=str(upper),
                               confidence_level=0.95, replicates=replicates,
                               bootstrap_seed=seed, resampling_unit="paired seed")
    return dict(available=True, sample_size=n, replicates=replicates, bootstrap_seed=seed,
                resampling_unit="paired seed", shared_indices=True,
                quantile_method="0-based linear interpolation at (B-1)*p",
                index_encoding="compact sorted-key UTF-8 JSON array + one LF per replicate",
                index_stream_sha256=digest.hexdigest(), intervals=intervals)
