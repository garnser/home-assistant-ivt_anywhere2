from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnergyData, IVTAnywhereIICoordinator


@dataclass
class IVTSensorDescription(SensorEntityDescription):
    value_fn: Callable[[EnergyData], Optional[float]] = lambda _: None


SENSORS: tuple[IVTSensorDescription, ...] = (
    # ----------------------------
    # Each completed hour is a separate energy cycle with an explicit reset.
    # ----------------------------
    IVTSensorDescription(
        key="electricity_last_hour",
        name="Electricity last complete hour",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.electricity_kwh_last_hour,
    ),
    IVTSensorDescription(
        key="compressor_last_hour",
        name="Compressor electricity last complete hour",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.compressor_kwh_last_hour,
    ),
    IVTSensorDescription(
        key="eheater_last_hour",
        name="E-heater electricity last complete hour",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.eheater_kwh_last_hour,
    ),
    IVTSensorDescription(
        key="heat_output_last_hour",
        name="Heat output last complete hour",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.heat_output_kwh_last_hour,
    ),
    IVTSensorDescription(
        key="cop_last_hour",
        name="COP last complete hour",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.cop_last_hour,
    ),
    # ----------------------------
    # Recorded monthly totals can be revised; explicit calendar resets keep
    # corrections from being misidentified as a whole new energy cycle.
    # ----------------------------
    IVTSensorDescription(
        key="compressor_month",
        name="Compressor electricity month-to-date",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.compressor_kwh_month,
    ),
    IVTSensorDescription(
        key="eheater_month",
        name="E-heater electricity month-to-date",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.eheater_kwh_month,
    ),
    IVTSensorDescription(
        key="electricity_month",
        name="Electricity month-to-date",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.electricity_kwh_month,
    ),
    IVTSensorDescription(
        key="heat_output_month",
        name="Heat output month-to-date",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.heat_output_kwh_month,
    ),
    IVTSensorDescription(
        key="cop_month",
        name="COP month-to-date",
        native_unit_of_measurement=None,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.cop_month,
    ),
)


class IVTEnergySensor(CoordinatorEntity[IVTAnywhereIICoordinator], SensorEntity):
    entity_description: IVTSensorDescription

    def __init__(
        self, coordinator: IVTAnywhereIICoordinator, description: IVTSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.gateway_id}_{description.key}"

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.entity_description.value_fn(self.coordinator.data) is not None
        )

    @property
    def last_reset(self) -> Optional[datetime]:
        """Use a stable reset for each recorded hour or calendar month."""
        if self.entity_description.state_class != SensorStateClass.TOTAL:
            return None
        data = self.coordinator.data
        if data is None:
            return None
        if self.entity_description.key.endswith("_month"):
            return data.month_reset
        return data.last_hour_reset

    @property
    def native_value(self) -> Optional[float]:
        data: EnergyData = self.coordinator.data
        return self.entity_description.value_fn(data) if data is not None else None

    @property
    def extra_state_attributes(self) -> Optional[dict[str, Any]]:
        d = self.coordinator.data
        if d is None:
            return None
        if "last_hour" in self.entity_description.key:
            return {"bucket": d.last_hour_label}
        return None

    @property
    def device_info(self) -> DeviceInfo:
        gw_id = str(self.coordinator.gateway_id)
        return DeviceInfo(
            identifiers={(DOMAIN, gw_id)},
            name=f"IVT Anywhere II {gw_id}",
            manufacturer="Bosch / IVT",
            model="IVT Anywhere II (iCom)",
        )


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator: IVTAnywhereIICoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    async_add_entities([IVTEnergySensor(coordinator, d) for d in SENSORS])
    runtime = hass.data[DOMAIN][entry.entry_id]
    if local := runtime.get("local_coordinator"):
        from .model_sensor import IVTLocalSensor

        entities = [IVTLocalSensor(local, "comparison", "IVT local cloud comparison")]
        for kind, label in (
            ("compressor", "compressor electricity"),
            ("eheater", "auxiliary electricity"),
            ("output", "heat output"),
        ):
            for period, suffix in (
                ("hour", "last complete hour"),
                ("month", "month to date"),
            ):
                entities.append(
                    IVTLocalSensor(
                        local, f"{kind}_{period}", f"IVT local {label} {suffix}"
                    )
                )
        async_add_entities(entities)
    if estimate := runtime.get("estimate_coordinator"):
        from .model_sensor import MODEL_SENSORS, IVTHeatEstimateSensor

        async_add_entities(
            [
                IVTHeatEstimateSensor(estimate, *description)
                for description in MODEL_SENSORS
            ]
        )
