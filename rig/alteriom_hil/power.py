"""Per-board power control.

The MVP rig powers boards from a uhubctl-capable USB hub so tests can
hard-reset or cut a node (see ``docs/runbook.md`` — power-cycling is the
first rung of the wedged-board recovery ladder).

Power control is **optional**. A rig may lack a switchable hub, lack the
``uhubctl`` binary, or map boards without power coordinates. In every one
of those cases callers get a controller that reports ``available`` /
``supports()`` as ``False`` rather than blowing up mid-suite:

    def test_survives_power_cut(power, bank):
        board = ...
        if not power.supports(board):
            pytest.skip("rig has no switchable power for this board")
        power.cycle(board)

Use :func:`power_for` (or the ``power`` pytest fixture) to get the right
controller for a rig; construct the classes directly only in tests.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Iterable, Union

from .board import Board


class NoopPower:
    """Fallback for rigs with no switchable power. Every call is a no-op."""

    available = False

    def supports(self, board: Board) -> bool:
        return False

    def on(self, board: Board):  # pragma: no cover - trivial
        pass

    def off(self, board: Board):  # pragma: no cover - trivial
        pass

    def cycle(self, board: Board, off_seconds: float = 1.0):  # pragma: no cover
        pass


class UhubctlPower:
    """Drives `uhubctl -l <hub> -p <port> -a on|off`."""

    def __init__(self, uhubctl: str = "uhubctl"):
        self.uhubctl = uhubctl

    @property
    def available(self) -> bool:
        """True only if the uhubctl binary is actually present on PATH."""
        return shutil.which(self.uhubctl) is not None

    def supports(self, board: Board) -> bool:
        """True if this board has usable power coordinates on a usable rig."""
        return (
            self.available
            and bool(board.power_hub)
            and board.power_port is not None
        )

    def _run(self, board: Board, action: str):
        if not board.power_hub or board.power_port is None:
            raise ValueError(
                f"board {board.id} has no power_hub/power_port in the board map"
            )
        if not self.available:
            raise RuntimeError(
                f"uhubctl ({self.uhubctl!r}) is not installed — cannot power "
                f"{action} board {board.id}. Install it (`apt install uhubctl`) "
                "or omit power_hub/power_port from the board map to opt out."
            )
        subprocess.run(
            [
                self.uhubctl,
                "-l",
                board.power_hub,
                "-p",
                str(board.power_port),
                "-a",
                action,
            ],
            check=True,
            capture_output=True,
        )

    def on(self, board: Board):
        self._run(board, "on")

    def off(self, board: Board):
        self._run(board, "off")

    def cycle(self, board: Board, off_seconds: float = 1.0):
        self.off(board)
        time.sleep(off_seconds)
        self.on(board)


def power_for(
    boards: Iterable[Board], uhubctl: str = "uhubctl"
) -> Union[UhubctlPower, NoopPower]:
    """Pick the right power controller for a rig.

    Returns :class:`UhubctlPower` only when the binary exists *and* at least
    one board carries power coordinates; otherwise :class:`NoopPower`, so
    power-dependent tests skip cleanly instead of erroring.
    """
    controller = UhubctlPower(uhubctl=uhubctl)
    if not controller.available:
        return NoopPower()
    if not any(controller.supports(b) for b in boards):
        return NoopPower()
    return controller
