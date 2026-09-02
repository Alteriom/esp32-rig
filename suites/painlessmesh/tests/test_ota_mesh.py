"""Transfer and activate a real firmware image through the physical mesh."""

import os
from pathlib import Path

import pytest

from suites.painlessmesh.build_artifacts import load_artifacts


@pytest.mark.hil_only(reason="multi_node")
@pytest.mark.capability("ota.mesh")
def test_same_family_firmware_transfers_activates_and_rejoins(mesh, board_map):
    if board_map is None:
        pytest.skip("mesh OTA activation is a physical-board property")

    classic = [board.id for board in board_map if board.target == "esp32"]
    if len(classic) < 2:
        pytest.skip("safe OTA validation requires two boards of the same MCU family")

    artifact_dir = os.environ.get("ALTERIOM_HIL_ARTIFACT_DIR")
    if not artifact_dir:
        pytest.skip("OTA candidate artifact is not available")
    artifacts = load_artifacts(Path(artifact_dir))
    ota = artifacts["targets"]["esp32"].get("ota")
    if not ota:
        pytest.fail("ESP32 artifact manifest is missing its OTA candidate")

    clients, _ = mesh
    sender = clients[classic[0]]
    receiver = clients[classic[1]]
    role = f"hil-{receiver.board_id}-{ota['sha256'][:8]}"

    receiver_boot = receiver.enable_ota_receiver(role)
    assert int(receiver_boot["otaGeneration"]) == 1
    sender.wait_mesh_size(len(clients) - 1, timeout=90)
    receiver.wait_mesh_size(len(clients) - 1, timeout=90)

    verified = sender.upload_ota_source(Path(ota["path"]), role)
    assert int(verified["bytes"]) == int(ota["size"])
    offered = sender.offer_ota()
    assert offered["md5"] == verified["md5"]

    activated = receiver.wait_ota_generation(int(ota["generation"]), timeout=360)
    assert activated["target"] == "esp32"
    assert activated["painlessMeshRef"] == os.environ.get("HIL_FIRMWARE_SHA")
    info = receiver.info(timeout=20)
    assert int(info["otaGeneration"]) == int(ota["generation"])
    receiver.wait_mesh_size(len(clients) - 1, timeout=120)
