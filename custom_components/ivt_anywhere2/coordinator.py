from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import PointtApi
from .const import DEFAULT_SCAN_INTERVAL_SECONDS
from .util import (
    _extract_payload_from_bulk,
    compute_cop,
    kwh_at_index,
    last_complete_hour_target,
    month_str,
    month_total_kwh,
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
            if comp_last is not None or eh_last is not None:
                elec_last = (comp_last or 0.0) + (eh_last or 0.0)

            cop_last = compute_cop(out_last, elec_last)

            # Month-to-date totals (sum daily buckets)
            comp_m = month_total_kwh(p_m_comp)
            eh_m = month_total_kwh(p_m_eh)
            out_m = month_total_kwh(p_m_out)

            elec_m = None
            if comp_m is not None or eh_m is not None:
                elec_m = (comp_m or 0.0) + (eh_m or 0.0)

            cop_m = compute_cop(out_m, elec_m)

            return EnergyData(
                gateway_id=self.gateway_id,
                last_hour_label=label,
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
