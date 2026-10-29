"""Regression tests using synthetic PointT data, without credentials or network."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math
import unittest
from unittest.mock import AsyncMock, patch

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
        self.month_values = {}
        self.error = None

    async def bulk(self, gateway, paths):
        if self.error:
            raise self.error
        rows = []
        for path in paths:
            kind = path.split("energyMonitoring/")[1].split("?")[0]
            monthly = len(path.split("interval=")[1]) == 7
            p = (
                payload(self.month_values.get(kind, self.month_value), 1440, "P1D")
                if monthly
                else payload(length=24)
            )
            if self.empty:
                p["recording"] = []
            if kind == self.missing:
                p = None
            rows.append({"resourcePath": path, "gatewayResponse": {"status": 200, "payload": p}})
        return [{"resourcePaths": rows}]


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sleeper = patch.object(coordinator.asyncio, "sleep", new_callable=AsyncMock)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        clock.now = lambda: datetime(2026, 2, 2, 0, tzinfo=timezone.utc)
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

    async def test_today_uses_hours_even_when_daily_projection_revises(self):
        original_bulk = self.api.bulk
        projection = {"y": 600, "c": 480}

        async def bulk(gateway, paths):
            response = await original_bulk(gateway, paths)
            for entry in response[0]["resourcePaths"]:
                p = entry["gatewayResponse"]["payload"]
                if p["sampleRate"] == "P1D":
                    p["recording"].append(dict(projection))
            return response

        self.api.bulk = bulk
        clock.now = lambda: datetime(2026, 2, 2, 8, tzinfo=timezone.utc)
        data = await self.update()
        self.assertEqual(data.compressor_kwh_month, 18)
        projection.update(y=120, c=60)
        self.assertEqual((await self.update()).compressor_kwh_month, 18)
        clock.now = lambda: datetime(2026, 2, 2, 9, tzinfo=timezone.utc)
        self.assertEqual((await self.update()).compressor_kwh_month, 19)

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
        clock.now = lambda: datetime(2026, 3, 1, 0, tzinfo=timezone.utc)
        self.api.month_value = 60
        data = await self.update()
        self.assertEqual(data.electricity_kwh_month, 0)
        self.assertEqual(data.month_reset, datetime(2026, 3, 1, tzinfo=timezone.utc))
        self.ctx = coordinator.IVTAnywhereIICoordinator(self.hass, self.api, "test-gateway")
        self.assertEqual((await self.update()).month_reset, data.month_reset)

    async def test_legacy_projected_peak_does_not_block_corrected_month(self):
        self.hass.storage[self.ctx._month_store.key] = {
            "month": "2026-02", "values": {"compressor": 1000, "eheater": 10, "output": 2000}
        }
        data = await self.update()
        self.assertEqual(data.electricity_kwh_month, 20)
        self.assertIsNone(data.month_reset)
        saved = self.hass.storage[self.ctx._month_store.key]
        self.assertEqual(saved["basis"], "completed_days_and_hours")
        self.ctx = coordinator.IVTAnywhereIICoordinator(self.hass, self.api, "test-gateway")
        self.assertIsNone((await self.update()).month_reset)

    async def test_invalid_saved_values_do_not_poison_counters(self):
        self.hass.storage[self.ctx._month_store.key] = {
            "month": "2026-02", "basis": "completed_days_and_hours",
            "values": {"compressor": math.nan, "eheater": True}
        }
        self.assertEqual((await self.update()).electricity_kwh_month, 20)

    async def test_api_failure_is_unavailable(self):
        self.api.error = RuntimeError("offline")
        with self.assertRaises(UpdateFailed):
            await self.update()
        self.assertEqual(self.ctx.update_interval.total_seconds(), 60)

    async def test_incomplete_reports_retry_in_one_minute_then_restore_normal_poll(self):
        self.api.empty = True
        await self.update()
        self.assertEqual(self.ctx.update_interval.total_seconds(), 60)
        self.api.empty = False
        await self.update()
        self.assertEqual(self.ctx.update_interval.total_seconds(), 1800)

    async def test_partial_bulk_retry_recovers_only_the_missing_resource(self):
        original = self.api.bulk
        requests = []
        async def bulk(gateway, paths):
            requests.append(list(paths))
            response = await original(gateway, paths)
            if len(requests) == 1:
                response[0]["resourcePaths"][0]["gatewayResponse"]["payload"] = None
            return response
        self.api.bulk = bulk
        data = await self.update()
        self.assertEqual(len(requests[1]), 1)
        self.assertIn("/compressor?", requests[1][0])
        self.assertEqual(data.electricity_kwh_last_hour, 2)
        self.assertEqual(self.ctx.update_interval.total_seconds(), 1800)

    async def test_retry_failure_preserves_successful_resource_payloads(self):
        original = self.api.bulk
        requests = []
        async def bulk(gateway, paths):
            requests.append(list(paths))
            if len(requests) == 2:
                raise RuntimeError("retry failed")
            response = await original(gateway, paths)
            if len(requests) == 1:
                response[0]["resourcePaths"][0]["gatewayResponse"]["payload"] = None
            return response
        self.api.bulk = bulk
        data = await self.update()
        self.assertIsNone(data.compressor_kwh_last_hour)
        self.assertEqual(data.eheater_kwh_last_hour, 1)
        self.assertEqual(data.heat_output_kwh_last_hour, 1)
        self.assertEqual(self.ctx.update_interval.total_seconds(), 60)

    async def test_zero_consumption_does_not_cause_fast_retry(self):
        original = self.api.bulk
        async def bulk(gateway, paths):
            response = await original(gateway, paths)
            for entry in response[0]["resourcePaths"]:
                for row in entry["gatewayResponse"]["payload"]["recording"]:
                    row["y"] = 0
            return response
        self.api.bulk = bulk
        self.assertEqual((await self.update()).electricity_kwh_month, 0)
        self.assertEqual(self.ctx.update_interval.total_seconds(), 1800)

    async def test_recorder_failure_does_not_take_energy_sensors_down(self):
        self.hass.fail_statistics = True
        with self.assertLogs(self.ctx.logger, level="WARNING"):
            data = await self.update()
        self.assertEqual(data.electricity_kwh_month, 20)
        self.assertEqual(data.electricity_kwh_last_hour, 2)

    async def test_tiny_floating_point_revision_never_decreases_counter(self):
        await self.update()
        self.api.month_value -= 1e-7
        self.assertEqual((await self.update()).electricity_kwh_month, 20)
    async def test_small_compressor_revision_keeps_total_and_cop_available(self):
        await self.update()
        # 597 / 1440 * 24 = 9.95 kWh, a 0.05 kWh revision of the accepted 10.
        self.api.month_values["compressor"] = 597
        data = await self.update()
        self.assertEqual(data.compressor_kwh_month, 10)
        self.assertEqual(data.electricity_kwh_month, 20)
        self.assertEqual(data.cop_month, 0.5)

    async def test_repeated_small_revisions_neither_decrease_nor_inflate(self):
        await self.update()
        for reading in [597, 598, 596, 599]:
            self.api.month_value = reading
            data = await self.update()
            self.assertEqual(data.electricity_kwh_month, 20)
            self.assertEqual(data.heat_output_kwh_month, 10)
        self.assertEqual(self.ctx._month_high_water, {"compressor": 10, "eheater": 10, "output": 10})

    async def test_restart_still_holds_small_cloud_revision(self):
        await self.update()
        self.api.month_values["compressor"] = 597
        await self.update()
        self.ctx = coordinator.IVTAnywhereIICoordinator(self.hass, self.api, "test-gateway")
        data = await self.update()
        self.assertEqual(data.compressor_kwh_month, 10)
        self.assertEqual(data.electricity_kwh_month, 20)

    async def test_growth_resumes_after_small_revision(self):
        await self.update()
        self.api.month_values["compressor"] = 597
        await self.update()
        self.api.month_values["compressor"] = 606
        data = await self.update()
        self.assertEqual(data.compressor_kwh_month, 10.1)
        self.assertEqual(data.electricity_kwh_month, 20.1)

    async def test_missing_report_after_small_revision_stays_unavailable(self):
        await self.update()
        self.api.month_values["compressor"] = 597
        await self.update()
        self.api.missing = "compressor"
        data = await self.update()
        self.assertIsNone(data.compressor_kwh_month)
        self.assertIsNone(data.electricity_kwh_month)
        self.assertIsNone(data.cop_month)


class MonthlyTotalTests(unittest.TestCase):
    def test_partial_day_projection_is_not_consumption(self):
        now = datetime(2026, 2, 2, 8, tzinfo=timezone.utc)
        monthly = payload(600, 1440, "P1D")
        monthly["recording"].append({"y": 600, "c": 480})
        hourly = {"interval": "2026-02-02", "sampleRate": "P1H", "recording": [
            {"y": value * 60, "c": 60} for value in [1, 2, 1, 1, 2, 1, 1, 1]
        ]}
        # The daily average extrapolates today's 10 kWh to 30. Only 10 was used.
        self.assertEqual(util.month_to_date_kwh(monthly, hourly, now), 20)
        monthly["recording"][1] = {"y": 120, "c": 60}
        self.assertEqual(util.month_to_date_kwh(monthly, hourly, now), 20)

    def test_only_completed_hours_are_counted(self):
        now = datetime(2026, 2, 1, 2, 30, tzinfo=timezone.utc)
        hourly = payload(length=24)
        hourly["recording"][2] = {"y": 999, "c": 1}
        self.assertEqual(util.month_to_date_kwh(None, hourly, now), 2)

    def test_missing_completed_hour_is_not_a_partial_total(self):
        now = datetime(2026, 2, 1, 8, tzinfo=timezone.utc)
        for bad in [{"y": 1, "c": 0}, {"y": None, "c": 60}]:
            hourly = payload(length=8)
            hourly["recording"][3] = bad
            self.assertIsNone(util.month_to_date_kwh(None, hourly, now))
        self.assertIsNone(util.month_to_date_kwh(None, payload(length=7), now))

    def test_midnight_needs_no_today_payload(self):
        first = datetime(2026, 2, 1, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(util.month_to_date_kwh(None, None, first), 0)
        later = first.replace(day=2)
        self.assertEqual(util.month_to_date_kwh(payload(600, 1440, "P1D"), None, later), 10)

    def test_wrong_day_or_rate_is_unavailable(self):
        now = datetime(2026, 2, 1, 8, tzinfo=timezone.utc)
        hourly = payload(length=8)
        hourly["interval"] = "2026-01-31"
        self.assertIsNone(util.month_to_date_kwh(None, hourly, now))
        self.assertIsNone(util.month_to_date_kwh(None, payload(rate="P1D", length=8), now))

    def test_completed_day_count_excludes_today_and_future_padding(self):
        monthly = payload(600, 1440, "P1D", length=3)
        monthly["recording"][1] = {"y": 999, "c": 1}
        monthly["recording"][2] = {"y": None, "c": 0}
        self.assertEqual(util.month_total_kwh(monthly, required_days=1), 10)

    def test_first_sample_and_growth(self):
        self.assertEqual(util.accept_monthly_total(0, None), 0)
        self.assertEqual(util.accept_monthly_total(12.5, None), 12.5)
        self.assertEqual(util.accept_monthly_total(101, 100), 101)

    def test_small_absolute_and_relative_revisions(self):
        for value, previous in [(99.95, 100), (999.5, 1000), (10 - 1e-9, 10)]:
            with self.subTest(value=value, previous=previous):
                self.assertEqual(util.accept_monthly_total(value, previous), previous)

    def test_threshold_boundaries(self):
        self.assertEqual(util.accept_monthly_total(99.9, 100), 100)
        self.assertIsNone(util.accept_monthly_total(99.899, 100))
        self.assertEqual(util.accept_monthly_total(999, 1000), 1000)
        self.assertIsNone(util.accept_monthly_total(998.99, 1000))

    def test_missing_and_invalid_values(self):
        for value in [None, -1, math.inf, -math.inf, math.nan]:
            with self.subTest(value=value):
                self.assertIsNone(util.accept_monthly_total(value, 100))

    def test_large_same_month_reset_remains_unavailable(self):
        self.assertIsNone(util.accept_monthly_total(1, 1000))


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
                self.assertEqual(description.state_class, "total")

    def test_existing_sensor_identities_are_unchanged(self):
        for key in ["electricity_month", "heat_output_month"]:
            self.assertEqual(self.entity(key)._attr_unique_id, f"test-gateway_{key}")
        self.assertEqual(self.entity("electricity_last_hour")._attr_unique_id, "test-gateway_electricity_last_hour")

    def test_reset_markers_apply_to_energy_periods_only(self):
        from types import SimpleNamespace
        marker = datetime(2026, 2, 1, 11, tzinfo=timezone.utc)
        self.ctx.data = SimpleNamespace(last_hour_reset=marker, month_reset=None)
        for key in ["electricity_last_hour", "compressor_last_hour", "eheater_last_hour", "heat_output_last_hour"]:
            self.assertEqual(self.entity(key).last_reset, marker)
        for key in ["electricity_month", "heat_output_month", "cop_last_hour", "cop_month"]:
            self.assertIsNone(self.entity(key).last_reset)
        self.ctx.data.month_reset = marker.replace(day=1, hour=0)
        self.assertEqual(self.entity("electricity_month").last_reset, self.ctx.data.month_reset)
        self.assertIsNone(self.entity("cop_month").last_reset)

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
