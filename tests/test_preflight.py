from types import SimpleNamespace

from suites.painlessmesh.preflight import validate_info


def test_validate_info_accepts_exact_artifact_identity():
    board = SimpleNamespace(target="esp32-s3")
    manifest = {"painlessmesh_sha": "mesh-sha", "hil_agent_sha": "agent-sha"}
    info = {
        "target": "esp32-s3",
        "painlessMeshRef": "mesh-sha",
        "hilAgentSha": "agent-sha",
    }
    assert validate_info(board, info, manifest) == []


def test_validate_info_reports_every_mismatch():
    board = SimpleNamespace(target="esp32-s3")
    manifest = {"painlessmesh_sha": "mesh-sha", "hil_agent_sha": "agent-sha"}
    errors = validate_info(board, {"target": "esp32"}, manifest)
    assert len(errors) == 3
    assert "target" in errors[0]
