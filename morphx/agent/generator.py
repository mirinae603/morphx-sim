"""Fake blood count results."""

import random
import uuid

from morphx.models import utc_iso


def _reading(rng: random.Random, mean: float, spread: float, low: float, high: float) -> float:
    return max(low, min(high, rng.gauss(mean, spread)))


def new_sample(rng: random.Random) -> dict:
    """One new test. The ids and values are made here once and never regenerated."""
    # roughly one test in ten is a bit off
    widen = 2.0 if rng.random() < 0.1 else 1.0
    return {
        "event_id": str(uuid.uuid4()),
        "sample_id": f"SMP-{uuid.uuid4().hex[:12].upper()}",
        "measured_at": utc_iso(),
        "measurements": {
            "wbc": {
                "value": round(_reading(rng, 7.5, 1.3 * widen, 1.0, 40.0), 2),
                "unit": "10^3/uL",
            },
            "rbc": {
                "value": round(_reading(rng, 5.0, 0.4 * widen, 2.0, 8.0), 2),
                "unit": "10^6/uL",
            },
            "hb": {"value": round(_reading(rng, 14.5, 1.2 * widen, 5.0, 20.0), 1), "unit": "g/dL"},
        },
    }
