"""Every board sees every other board — the baseline for all other tests."""

import json
import time

import pytest

from alteriom_hil.protocol import TimeoutWaitingFor


@pytest.mark.hil_only(reason="radio_timing")
@pytest.mark.capability("mesh.formation", "mesh.mixed_mcu")
def test_all_boards_form_one_mesh(mesh):
    clients, node_ids = mesh
    all_ids = set(node_ids.values())
    assert len(all_ids) == len(clients), "duplicate nodeIds on the rig"
    for board_id, client in clients.items():
        expected = all_ids - {node_ids[board_id]}
        deadline = time.monotonic() + 30
        while True:
            peers = set(client.node_list())
            if peers >= expected or time.monotonic() >= deadline:
                break
            time.sleep(1)
        assert peers >= expected, (
            f"{board_id} sees {peers}, expected at least {expected}"
        )


@pytest.mark.hil_only(reason="radio_timing")
@pytest.mark.capability("mesh.unicast", "mesh.bidirectional", "mesh.payload_integrity")
def test_every_board_delivers_to_every_other_board(mesh):
    """Prove exact bidirectional application data across the full board matrix."""
    clients, node_ids = mesh
    if len(clients) < 2:
        pytest.skip("needs at least 2 boards")

    for sender_id, sender in clients.items():
        for receiver_id, receiver in clients.items():
            if sender_id == receiver_id:
                continue
            receiver_node_id = node_ids[receiver_id]
            last_failure = None
            attempt = 0
            route_deadline = time.monotonic() + 60
            while time.monotonic() < route_deadline:
                attempt += 1
                # Exercise content that is materially closer to an application
                # message than a short sentinel.  Exact equality at the receiver
                # detects truncation, escaping damage, and cross-message mixing.
                payload = json.dumps(
                    {
                        "kind": "hil-mesh-data",
                        "source": sender_id,
                        "destination": receiver_id,
                        "attempt": attempt,
                        "escaped": "quote=\" slash=\\ newline=\\n",
                        "sequence": list(range(16)),
                        "padding": (f"{sender_id}>{receiver_id}|" * 24)[:320],
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if not sender.send_single(receiver_node_id, payload, ack=True):
                    last_failure = "sender had no route when it accepted the command"
                    time.sleep(1)
                    continue
                try:
                    ack = sender.wait_ack(node=receiver_node_id, timeout=15)
                    if ack["delivered"] is not True:
                        last_failure = (
                            f"delivery callback timed out after {ack['latencyMs']} ms"
                        )
                        time.sleep(1)
                        continue
                    received = receiver.wait_for(
                        lambda event, expected=payload: (
                            event["evt"] == "recv" and event["msg"] == expected
                        ),
                        f"unicast {sender_id} -> {receiver_id}",
                        timeout=8,
                    )
                    assert int(received["from"]) == node_ids[sender_id]
                    break
                except TimeoutWaitingFor as exc:
                    # CP2102 UART telemetry can occasionally lose one line even
                    # though painlessMesh delivered and ACKed the packet. A
                    # bounded retransmission distinguishes that observation loss
                    # from a persistently broken radio or monitoring path.
                    last_failure = exc
                    time.sleep(1)
            else:
                pytest.fail(
                    f"{sender_id} -> {receiver_id} was not fully observed "
                    f"after {attempt} attempts over 60 seconds: {last_failure}"
                )

    for client in clients.values():
        client.clear_pending()
