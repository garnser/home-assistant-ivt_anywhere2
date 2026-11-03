# ruff: noqa: I001
import model_imports  # noqa: F401 -- initialise the pure-model namespace
import unittest
from datetime import timedelta

from ivt_model_test.estimate_math import (
    ENTITIES,
    TANK_ENTITY,
    TANK_KWH_PER_K,
    estimate_window,
    hour_model,
    live_thermal_evidence,
)
from test_estimate import END, START, ZONE, fixtures


def hour_history(flow=35, ret=30, mod=60, tank_fn=None):
    h = {
        key: [
            {
                "state": str(v),
                "last_updated": (START - timedelta(minutes=15)).isoformat(),
            }
        ]
        for key, v in zip(ENTITIES, (flow, ret, mod))
    }
    if tank_fn:
        h[TANK_ENTITY] = [
            {
                "state": str(tank_fn(i)),
                "last_updated": (START + timedelta(minutes=i)).isoformat(),
            }
            for i in range(-15, 61)
        ]
    return h


class ThermalTests(unittest.TestCase):
    def test_temperature_gap_requires_compressor(self):
        d = hour_model(hour_history(mod=0), START, 0, 1)
        self.assertEqual(d["active_minutes"], 0)
        self.assertEqual(d["residual_or_circulation_minutes"], 60)

    def test_gap_with_compressor_confirms_activity(self):
        d = hour_model(hour_history(), START, 0, 1)
        self.assertEqual(d["active_minutes"], 60)
        self.assertEqual(d["active_supply_return_delta_c"], 5)

    def test_equal_temperatures_without_tank_rise_unconfirmed(self):
        d = hour_model(hour_history(ret=35, tank_fn=lambda i: 45), START, 0, 1)
        self.assertEqual(d["active_minutes"], 0)
        self.assertEqual(d["unconfirmed_compressor_minutes"], 60)

    def test_equal_loop_temperatures_do_not_hide_dhw_charging(self):
        d = hour_model(
            hour_history(ret=35, tank_fn=lambda i: 40 + 0.12 * i), START, 0, 1
        )
        self.assertEqual(d["active_minutes"], 60)
        self.assertEqual(d["probable_dhw_minutes"], 60)
        self.assertLess(d["model_cop"], 4.8)
        self.assertGreater(d["active_flow_c"], 40)

    def test_tank_rise_without_compressor_not_compressor_heat(self):
        d = hour_model(
            hour_history(mod=0, tank_fn=lambda i: 40 + 0.12 * i), START, 0, 1
        )
        self.assertEqual(d["active_minutes"], 0)
        self.assertEqual(d["probable_dhw_minutes"], 0)

    def test_tank_noise_does_not_create_dhw_cycles(self):
        d = hour_model(
            hour_history(tank_fn=lambda i: 45 + (0.1 if i % 2 else -0.1)), START, 0, 1
        )
        self.assertEqual(d["probable_dhw_minutes"], 0)

    def test_180_litre_storage_change_is_signed(self):
        up = hour_model(hour_history(tank_fn=lambda i: 40 + 0.1 * i), START, 0, 1)
        down = hour_model(hour_history(tank_fn=lambda i: 50 - 0.1 * i), START, 0, 1)
        self.assertAlmostEqual(
            up["tank_net_stored_heat_change_kwh"], 6 * TANK_KWH_PER_K
        )
        self.assertAlmostEqual(
            down["tank_net_stored_heat_change_kwh"], -6 * TANK_KWH_PER_K
        )

    def test_missing_tank_does_not_block_space_heating(self):
        d = hour_model(hour_history(), START, 0, 1)
        self.assertAlmostEqual(d["model_cop"], 4.8)
        self.assertIsNone(d["tank_net_stored_heat_change_kwh"])

    def test_unavailable_tank_not_a_zero_temperature(self):
        d = hour_model(hour_history(tank_fn=lambda i: "unavailable"), START, 0, 1)
        self.assertEqual(d["tank_temperature_coverage"], 0)
        self.assertIsNone(d["tank_net_stored_heat_change_kwh"])

    def test_tank_warming_not_added_twice_to_total(self):
        h, r = fixtures(flow=55, ret=50)
        before = estimate_window(h, r, END, ZONE)
        h[TANK_ENTITY] = [
            {
                "state": str(min(50, 40 + 0.3 * i)),
                "last_updated": (START + timedelta(minutes=i)).isoformat(),
            }
            for i in range(-15, 1441)
        ]
        after = estimate_window(h, r, END, ZONE)
        self.assertGreater(after["probable_dhw_heating_hours"], 0)
        self.assertGreater(after["tank_net_stored_heat_change_kwh"], 0)
        self.assertAlmostEqual(before["heat_kwh"], after["heat_kwh"])

    def test_zero_bucket_with_activity_is_preserved_and_flagged(self):
        h, r = fixtures(comp=0)
        d = estimate_window(h, r, END, ZONE)
        self.assertEqual(d["heat_kwh"], 0)
        self.assertEqual(d["electricity_kwh"], 0)
        self.assertEqual(d["zero_energy_active_hours"], 24)
        self.assertEqual(d["hours"][0]["status"], "estimated")

    def test_true_idle_has_zero_heat(self):
        h, r = fixtures(comp=0, mod=0, flow=35, ret=35)
        d = estimate_window(h, r, END, ZONE)
        self.assertEqual(d["heat_kwh"], 0)

    def test_live_activity_distinguishes_residual_from_idle(self):
        now = START + timedelta(minutes=30)
        self.assertEqual(
            live_thermal_evidence(hour_history(mod=0), now)["thermal_activity"],
            "Residual / circulating",
        )
        self.assertEqual(
            live_thermal_evidence(hour_history(mod=0, ret=35), now)["thermal_activity"],
            "Idle",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
