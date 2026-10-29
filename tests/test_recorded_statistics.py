"""Synthetic delayed reports must retain their measurement timestamps."""
from datetime import date, datetime, timedelta, timezone
import logging
import unittest
from zoneinfo import ZoneInfo

from ha_stubs import hass, recorded_statistics as recorded

UTC = timezone.utc


def payload(day, value=1, count=24):
    return {"interval": day, "sampleRate": "P1H", "recording": [
        {"y": value * 60, "c": 60} for _ in range(count)
    ]}


class RecordedHoursTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hass = hass()
        self.logger = logging.getLogger("recorded_test")
        self.publisher = recorded.RecordedEnergyStatistics(self.hass, "gateway-1", self.logger)
        self.now = datetime(2026, 2, 2, 10, 3, tzinfo=UTC)
        self.comp = payload("2026-02-02")
        self.aux = payload("2026-02-02", 0)
        self.fetched = []

    async def missing_day(self, day):
        self.fetched.append(day)
        return None, None

    async def complete_day(self, day):
        self.fetched.append(day)
        return payload(day), payload(day, 0)

    async def update(self, fetch=None):
        await self.publisher.async_update("2026-02-02", self.comp, self.aux, self.now, fetch or self.missing_day)
        return self.hass.statistics[self.publisher.statistic_id]

    async def test_late_report_is_in_covered_hour_not_receipt_hour(self):
        rows = await self.update()
        covered = datetime(2026, 2, 2, 9, tzinfo=UTC)
        self.assertIn(covered, rows)
        self.assertEqual(rows[covered]["sum"] - rows[covered - timedelta(hours=1)]["sum"], 1)
        self.assertNotIn(covered + timedelta(hours=1), rows)
        self.assertEqual(self.hass.imports[-1][0]["source"], "ivt_anywhere2")
        self.assertEqual(self.publisher.statistic_id, "ivt_anywhere2:gateway_1_electricity_recorded")

    async def test_first_hour_has_counter_origin(self):
        rows = await self.update()
        midnight = datetime(2026, 2, 2, tzinfo=UTC)
        baseline = min(rows)
        self.assertEqual(rows[baseline]["sum"], 0)
        self.assertEqual(rows[midnight]["sum"], 1)

    async def test_repeated_poll_and_restart_do_not_duplicate(self):
        first = dict(await self.update())
        self.assertEqual(await self.update(), first)
        self.publisher = recorded.RecordedEnergyStatistics(self.hass, "gateway-1", self.logger)
        self.assertEqual(await self.update(), first)

    async def test_new_hour_increments_once(self):
        await self.update()
        self.now = self.now.replace(hour=11)
        rows = await self.update()
        self.assertEqual(rows[datetime(2026, 2, 2, 10, tzinfo=UTC)]["sum"], 11)
        self.assertEqual((await self.update())[datetime(2026, 2, 2, 10, tzinfo=UTC)]["sum"], 11)

    async def test_missing_hour_is_absent_and_late_fill_updates_later_sums(self):
        self.comp["recording"][3] = {"y": 1, "c": 0}
        rows = await self.update()
        missing = datetime(2026, 2, 2, 3, tzinfo=UTC)
        latest = datetime(2026, 2, 2, 9, tzinfo=UTC)
        self.assertNotIn(missing, rows)
        self.assertEqual(rows[latest]["sum"], 9)
        self.comp["recording"][3] = {"y": 60, "c": 60}
        rows = await self.update()
        self.assertEqual(rows[missing]["sum"], 4)
        self.assertEqual(rows[latest]["sum"], 10)

    async def test_measured_zero_is_preserved(self):
        self.comp["recording"][3] = {"y": 0, "c": 60}
        rows = await self.update()
        before = datetime(2026, 2, 2, 2, tzinfo=UTC)
        self.assertEqual(rows[before]["sum"], rows[before + timedelta(hours=1)]["sum"])

    async def test_correction_replaces_original_hour_and_recomputes_future(self):
        await self.update()
        self.comp["recording"][3] = {"y": 120, "c": 60}
        rows = await self.update()
        self.assertEqual(rows[datetime(2026, 2, 2, 3, tzinfo=UTC)]["sum"], 5)
        self.assertEqual(rows[datetime(2026, 2, 2, 9, tzinfo=UTC)]["sum"], 11)

    async def test_temporary_missing_payload_does_not_erase_known_hours(self):
        first = dict(await self.update())
        self.comp = None
        self.assertEqual(await self.update(), first)

    async def test_late_previous_day_fills_without_a_negative_old_anchor(self):
        await self.update()
        rows = await self.update(self.complete_day)
        midnight = datetime(2026, 2, 2, tzinfo=UTC)
        self.assertEqual(rows[midnight]["sum"], 25)
        sums = [r["sum"] for _, r in sorted(rows.items())]
        self.assertEqual(sums, sorted(sums))

    async def test_month_rollover_preserves_cumulative_counter(self):
        await self.publisher.async_update("2026-01-31", payload("2026-01-31"), payload("2026-01-31", 0),
                                          datetime(2026, 1, 31, 23, tzinfo=UTC), self.missing_day)
        await self.publisher.async_update("2026-02-01", payload("2026-02-01"), payload("2026-02-01", 0),
                                          datetime(2026, 2, 1, 1, tzinfo=UTC), self.complete_day)
        rows = self.hass.statistics[self.publisher.statistic_id]
        # Catch-up also fills the previously missing Jan 30; the Feb 1 hour
        # still contributes exactly one unit without resetting the counter.
        latest = rows[datetime(2026, 2, 1, tzinfo=UTC)]["sum"]
        previous = rows[datetime(2026, 1, 31, 23, tzinfo=UTC)]["sum"]
        self.assertEqual(latest, 49)
        self.assertEqual(latest - previous, 1)

    async def test_catchup_is_bounded_and_persists_pending_days(self):
        await self.publisher.async_update("2026-02-01", payload("2026-02-01"), payload("2026-02-01", 0),
                                          datetime(2026, 2, 1, 12, tzinfo=UTC), self.missing_day)
        self.fetched.clear()
        await self.publisher.async_update("2026-02-10", payload("2026-02-10"), payload("2026-02-10", 0),
                                          datetime(2026, 2, 10, 12, tzinfo=UTC), self.complete_day)
        self.assertEqual(len(self.fetched), recorded.MAX_CATCHUP_DAYS)
        saved = self.hass.storage[self.publisher._store.key]
        self.assertIn("2026-02-08", saved["pending"])
        self.assertEqual(saved["last_day"], "2026-02-10")

    async def test_recorder_failure_retries_without_losing_journal(self):
        self.hass.fail_statistics = True
        with self.assertRaises(RuntimeError):
            await self.update()
        self.hass.fail_statistics = False
        rows = await self.update()
        self.assertEqual(rows[datetime(2026, 2, 2, 9, tzinfo=UTC)]["sum"], 10)

    async def test_restart_reimports_journal_if_recorder_history_was_removed(self):
        first = dict(await self.update())
        self.hass.statistics.clear()
        self.publisher = recorded.RecordedEnergyStatistics(self.hass, "gateway-1", self.logger)
        self.assertEqual(await self.update(), first)

    async def test_retention_preserves_cumulative_origin_and_old_recorder_points(self):
        old_hour = datetime(2025, 10, 1, tzinfo=UTC)
        current = datetime(2026, 2, 2, tzinfo=UTC)
        self.hass.storage[self.publisher._store.key] = {
            "hours": {old_hour.isoformat(): 4, current.isoformat(): 1},
            "anchor": (old_hour - timedelta(hours=1)).isoformat(),
            "base": 0, "last_day": "2026-02-02", "pending": [],
        }
        rows = await self.update()
        self.assertEqual(rows[datetime(2026, 2, 2, 9, tzinfo=UTC)]["sum"], 14)
        saved = self.hass.storage[self.publisher._store.key]
        self.assertNotIn(old_hour.isoformat(), saved["hours"])
        self.assertEqual(saved["base"], 4)
        self.publisher = recorded.RecordedEnergyStatistics(self.hass, "gateway-1", self.logger)
        self.assertEqual((await self.update())[datetime(2026, 2, 2, 9, tzinfo=UTC)]["sum"], 14)

    async def test_invalid_saved_shapes_do_not_break_new_reports(self):
        self.hass.storage[self.publisher._store.key] = {
            "hours": None, "pending": None, "anchor": "invalid", "last_day": "invalid", "base": False,
        }
        self.assertEqual((await self.update())[datetime(2026, 2, 2, 9, tzinfo=UTC)]["sum"], 10)


class HourMappingTests(unittest.TestCase):
    def test_stockholm_local_hour_maps_to_utc(self):
        now = datetime(2026, 10, 29, 10, 3, tzinfo=ZoneInfo("Europe/Stockholm"))
        rows = recorded.electricity_hours(now.date(), payload("2026-10-29"), payload("2026-10-29", 0), now)
        self.assertIn("2026-10-29T08:00:00+00:00", rows)
        self.assertNotIn("2026-10-29T09:00:00+00:00", rows)

    def test_dst_fall_repeated_hours_remain_distinct(self):
        now = datetime(2026, 10, 26, tzinfo=ZoneInfo("Europe/Stockholm"))
        rows = recorded.electricity_hours(date(2026, 10, 25), payload("2026-10-25", count=25),
                                          payload("2026-10-25", 0, 25), now)
        self.assertEqual(len(rows), 25)
        self.assertIn("2026-10-25T00:00:00+00:00", rows)
        self.assertIn("2026-10-25T01:00:00+00:00", rows)

    def test_dst_spring_has_23_hours(self):
        now = datetime(2026, 3, 30, tzinfo=ZoneInfo("Europe/Stockholm"))
        rows = recorded.electricity_hours(date(2026, 3, 29), payload("2026-03-29", count=23),
                                          payload("2026-03-29", 0, 23), now)
        self.assertEqual(len(rows), 23)

    def test_ambiguous_dst_or_wrong_interval_is_not_guessed(self):
        now = datetime(2026, 10, 26, tzinfo=ZoneInfo("Europe/Stockholm"))
        self.assertEqual(recorded.electricity_hours(date(2026, 10, 25), payload("2026-10-25"),
                                                   payload("2026-10-25", 0), now), {})
        self.assertEqual(recorded.electricity_hours(date(2026, 10, 25), payload("2026-10-24", count=25),
                                                   payload("2026-10-25", 0, 25), now), {})
