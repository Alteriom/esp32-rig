"""Broadcast delivery confirmation: exactly one ack per mesh peer."""

import pytest


@pytest.mark.hil_only(reason="radio_timing")
def test_broadcast_with_ack_confirms_every_node(mesh):
    clients, node_ids = mesh
    if len(clients) < 2:
        import pytest

        pytest.skip("needs at least 2 boards")
    ids = list(clients)
    sender = clients[ids[0]]
    others = {node_ids[b] for b in ids[1:]}

    assert sender.send_broadcast("hil-bcast-1", ack=True)
    seen = set()
    for _ in range(len(others)):
        ack = sender.wait_ack(timeout=20)
        assert ack["delivered"] is True
        seen.add(int(ack["node"]))
    assert seen == others

    for board_id in ids[1:]:
        clients[board_id].wait_for(
            lambda e: e["evt"] == "recv" and e["msg"] == "hil-bcast-1",
            "broadcast recv",
            timeout=15,
        )
