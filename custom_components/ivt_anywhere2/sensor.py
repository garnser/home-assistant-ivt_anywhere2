from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnergyData, IVTAnywhereIICoordinator


@dataclass
class IVTSensorDescription(SensorEntityDescription):
    value_fn: Callable[[EnergyData], Optional[float]] = lambda _: None


SENSORS: tuple[IVTSensorDescription, ...] = (
    IVTSensorDescription(
        key="electricity_last_hour",
        name="Electricity last complete hour",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class="energy",
        state_class="measurement",
        value_fn=lambda d: d.electricity_kwh_last_hour,
    ),
    IVTSensorDescription(
        key="compressor_last_hour",
        name="Compressor electricity last complete hour",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class="energy",
        state_class="measurement",
        value_fn=lambda d: d.compressor_kwh_last_hour,
    ),
    IVTSensorDescription(
        key="eheater_last_hour",
        name="E-heater electricity last complete hour",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class="energy",
        state_class="measurement",
        value_fn=lambda d: d.eheater_kwh_last_hour,
    ),
    IVTSensorDescription(
        key="heat_output_last_hour",
        name="Heat output last complete hour",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class="energy",
        state_class="measurement",
        value_fn=lambda d: d.heat_output_kwh_last_hour,
    ),
    IVTSensorDescription(
        key="cop_last_hour",
        name="COP last complete hour",
        native_unit_of_measurement=None,
        device_class=None,
        state_class="measurement",
        value_fn=lambda d: d.cop_last_hour,
    ),
    IVTSensorDescription(
        key="electricity_month",
        name="Electricity month-to-date",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class="energy",
        state_class="measurement",
        value_fn=lambda d: d.electricity_kwh_month,
    ),
    IVTSensorDescription(
        key="heat_output_month",
        name="Heat output month-to-date",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class="energy",
        state_class="measurement",
        value_fn=lambda d: d.heat_output_kwh_month,
    ),
    IVTSensorDescription(
        key="cop_month",
        name="COP month-to-date",
        native_unit_of_measurement=None,
        device_class=None,
        state_class="measurement",
        value_fn=lambda d: d.cop_month,
    ),
)


class IVTEnergySensor(CoordinatorEntity[IVTAnywhereIICoordinator], SensorEntity):
    entity_description: IVTSensorDescription

    def __init__(self, coordinator: IVTAnywhereIICoordinator, description: IVTSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.gateway_id}_{description.key}"

    @property
    def native_value(self):
        data: EnergyData = self.coordinator.data
        return self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self):
        # handy label for the “last complete hour”
        d = self.coordinator.data
        if "last_hour" in self.entity_description.key:
            return {"bucket": d.last_hour_label}
        return None


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator: IVTAnywhereIICoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([IVTEnergySensor(coordinator, d) for d in SENSORS])
