"""Is the rig healthy: the board's radio sees and joins the rig's access
point, and the rig's uplink and queue answer from the board.

These are the checks that fail on every board at once when something the
whole farm shares has stopped -- the AP, the broker, the Pi's forwarding --
which is exactly the distinction the report draws.
"""

from __future__ import annotations

import pytest

from conftest import RUN_TAG

pytestmark = [
    pytest.mark.failure_class("infra"),
]


@pytest.mark.hil_only(reason="radio_timing")
@pytest.mark.capability("esp.radio.scan")
def test_the_radio_sees_the_rig_access_point(board, canary, rig_wifi):
    """A scan that finds the rig's AP proves the receiver works and the
    rig is broadcasting. A board that sees nothing at all is an antenna or
    a shield; a rig no board can see is the access point."""
    ssid, _password = rig_wifi
    board.ensure_responsive()
    found = board.wifi_scan(ssid=ssid)
    assert found.get("ok") is True, f"{canary} could not scan: {found}"
    assert found.get("seen") is True, (
        f"{canary} did not see the rig access point {ssid!r} among "
        f"{found.get('count')} network(s)"
    )
    networks = found.get("networks") or []
    assert networks, f"{canary} reported seeing {ssid!r} but listed no network"
    rssi = int(networks[0].get("rssi") or 0)
    # A number at all, and one the radio could have measured. -100 dBm is
    # the floor of anything usable; 0 is a radio that reported nothing.
    assert -100 < rssi < 0, f"{canary} reports an implausible RSSI for {ssid!r}: {rssi}"


@pytest.mark.hil_only(reason="radio_timing")
@pytest.mark.capability("esp.radio.join")
def test_the_radio_joins_the_rig_and_is_given_an_address(board, canary, rig_wifi):
    """Associating and taking a DHCP lease is the whole radio path: the
    transmitter, the AP's authentication, and the Pi's DHCP. Station only,
    never an AP -- the ESP8266 is specified as a leaf and a board that
    brought up an AP would change what every other board can see."""
    ssid, password = rig_wifi
    board.ensure_responsive()
    joined = board.wifi_join(ssid, password)
    try:
        assert joined.get("joined") is True, (
            f"{canary} did not join {ssid!r} within {joined.get('ms')} ms "
            f"(status {joined.get('status')})"
        )
        address = joined.get("ip") or ""
        assert address and not address.startswith("0."), (
            f"{canary} joined {ssid!r} but was given no address: {address!r}"
        )
        assert joined.get("gateway"), f"{canary} was given no gateway on {ssid!r}"
    finally:
        # Leave the radio as it was found: the next check's scan, and the
        # next board's join, should not be racing this one's association.
        board.wifi_leave()


@pytest.mark.hil_only(reason="peripheral")
@pytest.mark.capability("farm.uplink")
def test_the_rig_uplink_answers_from_the_board(board, canary, rig_wifi, rig_uplink):
    """The gateway probe, reached from the board over the rig's AP.

    This is the path a consumer's firmware uses to reach anything off the
    rig, and the one that breaks silently when the Pi's forwarding or NAT
    is not up after a reboot.
    """
    ssid, password = rig_wifi
    board.ensure_responsive()
    joined = board.wifi_join(ssid, password)
    if joined.get("joined") is not True:
        pytest.skip(f"{canary} could not join the rig; the radio check reports that")
    try:
        answered = board.http_get(f"{rig_uplink}/healthz")
        assert answered.get("ok") is True, (
            f"{canary} could not reach the rig uplink at {rig_uplink}: "
            f"{answered.get('error') or answered.get('statusLine')}"
        )
        assert 200 <= int(answered.get("status") or 0) < 400, answered
    finally:
        board.wifi_leave()


@pytest.mark.hil_only(reason="peripheral")
@pytest.mark.capability("farm.queue")
def test_the_rig_broker_takes_a_message_from_the_board(board, canary, rig_wifi, rig_broker):
    """The rig's MQTT broker, published to from the board.

    A run's evidence includes what its boards published, so a broker that
    is not accepting connections costs a consumer its queue evidence
    without failing anything that looks related.
    """
    ssid, password = rig_wifi
    host, port = rig_broker
    board.ensure_responsive()
    joined = board.wifi_join(ssid, password)
    if joined.get("joined") is not True:
        pytest.skip(f"{canary} could not join the rig; the radio check reports that")
    try:
        published = board.mqtt_publish(
            host, port, f"alteriom/canary/{canary}", f"canary {RUN_TAG}"
        )
        assert published.get("ok") is True, (
            f"{canary} could not publish to the rig broker at {host}:{port}: "
            f"{published.get('error')}"
        )
    finally:
        board.wifi_leave()
