from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PointtApi
from .const import DOMAIN, CONF_REFRESH_TOKEN, CONF_GATEWAY_ID

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REFRESH_TOKEN): str,
    }
)


class IVTAnywhereIIConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_SCHEMA,
                errors=errors,
            )

        refresh_token_in = user_input[CONF_REFRESH_TOKEN].strip()

        try:
            session = async_get_clientsession(self.hass)
            api = PointtApi(session, refresh_token_in)

            # This call may trigger token refresh + rotation
            gateways = await api.get_gateways()

            # IMPORTANT: store the (possibly rotated) token, not the input token
            refresh_token_out = api.current_refresh_token().strip()
            self._refresh_token = refresh_token_out

        except Exception:
            errors["base"] = "auth_failed"
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_SCHEMA,
                errors=errors,
            )

        if not gateways:
            errors["base"] = "no_gateways"
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_SCHEMA,
                errors=errors,
            )

        # Store for next step
        self._refresh_token = refresh_token_out
        self._gateways = gateways

        return await self.async_step_gateway()

    async def async_step_gateway(self, user_input=None):
        errors = {}

        gw_map = {g["deviceId"]: f"{g['deviceId']} ({g.get('deviceType')})" for g in self._gateways}
        schema = vol.Schema({vol.Required(CONF_GATEWAY_ID): vol.In(gw_map)})

        if user_input is None:
            return self.async_show_form(
                step_id="gateway",
                data_schema=schema,
                errors=errors,
            )

        gateway_id = user_input[CONF_GATEWAY_ID]
        title = f"IVT Anywhere II {gateway_id}"

        return self.async_create_entry(
            title=title,
            data={
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_GATEWAY_ID: gateway_id,
            },
        )
