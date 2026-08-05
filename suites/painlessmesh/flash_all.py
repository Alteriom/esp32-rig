#!/usr/bin/env python3
"""Flash the HIL agent firmware onto every board in the map.

Usage (CI does exactly this):

    PAINLESSMESH_REF=<branch-or-sha> \
    ALTERIOM_HIL_BOARD_MAP=~/board-map.yaml \
    python3 flash_all.py

Boards are flashed sequentially (esptool contention on shared hubs makes
parallel flashing flaky on cheap rigs). Exits non-zero on the first
failure with the pio output, so CI cleanly distinguishes "flash failed"
from "test failed".
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hal"))
from alteriom_hil.board import BoardMap  # noqa: E402
from alteriom_hil.flash import flash_pio  # noqa: E402

FIRMWARE_DIR = Path(__file__).resolve().parent / "firmware"


def main() -> int:
    map_path = os.environ.get("ALTERIOM_HIL_BOARD_MAP")
    if not map_path:
        print("ALTERIOM_HIL_BOARD_MAP not set", file=sys.stderr)
        return 2
    ref = os.environ.get("PAINLESSMESH_REF") or "main"
    board_map = BoardMap.load(map_path)
    print(f"Flashing {len(board_map)} board(s) with painlessMesh@{ref}")
    for board in board_map:
        print(f"==> {board.id} ({board.port})")
        try:
            flash_pio(
                FIRMWARE_DIR,
                board,
                env="esp32dev",
                extra_env={"PAINLESSMESH_REF": ref},
            )
        except subprocess.CalledProcessError as exc:
            print(exc.stdout or "", file=sys.stderr)
            print(exc.stderr or "", file=sys.stderr)
            print(f"FLASH FAILED: {board.id}", file=sys.stderr)
            return 1
        print(f"    ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
