"""Persistent, independently scheduled local heat model with guarded learning."""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import datetime, timedelta
from functools import partial

from homeassistant.components.recorder import get_instance, history
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .estimate_math import (
    ENTITIES,
    TANK_ENTITY,
    energy_point,
    estimate_window,
    live_thermal_evidence,
    number,
)
from .gap_fill import fill_short_gaps
from .learning import apply_model, assess_day, fit_model, observe_day, parent_point
from .model_config import ModelSettings
from .month import aggregate_month, project_month

_LOGGER = logging.getLogger(__name__)
KINDS = ("compressor", "eheater", "outputProduced")
RETENTION_DAYS = 180


class HeatEstimateCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, local, settings: ModelSettings):
        super().__init__(
            hass,
            _LOGGER,
            name="IVT background heat model",
            config_entry=entry,
            update_interval=timedelta(minutes=5),
        )
        self.settings = settings
        self.entity_map = dict(
            zip(
                ENTITIES,
                (
                    settings.supply_entity,
                    settings.return_entity,
                    settings.modulation_entity,
                ),
            )
        )
        if settings.tank_entity:
            self.entity_map[TANK_ENTITY] = settings.tank_entity
        self.local = local
        self.gateway_id = local.gateway_id
        self._cache = {}
        self.store = Store(hass, 1, f"ivt_anywhere2.{self.gateway_id}.heat_model")
        self._saved = None

    async def _recordings(self, end, window_hours=24):
        start = end - timedelta(hours=window_hours)
        required = {}
        for i in range(window_hours):
            t = (start + timedelta(hours=i)).astimezone(dt_util.DEFAULT_TIME_ZONE)
            required.setdefault(t.strftime("%Y-%m-%d"), set()).add(t.hour)
        snapshots = list(self.local.history or [])
        records = {}
        for day, hours in required.items():
            for kind in KINDS:
                key = (day, kind)
                sample_kind = "output" if kind == "outputProduced" else kind
                candidates = []
                for sample in reversed(snapshots):
                    payload = (
                        (sample.get("local_recordings") or {})
                        .get("day", {})
                        .get(sample_kind)
                    )
                    if isinstance(payload, dict) and payload.get("interval") == day:
                        candidates.append(payload)
                        break
                if key in self._cache:
                    candidates.append(self._cache[key])
                payload = next(
                    (
                        p
                        for p in candidates
                        if all(energy_point(p, day, h) is not None for h in hours)
                    ),
                    None,
                )
                if payload is None:
                    try:
                        await asyncio.sleep(1)
                        path = f"/recordings/heatSources/total/energyMonitoring/{kind}?interval={day}"
                        payload = await asyncio.wait_for(
                            self.local._async_read_recording(path), timeout=18
                        )
                        if (
                            isinstance(payload, dict)
                            and payload.get("interval") == day
                            and payload.get("sampleRate") == "P1H"
                        ):
                            self._cache[key] = payload
                        else:
                            payload = candidates[0] if candidates else None
                    except Exception as err:
                        _LOGGER.debug(
                            "Model recording unavailable for %s (%s)",
                            day,
                            type(err).__name__,
                        )
                        payload = candidates[0] if candidates else None
                if payload is not None:
                    records[key] = payload
        self._cache = {k: v for k, v in self._cache.items() if k[0] in required}
        return records

    def _daily_parents(self, day):
        """Use local observations only; no dependency on a cloud refresh."""
        parents = {}
        for kind in KINDS:
            sample_kind = "output" if kind == "outputProduced" else kind
            for sample in reversed(self.local.history or []):
                # A stale saved monthly snapshot cannot establish stability.
                try:
                    age = dt_util.utcnow() - datetime.fromisoformat(
                        sample["sampled_at"]
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if not timedelta(0) <= age <= timedelta(minutes=15):
                    continue
                payload = (
                    (sample.get("local_recordings") or {})
                    .get("month", {})
                    .get(sample_kind)
                )
                point = parent_point(payload, day)
                if point is not None:
                    parents[kind] = point
                    break
        return parents

    async def _learn(self, baseline, records, now, brine):
        if self._saved is None:
            self._saved = await self.store.async_load() or {}
        signature = self.settings.signature()
        if self._saved.get("signature") != signature:
            self._saved = {"signature": signature, "hours": {}, "days": {}}
        saved = deepcopy(self._saved)
        # Always retain the unadjusted baseline, never our own predictions as labels.
        for source in baseline["hours"]:
            row = deepcopy(source)
            t = datetime.fromisoformat(row["hour"])
            day = t.date().isoformat()
            row["raw"] = {}
            for kind in KINDS:
                payload = records.get((day, kind))
                if (
                    isinstance(payload, dict)
                    and len(payload.get("recording", [])) > t.hour
                ):
                    row["raw"][kind] = payload["recording"][t.hour]
            saved["hours"][row["hour"]] = row
        cutoff = (now - timedelta(days=RETENTION_DAYS)).date().isoformat()
        saved["hours"] = {k: v for k, v in saved["hours"].items() if k[:10] >= cutoff}
        saved["days"] = {k: v for k, v in saved["days"].items() if k >= cutoff}
        today = now.astimezone(dt_util.DEFAULT_TIME_ZONE).date().isoformat()
        grouped = {}
        for row in saved["hours"].values():
            grouped.setdefault(row["hour"][:10], []).append(row)
        for day, rows in grouped.items():
            if day >= today:
                continue
            parents = self._daily_parents(day)
            # Keep a previously settled day when its month resource rolls off.
            previous = saved["days"].get(day)
            if not parents and previous and previous.get("qualified"):
                continue
            candidate = assess_day(
                day,
                sorted(rows, key=lambda r: r["hour"]),
                parents,
                self.settings.reported_heat_includes_auxiliary,
            )
            saved["days"][day] = observe_day(candidate, previous, now)
        fitted = fit_model(list(saved["days"].values()), now)
        if not self.settings.calibration_enabled:
            fitted.update(status="calibration_disabled", factor=1.0)
        rejected = {}
        for day in saved["days"].values():
            if not day.get("qualified"):
                rejected[day["status"]] = rejected.get(day["status"], 0) + 1
        saved.update(model=fitted, updated_at=now.isoformat())
        await self.store.async_save(saved)
        self._saved = saved
        return fitted, rejected, len(saved["hours"])

    async def _async_update_data(self):
        try:
            now = dt_util.utcnow()
            end = now.replace(minute=0, second=0, microsecond=0)
            if self._saved is None:
                self._saved = await self.store.async_load() or {}
            # Seed the preceding full calendar day once on first installation.
            window_hours = 48 if not self._saved.get("hours") else 24
            start = end - timedelta(hours=window_hours, minutes=15)
            brine = self.settings.assumed_source_c
            manual_factor = self.settings.performance_factor
            records = await self._recordings(end, window_hours)
            raw = await get_instance(self.hass).async_add_executor_job(
                partial(
                    history.get_significant_states,
                    self.hass,
                    start,
                    now,
                    entity_ids=list(self.entity_map.values()),
                    include_start_time_state=True,
                    significant_changes_only=False,
                    minimal_response=False,
                    no_attributes=False,
                )
            )
            operating = {}
            for canonical, entity in self.entity_map.items():
                converted = []
                for state in raw.get(entity, []):
                    value = number(state.state)
                    unit = state.attributes.get("unit_of_measurement")
                    if canonical != ENTITIES[2]:
                        if value is not None and unit == "°F":
                            value = (value - 32) * 5 / 9
                        elif unit != "°C":
                            value = None
                    elif unit != "%":
                        value = None
                    converted.append(
                        {"state": value, "last_updated": state.last_updated.isoformat()}
                    )
                operating[canonical] = converted
            baseline = await self.hass.async_add_executor_job(
                partial(
                    estimate_window,
                    operating,
                    records,
                    end,
                    dt_util.DEFAULT_TIME_ZONE,
                    brine,
                    1.0,
                    self.settings,
                )
            )
            learning_rows = deepcopy(baseline)
            if window_hours == 48:
                preceding = await self.hass.async_add_executor_job(
                    partial(
                        estimate_window,
                        operating,
                        records,
                        end - timedelta(hours=24),
                        dt_util.DEFAULT_TIME_ZONE,
                        brine,
                        1.0,
                        self.settings,
                    )
                )
                learning_rows["hours"] = preceding["hours"] + learning_rows["hours"]
            fitted, rejected, hours = await self._learn(
                learning_rows, records, now, brine
            )
            result = apply_model(baseline, fitted["factor"], manual_factor)
            # Persist only raw observations above. Gap filling is a separate
            # presentation estimate, so inferred values cannot train themselves.
            result = await self.hass.async_add_executor_job(
                partial(
                    fill_short_gaps,
                    result,
                    list(self._saved["hours"].values()),
                    fitted["factor"],
                    manual_factor,
                )
            )
            live = live_thermal_evidence(operating, now, self.settings)
            for entity in (self.entity_map[e] for e in ENTITIES):
                state = self.hass.states.get(entity)
                if state is None or now - state.last_reported > timedelta(minutes=20):
                    live.update(thermal_activity="Unknown", supply_return_delta_c=None)
                    break
            result.update(live)
            month = aggregate_month(
                self._saved["hours"].values(),
                end,
                dt_util.DEFAULT_TIME_ZONE,
                fitted["factor"],
                manual_factor,
            )
            projection = project_month(month, result, self.local.data, now)
            result.update(
                month_projection=projection,
                projected_heat_month_kwh=projection.get("heat_kwh"),
                projected_cop_month=projection.get("cop"),
            )
            result.update(
                month=month,
                month_modelled_heat_kwh=month["heat_kwh"],
                month_modelled_electricity_kwh=month["electricity_kwh"],
                month_modelled_cop=month["cop"],
            )
            result.update(
                {
                    "updated_at": now.isoformat(),
                    "model_status": fitted["status"],
                    "model": "Persistent manufacturer-anchored temperature model with quality-gated daily statistical calibration; estimated heat",
                    "poll_interval_seconds": 300,
                    "calibration": fitted,
                    "rejected_training_days": rejected,
                    "retained_hourly_observations": hours,
                    "retention_days": RETENTION_DAYS,
                    "data_source": "Local iCom electricity and Home Assistant operating history; cloud independent",
                    "settings": self.settings.signature(),
                    "assumptions": "Brine temperature is assumed. The configured manufacturer rating anchors the temperature curve until a learned adjustment passes chronological validation. Supply/return difference and compressor activity identify heat transfer. Rising tank temperature identifies probable DHW charging and sets a minimum hot-side temperature. Auxiliary heat COP1. Tank stored-energy change is diagnostic and is not added to compressor heat.",
                    "calibration_limit": "Gateway energy consistency is a quality check, not independent heat-meter validation. No measured water flow; actual efficiency remains unknown.",
                }
            )
            return result
        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(
                f"Background heat model unavailable ({type(err).__name__})"
            ) from err
