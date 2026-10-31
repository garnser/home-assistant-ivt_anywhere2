"""Diagnostic summaries without refresh tokens, gateway addresses or sensor IDs."""

from homeassistant.components.diagnostics import async_redact_data

from .const import DOMAIN

REDACT = {
    "refresh_token",
    "access_token",
    "access_key",
    "password",
    "gateway_id",
    "host",
    "supply_entity",
    "return_entity",
    "modulation_entity",
    "tank_entity",
}


async def async_get_config_entry_diagnostics(hass, entry):
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    local = runtime.get("local_coordinator")
    estimate = runtime.get("estimate_coordinator")
    model = estimate.data or {} if estimate else {}
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "local": {
                key: value
                for key, value in (local.data or {}).items()
                if key not in {"local_recordings", "cloud_recordings"}
            }
            if local
            else None,
            "model": {
                key: model.get(key)
                for key in (
                    "updated_at",
                    "model_status",
                    "coverage_percent",
                    "covered_hours",
                    "usable_hours",
                    "zero_energy_active_hours",
                    "gap_filled_hours",
                    "calibration",
                    "rejected_training_days",
                    "retained_hourly_observations",
                )
            },
        },
        REDACT,
    )
