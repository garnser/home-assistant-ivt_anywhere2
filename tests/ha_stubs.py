"""Minimal HA interfaces for offline tests; production modules run unchanged."""

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


class Store:
    def __init__(self, hass, version, key):
        self.hass, self.key = hass, key

    async def async_load(self):
        return copy.deepcopy(self.hass.storage.get(self.key))

    def async_delay_save(self, getter, delay):
        self.hass.storage[self.key] = copy.deepcopy(getter())


class DataUpdateCoordinator:
    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, *, logger, **kwargs):
        self.hass, self.logger = hass, logger
        self.last_update_success = True
        self.data = None


class CoordinatorEntity:
    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator):
        self.coordinator = coordinator

    @property
    def available(self):
        return self.coordinator.last_update_success


class UpdateFailed(Exception):
    pass


def add_external_statistics(hass, metadata, rows):
    if hass.fail_statistics:
        raise RuntimeError("recorder unavailable")
    rows = copy.deepcopy(rows)
    hass.imports.append((copy.deepcopy(metadata), rows))
    series = hass.statistics.setdefault(metadata["statistic_id"], {})
    for row in rows:
        series[row["start"]] = row


@dataclass
class SensorEntityDescription:
    key: str
    name: str = ""
    native_unit_of_measurement: str | None = None
    device_class: str | None = None
    state_class: str | None = None


PACKAGE = "ivt_test_component"
component = Path(__file__).resolve().parents[1] / "custom_components/ivt_anywhere2"
package = module(PACKAGE)
package.__path__ = [str(component)]
clock = SimpleNamespace(now=lambda: datetime(2026, 2, 1, 12, tzinfo=timezone.utc))
modules = {
    PACKAGE: package,
    f"{PACKAGE}.api": module(f"{PACKAGE}.api", PointtApi=object),
    "homeassistant": module("homeassistant"),
    "homeassistant.core": module("homeassistant.core", HomeAssistant=object),
    "homeassistant.helpers": module("homeassistant.helpers"),
    "homeassistant.helpers.storage": module("homeassistant.helpers.storage", Store=Store),
    "homeassistant.helpers.update_coordinator": module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=DataUpdateCoordinator,
        CoordinatorEntity=CoordinatorEntity,
        UpdateFailed=UpdateFailed,
    ),
    "homeassistant.util": module("homeassistant.util", dt=clock),
    "homeassistant.components": module("homeassistant.components"),
    "homeassistant.components.recorder": module("homeassistant.components.recorder"),
    "homeassistant.components.recorder.models": module(
        "homeassistant.components.recorder.models", StatisticMeanType=SimpleNamespace(NONE=0)
    ),
    "homeassistant.components.recorder.statistics": module(
        "homeassistant.components.recorder.statistics", async_add_external_statistics=add_external_statistics
    ),
    "homeassistant.components.sensor": module(
        "homeassistant.components.sensor",
        SensorEntity=type("SensorEntity", (), {}),
        SensorEntityDescription=SensorEntityDescription,
        SensorDeviceClass=SimpleNamespace(ENERGY="energy"),
        SensorStateClass=SimpleNamespace(MEASUREMENT="measurement", TOTAL="total", TOTAL_INCREASING="total_increasing"),
    ),
    "homeassistant.const": module(
        "homeassistant.const", UnitOfEnergy=SimpleNamespace(KILO_WATT_HOUR="kWh")
    ),
    "homeassistant.helpers.entity": module("homeassistant.helpers.entity", DeviceInfo=dict),
}
with patch.dict(sys.modules, modules):
    util = importlib.import_module(f"{PACKAGE}.util")
    coordinator = importlib.import_module(f"{PACKAGE}.coordinator")
    sensor = importlib.import_module(f"{PACKAGE}.sensor")
    recorded_statistics = importlib.import_module(f"{PACKAGE}.statistics")


def hass(storage=None):
    return SimpleNamespace(storage={} if storage is None else storage, statistics={}, imports=[], fail_statistics=False)
