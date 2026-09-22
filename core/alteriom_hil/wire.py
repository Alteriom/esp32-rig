"""The wire between a portal and the rigs that work for it.

What both ends must agree on and neither owns: how long a worker may be
quiet before its boards leave the pool, how long a lease may go unstarted,
how big a log chunk or a release may be, what states an update passes
through, and the digest an agent's source is known by.

It is core because both halves read it. A portal times its workers out by
these; a rig heartbeats and takes leases by them; the base manager reports
them. Put them in either half and the other imports that half to speak to
it (docs/public-release-plan.md, step 12).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath


WORKER_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}\Z")


WORKER_KINDS = ("hardware", "simulation")


# A worker that has not been heard from for this long has its boards taken out
# of the pool; for twice as long, its running jobs are failed as lost.
WORKER_STALE_SECONDS = 90


WORKER_LOST_SECONDS = 180


# A job leased and not started -- no stage reported -- in this long goes back
# to the queue: the worker died between taking it and running it.
LEASE_ACK_SECONDS = 90


LEASE_WAIT_SECONDS = 25


HEARTBEAT_SECONDS = 15


MAX_EVIDENCE_BYTES = 512 * 1024 * 1024


MAX_LOG_CHUNK_BYTES = 256 * 1024


HISTORY_KINDS = ("inventory", "suite", "build")


HISTORY_STATUSES = ("passed", "failed", "cancelled")


MAX_RELEASE_BYTES = 64 * 1024 * 1024


RELEASES_KEPT = 10


# Where a node is with the portal's current release (runner/farm_node.py).
# While pending, staged or installing, the portal gives it no work: it is
# finishing what it has, or about to restart.
UPDATE_STATES = ("pending", "staged", "installing", "installed", "failed")


UPDATING_STATES = ("pending", "staged", "installing")


def agent_digest(files) -> str:
    """The HIL agent digest over (path relative to the firmware directory,
    bytes) pairs -- the digest build_artifacts.py stamps into a manifest as
    hil_agent_sha, whether the files come from a checkout or a release."""
    digest = hashlib.sha256()
    for relative, data in sorted(files, key=lambda item: PurePosixPath(item[0]).parts):
        if ".pio" in PurePosixPath(relative).parts:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()
