"""Sustained mixed-family traffic and heap-stability validation."""

from __future__ import annotations

import os
import time

import pytest

from alteriom_hil.protocol import TimeoutWaitingFor

# The ESP8266 envelope, in bytes free. Below this an 8 KB package or an OTA
# part fails to allocate; above it the part is within its specification as
# a leaf. Measured working set as an interior node of a seven-node mesh:
# 10–13 KB. See painlessMesh README, "ESP8266 capacity".
ESP8266_HEAP_FLOOR = 8 * 1024

pytestmark = [
    pytest.mark.hil_only(reason="soak"),
    pytest.mark.failure_class("real_bug"),
]


@pytest.mark.capability("soak.stability", "mesh.heap_stability", "delivery.ack.sustained")
def test_sustained_round_robin_delivery_has_no_loss_or_heap_collapse(mesh):
    if os.environ.get("ALTERIOM_HIL_MODE") != "hardware":
        pytest.skip("soak validation measures physical radios and ESP heap")
    clients, node_ids = mesh
    if len(clients) < 2:
        pytest.skip("soak validation needs at least two physical nodes")
    for client in clients.values():
        client.wait_mesh_size(len(clients) - 1, timeout=120)
    duration = max(10.0, float(os.environ.get("ALTERIOM_HIL_SOAK_SECONDS", "30")))
    initial_heap = {board_id: int(client.info()["freeHeap"]) for board_id, client in clients.items()}
    ordered = list(clients)
    deadline = time.monotonic() + duration
    delivered = 0
    while time.monotonic() < deadline:
        sender_id = ordered[delivered % len(ordered)]
        receiver_id = ordered[(delivered + 1) % len(ordered)]
        sender, receiver = clients[sender_id], clients[receiver_id]
        for observation_attempt in range(2):
            payload = (
                f"soak:{delivered}:{observation_attempt}:{sender_id}:{receiver_id}"
            )
            # The ack window and the receive wait must agree: with the ack at
            # 4 s and the wait at 8 s, a message delivered in between was
            # "received" and "not delivered" at once, and a sound delivery
            # that was merely slow failed the run as loss.
            assert sender.send_single(
                node_ids[receiver_id], payload, ack=True, ack_timeout_ms=8000
            )
            received = None
            acknowledgement = None
            try:
                # By payload, not merely by sender: the previous attempt's
                # message can arrive after its wait expired and after the
                # clear below, and it is not this attempt's evidence.
                received = receiver.wait_for(
                    lambda e, want=payload: e["evt"] == "recv"
                    and e.get("msg") == want,
                    f"recv of {payload}",
                    timeout=8,
                )
            except TimeoutWaitingFor:
                pass
            try:
                acknowledgement = sender.wait_ack(
                    node_ids[receiver_id], timeout=8
                )
            except TimeoutWaitingFor:
                pass
            if received is not None and acknowledgement is not None:
                assert received["msg"] == payload
                assert acknowledgement["delivered"] is True
                break
            if observation_attempt == 1:
                pytest.fail(
                    f"incomplete serial evidence after two delivered attempts: "
                    f"recv={received is not None}, ack={acknowledgement is not None}"
                )
            # Either independent telemetry line was damaged. Retry with a
            # unique payload so both payload and ACK evidence remain mandatory.
            sender.clear_pending()
            receiver.clear_pending()
        delivered += 1
    assert delivered >= len(clients) * 2
    final = {board_id: client.info() for board_id, client in clients.items()}
    for board_id, before in initial_heap.items():
        after = int(final[board_id]["freeHeap"])
        if final[board_id].get("target") == "esp8266":
            # Specified, not suspected: the ESP8266 is a leaf part in meshes
            # this size, and its heap tracks its live connections and the
            # traffic through them rather than leaking. Its envelope is a
            # floor below which packages stop allocating, not a fraction of
            # wherever it happened to start. The agent reports the leaf
            # condition itself as `capacity_warning`.
            assert after >= ESP8266_HEAP_FLOOR, (
                f"{board_id}: {after} B free is below the ESP8266 envelope "
                f"({ESP8266_HEAP_FLOOR} B); see painlessMesh README, "
                f"'ESP8266 capacity'"
            )
            continue
        # Allow allocator settling, but catch an operationally significant leak.
        assert after >= before * 0.75, (board_id, before, after)
