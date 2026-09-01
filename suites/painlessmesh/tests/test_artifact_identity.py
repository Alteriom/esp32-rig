"""Every physical board must report the exact artifact selected by its map."""

import os

import pytest

from suites.painlessmesh.build_artifacts import agent_source_sha


@pytest.mark.hil_only(reason="multi_node")
@pytest.mark.capability("artifact.provenance", "artifact.family_matrix")
def test_board_artifact_matches_inventory(bank, board_map):
    if board_map is None:
        pytest.skip("artifact identity is a physical-board property")
    expected_ref = os.environ.get("HIL_FIRMWARE_SHA")
    expected_agent = agent_source_sha()
    inventory = {board.id: board for board in board_map}
    for board_id, client in bank.items():
        info = client.info(timeout=15)
        assert info["target"] == inventory[board_id].target
        assert info["hilAgentSha"] == expected_agent
        if expected_ref:
            assert info["painlessMeshRef"] == expected_ref
