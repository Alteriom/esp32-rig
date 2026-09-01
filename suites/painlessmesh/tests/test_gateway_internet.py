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
        if upstream_ready:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    sender.wait_mesh_size(len(clients) - 1, timeout=10)
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
            "upstream_ready": upstream_ready,
        }
    finally:
        if gateway_started:
            gateway.start_regular_mesh(timeout=30)
            # Role changes reboot the bridge.  Restore the complete topology
            # before later modules run so a gateway failure cannot manufacture
            # unrelated mesh regressions.
            for client in clients.values():
                client.wait_mesh_size(len(clients) - 1, timeout=120)
        for client in clients.values():
            client.clear_pending()


def _require_upstream(gateway_mesh):
    if not gateway_mesh["upstream_ready"]:
        pytest.skip("blocked: bridge did not establish a usable upstream connection")


@pytest.mark.capability("gateway.bridge")
def test_dedicated_bridge_has_upstream_wifi_and_mesh(gateway_mesh):
    state = gateway_mesh["gateway_state"]
    assert state["initialized"] is True
    assert state["isBridge"] is True
    assert int(state["wifiStatus"]) == 3  # Arduino WL_CONNECTED
    assert state["localIP"] != "0.0.0.0"
    assert 1 <= int(state["channel"]) <= 13
    assert len(gateway_mesh["gateway"].node_list()) >= 1


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
