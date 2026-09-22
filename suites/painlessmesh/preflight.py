#!/usr/bin/env python3
"""Verify every freshly flashed board before running capability tests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rig"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

from alteriom_hil.board import BoardMap  # noqa: E402
from alteriom_hil.protocol import BoardClient  # noqa: E402
from alteriom_hil.pytest_plugin import _serial_opener  # noqa: E402
from alteriom_hil.serial_capture import SerialCapture  # noqa: E402


def validate_info(board, info: dict, manifest: dict) -> list[str]:
    errors = []
    expected = {
        "target": board.target,
        "painlessMeshRef": manifest["painlessmesh_sha"],
        "hilAgentSha": manifest["hil_agent_sha"],
    }
    for field, value in expected.items():
        if info.get(field) != value:
            errors.append(f"{field}: expected {value!r}, got {info.get(field)!r}")
    # The base image is OTA generation 1. A board still running the
    # generation-2 image the OTA test sent it matches on every other field,
    # and a run that reuses a flash on the strength of this check must not
    # take it for the base image.
    if "otaGeneration" in info and info["otaGeneration"] != 1:
        errors.append(f"otaGeneration: expected 1, got {info['otaGeneration']!r}")
    return errors


def run(board_map: BoardMap, manifest: dict, log_dir: Path) -> dict:
    import serial  # pyserial is required only on the physical runner

    log_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for board in board_map:
        raw_log, info, errors = [], {}, []
        attempts = 0
        for attempts in range(1, 3):
            capture = SerialCapture(_serial_opener(serial, board)).start()
            try:
                client = BoardClient(board.id, capture)
                info = client.info(timeout=30)
                errors = validate_info(board, info, manifest)
                if not errors:
                    break
            except Exception as exc:
                errors = [f"{type(exc).__name__}: {exc}"]
            finally:
                raw_log.extend(
                    [f"--- attempt {attempts} ---", *capture.raw_log]
                )
                capture.stop()
        (log_dir / f"{board.id}.preflight.serial.log").write_text(
            "\n".join(raw_log) + "\n", encoding="utf-8"
        )
        results.append(
            {
                "board": board.id,
                "port": board.port,
                "target": board.target,
                "node_id": info.get("nodeId"),
                "status": "failed" if errors else "passed",
                "attempts": attempts,
                "recovered_after_retry": not errors and attempts > 1,
                "errors": errors,
            }
        )
    return {
        "status": "passed" if all(item["status"] == "passed" for item in results) else "failed",
        "boards": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-map", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = run(BoardMap.load(args.board_map), manifest, args.log_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for item in result["boards"]:
        details = "; ".join(item["errors"]) if item["errors"] else f"node {item['node_id']}"
        print(f"{item['status'].upper()}: {item['board']} ({item['target']}): {details}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
