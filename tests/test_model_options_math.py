# ruff: noqa: I001
import model_imports  # noqa: F401 -- initialise the pure-model namespace

"""Cross-appliance settings, coarse readings, strict totals and projection guards."""

import copy
import unittest
from dataclasses import replace
from datetime import timedelta

from ivt_model_test.estimate_math import compressor_cop, estimate_window, hour_model
from ivt_model_test.gap_fill import fill_short_gaps
from ivt_model_test.learning import assess_day
from ivt_model_test.model_config import ModelSettings
from ivt_model_test.month import project_month
from test_estimate import END, ENTITIES, START, ZONE, fixtures
from test_learning import day_fixture
from test_thermal import hour_history


class ConfigurableModelTests(unittest.TestCase):
    def test_calibration_toggle_and_manual_factor_retain_raw_history(self):
        settings = ModelSettings()
        self.assertEqual(
            settings.signature(),
            replace(
                settings, calibration_enabled=True, performance_factor=0.9
            ).signature(),
        )
        self.assertNotEqual(
            settings.signature(), replace(settings, reference_cop=3.5).signature()
        )

    def test_another_rating_and_source_temperature(self):
        s = replace(
            ModelSettings(),
            reference_cop=3.7,
            reference_source_c=2,
            reference_flow_c=45,
        )
        self.assertAlmostEqual(compressor_cop(45, 2, settings=s), 3.7)
        self.assertAlmostEqual(compressor_cop(35), 4.8)  # no globals changed

    def test_another_tank_volume_changes_storage_only(self):
        h = hour_history(tank_fn=lambda i: 40 + 0.1 * i)
        small = hour_model(
            h, START, 0, 1, replace(ModelSettings(), tank_volume_litres=100)
        )
        large = hour_model(
            h, START, 0, 1, replace(ModelSettings(), tank_volume_litres=200)
        )
        self.assertAlmostEqual(
            large["tank_net_stored_heat_change_kwh"],
            2 * small["tank_net_stored_heat_change_kwh"],
        )
        self.assertEqual(large["model_cop"], small["model_cop"])

    def test_settings_reject_invalid_and_nonfinite_physics(self):
        base = {
            "supply_entity": "sensor.supply",
            "return_entity": "sensor.return",
            "modulation_entity": "sensor.modulation",
        }
        self.assertEqual(ModelSettings.from_options(base).reference_cop, 4.8)
        for extra in [
            {"reference_cop": float("nan")},
            {"tank_volume_litres": 0},
            {"performance_factor": True},
            {"modulation_entity": "light.modulation"},
            {"assumed_source_c": -50},
            {"calibration_enabled": "yes"},
        ]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                ModelSettings.from_options({**base, **extra})

    def test_zero_does_not_create_inferred_electricity(self):
        h, r = fixtures(comp=0)
        raw = estimate_window(h, r, END, ZONE)
        result = fill_short_gaps(raw, [])
        self.assertEqual(result["electricity_kwh"], 0)
        self.assertEqual(result["inferred_electricity_kwh"], 0)
        self.assertEqual(result["gap_filled_hours"], 0)
        self.assertEqual(result["zero_energy_active_hours"], 24)

    def test_input_coverage_counts_missing_temperatures_and_missing_energy(self):
        h, r = fixtures()
        d = estimate_window({}, r, END, ZONE)
        self.assertEqual(d["coverage_percent"], 0)
        next(iter(r.values()))["recording"][23]["c"] = 0
        d = estimate_window(h, r, END, ZONE)
        self.assertLess(d["coverage_percent"], 100)
        self.assertLess(d["energy_sample_coverage_percent"], 100)

    def test_short_temperature_gap_is_separate_from_raw_observations(self):
        h, r = fixtures()
        h[ENTITIES[0]] += [
            {
                "state": "unavailable",
                "last_updated": (START + timedelta(hours=4)).isoformat(),
            },
            {"state": "35", "last_updated": (START + timedelta(hours=5)).isoformat()},
        ]
        raw = estimate_window(h, r, END, ZONE)
        original = copy.deepcopy(raw)
        d = fill_short_gaps(raw, raw["hours"])
        self.assertEqual(raw, original)
        self.assertEqual(d["gap_filled_hours"], 1)
        self.assertEqual(d["usable_hours"], 24)
        self.assertEqual(d["model_electricity_kwh"], 24)
        self.assertAlmostEqual(d["cop"], 4.8)
        self.assertLess(d["coverage_percent"], 100)

    def test_missing_energy_is_never_guessed_even_with_activity(self):
        h, r = fixtures()
        next(iter(r.values()))["recording"][23]["c"] = 0
        raw = estimate_window(h, r, END, ZONE)
        d = fill_short_gaps(raw, raw["hours"])
        self.assertEqual(d["gap_filled_hours"], 0)
        self.assertIsNone(d["heat_kwh"])
        self.assertIsNone(d["electricity_kwh"])

    def test_no_future_donors_or_large_gap_fill(self):
        h, r = fixtures()
        raw = estimate_window(h, r, END, ZONE)
        for index in [0, 1, 2, 3]:
            raw["hours"][index]["status"] = "missing_operating_history"
        raw.update(covered_hours=20, heat_kwh=None, cop=None)
        d = fill_short_gaps(raw, raw["hours"])
        self.assertEqual(d["gap_filled_hours"], 0)
        # First hour cannot borrow future temperature COP.
        raw["hours"][1:4] = estimate_window(h, r, END, ZONE)["hours"][1:4]
        d = fill_short_gaps(raw, raw["hours"])
        self.assertEqual(d["gap_filled_hours"], 0)

    def test_auxiliary_output_accounting_is_explicit(self):
        rows, parents = day_fixture()
        with_aux = assess_day("2026-10-28", rows, parents, True)
        without_aux = assess_day("2026-10-28", rows, parents, False)
        self.assertEqual(with_aux["observed_compressor_heat_kwh"], 72)
        self.assertEqual(without_aux["observed_compressor_heat_kwh"], 84)


class ProjectionTests(unittest.TestCase):
    def fixture(self):
        month = {
            "covered_until": END.isoformat(),
            "covered_hours": 100,
            "compressor_kwh": 20,
            "auxiliary_kwh": 5,
            "heat_kwh": 85,
        }
        local = {
            "month": END.astimezone(ZONE).strftime("%Y-%m"),
            "sampled_at": END.isoformat(),
            "values": {"compressor_month": 100, "eheater_month": 10},
        }
        return month, {}, local

    def test_full_electricity_not_covered_electricity(self):
        m, d, l = self.fixture()
        r = project_month(m, d, l, END)
        self.assertEqual(r["heat_kwh"], 410)
        self.assertEqual(r["electricity_kwh"], 110)
        self.assertAlmostEqual(r["cop"], 410 / 110)

    def test_month_rollover_and_stale_input(self):
        m, d, l = self.fixture()
        l["month"] = "2025-01"
        self.assertIsNone(project_month(m, d, l, END)["heat_kwh"])
        m, d, l = self.fixture()
        l["sampled_at"] = (END - timedelta(hours=1)).isoformat()
        self.assertIsNone(project_month(m, d, l, END)["heat_kwh"])

    def test_stale_model_basis_does_not_project_current_month(self):
        m, d, l = self.fixture()
        m["covered_until"] = (END - timedelta(days=7)).isoformat()
        self.assertIsNone(project_month(m, d, l, END)["heat_kwh"])
        m, d, l = self.fixture()
        l["sampled_at"] = "2026-10-30T20:00:00"
        self.assertIsNone(project_month(m, d, l, END)["heat_kwh"])

    def test_early_month_fallback_requires_full_window(self):
        m, d, l = self.fixture()
        m["covered_hours"] = 3
        self.assertIsNone(project_month(m, d, l, END)["heat_kwh"])
        d.update(
            usable_hours=24,
            model_compressor_kwh=10,
            model_auxiliary_kwh=1,
            model_heat_kwh=41,
        )
        r = project_month(m, d, l, END)
        self.assertEqual(r["heat_kwh"], 410)
        self.assertEqual(r["basis_period"], "last_24_hours")

    def test_negative_missing_and_auxiliary_only(self):
        m, d, l = self.fixture()
        l["values"]["compressor_month"] = -1
        self.assertIsNone(project_month(m, d, l, END)["heat_kwh"])
        l["values"]["compressor_month"] = None
        self.assertIsNone(project_month(m, d, l, END)["heat_kwh"])
        l["values"]["compressor_month"] = 0
        r = project_month(m, d, l, END)
        self.assertEqual(r["heat_kwh"], 10)
        self.assertEqual(r["cop"], 1)


if __name__ == "__main__":
    unittest.main()
