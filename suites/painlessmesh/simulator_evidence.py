#!/usr/bin/env python3
"""Create the bounded simulator evidence payload consumed by the farm service."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def build_evidence(
    protocol_report: dict,
    painlessmesh_sha: str,
    simulator_sha: str,
    scenarios: list[str],
) -> dict:
    if protocol_report.get("validation_gate") != "passed":
        raise ValueError("protocol simulator validation gate did not pass")
    capabilities = sorted(
        name
        for name, item in (protocol_report.get("capabilities") or {}).items()
        if item.get("status") == "validated"
    )
    return {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "painlessmesh_sha": painlessmesh_sha.lower(),
        "protocol_sim": {
            "status": "passed",
            "tests": int(protocol_report.get("total_runs", 0)),
            "capabilities": capabilities,
            "summary": f"{protocol_report.get('total_runs', 0)} HAL protocol scenarios passed",
        },
        "mesh_sim": {
            "status": "passed",
            "simulator_sha": simulator_sha.lower(),
            "scenarios": scenarios,
            "summary": f"painlessMesh simulator passed {len(scenarios)} behavioural scenario group(s)",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol-report", type=Path, required=True)
    parser.add_argument("--painlessmesh-sha", required=True)
    parser.add_argument("--simulator-sha", required=True)
    parser.add_argument("--scenario", action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    report = json.loads(args.protocol_report.read_text(encoding="utf-8"))
    evidence = build_evidence(
        report, args.painlessmesh_sha, args.simulator_sha, args.scenario
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(f"simulator evidence: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
