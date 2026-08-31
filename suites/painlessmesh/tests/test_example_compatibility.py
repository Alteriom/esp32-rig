"""Exercise public API patterns used by the upstream painlessMesh examples."""

import pytest


@pytest.mark.hil_only(reason="multi_node")
@pytest.mark.parametrize("priority", [0, 1, 2, 3])
def test_priority_example_broadcast_levels(mesh, priority):
    """Mirrors examples/priority/priority_basic_example on real radios."""
    clients, _ = mesh
    if len(clients) < 2:
        pytest.skip("needs at least 2 boards")
    ids = list(clients)
    sender = clients[ids[0]]
    payload = f'{{"example":"priority","level":{priority}}}'
    assert sender.send_broadcast(payload, priority=priority)
    for board_id in ids[1:]:
        event = clients[board_id].wait_recv(timeout=15)
        assert event["msg"] == payload


@pytest.mark.hil_only(reason="multi_node")
def test_priority_example_direct_message(pair):
    """Mirrors the priority example's sendCommandToNode helper."""
    sender, receiver, receiver_id = pair
    assert sender.send_single(receiver_id, "priority-command", priority=1)
    assert receiver.wait_recv(timeout=15)["msg"] == "priority-command"
