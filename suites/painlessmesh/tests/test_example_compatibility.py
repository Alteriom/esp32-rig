"""Exercise public API patterns used by the upstream painlessMesh examples."""

import pytest

from alteriom_hil.protocol import TimeoutWaitingFor


@pytest.mark.hil_only(reason="multi_node")
@pytest.mark.capability("priority.levels")
@pytest.mark.parametrize("priority", [0, 1, 2, 3])
def test_priority_example_broadcast_levels(mesh, priority):
    """Mirrors examples/priority/priority_basic_example on real radios."""
    clients, _ = mesh
    if len(clients) < 2:
        pytest.skip("needs at least 2 boards")
    ids = list(clients)
    sender = clients[ids[0]]
    payload = f'{{"example":"priority","level":{priority}}}'
    # A priority broadcast intentionally has no delivery acknowledgement.
    # Require every receiver to observe the exact payload, with bounded
    # retransmission so one lost best-effort radio frame or damaged UART event
    # cannot masquerade as a broken priority implementation.
    outstanding = set(ids[1:])
    last_timeout = None
    for _ in range(3):
        assert sender.send_broadcast(payload, priority=priority)
        for board_id in list(outstanding):
            try:
                clients[board_id].wait_for(
                    lambda item, expected=payload: (
                        item["evt"] == "recv" and item["msg"] == expected
                    ),
                    f"priority {priority} broadcast",
                    timeout=6,
                )
                outstanding.remove(board_id)
            except TimeoutWaitingFor as exc:
                last_timeout = exc
        if not outstanding:
            break
    assert not outstanding, (
        f"priority {priority} was not observed by {sorted(outstanding)} "
        f"after 3 broadcasts: {last_timeout}"
    )


@pytest.mark.hil_only(reason="multi_node")
@pytest.mark.capability("priority.levels")
def test_priority_example_direct_message(pair):
    """Mirrors the priority example's sendCommandToNode helper."""
    sender, receiver, receiver_id = pair
    assert sender.send_single(receiver_id, "priority-command", priority=1)
    event = receiver.wait_for(
        lambda item: item["evt"] == "recv" and item["msg"] == "priority-command",
        "priority direct message",
        timeout=15,
    )
    assert event["msg"] == "priority-command"
