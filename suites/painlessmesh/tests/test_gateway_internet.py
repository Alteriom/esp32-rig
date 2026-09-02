"""Dedicated-bridge and sendToInternet validation on the physical farm."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.request import urlopen

import pytest

from alteriom_hil.protocol import TimeoutWaitingFor

pytestmark = [
    pytest.mark.hil_only(reason="peripheral"),
    # A fixture failure here is a failed physical library capability, not a
    # runner outage: the fixture has already established serial and mesh I/O.
    pytest.mark.failure_class("real_bug"),
]


def _gateway_settings():
    if os.environ.get("ALTERIOM_HIL_MODE") != "hardware":
        pytest.skip("gateway validation requires physical Wi-Fi radios")
    ssid = os.environ.get("ALTERIOM_HIL_WIFI_SSID")
    password_file = os.environ.get("ALTERIOM_HIL_WIFI_PASSWORD_FILE")
    endpoint = os.environ.get("ALTERIOM_HIL_GATEWAY_ENDPOINT")
    if not ssid or not password_file or not endpoint:
        pytest.skip("gateway test network is not configured")
    try:
        password = Path(password_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        pytest.skip(f"cannot read gateway Wi-Fi password: {exc}")
    return ssid, password, endpoint.rstrip("/")


def _wait_for_peer(client, peer_node_id: int, timeout: float) -> set[int]:
    deadline = time.monotonic() + timeout
    peers: set[int] = set()
    while time.monotonic() < deadline:
        peers = set(client.node_list(timeout=10))
        if peer_node_id in peers:
            return peers
        time.sleep(2)
    return peers


@pytest.fixture(scope="module")
def gateway_mesh(mesh):
    clients, node_ids = mesh
    if len(clients) < 2:
        pytest.skip("gateway validation needs a bridge and a regular node")
    ssid, password, endpoint = _gateway_settings()
    requested = os.environ.get("ALTERIOM_HIL_GATEWAY_BOARD")
    gateway_id = requested or next(iter(clients))
    if gateway_id not in clients:
        pytest.fail(f"configured gateway board is absent: {gateway_id}")
    sender_id = next(board_id for board_id in clients if board_id != gateway_id)
    gateway = clients[gateway_id]
    sender = clients[sender_id]

    gateway_started = False
    try:
        state = gateway.start_gateway(ssid, password)
        gateway_started = True
        # initAsBridge returns after the station starts connecting.  Do not
        # mistake that asynchronous initial state for an association failure.
        association_deadline = time.monotonic() + 45
        while int(state["wifiStatus"]) != 3 and time.monotonic() < association_deadline:
            time.sleep(1)
            state = gateway.gateway_status(timeout=10)

        sender_state = None
        upstream_ready = (
            state["initialized"] is True
            and state["isBridge"] is True
            and int(state["wifiStatus"]) == 3
            and state["localIP"] != "0.0.0.0"
        )
        gateway_peers: set[int] = set()
        sender_peers: set[int] = set()
        if upstream_ready:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    gateway_peers = _wait_for_peer(
                        gateway, node_ids[sender_id], timeout=10
                    )
                    sender_peers = _wait_for_peer(
                        sender, node_ids[gateway_id], timeout=10
                    )
                    if (
                        node_ids[sender_id] not in gateway_peers
                        or node_ids[gateway_id] not in sender_peers
                    ):
                        continue
                    sender_state = sender.gateway_status(timeout=10)
                    if sender_state["hasInternet"]:
                        break
                except TimeoutWaitingFor:
                    pass
                time.sleep(2)

        yield {
            "clients": clients,
            "node_ids": node_ids,
            "gateway_id": gateway_id,
            "gateway": gateway,
            "sender_id": sender_id,
            "sender": sender,
            "endpoint": endpoint,
            "gateway_state": state,
            "sender_state": sender_state,
            "gateway_peers": gateway_peers,
            "sender_peers": sender_peers,
            "upstream_ready": upstream_ready,
        }
    finally:
        if gateway_started:
            gateway.start_regular_mesh(timeout=30)
            # Role changes reboot the bridge.  Restore the complete topology
            # before later modules run so a gateway failure cannot manufacture
            # unrelated mesh regressions.
            for board_id, client in clients.items():
                expected = {
                    node_id for peer_id, node_id in node_ids.items() if peer_id != board_id
                }
                for peer_node_id in expected:
                    peers = _wait_for_peer(client, peer_node_id, timeout=120)
                    if peer_node_id not in peers:
                        pytest.fail(
                            f"{board_id} did not restore peer {peer_node_id}: {peers}"
                        )
        for client in clients.values():
            client.clear_pending()


def _require_upstream(gateway_mesh):
    if not gateway_mesh["upstream_ready"]:
        pytest.skip("blocked: bridge did not establish a usable upstream connection")
    if gateway_mesh["sender_state"] is None:
        pytest.skip("blocked: controlled bridge/sender mesh route did not converge")


@pytest.mark.capability("gateway.bridge")
def test_dedicated_bridge_has_upstream_wifi_and_mesh(gateway_mesh):
    state = gateway_mesh["gateway_state"]
    assert state["initialized"] is True
    assert state["isBridge"] is True
    assert int(state["wifiStatus"]) == 3  # Arduino WL_CONNECTED
    assert state["localIP"] != "0.0.0.0"
    assert 1 <= int(state["channel"]) <= 13
    assert gateway_mesh["node_ids"][gateway_mesh["sender_id"]] in gateway_mesh[
        "gateway_peers"
    ]


@pytest.mark.capability("gateway.discovery")
def test_regular_node_discovers_gateway_internet(gateway_mesh):
    _require_upstream(gateway_mesh)
    state = gateway_mesh["sender_state"]
    assert state is not None
    assert state["isBridge"] is False
    assert state["hasInternet"] is True
    assert state["hasLocalInternet"] is False


def _send_and_wait(gateway_mesh, tag: str, path: str, payload: str = "") -> dict:
    _require_upstream(gateway_mesh)
    sender = gateway_mesh["sender"]
    url = f"{gateway_mesh['endpoint']}{path}"
    message_id = sender.send_to_internet(tag, url, payload=payload)
    assert message_id > 0
    return sender.wait_internet_result(tag, timeout=90)


@pytest.mark.capability("internet.get")
def test_regular_node_relays_http_get_through_bridge(gateway_mesh):
    tag = f"get-{time.time_ns()}"
    result = _send_and_wait(gateway_mesh, tag, f"/status/200?tag={tag}")
    assert result["success"] is True
    assert result["httpStatus"] == 200


@pytest.mark.capability("internet.post")
def test_regular_node_relays_http_post_payload_through_bridge(gateway_mesh):
    tag = f"post-{time.time_ns()}"
    payload = json.dumps({"source": gateway_mesh["sender_id"], "value": 42})
    result = _send_and_wait(gateway_mesh, tag, f"/echo?tag={tag}", payload)
    assert result["success"] is True
    assert result["httpStatus"] == 200

    with urlopen(f"{gateway_mesh['endpoint']}/requests/{tag}", timeout=5) as response:
        observed = json.load(response)
    assert observed["method"] == "POST"
    assert json.loads(observed["body"]) == json.loads(payload)


@pytest.mark.capability("internet.errors")
def test_gateway_returns_honest_http_failure(gateway_mesh):
    tag = f"error-{time.time_ns()}"
    result = _send_and_wait(gateway_mesh, tag, f"/status/400?tag={tag}")
    assert result["success"] is False
    assert result["httpStatus"] == 400
    assert "400" in result["error"]


@pytest.mark.capability("internet.recovery")
def test_gateway_relays_again_after_upstream_request_failure(gateway_mesh):
    """A failed upstream transaction must not poison the relay data path."""
    failed_tag = f"recovery-fail-{time.time_ns()}"
    failed = _send_and_wait(
        gateway_mesh, failed_tag, f"/status/503?tag={failed_tag}"
    )
    assert failed["success"] is False
    assert failed["httpStatus"] == 503

    recovered_tag = f"recovery-ok-{time.time_ns()}"
    recovered = _send_and_wait(
        gateway_mesh, recovered_tag, f"/status/200?tag={recovered_tag}"
    )
    assert recovered["success"] is True
    assert recovered["httpStatus"] == 200


@pytest.mark.capability("gateway.failover", "internet.recovery")
def test_backup_gateway_carries_traffic_after_primary_leaves(gateway_mesh):
    """Remove the active bridge and prove a second physical bridge carries data."""
    clients = gateway_mesh["clients"]
    if len(clients) < 3:
        pytest.skip("gateway failover needs two bridges and one regular node")
    _require_upstream(gateway_mesh)

    backup_id = next(
        board_id
        for board_id in clients
        if board_id not in {gateway_mesh["gateway_id"], gateway_mesh["sender_id"]}
    )
    backup = clients[backup_id]
    ssid, password, _ = _gateway_settings()
    backup_started = False
    try:
        backup_state = backup.start_gateway_failover(ssid, password)
        backup_started = True
        assert backup_state["isBridge"] is False, backup_id

        backup_node = gateway_mesh["node_ids"][backup_id]
        sender = gateway_mesh["sender"]
        sender_peers = _wait_for_peer(sender, backup_node, timeout=120)
        assert backup_node in sender_peers

        control_tag = f"failover-before-{time.time_ns()}"
        control = _send_and_wait(
            gateway_mesh, control_tag, f"/status/200?tag={control_tag}"
        )
        assert control["success"] is True

        # This reboots the primary into a regular mesh node, removing its
        # upstream route without relying on unsupported per-port hub power.
        gateway_mesh["gateway"].start_regular_mesh(timeout=35)

        # Election/promotion and regular-node route convergence are separate
        # asynchronous phases.  Do not spend the sender's reconnect budget
        # while the candidate is still waiting for the election monitor, and
        # avoid continuously polling both radios during that transition.
        promotion_deadline = time.monotonic() + 120
        while time.monotonic() < promotion_deadline:
            backup_state = backup.gateway_status(timeout=10)
            if (
                backup_state["isBridge"] is True
                and backup_state["hasInternet"] is True
            ):
                break
            time.sleep(2)
        assert backup_state["isBridge"] is True, backup_state
        assert backup_state["hasInternet"] is True, backup_state

        discovery_deadline = time.monotonic() + 120
        sender_state = sender.gateway_status(timeout=10)
        while (
            int(sender_state["primaryGateway"]) != backup_node
            and time.monotonic() < discovery_deadline
        ):
            time.sleep(2)
            sender_state = sender.gateway_status(timeout=10)
        assert int(sender_state["primaryGateway"]) == backup_node, sender_state

        # Gateway discovery can precede readiness of the newly rebuilt data
        # path by one transaction.  Use unique request IDs and require a real
        # successful relay within a small bounded recovery window.
        recovered = None
        for attempt in range(1, 4):
            recovered_tag = f"failover-after-{attempt}-{time.time_ns()}"
            recovered = _send_and_wait(
                gateway_mesh, recovered_tag, f"/status/200?tag={recovered_tag}"
            )
            if recovered["success"] is True:
                break
            time.sleep(3)
            sender_state = sender.gateway_status(timeout=10)
            assert int(sender_state["primaryGateway"]) == backup_node, sender_state
        assert recovered is not None
        assert recovered["success"] is True
        assert recovered["httpStatus"] == 200, recovered
    finally:
        if backup_started:
            backup.start_regular_mesh(timeout=35)
