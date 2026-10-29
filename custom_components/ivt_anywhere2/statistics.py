"""Import cloud readings at their measurement hour, not their arrival time."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import logging
import math
import re
from typing import Awaitable, Callable, Optional

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .util import kwh_at_index

DayFetcher = Callable[[str], Awaitable[tuple[Optional[dict], Optional[dict]]]]
MAX_CATCHUP_DAYS = 3
JOURNAL_DAYS = 62


def electricity_hours(day: date, compressor, heater, now: datetime) -> dict[str, float]:
    """Return valid completed hours, using UTC keys even across DST folds.

    Missing buckets are absent, not measured zeros. A DST day's response must
    have its actual 23/25 slots: a 24-slot response is ambiguous on those days.
    """
    start = datetime.combine(day, time.min, tzinfo=now.tzinfo).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=now.tzinfo).astimezone(timezone.utc)
    slots = int((end - start).total_seconds() / 3600)
    for payload in (compressor, heater):
        if not isinstance(payload, dict) or payload.get("sampleRate") != "P1H":
            return {}
        if payload.get("interval") not in (None, day.isoformat()):
            return {}
        rows = payload.get("recording")
        if not isinstance(rows, list) or (slots != 24 and len(rows) != slots):
            return {}
    cutoff = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    result = {}
    for index in range(slots):
        hour = start + timedelta(hours=index)
        if hour + timedelta(hours=1) > cutoff:
            break
        comp, aux = kwh_at_index(compressor, index), kwh_at_index(heater, index)
        if comp is not None and aux is not None and math.isfinite(comp + aux):
            result[hour.isoformat()] = comp + aux
    return result


class RecordedEnergyStatistics:
    """Maintain an idempotent journal of measured hours and cumulative sums.

    The journal lets a late/corrected hour replace its original point and
    recompute later sums. Counter origin survives calendar and HA restarts.
    Current/previous days are refreshed; missed days are caught up in bounded
    batches. Recorder remains the long-term statistics store.
    """

    def __init__(self, hass, gateway_id: str, logger: logging.Logger):
        self.hass, self.logger = hass, logger
        slug = re.sub(r"[^a-z0-9_]", "_", str(gateway_id).lower())
        self.statistic_id = f"{DOMAIN}:{slug}_electricity_recorded"
        self._name = f"IVT {gateway_id} electricity (recorded hours)"
        self._store = Store(hass, 1, f"{DOMAIN}.{gateway_id}.recorded_hours_v1")
        self._loaded = False
        self._republish = True
        self._hours: dict[str, float] = {}
        self._pending: set[str] = set()
        self._last_day: Optional[str] = None
        self._anchor: Optional[str] = None
        self._base = 0.0

    async def async_update(self, day: str, compressor, heater, now: datetime, fetch_day: DayFetcher):
        target = date.fromisoformat(day)
        if not self._loaded:
            saved = await self._store.async_load()
            if isinstance(saved, dict):
                base = saved.get("base", 0)
                if not isinstance(base, bool) and isinstance(base, (int, float)) and math.isfinite(base) and base >= 0:
                    self._base = float(base)
                hours = saved.get("hours", {})
                if not isinstance(hours, dict):
                    hours = {}
                for key, value in hours.items():
                    try:
                        stamp = datetime.fromisoformat(key)
                        if (stamp.tzinfo is not None and stamp.minute == stamp.second == stamp.microsecond == 0
                                and not isinstance(value, bool) and isinstance(value, (int, float))
                                and math.isfinite(value) and value >= 0):
                            self._hours[stamp.astimezone(timezone.utc).isoformat()] = float(value)
                    except (ValueError, TypeError):
                        continue
                pending = saved.get("pending", [])
                if not isinstance(pending, list):
                    pending = []
                for value in pending:
                    try:
                        self._pending.add(date.fromisoformat(value).isoformat())
                    except (ValueError, TypeError):
                        continue
                self._last_day = saved.get("last_day")
                try:
                    anchor = datetime.fromisoformat(saved["anchor"])
                    if anchor.tzinfo is not None and anchor.minute == anchor.second == anchor.microsecond == 0:
                        canonical = anchor.astimezone(timezone.utc).isoformat()
                        if not self._hours or canonical < min(self._hours):
                            self._anchor = canonical
                except (KeyError, ValueError, TypeError):
                    pass
            self._loaded = True

        previous = target - timedelta(days=1)
        oldest_day = now.date() - timedelta(days=JOURNAL_DAYS)
        self._pending = {value for value in self._pending if oldest_day.isoformat() <= value < day}
        if self._anchor is None:
            origin = datetime.combine(previous, time.min, tzinfo=now.tzinfo).astimezone(timezone.utc)
            if self._hours:
                origin = min(origin, datetime.fromisoformat(min(self._hours)))
            self._anchor = (origin - timedelta(hours=1)).isoformat()
        try:
            cursor = date.fromisoformat(self._last_day) if self._last_day else target
        except (ValueError, TypeError):
            cursor = target
        cursor = max(cursor, oldest_day)
        while cursor < target:
            self._pending.add(cursor.isoformat())
            cursor += timedelta(days=1)
        self._last_day = day

        earliest = None
        refresh_start = datetime.combine(previous, time.min, tzinfo=now.tzinfo).astimezone(timezone.utc).isoformat()

        def remember(which: date, comp, aux):
            nonlocal earliest
            points = electricity_hours(which, comp, aux, now)
            for stamp, value in points.items():
                if stamp <= self._anchor:
                    continue
                if self._hours.get(stamp) != value:
                    earliest = min(earliest, stamp) if earliest else stamp
                    self._hours[stamp] = value
            start = datetime.combine(which, time.min, tzinfo=now.tzinfo).astimezone(timezone.utc)
            end = datetime.combine(which + timedelta(days=1), time.min, tzinfo=now.tzinfo).astimezone(timezone.utc)
            return len(points) == int((end - start).total_seconds() / 3600)

        remember(target, compressor, heater)
        candidates = [previous.isoformat()] + sorted(self._pending - {previous.isoformat(), day})
        for old_day in candidates[:MAX_CATCHUP_DAYS]:
            if old_day >= day:
                continue
            try:
                comp, aux = await fetch_day(old_day)
                complete = remember(date.fromisoformat(old_day), comp, aux)
            except Exception as err:
                self.logger.debug("Recorded-hour catch-up for %s failed: %s", old_day, type(err).__name__)
                complete = False
            if complete:
                self._pending.discard(old_day)
            else:
                self._pending.add(old_day)

        # Bound storage while preserving the cumulative origin. The last
        # removed measured point is a counter baseline, not invented usage.
        cutoff = datetime.combine(oldest_day, time.min, tzinfo=now.tzinfo).astimezone(timezone.utc).isoformat()
        removed = sorted(stamp for stamp in self._hours if stamp < cutoff)
        for stamp in removed:
            self._base += self._hours.pop(stamp)
            self._anchor = stamp
        if removed:
            self._republish = True
        if self._hours:
            start_import = self._anchor if self._republish else min(refresh_start, earliest or refresh_start)
            total = self._base
            rows = []
            if start_import == self._anchor:
                rows.append({"start": datetime.fromisoformat(self._anchor), "sum": self._base, "state": self._base})
            for stamp, value in sorted(self._hours.items()):
                total += value
                if not math.isfinite(total):
                    raise ValueError("Recorded electricity total overflow")
                if stamp >= start_import:
                    rows.append({"start": datetime.fromisoformat(stamp), "sum": total, "state": total})
            self._republish = True  # Retry the full journal if recorder rejects the import.
            async_add_external_statistics(self.hass, {
                "statistic_id": self.statistic_id, "source": DOMAIN,
                "name": self._name, "mean_type": StatisticMeanType.NONE,
                "has_sum": True, "unit_class": "energy", "unit_of_measurement": "kWh",
            }, rows)
            self._republish = False
        self._store.async_delay_save(lambda: {
            "hours": dict(self._hours), "pending": sorted(self._pending),
            "last_day": self._last_day, "anchor": self._anchor,
            "base": self._base,
        }, 5)
