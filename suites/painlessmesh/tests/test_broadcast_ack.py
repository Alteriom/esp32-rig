"""Broadcast delivery confirmation and application-payload integrity."""

import json
import time

import pytest

from alteriom_hil.protocol import TimeoutWaitingFor


@pytest.mark.hil_only(reason="radio_timing")
@pytest.mark.capability("mesh.broadcast", "mesh.payload_integrity", "delivery.ack")
def test_broadcast_with_ack_confirms_every_node(mesh):
    clients, node_ids = mesh
    if len(clients) < 2:
        pytest.skip("needs at least 2 boards")
    for sender_id, sender in clients.items():
        receivers = {
            board_id: client
            for board_id, client in clients.items()
            if board_id != sender_id
        }
        expected_acks = {node_ids[board_id] for board_id in receivers}
        last_failure = None
        for attempt in range(1, 4):
            payload = json.dumps(
                {
                    "kind": "hil-broadcast-data",
                    "source": sender_id,
                    "attempt": attempt,
                    "escaped": "quote=\" slash=\\ newline=\\n",
                    "sequence": list(range(24)),
                    "padding": (f"broadcast:{sender_id}|" * 24)[:384],
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            for client in clients.values():
                client.clear_pending()
            try:
                assert sender.send_broadcast(payload, ack=True)
                seen = set()
                for expected_node in expected_acks:
                    ack = sender.wait_ack(node=expected_node, timeout=20)
                    if ack["delivered"] is not True:
                        raise AssertionError(
                            f"delivery callback timed out for node {expected_node}"
                        )
                    seen.add(int(ack["node"]))
                assert seen == expected_acks

                for receiver_id, receiver in receivers.items():
                    event = receiver.wait_for(
                        lambda item, expected=payload: (
                            item["evt"] == "recv" and item["msg"] == expected
                        ),
                        f"broadcast {sender_id} -> {receiver_id}",
                        timeout=15,
                    )
                    assert int(event["from"]) == node_ids[sender_id]
                break
            except (AssertionError, TimeoutWaitingFor) as exc:
                last_failure = exc
                time.sleep(1)
        else:
            pytest.fail(
                f"{sender_id} did not deliver one acknowledged broadcast to "
                f"every receiver after 3 attempts: {last_failure}"
            )

    for client in clients.values():
        client.clear_pending()
