"""painlessMesh HIL suite configuration.

Uses the shared ``bank`` fixture from the alteriom_hil pytest plugin.
In hardware mode, boards must already be flashed (CI runs flash_all.py
first) and need time to form a mesh — ``mesh`` waits for that.
"""

from __future__ import annotations

import os
import secrets

import sys
from pathlib import Path

import pytest

# The agent's client lives beside the suite, one directory up, with the
# scripts that build, flash and preflight the same firmware.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meshclient import MeshBoardClient as BoardClient  # noqa: E402

MESH_FORM_TIMEOUT = float(os.environ.get("ALTERIOM_HIL_MESH_TIMEOUT", "120"))

# How long `pair` will wait for its two boards to see each other before it
# calls the mesh broken. Deliberately short: see the fixture.
PAIR_PEER_TIMEOUT = float(os.environ.get("ALTERIOM_HIL_PAIR_PEER_TIMEOUT", "3"))


@pytest.fixture(scope="session")
def board_client_class():
    """This suite's boards run painlessMesh's HIL agent, and are driven
    through its verbs (alteriom_hil.pytest_plugin.board_client_class)."""
    return BoardClient


@pytest.fixture(scope="session")
def mesh(bank):
    """The full board bank, verified to have formed one mesh.

    Yields ``(clients, node_ids)`` where node_ids maps board_id -> nodeId.
    """
    clients: dict[str, BoardClient] = bank
    if os.environ.get("ALTERIOM_HIL_MODE") == "hardware":
        run_id = os.environ.get("ALTERIOM_HIL_MESH_ID") or secrets.token_hex(5)
        mesh_prefix = f"AlteriomHIL-{run_id}"[:31]
        mesh_password = os.environ.get(
            "ALTERIOM_HIL_MESH_PASSWORD", "hil-isolated-mesh"
        )
        for client in clients.values():
            client.configure_mesh(mesh_prefix, mesh_password)
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
    """(sender, receiver, receiver_node_id) from two distinct boards.

    The two are checked to be in each other's peer list first. `mesh` is
    session-scoped, so it proves the mesh formed once, at the start of a
    twenty-minute suite; it says nothing about whether these two boards can
    still reach each other now.

    That gap cost an hour of log reading. A board dropped its uplink on a
    neighbour disagreement (`handleNodeSync(): invalid new connection`) and
    was out of the mesh for 16.2 s -- painlessMesh waits `0.5 *
    SCAN_INTERVAL` = 15 s before it even rescans, which its own log calls
    "fast". A unicast sent into that hole is accepted by the sender, whose
    routing table still lists the board until its own NODE_TIMEOUT, and is
    dropped. The test that did so reported "timed out waiting for recv
    event" -- true, and about the wrong thing: the sender had already said
    `delivered=False latencyMs=5000`, correctly, and the mesh was the
    problem.

    So this fails first, and names the board that left. The window is three
    seconds, not sixteen: a rejoin must still fail the test, because a mesh
    that drops a node for a quarter of a minute is a finding and not
    something to wait out.
    """
    clients, node_ids = mesh
    if len(clients) < 2:
        pytest.skip("needs at least 2 boards")
    ids = list(clients)
    sender_id, receiver_id = ids[0], ids[1]
    sender, receiver = clients[sender_id], clients[receiver_id]
    # Both directions: a unicast needs a route out and an answer back, and
    # the asymmetric case (one side has forgotten the other) is exactly the
    # state a half-torn-down connection leaves behind.
    sender.wait_for_peer(node_ids[receiver_id], timeout=PAIR_PEER_TIMEOUT)
    receiver.wait_for_peer(node_ids[sender_id], timeout=PAIR_PEER_TIMEOUT)
    yield sender, receiver, node_ids[receiver_id]
    sender.clear_pending()
    receiver.clear_pending()
