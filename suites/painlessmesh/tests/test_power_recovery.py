"""A power-cut node must come back and rejoin the mesh.

This is the rig's own recovery path under test (docs/runbook.md rung 1):
cut a board's USB power, restore it, reattach the serial capture, and
require the mesh to heal. It is unreachable in compile-only CI and skips
automatically on rigs without a switchable hub.
"""

from __future__ import annotations

import time

import pytest

from alteriom_hil.protocol import TimeoutWaitingFor

REJOIN_TIMEOUT = 180.0


def _node_id_when_ready(client, timeout: float):
    """Poll a freshly-booted board until it answers an info query."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return client.node_id(timeout=5.0)
        except TimeoutWaitingFor:
            time.sleep(1.0)
    raise AssertionError(
        f"{client.board_id} never answered after power-on "
        f"(waited {timeout:.0f}s)"
    )


@pytest.mark.hil_only(reason="power")
def test_node_rejoins_mesh_after_power_cut(mesh, power, board_map):
    if board_map is None:
        pytest.skip("no hardware board map")
    if not power.available:
        pytest.skip("rig has no switchable power")

    clients, node_ids = mesh
    if len(clients) < 2:
        pytest.skip("needs at least 2 boards to observe a mesh heal")

    # Cut a board that (a) has power coordinates and (b) is in the mesh, and
    # never the first one — the survivors are who we assert the heal against.
    victim = next(
        (
            b
            for b in board_map
            if power.supports(b) and b.id in clients and b.id != list(clients)[0]
        ),
        None,
    )
    if victim is None:
        pytest.skip("no switchable board available to cut")

    victim_client = clients[victim.id]
    victim_node = node_ids[victim.id]
    survivors = [c for bid, c in clients.items() if bid != victim.id]

    power.cycle(victim, off_seconds=2.0)

    # The device node vanished with the power; wait for udev to recreate it.
    victim_client.reattach(timeout=60.0)

    rebooted_node = _node_id_when_ready(victim_client, timeout=60.0)
    assert rebooted_node == victim_node, (
        f"{victim.id} changed nodeId across a reboot "
        f"({victim_node} -> {rebooted_node}); mesh routing assumes it is stable"
    )

    for client in survivors:
        client.wait_mesh_size(len(clients) - 1, timeout=REJOIN_TIMEOUT)

    for client in survivors:
        assert victim_node in client.node_list(), (
            f"{client.board_id} never saw {victim.id} ({victim_node}) rejoin "
            f"within {REJOIN_TIMEOUT:.0f}s"
        )

    victim_client.clear_pending()
    for client in survivors:
        client.clear_pending()
