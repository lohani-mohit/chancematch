import contextlib
import io
import json
import os
import tempfile
import unittest

from chancematch import core
from chancematch.cli import main
from chancematch.simulate import Params, simulate

H = 3600.0
D = 86400.0


def events(rows):
    """rows: (id, time, key or None)"""
    out = core.Events()
    for ident, when, key in rows:
        out.ids.append(ident)
        out.times.append(when)
        out.keys.append(key)
    return out


class Parsing(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(core.parse_duration("24h"), 24 * H)
        self.assertEqual(core.parse_duration("7d"), 7 * D)
        self.assertEqual(core.parse_duration("90"), 90.0)
        self.assertEqual(core.format_duration(24 * H), "1d")
        self.assertEqual(core.format_duration(6 * H), "6h")
        for bad in ("", "abc", "-1h", "0d"):
            with self.assertRaises(core.InputError):
                core.parse_duration(bad)

    def test_times(self):
        self.assertEqual(core.parse_time("1767571200"), 1767571200.0)
        self.assertEqual(core.parse_time("1767571200000"), 1767571200.0)
        self.assertEqual(core.parse_time("2026-01-05T00:00:00Z"), 1767571200.0)
        self.assertEqual(core.parse_time("2026-01-05 00:00:00"), 1767571200.0)
        self.assertEqual(core.parse_time("2026-01-05T05:30:00+05:30"), 1767571200.0)


class Matching(unittest.TestCase):
    def setUp(self):
        # Padding clicks at day 0 and day 20 make every install below eligible.
        self.clicks = events(
            [
                ("pad0", 0.0, ("z",)),
                ("c1", 5 * D, ("a",)),
                ("c2", 5 * D + 2 * H, ("a",)),
                ("c3", 5 * D + 3 * H, ("b",)),
                ("c4", 9 * D + 1 * H, ("b",)),  # one day after install i3
                ("pad1", 20 * D, ("z",)),
            ]
        )

    def run_rule(self, installs, truth=None, offsets=(D,)):
        return core.run(self.clicks, events(installs), ["k"], D, list(offsets), truth)

    def test_latest_click_wins_and_window_edges(self):
        index = core.build_index(self.clicks)
        when = 5 * D + 4 * H
        self.assertEqual(core.last_click(index, ("a",), when - D, when), 2)
        self.assertEqual(core.last_click(index, ("a",), 5 * D, 5 * D), 1)  # both edges inclusive
        self.assertIsNone(core.last_click(index, ("a",), 0.0, 5 * D - 1))
        self.assertIsNone(core.last_click(index, ("missing",), 0.0, 20 * D))
        self.assertIsNone(core.last_click(index, None, 0.0, 20 * D))

    def test_real_and_placebo_counts(self):
        installs = [
            ("i1", 5 * D + 4 * H, ("a",)),  # real match, no future click
            ("i2", 6 * D + 12 * H, ("a",)),  # no click in the day before, none after
            ("i3", 8 * D + 2 * H, ("b",)),  # no real match, placebo match (c4)
            ("i4", 5 * D + 4 * H, None),  # empty key never matches
        ]
        result = self.run_rule(installs)
        self.assertEqual(result.n_eligible, 4)
        self.assertEqual(result.real_matched, 1)
        self.assertEqual(result.placebo_matched, [1])
        self.assertEqual(result.installs_empty_key, 1)

    def test_installs_outside_the_click_log_are_not_eligible(self):
        installs = [
            ("early", 0.5 * D, ("a",)),  # window starts before the first click
            ("late", 19.5 * D, ("a",)),  # placebo window ends after the last click
            ("ok", 10 * D, ("a",)),
        ]
        self.assertEqual(self.run_rule(installs).n_eligible, 1)

    def test_offset_shorter_than_window_is_refused(self):
        with self.assertRaises(core.InputError):
            self.run_rule([("ok", 10 * D, ("a",))], offsets=(12 * H,))

    def test_no_eligible_installs_is_an_error(self):
        with self.assertRaises(core.InputError):
            self.run_rule([("early", 0.5 * D, ("a",))])

    def test_truth_scoring(self):
        installs = [
            ("i1", 5 * D + 4 * H, ("a",)),  # caused by c1, but c2 is later: displaced
            ("i2", 5 * D + 5 * H, ("a",)),  # caused by c2: correct
            ("i3", 5 * D + 6 * H, ("b",)),  # no true click, matched to c3: chance only
            ("i4", 5 * D + 6 * H, ("b",)),  # true click c1 has another key: chance only
        ]
        truth = {"i1": "c1", "i2": "c2", "i3": "", "i4": "c1"}
        score = self.run_rule(installs, truth).truth
        self.assertEqual(
            (score.real_link, score.matched, score.matched_correct, score.matched_displaced, score.matched_chance_only),
            (2, 4, 1, 1, 2),
        )


class Estimates(unittest.TestCase):
    def test_formula(self):
        # 1000 installs, R = 0.5, P = 0.2, one placebo window, r and p independent.
        real, placebo, link, share = core.estimate(1000, 500, 200, 200, 100)
        self.assertAlmostEqual(real.value, 0.5)
        self.assertAlmostEqual(placebo.value, 0.2)
        self.assertAlmostEqual(link.value, 0.375)
        self.assertAlmostEqual(share.value, 0.25)
        self.assertTrue(link.low < 0.375 < link.high)
        self.assertTrue(share.low < 0.25 < share.high)

    def test_real_not_above_placebo(self):
        _, _, link, share = core.estimate(1000, 200, 300, 300, 60)
        self.assertEqual(link.value, 0.0)
        self.assertEqual(share.value, 1.0)

    def test_no_matches_at_all(self):
        _, _, link, share = core.estimate(1000, 0, 0, 0, 0)
        self.assertEqual(link.value, 0.0)
        self.assertIsNone(share)


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.counts = simulate(cls.tmp.name, Params())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_simulation_is_repeatable(self):
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(simulate(other, Params()), self.counts)
            for name in ("clicks.csv", "installs.csv", "truth.csv"):
                with open(self.path(name)) as first, open(os.path.join(other, name)) as second:
                    self.assertEqual(first.read(), second.read())

    def test_estimate_recovers_the_truth(self):
        for keys in ("attr_a,attr_b", "attr_a,attr_b,attr_c"):
            code, out, _ = self.cli(
                "run", "--clicks", self.path("clicks.csv"), "--installs", self.path("installs.csv"),
                "--keys", keys, "--truth", self.path("truth.csv"), "--json",
            )
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertAlmostEqual(
                data["estimated_real_link_rate"]["value"], data["truth"]["real_link_rate"], delta=0.05
            )
            self.assertAlmostEqual(
                data["estimated_chance_share_of_matches"]["value"],
                data["truth"]["chance_share_of_matches"],
                delta=0.05,
            )

    def test_text_report(self):
        code, out, _ = self.cli(
            "run", "--clicks", self.path("clicks.csv"), "--installs", self.path("installs.csv"),
            "--keys", "attr_a,attr_b,attr_c",
        )
        self.assertEqual(code, 0)
        self.assertIn("Chance-match rate", out)
        self.assertNotIn("Ground truth", out)

    def test_input_errors_exit_2(self):
        code, _, err = self.cli("run", "--clicks", "nope.csv", "--installs", "nope.csv", "--keys", "attr_a")
        self.assertEqual(code, 2)
        self.assertIn("cannot open", err)
        code, _, err = self.cli(
            "run", "--clicks", self.path("clicks.csv"), "--installs", self.path("installs.csv"), "--keys", "nope",
        )
        self.assertEqual(code, 2)
        self.assertIn("missing column", err)


if __name__ == "__main__":
    unittest.main()
