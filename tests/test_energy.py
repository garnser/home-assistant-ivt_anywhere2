"""Regression tests using synthetic PointT data, without credentials or network."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math
import unittest

from ha_stubs import clock, coordinator, hass, sensor, util, UpdateFailed


def payload(y=60, c=60, rate="P1H", length=1):
    return {"sampleRate": rate, "recording": [{"y": y, "c": c} for _ in range(length)]}


class RecordingTests(unittest.TestCase):
    def test_hourly_sample_normalization(self):
        self.assertEqual(util.sum_kwh(payload()), 1)
        self.assertEqual(util.sum_kwh(payload(120)), 2)
        self.assertAlmostEqual(util.sum_kwh(payload(60, 59)), 60 / 59)

    def test_daily_sample_normalization(self):
        self.assertEqual(util.month_total_kwh(payload(600, 1440, "P1D")), 10)
        self.assertEqual(util.month_total_kwh(payload(600, 1200, "P1D")), 12)

    def test_actual_wh_conversion_is_not_recording_conversion(self):
        self.assertEqual(util.wh_to_kwh(1000), 1)

    def test_genuine_zero_is_valid(self):
        self.assertEqual(util.sum_kwh(payload(0)), 0)
        self.assertEqual(util.kwh_at_index(payload(0), 0), 0)

    def test_padding_is_not_usage(self):
        p = payload()
        p["recording"].append({"y": 1, "c": 0})
        self.assertEqual(util.sum_kwh(p), 1)
        self.assertIsNone(util.kwh_at_index(p, 1))

    def test_missing_empty_or_unsupported_recordings_are_unavailable(self):
        for p in [None, {}, {"recording": []}, payload(1, 0), payload(rate="P31D")]:
            with self.subTest(payload=p):
                self.assertIsNone(util.sum_kwh(p))

    def test_invalid_values_reject_the_whole_total(self):
        for value in [None, True, "60", -1, math.inf, math.nan]:
            p = payload(length=2)
            p["recording"][1]["y"] = value
            with self.subTest(value=value):
                self.assertIsNone(util.sum_kwh(p))

    def test_invalid_sample_counts_are_rejected(self):
        for count in [None, True, "60", -1, math.inf, math.nan]:
            with self.subTest(count=count):
                self.assertIsNone(util.sum_kwh(payload(c=count)))

    def test_numeric_overflow_is_unavailable(self):
        self.assertIsNone(util.sum_kwh(payload(1e308, 1e-308)))
        self.assertIsNone(util.sum_kwh(payload(1e308, 1, length=2)))

    def test_missing_completed_day_rejects_month(self):
        p = payload(600, 1440, "P1D")
        p["recording"].append({"y": 1, "c": 0})
        self.assertIsNone(util.month_total_kwh(p, required_days=2))
        self.assertEqual(util.month_total_kwh(p, required_days=1), 10)
        self.assertIsNone(util.month_total_kwh(p, required_days=3))

    def test_hour_index_boundaries(self):
        self.assertIsNone(util.kwh_at_index(payload(), -1))
        self.assertIsNone(util.kwh_at_index(payload(), 1))
        self.assertIsNone(util.kwh_at_index(None, 0))

    def test_hour_reset_is_stable_within_a_period(self):
        first = util.last_complete_hour_start(datetime.fromisoformat("2026-02-01T12:01:00+01:00"))
        later = util.last_complete_hour_start(datetime.fromisoformat("2026-02-01T12:59:00+01:00"))
        next_hour = util.last_complete_hour_start(datetime.fromisoformat("2026-02-01T13:01:00+01:00"))
        self.assertEqual(first, later)
        self.assertNotEqual(first, next_hour)
        self.assertEqual(first.isoformat(), "2026-02-01T10:00:00+00:00")

    def test_repeated_dst_hours_have_distinct_reset_markers(self):
        zone = ZoneInfo("Europe/Stockholm")
        first = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0)
        second = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
        self.assertNotEqual(util.last_complete_hour_start(first), util.last_complete_hour_start(second))
        self.assertEqual(util.last_complete_hour_target(second)[1], 2)

    def test_recording_rate_must_match_the_requested_period(self):
        self.assertIsNone(util.kwh_at_index(payload(rate="P1D"), 0))
        self.assertIsNone(util.month_total_kwh(payload(rate="P1H")))


class Api:
    def __init__(self):
        self.missing = None
        self.empty = False
        self.month_value = 600
        self.error = None

    async def bulk(self, gateway, paths):
        if self.error:
            raise self.error
        rows = []
        for path in paths:
            kind = path.split("energyMonitoring/")[1].split("?")[0]
            monthly = len(path.split("interval=")[1]) == 7
            p = payload(self.month_value, 1440, "P1D") if monthly else payload(length=24)
            if self.empty:
                p["recording"] = []
            if kind == self.missing:
                p = None
            rows.append({"resourcePath": path, "gatewayResponse": {"status": 200, "payload": p}})
        return [{"resourcePaths": rows}]


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clock.now = lambda: datetime(2026, 2, 1, 12, tzinfo=timezone.utc)
        self.hass = hass()
        self.api = Api()
        self.ctx = coordinator.IVTAnywhereIICoordinator(self.hass, self.api, "test-gateway")

    async def update(self):
        self.ctx.data = await self.ctx._async_update_data()
        return self.ctx.data

    async def test_complete_total_and_cop(self):
        data = await self.update()
        self.assertEqual(data.electricity_kwh_last_hour, 2)
        self.assertEqual(data.electricity_kwh_month, 20)
        self.assertEqual(data.cop_month, 0.5)

    async def test_equal_hourly_values_still_have_distinct_periods(self):
        first = await self.update()
        repeated = await self.update()
        self.assertEqual(first.last_hour_reset, repeated.last_hour_reset)
        clock.now = lambda: datetime(2026, 2, 1, 13, tzinfo=timezone.utc)
        next_hour = await self.update()
        self.assertEqual(first.electricity_kwh_last_hour, next_hour.electricity_kwh_last_hour)
        self.assertNotEqual(first.last_hour_reset, next_hour.last_hour_reset)

    async def test_missing_compressor_never_becomes_heater_only_total(self):
        self.api.missing = "compressor"
        data = await self.update()
        self.assertIsNone(data.electricity_kwh_month)
        self.assertIsNone(data.electricity_kwh_last_hour)
        self.assertIsNone(data.cop_month)
        self.assertEqual(data.eheater_kwh_month, 10)

    async def test_missing_heater_does_not_disappear_from_total(self):
        self.api.missing = "eheater"
        data = await self.update()
        self.assertIsNone(data.electricity_kwh_month)
        self.assertIsNone(data.electricity_kwh_last_hour)

    async def test_empty_recordings_do_not_reset_counters(self):
        self.api.empty = True
        self.assertIsNone((await self.update()).electricity_kwh_month)

    async def test_missing_past_days_are_not_a_partial_month_total(self):
        clock.now = lambda: datetime(2026, 2, 3, 12, tzinfo=timezone.utc)
        self.assertIsNone((await self.update()).electricity_kwh_month)

    async def test_downward_revision_and_recovery(self):
        await self.update()
        self.api.month_value = 60
        self.assertIsNone((await self.update()).electricity_kwh_month)
        self.api.month_value = 660
        self.assertEqual((await self.update()).electricity_kwh_month, 22)

    async def test_restart_preserves_last_accepted_value(self):
        await self.update()
        self.ctx = coordinator.IVTAnywhereIICoordinator(self.hass, self.api, "test-gateway")
        self.api.month_value = 60
        self.assertIsNone((await self.update()).electricity_kwh_month)

    async def test_real_month_boundary_allows_reset(self):
        await self.update()
        clock.now = lambda: datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
        self.api.month_value = 60
        self.assertEqual((await self.update()).electricity_kwh_month, 2)

    async def test_invalid_saved_values_do_not_poison_counters(self):
        self.hass.storage[self.ctx._month_store.key] = {
            "month": "2026-02", "values": {"compressor": math.nan, "eheater": True}
        }
        self.assertEqual((await self.update()).electricity_kwh_month, 20)

    async def test_api_failure_is_unavailable(self):
        self.api.error = RuntimeError("offline")
        with self.assertRaises(UpdateFailed):
            await self.update()

    async def test_tiny_floating_point_revision_never_decreases_counter(self):
        await self.update()
        self.api.month_value -= 1e-7
        self.assertEqual((await self.update()).electricity_kwh_month, 20)


class SensorTests(unittest.TestCase):
    def setUp(self):
        self.ctx = coordinator.IVTAnywhereIICoordinator(hass(), Api(), "test-gateway")

    def entity(self, key):
        return sensor.IVTEnergySensor(self.ctx, next(d for d in sensor.SENSORS if d.key == key))

    def test_hourly_energy_uses_supported_periodic_total_state_class(self):
        for description in sensor.SENSORS:
            if description.key.endswith("last_hour") and description.device_class == "energy":
                self.assertEqual(description.state_class, "total")

    def test_monthly_energy_still_supports_energy_dashboard(self):
        for description in sensor.SENSORS:
            if description.key.endswith("month") and description.device_class == "energy":
                self.assertEqual(description.state_class, "total_increasing")

    def test_existing_sensor_identities_are_unchanged(self):
        for key in ["electricity_month", "heat_output_month"]:
            self.assertEqual(self.entity(key)._attr_unique_id, f"test-gateway_{key}")
        self.assertEqual(self.entity("electricity_last_hour")._attr_unique_id, "test-gateway_electricity_last_hour")

    def test_reset_marker_applies_only_to_hourly_energy(self):
        from types import SimpleNamespace
        marker = datetime(2026, 2, 1, 11, tzinfo=timezone.utc)
        self.ctx.data = SimpleNamespace(last_hour_reset=marker)
        for key in ["electricity_last_hour", "compressor_last_hour", "eheater_last_hour", "heat_output_last_hour"]:
            self.assertEqual(self.entity(key).last_reset, marker)
        for key in ["electricity_month", "heat_output_month", "cop_last_hour", "cop_month"]:
            self.assertIsNone(self.entity(key).last_reset)

    def test_missing_value_is_unavailable_but_measured_zero_is_available(self):
        from types import SimpleNamespace
        entity = self.entity("electricity_month")
        self.ctx.data = SimpleNamespace(electricity_kwh_month=None)
        self.assertFalse(entity.available)
        self.ctx.data.electricity_kwh_month = 0
        self.assertTrue(entity.available)
        self.ctx.last_update_success = False
        self.assertFalse(entity.available)


if __name__ == "__main__":
    unittest.main()
