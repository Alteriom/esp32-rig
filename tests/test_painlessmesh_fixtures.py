"""The painlessMesh suite's own fixtures.

`mesh` proves the mesh formed once, at the start of a twenty-minute suite.
`pair` is per-test, and until 2026-09-13 it trusted that session-scoped
proof: it handed a test two boards without asking whether they could still
reach each other. A board that dropped its uplink mid-suite therefore
reached a delivery assertion, and the failure said "timed out waiting for
recv event" while the sender's own verdict -- delivered=False after 5 s --
went unread. This is what keeps that from happening again.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _conftest_source() -> str:
    return (REPO / "suites" / "painlessmesh" / "tests" / "conftest.py").read_text(
        encoding="utf-8"
    )


def test_the_pair_fixture_checks_both_boards_are_still_peers():
    source = _conftest_source()
    body = source.split("def pair(mesh):", 1)[1]
    # Both directions. A unicast needs a route out and an answer back, and a
    # half-torn-down connection is exactly the asymmetric case.
    assert "sender.wait_for_peer(node_ids[receiver_id]" in body
    assert "receiver.wait_for_peer(node_ids[sender_id]" in body
    # Before the test body, not after it.
    checks = body.index("wait_for_peer")
    assert checks < body.index("yield"), "the check is a precondition"


def test_the_pair_window_is_short_enough_that_a_rejoin_still_fails():
    """painlessMesh takes about 15 s to rejoin after losing its last uplink
    (`0.5 * SCAN_INTERVAL`, which its log calls "fast"). A window that
    covered that would turn a mesh outage into a slow pass, which is the
    failure mode this whole change exists to remove."""
    source = _conftest_source()
    line = next(
        item for item in source.splitlines() if item.startswith("PAIR_PEER_TIMEOUT")
    )
    default = float(line.split('"')[-2])
    assert 0 < default <= 5, f"{default}s would wait out a rejoin, not report it"
    assert "ALTERIOM_HIL_PAIR_PEER_TIMEOUT" in line, "an operator can widen it"


def test_the_client_offers_the_primitive_the_fixture_needs():
    sys.path.insert(0, str(REPO / "rig"))
    sys.path.insert(0, str(REPO / "core"))
    from alteriom_hil.protocol import BoardClient
    from suites.painlessmesh.meshclient import MeshBoardClient

    assert hasattr(MeshBoardClient, "wait_for_peer")
    assert not hasattr(BoardClient, "wait_for_peer"), "the HAL's client knows no mesh"
    # A count is a different question, and both are kept: mesh formation
    # asks for N peers, a delivery test asks for one named board.
    assert hasattr(MeshBoardClient, "wait_mesh_size")
