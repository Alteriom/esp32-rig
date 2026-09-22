import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "suites" / "painlessmesh" / "simulator_evidence.py"
SPEC = importlib.util.spec_from_file_location("simulator_evidence", SCRIPT_PATH)
simulator_evidence = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(simulator_evidence)


def test_build_evidence_normalizes_protocol_and_mesh_results():
    report = {
        "validation_gate": "passed",
        "total_runs": 12,
        "capabilities": {
            "broadcast": {"status": "validated"},
            "gateway": {"status": "gap"},
            "mesh_formation": {"status": "validated"},
        },
    }
    result = simulator_evidence.build_evidence(
        report, "A" * 40, "B" * 40, ["message-accounting"]
    )
    assert result["painlessmesh_sha"] == "a" * 40
    assert result["protocol_sim"]["capabilities"] == ["broadcast", "mesh_formation"]
    assert result["mesh_sim"]["simulator_sha"] == "b" * 40


def test_build_evidence_refuses_incomplete_protocol_gate():
    with pytest.raises(ValueError, match="did not pass"):
        simulator_evidence.build_evidence(
            {"validation_gate": "incomplete"}, "a" * 40, "b" * 40, ["smoke"]
        )
