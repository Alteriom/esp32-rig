"""Broadcast delivery confirmation: exactly one ack per mesh peer."""

import pytest


@pytest.mark.hil_only(reason="radio_timing")
@pytest.mark.capability("mesh.broadcast", "delivery.ack")
def test_broadcast_with_ack_confirms_every_node(mesh):
    clients, node_ids = mesh
    if len(clients) < 2:
        import pytest

        pytest.skip("needs at least 2 boards")
    for sender_id, sender in clients.items():
        payload = f"hil-bcast:{sender_id}"
        receivers = {
            board_id: client
            for board_id, client in clients.items()
            if board_id != sender_id
        }
        expected_acks = {node_ids[board_id] for board_id in receivers}

        assert sender.send_broadcast(payload, ack=True)
        seen = set()
        for expected_node in expected_acks:
            ack = sender.wait_ack(node=expected_node, timeout=20)
            assert ack["delivered"] is True
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

    for client in clients.values():
        client.clear_pending()
