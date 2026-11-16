"""Core of chancematch.

Apply a click-to-install matching rule to the real data, then apply the same
rule to placebo windows that lie entirely after each install. A click that
happens after an install cannot have caused it, so every placebo match is a
match that cannot be causal. The placebo match rate is the chance-match rate.
"""
from __future__ import annotations

import bisect
import csv
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

Z95 = 1.959963984540054

_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}


class InputError(ValueError):
    """A problem with the user's files or options, reported without a traceback."""


def parse_duration(text: str) -> float:
    """'24h', '7d', '30m', '90s' or a bare number of seconds -> seconds."""
    raw = text.strip().lower()
    if not raw:
        raise InputError("empty duration")
    unit = _UNITS.get(raw[-1])
    number = raw[:-1] if unit else raw
    try:
        value = float(number) * (unit or 1.0)
    except ValueError:
        raise InputError(f"cannot read duration {text!r}; use forms like 24h, 7d, 30m, 90s")
    if value <= 0:
        raise InputError(f"duration must be positive, got {text!r}")
    return value


def format_duration(seconds: float) -> str:
    for suffix, size in (("d", 86400.0), ("h", 3600.0), ("m", 60.0)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{suffix}"
    return f"{seconds:g}s"


def parse_time(text: str) -> float:
    """Epoch seconds, epoch milliseconds or ISO 8601 -> epoch seconds (UTC if no zone)."""
    raw = text.strip()
    try:
        value = float(raw)
    except ValueError:
        pass
    else:
        return value / 1000.0 if value > 1e11 else value
    if raw[-1:] in ("Z", "z"):
        raw = raw[:-1] + "+00:00"
    stamp = datetime.fromisoformat(raw)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


Key = Tuple[str, ...]


@dataclass
class Events:
    ids: List[str] = field(default_factory=list)
    times: List[float] = field(default_factory=list)
    keys: List[Optional[Key]] = field(default_factory=list)  # None when any key value is empty

    def __len__(self) -> int:
        return len(self.times)

    @property
    def empty_keys(self) -> int:
        return sum(1 for key in self.keys if key is None)


def load_events(path: str, time_col: str, key_cols: Sequence[str], id_col: str) -> Events:
    events = Events()
    try:
        handle = open(path, newline="", encoding="utf-8-sig")
    except OSError as err:
        raise InputError(f"cannot open {path}: {err.strerror}")
    with handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        missing = [col for col in (time_col, *key_cols) if col not in columns]
        if missing:
            raise InputError(
                f"{path}: missing column(s) {', '.join(missing)}; found {', '.join(columns) or 'none'}"
            )
        has_id = id_col in columns
        for line, row in enumerate(reader, start=2):
            raw_time = (row.get(time_col) or "").strip()
            if not raw_time:
                raise InputError(f"{path} line {line}: empty {time_col}")
            try:
                when = parse_time(raw_time)
            except ValueError:
                raise InputError(f"{path} line {line}: cannot read time {raw_time!r}")
            values = tuple((row.get(col) or "").strip() for col in key_cols)
            events.ids.append((row.get(id_col) or "").strip() if has_id else str(line - 1))
            events.times.append(when)
            events.keys.append(values if all(values) else None)
    if not events.times:
        raise InputError(f"{path}: no rows")
    return events


def load_truth(path: str, install_id_col: str, click_id_col: str) -> Dict[str, str]:
    """install id -> id of the click that really caused it ('' for none)."""
    try:
        handle = open(path, newline="", encoding="utf-8-sig")
    except OSError as err:
        raise InputError(f"cannot open {path}: {err.strerror}")
    with handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        missing = [col for col in (install_id_col, click_id_col) if col not in columns]
        if missing:
            raise InputError(f"{path}: missing column(s) {', '.join(missing)}")
        return {
            (row.get(install_id_col) or "").strip(): (row.get(click_id_col) or "").strip()
            for row in reader
        }


Index = Dict[Key, Tuple[List[float], List[int]]]


def build_index(clicks: Events) -> Index:
    groups: Dict[Key, List[Tuple[float, int]]] = {}
    for position, (when, key) in enumerate(zip(clicks.times, clicks.keys)):
        if key is not None:
            groups.setdefault(key, []).append((when, position))
    index: Index = {}
    for key, pairs in groups.items():
        pairs.sort()
        index[key] = ([when for when, _ in pairs], [position for _, position in pairs])
    return index


def last_click(index: Index, key: Optional[Key], low: float, high: float) -> Optional[int]:
    """Position of the latest click with this key and low <= time <= high, else None."""
    if key is None:
        return None
    entry = index.get(key)
    if entry is None:
        return None
    times, positions = entry
    spot = bisect.bisect_right(times, high) - 1
    if spot >= 0 and times[spot] >= low:
        return positions[spot]
    return None


def wilson(successes: int, total: int) -> Tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    rate = successes / total
    denom = 1 + Z95 * Z95 / total
    centre = (rate + Z95 * Z95 / (2 * total)) / denom
    half = Z95 * math.sqrt(rate * (1 - rate) / total + Z95 * Z95 / (4 * total * total)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _clip(low: float, high: float) -> Tuple[float, float]:
    return (min(1.0, max(0.0, low)), min(1.0, max(0.0, high)))


@dataclass
class Estimate:
    value: float
    low: float
    high: float


@dataclass
class Truth:
    """Ground truth for the eligible installs (only available for simulated data)."""

    real_link: int  # installs whose true click satisfies the rule
    matched: int
    matched_correct: int  # matched to the true click
    matched_displaced: int  # true click satisfied the rule, a later click won
    matched_chance_only: int  # no true click satisfied the rule, matched anyway


@dataclass
class Result:
    key_cols: List[str]
    window: float
    offsets: List[float]
    n_clicks: int
    n_installs: int
    n_eligible: int
    clicks_empty_key: int
    installs_empty_key: int
    real_matched: int
    placebo_matched: List[int]
    real: Estimate
    placebo: Estimate
    real_link: Estimate
    chance_share: Optional[Estimate]
    truth: Optional[Truth] = None

    @property
    def placebo_rates(self) -> List[float]:
        return [count / self.n_eligible for count in self.placebo_matched]

    @property
    def lift(self) -> Optional[float]:
        return self.real.value / self.placebo.value if self.placebo.value > 0 else None

    def to_dict(self) -> dict:
        def est(item: Optional[Estimate]) -> Optional[dict]:
            if item is None:
                return None
            return {"value": item.value, "ci95": [item.low, item.high]}

        out = {
            "rule": {
                "keys": self.key_cols,
                "window_seconds": self.window,
                "tie_break": "last click",
            },
            "placebo_offsets_seconds": self.offsets,
            "clicks": self.n_clicks,
            "installs": self.n_installs,
            "eligible_installs": self.n_eligible,
            "clicks_with_empty_key": self.clicks_empty_key,
            "installs_with_empty_key": self.installs_empty_key,
            "real_matched": self.real_matched,
            "real_match_rate": est(self.real),
            "placebo_matched": self.placebo_matched,
            "placebo_match_rates": self.placebo_rates,
            "chance_match_rate": est(self.placebo),
            "estimated_real_link_rate": est(self.real_link),
            "estimated_chance_share_of_matches": est(self.chance_share),
            "real_over_placebo": self.lift,
        }
        if self.truth is not None:
            truth = self.truth
            out["truth"] = {
                "real_link_rate": truth.real_link / self.n_eligible,
                "matched": truth.matched,
                "matched_correct": truth.matched_correct,
                "matched_displaced": truth.matched_displaced,
                "matched_chance_only": truth.matched_chance_only,
                "chance_share_of_matches": (
                    truth.matched_chance_only / truth.matched if truth.matched else None
                ),
                "wrong_click_share_of_matches": (
                    (truth.matched - truth.matched_correct) / truth.matched if truth.matched else None
                ),
            }
        return out


def estimate(total: int, sum_r: float, sum_p: float, sum_pp: float, sum_rp: float):
    """Rates and delta-method intervals from per-install sums.

    r is 1 when the install matched in the real run. p is the share of placebo
    windows in which it matched. With R = mean(r) and P = mean(p):

        R = T + (1 - T) * P    =>    T = (R - P) / (1 - P)

    T is the share of installs that have a real link under the rule, assuming a
    chance match is as likely for an install with a real link as for one
    without. The share of matches that exist only by chance is 1 - T / R.
    """
    rate_r = sum_r / total
    rate_p = sum_p / total
    var_r = rate_r * (1 - rate_r) / total
    var_p = max(sum_pp / total - rate_p * rate_p, 0.0) / total
    cov = (sum_rp / total - rate_r * rate_p) / total

    real = Estimate(rate_r, *wilson(int(round(sum_r)), total))
    half_p = Z95 * math.sqrt(var_p)
    placebo = Estimate(rate_p, *_clip(rate_p - half_p, rate_p + half_p))

    if rate_p >= 1.0 or rate_r <= rate_p:
        link = Estimate(0.0, 0.0, 0.0)
        share = Estimate(1.0, 1.0, 1.0) if rate_r > 0 else None
        return real, placebo, link, share

    d_r = 1.0 / (1.0 - rate_p)
    d_p = (rate_r - 1.0) / (1.0 - rate_p) ** 2
    value = (rate_r - rate_p) / (1.0 - rate_p)
    spread = Z95 * math.sqrt(max(d_r * d_r * var_r + d_p * d_p * var_p + 2 * d_r * d_p * cov, 0.0))
    link = Estimate(value, *_clip(value - spread, value + spread))

    c_r = value / (rate_r * rate_r) - d_r / rate_r
    c_p = -d_p / rate_r
    share_value = 1.0 - value / rate_r
    share_spread = Z95 * math.sqrt(
        max(c_r * c_r * var_r + c_p * c_p * var_p + 2 * c_r * c_p * cov, 0.0)
    )
    share = Estimate(share_value, *_clip(share_value - share_spread, share_value + share_spread))
    return real, placebo, link, share


def run(
    clicks: Events,
    installs: Events,
    key_cols: Sequence[str],
    window: float,
    offsets: Sequence[float],
    truth: Optional[Dict[str, str]] = None,
) -> Result:
    """Apply the rule to the real window and to each placebo window.

    Rule: an install at time t matches when at least one click has the same key
    values and a time in [t - window, t]; the latest such click wins.
    Placebo with offset o: the same rule on clicks in [t - window + o, t + o].
    Every offset must be at least the window, so the placebo window starts at or
    after the install.
    """
    if not offsets:
        raise InputError("give at least one placebo offset")
    short = [o for o in offsets if o < window]
    if short:
        raise InputError(
            f"placebo offset {format_duration(short[0])} is shorter than the window "
            f"{format_duration(window)}; the placebo window would overlap the real one"
        )

    index = build_index(clicks)
    first_click, last_click_time = min(clicks.times), max(clicks.times)
    longest = max(offsets)
    click_position = {cid: pos for pos, cid in enumerate(clicks.ids)} if truth is not None else {}

    eligible = 0
    real_matched = 0
    placebo_matched = [0] * len(offsets)
    sum_p = sum_pp = sum_rp = 0.0
    score = Truth(0, 0, 0, 0, 0) if truth is not None else None

    for install_id, when, key in zip(installs.ids, installs.times, installs.keys):
        # Only installs whose real and placebo windows all lie inside the click log.
        if when - window < first_click or when + longest > last_click_time:
            continue
        eligible += 1
        winner = last_click(index, key, when - window, when)
        hit = 1 if winner is not None else 0
        real_matched += hit

        hits = 0
        for slot, offset in enumerate(offsets):
            if last_click(index, key, when - window + offset, when + offset) is not None:
                placebo_matched[slot] += 1
                hits += 1
        share = hits / len(offsets)
        sum_p += share
        sum_pp += share * share
        sum_rp += hit * share

        if score is not None:
            true_pos = click_position.get(truth.get(install_id, ""))
            linked = (
                true_pos is not None
                and key is not None
                and clicks.keys[true_pos] == key
                and when - window <= clicks.times[true_pos] <= when
            )
            if linked:
                score.real_link += 1
            if winner is not None:
                score.matched += 1
                if linked and winner == true_pos:
                    score.matched_correct += 1
                elif linked:
                    score.matched_displaced += 1
                else:
                    score.matched_chance_only += 1

    if eligible == 0:
        raise InputError(
            "no eligible installs: the click log must cover the window before each install "
            f"({format_duration(window)}) and the longest placebo offset after it "
            f"({format_duration(longest)}). Use a longer click log or shorter offsets."
        )

    real, placebo, link, chance = estimate(eligible, real_matched, sum_p, sum_pp, sum_rp)
    return Result(
        key_cols=list(key_cols),
        window=window,
        offsets=list(offsets),
        n_clicks=len(clicks),
        n_installs=len(installs),
        n_eligible=eligible,
        clicks_empty_key=clicks.empty_keys,
        installs_empty_key=installs.empty_keys,
        real_matched=real_matched,
        placebo_matched=placebo_matched,
        real=real,
        placebo=placebo,
        real_link=link,
        chance_share=chance,
        truth=score,
    )
