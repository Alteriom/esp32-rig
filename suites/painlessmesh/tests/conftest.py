"""painlessMesh HIL suite configuration.

Uses the shared ``bank`` fixture from the alteriom_hil pytest plugin.
In hardware mode, boards must already be flashed (CI runs flash_all.py
first) and need time to form a mesh — ``mesh`` waits for that.
"""

from __future__ import annotations

import os

import pytest

from alteriom_hil.protocol import BoardClient

MESH_FORM_TIMEOUT = float(os.environ.get("ALTERIOM_HIL_MESH_TIMEOUT", "120"))


@pytest.fixture(scope="session")
def mesh(bank):
    """The full board bank, verified to have formed one mesh.

    Yields ``(clients, node_ids)`` where node_ids maps board_id -> nodeId.
    """
    clients: dict[str, BoardClient] = bank
    expected_peers = len(clients) - 1
    node_ids = {}
    for board_id, client in clients.items():
        node_ids[board_id] = client.node_id(timeout=30)
    if expected_peers > 0:
        for board_id, client in clients.items():
            client.wait_mesh_size(expected_peers, timeout=MESH_FORM_TIMEOUT)
    for client in clients.values():
        client.clear_pending()
    return clients, node_ids


@pytest.fixture()
def pair(mesh):
    """(sender, receiver, receiver_node_id) from two distinct boards."""
    clients, node_ids = mesh
    if len(clients) < 2:
        pytest.skip("needs at least 2 boards")
    ids = list(clients)
    sender, receiver = clients[ids[0]], clients[ids[1]]
    yield sender, receiver, node_ids[ids[1]]
    sender.clear_pending()
    receiver.clear_pending()
