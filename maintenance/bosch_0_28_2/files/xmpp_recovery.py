"""Reconnect compatibility for bosch-thermostat-client 0.28.2."""

from bosch_thermostat_client.connectors.xmpp import XMPPBaseConnector


def configure_xmpp_recovery(gateway) -> None:
    """Clear stale authentication flags so the next request reconnects.

    The client library sets _auth_success on login, but does not reset it when
    the TCP session ends. Its request path then sends into a closed session
    indefinitely. Keep this workaround in /config, not ephemeral site-packages.
    """
    connector = getattr(gateway, "_connector", None)
    if not isinstance(connector, XMPPBaseConnector):
        return

    def disconnected(_event):
        connector._auth_success = False
        connector.connected_event.clear()
        connector.disconnect_event.set()

    def session_started(_event):
        connector.disconnect_event.clear()

    connector.client.add_event_handler("session_end", disconnected)
    connector.client.add_event_handler("disconnected", disconnected)
    connector.client.add_event_handler("session_start", session_started)


async def async_close_gateway(gateway, *, force=False) -> None:
    """Cancel pending connection attempts even if login never completed."""
    connector = getattr(gateway, "_connector", None)
    if isinstance(connector, XMPPBaseConnector):
        connector.client.cancel_connection_attempt()
    await gateway.close(force=force)
