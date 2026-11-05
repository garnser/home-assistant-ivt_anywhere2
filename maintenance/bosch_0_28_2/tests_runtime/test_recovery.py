"""Run with the staged component and installed Home Assistant dependencies."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from bosch_thermostat_client.connectors.xmpp import XMPPBaseConnector
from bosch_thermostat_client.const import GET
from custom_components import bosch
from custom_components.bosch import services
from custom_components.bosch.sensor.bosch import BoschSensor
from custom_components.bosch.xmpp_recovery import async_close_gateway, configure_xmpp_recovery


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        await bosch.async_setup(self.hass, {})

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_actions_exist_before_gateway_setup_and_after_unload(self):
        for name in ['send_custom_get', 'update_thermostat', 'debug_scan', 'send_custom_put_float']:
            self.assertTrue(self.hass.services.has_service('bosch', name))
        services.async_remove_services(self.hass, None)
        self.assertTrue(self.hass.services.has_service('bosch', 'send_custom_get'))
        self.assertTrue(self.hass.services.has_service('bosch', 'update_thermostat'))

    async def test_offline_get_is_recoverable_runtime_error(self):
        entry = SimpleNamespace(domain='bosch', entry_id='test', state=ConfigEntryState.SETUP_RETRY)
        self.hass.config_entries = SimpleNamespace(async_get_entry=lambda key: entry)
        registry = SimpleNamespace(async_get=lambda key: SimpleNamespace(config_entries={'test'}))
        with patch.object(services.dr, 'async_get', return_value=registry):
            with self.assertRaisesRegex(HomeAssistantError, 'not loaded'):
                await self.hass.services.async_call('bosch', 'send_custom_get',
                    {'device_id': ['device'], 'path': '/temperature'}, blocking=True, return_response=True)

    async def test_loaded_get_keeps_response_and_target_routing(self):
        entry = SimpleNamespace(domain='bosch', entry_id='test', state=ConfigEntryState.LOADED)
        self.hass.config_entries = SimpleNamespace(async_get_entry=lambda key: entry)
        registry = SimpleNamespace(async_get=lambda key: SimpleNamespace(config_entries={'test'}))
        gateway = SimpleNamespace(device_id='test', custom_get=AsyncMock(return_value={'value': 42.5}))
        self.hass.data['bosch']['uuid'] = {bosch.BOSCH_GATEWAY_ENTRY: gateway}
        with patch.object(services.dr, 'async_get', return_value=registry):
            result = await self.hass.services.async_call('bosch', 'send_custom_get',
                {'device_id': ['device'], 'path': '/temperature'}, blocking=True, return_response=True)
        self.assertEqual(result, {'data': [{'value': 42.5}]})
        gateway.custom_get.assert_awaited_once_with(path='/temperature')

    async def test_failed_setup_closes_each_client_and_removes_gateway(self):
        entry = SimpleNamespace(data={bosch.UUID: 'uuid', 'address': 'test',
            bosch.CONF_PROTOCOL: 'XMPP', bosch.CONF_DEVICE_TYPE: 'IVT',
            bosch.ACCESS_KEY: 'test', bosch.ACCESS_TOKEN: 'test'},
            async_on_unload=Mock(), add_update_listener=Mock(return_value=lambda: None))
        for _ in range(2):
            client = SimpleNamespace(close=AsyncMock())
            gateway = SimpleNamespace(gateway=client,
                async_init=AsyncMock(side_effect=ConfigEntryNotReady('offline')))
            with patch.object(bosch, 'BoschGatewayEntry', return_value=gateway):
                with self.assertRaises(ConfigEntryNotReady):
                    await bosch.async_setup_entry(self.hass, entry)
            client.close.assert_awaited_once_with(force=True)
            self.assertEqual(self.hass.data['bosch'], {})

    async def test_sensor_without_state_class_handles_missing_data_and_zero(self):
        upstream = SimpleNamespace(device_class=None, state_class=None, entity_category=None,
            parent_id=None, id='temperature', state='unavailable', state_message='no response',
            update_initialized=False, path='/temperature', get_property=Mock(return_value={}))
        sensor = BoschSensor(self.hass, 'uuid', upstream, None, 'Temperature', 'temperature')
        sensor.async_schedule_update_ha_state = Mock()
        await sensor.async_update()
        self.assertIsNone(sensor.native_value)
        self.assertIsNone(sensor._attr_state_class)
        self.assertIsNone(sensor._attr_device_class)
        upstream.get_property.return_value = {'value': 0.0, 'unitOfMeasure': 'C'}
        upstream.state = 0.0
        await sensor.async_update()
        self.assertEqual(sensor.native_value, 0.0)
        self.assertEqual(sensor.native_unit_of_measurement, '°C')

    async def test_disconnect_reconnects_using_real_client_request_path(self):
        connector = XMPPBaseConnector.__new__(XMPPBaseConnector)
        connector._auth_success = True
        connector.connected_event = asyncio.Event()
        connector.connected_event.set()
        connector.disconnect_event = asyncio.Event()
        connector.disconnect_event.set()
        connector._lock = asyncio.Lock()
        connector.listeners = set()
        connector._to = 'test'
        connector._build_message = lambda **kwargs: 'test'
        events = {}
        connector.client = Mock()
        connector.client.add_event_handler.side_effect = lambda name, handler: events.setdefault(name, []).append(handler)

        def connect():
            connector._auth_success = True
            connector.connected_event.set()
            for callback in events['session_start']:
                callback(None)

        def send(**kwargs):
            for listener in connector.listeners:
                listener({'id': '/temperature', 'value': 42.5}, 'HTTP/1.1 200 OK')

        connector.client.connect.side_effect = connect
        connector.client.send_message.side_effect = send
        configure_xmpp_recovery(SimpleNamespace(_connector=connector))
        for callback in events['disconnected']:
            callback(None)
        self.assertFalse(connector._auth_success)
        self.assertFalse(connector.connected_event.is_set())
        self.assertTrue(connector.disconnect_event.is_set())
        result = await connector._request(GET, '/temperature')
        self.assertEqual(result['value'], 42.5)
        connector.client.connect.assert_called_once()
        self.assertFalse(connector.disconnect_event.is_set())

    async def test_failed_login_cleanup_cancels_pending_connection(self):
        connector = XMPPBaseConnector.__new__(XMPPBaseConnector)
        connector.client = Mock()
        gateway = SimpleNamespace(_connector=connector, close=AsyncMock())
        await async_close_gateway(gateway, force=True)
        connector.client.cancel_connection_attempt.assert_called_once()
        gateway.close.assert_awaited_once_with(force=True)

    async def test_failed_unload_retains_gateway_and_update_timers(self):
        entry = SimpleNamespace(data={bosch.UUID: 'uuid'})
        gateway = SimpleNamespace(async_reset=AsyncMock(return_value=False))
        cancel = Mock()
        data = {bosch.BOSCH_GATEWAY_ENTRY: gateway, bosch.INTERVAL: cancel}
        self.hass.data['bosch']['uuid'] = data
        self.assertFalse(await bosch.async_unload_entry(self.hass, entry))
        self.assertIs(self.hass.data['bosch']['uuid'], data)
        cancel.assert_not_called()

    async def test_successful_unload_cleans_timers_and_keeps_actions(self):
        entry = SimpleNamespace(data={bosch.UUID: 'uuid'})
        gateway = SimpleNamespace(async_reset=AsyncMock(return_value=True))
        cancel = Mock()
        self.hass.data['bosch']['uuid'] = {bosch.BOSCH_GATEWAY_ENTRY: gateway, bosch.INTERVAL: cancel}
        self.assertTrue(await bosch.async_unload_entry(self.hass, entry))
        cancel.assert_called_once()
        self.assertNotIn('uuid', self.hass.data['bosch'])
        self.assertTrue(self.hass.services.has_service('bosch', 'send_custom_get'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
