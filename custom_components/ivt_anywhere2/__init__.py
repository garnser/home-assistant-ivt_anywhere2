from __future__ import annotations

from aiohttp import ClientSession

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import PointtApi
from .const import DOMAIN, CONF_GATEWAY_ID, CONF_REFRESH_TOKEN
from .coordinator import IVTAnywhereIICoordinator

PLATFORMS = ["sensor"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = ClientSession()

    refresh_token = entry.data[CONF_REFRESH_TOKEN]
    gateway_id = entry.data[CONF_GATEWAY_ID]

    async def _store_refresh_token(new_refresh: str) -> None:
        # Persist new refresh token so restarts don’t break
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_REFRESH_TOKEN: new_refresh},
        )

    api = PointtApi(session, refresh_token, on_refresh_token=_store_refresh_token)
    coordinator = IVTAnywhereIICoordinator(hass, api, gateway_id)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "session": session,
        "api": api,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    data = hass.data[DOMAIN].pop(entry.entry_id, None)

    if data:
        session: ClientSession = data["session"]
        await session.close()

    return unload_ok
