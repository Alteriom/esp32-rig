"""Every board sees every other board — the baseline for all other tests."""

import pytest


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
