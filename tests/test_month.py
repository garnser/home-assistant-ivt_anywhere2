# ruff: noqa: I001
import model_imports  # noqa: F401 -- initialise the pure-model namespace
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ivt_model_test.month import aggregate_month

ZONE = ZoneInfo("Europe/Stockholm")


def row(stamp, comp=1, aux=0, cop=4):
    return {
        "hour": stamp.isoformat(),
        "status": "estimated",
        "compressor_kwh": comp,
        "auxiliary_kwh": aux,
        "model_cop": cop,
    }


class MonthTests(unittest.TestCase):
    def test_partial_month_does_not_extrapolate(self):
        end = datetime(2026, 10, 30, 22, tzinfo=ZONE)
        d = aggregate_month([row(end - timedelta(hours=1), 2)], end, ZONE)
        self.assertEqual(d["heat_kwh"], 8)
        self.assertEqual(d["covered_hours"], 1)
        self.assertEqual(d["expected_hours"], 719)
        self.assertIsNone(d["full_month_heat_kwh"])
        self.assertEqual(d["status"], "partial")

    def test_energy_weighted_cop_and_auxiliary(self):
        end = datetime(2026, 10, 2, tzinfo=ZONE)
        d = aggregate_month(
            [
                row(end - timedelta(hours=1), 1, 1, 4),
                row(end - timedelta(hours=2), 3, 0, 2),
            ],
            end,
            ZONE,
        )
        self.assertEqual(d["heat_kwh"], 11)
        self.assertEqual(d["electricity_kwh"], 5)
        self.assertEqual(d["cop"], 2.2)

    def test_rescales_compressor_only(self):
        end = datetime(2026, 10, 2, tzinfo=ZONE)
        d = aggregate_month([row(end - timedelta(hours=1), 1, 2)], end, ZONE, 0.8, 1)
        self.assertAlmostEqual(d["heat_kwh"], 5.2)
        self.assertEqual(d["electricity_kwh"], 3)

    def test_zero_use_is_valid_hour_but_missing_is_not(self):
        end = datetime(2026, 10, 2, tzinfo=ZONE)
        a = row(end - timedelta(hours=1), 0, 0, None)
        b = row(end - timedelta(hours=2))
        b["status"] = "missing_electricity"
        d = aggregate_month([a, b], end, ZONE)
        self.assertEqual(d["covered_hours"], 1)
        self.assertEqual(d["heat_kwh"], 0)
        self.assertIsNone(d["cop"])

    def test_no_data_does_not_become_zero(self):
        d = aggregate_month([], datetime(2026, 10, 2, tzinfo=ZONE), ZONE)
        self.assertEqual(d["status"], "no_data")
        self.assertIsNone(d["heat_kwh"])
        self.assertIsNone(d["electricity_kwh"])

    def test_complete_month_so_far(self):
        start = datetime(2026, 11, 1, tzinfo=ZONE)
        end = start + timedelta(hours=3)
        d = aggregate_month(
            [row(start + timedelta(hours=i)) for i in range(3)], end, ZONE
        )
        self.assertTrue(d["is_complete"])
        self.assertEqual(d["full_month_heat_kwh"], 12)

    def test_month_reset_and_future_hours_excluded(self):
        start = datetime(2026, 11, 1, tzinfo=ZONE)
        end = start + timedelta(hours=1)
        d = aggregate_month(
            [row(start - timedelta(hours=1)), row(start), row(end)], end, ZONE
        )
        self.assertEqual(d["heat_kwh"], 4)
        self.assertEqual(d["covered_hours"], 1)

    def test_duplicate_refreshes_count_once(self):
        end = datetime(2026, 10, 2, tzinfo=ZONE)
        a = row(end - timedelta(hours=1))
        d = aggregate_month([a, a], end, ZONE)
        self.assertEqual(d["covered_hours"], 1)

    def test_dst_fall_back_has_two_distinct_hours(self):
        start = datetime(2026, 10, 25, 0, tzinfo=ZONE).astimezone(timezone.utc)
        end = datetime(2026, 10, 26, 0, tzinfo=ZONE)
        rows = [row((start + timedelta(hours=i)).astimezone(ZONE)) for i in range(25)]
        d = aggregate_month(rows, end, ZONE)
        self.assertEqual(d["covered_hours"], 25)
        self.assertEqual(d["expected_hours"], 601)

    def test_invalid_operating_values_not_summed(self):
        end = datetime(2026, 10, 2, tzinfo=ZONE)
        d = aggregate_month(
            [
                row(end - timedelta(hours=1), cop=None),
                row(end - timedelta(hours=2), comp=-1),
            ],
            end,
            ZONE,
        )
        self.assertIsNone(d["heat_kwh"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
