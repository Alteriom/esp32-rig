#!/usr/bin/env python3
"""Flash a built bundle onto every matching board.

The farm does not build firmware: the bundle comes from painlessMesh's
pipeline, which builds it on a CI runner and hands it to the farm. This flashes
what it was given, after verifying every checksum.

Usage (the painlessmesh profile runs exactly this):

    ALTERIOM_HIL_BOARD_MAP=~/board-map.yaml \
    python3 flash_all.py --artifacts <bundle directory>

Boards are flashed sequentially (esptool contention on shared hubs makes
parallel flashing flaky on cheap rigs). Exits non-zero on the first
failure with the esptool output, so CI cleanly distinguishes "flash failed"
from "test failed".
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hal"))
from alteriom_hil.artifacts import load_artifacts  # noqa: E402
from alteriom_hil.board import BoardMap  # noqa: E402
from alteriom_hil.flash import flash_esptool  # noqa: E402

DEFAULT_ARTIFACT_DIR = Path(
    os.environ.get("ALTERIOM_HIL_ARTIFACT_DIR", "hil-firmware")
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
        help="artifact directory containing manifest.json",
    )
    args = parser.parse_args(argv)
    map_path = os.environ.get("ALTERIOM_HIL_BOARD_MAP")
    if not map_path:
        print("ALTERIOM_HIL_BOARD_MAP not set", file=sys.stderr)
        return 2
    board_map = BoardMap.load(map_path)
    targets = sorted({board.target for board in board_map})
    manifest = load_artifacts(args.artifacts)
    resolved_ref = manifest["painlessmesh_sha"]
    print(
        f"Flashing {len(board_map)} board(s) from {len(targets)} "
        f"immutable artifact(s) with painlessMesh@{resolved_ref}"
    )
    for board in board_map:
        artifact = manifest["targets"][board.target]
        print(
            f"==> {board.id} ({board.port}) <- {board.target} "
            f"sha256:{artifact['sha256'][:12]}"
        )
        # One retry, after a pause. esptool lost the serial stream once
        # mid-write on a UART-bridge board ("possible serial noise or
        # corruption") — the only such failure in dozens of suites — and a
        # single transient cost the run its twenty-five minutes before a
        # test had started. A second failure is a board that needs a
        # person, and still fails the run.
        for attempt in (1, 2):
            try:
                flash_esptool(
                    artifact["path"],
                    board,
                    offset=artifact["flash_offset"],
                )
                break
            except subprocess.CalledProcessError as exc:
                print(exc.stdout or "", file=sys.stderr)
                print(exc.stderr or "", file=sys.stderr)
                if attempt == 1:
                    print(
                        f"    flash of {board.id} failed once; retrying in 3 s",
                        file=sys.stderr,
                    )
                    time.sleep(3)
                    continue
                print(f"FLASH FAILED: {board.id}", file=sys.stderr)
                return 1
        print(f"    ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
