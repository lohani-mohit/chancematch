"""Command line for chancematch."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from typing import List, Optional

from . import __version__
from .core import (
    Estimate,
    InputError,
    Result,
    format_duration,
    load_events,
    load_truth,
    parse_duration,
    run,
)
from .simulate import Params, simulate


def _pct(value: float) -> str:
    return f"{100 * value:5.1f}%"


def _range(item: Estimate) -> str:
    return f"{100 * item.low:.1f} to {100 * item.high:.1f}%"


def render(result: Result) -> str:
    lines: List[str] = []
    add = lines.append
    add(f"chancematch {__version__}")
    add(
        f"Rule: same {', '.join(result.key_cols)}; click within {format_duration(result.window)} "
        "before the install; latest click wins"
    )
    add(
        f"Clicks: {result.n_clicks:,}   Installs: {result.n_installs:,}   "
        f"Eligible installs: {result.n_eligible:,}"
    )
    if result.clicks_empty_key or result.installs_empty_key:
        add(
            f"Rows with an empty key value (never matched): {result.clicks_empty_key:,} clicks, "
            f"{result.installs_empty_key:,} installs"
        )
    add("")
    add(f"{'':<26}{'matched':>9}  {'rate':>6}   95% interval")
    add(f"{'Real run':<26}{result.real_matched:>9,}  {_pct(result.real.value)}   {_range(result.real)}")
    for offset, count, rate in zip(result.offsets, result.placebo_matched, result.placebo_rates):
        label = f"Placebo, +{format_duration(offset)}"
        add(f"{label:<26}{count:>9,}  {_pct(rate)}")
    add(f"{'Chance-match rate (mean)':<26}{'':>9}  {_pct(result.placebo.value)}   {_range(result.placebo)}")
    add("")
    add(
        f"Estimated installs with a real link:   {_pct(result.real_link.value)}   "
        f"{_range(result.real_link)}"
    )
    if result.chance_share is not None:
        add(
            f"Estimated matches that are chance:     {_pct(result.chance_share.value)}   "
            f"{_range(result.chance_share)}"
        )
    if result.lift is not None:
        add(f"Real run over placebo:                 {result.lift:5.2f}x")
    else:
        add("Real run over placebo:                 placebo found no matches")

    truth = result.truth
    if truth is not None:
        add("")
        add("Ground truth (eligible installs)")
        add(f"Installs with a real link:             {_pct(truth.real_link / result.n_eligible)}")
        if truth.matched:
            add(f"Matches that are chance only:          {_pct(truth.matched_chance_only / truth.matched)}")
            add(
                f"Matches given to the wrong click:      "
                f"{_pct((truth.matched - truth.matched_correct) / truth.matched)}"
                f"   ({truth.matched_displaced:,} had a real link but a later click won)"
            )
    add("")
    add("Placebo windows lie after each install, so no placebo match can be causal.")
    add("Eligible installs are those whose real and placebo windows all fall inside the click log.")
    return "\n".join(lines)


def _durations(text: str) -> List[float]:
    return [parse_duration(part) for part in text.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chancematch",
        description="Measure the chance-match rate of a click-to-install matching rule with a placebo test.",
    )
    parser.add_argument("--version", action="version", version=f"chancematch {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help="apply a rule to the real data and to placebo windows")
    run_cmd.add_argument("--clicks", required=True, help="CSV of clicks")
    run_cmd.add_argument("--installs", required=True, help="CSV of installs")
    run_cmd.add_argument("--keys", required=True, help="comma-separated columns that must be equal, e.g. attr_a,attr_b")
    run_cmd.add_argument("--window", default="24h", help="how long before the install a click may be (default 24h)")
    run_cmd.add_argument(
        "--offsets",
        default="1d,2d,3d,7d",
        help="placebo shifts into the future; each at least the window (default 1d,2d,3d,7d)",
    )
    run_cmd.add_argument("--click-time-col", default="click_time")
    run_cmd.add_argument("--install-time-col", default="install_time")
    run_cmd.add_argument("--click-id-col", default="click_id")
    run_cmd.add_argument("--install-id-col", default="install_id")
    run_cmd.add_argument("--truth", help="CSV of install_id,click_id with the true cause, for simulated data")
    run_cmd.add_argument("--json", action="store_true", help="print JSON instead of the text report")

    sim_cmd = commands.add_parser("simulate", help="write synthetic clicks.csv, installs.csv and truth.csv")
    sim_cmd.add_argument("--out", required=True, help="directory to write into")
    defaults = Params()
    for item in fields(Params):
        sim_cmd.add_argument(
            "--" + item.name.replace("_", "-"),
            type=item.type if isinstance(item.type, type) else type(getattr(defaults, item.name)),
            default=getattr(defaults, item.name),
            help=f"default {getattr(defaults, item.name)}",
        )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "simulate":
            params = Params(**{item.name: getattr(args, item.name) for item in fields(Params)})
            clicks, installs, attributed = simulate(args.out, params)
            print(
                f"Wrote {clicks:,} clicks and {installs:,} installs to {args.out} "
                f"({attributed:,} installs have a true click, {installs - attributed:,} have none)."
            )
            return 0

        key_cols = [part.strip() for part in args.keys.split(",") if part.strip()]
        if not key_cols:
            raise InputError("--keys needs at least one column")
        window = parse_duration(args.window)
        offsets = _durations(args.offsets)
        clicks = load_events(args.clicks, args.click_time_col, key_cols, args.click_id_col)
        installs = load_events(args.installs, args.install_time_col, key_cols, args.install_id_col)
        truth = load_truth(args.truth, args.install_id_col, args.click_id_col) if args.truth else None
        result = run(clicks, installs, key_cols, window, offsets, truth)
        print(json.dumps(result.to_dict(), indent=2) if args.json else render(result))
        return 0
    except InputError as err:
        print(f"chancematch: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
