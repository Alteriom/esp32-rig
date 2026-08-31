#!/usr/bin/env python3
"""Build each MCU artifact once, then flash it onto every matching board.

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
from alteriom_hil.flash import flash_esptool  # noqa: E402

try:  # script execution and package import use different module roots
    from .build_artifacts import build_artifacts, load_artifacts  # type: ignore
except ImportError:
    from build_artifacts import build_artifacts, load_artifacts  # noqa: E402

DEFAULT_ARTIFACT_DIR = Path(
    os.environ.get("ALTERIOM_HIL_ARTIFACT_DIR", "hil-firmware")
)


def main() -> int:
    map_path = os.environ.get("ALTERIOM_HIL_BOARD_MAP")
    if not map_path:
        print("ALTERIOM_HIL_BOARD_MAP not set", file=sys.stderr)
        return 2
    ref = os.environ.get("PAINLESSMESH_REF") or "main"
    board_map = BoardMap.load(map_path)
    targets = sorted({board.target for board in board_map})
    build_artifacts(ref, DEFAULT_ARTIFACT_DIR, targets)
    manifest = load_artifacts(DEFAULT_ARTIFACT_DIR)
    print(
        f"Flashing {len(board_map)} board(s) from {len(targets)} "
        f"immutable artifact(s) with painlessMesh@{ref}"
    )
    for board in board_map:
        artifact = manifest["targets"][board.target]
        print(
            f"==> {board.id} ({board.port}) <- {board.target} "
            f"sha256:{artifact['sha256'][:12]}"
        )
        try:
            flash_esptool(
                artifact["path"],
                board,
                offset=artifact["flash_offset"],
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
