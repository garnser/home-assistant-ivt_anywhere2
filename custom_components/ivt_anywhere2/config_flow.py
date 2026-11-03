from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PointtApi
from .const import CONF_GATEWAY_ID, CONF_REFRESH_TOKEN, DOMAIN
from .model_config import LOCAL, MODEL, ModelSettings, bosch_credentials

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

        gw_map = {
            g["deviceId"]: f"{g['deviceId']} ({g.get('deviceType')})"
            for g in self._gateways
        }
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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return IVTOptionsFlow(config_entry)


class IVTOptionsFlow(config_entries.OptionsFlow):
    """Optional local recording reads and explicit model assumptions."""

    def __init__(self, entry):
        self._entry = entry
        self._options = deepcopy(dict(entry.options))

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input["model_enabled"] and not user_input["local_enabled"]:
                errors["base"] = "model_requires_local"
            else:
                self._options.setdefault(LOCAL, {})["enabled"] = user_input[
                    "local_enabled"
                ]
                self._options.setdefault(MODEL, {})["enabled"] = user_input[
                    "model_enabled"
                ]
                return (
                    await self.async_step_local()
                    if user_input["local_enabled"]
                    else self._finish()
                )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "local_enabled",
                        default=self._options.get(LOCAL, {}).get("enabled", False),
                    ): bool,
                    vol.Required(
                        "model_enabled",
                        default=self._options.get(MODEL, {}).get("enabled", False),
                    ): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_local(self, user_input=None):
        errors = {}
        old = self._options.get(LOCAL, {})
        if user_input is not None:
            host = user_input["host"].strip()
            if not host or any(c in host for c in "/?#@ ") or "://" in host:
                errors["base"] = "invalid_host"
            elif (
                bosch_credentials(self.hass, self._entry.data[CONF_GATEWAY_ID]) is None
            ):
                errors["base"] = "bosch_credentials_required"
            else:
                self._options[LOCAL] = {
                    **old,
                    **user_input,
                    "host": host,
                    "enabled": True,
                }
                return (
                    await self.async_step_model()
                    if self._options.get(MODEL, {}).get("enabled")
                    else self._finish()
                )
        return self.async_show_form(
            step_id="local",
            data_schema=vol.Schema(
                {
                    vol.Required("host", default=old.get("host", "")): str,
                    vol.Required(
                        "poll_seconds", default=old.get("poll_seconds", 300)
                    ): vol.All(vol.Coerce(int), vol.Range(min=300, max=3600)),
                }
            ),
            errors=errors,
        )

    async def async_step_model(self, user_input=None):
        errors = {}
        old = self._options.get(MODEL, {})
        if user_input is not None:
            try:
                settings = ModelSettings.from_options(
                    {**user_input, "tank_entity": user_input.get("tank_entity", "")}
                )
                entities = (
                    settings.supply_entity,
                    settings.return_entity,
                    settings.modulation_entity,
                )
                if len(set(entities)) != 3:
                    raise ValueError("Distinct sensors are required")
                for entity in (
                    *entities,
                    *((settings.tank_entity,) if settings.tank_entity else ()),
                ):
                    state = self.hass.states.get(entity)
                    units = (
                        {"%"} if entity == settings.modulation_entity else {"°C", "°F"}
                    )
                    if (
                        state is None
                        or state.attributes.get("unit_of_measurement") not in units
                    ):
                        raise ValueError(
                            "Temperature or modulation sensor has unsupported units"
                        )
                self._options[MODEL] = {**asdict(settings), "enabled": True}
                return self._finish()
            except (ValueError, TypeError):
                errors["base"] = "invalid_model_settings"
            old = user_input
        defaults = asdict(ModelSettings())
        schema = {}
        for key in (
            "supply_entity",
            "return_entity",
            "modulation_entity",
            "tank_entity",
        ):
            marker = (
                vol.Optional(key, description={"suggested_value": old.get(key)})
                if key == "tank_entity"
                else vol.Required(key, description={"suggested_value": old.get(key)})
            )
            schema[marker] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            )
        for key in (
            "reference_cop",
            "reference_source_c",
            "reference_flow_c",
            "assumed_source_c",
            "tank_volume_litres",
            "performance_factor",
            "condensing_offset_k",
            "evaporating_offset_k",
        ):
            schema[vol.Required(key, default=old.get(key, defaults[key]))] = vol.Coerce(
                float
            )
        for key in ("calibration_enabled", "reported_heat_includes_auxiliary"):
            schema[vol.Required(key, default=old.get(key, defaults[key]))] = bool
        return self.async_show_form(
            step_id="model", data_schema=vol.Schema(schema), errors=errors
        )

    def _finish(self):
        return self.async_create_entry(title="", data=self._options)
