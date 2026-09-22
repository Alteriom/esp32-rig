"""What both halves of the farm manager say the same way.

The errors a request is refused with, the shapes a board id and a commit
must have, the families a farm knows, the profile a request means when it
names none, and the rig lock -- taken shared by the dispatcher and exclusively
by a run, so read through this module (`farm_shared.RIG_LOCK_PATH`) and
changed in one place by a test. A rig and a portal each raise and check these, so neither
half's module is the place for them (docs/public-release-plan.md, step 8).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .board import SUPPORTED_TARGETS


BOARD_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}\Z")


SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")


TARGETS = SUPPORTED_TARGETS


# The consumer a request means when it names none. painlessMesh predates
# profiles and every existing caller -- its CI, the watcher, the dashboard --
# submits without a profile field, so the default has to stay what they expect.
DEFAULT_PROFILE = "painlessmesh"


class RigBusyError(RuntimeError):
    """A job holds the rig, so a read that needs a serial port cannot run now."""


class ElsewhereError(RigBusyError):
    """What was asked is done by another part of the farm: a portal runs no
    hardware, and a node takes its runs from its portal."""


class PipelineError(RuntimeError):
    def __init__(self, stage: str, summary: str, detail: str | None = None,
                 result: dict | None = None):
        super().__init__(summary)
        self.stage = stage
        self.summary = summary
        self.detail = detail or summary
        # Evidence the stage gathered that outlives its failure. A canary run
        # records a verdict per board and then fails *because* of them, and
        # the result dict carrying those verdicts is only returned when a run
        # succeeds -- so the deploy's gate, which reads them, was told "no
        # per-board verdicts" by exactly the runs it exists to judge.
        self.result = result or {}


# /run/lock, not /var/lock. On Raspberry Pi OS the second is a symlink to the
# first and either works; on the Ubuntu image rig02 runs it is a directory of
# its own, which `ProtectSystem=strict` makes read-only inside the unit -- so
# the service answered "[Errno 30] Read-only file system" for a lock the rest
# of the farm was taking somewhere else entirely (2026-09-16). The units'
# ReadWritePaths and the tmpfiles rule both name /run/lock; this is the same
# path the admin CLI has always used.
RIG_LOCK_PATH = Path(os.environ.get("ALTERIOM_HIL_RIG_LOCK", "/run/lock/alteriom-hil.lock"))


def board_lock_path(board_id: str) -> Path:
    """One board's own lock, beside the rig lock.

    A job that shares the rig takes the rig lock shared and each of its boards'
    locks exclusively, so anything else that opens that board's serial port --
    a chip-details probe -- waits for it or says the board is busy. A deploy
    or a discovery still takes the rig lock exclusively and waits for all of
    them. Board ids are filename-safe by BOARD_ID_PATTERN.
    """
    return RIG_LOCK_PATH.with_name(f"alteriom-hil-board-{board_id}.lock")
