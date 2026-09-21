"""Shared limits for exact-pixel verification.

Sampling and explicit pairs retain their limits. Group comparison can cover a
visible palette without enumerating its Cartesian product on the model wire.
"""

MAX_SUCCESSFUL_CALLS_PER_INVOCATION = 2
MAX_REGIONS_PER_CALL = 12
MAX_COMPARISON_REGIONS = 32
MAX_COMPARISON_REFERENCES = 8
MAX_COMPARISON_TOP_K = 5
MAX_PAIRS_PER_CALL = 6
MAX_PIXEL_SAMPLES_PER_REGION = 4096
MIXED_REGION_VARIANCE_THRESHOLD = 400.0


def region_limit(args: dict) -> int:
    return MAX_COMPARISON_REGIONS if "compare" in args else MAX_REGIONS_PER_CALL
