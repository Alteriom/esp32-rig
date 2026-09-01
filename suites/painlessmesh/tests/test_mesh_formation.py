"""Every board sees every other board — the baseline for all other tests."""

import pytest

from alteriom_hil.protocol import TimeoutWaitingFor


@pytest.mark.hil_only(reason="radio_timing")
def test_all_boards_form_one_mesh(mesh):
    clients, node_ids = mesh
    all_ids = set(node_ids.values())
    assert len(all_ids) == len(clients), "duplicate nodeIds on the rig"
    for board_id, client in clients.items():
        peers = set(client.node_list())
        expected = all_ids - {node_ids[board_id]}
        assert peers >= expected, (
            f"{board_id} sees {peers}, expected at least {expected}"
        )


@pytest.mark.hil_only(reason="radio_timing")
def test_every_board_delivers_to_every_other_board(mesh):
    """Prove bidirectional unicast across the full physical-board matrix."""
    clients, node_ids = mesh
    if len(clients) < 2:
        pytest.skip("needs at least 2 boards")

    for sender_id, sender in clients.items():
        for receiver_id, receiver in clients.items():
            if sender_id == receiver_id:
                continue
            receiver_node_id = node_ids[receiver_id]
            last_timeout = None
            for attempt in range(1, 3):
                payload = f"hil-matrix:{sender_id}->{receiver_id}:{attempt}"
                assert sender.send_single(receiver_node_id, payload, ack=True)
                try:
                    ack = sender.wait_ack(node=receiver_node_id, timeout=15)
                    assert ack["delivered"] is True
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
                    last_timeout = exc
            else:
                pytest.fail(
                    f"{sender_id} -> {receiver_id} was not fully observed "
                    f"after 2 attempts: {last_timeout}"
                )

    for client in clients.values():
        client.clear_pending()
