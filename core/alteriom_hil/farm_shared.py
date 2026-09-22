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
import threading
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
# first and either works; on the Ubuntu image one rig runs it is a directory of
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


# What a dispatch may put in a run's environment, and what it may not. The
# base checks it when a run is submitted -- on a portal as much as on a rig,
# and a portal has no rig's half to ask -- so the rule lives here with the
# rest of what both halves say the same way.
# Names in that namespace a dispatch still may not set, because the rig or
# the farm does. The dispatch's env is laid over the service's own environment,
# so without this a run could point the suite at another Wi-Fi password file,
# another gateway endpoint, or a provider link it chose -- the farm re-applies
# only its per-run values after the dispatch's, not the rig's. What the
# runtime environment can hold is derived from alteriom_hil.hil_config; these are
# what the service sets per run or reads from its own environment.
FARM_OWNED_ENV_KEYS = frozenset({
    "ALTERIOM_HIL_MODE", "ALTERIOM_HIL_BOARD_MAP", "ALTERIOM_HIL_ARTIFACT_DIR",
    "ALTERIOM_HIL_LOG_DIR", "ALTERIOM_HIL_RUN_LOG", "ALTERIOM_HIL_SUITE_TIMEOUT",
    "ALTERIOM_HIL_CONFIG", "ALTERIOM_HIL_HOME", "ALTERIOM_HIL_UPDATE_DIR",
    "ALTERIOM_HIL_VERSION_FILE", "ALTERIOM_HIL_SERVICE_UNIT", "ALTERIOM_HIL_GATEWAY_PROBE_UNIT",
})


# Every provider's settings are the rig's (docs/providers.md).
RIG_OWNED_ENV_PREFIXES = ("ALTERIOM_HIL_CALLMEBOT_",)


# What a dispatch says about the run that the rig's providers act on: a
# release build is the only run a rig with `send: release` spends a real
# message on. The one value it may take.
RUN_KIND_ENV_KEY = "ALTERIOM_HIL_RUN_KIND"


RUN_KINDS = ("release",)


def _runtime_env_keys() -> frozenset:
    """The names alteriom_hil.hil_config can write into the runtime environment."""
    cached = globals().get("_RUNTIME_ENV_KEYS")
    if cached is not None:
        return cached
    from alteriom_hil import hil_config

    keys = frozenset(hil_config.runtime_env_keys())
    globals()["_RUNTIME_ENV_KEYS"] = keys
    return keys


def refused_suite_env(key: str, value: str) -> str | None:
    """Why a dispatch may not set this name, or None when it may."""
    if key == RUN_KIND_ENV_KEY:
        return None if value in RUN_KINDS else f"{key} may only be {' or '.join(RUN_KINDS)}"
    if key.endswith("_FILE"):
        return f"{key} names a file on the rig; the rig sets it"
    if key.startswith(RIG_OWNED_ENV_PREFIXES):
        return f"{key} is a provider setting; the rig sets it"
    if key in FARM_OWNED_ENV_KEYS or key in _runtime_env_keys():
        return f"{key} is set by the rig, not by a dispatch"
    return None


# The JSON files beside the job store that several jobs may now write at once:
# chip details and board health. Each write is read-modify-replace.
_STATE_FILE_LOCK = threading.Lock()
