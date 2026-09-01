"""Validate the next-release shared-gateway role on every attached family."""

from __future__ import annotations

import time
import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.hil_only(reason="peripheral"),
    pytest.mark.failure_class("real_bug"),
]


def _gateway_settings():
    if os.environ.get("ALTERIOM_HIL_MODE") != "hardware":
        pytest.skip("shared gateway validation requires physical Wi-Fi radios")
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
def shared_gateway_mesh(mesh):
    clients, _ = mesh
    if len(clients) < 2:
        pytest.skip("shared gateway validation needs at least two physical nodes")
    ssid, password, endpoint = _gateway_settings()
    states = {}
    try:
        for board_id, client in clients.items():
            states[board_id] = client.start_shared_gateway(ssid, password, endpoint)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            states = {board_id: client.gateway_status() for board_id, client in clients.items()}
            if all(state["hasLocalInternet"] and state["localIP"] != "0.0.0.0" for state in states.values()):
                break
            time.sleep(2)
        yield clients, states, endpoint
    finally:
        for client in clients.values():
            client.start_regular_mesh(timeout=35)
            client.clear_pending()


@pytest.mark.capability("gateway.shared", "gateway.shared.mixed_mcu")
def test_every_node_establishes_local_upstream(shared_gateway_mesh):
    _, states, _ = shared_gateway_mesh
    assert len(states) >= 2
    for board_id, state in states.items():
        assert state["initialized"] is True, board_id
        assert state["isSharedGateway"] is True, board_id
        assert state["hasLocalInternet"] is True, board_id
        assert state["localIP"] != "0.0.0.0", board_id
        assert 1 <= int(state["channel"]) <= 13, board_id


@pytest.mark.capability("gateway.shared.internet")
def test_every_shared_gateway_can_reach_endpoint(shared_gateway_mesh):
    clients, _, endpoint = shared_gateway_mesh
    for board_id, client in clients.items():
        tag = f"shared-{board_id}-{time.time_ns()}"
        message_id = client.send_to_internet(tag, f"{endpoint}/status/200?tag={tag}")
        assert message_id > 0
        result = client.wait_internet_result(tag, timeout=60)
        assert result["success"] is True, board_id
        assert result["httpStatus"] == 200, board_id
