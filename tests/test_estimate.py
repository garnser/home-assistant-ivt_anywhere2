# ruff: noqa: I001
import model_imports  # noqa: F401 -- initialise the pure-model namespace
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ivt_model_test.estimate_math import ENTITIES, compressor_cop, estimate_window

ZONE = ZoneInfo("Europe/Stockholm")
END = datetime(2026, 10, 30, 21, tzinfo=timezone.utc)
START = END - timedelta(hours=24)


def fixtures(comp=1, aux=0, flow=35, ret=30, mod=60):
    history = {
        key: [{"state": str(value), "last_updated": START.isoformat()}]
        for key, value in zip(ENTITIES, (flow, ret, mod))
    }
    records = {}
    for i in range(24):
        t = (START + timedelta(hours=i)).astimezone(ZONE)
        day = t.strftime("%Y-%m-%d")
        for kind, value in [("compressor", comp), ("eheater", aux)]:
            if (day, kind) not in records:
                records[(day, kind)] = {
                    "interval": day,
                    "sampleRate": "P1H",
                    "recording": [{"y": 1, "c": 0} for _ in range(24)],
                }
            records[(day, kind)]["recording"][t.hour] = {"y": value * 60, "c": 60}
    return history, records


class EstimateTests(unittest.TestCase):
    def run_model(self, h, r):
        return estimate_window(h, r, END, ZONE)

    def test_factory_reference(self):
        self.assertAlmostEqual(compressor_cop(35, 0), 4.8)

    def test_temperature_direction(self):
        self.assertLess(compressor_cop(55, 0), compressor_cop(35, 0))
        self.assertGreater(compressor_cop(35, 5), compressor_cop(35, 0))

    def test_invalid_inputs(self):
        for flow in [None, "unavailable", float("nan"), 5, 100]:
            self.assertIsNone(compressor_cop(flow))

    def test_electricity_weighted_total_with_auxiliary(self):
        h, r = fixtures(comp=1, aux=0.5)
        d = self.run_model(h, r)
        self.assertAlmostEqual(d["heat_kwh"], 24 * 5.3)
        self.assertAlmostEqual(d["electricity_kwh"], 36)
        self.assertAlmostEqual(d["cop"], 5.3 / 1.5)

    def test_zero_consumption_preserved(self):
        h, r = fixtures(comp=0, mod=0)
        d = self.run_model({}, r)
        self.assertEqual(d["heat_kwh"], 0)
        self.assertIsNone(d["cop"])

    def test_auxiliary_only_is_cop_one(self):
        h, r = fixtures(comp=0, aux=0.5)
        d = self.run_model({}, r)
        self.assertEqual(d["heat_kwh"], 12)
        self.assertEqual(d["cop"], 1)

    def test_missing_energy_does_not_become_zero(self):
        h, r = fixtures()
        next(iter(r.values()))["recording"][23] = {"y": 1, "c": 0}
        d = self.run_model(h, r)
        self.assertIsNone(d["heat_kwh"])
        self.assertEqual(d["covered_hours"], 23)

    def test_poor_sample_coverage_not_extrapolated(self):
        h, r = fixtures()
        next(iter(r.values()))["recording"][23] = {"y": 60, "c": 30}
        d = self.run_model(h, r)
        self.assertIsNone(d["heat_kwh"])

    def test_temperature_gap_rejected(self):
        h, r = fixtures()
        h[ENTITIES[0]] += [
            {
                "state": "unavailable",
                "last_updated": (START + timedelta(hours=3)).isoformat(),
            },
            {
                "state": "35",
                "last_updated": (START + timedelta(hours=3, minutes=20)).isoformat(),
            },
        ]
        d = self.run_model(h, r)
        self.assertEqual(d["covered_hours"], 23)
        self.assertIsNone(d["heat_kwh"])

    def test_no_active_history_is_not_assumed_heating(self):
        h, r = fixtures(mod=0)
        d = self.run_model(h, r)
        self.assertEqual(d["covered_hours"], 0)
        self.assertIsNone(d["cop"])

    def test_modulation_weights_active_temperatures(self):
        h, r = fixtures(mod=0)
        for i in range(24):
            t = START + timedelta(hours=i)
            h[ENTITIES[0]] += [
                {"state": "35", "last_updated": t.isoformat()},
                {
                    "state": "55",
                    "last_updated": (t + timedelta(minutes=30)).isoformat(),
                },
            ]
            h[ENTITIES[1]] += [
                {"state": "30", "last_updated": t.isoformat()},
                {
                    "state": "50",
                    "last_updated": (t + timedelta(minutes=30)).isoformat(),
                },
            ]
            h[ENTITIES[2]] += [
                {"state": "0", "last_updated": t.isoformat()},
                {
                    "state": "60",
                    "last_updated": (t + timedelta(minutes=30)).isoformat(),
                },
            ]
        d = self.run_model(h, r)
        self.assertAlmostEqual(d["cop"], compressor_cop(55))

    def test_adjustment_scales_compressor_only(self):
        h, r = fixtures(comp=1, aux=1)
        d = estimate_window(h, r, END, ZONE, factor=0.8)
        self.assertAlmostEqual(d["heat_kwh"], 24 * (4.8 * 0.8 + 1))

    def test_ambiguous_dst_recording_refused(self):
        d = estimate_window(
            {}, {}, datetime(2026, 10, 25, 23, tzinfo=timezone.utc), ZONE
        )
        self.assertEqual(d["status"], "ambiguous_dst_hour")
        self.assertIsNone(d["heat_kwh"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
