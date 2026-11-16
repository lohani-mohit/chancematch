"""Synthetic clicks and installs with known ground truth.

Nothing here describes a real system. The generator exists so the method can
be tried, and checked against the truth, without anyone's data. Every number
it produces follows from the parameters you pass.
"""
from __future__ import annotations

import csv
import math
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import accumulate
from typing import List, Tuple

START = datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp()  # a Monday, fixed for repeatability
DAY = 86400.0


@dataclass
class Params:
    clicks: int = 20000
    days: int = 21
    conversion: float = 0.08  # share of clicks that lead to an install
    organic: int = 2400  # installs with no click at all
    attr_a_values: int = 60  # three made-up attributes, from coarse to fine
    attr_b_values: int = 30
    attr_c_values: int = 8
    skew: float = 0.8  # Zipf exponent for how unevenly key values are used
    median_lag_hours: float = 1.0  # click-to-install delay
    lag_sigma: float = 1.6  # log-normal spread of that delay
    key_stability: float = 0.92  # chance the install carries the click's attr_a value
    seed: int = 7


def _cumulative(count: int, skew: float) -> List[float]:
    return list(accumulate(1.0 / (rank ** skew) for rank in range(1, count + 1)))


def _iso(when: float) -> str:
    return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def simulate(out_dir: str, params: Params) -> Tuple[int, int, int]:
    """Write clicks.csv, installs.csv and truth.csv. Returns (clicks, installs, with a true click)."""
    rng = random.Random(params.seed)
    span = params.days * DAY

    def moment() -> float:
        # More activity in the evening than at night: a plain daily cycle.
        while True:
            offset = rng.random() * span
            hour = (offset % DAY) / 3600.0
            if rng.random() < (1 + 0.6 * math.sin(2 * math.pi * (hour - 15) / 24)) / 1.6:
                return START + offset

    values_a = [f"a{n:02d}" for n in range(1, params.attr_a_values + 1)]
    values_b = [f"b{n:02d}" for n in range(1, params.attr_b_values + 1)]
    values_c = [f"c{n:02d}" for n in range(1, params.attr_c_values + 1)]
    cum_a = _cumulative(params.attr_a_values, params.skew)
    cum_b = _cumulative(params.attr_b_values, params.skew)
    cum_c = _cumulative(params.attr_c_values, params.skew)

    def keys(count: int) -> List[Tuple[str, str, str]]:
        return list(
            zip(
                rng.choices(values_a, cum_weights=cum_a, k=count),
                rng.choices(values_b, cum_weights=cum_b, k=count),
                rng.choices(values_c, cum_weights=cum_c, k=count),
            )
        )

    click_rows = sorted(zip((moment() for _ in range(params.clicks)), keys(params.clicks)))
    clicks = [(f"c{n:06d}", when, key) for n, (when, key) in enumerate(click_rows, start=1)]

    installs: List[Tuple[float, Tuple[str, str, str], str]] = []
    mu = math.log(params.median_lag_hours * 3600.0)
    for click_id, when, key in clicks:
        if rng.random() >= params.conversion:
            continue
        installed = when + rng.lognormvariate(mu, params.lag_sigma)
        if installed >= START + span:
            continue
        first, second, third = key
        if rng.random() >= params.key_stability:
            first = rng.choice([other for other in values_a if other != first])
        installs.append((installed, (first, second, third), click_id))
    attributed = len(installs)

    for key in keys(params.organic):
        installs.append((moment(), key, ""))
    rng.shuffle(installs)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "clicks.csv"), "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["click_id", "click_time", "attr_a", "attr_b", "attr_c"])
        for click_id, when, key in clicks:
            writer.writerow([click_id, _iso(when), *key])
    with open(os.path.join(out_dir, "installs.csv"), "w", newline="", encoding="utf-8") as installs_file, open(
        os.path.join(out_dir, "truth.csv"), "w", newline="", encoding="utf-8"
    ) as truth_file:
        install_writer = csv.writer(installs_file)
        truth_writer = csv.writer(truth_file)
        install_writer.writerow(["install_id", "install_time", "attr_a", "attr_b", "attr_c"])
        truth_writer.writerow(["install_id", "click_id"])
        for number, (when, key, click_id) in enumerate(installs, start=1):
            install_id = f"i{number:06d}"
            install_writer.writerow([install_id, _iso(when), *key])
            truth_writer.writerow([install_id, click_id])
    return len(clicks), len(installs), attributed
