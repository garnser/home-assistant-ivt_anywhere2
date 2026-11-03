from __future__ import annotations

import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PointtApi
from .const import CONF_GATEWAY_ID, CONF_REFRESH_TOKEN, DOMAIN
from .coordinator import IVTAnywhereIICoordinator
from .model_config import LOCAL, MODEL, ModelSettings, bosch_credentials

PLATFORMS = ["sensor"]
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Use Home Assistant's managed aiohttp session
    session = async_get_clientsession(hass)

    refresh_token = entry.data[CONF_REFRESH_TOKEN].strip()
    gateway_id = entry.data[CONF_GATEWAY_ID]

    async def _store_refresh_token(new_refresh: str) -> None:
        """Persist rotated refresh token back to the config entry."""
        new_refresh = new_refresh.strip()

        if not new_refresh:
            return

        # Avoid unnecessary writes
        if new_refresh == entry.data.get(CONF_REFRESH_TOKEN):
            return

        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_REFRESH_TOKEN: new_refresh},
        )

    api = PointtApi(
        session=session,
        refresh_token=refresh_token,
        on_refresh_token=_store_refresh_token,
    )

    coordinator = IVTAnywhereIICoordinator(hass, api, gateway_id)
    local_options = entry.options.get(LOCAL, {})
    if local_options.get("enabled"):
        # Local reads and the model must start even when the cloud is offline.
        await coordinator.async_refresh()
    else:
        await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }

    try:
        if local_options.get("enabled"):
            credentials = bosch_credentials(hass, gateway_id)
            if credentials is None:
                _LOGGER.warning(
                    "Local reads require a matching Bosch integration entry; reconfigure IVT options"
                )
            else:
                from .local import LocalEnergyCoordinator

                local_session = _create_local_session()
                entry.async_on_unload(local_session.close)

                async def close_at_stop(event):
                    await local_session.close()

                entry.async_on_unload(
                    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, close_at_stop)
                )
                hass.data[DOMAIN][entry.entry_id]["local_session"] = local_session
                local = LocalEnergyCoordinator(
                    hass,
                    local_session,
                    coordinator,
                    credentials,
                    local_options["host"],
                    local_options.get("poll_seconds", 300),
                )
                hass.data[DOMAIN][entry.entry_id]["local_coordinator"] = local
                await local.async_refresh()
                if entry.options.get(MODEL, {}).get("enabled"):
                    from .estimate import HeatEstimateCoordinator

                    settings = ModelSettings.from_options(entry.options[MODEL])
                    estimate = HeatEstimateCoordinator(hass, entry, local, settings)
                    hass.data[DOMAIN][entry.entry_id]["estimate_coordinator"] = estimate
                    await estimate.async_refresh()

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        session = hass.data[DOMAIN].pop(entry.entry_id, {}).get("local_session")
        if session is not None:
            await session.close()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime = hass.data[DOMAIN].pop(entry.entry_id, {})
        if runtime.get("local_session") is not None:
            await runtime["local_session"].close()
    # Do NOT close the aiohttp session (managed by Home Assistant)
    return unload_ok


async def _async_options_updated(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


def _create_local_session():
    """Old iCom gateways require serial requests on fresh HTTP connections.

    HA's session factory owns a shared connector and cannot accept a custom
    connector. This dedicated session is closed on unload, setup failure and stop.
    """
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(force_close=True, limit=1)
    )
