"""Validated per-entry settings for the optional, unmetered heat model."""

from dataclasses import asdict, dataclass, fields
from math import isfinite

LOCAL = "local"
MODEL = "heat_model"


@dataclass(frozen=True)
class ModelSettings:
    """Defaults describe GEO 512C; other appliances need their own rating."""

    supply_entity: str = ""
    return_entity: str = ""
    modulation_entity: str = ""
    tank_entity: str = ""
    calibration_enabled: bool = False
    reported_heat_includes_auxiliary: bool = True
    reference_cop: float = 4.8
    reference_source_c: float = 0.0
    reference_flow_c: float = 35.0
    assumed_source_c: float = 0.0
    tank_volume_litres: float = 180.0
    performance_factor: float = 1.0
    condensing_offset_k: float = 2.0
    evaporating_offset_k: float = 3.0

    @classmethod
    def from_options(cls, options):
        settings = cls(
            **{f.name: options[f.name] for f in fields(cls) if f.name in options}
        )
        for name in ("supply_entity", "return_entity", "modulation_entity"):
            if not isinstance(getattr(settings, name), str) or not getattr(
                settings, name
            ).startswith("sensor."):
                raise ValueError(
                    "Select supply, return and compressor modulation sensors"
                )
        if not isinstance(settings.tank_entity, str) or (
            settings.tank_entity and not settings.tank_entity.startswith("sensor.")
        ):
            raise ValueError("The tank temperature must be a sensor")
        for name in ("calibration_enabled", "reported_heat_includes_auxiliary"):
            if not isinstance(getattr(settings, name), bool):
                raise ValueError(f"Invalid model setting: {name}")
        bounds = {
            "reference_cop": (1.0, 8.0),
            "reference_source_c": (-10, 25),
            "reference_flow_c": (20, 70),
            "assumed_source_c": (-10, 25),
            "tank_volume_litres": (1, 2000),
            "performance_factor": (0.5, 1.2),
            "condensing_offset_k": (0, 10),
            "evaporating_offset_k": (0, 10),
        }
        for name, (low, high) in bounds.items():
            value = getattr(settings, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or not low <= value <= high
            ):
                raise ValueError(f"Invalid model setting: {name}")
        if (
            settings.reference_flow_c
            + settings.condensing_offset_k
            - settings.reference_source_c
            + settings.evaporating_offset_k
            <= 10
        ):
            raise ValueError("The reference temperature lift must exceed 10 K")
        return settings

    def signature(self):
        values = asdict(self)
        # These controls do not change the raw temperature-model observations.
        # Enabling calibration must retain the days collected while disabled.
        values.pop("calibration_enabled")
        values.pop("performance_factor")
        return {"model_version": 3, **values}


def bosch_credentials(hass, gateway_id):
    """Reuse a matching Bosch entry; never expose or copy credentials to options."""
    for entry in hass.config_entries.async_entries("bosch"):
        if str(entry.data.get("address")) == str(gateway_id):
            credentials = entry.data
            if credentials.get("access_token"):
                return credentials
    return None
