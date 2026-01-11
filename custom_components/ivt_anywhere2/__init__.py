from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PointtApi
from .const import DOMAIN, CONF_GATEWAY_ID, CONF_REFRESH_TOKEN
from .coordinator import IVTAnywhereIICoordinator

PLATFORMS = ["sensor"]


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
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.data[DOMAIN].pop(entry.entry_id, None)
    # Do NOT close the aiohttp session (managed by Home Assistant)
    return unload_ok
