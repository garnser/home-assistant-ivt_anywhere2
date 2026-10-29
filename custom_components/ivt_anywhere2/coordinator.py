from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import PointtApi
from .const import DEFAULT_SCAN_INTERVAL_SECONDS
from .statistics import RecordedEnergyStatistics
from .util import (
    _extract_payload_from_bulk,
    accept_monthly_total,
    compute_cop,
    kwh_at_index,
    last_complete_hour_start,
    last_complete_hour_target,
    month_str,
    month_to_date_kwh,
)


@dataclass
class EnergyData:
    gateway_id: str
    # last complete hour
    last_hour_label: Optional[str]
    compressor_kwh_last_hour: Optional[float]
    eheater_kwh_last_hour: Optional[float]
    electricity_kwh_last_hour: Optional[float]
    heat_output_kwh_last_hour: Optional[float]
    cop_last_hour: Optional[float]
    # month-to-date totals
    compressor_kwh_month: Optional[float]
    eheater_kwh_month: Optional[float]
    electricity_kwh_month: Optional[float]
    heat_output_kwh_month: Optional[float]
    cop_month: Optional[float]
    last_hour_reset: Optional[datetime] = None
    month_reset: Optional[datetime] = None


class IVTAnywhereIICoordinator(DataUpdateCoordinator[EnergyData]):
    def __init__(self, hass: HomeAssistant, api: PointtApi, gateway_id: str) -> None:
        super().__init__(
            hass,
            logger=__import__("logging").getLogger(__name__),
            name="IVT Anywhere II Energy",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS),
        )
        self.api = api
        self.gateway_id = gateway_id
        self._month_store = Store(
            hass, 1, f"ivt_anywhere2.{gateway_id}.monthly_samples_v1"
        )
        self._month_guard_loaded = False
        self._counter_month: Optional[str] = None
        self._month_high_water: dict[str, float] = {}
        self._month_reset: Optional[datetime] = None
        self.recorded_energy = RecordedEnergyStatistics(hass, gateway_id, self.logger)

    async def _async_update_data(self) -> EnergyData:
        try:
            # Timezone-aware "now" in the Home Assistant configured timezone
            now = dt_util.now()

            # Determine the last *completed* hour in HA's timezone
            day, hour_idx, label = last_complete_hour_target(now)
            month = month_str(now)

            self.logger.debug("Energy target bucket: %s (idx=%s)", label, hour_idx)

            # Fetch day (hourly) + month (daily) in two bulk calls.
            day_paths = [
                f"/recordings/heatSources/total/energyMonitoring/compressor?interval={day}",
                f"/recordings/heatSources/total/energyMonitoring/eheater?interval={day}",
                f"/recordings/heatSources/total/energyMonitoring/outputProduced?interval={day}",
            ]
            month_paths = [
                f"/recordings/heatSources/total/energyMonitoring/compressor?interval={month}",
                f"/recordings/heatSources/total/energyMonitoring/eheater?interval={month}",
                f"/recordings/heatSources/total/energyMonitoring/outputProduced?interval={month}",
            ]

            # Some flakiness is possible → retry quickly if everything comes back empty
            async def _bulk_with_retry(paths):
                r = await self.api.bulk(self.gateway_id, paths)
                # If all payloads are null, sleep and try once more
                try:
                    entries = r[0].get("resourcePaths", [])
                    nullish = sum(
                        1 for e in entries if (e.get("gatewayResponse") or {}).get("payload") is None
                    )
                    if entries and nullish == len(entries):
                        await asyncio.sleep(0.8)
                        r = await self.api.bulk(self.gateway_id, paths)
                except Exception:
                    pass
                return r

            day_resp = await _bulk_with_retry(day_paths)
            month_resp = await _bulk_with_retry(month_paths)

            p_day_comp = _extract_payload_from_bulk(day_resp, "/energyMonitoring/compressor?interval=")
            p_day_eh = _extract_payload_from_bulk(day_resp, "/energyMonitoring/eheater?interval=")
            p_day_out = _extract_payload_from_bulk(day_resp, "/energyMonitoring/outputProduced?interval=")

            p_m_comp = _extract_payload_from_bulk(month_resp, "/energyMonitoring/compressor?interval=")
            p_m_eh = _extract_payload_from_bulk(month_resp, "/energyMonitoring/eheater?interval=")
            p_m_out = _extract_payload_from_bulk(month_resp, "/energyMonitoring/outputProduced?interval=")

            # Pick the exact bucket for the last completed hour
            comp_last = kwh_at_index(p_day_comp, hour_idx)
            eh_last = kwh_at_index(p_day_eh, hour_idx)
            out_last = kwh_at_index(p_day_out, hour_idx)

            elec_last = None
            if comp_last is not None and eh_last is not None:
                elec_last = comp_last + eh_last

            cop_last = compute_cop(out_last, elec_last)

            # Today's daily bucket is a full-day projection. Use only actual
            # completed hours for today, and daily buckets for earlier days.
            comp_m = month_to_date_kwh(p_m_comp, p_day_comp, now)
            eh_m = month_to_date_kwh(p_m_eh, p_day_eh, now)
            out_m = month_to_date_kwh(p_m_out, p_day_out, now)

            comp_m, eh_m, out_m = await self._async_accept_monthly_totals(
                month, comp_m, eh_m, out_m
            )
            elec_m = (
                comp_m + eh_m
                if comp_m is not None and eh_m is not None
                else None
            )

            cop_m = compute_cop(out_m, elec_m)

            async def fetch_recorded_day(recorded_day):
                paths = [
                    f"/recordings/heatSources/total/energyMonitoring/{kind}?interval={recorded_day}"
                    for kind in ("compressor", "eheater")
                ]
                response = await _bulk_with_retry(paths)
                return (
                    _extract_payload_from_bulk(response, "/energyMonitoring/compressor?interval="),
                    _extract_payload_from_bulk(response, "/energyMonitoring/eheater?interval="),
                )

            try:
                await self.recorded_energy.async_update(day, p_day_comp, p_day_eh, now, fetch_recorded_day)
            except Exception:
                # A recorder failure must not take the live energy sensors down.
                self.logger.warning("Unable to import recorded-hour electricity statistics", exc_info=True)

            return EnergyData(
                gateway_id=self.gateway_id,
                last_hour_label=label,
                last_hour_reset=last_complete_hour_start(now),
                month_reset=self._month_reset,
                compressor_kwh_last_hour=comp_last,
                eheater_kwh_last_hour=eh_last,
                electricity_kwh_last_hour=elec_last,
                heat_output_kwh_last_hour=out_last,
                cop_last_hour=cop_last,
                compressor_kwh_month=comp_m,
                eheater_kwh_month=eh_m,
                electricity_kwh_month=elec_m,
                heat_output_kwh_month=out_m,
                cop_month=cop_m,
            )

        except Exception as e:
            raise UpdateFailed(str(e)) from e

    async def _async_accept_monthly_totals(
        self,
        month: str,
        compressor: Optional[float],
        eheater: Optional[float],
        output: Optional[float],
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Keep cloud revisions from looking like a cumulative meter reset.

        Small downward revisions retain the accepted total; larger drops and
        missing reports remain unavailable. The accepted values survive restarts
        and are reset only at a real month boundary.
        """
        changed = False
        if not self._month_guard_loaded:
            saved = await self._month_store.async_load()
            if isinstance(saved, dict):
                self._counter_month = saved.get("month")
                values = saved.get("values", {})
                # Old stored values may contain a projected unfinished day.
                # Do not reject corrected readings against that inflated peak.
                compatible = saved.get("basis") == "completed_days_and_hours"
                changed = not compatible
                reset = saved.get("reset")
                if compatible and isinstance(reset, str):
                    try:
                        parsed = datetime.fromisoformat(reset)
                        if parsed.tzinfo is not None:
                            self._month_reset = parsed
                    except ValueError:
                        pass
                if compatible and isinstance(values, dict):
                    self._month_high_water = {
                        key: float(value)
                        for key, value in values.items()
                        if key in {"compressor", "eheater", "output"}
                        and not isinstance(value, bool)
                        and isinstance(value, (int, float))
                        and math.isfinite(value)
                        and value >= 0
                    }
            self._month_guard_loaded = True

        if self._counter_month != month:
            # On initial installation/migration keep the old None reset marker
            # so HA does not count the entire current month as a new cycle.
            # Subsequent real month boundaries get a stable explicit reset.
            self._month_reset = (
                dt_util.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
                if self._counter_month is not None else None
            )
            changed = True
            self._counter_month = month
            self._month_high_water = {}

        accepted: list[Optional[float]] = []
        for key, value in zip(
            ("compressor", "eheater", "output"),
            (compressor, eheater, output),
        ):
            previous = self._month_high_water.get(key)
            result = accept_monthly_total(value, previous)
            if result is None:
                if value is not None:
                    self.logger.debug(
                        "Ignoring invalid or large downward monthly recording revision for %s", key
                    )
                accepted.append(None)
                continue
            changed |= result != previous
            self._month_high_water[key] = result
            accepted.append(result)

        if changed:
            self._month_store.async_delay_save(
                lambda: {
                    "month": self._counter_month,
                    "basis": "completed_days_and_hours",
                    "reset": self._month_reset.isoformat() if self._month_reset else None,
                    "values": dict(self._month_high_water),
                },
                5,
            )
        return accepted[0], accepted[1], accepted[2]
