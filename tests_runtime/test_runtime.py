"""Native HA API tests, synthetic inputs only; no gateway, cloud or recorder DB."""

import sys
import tempfile
import unittest
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from custom_components.ivt_anywhere2 import (
    async_setup,
    async_setup_entry,
    async_unload_entry,
    config_flow,
    diagnostics,
    estimate,
    local,
    model_sensor,
)
from custom_components.ivt_anywhere2.model_config import ModelSettings


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        dt_util.set_default_time_zone(ZoneInfo("Europe/Stockholm"))
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        self.hass.config_entries = SimpleNamespace(
            async_entries=lambda domain: [],
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
            async_reload=AsyncMock(),
        )
        self.entry = SimpleNamespace(
            data={"gateway_id": "test-gateway", "refresh_token": "test-refresh"},
            options={},
            entry_id="test-entry",
            async_on_unload=MagicMock(),
            add_update_listener=MagicMock(return_value=lambda: None),
        )
        session_patch = patch(
            "custom_components.ivt_anywhere2.async_get_clientsession",
            return_value=MagicMock(),
        )
        session_patch.start()
        self.addCleanup(session_patch.stop)
        await async_setup(self.hass, {})

    async def asyncTearDown(self):
        await self.hass.async_block_till_done()
        self.tmp.cleanup()

    def settings(self, **kw):
        return ModelSettings.from_options(
            {
                "supply_entity": "sensor.test_supply",
                "return_entity": "sensor.test_return",
                "modulation_entity": "sensor.test_modulation",
                **kw,
            }
        )

    async def test_options_defaults_keep_existing_cloud_only_behavior(self):
        flow = config_flow.IVTOptionsFlow(self.entry)
        flow.hass = self.hass
        form = await flow.async_step_init()
        self.assertEqual(
            form["data_schema"]({}), {"local_enabled": False, "model_enabled": False}
        )
        r = await flow.async_step_init({"local_enabled": False, "model_enabled": False})
        self.assertFalse(r["data"]["local"]["enabled"])
        self.assertFalse(r["data"]["heat_model"]["enabled"])

    async def test_model_requires_local_and_matching_bosch_entry(self):
        flow = config_flow.IVTOptionsFlow(self.entry)
        flow.hass = self.hass
        r = await flow.async_step_init({"local_enabled": False, "model_enabled": True})
        self.assertEqual(r["errors"]["base"], "model_requires_local")
        await flow.async_step_init({"local_enabled": True, "model_enabled": True})
        r = await flow.async_step_local({"host": "gateway.test", "poll_seconds": 300})
        self.assertEqual(r["errors"]["base"], "bosch_credentials_required")
        r = await flow.async_step_local(
            {"host": "http://gateway.test/path", "poll_seconds": 300}
        )
        self.assertEqual(r["errors"]["base"], "invalid_host")

    async def test_options_map_entities_validate_units_and_preserve_other_options(self):
        self.entry.options = {"unrelated": "keep"}
        flow = config_flow.IVTOptionsFlow(self.entry)
        flow.hass = self.hass
        await flow.async_step_init({"local_enabled": True, "model_enabled": True})
        with patch.object(
            config_flow, "bosch_credentials", return_value={"access_token": "secret"}
        ):
            r = await flow.async_step_local(
                {"host": "gateway.test", "poll_seconds": 600}
            )
        self.assertEqual(r["step_id"], "model")
        self.hass.states.async_set(
            "sensor.test_supply", "95", {"unit_of_measurement": "°F"}
        )
        self.hass.states.async_set(
            "sensor.test_return", "30", {"unit_of_measurement": "°C"}
        )
        self.hass.states.async_set(
            "sensor.test_modulation", "50", {"unit_of_measurement": "%"}
        )
        r = await flow.async_step_model(asdict(self.settings()))
        self.assertEqual(r["type"], "create_entry")
        self.assertEqual(r["data"]["unrelated"], "keep")
        self.assertEqual(r["data"]["local"]["host"], "gateway.test")
        self.assertNotIn("secret", str(r))
        settings = asdict(self.settings())
        settings["reference_cop"] = float("nan")
        r = await flow.async_step_model(settings)
        self.assertEqual(r["errors"]["base"], "invalid_model_settings")
        settings = asdict(self.settings())
        settings["return_entity"] = settings["supply_entity"]
        r = await flow.async_step_model(settings)
        self.assertEqual(r["errors"]["base"], "invalid_model_settings")

    async def test_legacy_cloud_setup_does_not_create_local_client(self):
        cloud = SimpleNamespace(
            async_config_entry_first_refresh=AsyncMock(), async_refresh=AsyncMock()
        )
        with (
            patch("custom_components.ivt_anywhere2.PointtApi"),
            patch(
                "custom_components.ivt_anywhere2.IVTAnywhereIICoordinator",
                return_value=cloud,
            ),
            patch.object(
                self.hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
            ),
            patch("custom_components.ivt_anywhere2._create_local_session") as session,
        ):
            await async_setup_entry(self.hass, self.entry)
        cloud.async_config_entry_first_refresh.assert_awaited_once()
        session.assert_not_called()

    async def test_local_and_model_load_even_when_cloud_refresh_fails(self):
        self.entry.options = {
            "local": {"enabled": True, "host": "gateway.test", "poll_seconds": 600},
            "heat_model": {**asdict(self.settings()), "enabled": True},
        }
        cloud = SimpleNamespace(
            async_config_entry_first_refresh=AsyncMock(),
            async_refresh=AsyncMock(),
            last_update_success=False,
        )
        lc = SimpleNamespace(async_refresh=AsyncMock())
        mc = SimpleNamespace(async_refresh=AsyncMock())
        session = SimpleNamespace(close=AsyncMock())
        with (
            patch("custom_components.ivt_anywhere2.PointtApi"),
            patch(
                "custom_components.ivt_anywhere2.IVTAnywhereIICoordinator",
                return_value=cloud,
            ),
            patch(
                "custom_components.ivt_anywhere2.bosch_credentials",
                return_value={"access_token": "secret"},
            ),
            patch(
                "custom_components.ivt_anywhere2._create_local_session",
                return_value=session,
            ),
            patch.object(
                local, "LocalEnergyCoordinator", return_value=lc
            ) as make_local,
            patch.object(estimate, "HeatEstimateCoordinator", return_value=mc),
            patch.object(
                self.hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
            ),
        ):
            await async_setup_entry(self.hass, self.entry)
        cloud.async_refresh.assert_awaited_once()
        cloud.async_config_entry_first_refresh.assert_not_called()
        lc.async_refresh.assert_awaited_once()
        mc.async_refresh.assert_awaited_once()
        self.assertEqual(make_local.call_args.args[-2:], ("gateway.test", 600))
        with patch.object(
            self.hass.config_entries,
            "async_unload_platforms",
            new=AsyncMock(return_value=False),
        ):
            self.assertFalse(await async_unload_entry(self.hass, self.entry))
        session.close.assert_not_called()
        with patch.object(
            self.hass.config_entries,
            "async_unload_platforms",
            new=AsyncMock(return_value=True),
        ):
            self.assertTrue(await async_unload_entry(self.hass, self.entry))
        session.close.assert_awaited_once()

    async def test_model_sensor_energy_has_no_cumulative_state_class(self):
        coordinator = SimpleNamespace(
            data={
                "heat_kwh": 10,
                "projected_heat_month_kwh": 100,
                "month_projection": {"status": "rough_projection"},
                "hours": [{"private": "large"}],
            },
            last_update_success=True,
            gateway_id="test",
        )
        for key in [
            "heat_kwh",
            "electricity_kwh",
            "month_modelled_heat_kwh",
            "projected_heat_month_kwh",
        ]:
            entity = model_sensor.IVTHeatEstimateSensor(coordinator, key, "Test", "kWh")
            self.assertIsNone(entity.state_class)
            self.assertNotIn("hours", entity.extra_state_attributes)
        entity = model_sensor.IVTHeatEstimateSensor(
            coordinator, "projected_heat_month_kwh", "Test", "kWh"
        )
        self.assertEqual(entity.extra_state_attributes["status"], "rough_projection")

    async def test_partial_local_reads_keep_healthy_electricity(self):
        cloud = SimpleNamespace(
            gateway_id="test", recording_diagnostics=None, last_update_success=False
        )
        gateway = MagicMock()
        with patch.object(local, "IVTGateway", return_value=gateway):
            coord = local.LocalEnergyCoordinator(
                self.hass, MagicMock(), cloud, {"access_token": "test"}, "gateway.test"
            )
        coord.store = SimpleNamespace(
            async_load=AsyncMock(return_value=None), async_save=AsyncMock()
        )
        now = dt_util.now()
        day, _, _ = local.last_complete_hour_target(now)

        async def read(path):
            if "outputProduced" in path:
                raise RuntimeError("unavailable output")
            interval = path.split("interval=")[1]
            hourly = len(interval) == 10
            return {
                "interval": interval,
                "sampleRate": "P1H" if hourly else "P1D",
                "recording": [
                    {"y": 0 if "eheater" in path else 60, "c": 60 if hourly else 1440}
                    for _ in range(24 if hourly else 31)
                ],
            }

        with (
            patch.object(coord, "_async_read_recording", side_effect=read),
            patch.object(local.asyncio, "sleep", new=AsyncMock()),
        ):
            result = await coord._async_update_data()
        self.assertEqual(result["values"]["compressor_hour"], 1)
        self.assertIsNone(result["values"]["output_hour"])
        self.assertEqual(result["status"], "cloud_unavailable")
        coord.store.async_save.assert_awaited_once()

    async def test_model_maps_fahrenheit_and_preserves_raw_zero_without_training(self):
        settings = self.settings()
        now = dt_util.utcnow()
        end = now.replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=24)
        for entity, value, unit in [
            ("sensor.test_supply", "95", "°F"),
            ("sensor.test_return", "30", "°C"),
            ("sensor.test_modulation", "60", "%"),
        ]:
            self.hass.states.async_set(entity, value, {"unit_of_measurement": unit})
        states = {
            entity: [
                State(
                    entity,
                    value,
                    attributes={"unit_of_measurement": unit},
                    last_updated=start,
                )
            ]
            for entity, value, unit in [
                ("sensor.test_supply", "95", "°F"),
                ("sensor.test_return", "30", "°C"),
                ("sensor.test_modulation", "60", "%"),
            ]
        }
        records = {}
        for i in range(24):
            t = (start + timedelta(hours=i)).astimezone(dt_util.DEFAULT_TIME_ZONE)
            day = t.date().isoformat()
            for kind in ["compressor", "eheater"]:
                records.setdefault(
                    (day, kind),
                    {
                        "interval": day,
                        "sampleRate": "P1H",
                        "recording": [{"y": 0, "c": 60} for _ in range(24)],
                    },
                )
        lc = SimpleNamespace(gateway_id="test", history=[], data=None)
        coord = estimate.HeatEstimateCoordinator(self.hass, self.entry, lc, settings)
        coord.hass = self.hass
        coord.settings = settings
        coord.local = lc
        coord.gateway_id = "test"
        coord._saved = None
        coord.entity_map = dict(
            zip(
                estimate.ENTITIES,
                ("sensor.test_supply", "sensor.test_return", "sensor.test_modulation"),
            )
        )
        coord.store = SimpleNamespace(
            async_load=AsyncMock(
                return_value={
                    "signature": settings.signature(),
                    "hours": {},
                    "days": {},
                }
            ),
            async_save=AsyncMock(),
        )
        coord._recordings = AsyncMock(return_value=records)

        async def executor(func, *args):
            return func(*args)

        recorder = SimpleNamespace(async_add_executor_job=executor)
        with (
            patch.object(estimate, "get_instance", return_value=recorder),
            patch.object(
                estimate.history, "get_significant_states", return_value=states
            ),
        ):
            result = await coord._async_update_data()
        self.assertEqual(result["electricity_kwh"], 0)
        self.assertEqual(result["zero_energy_active_hours"], 24)
        self.assertEqual(result["inferred_electricity_kwh"], 0)
        self.assertEqual(result["model_status"], "calibration_disabled")
        saved = coord.store.async_save.call_args.args[0]
        self.assertFalse(any(r.get("imputed") for r in saved["hours"].values()))
        self.assertAlmostEqual(
            next(
                r for r in saved["hours"].values() if r.get("active_flow_c") is not None
            )["active_flow_c"],
            35,
        )

    async def test_dedicated_session_uses_a_fresh_serial_connector(self):
        from custom_components.ivt_anywhere2 import _create_local_session

        session = _create_local_session()
        try:
            self.assertTrue(session.connector.force_close)
            self.assertEqual(session.connector.limit, 1)
        finally:
            await session.close()

    async def test_cloud_sensor_attributes_are_safe_before_first_cloud_success(self):
        from custom_components.ivt_anywhere2.sensor import SENSORS, IVTEnergySensor

        coordinator = SimpleNamespace(
            data=None, last_update_success=False, gateway_id="test"
        )
        for description in SENSORS:
            entity = IVTEnergySensor(coordinator, description)
            self.assertFalse(entity.available)
            self.assertIsNone(entity.native_value)
            self.assertIsNone(entity.extra_state_attributes)

    async def test_options_update_reloads_only_this_entry(self):
        from custom_components.ivt_anywhere2 import _async_options_updated

        await _async_options_updated(self.hass, self.entry)
        self.hass.config_entries.async_reload.assert_awaited_once_with("test-entry")

    async def test_diagnostics_redact_credentials_and_sensor_mappings(self):
        self.entry.data.update(access_key="secret-key", password="secret-password")
        self.entry.options = {
            "local": {"host": "private-host"},
            "heat_model": {"supply_entity": "sensor.private"},
        }
        result = await diagnostics.async_get_config_entry_diagnostics(
            self.hass, self.entry
        )
        for secret in [
            "test-refresh",
            "secret-key",
            "secret-password",
            "test-gateway",
            "private-host",
            "sensor.private",
        ]:
            self.assertNotIn(secret, str(result))


if __name__ == "__main__":
    unittest.main()
