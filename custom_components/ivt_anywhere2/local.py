"""Independent, read-only local energy sampling and optional cloud comparison."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp
from bosch_thermostat_client.exceptions import DeviceException
from bosch_thermostat_client.gateway.ivt import IVTGateway
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .util import kwh_at_index, last_complete_hour_target, month_str, month_to_date_kwh

_LOGGER = logging.getLogger(__name__)
KINDS = ("compressor", "eheater", "output")
LOCAL_POLL_INTERVAL = timedelta(minutes=5)
CLOUD_MAX_AGE = timedelta(minutes=35)
REQUEST_PAUSE_SECONDS = 1
RETRY_PAUSE_SECONDS = 2
RECORDING_KEYS = (
    "interval",
    "sampleRate",
    "recording-type",
    "type",
    "unitOfMeasure",
    "recording",
)


def build_comparison(local, cloud, cloud_success, now, label, hour_index):
    """Calculate local values even when cloud data is absent, failed or stale."""
    values = {}
    for kind in KINDS:
        values[f"{kind}_hour"] = kwh_at_index(local["day"].get(kind), hour_index)
        values[f"{kind}_month"] = month_to_date_kwh(
            local["month"].get(kind), local["day"].get(kind), now
        )
    differences = {key: None for key in values}
    cloud_fetched_at = cloud.get("fetched_at") if cloud else None
    if not cloud_success or not cloud:
        status = "cloud_unavailable"
    else:
        try:
            fetched = datetime.fromisoformat(cloud_fetched_at)
            age = now - fetched
        except (TypeError, ValueError):
            age = None
        if age is None or age > CLOUD_MAX_AGE or age < timedelta(0):
            status = "cloud_stale"
        elif cloud.get("target") != label or any(
            (cloud.get("month", {}).get(kind) or {}).get("interval")
            != (local["month"].get(kind) or {}).get("interval")
            for kind in KINDS
        ):
            status = "awaiting_same_bucket"
        else:
            # Compare matching periods, without the cloud sensor's high-water guard.
            for kind in KINDS:
                cloud_day = cloud.get("day", {}).get(kind)
                cloud_month = cloud.get("month", {}).get(kind)
                for period in ("hour", "month"):
                    key = f"{kind}_{period}"
                    other = (
                        kwh_at_index(cloud_day, hour_index)
                        if period == "hour"
                        else month_to_date_kwh(cloud_month, cloud_day, now)
                    )
                    value = values[key]
                    if value is not None and other is not None:
                        differences[key] = round(value - other, 6)
            status = (
                "incomplete"
                if any(v is None for v in differences.values())
                else "different"
                if any(abs(v) > 0.05 for v in differences.values())
                else "matching"
            )
    return {
        "status": status,
        "sampled_at": now.isoformat(),
        "cloud_fetched_at": cloud_fetched_at,
        "bucket": label,
        "values": values,
        "differences_kwh": differences,
        "poll_interval_seconds": int(LOCAL_POLL_INTERVAL.total_seconds()),
    }


class LocalEnergyCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, session, cloud, credentials, host, poll_seconds=300):
        super().__init__(
            hass,
            _LOGGER,
            config_entry=None,
            name="IVT local energy comparison",
            update_interval=timedelta(seconds=poll_seconds),
        )
        self.cloud = cloud
        self.gateway_id = cloud.gateway_id
        self.host = host
        self.gateway = IVTGateway(
            session=session,
            session_type="HTTP",
            host=host,
            access_token=credentials["access_token"],
            access_key=credentials.get("access_key"),
            password=credentials.get("password"),
        )
        self.store = Store(hass, 1, f"ivt_anywhere2.{self.gateway_id}.local_comparison")
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self.history = None
        self.gateway._connector.set_timeout(8)

    async def _async_read_recording(self, path):
        # The model and local coordinator share this connector. Serialize and
        # pace both callers, including historical model requests.
        async with self._request_lock:
            loop = asyncio.get_running_loop()
            delay = self._last_request_at + REQUEST_PAUSE_SECONDS - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                return await self._read_with_retry(path)
            finally:
                self._last_request_at = loop.time()

    async def _read_with_retry(self, path):
        """Retry a transient transport failure once, without retrying access errors."""
        try:
            # Preserve transport errors; raw_query logs and swallows them.
            return await self.gateway._connector.get(path)
        except DeviceException as err:
            if not isinstance(
                err.__context__, (TimeoutError, aiohttp.ClientConnectionError)
            ):
                raise
            _LOGGER.debug(
                "Retrying local recording after a temporary connection failure: %s",
                path,
            )
            await asyncio.sleep(RETRY_PAUSE_SECONDS)
            return await self.gateway._connector.get(path)

    async def _async_update_data(self):
        try:
            now = dt_util.now()
            day, hour_index, label = last_complete_hour_target(now)
            month = month_str(now)
            local = {"day": {}, "month": {}}
            first_request = True
            for period, interval in (("day", day), ("month", month)):
                for kind in KINDS:
                    resource = "outputProduced" if kind == "output" else kind
                    path = f"/recordings/heatSources/total/energyMonitoring/{resource}?interval={interval}"
                    if not first_request:
                        await asyncio.sleep(REQUEST_PAUSE_SECONDS)
                    first_request = False
                    try:
                        payload = await self._async_read_recording(path)
                    except Exception as err:
                        _LOGGER.debug(
                            "Local %s %s read failed (%s)",
                            period,
                            kind,
                            type(err).__name__,
                        )
                        payload = None
                    local[period][kind] = (
                        {key: payload[key] for key in RECORDING_KEYS if key in payload}
                        if isinstance(payload, dict) and "recording" in payload
                        else None
                    )
            cloud = getattr(self.cloud, "recording_diagnostics", None)
            result = build_comparison(
                local, cloud, self.cloud.last_update_success, now, label, hour_index
            )
            result["host"] = self.host
            result["month"] = month
            result["poll_interval_seconds"] = int(self.update_interval.total_seconds())
            if all(v is None for v in result["values"].values()):
                raise UpdateFailed("No usable local energy recordings")
            if self.history is None:
                saved = await self.store.async_load()
                self.history = (saved or {}).get("samples", [])
            self.history.append(
                {**result, "local_recordings": local, "cloud_recordings": cloud}
            )
            # Five-minute sampling retains at least the intended two-day comparison window.
            self.history = self.history[-576:]
            await self.store.async_save({"samples": self.history})
            return result
        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(
                f"Local energy read failed ({type(err).__name__})"
            ) from err
