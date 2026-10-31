"""Optional local and estimated sensors; reported energy identities stay unchanged."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

MODEL_SENSORS = (
    ("heat_kwh", "IVT estimated heat last 24 hours", "kWh"),
    ("electricity_kwh", "IVT model electricity last 24 hours", "kWh"),
    ("cop", "IVT estimated COP last 24 hours", None),
    ("coverage_percent", "IVT heat estimate input coverage", "%"),
    ("model_status", "IVT heat model status", None),
    ("gap_filled_hours", "IVT gap filled model hours", "h"),
    ("zero_energy_active_hours", "IVT zero input hours with activity", "h"),
    ("thermal_activity", "IVT thermal activity", None),
    ("supply_return_delta_c", "IVT supply return temperature difference", "°C"),
    ("probable_dhw_heating_hours", "IVT detected tank warming last 24 hours", "h"),
    ("month_modelled_heat_kwh", "IVT modelled heat this month", "kWh"),
    ("month_modelled_electricity_kwh", "IVT modelled electricity this month", "kWh"),
    ("month_modelled_cop", "IVT modelled COP this month", None),
    ("projected_heat_month_kwh", "IVT projected heat this month", "kWh"),
    ("projected_cop_month", "IVT projected COP this month", None),
)


class IVTLocalSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, key, name):
        super().__init__(coordinator)
        self.key = key
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.gateway_id}_local_{key}"
        if key != "comparison":
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
        # Comparison values describe independent buckets, not cumulative meters.

    @property
    def available(self):
        return super().available and self.native_value is not None

    @property
    def native_value(self):
        d = self.coordinator.data or {}
        return (
            d.get("status")
            if self.key == "comparison"
            else d.get("values", {}).get(self.key)
        )

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data or {}
        return {
            k: d.get(k)
            for k in (
                "bucket",
                "sampled_at",
                "cloud_fetched_at",
                "host",
                "differences_kwh",
                "poll_interval_seconds",
            )
        }

    @property
    def device_info(self):
        return DeviceInfo(identifiers={(DOMAIN, str(self.coordinator.gateway_id))})


class IVTHeatEstimateSensor(CoordinatorEntity, SensorEntity):
    """Model outputs deliberately kept separate from reported energy sensors."""

    _attr_should_poll = False

    def __init__(self, coordinator, key, name, unit):
        super().__init__(coordinator)
        self.key = key
        self._attr_unique_id = f"{coordinator.gateway_id}_model_estimate_{key}_v1"
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        if key in {
            "heat_kwh",
            "electricity_kwh",
            "month_modelled_heat_kwh",
            "month_modelled_electricity_kwh",
            "projected_heat_month_kwh",
        }:
            self._attr_device_class = SensorDeviceClass.ENERGY
            # Rolling windows are not cumulative Energy-dashboard sources.
        elif key not in {"model_status", "thermal_activity"}:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = (
            "mdi:fire" if key == "heat_kwh" else "mdi:calculator-variant-outline"
        )

    @property
    def available(self):
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.data.get(self.key) is not None
        )

    @property
    def native_value(self):
        value = (self.coordinator.data or {}).get(self.key)
        if self.key in {"model_status", "thermal_activity"}:
            return value
        return round(value, 3) if value is not None else None

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        if self.key in {"gap_filled_hours", "zero_energy_active_hours"}:
            return {
                key: data.get(key)
                for key in (
                    "updated_at",
                    "status",
                    "covered_hours",
                    "usable_hours",
                    "imputation_note",
                    "imputation_details",
                )
            }
        if self.key.startswith("month_modelled_"):
            return {
                **data.get("month", {}),
                "model_status": data.get("model_status"),
                "updated_at": data.get("updated_at"),
                "calibration": data.get("calibration"),
                "calibration_limit": data.get("calibration_limit"),
            }
        if self.key.startswith("projected_"):
            return {
                **data.get("month_projection", {}),
                "updated_at": data.get("updated_at"),
            }
        return {
            key: value
            for key, value in data.items()
            if key
            not in {"heat_kwh", "cop", "hours", "month", "month_projection", "settings"}
        }

    @property
    def device_info(self):
        return DeviceInfo(identifiers={(DOMAIN, str(self.coordinator.gateway_id))})
