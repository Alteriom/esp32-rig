#!/usr/bin/env python3
"""Flash every board in the board map from a verified artifact directory.

Farm-owned and project-agnostic: it reads the schema-2 manifest contract in
`alteriom_hil.artifacts` and nothing else, so a consuming project supplies a
build script and gets flashing for free rather than shipping its own flasher.

`suites/painlessmesh/flash_all.py` predates this and stays as painlessMesh's
entry point -- it is the production release gate and is deliberately left
untouched. Both now read the same manifest through the same HAL loader; the
remaining difference is the CLI around it, and collapsing the two is a
follow-up worth doing on its own, not folded into a decoupling change.

The board map is the *scoped* one for this run: with per-board allocation a
job must flash only what it was given, and flashing "every board in the map"
is correct precisely because the map is already narrowed to the allocation.

Usage:
    python alteriom_hil.flash_artifacts --artifacts <dir> [--revision-key KEY]
    # board map from --board-map or ALTERIOM_HIL_BOARD_MAP
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from alteriom_hil.artifacts import load_artifacts
from alteriom_hil.board import BoardMap
from alteriom_hil.plugins import flasher_for


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts", type=Path, required=True,
        help="artifact directory containing manifest.json",
    )
    parser.add_argument(
        "--board-map", default=os.environ.get("ALTERIOM_HIL_BOARD_MAP"),
        help="scoped board map for this run (default: $ALTERIOM_HIL_BOARD_MAP)",
    )
    parser.add_argument(
        "--revision-key", default=None,
        help="manifest key naming the built revision, for the log line only",
    )
    args = parser.parse_args(argv)

    if not args.board_map:
        print("no board map: pass --board-map or set ALTERIOM_HIL_BOARD_MAP", file=sys.stderr)
        return 2

    board_map = BoardMap.load(args.board_map)
    if len(board_map) == 0:
        print("board map is empty; nothing to flash", file=sys.stderr)
        return 2

    manifest = load_artifacts(args.artifacts)
    revision = manifest.get(args.revision_key) if args.revision_key else None
    described = f" built from {revision}" if revision else ""
    print(f"Flashing {len(board_map)} board(s) from {args.artifacts}{described}")

    missing = sorted({b.target for b in board_map} - set(manifest.get("targets", {})))
    if missing:
        # Refuse before touching any board. Flashing half the allocation and
        # then failing leaves the rig in a state no one asked for.
        print(f"manifest has no artifact for target(s): {missing}", file=sys.stderr)
        return 1

    for board in board_map:
        artifact = manifest["targets"][board.target]
        print(
            f"==> {board.id} ({board.port}) <- {board.target} "
            f"sha256:{artifact['sha256'][:12]}"
        )
        # One retry, after a pause. esptool has lost the serial stream
        # mid-write on a UART-bridge board; a single transient should not cost
        # the run its rig time. A second failure is a board that needs a
        # person, and still fails the run.
        # The flasher the board's family names (alteriom_hil.plugins): esptool
        # for every ESP family, and whatever a plugin brings for another chip.
        flash = flasher_for(board.target)
        for attempt in (1, 2):
            try:
                flash(
                    artifact["path"], board, offset=artifact.get("flash_offset", "0x0")
                )
                break
            except subprocess.CalledProcessError as exc:
                print(exc.stdout or "", file=sys.stderr)
                print(exc.stderr or "", file=sys.stderr)
                if attempt == 1:
                    print(f"retrying {board.id} once after a transient flash failure", file=sys.stderr)
                    time.sleep(2)
                    continue
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
