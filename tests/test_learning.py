# ruff: noqa: I001
import model_imports  # noqa: F401 -- initialise the pure-model namespace
import copy
import unittest
from datetime import datetime, timedelta, timezone

from ivt_model_test.learning import (
    apply_model,
    assess_day,
    fit_model,
    observe_day,
    parent_point,
)

NOW = datetime(2026, 10, 30, 22, tzinfo=timezone.utc)


def day_fixture():
    rows = []
    for h in range(24):
        rows.append(
            {
                "hour": f"2026-10-28T{h:02}:00:00+01:00",
                "status": "estimated",
                "temperature_coverage": 1,
                "compressor_kwh": 1,
                "auxiliary_kwh": 0.5,
                "model_cop": 4,
                "raw": {
                    "compressor": {"y": 60, "c": 60},
                    "eheater": {"y": 30, "c": 60},
                    "outputProduced": {"y": 210, "c": 60},
                },
            }
        )
    parents = {
        k: {"y": sum(r["raw"][k]["y"] for r in rows), "c": 1440}
        for k in ("compressor", "eheater", "outputProduced")
    }
    return rows, parents


def examples(factor=0.8, n=14):
    return [
        {
            "day": (NOW - timedelta(days=n - i)).date().isoformat(),
            "qualified": True,
            "base_compressor_heat_kwh": 100,
            "observed_compressor_heat_kwh": 100 * factor,
        }
        for i in range(n)
    ]


class LearningTests(unittest.TestCase):
    def test_whole_day_with_auxiliary(self):
        rows, parents = day_fixture()
        d = assess_day("2026-10-28", rows, parents)
        self.assertEqual(d["status"], "awaiting_stability")
        self.assertEqual(d["observed_compressor_heat_kwh"], 72)
        self.assertEqual(d["base_compressor_heat_kwh"], 96)

    def test_missing_hour_rejected(self):
        rows, parents = day_fixture()
        self.assertFalse(assess_day("2026-10-28", rows[:-1], parents)["qualified"])

    def test_duplicate_dst_hour_rejected(self):
        rows, parents = day_fixture()
        rows[-1]["hour"] = rows[0]["hour"]
        self.assertEqual(
            assess_day("2026-10-28", rows, parents)["status"], "incomplete_hours"
        )

    def test_low_coverage_rejected(self):
        rows, parents = day_fixture()
        rows[0]["raw"]["outputProduced"]["c"] = 30
        self.assertEqual(
            assess_day("2026-10-28", rows, parents)["status"],
            "recording_coverage_below_98_percent",
        )

    def test_same_value_59_samples_is_allowed(self):
        rows, parents = day_fixture()
        for row in rows:
            for k in parents:
                row["raw"][k]["c"] = 59
        for p in parents.values():
            p["c"] = 59 * 24
        self.assertEqual(
            assess_day("2026-10-28", rows, parents)["status"], "awaiting_stability"
        )

    def test_aggregate_disagreement_rejected(self):
        rows, parents = day_fixture()
        parents["outputProduced"]["y"] *= 2
        self.assertEqual(
            assess_day("2026-10-28", rows, parents)["status"],
            "hourly_daily_disagreement",
        )

    def test_zero_heat_gaps_reject_entire_day(self):
        rows, parents = day_fixture()
        for row in rows[:12]:
            row["raw"]["outputProduced"]["y"] = 0
        parents["outputProduced"]["y"] = sum(
            r["raw"]["outputProduced"]["y"] for r in rows
        )
        d = assess_day("2026-10-28", rows, parents)
        self.assertEqual(d["status"], "heat_output_gaps")
        self.assertFalse(d["qualified"])

    def test_stability_not_poll_count(self):
        rows, parents = day_fixture()
        d = assess_day("2026-10-28", rows, parents)
        first = observe_day(d, None, NOW)
        self.assertFalse(observe_day(d, first, NOW + timedelta(minutes=5))["qualified"])
        self.assertTrue(observe_day(d, first, NOW + timedelta(minutes=30))["qualified"])
        changed = copy.deepcopy(d)
        changed["signature"][0] += 1
        self.assertFalse(
            observe_day(changed, first, NOW + timedelta(minutes=30))["qualified"]
        )

    def test_minimum_days_and_rejections(self):
        self.assertEqual(fit_model(examples(n=13), NOW)["status"], "collecting")
        d = examples()
        d[-1]["qualified"] = False
        self.assertEqual(fit_model(d, NOW)["qualified_days"], 13)

    def test_learns_factor_with_later_validation(self):
        d = fit_model(examples(), NOW)
        self.assertEqual(d["status"], "calibrated")
        self.assertAlmostEqual(d["factor"], 0.8)
        self.assertEqual(d["training_days"], 10)
        self.assertEqual(d["validation_days"], 4)
        self.assertEqual(d["validation_wape_percent"], 0)

    def test_validation_not_leaked_into_training(self):
        d = examples(0.8)
        for row in d[-4:]:
            row["observed_compressor_heat_kwh"] = 100
        model = fit_model(d, NOW)
        self.assertEqual(model["candidate_factor"], 0.8)
        self.assertEqual(model["factor"], 1)
        self.assertEqual(model["status"], "baseline_retained")

    def test_extreme_factor_never_used(self):
        d = fit_model(examples(0.2), NOW)
        self.assertEqual(d["status"], "candidate_out_of_bounds")
        self.assertEqual(d["factor"], 1)

    def test_old_calibration_not_used(self):
        d = examples()
        for r in d:
            r["day"] = (
                (datetime.fromisoformat(r["day"]) - timedelta(days=31))
                .date()
                .isoformat()
            )
        self.assertEqual(fit_model(d, NOW)["status"], "calibration_stale")

    def test_no_error_does_not_invent_adjustment(self):
        model = fit_model(examples(1), NOW)
        self.assertEqual(model["factor"], 1)

    def test_factor_preserves_auxiliary_and_energy(self):
        result = {
            "heat_kwh": 9,
            "electricity_kwh": 3,
            "hours": [
                {
                    "status": "estimated",
                    "compressor_kwh": 2,
                    "auxiliary_kwh": 1,
                    "model_cop": 4,
                }
            ],
        }
        d = apply_model(result, 0.8, 1)
        self.assertAlmostEqual(d["heat_kwh"], 7.4)
        self.assertEqual(d["electricity_kwh"], 3)
        self.assertAlmostEqual(d["cop"], 7.4 / 3)

    def test_incomplete_prediction_stays_unavailable(self):
        d = apply_model({"heat_kwh": None, "cop": None, "hours": []}, 0.8, 1)
        self.assertIsNone(d["heat_kwh"])
        self.assertIsNone(d["cop"])

    def test_parent_period_and_missing_sample_checks(self):
        payload = {
            "interval": "2026-10",
            "sampleRate": "P1D",
            "recording": [{"y": 1, "c": 0}],
        }
        self.assertIsNone(parent_point(payload, "2026-10-01"))
        self.assertIsNone(parent_point(payload, "2026-09-01"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
