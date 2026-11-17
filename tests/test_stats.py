import math
import unittest

from experiment import stats


class BoundaryTests(unittest.TestCase):
    def test_single_look_matches_standard_normal(self):
        boundaries = stats.sequential_boundaries([1.0], 0.05)
        self.assertAlmostEqual(boundaries[0], 1.959963984540054, places=9)

    def test_obf_spends_exactly_alpha_over_planned_looks(self):
        fractions = [0.25, 0.5, 0.75, 1.0]
        boundaries = tuple(stats.sequential_boundaries(fractions, 0.05))
        self.assertAlmostEqual(stats.crossing_probability(boundaries, tuple(fractions)), 0.05, places=6)

    def test_more_looks_require_stronger_first_boundary(self):
        one = stats.sequential_boundaries([1.0], 0.05)[0]
        four = stats.sequential_boundaries([0.25, 0.5, 0.75, 1.0], 0.05)[0]
        self.assertGreater(four, one)

    def test_boundaries_do_not_depend_on_future_looks(self):
        early = stats.sequential_boundaries([0.2, 0.5], 0.05)[1]
        later_plan = stats.sequential_boundaries([0.2, 0.5, 1.0], 0.05)[1]
        self.assertAlmostEqual(early, later_plan, places=9)

    def test_zero_information_is_not_a_boundary(self):
        self.assertEqual(stats.sequential_boundaries([0.0], 0.05), [math.inf])

    def test_decreasing_information_is_rejected(self):
        with self.assertRaises(ValueError):
            stats.sequential_boundaries([0.5, 0.4], 0.05)

    def test_spending_function_is_monotone(self):
        values = [stats.obf_spending(f, 0.05) for f in (0.1, 0.25, 0.5, 0.75, 1.0)]
        self.assertEqual(values, sorted(values))
        self.assertAlmostEqual(values[-1], 0.05, places=12)


class IntervalTests(unittest.TestCase):
    def test_wilson_interval_contains_estimate_and_bounds(self):
        low, high = stats.wilson_interval(10, 20, 0.95)
        self.assertLess(low, 0.5)
        self.assertGreater(high, 0.5)
        self.assertGreaterEqual(low, 0.0)
        self.assertLessEqual(high, 1.0)

    def test_newcombe_contains_observed_difference(self):
        low, high = stats.newcombe_difference(30, 100, 20, 100, 0.95)
        self.assertLess(low, 0.10)
        self.assertGreater(high, 0.10)

    def test_zero_cells_are_estimable_without_nan(self):
        self.assertIsNone(stats.newcombe_difference(0, 0, 0, 10))
        low, high = stats.newcombe_difference(0, 10, 0, 10)
        self.assertTrue(math.isfinite(low) and math.isfinite(high))

    def test_pooled_z_is_zero_when_rates_equal(self):
        z, p = stats.pooled_z_test(10, 40, 10, 40)
        self.assertAlmostEqual(z, 0.0)
        self.assertAlmostEqual(p, 1.0)

    def test_repeated_interval_rejected_without_boundary(self):
        self.assertIsNone(stats.sequential_interval(0.1, 0.05, None))


if __name__ == "__main__":
    unittest.main()
