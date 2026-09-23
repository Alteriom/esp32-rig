"""The farm service: what every farm is, and how it answers.

The queue and its dispatcher, the job store, the profiles, health and
quarantine, the artifact store, retention, statistics and storage
(`BaseManager`), and the HTTP surface over them (`make_handler`).

It is core because a portal holds it with no board in reach and a rig holds
it with no portal beside it. It imports neither half: the rig's is
`alteriom_hil.rig_manager`, the portal's `alteriom_hil.portal_manager`, and
what composes a mode's class from the base and the halves it has is the
launcher (`alteriom_hil.launcher`), which is the one thing that knows both
(docs/public-release-plan.md, step 12c).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import hmac
import http.cookies
import io
import json
import math
import os
import queue
import re
import secrets
import shutil
import tarfile
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import uuid

import yaml
from dataclasses import asdict, replace as dataclass_replace
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

from alteriom_hil import allocation, farm_shared, rig_setup, artifact_store
from alteriom_hil import signin as farm_signin
from alteriom_hil.api_keys import NAME_PATTERN as KEY_NAME_PATTERN
from alteriom_hil.api_keys import FARM_KEY_NAME, KeyReadError, namespace_lock
from html import escape as html_escape
from alteriom_hil.api_keys import Identity, KeyStore, allowed, keys_path_for, public_route, required_role
from typing import NamedTuple

from alteriom_hil.api_keys import allowed as role_allows
from alteriom_hil.api_keys import half_route
from alteriom_hil import backup as farm_backup
from alteriom_hil import notify as notify_module
from alteriom_hil.notify import Notification, Notifier
from alteriom_hil import providers as farm_providers
from alteriom_hil import webhooks as farm_webhooks
from alteriom_hil.providers import Redactor, budget_used, scrub_tree
from alteriom_hil.artifacts import MAX_BUNDLE_BYTES, extract_bundle, load_artifacts
from alteriom_hil.board import SUPPORTED_TARGETS, Board
from alteriom_hil.profiles import ProfileError, load_profiles
from alteriom_hil.instrument_registry import instruments_path_for, load_instruments, wired_to
from alteriom_hil.board_registry import (
    load_inventory_snapshot,
    load_registry,
    normalize_mac,
    reconcile,
    write_active_map,
    write_inventory_snapshot,
    write_registry,
)
# The job store is core: what both a rig and a portal keep. Its names stay
# importable from here, where every caller and test has found them.
from alteriom_hil.jobstore import (  # noqa: F401 -- re-exported
    EVENT_KEEP,
    EVENT_PRUNE_EVERY,
    MAX_PAGE_OFFSET,
    SEALED_PLACEHOLDER,
    SIGNIN_CODE_ATTEMPTS,
    WEBHOOK_DELIVERIES_KEPT,
    JobCancelled,
    JobStore,
    _elapsed,
    max_runs,
    utcnow,
)
# What both halves say the same way, and the wire they say it over. Both are
# core and both are taken from core, so that a farm which has only one half
# still has them: these were imported through the portal's half only because
# that is where they were written.
from alteriom_hil.farm_shared import (  # noqa: F401,E402 -- re-exported
    _STATE_FILE_LOCK,
    refused_suite_env,
    BOARD_ID_PATTERN,
    DEFAULT_PROFILE,
    ElsewhereError,
    PipelineError,
    RigBusyError,
    SHA_PATTERN,
    TARGETS,
)
from alteriom_hil.wire import (  # noqa: F401,E402 -- re-exported
    agent_digest,
    HEARTBEAT_SECONDS,
    HISTORY_KINDS,
    HISTORY_STATUSES,
    LEASE_ACK_SECONDS,
    LEASE_WAIT_SECONDS,
    MAX_EVIDENCE_BYTES,
    MAX_LOG_CHUNK_BYTES,
    MAX_RELEASE_BYTES,
    RELEASES_KEPT,
    UPDATE_STATES,
    UPDATING_STATES,
    WORKER_KINDS,
    WORKER_LOST_SECONDS,
    WORKER_NAME_PATTERN,
    WORKER_STALE_SECONDS,
)


# A healthy painlessMesh suite takes about fourteen minutes on the rig; a
# failing failover test adds seven or more of waits and restores. The limit
# exists so a wedged fixture cannot hold the farm, not to cut a failing run
# short of the evidence it was about to write.
DEFAULT_SUITE_TIMEOUT_SECONDS = 1800
REF_PATTERN = re.compile(r"(?!-)[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
# A dispatch may hand the suite settings of its own (`env` in the request):
# names in the suite's namespace, values a shell would find boring.
SUITE_ENV_KEY = re.compile(r"ALTERIOM_HIL_[A-Z0-9_]{1,40}\Z")
SUITE_ENV_VALUE = re.compile(r"[A-Za-z0-9._:/,-]{1,100}\Z")
# A partial run names test files or tests of the suite, and may add a pytest
# keyword expression. Both are bounded and shell-free by construction.
# The directory itself now comes from the running job's profile; this remains
# the default for callers that select tests without naming one.
TEST_PATTERN = re.compile(r"test_[a-z0-9_]{1,60}\.py(::test_[A-Za-z0-9_]{1,80})?\Z")
KEYWORD_PATTERN = re.compile(r"[A-Za-z0-9_ ().\[\]-]{1,200}\Z")
CAPABILITY_MARK = re.compile(r"@pytest\.mark\.capability\((.*?)\)", re.S)
# The farm's own choice of it, for what is asked without naming a profile
# (FarmManager.default_profile).
DEFAULT_PROFILE_ENV = "ALTERIOM_HIL_DEFAULT_PROFILE"
# The checkout this module lives in (core/alteriom_hil/ -> the root), used
# only to find `profiles/` when a manager was built without __init__ and has
# no repo of its own. A farm that was started properly reads its own.
FARM_REPO_ROOT = Path(__file__).resolve().parents[2]
# Sign-in codes one caller may ask for, and in how long; the session
# cookie's name, and how long a session lasts.
SIGNIN_REQUESTS_ALLOWED = 5
SIGNIN_STARTS_ALLOWED = 30
SIGNIN_REQUEST_WINDOW = 600.0
# The nonce cookie that ties a code to the browser that asked for it.
SIGNIN_COOKIE = "farm_signin"

# A GitHub code and state ride in the callback's query: request lines are
# written to the log, so these are scrubbed there before they are.
_SECRET_QUERY_IN_LOG = re.compile(r'([?&](?:code|state)=)[^&\s"]+')


class AddressThrottle:
    """How many times an address has done something lately, bounded.

    A refused attempt is not remembered: an address past its allowance that
    keeps asking would otherwise grow its own list for the whole window and
    have every later check walk it, and addresses that stopped asking would
    stay in the book for good -- a limiter that unauthenticated traffic can
    turn into memory and time. What is kept per address is at most the
    allowance, what is kept at all is only addresses seen inside the
    window, and the sweep of the book is paid for by the callers, once
    every so many calls.
    """

    def __init__(self, window: float = SIGNIN_REQUEST_WINDOW, sweep_every: int = 256):
        self.window = window
        self.sweep_every = sweep_every
        self._book: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def too_many(self, address: str, allowed: int, now: float | None = None) -> bool:
        """Whether this attempt is one too many -- and if not, count it."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._calls += 1
            if self._calls % self.sweep_every == 0:
                self._sweep(now)
            recent = [at for at in self._book.get(address, ()) if now - at < self.window]
            if len(recent) >= allowed:
                # Refused, and not remembered: the list stays at the
                # allowance however long the caller keeps trying.
                self._book[address] = recent[-allowed:] if allowed > 0 else []
                return True
            recent.append(now)
            self._book[address] = recent
            return False

    def _sweep(self, now: float) -> None:
        for address in [key for key, times in self._book.items()
                        if not times or now - times[-1] >= self.window]:
            del self._book[address]

    def tracked(self) -> int:
        with self._lock:
            return len(self._book)
SESSION_COOKIE = "farm_session"
SESSION_DAYS = 30
OAUTH_COOKIE = "farm_oauth"
# How this process takes part in the farm (docs/portal-plan.md).
#   standalone -- the queue and the hardware in one process, as the farm began
#   portal     -- the queue, the API and the UI; no hardware; workers lease work
#   node       -- the hardware; no queue of its own; leases work from a portal
MODES = ("standalone", "portal", "node")
# A farm that joins a portal brings its history (alteriom_hil.farm_node
# export-history): one job's log, or one bundle, per request.
MAX_HISTORY_LOG_BYTES = 64 * 1024 * 1024
# A release of this repository as a node installs it: a git bundle of the
# whole history at one commit. The farm's is a few megabytes.
# A worker's routine calls -- hello, heartbeat and lease polls, a run's stages
# and log, the history it brings -- are the protocol at work, not changes
# anyone made. Recorded, they buried every real change: six thousand rows on
# the portal's first day, a heartbeat every fifteen seconds.
ROUTINE_WORKER_CALL = re.compile(
    r"/api/v1/(?:workers/[a-z0-9][a-z0-9._-]{0,31}/(?:hello|heartbeat|lease|history/.+)"
    r"|jobs/[0-9a-f]{32}/(?:stages|log))"
)
# The script a rig joining a portal runs. It is the farm's own file, under
# the checkout the portal serves from -- `--repo`, which is /app in the
# image -- and not beside this module: the service is an installed package
# now and the script is not part of it (docs/public-release-plan.md, 12c).
JOIN_SCRIPT_PATH = ("rig", "join-rig.sh")


def join_script(repo) -> Path:
    return Path(repo).joinpath(*JOIN_SCRIPT_PATH)
# The public site: the pages anyone may read, at the root, and the dashboard
# beside them at /app. Each page is served through `_site_page` so its
# link-preview tags can carry an absolute URL; the files they load sit in
# web_root and are served as files. /world was the site's first address and
# redirects to its page, so a link somebody kept still lands.
SITE_PAGES = {
    "/": "site-home.html",
    "/rigs": "site-rigs.html",
    "/software": "site-software.html",
    "/how-it-works": "site-how.html",
}
WORLD_MOVED = {"/world": "/rigs", "/world/software": "/software"}
# The brand files a mail client may fetch from the site's first address
# (see the /world/brand route). Only these; nothing else lives on there.
WORLD_BRAND = frozenset({"favicon.svg", "mark.svg", "apple-touch-icon.png", "icon-192.png",
                         "icon-512.png", "logo-email.png", "og.png"})
ENROLL_FAILURE_LIMIT = 10
ENROLL_FAILURE_WINDOW = 10 * 60


class ArtifactProtected(RuntimeError):
    """A bundle that cannot be handed over or deleted right now: pinned,
    held by a job, or still being built."""


# A pin's note is shown on the dashboard and nowhere else; keep it to a line.
PIN_NOTE_PATTERN = re.compile(r"[^\x00-\x1f\x7f]{0,200}\Z")
# A confirmed prune names the bundles its preview listed; bounded like every
# other list a request may carry. A preview never lists more than this, so a
# confirmation never needs to send more -- and 1000 ids stay well inside the
# 64 KB a request body may be.
MAX_PRUNE_IDS = 1000
# The storage panel walks the bundle store and every run's evidence and log --
# thousands of files on the Pi's SD card -- so a measurement is kept this long
# unless an operator asks for a fresh one.
STORAGE_CACHE_SECONDS = 300
# A supplied bundle is the one request body that is not small: a six-family
# painlessMesh bundle is about 17 MB today, against the 64 KB every other
# route reads. The limit is this route's alone, and it bounds the archive as
# received and again as it expands, so a small archive cannot become a full
# disk. nginx's own client_max_body_size still applies to an upload from the
# dashboard; the CI client posts to 127.0.0.1 and never passes it.
# The size a bundle may expand to is defined with the manifest it bounds
# (alteriom_hil.artifacts); the routes here read it.
# What a producer says about itself when it hands a bundle over. The run id is
# GitHub's, numeric; the URL is only ever shown.
SUPPLY_RUN_PATTERN = re.compile(r"[0-9]{1,20}\Z")
# A GitHub login, or an app's bot account (`github-actions[bot]`). Who started
# the run a bundle or a job came from: for display, never for authority -- the
# farm's one token is what authorises anything.
ACTOR_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?\Z")
SUPPLY_URL_PATTERN = re.compile(r"https://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]{1,400}\Z")


# Written by install-health-service.sh next to the code it installed, so this
# reports the revision the service is running rather than whatever the clone
# has since become.
# What the health timer last wrote about this host.
STATUS_SNAPSHOT = Path(
    os.environ.get("ALTERIOM_HIL_STATUS_FILE", "/var/lib/alteriom-hil/status.json")
)
VERSION_FILE = Path(
    os.environ.get("ALTERIOM_HIL_VERSION_FILE", "/usr/local/lib/alteriom-hil/version.json")
)


def service_version() -> dict:
    """The deployed revision, or an explicit unknown.

    Read on every request rather than cached: a deploy rewrites this file and
    restarts the service, but a manual install does not, and a dashboard that
    keeps claiming the old revision until someone restarts it would be worse
    than one that costs a few microseconds.
    """
    unknown = {"version": "unknown", "short": "unknown", "commit": None}
    try:
        version = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return unknown
    if not isinstance(version, dict) or not version.get("version"):
        return unknown
    return version


def _https_repo(url: str) -> str | None:
    """A browsable URL for a git remote: https as it is, ssh rewritten."""
    url = url.strip()
    if not url:
        return None
    match = re.fullmatch(r"(?:git@|ssh://git@)([^:/]+)[:/](.+?)(?:\.git)?/?", url)
    if match:
        return f"https://{match.group(1)}/{match.group(2)}"
    if url.startswith("https://"):
        return re.sub(r"\.git/?$", "", url)
    return None


def repositories(repo: Path, profiles: dict | None = None) -> dict:
    """Where the code came from, for the links the dashboard shows.

    The farm's own remote is read from the checkout the service runs from;
    each consumer's remote comes from its profile, under `profiles`. Neither
    is guessed from a name.
    """
    farm = None
    try:
        found = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if found.returncode == 0:
            farm = _https_repo(found.stdout)
    except (OSError, subprocess.SubprocessError):
        farm = None
    by_profile = {
        name: _https_repo(profile.repo) for name, profile in (profiles or {}).items()
    }
    return {"farm": farm, "profiles": by_profile}


def _reject_missing_ref(ref: str, remote: str, project: str) -> str | None:
    """Refuse a ref the remote does not have, before it costs a run.

    A branch deleted when its pull request merged is the common case, and
    without this the farm accepts the run, occupies the rig, and fails in the
    build stage with a git traceback — which reads like a farm fault rather
    than a typo. Refusing at submit turns twenty wasted minutes into an
    immediate, specific message.

    Best effort on purpose: a commit SHA is not listable, and an unreachable
    remote must not stop the farm accepting work. Only a definite "the remote
    answered and does not have this" refuses.

    Returns the commit the ref names right now — the SHA itself, or the
    first one the remote listed — or None when the remote could not say.
    The job is pinned to it: a branch is a moving name, and the run that
    was asked for is the commit it pointed at when it was asked for.
    """
    if SHA_PATTERN.fullmatch(ref):
        return ref
    try:
        found = subprocess.run(
            # No --heads/--tags filter: `pull/383/head` is a ref the farm
            # legitimately accepts from painlessMesh CI, and those filters
            # hide refs/pull/*, so filtering would refuse a supported case.
            ["git", "ls-remote", remote, ref],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None  # cannot verify; let the build stage be the judge
    if found.returncode == 0 and not found.stdout.strip():
        raise ValueError(
            f"{project} has no branch or tag {ref!r}. If its pull request "
            f"merged, the branch was probably deleted — use the branch it "
            f"merged into, or a commit SHA."
        )
    if found.returncode != 0:
        return None
    first = found.stdout.split()[0]
    return first if SHA_PATTERN.fullmatch(first) else None


class NotThisHalf(ElsewhereError, AttributeError):
    """Asked of a farm that has not got the half that does it."""


class BaseManager:
    """What every farm is: the queue and its dispatcher, the job store, the
    profiles, health and quarantine, the artifact store, retention,
    statistics, storage. A rig adds the pipeline (RigMixin); a portal adds
    its rigs, accounts and the worker protocol (PortalMixin); a standalone
    farm is all three. The mode picks the class (`manager_for`).

    The few things the base reaches for that only one half has -- a webhook
    event, an imported bundle, the farm's own notifier -- are defined here as
    what a farm without that half does about them: nothing.

    Anything else of the other half's, asked of a farm that has not got it,
    is refused as it always was: an ElsewhereError, which the API answers
    with 409 and the reason. Registering a board on a portal, publishing a
    release on a node -- each method used to check the mode and raise this
    itself; a farm that does not carry the method says the same thing.
    """

    # The method names each half brings, and whether the portal's half is
    # installed at all. The base does not import either half -- a portal
    # holds it with no driver in reach, which is what makes it core -- so
    # whoever composes the two fills these in (`manager_for`).
    _portal_only: frozenset = frozenset()
    _rig_only: frozenset = frozenset()
    _portal_half: str | None = None

    def __getattr__(self, name: str):
        # Only reached when normal lookup fails, so it never shadows a method
        # a class has. An AttributeError too, so hasattr and getattr with a
        # default behave; an ElsewhereError first, so the API answers 409.
        if name in self._portal_only:
            raise NotThisHalf(f"this farm is not a portal; {name.strip('_')} is done on one")
        if name in self._rig_only:
            raise NotThisHalf(f"{name.strip('_')} is done on the worker that has the board, not on the portal")
        if self._portal_half is None:
            # The portal's half is not installed at all, so its method names
            # are not here to be listed. A rig on its own: the same answer,
            # from the same place, without pretending to know which name.
            raise NotThisHalf(f"this farm has no portal half installed; {name.strip('_')} is done on a portal")
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def emit(self, event) -> None:
        """Send one event to everything subscribed: a portal's (PortalMixin).
        A farm without one has no subscribers."""

    def _emit_run(self, job_id: str, status: str, job: dict, result: dict | None = None) -> None:
        """A run reaching a status somebody subscribed to.

        Every farm says it -- a run is queued, started and ends on a rig as
        much as on a portal -- and `emit` is what differs: a portal delivers
        it, a farm with no subscribers drops it. The payload is what a consumer needs to act without asking the farm a
        second question: which rig, which profile, what it was built from, and
        what the run concluded.
        """
        if status not in farm_webhooks.EVENTS["run"]:
            return
        request = job.get("request") or {}
        outcome = (result or {}).get("summary") or (result or {}).get("detail") or ""
        profile = request.get("profile") or job.get("kind") or "run"
        rig = job.get("worker")
        said = f"{profile} {status} on {rig}" if rig else f"{profile} {status}"
        self.emit(farm_webhooks.Event(
            "run", status, rig=rig, sender=request.get("submitted_by"),
            summary=(f"{said}: {outcome}" if outcome else said)[:200],
            payload={
                "job_id": job_id, "status": status, "profile": profile, "kind": job.get("kind"),
                "rig": rig, "ref": request.get("ref"), "branch": request.get("branch"),
                "commit": request.get("resolved_sha"), "boards": request.get("boards") or [],
                "result": result or {},
            },
        ))

    def imported_artifact(self, entry_id: str) -> bool:
        """History a node brought is a portal's; a rig has brought none."""
        return False

    def _build_farm_notifier(self) -> None:
        """The farm's own channel is a portal's; a rig notifies from its host
        configuration (self.notifiers)."""

    def _require_worker(self, name: str) -> dict:
        """A worker is a portal's; asked of a farm that is not one, the
        answer is the one every such read gives."""
        raise ElsewhereError("this farm is not a portal; workers connect to one")

    def mqtt_evidence_dir(self, job_id: str) -> Path:
        """Where a run's queue evidence lands: mqtt/ inside the one directory
        the suite is given (ALTERIOM_HIL_LOG_DIR, which is the run's serial/).
        The report's --gateway-matrix and the mqtt:<name> artifacts read
        from here. The first automatic run with a broker (2026-09-10) had
        its capture and its matrix row on disk and neither in the report or
        the consumer's evidence, because the service looked for them at the
        run root. A path, so both halves read evidence from it: a rig where
        the suite wrote it, a portal where a node's evidence landed."""
        return self.state / "runs" / job_id / "serial" / "mqtt"

    @property
    def profiles(self) -> dict:
        """The validation profiles this farm can run, keyed by name.

        __init__ loads these eagerly so a malformed document stops the service
        starting. Resolving lazily here as well keeps a manager built without
        __init__ working -- the tests do that deliberately, to exercise one
        method without standing up a whole farm.
        """
        found = self.__dict__.get("_profiles")
        if found is None:
            found = load_profiles(self.__dict__.get("repo") or FARM_REPO_ROOT)
            self.__dict__["_profiles"] = found
        return found

    @property
    def default_profile(self) -> str:
        """The profile a run is for when it names none, and the one the
        dashboard's run form opens on: this farm's to say
        (`ALTERIOM_HIL_DEFAULT_PROFILE`), because which project a rig mostly
        serves is a fact about the rig and not about the rig software.

        Only what is *asked* defaults through here. A stored job that records
        no profile is older than profiles, and what it ran is a fact about
        this farm's history that no setting changes: those reads keep
        DEFAULT_PROFILE.
        """
        named = os.environ.get(DEFAULT_PROFILE_ENV, "").strip()
        return named if named and named in self.profiles else DEFAULT_PROFILE

    def __init__(self, repo: Path, state: Path, registry: Path, board_map: Path, python: Path,
                 mode: str = "standalone"):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        self.mode = mode
        self.repo = repo.resolve()
        self.state = state.resolve()
        self.registry = registry.resolve()
        self.board_map = board_map.resolve()
        # Do not resolve this symlink: a venv's python commonly points at the
        # system interpreter binary, but executing through the venv path is
        # what selects its esptool site-packages.
        self.python = python.absolute()
        # Loaded once, at start: a malformed profile should stop the service
        # coming up, where an operator sees it, rather than failing the first
        # run that happens to name it. The property below is the lazy path.
        self._profiles = load_profiles(self.repo)
        named = os.environ.get(DEFAULT_PROFILE_ENV, "").strip()
        if named and named not in self._profiles:
            # Said at start, like a malformed profile: otherwise it is found
            # by the first run that names no profile, as a refusal that reads
            # like the caller's mistake.
            raise ValueError(
                f"{DEFAULT_PROFILE_ENV} names {named!r}, which is not a profile here "
                f"(profiles: {', '.join(sorted(self._profiles))})"
            )
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "logs").mkdir(exist_ok=True)
        self.store = JobStore(self.state / "farm.sqlite3")
        carried_over = self.store.recover_incomplete(keep_remote=mode == "portal")
        self.store.normalize_legacy_failures()
        self.pending: queue.Queue[tuple[str, str, dict]] = queue.Queue()
        # What each started job holds (alteriom_hil.allocation.Grant), by job
        # id, and why each queued job has not started. The dispatcher is the
        # only writer of both, under the dispatch lock.
        self._grants: dict[str, allocation.Grant] = {}
        self._reservation_lock = threading.Lock()
        self._waiting: dict[str, str] = {}
        self._dispatch_lock = threading.Lock()
        self.max_runs = max_runs(os.environ)
        # Which job the current thread is running, for the child-process wait
        # that checks for a cancellation: several jobs can run at once now.
        self._job_local = threading.local()
        # Cancellation of the running job: the worker checks this between
        # waits on its child process. The id of the job it is running is
        # what lets a request address "the job", not "whatever runs next".
        self._cancel_lock = threading.Lock()
        self._cancel_requests: dict[str, tuple[str, str | None]] = {}
        self._current_job: str | None = None
        # How many links each recipient has been sent lately: a limiter the
        # refused cannot grow, keyed on the address, so one caller -- or many
        # -- cannot bury a person under mail from the farm's sender.
        self._signin_to = AddressThrottle()
        # Taken by a run choosing a bundle to reuse and by an operator deleting
        # one, so a delete can never land between "this bundle will do" and
        # the link that makes the run hold it.
        self._artifact_lock = threading.Lock()
        # The farm's channels are read, given an id and written back as a
        # whole. Two admins adding one at the same moment -- the API is a
        # ThreadingHTTPServer -- would choose the same id, write to the same
        # credential file, and each store a list without the other's channel.
        self._farm_notify_lock = threading.RLock()
        # An operator can hold the queue: the running job finishes, nothing
        # queued starts until the queue is resumed. Submissions still queue.
        self.paused = False
        self.paused_since: str | None = None
        # Why, when something other than an operator paused it: a farm-wide
        # canary failure. A queue that is paused for no stated reason is one
        # an operator resumes without knowing what it was protecting.
        self.paused_reason: str | None = None
        # Where the farm says it broke, when the host has asked it to
        # (notify: in the host configuration, exported by runtime_env).
        self.notifiers = Notifier.all_from_env(os.environ)
        # The farm's own channel, from the portal's store rather than the
        # environment: on a portal the configuration file is read-only.
        self.__dict__["farm_notifiers"] = {}
        try:
            self._build_farm_notifier()
        except Exception:  # a farm that cannot build it still runs
            pass
        self._retention_last: dict | None = None
        # A portal's side of the worker protocol: the jobs waiting to be
        # leased to each worker, when each was leased and first reported on,
        # and what each worker is asked to do at its next heartbeat.
        self._worker_cond = threading.Condition()
        self._outbox: dict[str, list[str]] = {}
        self._leased_at: dict[str, float] = {}
        self._acked: dict[str, float] = {}
        self._worker_commands: dict[str, dict] = {}
        if mode == "portal":
            for job in self.store.active():
                if job["status"] == "running" and job.get("worker") and job.get("grant"):
                    self._grants[job["id"]] = allocation.Grant.from_dict(job["grant"])
                    self._acked[job["id"]] = time.monotonic()
        for job in carried_over:
            self.pending.put((job["id"], job["kind"], job["request"]))
        self.worker = threading.Thread(target=self._worker, name="farm-worker", daemon=True)
        self.worker.start()

    @staticmethod
    def _initial_progress(kind: str, request: dict | None = None) -> list[dict]:
        """The stages a job will report, as it is created.

        Simulation is a parameter of the job, not a fixture of the pipeline.
        painlessMesh's CI runs a protocol simulator and a behavioural mesh
        simulator before it touches hardware and attaches their evidence to
        the request; the two stages exist on such a job because that evidence
        was supplied, and they are already decided when the job is created.
        A run submitted without it -- another consumer, or an operator from
        the dashboard -- has no such stages. It used to: every suite job
        opened with "HAL protocol simulation: skipped" and "Large-scale mesh
        simulation: skipped", which read as two gaps in every run of a product
        that has no simulator, and as the farm being painlessMesh's.
        """
        request = request or {}
        names = {
            "inventory": [("discover", "Discover hardware"), ("details", "Read chip details")],
            "suite": [
                # Still named `build`, which is what every run in the history
                # calls its first stage; it has never compiled since
                # artifact-first step 5, and does not now. It takes the bundle.
                ("build", "Firmware bundle"),
                ("discover", "Discover hardware"),
                ("flash", "Flash devices"),
                ("preflight", "Verify flashed devices"),
                ("test", "Run validation"),
                ("report", "Generate report"),
            ],
        }[kind]
        groups = {
            "protocol_sim": "software",
            "mesh_sim": "simulation",
            "build": "hardware",
            "discover": "hardware",
            "details": "hardware",
            "flash": "hardware",
            "preflight": "hardware",
            "test": "hardware",
            "report": "evidence",
        }
        progress = []
        simulation = request.get("simulation") or {}
        if kind == "suite" and simulation:
            # Validated at submit: both stages passed, or the request was
            # refused. Recorded here with the evidence's own words.
            for name, label in (
                ("protocol_sim", "Protocol simulation"),
                ("mesh_sim", "Behavioural mesh simulation"),
            ):
                evidence = simulation.get(name) or {}
                progress.append({
                    "name": name,
                    "label": label,
                    "group": groups[name],
                    "status": "passed",
                    "summary": evidence.get("summary", "Validated by hosted CI"),
                })
        for name, label in names:
            stage = {"name": name, "label": label, "status": "pending"}
            if name in groups:
                stage["group"] = groups[name]
            progress.append(stage)
        return progress

    def _event(self, source: str, kind: str, text: str, *, worker: str | None = None,
               level: str = "info", job_id: str | None = None, command_id: str | None = None) -> None:
        """One console line. `source` is who is speaking -- `farm` for this
        service, the rig's name for something a rig did -- so a reader can
        tell an instruction from what came back."""
        self.store.record_event(
            source, kind, text, worker=worker or (source if source != "farm" else None),
            level=level, job_id=job_id, command_id=command_id,
        )

    def console(self, after: int = 0, worker: str | None = None, limit: int = 200) -> dict:
        if worker:
            self._require_worker(worker)
        return self.store.events_since(after, worker, limit)

    def _stage(
        self, job_id: str, name: str, status: str, summary: str | None = None, bundle: str | None = None
    ):
        job = self.store.get(job_id)
        progress = job.get("progress", []) if job else []
        for stage in progress:
            if stage["name"] == name:
                stage["status"] = status
                if status == "running" and "started_at" not in stage:
                    stage["started_at"] = utcnow()
                if status in ("passed", "failed", "skipped"):
                    stage["finished_at"] = utcnow()
                if summary:
                    stage["summary"] = summary
                if bundle:
                    # The bundle this run flashes when it reused one, kept
                    # with the run from the moment it is chosen: a run that
                    # fails or is cancelled writes a result without it.
                    stage["bundle"] = bundle
                break
        self.store.update_progress(job_id, progress)
        stage = next((item for item in progress if item["name"] == name), None)
        if stage is not None:
            said = f"{name}: {status}"
            self._event(
                "farm", "stage", f"{said} -- {summary}" if summary else said,
                worker=(self.store.get(job_id) or {}).get("worker"),
                level="error" if status == "failed" else "info", job_id=job_id,
            )
        hook = self.__dict__.get("_on_stage")
        if hook is not None:
            hook(job_id, progress)

    def submit(self, kind: str, request: dict, submitted_by: str | None = None) -> dict:
        if kind == "build":
            # The farm runs firmware; the project that ships it builds it. A
            # build job is refused by name rather than as an unknown kind, so
            # an old client learns what changed instead of what it got wrong.
            raise ValueError(
                "the farm does not build firmware: a project builds its own bundle in its "
                "CI, hands it over (POST /api/v1/artifacts), and a suite run flashes it"
            )
        if kind not in ("inventory", "suite"):
            raise ValueError(f"unsupported job kind: {kind}")
        mode = self.__dict__.get("mode", "standalone")
        if mode == "portal" and kind == "inventory":
            raise ElsewhereError("discovery runs on each worker; rediscover asks them to")
        if mode == "node" and kind == "suite" and submitted_by is not None:
            raise ElsewhereError("this farm takes its runs from its portal; submit the run there")
        if kind == "inventory" and self.rig_busy():
            # Discovery opens every serial port and rewrites the active board
            # map a running suite is flashing from. Queued behind the run it
            # would be harmless but pointless — the run's own discover stage
            # has just happened — and an operator who clicked it wants an
            # answer now, which is: not while the rig is working.
            raise RigBusyError("the rig is running a job; rediscover when it is idle")
        resolved = self._validate(kind, request)
        request = dict(request)
        if resolved:
            request["resolved_sha"] = resolved
        # The API key that asked, by name: which runs a user may cancel, and
        # who started one that has no GitHub actor. Set here, after
        # validation, so a request cannot claim to be somebody else's.
        if submitted_by:
            request["submitted_by"] = submitted_by
        if kind == "suite":
            # What this job is for, in the job itself: the profile name (the
            # default written in, so nothing downstream has to know what a
            # missing one means), the project label, and the repository the
            # ref and revision belong to. A run used to be described by a
            # revision and a result alone, and a revision is only a link when
            # its repository is known -- the dashboard linked every commit to
            # painlessMesh's.
            spec = self.profiles[request.get("profile", self.default_profile)]
            request["profile"] = spec.name
            request["project"] = spec.label
            request["repo"] = _https_repo(spec.repo)
        job_id = uuid.uuid4().hex
        log_path = self.state / "logs" / f"{job_id}.log"
        job = self.store.create(kind, request, log_path, self._initial_progress(kind, request))
        # Store owns the authoritative id, so use it rather than the temporary name.
        if job["id"] != job_id:
            log_path = self.state / "logs" / f"{job['id']}.log"
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET log_path=? WHERE id=?", (str(log_path), job["id"]))
        self._supersede(job["id"], kind, request)
        described = "discovery" if kind == "inventory" else (request.get("project") or kind)
        self._event(
            "farm", "job", f"queued {described} ({job['id'][:8]})",
            job_id=job["id"],
        )
        self.pending.put((job["id"], kind, request))
        queued = self.store.get(job["id"])
        self._emit_run(job["id"], "queued", queued or job)
        return queued

    def _supersede(self, job_id: str, kind: str, request: dict) -> list[str]:
        """Cancel the jobs this one makes pointless: same ref, older commit.

        A push to a branch under validation used to leave the previous
        commit's run queued, or running, ahead of the new one — twenty to
        forty minutes of rig time spent on a commit nobody is waiting for.
        The new job is the same request for the same branch; the older
        commit's runs are superseded, the queued ones at once and the running
        one through the same interrupt the safety limit uses, so it still
        leaves its evidence.

        Keyed on the commit, not the name: three submissions of one branch at
        one commit — a stability sweep — are three runs wanted, and stay.
        A request that says ``"supersede": false`` never cancels anything.
        """
        sha = request.get("resolved_sha")
        if not sha or request.get("supersede", True) is False or kind == "inventory":
            return []
        ref = request.get("ref", "main")
        profile = request.get("profile", DEFAULT_PROFILE)
        cancelled = []
        for job in self.store.active():
            if job["id"] == job_id or job["kind"] != kind:
                continue
            old = job["request"]
            if old.get("ref", "main") != ref or old.get("profile", DEFAULT_PROFILE) != profile:
                continue
            old_sha = old.get("resolved_sha")
            if not old_sha or old_sha == sha:
                continue
            self.cancel(
                job["id"],
                f"Superseded: {ref} moved on to {sha[:12]}",
                f"Job {job_id} validates {ref} at {sha}; this run was for {old_sha}.",
                superseded_by=job_id,
            )
            cancelled.append(job["id"])
        return cancelled

    def cancel(
        self, job_id: str, summary: str, detail: str | None = None, superseded_by: str | None = None
    ) -> dict:
        """Cancel a queued job now, or ask its thread to stop a running one.

        A running job is interrupted, not killed: pytest tears its fixtures
        down on SIGINT, which is where the boards' serial logs are written
        and the gateway is put back, and a cancelled run is still a run whose
        evidence someone may want.
        """
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] == "running":
            with self._cancel_lock:
                self._cancel_requests[job_id] = (summary, detail)
            if self.__dict__.get("mode") == "portal" and self._unlease(job_id):
                # Leased and not yet taken: nobody is running it to interrupt.
                with self._cancel_lock:
                    self._cancel_requests.pop(job_id, None)
                self.store.requeue(job_id)
                job = self.store.get(job_id)
            else:
                return self.store.get(job_id)
        if job["status"] != "queued":
            raise ValueError(f"job {job_id} is {job['status']}; only queued or running jobs can be cancelled")
        for stage in job["progress"]:
            if stage["status"] in ("pending", "running"):
                stage["status"] = "skipped"
        self.store.update_progress(job_id, job["progress"])
        result = {"summary": summary, "detail": detail, "cancelled": True}
        if superseded_by:
            result["superseded_by"] = superseded_by
        self.store.update(job_id, "cancelled", result)
        self._emit_run(job_id, "cancelled", self.store.get(job_id) or {}, result)
        # A job queued ahead of others may have been holding them back.
        pending = self.__dict__.get("pending")
        if pending is not None:
            pending.put(("cancelled", job_id, {}))
        return self.store.get(job_id)

    def _cancel_requested(self, job_id: str | None) -> tuple[str, str | None] | None:
        if job_id is None:
            return None
        with self._cancel_lock:
            return self._cancel_requests.get(job_id)

    # ---- queue control ------------------------------------------------------

    def rig_busy(self) -> bool:
        """Whether a build or suite is running or waiting to."""
        return any(job["kind"] in ("build", "suite") for job in self.store.active())

    def _link_notifier(self):
        """Whichever channel can build a link into the dashboard. They all
        share the public host, so the first will do -- and a host with no
        channel at all still puts no link in a message."""
        channels = self.__dict__.get("notifiers") or []
        return channels[0] if channels else None

    def _notify(self, note: Notification) -> None:
        """Down every channel this host has. It held one; a rig can be told to
        say things in several places now, and each decides for itself whether
        it wants this event."""
        for notifier in self.__dict__.get("notifiers") or []:
            notifier.send_later(note)

    def pause(self, reason: str | None = None) -> dict:
        if not self.paused:
            self.paused = True
            self.paused_since = utcnow()
            self.paused_reason = reason
            # Only the farm pausing itself is news: an operator who paused it
            # knows. Once per pause, since this only runs on the transition.
            if reason:
                notifier = self._link_notifier()
                self._notify(Notification(
                    "queue_paused", "The farm paused its own queue",
                    f"{reason}. Nothing queued starts until an operator resumes it.",
                    link=notifier.link("#overview") if notifier else None,
                ))
            self.emit(farm_webhooks.Event(
                "queue", "paused", summary="The farm's queue is paused"
                + (f": {reason}" if reason else " by an operator"),
                payload={"reason": reason, "self_paused": bool(reason)},
            ))
        return self.queue_state()

    def resume(self) -> dict:
        if self.paused:
            self.paused = False
            self.paused_since = None
            self.paused_reason = None
            # Wake the worker in case it is holding a token it could not use.
            self.pending.put(("resume", "queue", {}))
            self.emit(farm_webhooks.Event(
                "queue", "resumed", summary="The farm's queue is taking work again", payload={},
            ))
        return self.queue_state()

    def promote(self, job_id: str) -> dict:
        return self.store.promote(job_id)

    def queue_state(self, workers: set[str] | None = None,
                    submitted_by: str | None = None) -> dict:
        """What is running and what is waiting.

        `workers` cuts it to one caller's workspace: their runs, and why
        theirs are waiting. Whether the farm is paused stays whole, because
        a paused farm is why *their* run has not started -- a workspace that
        reported itself un-paused while nothing moved would be a lie by
        omission.

        This panel is the one that most needed `submitted_by` beside the rig
        names: everything it lists as queued has no worker yet, so scoping on
        the rig alone emptied it of exactly the runs it exists to explain.
        """
        # The same list twice until `workers` cuts one of them. The reasons
        # below are resolved against the uncut one, because a run this caller
        # may not see is still the run their own is waiting behind.
        everything = self.store.active()
        active = everything
        if workers is not None:
            active = [job for job in everything if self.store.mine(job, workers, submitted_by)]
        running = [job["id"] for job in active if job["status"] == "running"]
        queued = [job["id"] for job in active if job["status"] == "queued"]
        waiting = dict(self.__dict__.get("_waiting") or {})
        return {
            "paused": self.paused,
            "paused_since": self.paused_since,
            # Why the farm is paused is everybody's business -- it is why
            # their own run has not started -- but the reason the farm writes
            # when it pauses itself names the run that failed, and that run
            # may be one this caller's own detail route answers 404 for.
            "paused_reason": self._unnamed_blockers(self.paused_reason, everything,
                                                    workers, submitted_by),
            # The first running job, as before several could run; every one of
            # them in `running_jobs`.
            "running": running[0] if running else None,
            "running_jobs": running,
            "queued": queued,
            # Why each queued job has not started, in words: "waiting for
            # 1 x esp32-c3: in use by run 1a2b3c4d (Alteriom firmware)". The
            # keys were scoped and the sentences were not, so a reason named
            # the id and the project of the run in front -- a run whose own
            # detail route answers this caller 404.
            "waiting": {job_id: self._unnamed_blockers(waiting[job_id], everything,
                                                       workers, submitted_by)
                        for job_id in queued if job_id in waiting},
            "concurrency": self.__dict__.get("max_runs", 1),
        }

    # How the farm names a run in a sentence about itself: "run 1a2b3c4d
    # (Alteriom firmware)" in a waiting reason (alteriom_hil.allocation,
    # `_held` and `_short`), and a bare "run 1a2b3c4d" in the reason it
    # writes when it pauses itself. Both, or the second is a way round the
    # first.
    _NAMED_RUN = re.compile(r"run ([0-9a-f]{8})")

    def _unnamed_blockers(self, reason: str, active: list[dict],
                          workers: set[str] | None, submitted_by: str | None) -> str:
        """A waiting reason with other people's runs made anonymous.

        Why a run has not started is its submitter's business, and part of
        that answer is that something else is in the way. Which something
        is not: the sentence carries the blocking run's short id and its
        project label, and that run's own detail route answers this caller
        404. So the shape of the explanation stays -- behind a run, or
        holding a resource -- and the run stops being named.
        """
        if not reason or workers is None:
            return reason
        known = {job["id"][:8]: job for job in active}

        def theirs(short_id: str) -> bool:
            # `active` covers the runs in flight, which is where a waiting
            # reason points. A reason the farm wrote when it paused itself
            # can name a run that has since finished, so fall back to the
            # history -- and treat an id that names no one job, or more than
            # one, as not this caller's.
            job = known.get(short_id) or self.store.by_prefix(short_id)
            return job is not None and self.store.mine(job, workers, submitted_by)

        out, at = [], 0
        for match in self._NAMED_RUN.finditer(reason):
            if match.start() < at:
                continue
            end = self._end_of_run_name(reason, match.end())
            out.append(reason[at:match.start()])
            out.append(reason[match.start():end] if theirs(match.group(1)) else "another run")
            at = end
        out.append(reason[at:])
        return "".join(out)

    @staticmethod
    def _end_of_run_name(reason: str, after_id: int) -> int:
        """Where the name of a run ends, given where its id ends.

        A label follows in brackets and a label may itself contain brackets
        -- "run deadbeef (Acme (private) nightly)" -- so counting them is the
        only way to know where the name stops. A pattern that ended at the
        first `)` left the tail of somebody's project sitting in the
        sentence.

        A label that never closes ends the sentence. Nothing validates a
        profile label against brackets, so `(Acme (private)` is a label
        somebody can have, and returning "only the id goes" here left the
        rest of it in place -- failing open on exactly the malformed input
        that would be chosen deliberately. Over-redacting a tail the farm
        wrote costs a clause of explanation; under-redacting it hands over a
        name.
        """
        if not reason.startswith(" (", after_id):
            return after_id
        depth = 0
        for i in range(after_id + 1, len(reason)):
            if reason[i] == "(":
                depth += 1
            elif reason[i] == ")":
                depth -= 1
                if depth == 0:
                    return i + 1
        return len(reason)

    def _validate(self, kind: str, request: dict) -> str | None:
        allowed = {
            "inventory": set(),
            "suite": {"profile", "ref", "branch", "actor", "targets", "simulation", "supersede", "tests", "keyword", "reuse", "env", "artifact", "boards"},
        }.get(kind)
        if allowed is None:
            raise ValueError(f"unsupported job kind: {kind}")
        if kind == "suite":
            # A partial run: which test files or tests, and a pytest keyword
            # expression. Names are checked against the files in the repo,
            # never passed through a shell; a keyword is a bounded charset.
            tests = request.get("tests", [])
            if not isinstance(tests, list) or len(tests) > 50 or not all(
                isinstance(item, str) and TEST_PATTERN.fullmatch(item) for item in tests
            ):
                raise ValueError("tests must be a list of up to 50 test files or test ids, like test_soak_stability.py or test_ota_mesh.py::test_x")
            named = self.profiles.get(request.get("profile", self.default_profile))
            if named is not None and not named.runs_in_consumer_repo:
                # Only checkable for a suite this repository holds. A consumer
                # profile's suite arrives with the checkout, so the names are
                # verified when the run reaches pytest -- refusing here would
                # mean refusing every valid selection for those profiles.
                for item in tests:
                    if not (self.repo / named.suite_path / item.split("::")[0]).is_file():
                        raise ValueError(f"no such test file: {item.split('::')[0]}")
            if named is not None and named.has_test_command and (tests or request.get("keyword")):
                # tests and keyword are pytest's -k and node ids. A profile that
                # runs its own command has nothing to pass them to, and silently
                # running the whole suite in reply to a request for one test
                # would report a pass for a selection that never ran.
                raise ValueError(
                    f"profile {named.name} runs its own test command; selecting "
                    f"individual tests or a keyword is not supported for it"
                )
            keyword = request.get("keyword", "")
            if not isinstance(keyword, str) or (keyword and not KEYWORD_PATTERN.fullmatch(keyword)):
                raise ValueError("keyword must be a pytest -k expression of letters, digits, spaces, _ - . ( ) [ ] up to 200 characters")
            if not isinstance(request.get("reuse", True), bool):
                raise ValueError("reuse must be true or false")
            supplied = request.get("artifact")
            if supplied is not None and (
                not isinstance(supplied, str) or not artifact_store.BUNDLE_ID.fullmatch(supplied)
            ):
                raise ValueError("artifact must be the id of a bundle the farm holds")
        unknown = set(request) - allowed
        if unknown:
            raise ValueError(f"unknown request fields: {sorted(unknown)}")
        resolved = None
        if kind == "suite":
            profile = request.get("profile", self.default_profile)
            if profile not in self.profiles:
                raise ValueError(f"unsupported validation profile: {profile}")
            spec = self.profiles[profile]
            # Fill a missing ref from the profile before anything reads it, and
            # write it back into the request: every later stage -- supersede,
            # the pipeline, the stored job -- takes the ref from there, and
            # each defaulting to "main" on its own would ignore a profile whose
            # default branch is something else and validate the wrong code.
            ref = request.get("ref") or spec.default_ref
            request["ref"] = ref
            if not isinstance(ref, str) or not REF_PATTERN.fullmatch(ref):
                raise ValueError("ref must be a branch, tag, or commit without shell metacharacters")
            # The branch the ref was taken from, for display only. A
            # consumer's CI validates a commit, so the ref it sends is a SHA
            # -- and the dashboard's Branch column was blank for every run a
            # consumer ever sent. The checkout uses ref, never this.
            branch = request.get("branch")
            if branch is not None and (not isinstance(branch, str) or not REF_PATTERN.fullmatch(branch)):
                raise ValueError("branch must be a branch name without shell metacharacters")
            # Who started it, as GitHub names them. Display only, like the
            # branch: a run's page and the bundles it flashed say whose it was.
            actor = request.get("actor")
            if actor is not None and (not isinstance(actor, str) or not ACTOR_PATTERN.fullmatch(actor)):
                raise ValueError("actor must be a GitHub login")
            # Per-run settings for the suite, from the dispatch, as
            # environment: which family plays the gateway, a timeout. Only
            # the suite's own namespace and a bounded charset -- and never a
            # name the rig or the farm sets (refused_suite_env): these are
            # laid over the service's environment, where the rig's secrets'
            # paths and endpoints are, and only the farm's per-run values are
            # applied after them.
            extra_env = request.get("env")
            if extra_env is not None and (
                not isinstance(extra_env, dict)
                or len(extra_env) > 16
                or not all(
                    isinstance(key, str) and SUITE_ENV_KEY.fullmatch(key)
                    and isinstance(value, str) and SUITE_ENV_VALUE.fullmatch(value)
                    for key, value in extra_env.items()
                )
            ):
                raise ValueError(
                    "env must map up to 16 ALTERIOM_HIL_* names to values of up to 100 "
                    "letters, digits, dots, dashes, colons, slashes or commas"
                )
            for key, value in sorted((extra_env or {}).items()):
                refused = refused_suite_env(key, value)
                if refused:
                    raise ValueError(f"env cannot set {key}: {refused}")
            resolved = _reject_missing_ref(ref, spec.repo, spec.label)
            targets = request.get("targets", sorted(TARGETS))
            if not isinstance(targets, list) or not targets or not set(targets) <= TARGETS:
                raise ValueError(f"targets must be a non-empty subset of {sorted(TARGETS)}")
            if not isinstance(request.get("supersede", True), bool):
                raise ValueError("supersede must be true or false")
            # Which boards this run may touch. Until now the allocation came
            # from the profile alone -- the bank for an exclusive profile,
            # its `needs` for a shared one -- and that cannot express "this
            # board", which is the whole of a health check on one board.
            if kind == "suite" and request.get("boards") is not None:
                self._check_named_boards(request["boards"], request.get("profile", self.default_profile))
            # A run that names a bundle flashes that bundle: it must be for
            # this profile's library, at the commit the run asked for, built
            # against the agent this farm speaks to, and cover every family
            # the run wants. Checked here so a mismatch is a rejected request
            # rather than a failure deep into a run holding the rig.
            if kind == "suite" and request.get("artifact"):
                self._check_supplied_bundle(request["artifact"], spec, resolved, targets)
            self._check_images_exist(spec, request, resolved, targets)
        if kind == "suite" and request.get("simulation") is not None:
            self._validate_simulation(request["simulation"], spec.revision_key)
        return resolved

    def _check_images_exist(self, spec, request: dict, resolved: str | None,
                            targets: list[str]) -> None:
        """Refuse a run with no firmware to flash.

        The farm never builds, so a run has exactly two ways to get firmware:
        it names a bundle its project's CI built and handed over, or an
        earlier run left one for this commit that it can reuse. With neither
        it would queue, take the rig and fail with nothing to run -- so it is
        refused here, naming the workflow that builds and dispatches.

        The check can still be overtaken: a bundle found now may be pruned
        before the run reaches the rig. The firmware stage says so in that
        case; this is the refusal that saves the common mistake, not a
        promise about the future.
        """
        if request.get("artifact"):
            return
        if resolved and request.get("reuse", True) and self.reusable_artifacts(
            resolved, targets, spec.revision_key, spec.name
        ):
            return
        raise ValueError(
            f"the farm does not build firmware, and it holds no {spec.name} bundle "
            f"for {(resolved or request.get('ref') or 'that revision')[:12]}"
            f"{' covering ' + ', '.join(sorted(targets)) if targets else ''}. "
            f"Run {spec.supply_workflow or 'its build workflow'} in "
            f"{_https_repo(spec.supply_repo) if spec.supply_repo else 'its repository'}, "
            f"which builds the bundle and dispatches the run that flashes it."
        )

    def _check_named_boards(self, named: object, profile: str | None = None) -> list[str]:
        """The boards a request named, if the rig has them all right now.

        Refused at submit rather than in the run: a health check on a board
        that is not plugged in is a typo or a board that has gone, and both
        are answers an operator wants immediately -- not after the job has
        queued, taken the rig and flashed the boards it could find.
        """
        if not isinstance(named, list) or not named or len(named) > 64 or not all(
            isinstance(item, str) and BOARD_ID_PATTERN.fullmatch(item) for item in named
        ):
            raise ValueError("boards must be a list of up to 64 board ids")
        connected = {
            board["id"] for board in (self.inventory_snapshot().get("boards") or [])
            if board.get("id")
        }
        missing = sorted(set(named) - connected)
        if missing:
            raise ValueError(
                f"not connected: {', '.join(missing)}; the rig has "
                f"{', '.join(sorted(connected)) or 'no boards'}"
            )
        # A reserved board is on somebody's bench; a quarantined one is only
        # the canary's, since a clean check is what releases it.
        store = self.__dict__.get("store")
        holds = store.holds() if store is not None else {}
        for board_id in sorted(set(named)):
            hold = holds.get(board_id)
            if hold is None:
                continue
            if hold["state"] == "reserved" or profile != self.CANARY_PROFILE:
                raise ValueError(
                    f"{board_id} is {hold['state']}"
                    + (f": {hold['reason']}" if hold.get("reason") else "")
                    + ("; release it first" if hold["state"] == "reserved" else "; only a health check may use it")
                )
        # Order and duplicates are the caller's; the allocation is a set.
        return sorted(set(named))

    @staticmethod
    def _validate_simulation(evidence: object, revision_key: str) -> None:
        """`revision_key` is the profile's: the evidence says which commit the
        simulator tested under the same name the profile's bundles say which
        commit they were built from, and the two are compared at the
        firmware stage."""
        if not isinstance(evidence, dict):
            raise ValueError("simulation evidence must be an object")
        allowed = {"schema", "generated_at", revision_key, "protocol_sim", "mesh_sim"}
        if set(evidence) - allowed:
            raise ValueError("simulation evidence contains unknown fields")
        if evidence.get("schema") != 1 or not SHA_PATTERN.fullmatch(
            str(evidence.get(revision_key, ""))
        ):
            raise ValueError(f"simulation evidence schema or {revision_key} is invalid")
        for name in ("protocol_sim", "mesh_sim"):
            stage = evidence.get(name)
            if not isinstance(stage, dict) or stage.get("status") != "passed":
                raise ValueError(f"{name} evidence must have passed")
            if len(str(stage.get("summary", ""))) > 500:
                raise ValueError(f"{name} evidence summary is too long")
        mesh = evidence["mesh_sim"]
        if not SHA_PATTERN.fullmatch(str(mesh.get("simulator_sha", ""))):
            raise ValueError("simulator SHA is invalid")
        scenarios = mesh.get("scenarios")
        if not isinstance(scenarios, list) or not scenarios or len(scenarios) > 50:
            raise ValueError("mesh simulator scenarios must be a non-empty bounded list")

    def _worker(self):
        while True:
            # A token says something changed: a submission, a job ending, a
            # resume, a cancellation. Which jobs start is the allocator's
            # answer over the whole queue, by priority then age, so a
            # promotion or a cancellation made while the token waited is
            # honoured. A paused queue starts nothing; resuming sends a token.
            # It also looks again every half minute without one, so a board
            # that reappeared, or a decision a transient error interrupted, is
            # not left waiting for the next submission.
            try:
                self.pending.get(timeout=30)
                token = True
            except queue.Empty:
                token = False
            try:
                if self.__dict__.get("mode") == "portal":
                    self._sweep_workers()
                self._dispatch()
            except Exception as exc:  # the dispatcher must outlive any one bad decision
                print(f"farm-dispatch: {exc!r}", file=sys.stderr, flush=True)
            finally:
                if token:
                    self.pending.task_done()

    def _demand(self, job: dict) -> allocation.Demand:
        """What a queued job asks of the rig (alteriom_hil.allocation)."""
        kind, request = job["kind"], job["request"] or {}
        if kind != "suite":
            # Discovery opens every serial port; a build queued before the
            # farm stopped building fails at once, and takes nothing to do so.
            return allocation.Demand(
                job_id=job["id"], label="discovery" if kind == "inventory" else kind, kind=kind,
                whole_rig=True, holds_boards=False,
            )
        spec = self.profiles.get(request.get("profile", DEFAULT_PROFILE))
        if spec is None:
            return allocation.Demand(job_id=job["id"], label=request.get("profile") or kind, whole_rig=True)
        common = {
            "job_id": job["id"], "label": spec.label, "kind": kind,
            "concurrent": spec.concurrent, "resources": frozenset(spec.resources),
            "profile": spec.name,
        }
        if request.get("boards"):
            # "Check this board": exactly those, whatever the profile says.
            return allocation.Demand(**common, boards=tuple(sorted(set(request["boards"]))))
        if spec.exclusive:
            return allocation.Demand(**{**common, "concurrent": False}, whole_rig=True)
        return allocation.Demand(**common, needs=tuple(spec.needs))

    def api_routes(self) -> tuple:
        """What this half adds to the API, beyond the service's own routes.

        A tuple of `ApiRoute`. The base has none: a service with no half
        installed answers exactly what it has always answered.

        It exists because a half is a distribution of its own now, and this
        file is the core's: a portal that had to edit the service to add a
        route would wait on a release of the rig to ship it
        (docs/public-release-plan.md, step 16a).
        """
        return ()

    def _dispatch(self) -> list[str]:
        """Start every queued job the rig can take now; the ids started."""
        with self._dispatch_lock:
            queued = [job for job in self.store.active() if job["status"] == "queued"]
            with self._reservation_lock:
                running = list(self._grants.values())
            holds = self.store.holds()
            limits = profiles = None
            updating: dict[str, dict] = {}
            if self.__dict__.get("mode") == "portal":
                everyone = self.online_workers()
                # A worker on its way to the current release is finishing what
                # it runs, or about to restart: it is given nothing new.
                updating = {
                    worker["name"]: worker for worker in everyone
                    if (worker.get("update") or {}).get("state") in UPDATING_STATES or worker.get("drained")
                }
                online = [worker for worker in everyone if worker["name"] not in updating]
                if not online:
                    # A portal runs nothing itself: with no worker, nothing starts.
                    reason = "no worker is connected" if not updating else "waiting for " + ", ".join(
                        f"{name}, drained" if worker.get("drained") else
                        f"{name} to update to {str((worker.get('update') or {}).get('commit') or '')[:7]}"
                        for name, worker in sorted(updating.items())
                    )
                    self._waiting = {job["id"]: reason for job in queued}
                    return []
                limits = {worker["name"]: worker["max_runs"] for worker in online}
                profiles = {worker["name"]: frozenset(worker["profiles"]) for worker in online}
            decision = allocation.plan(
                [self._demand(job) for job in queued],
                [
                    {**board, "hold": holds.get(board["id"])}
                    for board in (self.inventory_snapshot().get("boards") or [])
                    if board.get("id") and board.get("worker") not in (updating if limits is not None else ())
                ],
                running,
                limit=self.max_runs,
                paused=self.paused,
                limits=limits,
                profiles=profiles,
            )
            self._waiting = decision.waiting
            by_id = {job["id"]: job for job in queued}
            started = []
            for grant in decision.start:
                job = by_id[grant.job_id]
                # Only a job still queued: a cancellation that landed after the
                # queue was read has already decided this one.
                claimed = self.store.claim(grant.job_id)
                if claimed:
                    self._emit_run(grant.job_id, "started", self.store.get(grant.job_id) or {})
                if not claimed:
                    continue
                with self._reservation_lock:
                    self._grants[grant.job_id] = allocation.with_since(grant, utcnow())
                if grant.worker is not None:
                    self._lease_out(job, grant)
                    started.append(grant.job_id)
                    continue
                if self.__dict__.get("mode") == "portal":
                    # Never run on the portal itself.
                    self._release(grant.job_id)
                    self.store.requeue(grant.job_id)
                    continue
                threading.Thread(
                    target=self._run_job, args=(job, grant),
                    name=f"farm-job-{grant.job_id[:8]}", daemon=True,
                ).start()
                started.append(grant.job_id)
            return started

    @contextlib.contextmanager
    def _hold(self, grant: allocation.Grant):
        """The locks a grant stands for, for as long as the job runs.

        Alone: the rig lock exclusively, as every job took it before. Shared:
        the rig lock shared -- so a deploy or a discovery, which take it
        exclusively, wait for every running job -- and each board's own lock
        exclusively, in id order so two jobs can never wait on each other.
        """
        with contextlib.ExitStack() as stack:
            rig = stack.enter_context(farm_shared.RIG_LOCK_PATH.open("w"))
            fcntl.flock(rig, fcntl.LOCK_SH if grant.shared else fcntl.LOCK_EX)
            if grant.shared:
                for board_id in sorted(grant.boards):
                    handle = stack.enter_context(farm_shared.board_lock_path(board_id).open("w"))
                    fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    # ---- the portal's side of the worker protocol -------------------------------
    # docs/portal-plan.md. A worker connects out: it says hello with what it
    # offers, heartbeats its inventory and what it runs, and long-polls for a
    # lease. The dispatcher's grants name the worker; starting a job on a
    # portal is putting it in that worker's outbox. What the worker reports --
    # stages, log, evidence, result -- lands where a local run would have put
    # it, so every page, statistic and report reads a remote run as it reads a
    # local one.

    # ---- events, to somebody else's software -----------------------------------
    # A notification is for a person. This is the other audience: a program
    # that acts on what the farm sees. Same facts, whole, signed, with an
    # identity a receiver can deduplicate on -- the shape Alteriom's webhook
    # connector already sends, so an existing consumer needs no teaching
    # (docs/webhooks.md, alteriom_hil.webhooks).

    WEBHOOK_SCOPES_HELP = "scope is 'farm' for the whole fleet, or the name of one rig"

    # ---- accounts: a person, signed in ---------------------------------------
    # A person becomes an account by GitHub or by an emailed link, and is the
    # same account either way; the account's handle is what owns rigs and
    # what the audit names. The farm's own people are `admin` by being named
    # in ALTERIOM_HIL_ADMINS. What this needs is read from the environment,
    # secrets as files where the farm's convention wants them
    # (docs/farm-service.md, "Accounts").
    SESSION_DAYS = SESSION_DAYS
    SIGNIN_CODE_MINUTES = 10
    OAUTH_STATE_MINUTES = 10

    # A session is shown by the first 12 characters of its digest. The digest
    # is a hash of the cookie and cannot be turned back into one, and a prefix
    # is enough to name a row for revoking without putting the whole thing on
    # a page. Sessions are few, so a collision is a mis-click, not a breach --
    # and the delete is scoped to the account either way.
    SESSION_REF = 12

    VISIBILITIES = ("private", "public", "shared")
    PUBLIC_VISIBILITIES = ("public", "shared")

    # ---- the world page: what anyone may see ------------------------------------
    WORLD_WINDOW_DAYS = 7

    # ---- the farm's own notifications --------------------------------------
    # A rig tells its owner about itself (docs/notifications.md). This is the
    # other audience: whoever looks after the farm, told the things no single
    # rig can say -- that one went quiet, that one is behind, that one joined.

    FARM_NOTIFY = "notify"

    def own_setup(self) -> dict:
        """What this host is set up to do, for a farm that has no portal to ask.

        The same rows a portal shows for one of its rigs, from this host's own
        configuration, health snapshot and inventory -- so a standalone farm
        and a rig read the same page about themselves.
        """
        try:
            config = self.configuration()
        except Exception:  # a host whose configuration cannot be read says so
            config = None
        # The health snapshot the timer writes, where the service's own
        # configuration says it is; an absent one leaves the rows that need it
        # unknown rather than guessed.
        try:
            health = json.loads(STATUS_SNAPSHOT.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            health = None
        inventory = self.inventory_snapshot()
        return {"setup": rig_setup.capabilities(config, health, inventory, None)}

    # The rig view: one shape for "this rig", served by a rig about itself
    # (GET /api/v1/view) and by a portal about each of its rigs
    # (GET /api/v1/rigs/<name>/view), so one dashboard draws either
    # (docs/public-release-plan.md, step 10). The keys are a portal's
    # `worker_detail`, and the version is bumped when one changes meaning.
    RIG_VIEW_CONTRACT = 1
    RIG_VIEW_KEYS = frozenset({
        "contract", "name", "kind", "version", "commit", "online", "seen_at", "hello_at",
        "boards", "missing", "running", "max_runs", "profiles", "health", "update", "drained",
        "description", "location", "owner", "visibility", "config", "setup", "commands", "inventory",
    })

    def rig_view(self) -> dict:
        """This rig, as a portal would describe it: what `worker_detail` says
        of a connected rig, from this host's own configuration, health
        snapshot, inventory and queue. `name` is what a portal would call
        it -- the worker name a node reports, or "local" on a farm that is
        nobody's node."""
        try:
            config = self.configuration()
        except Exception:  # a host whose configuration cannot be read says so
            config = None
        try:
            health = json.loads(STATUS_SNAPSHOT.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            health = None
        inventory = self.inventory_snapshot()
        running = self.running_job_ids()
        version = service_version()
        return {
            "contract": self.RIG_VIEW_CONTRACT,
            "name": os.environ.get("ALTERIOM_HIL_WORKER_NAME") or "local",
            "kind": "hardware",
            "version": version.get("version"),
            "commit": version.get("commit"),
            "online": True,
            "seen_at": utcnow(),
            "hello_at": None,
            "boards": len(inventory.get("boards") or []),
            "missing": len(inventory.get("missing") or []),
            "running": len(running),
            "max_runs": self.max_runs,
            "profiles": sorted(self.profiles),
            "health": health,
            "update": None,
            "drained": None,
            "description": None,
            "location": None,
            "owner": None,
            "visibility": None,
            "config": config,
            "setup": rig_setup.capabilities(config, health, inventory, None),
            "commands": [],
            "inventory": {key: inventory.get(key) or [] for key in
                          ("missing", "unregistered", "probe_errors", "instruments", "missing_instruments")},
        }

    # ---- managing a worker from its page ---------------------------------------
    # A command is queued on the portal, handed to the worker in its next
    # heartbeat answer, carried out there and reported back
    # (POST /api/v1/workers/<name>/commands/<id>). Given once: a worker that
    # restarted before reporting does not get it again, it expires.

    # ---- adding a rig ------------------------------------------------------------------------
    # An admin names the rig; the portal gives a token that works once, for an
    # hour, inside a command to run on the new host (rig/join-rig.sh). The
    # host trades it for its node key and installs the current release.

    # A rig is one resource from the moment it is added until it is deleted:
    # pending while it has not joined (waiting for its join command to be run,
    # its token expired, or installing), then the worker it became. Created,
    # read, edited and deleted as one thing (/api/v1/rigs).

    # What a board is, on a rig lent to you: which board, what it can run,
    # and the tags a run selects on. Not where it is plugged in, and not its
    # hardware address -- the world page withholds both for the same reason.
    LENT_BOARD_FIELDS = frozenset({"id", "target", "tags"})

    # ---- releases ------------------------------------------------------------
    # docs/portal-plan.md. Which commit of this repository the nodes run is
    # the portal's to say. CI publishes each release here as a git bundle and
    # makes it current; every heartbeat names the current one; a node running
    # anything else stops taking work, installs it when idle and says hello on
    # the new commit (alteriom_hil.farm_node, rig/node-update.sh). A node
    # needs no GitHub runner, no inbound port and no GitHub credentials.

    # ---- history a node brings -----------------------------------------------
    # A farm that ran standalone before it joined keeps its history on the
    # portal: alteriom_hil.farm_node export-history sends every finished job --
    # its evidence, its log, its bundle or the bundle it reused, and last the
    # job itself with its board verdicts -- with the node's key. A node writes
    # only history that is its own and that the portal does not already hold,
    # and a bundle it brings is history: shown and downloadable, never picked
    # to flash a new run.

    # ---- board reservation -------------------------------------------------
    # Which boards each running job is holding: the allocation contract in
    # docs/farm-allocation.md, filled in by alteriom_hil.allocation. A whole-
    # bank run holds every connected board, a scoped run the boards it was
    # given, discovery none.

    def _grant(self, job_id: str) -> allocation.Grant | None:
        with self._reservation_lock:
            return self.__dict__.get("_grants", {}).get(job_id)

    def _hold_boards(self, job_id: str, boards: list[str]) -> None:
        """Record the boards a job ended up with, once its discover stage chose.

        A job that has the rig to itself may rediscover and scope its map to
        boards other than the ones the dispatcher picked from the last
        inventory; what the dashboard says it holds follows the map it runs.
        """
        with self._reservation_lock:
            grant = self._grants.get(job_id)
            if grant is not None and not grant.shared and not grant.whole_rig:
                self._grants[job_id] = dataclass_replace(grant, boards=tuple(boards))

    def _release(self, job_id: str) -> None:
        with self._reservation_lock:
            self._grants.pop(job_id, None)

    def running_job_ids(self) -> set[str]:
        with self._reservation_lock:
            ids = set(self.__dict__.get("_grants", {}))
        current = self.__dict__.get("_current_job")
        return ids | ({current} if current else set())

    def reservations(self) -> list[dict]:
        with self._reservation_lock:
            return [grant.as_dict() for grant in self.__dict__.get("_grants", {}).values()]

    def reservation(self) -> dict | None:
        """The first running job's hold, as before several could run."""
        held = self.reservations()
        return held[0] if held else None

    # ---- chip details -------------------------------------------------------
    # ---- health ---------------------------------------------------------------
    # The farm checking its own hardware, with its own firmware: one board or
    # the whole rig, at any time. A canary run is an ordinary suite run of the
    # `canary` profile scoped to the boards asked about -- so it queues, holds
    # the rig, flashes, reports and is cancelled like any other run, and needs
    # no second pipeline. See docs/canary.md.

    CANARY_PROFILE = "canary"

    def current_canary(self) -> tuple[str, str] | None:
        """The canary bundle to flash, and the commit it was built from.

        The newest pinned bundle a canary build produced. Pinned is what
        makes it *current*: a deploy installs the release's canary and pins
        it, so it outlives every prune, and the bundle an operator checks a
        board against today is the one the running farm was released with.

        None when there is no such bundle, and then a health check builds
        the canary on the Pi instead -- the fallback that keeps the feature
        usable on a farm that has not deployed since the canary landed.
        """
        spec = self.profiles.get(self.CANARY_PROFILE)
        if spec is None:
            return None
        records = self.store.artifact_records()
        newest: tuple[tuple[str, str], str, str] | None = None
        for bundle in artifact_store.scan(self.artifact_root).bundles.values():
            manifest = bundle.manifest or {}
            if manifest.get("producer") != self.CANARY_PROFILE:
                continue
            pinned_at = (records.get(bundle.id) or {}).get("pinned_at")
            if not pinned_at:
                continue
            revision = manifest.get(spec.revision_key)
            if not isinstance(revision, str) or not SHA_PATTERN.fullmatch(revision):
                # Without a revision the run cannot ask for this bundle by
                # commit, and a bundle nothing can ask for is not current.
                continue
            # Most recently *pinned*, not most recently built: a deploy pins
            # the release's canary, and an operator who deliberately pins an
            # older one has said which canary the farm should be checked
            # against. When two were pinned at once, the newer bundle wins.
            key = (str(pinned_at), bundle.modified)
            if newest is None or key > newest[0]:
                newest = (key, bundle.id, revision)
        return (newest[1], newest[2]) if newest else None

    def health_check(self, request: dict, submitted_by: str | None = None) -> dict:
        """Run the canary against one board, several, or the whole rig.

        `{"boards": ["esp32-c6-01"]}`, or `"all"` / nothing for every
        connected board. The families asked for are the families of those
        boards: a check on one C6 must not demand -- or flash -- an image
        for anything else.
        """
        unknown = set(request) - {"boards"}
        if unknown:
            raise ValueError(f"unknown request fields: {sorted(unknown)}")
        if self.CANARY_PROFILE not in self.profiles:
            raise ValueError(
                f"this farm has no {self.CANARY_PROFILE!r} profile; a health "
                f"check needs the Rig Health Check (see docs/canary.md)"
            )
        wanted = request.get("boards", "all")
        connected = {
            board["id"]: board.get("target")
            for board in (self.inventory_snapshot().get("boards") or [])
            if board.get("id")
        }
        if wanted == "all" or wanted is None:
            # Every board -- a quarantined one included, since a pass is what
            # releases it -- except one reserved for bench work, which is
            # nobody's to flash.
            reserved = {board_id for board_id, hold in self.store.holds().items() if hold["state"] == "reserved"}
            boards = sorted(set(connected) - reserved)
            if not boards:
                raise ValueError("no boards are connected to check" if not connected else "every connected board is reserved")
        else:
            boards = self._check_named_boards(wanted, self.CANARY_PROFILE)
        targets = sorted({connected[board] for board in boards if connected.get(board)})
        if not targets:
            raise ValueError(
                f"the board map gives no MCU family for {', '.join(boards)}; "
                f"rediscover the rig"
            )
        suite = {
            "profile": self.CANARY_PROFILE,
            "boards": boards,
            "targets": targets,
            # A health check is not a validation of a commit, so it never
            # supersedes a queued run and never takes one's place.
            "supersede": False,
        }
        current = self.current_canary()
        if current:
            bundle_id, revision = current
            # Ask for the commit the pinned canary was built from, so the
            # bundle the run names is the bundle the request is checked
            # against -- the same rule a consumer's supplied bundle meets.
            suite["artifact"] = bundle_id
            suite["ref"] = revision
        return self.submit("suite", suite, submitted_by=submitted_by)

    @property
    def board_health_path(self) -> Path:
        return self.state / "board-health.json"

    def load_board_health(self) -> dict:
        try:
            data = json.loads(self.board_health_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # Canary checks whose red is never the farm's (see _record_board_health).
    BOARD_ONLY_CHECKS = frozenset({"test_every_wire_carries_a_level_both_ways"})

    def _record_board_health(self, job_id: str, results: Path, revision: str | None,
                             version: str | None = None) -> dict:
        """Each board's verdict from a canary run, kept for the next look.

        The suite runs every check once per board, so the JUnit document is
        already a board x check matrix -- pytest writes the board id as the
        parameter in each test's name. Read back here, per board, and kept on
        disk beside the chip details so a board's page can say when each
        board was last checked and what failed, without opening a port.

        It also separates the two kinds of red, which is what the per-board
        shape was for: a check that failed on **every** board is the farm,
        and is recorded as such on each board rather than read as six broken
        boards.
        """
        from alteriom_hil.junit import records_from_junit

        checked_at = utcnow()
        per_board: dict[str, dict] = {}
        for record in records_from_junit(
            results, suite=self.CANARY_PROFILE, mode="hardware", boards=0
        ):
            name = record.test.split("::")[-1]
            check, _, parameter = name.partition("[")
            board_id = parameter.rstrip("]")
            if not board_id or not BOARD_ID_PATTERN.fullmatch(board_id):
                # A check that is not per-board says nothing about a board.
                continue
            entry = per_board.setdefault(board_id, {"checks": {}})
            # A board is only as good as its worst verdict for a check: a
            # check retried and passed is a pass, a check that failed is not
            # hidden by a later skip.
            worst = {"failed": 3, "error": 3, "passed": 2, "skipped": 1}
            previous = entry["checks"].get(check)
            if previous is None or worst.get(record.verdict, 0) > worst.get(previous, 0):
                entry["checks"][check] = record.verdict
        if not per_board:
            return {}
        # A check red on every board that ran it is the farm, not the boards --
        # except the wiring check, which is about each board's jumpers and the
        # instrument, never what the farm shares. One unplugged instrument
        # fails every wired board, and pausing the queue for it would stop
        # runs that never touch a wire.
        ran = {
            check for entry in per_board.values() for check, verdict in entry["checks"].items()
            if verdict != "skipped" and check not in self.BOARD_ONLY_CHECKS
        }
        farm_wide = sorted(
            check for check in ran
            if all(
                entry["checks"].get(check) in ("failed", "error")
                for entry in per_board.values()
                if entry["checks"].get(check, "skipped") != "skipped"
            )
        )
        _STATE_FILE_LOCK.acquire()
        try:
            return self._write_board_health(job_id, per_board, farm_wide, checked_at, revision,
                                            version)
        finally:
            _STATE_FILE_LOCK.release()

    def _write_board_health(self, job_id, per_board, farm_wide, checked_at, revision,
                            version=None) -> dict:
        written = self._write_board_health_file(job_id, per_board, farm_wide, checked_at, revision,
                                                version)
        store = self.__dict__.get("store")
        if store is not None:
            for board_id, entry in per_board.items():
                failed = sorted(
                    check for check, verdict in entry["checks"].items() if verdict in ("failed", "error")
                )
                own = [check for check in failed if check not in farm_wide and check not in self.BOARD_ONLY_CHECKS]
                # Whether this check says anything about the board itself: a
                # check red everywhere is the farm's, and a wire is the
                # jumper's and the instrument's -- neither is the board.
                outcome = "board_failed" if own else ("inconclusive" if failed else "passed")
                store.record_board_verdict(job_id, board_id, checked_at, "failed" if failed else "passed", outcome, own)
            if self.__dict__.get("mode") != "node":
                written["quarantine"] = self._apply_quarantine(job_id, list(per_board))
        return written

    # ---- quarantine -------------------------------------------------------------
    # A board that fails checks of its own poisons every run it lands in, and
    # used to be found by a person noticing one id in three failure reports
    # (docs/farm-allocation.md, "Flakiness is an allocation problem"). The
    # canary is the only evidence that is about the board and nobody's code,
    # so it is what decides: a board red on its own checks for
    # `quarantine.after_failures` canary runs in a row is held out of the pool,
    # and the next clean check releases it. Checks red on every board (the
    # farm) and the wiring check (the jumpers) neither count nor clear.

    def quarantine_settings(self) -> dict:
        try:
            from alteriom_hil import hil_config

            configured = hil_config.load_config().get("quarantine") or {}
            defaults = dict(hil_config.DEFAULT_QUARANTINE)
        except Exception:
            configured, defaults = {}, {"enabled": False, "after_failures": 2}
        return {**defaults, **configured}

    def _apply_quarantine(self, job_id: str, board_ids: list[str]) -> dict:
        settings = self.quarantine_settings()
        holds = self.store.holds()
        quarantined, released = [], []
        for board_id in sorted(board_ids):
            history = self.store.board_verdicts(board_id, limit=50)
            hold = holds.get(board_id)
            latest = next((row for row in history if row["outcome"] != "inconclusive"), None)
            if hold and hold["state"] == "quarantined" and latest and latest["outcome"] == "passed" \
                    and latest["job_id"] == job_id:
                self.store.release_hold(board_id)
                released.append(board_id)
                continue
            if not settings.get("enabled") or hold:
                continue
            streak, checks = 0, set()
            for row in history:
                if row["outcome"] == "inconclusive":
                    continue
                if row["outcome"] != "board_failed":
                    break
                streak += 1
                checks.update(row["failed"])
            if streak >= settings["after_failures"] and history and history[0]["job_id"] == job_id:
                self.store.set_hold(
                    board_id, "quarantined",
                    f"failed its Rig Health Check {streak} runs in a row: {', '.join(sorted(checks))}",
                    by="canary", job_id=job_id,
                )
                quarantined.append(board_id)
        if quarantined or released:
            notifier = self._link_notifier()
            self._notify(Notification(
                "board_red",
                (f"Quarantined {', '.join(quarantined)}" if quarantined else "")
                + ("; " if quarantined and released else "")
                + (f"released {', '.join(released)}" if released else ""),
                "Quarantined boards are left out of every run until a clean health check releases "
                "them, or an operator does." if quarantined else "A clean health check released them.",
                link=notifier.link("#hardware") if notifier else None,
                tone="warn" if quarantined else "good",
            ))
        return {"quarantined": quarantined, "released": released}

    def reserve_board(self, board_id: str, reason: str | None, by: str | None) -> dict:
        """Take a registered board out of the pool for bench work."""
        if not isinstance(board_id, str) or not BOARD_ID_PATTERN.fullmatch(board_id):
            raise ValueError("invalid board id")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 200):
            raise ValueError("reason must be text of up to 200 characters")
        registered = {board.id for board in load_registry(self.registry)}
        if board_id not in registered:
            raise LookupError(f"{board_id} is not a registered board")
        hold = self.store.set_hold(board_id, "reserved", (reason or "").strip() or None, by)
        holder = next((held for held in self.reservations() if board_id in held["boards"]), None)
        self.pending.put(("reserved", board_id, {}))
        return {
            "board": board_id, "hold": hold,
            # Reserving does not stop the run on it; nothing new is given it.
            "in_use_by": holder["job_id"] if holder else None,
        }

    def release_board(self, board_id: str, by: str | None) -> dict:
        """Put a reserved or quarantined board back in the pool."""
        if not isinstance(board_id, str) or not BOARD_ID_PATTERN.fullmatch(board_id):
            raise ValueError("invalid board id")
        released = self.store.release_hold(board_id)
        if released is None:
            raise LookupError(f"{board_id} is not reserved or quarantined")
        self.pending.put(("released", board_id, {}))
        return {"board": board_id, "released": released, "by": by}

    def board_history(self, board_id: str, limit: int = 20,
                      runs: tuple[set[str] | None, str | None] | None = None,
                      rig: str | None = None) -> dict:
        if not isinstance(board_id, str) or not BOARD_ID_PATTERN.fullmatch(board_id):
            return {"board": board_id, "hold": None, "verdicts": []}
        verdicts = self.store.board_verdicts(board_id, limit)
        hold = self.store.holds().get(board_id)
        if hold is not None and not self._may_read_bench(rig, hold.get("job_id"), runs):
            # The same redaction the board list makes, because this page is
            # the board list's drill-down: the state and the time say why the
            # board will not take a run, and who took it out of the pool and
            # for what is the owner's bench.
            hold = {key: value for key, value in hold.items()
                    if key not in ("by", "reason", "job_id")}
        if runs is not None:
            # A verdict is the board's health, and a board's health is fair
            # to ask of a board you may use. The run that found it out is a
            # different thing: on a rig lent to this caller the run is not
            # theirs to read (`run_scope`), and the verdict names it. So the
            # line stays and the run id goes -- dropping the row instead
            # would answer "how has this board been" with a history full of
            # holes, which is worse than one that will not say which run
            # each line came from.
            run_rigs, submitter = runs
            mine = rig is not None and rig in (run_rigs or set())
            jobs = self.store.many([item["job_id"] for item in verdicts if item.get("job_id")])
            verdicts = [
                item if mine or self.store.mine(jobs.get(item.get("job_id")) or {}, run_rigs, submitter)
                else {**item, "job_id": None}
                for item in verdicts
            ]
        return {
            "board": board_id,
            "hold": hold,
            "verdicts": verdicts,
        }

    def _may_read_bench(self, rig, job_id, runs) -> bool:
        """Whether the operator metadata hanging off a board is this caller's
        to read. Two ways in: the board is on a rig they administer, so the
        bench is theirs; or the run that wrote the metadata is one they may
        read. A job id naming no run we hold is not readable -- unknown
        answers the same as somebody else's -- but a rig of their own still
        is, which is what makes a hand reservation on an owner's own board
        (no run at all) stay legible to them."""
        if runs is None:
            return True
        run_rigs, submitter = runs
        # Whole-farm access arrives here two ways: `runs` is None, or it is
        # the `(None, None)` that `run_scope` gives a key and an admin. Both
        # mean every bench, and only the first was read that way -- so a hand
        # reservation, which has no run to fall back on, came back stripped
        # of who made it and why for the very operator who made it.
        if run_rigs is None:
            return True
        if rig is not None and rig in run_rigs:
            return True
        return bool(job_id) and self.store.mine(self.store.get(job_id) or {}, run_rigs, submitter)

    def _health_for(self, record, rig, runs) -> dict | None:
        """A board's canary record as this caller may read it: the verdict,
        when it was checked and which checks, always; the run that found out
        and the consumer revision it flashed, only on their own bench."""
        if record is None or self._may_read_bench(rig, record.get("job_id"), runs):
            return record
        return {key: value for key, value in record.items()
                if key not in ("job_id", "canary_revision")}

    def rig_of_board(self, board_id: str) -> str | None:
        """Which rig reports this board, or None if none does. Whether the
        caller may read it is the caller's question, asked against this.

        Read from the stored inventories, not the live snapshot, because the
        snapshot answers a different question. A rig that has gone quiet has
        its boards moved to `missing` and listed nowhere -- rightly, since
        none of them can be given to anyone -- but whose board it is has not
        changed with the rig's uptime. Asking the snapshot made a board's
        history 404 for its own owner exactly when the rig went offline,
        which is when that history is most worth reading.
        """
        store = self.__dict__.get("store")
        if store is None:
            return None
        for worker in store.workers():
            inventory = worker.get("inventory") or {}
            for board in (inventory.get("boards") or []):
                if board.get("id") == board_id:
                    return worker["name"]
            # A rig lists a board it is registered for but cannot see right
            # now under `missing`. That is an availability answer -- the
            # board cannot be given to anyone -- and whose board it is does
            # not change with whether its rig can see it this minute. The
            # offline case was fixed by reading the stored inventories; this
            # is the same mistake one level in, for a rig that is up and has
            # a board unplugged.
            for missing in (inventory.get("missing") or []):
                name = missing.get("id") if isinstance(missing, dict) else missing
                if name == board_id:
                    return worker["name"]
        return None

    def _write_board_health_file(self, job_id, per_board, farm_wide, checked_at, revision,
                                 version=None) -> dict:
        known = self.load_board_health()
        macs = {
            board["id"]: board.get("mac")
            for board in (self.inventory_snapshot().get("boards") or [])
            if board.get("id")
        }
        for board_id, entry in per_board.items():
            failed = sorted(
                check for check, verdict in entry["checks"].items()
                if verdict in ("failed", "error")
            )
            known[board_id] = {
                "id": board_id,
                "mac": macs.get(board_id),
                "verdict": "failed" if failed else "passed",
                "checked_at": checked_at,
                "job_id": job_id,
                "canary_revision": revision,
                # The Rig Health Check firmware version that checked it.
                "canary_version": version,
                "checks": entry["checks"],
                "failed": failed,
                # Of what failed here, what failed everywhere: the farm's
                # fault to answer for, not this board's.
                "farm_wide": [check for check in failed if check in farm_wide],
            }
        tmp = self.board_health_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(known, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.board_health_path)
        return {
            "boards": {board_id: known[board_id]["verdict"] for board_id in per_board},
            "farm_wide": farm_wide,
        }

    def _notify_boards_red(self, job_id: str, health: dict) -> None:
        """Boards a canary found failing checks of their own.

        What failed on every board is the farm's, and the paused queue says
        so; this is the boards that failed something the rest passed -- the
        board to look at before it fails somebody's run.
        """
        known = self.load_board_health()
        own = {}
        for board_id, verdict in (health.get("boards") or {}).items():
            entry = known.get(board_id) or {}
            failed = [check for check in entry.get("failed") or [] if check not in (entry.get("farm_wide") or [])]
            if verdict == "failed" and failed:
                own[board_id] = failed
        if not own:
            return
        notifier = self._link_notifier()
        names = ", ".join(sorted(own))
        self._notify(Notification(
            "board_red",
            f"Rig Health Check red on {len(own)} board{'s' if len(own) != 1 else ''}: {names}",
            "; ".join(f"{board}: {', '.join(checks)}" for board, checks in sorted(own.items()))
            + f" (run {job_id[:8]})",
            link=notifier.link(f"#run/{job_id}") if notifier else None,
            tone="warn",
        ))
        # One event per board: a consumer acting on a board -- quarantining it,
        # opening something against it -- wants the board, not a list to parse.
        # Only a portal has subscriptions, and only a portal has the jobs table
        # to ask which rig this ran on.
        if self.__dict__.get("mode") != "portal":
            return
        rig = (self.store.get(job_id) or {}).get("worker")
        for board_id, checks in sorted(own.items()):
            self.emit(farm_webhooks.Event(
                "board", "red", rig=rig,
                summary=f"{board_id} failed {', '.join(checks)}",
                payload={"board": board_id, "failed": checks, "job_id": job_id, "rig": rig},
            ))

    # ---- retention ------------------------------------------------------------
    # Run evidence and logs are the only things on the card that grow without
    # end once bundles have their prune. Retention deletes what is bulky and
    # old -- each run's serial and broker captures, whole job logs, stale
    # checkouts -- never the newest runs, never a queued or running job's,
    # and records what it removed so a run's page says so. What a run *found*
    # stays: its report, its JUnit, its per-test records and the board
    # verdicts are kilobytes, and they are the history the statistics and
    # release reports are made of. So is the job row, which is never deleted.

    def retention_settings(self) -> dict:
        try:
            from alteriom_hil import hil_config

            configured = hil_config.load_config().get("retention") or {}
            defaults = dict(hil_config.DEFAULT_RETENTION)
        except Exception:
            configured, defaults = {}, {
                "enabled": False, "run_evidence_days": 90, "log_days": 90,
                "keep_newest_runs": 100, "workspace_days": 2,
            }
        return {**defaults, **configured}

    def retention_plan(self, settings: dict | None = None, now: datetime | None = None) -> dict:
        """What retention would delete now, and what it would free as far as
        the last disk measurement can say. Nothing walks a directory."""
        settings = settings or self.retention_settings()
        now = now or datetime.now(timezone.utc)
        with self.store.connect() as db:
            rows = db.execute("SELECT id, status, created_at FROM jobs ORDER BY created_at DESC").fetchall()
        storage = self._storage_state()
        with storage["lock"]:
            sizes = {
                kind: {child["name"]: child.get("bytes") for child in children}
                for kind, children in storage["children"].items()
            }
        protected = {row["id"] for row in rows[: settings["keep_newest_runs"]]}
        protected |= {row["id"] for row in rows if row["status"] in ("queued", "running")}
        with self.store.connect() as db:
            already = {
                (row["job_id"], row["kind"])
                for row in db.execute("SELECT job_id, kind FROM evidence_records").fetchall()
            }
        evidence_before = now - timedelta(days=settings["run_evidence_days"])
        logs_before = now - timedelta(days=settings["log_days"])
        runs, logs = [], []
        for row in rows:
            if row["id"] in protected:
                continue
            created = datetime.fromisoformat(row["created_at"])
            if (created < evidence_before and (row["id"], "serial") not in already
                    and (self.state / "runs" / row["id"] / "serial").is_dir()):
                # The measurement knows the whole run directory, not its
                # captures alone: an upper bound, and said to be one.
                runs.append({"id": row["id"], "created_at": row["created_at"], "bytes": sizes.get("runs", {}).get(row["id"])})
            if created < logs_before and (self.state / "logs" / f"{row['id']}.log").is_file():
                logs.append({"id": row["id"], "created_at": row["created_at"], "bytes": sizes.get("logs", {}).get(f"{row['id']}.log")})
        workspaces = []
        workspace_before = (now - timedelta(days=settings["workspace_days"])).timestamp()
        current = self.running_job_ids() if "_reservation_lock" in self.__dict__ else {self.__dict__.get("_current_job")}
        try:
            entries = list((self.state / "workspaces").iterdir())
        except OSError:
            entries = []
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir() or entry.name in current or entry.stat().st_mtime >= workspace_before:
                    continue
            except OSError:
                continue
            if entry.name in protected:
                continue
            workspaces.append({"name": entry.name, "bytes": sizes.get("workspaces", {}).get(entry.name)})
        known = [item["bytes"] for item in [*runs, *logs, *workspaces] if item["bytes"] is not None]
        return {
            "settings": settings,
            "runs": runs,
            "logs": logs,
            "workspaces": workspaces,
            # At most: a run's figure is its whole directory, of which only
            # the captures go.
            "bytes_at_most": sum(known),
            "bytes_known": len(known) == len(runs) + len(logs) + len(workspaces),
        }

    def retention_sweep(self, dry_run: bool = True) -> dict:
        settings = self.retention_settings()
        plan = self.retention_plan(settings)
        if dry_run:
            return {**plan, "dry_run": True, "last": self.__dict__.get("_retention_last")}
        removed = {"runs": 0, "logs": 0, "workspaces": 0}
        errors = []
        for item in plan["runs"]:
            serial = self.state / "runs" / item["id"] / "serial"
            try:
                for path in serial.iterdir():
                    # The board verdicts beside the captures are the run's
                    # findings, not its bulk.
                    if path.name == "board-health.json":
                        continue
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                self.store.record_evidence_removal(item["id"], "serial", None)
                removed["runs"] += 1
            except OSError as exc:
                errors.append({"id": item["id"], "error": str(exc)})
        for item in plan["logs"]:
            try:
                (self.state / "logs" / f"{item['id']}.log").unlink()
                self.store.record_evidence_removal(item["id"], "log", item["bytes"])
                removed["logs"] += 1
            except OSError as exc:
                errors.append({"id": item["id"], "error": str(exc)})
        for item in plan["workspaces"]:
            try:
                shutil.rmtree(self.state / "workspaces" / item["name"])
                removed["workspaces"] += 1
            except OSError as exc:
                errors.append({"id": item["name"], "error": str(exc)})
        if any(removed.values()):
            self._storage_changed()
        self._retention_last = {
            "finished_at": utcnow(), "removed": removed, "bytes_at_most": plan["bytes_at_most"],
            "errors": errors,
        }
        return {**plan, "dry_run": False, "last": self._retention_last}

    def retention_loop(self, first_delay: float = 600, interval: float = 86400) -> None:
        """Sweep once a day while the host has retention on. A thread of its
        own, started by main(): a sweep deletes files, never holds the rig."""
        time.sleep(first_delay)
        while True:
            try:
                if self.retention_settings().get("enabled"):
                    self.retention_sweep(dry_run=False)
            except Exception as exc:  # the next day tries again
                sys.stderr.write(f"farm-api: retention sweep failed: {exc}\n")
            time.sleep(interval)

    # What esptool can say about each part, read at discovery and kept on
    # disk keyed by MAC, so the hardware page describes the silicon without
    # opening a port — which it cannot do while a run holds the rig.

    @property
    def chip_details_path(self) -> Path:
        return self.state / "chip-details.json"

    def load_chip_details(self) -> dict:
        try:
            data = json.loads(self.chip_details_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def configuration(self) -> dict:
        """What this farm is actually configured to do, for the dashboard.

        Read from the running service rather than restated in the page, so a
        setting an operator changed on the host shows up here instead of the
        documentation's idea of it. Nothing secret is returned: the API token
        is named by path only, never read.
        """
        config: dict = {}
        try:
            from alteriom_hil import hil_config

            config = hil_config.load_config()
        except Exception:
            config = {}
        service = config.get("service") or {}
        paths = config.get("paths") or {}
        health = config.get("health") or {}
        gateway = config.get("gateway") or {}
        mqtt = config.get("mqtt") or {}
        channels = _notify_channels_of(config)
        notify = channels[0] if channels else {}
        backup = config.get("backup") or {}
        callmebot: dict = {}
        callmebot_budget = None
        try:
            from alteriom_hil import hil_config

            callmebot = hil_config.callmebot_settings(config) or {}
            callmebot_budget = hil_config.callmebot_budget_file(config) if callmebot else None
        except Exception:
            pass
        # The stored link as `providers show` prints it -- the key never, the
        # number by its last two digits -- and only a link that loads: a file
        # that does not parse is not echoed in any form.
        callmebot_link = None
        if callmebot.get("url_file"):
            try:
                callmebot_link = farm_providers.redacted(farm_providers.load_callmebot_link(callmebot["url_file"]))
            except (farm_providers.ProviderError, OSError):
                callmebot_link = None
        # What the farm acts on, not only what a file says: with no file (a
        # portal nobody configured) these are their defaults -- off -- and the
        # page should say off rather than say nothing.
        retention = self.retention_settings()
        quarantine = self.quarantine_settings()
        notifier = self._link_notifier()
        last_backup = farm_backup.last_backup(Path(backup["directory"])) if backup.get("directory") else None
        free_gb = None
        try:
            free_gb = round(shutil.disk_usage(self.state).free / 1_000_000_000, 1)
        except OSError:
            pass
        portal = None
        if self.__dict__.get("mode") == "portal":
            from alteriom_hil import hil_config as _config_module

            current = self.current_release() or {}
            portal = {
                # Where the settings a portal decides with come from. None when
                # the deployment mounts no file: quarantine and retention are
                # then off, whatever the nodes' own files say.
                "config_file": str(_config_module.CONFIG_PATH) if _config_module.CONFIG_PATH.is_file() else None,
                "public_host": os.environ.get("ALTERIOM_HIL_PUBLIC_HOST"),
                "keys_file": os.environ.get("ALTERIOM_HIL_API_KEYS_FILE"),
                "current_release": current.get("commit"),
                "releases_kept": RELEASES_KEPT,
                "heartbeat_seconds": HEARTBEAT_SECONDS,
                "worker_stale_seconds": WORKER_STALE_SECONDS,
                "worker_lost_seconds": WORKER_LOST_SECONDS,
                "lease_ack_seconds": LEASE_ACK_SECONDS,
                "workers": len(self.store.workers()),
            }
        report = {
            "version": service_version(),
            "portal": portal,
            "host": {
                "hostname": socket.gethostname(),
                "mode": config.get("mode", "hardware"),
                # Which configuration schema the host file is written to, so
                # a host provisioned before a field existed is identifiable
                # from the page rather than by reading the file.
                "config_schema": config.get("schema"),
                # standalone, portal or node (docs/portal-plan.md).
                "farm_mode": self.__dict__.get("mode", "standalone"),
                "runner_unit": (config.get("runner") or {}).get("unit"),
                "state_free_gb": free_gb,
            },
            "service": {
                "bind": service.get("bind", "127.0.0.1"),
                "port": service.get("port", 8090),
                # The path, never the token. An operator needs to know which
                # file to rotate; nobody needs the secret rendered in a page.
                "token_file": service.get("token_file"),
                "suite_timeout_seconds": int(os.environ.get("ALTERIOM_HIL_SUITE_TIMEOUT", DEFAULT_SUITE_TIMEOUT_SECONDS)),
                "rig_lock": str(farm_shared.RIG_LOCK_PATH),
                # How many runs may be in progress at once (queue.concurrency).
                "concurrency": self.__dict__.get("max_runs", 1),
                # Whether the host's configuration asks for this service at
                # all, and the name a reverse proxy answers on. Both were
                # readable only in the file on the host until now.
                "enabled": service.get("enabled", True) if service else None,
                "public_host": service.get("public_host"),
            },
            "paths": {
                "repo": str(self.repo),
                "state": str(self.state),
                "inventory": str(self.registry),
                "board_map": str(self.board_map),
                "venv": paths.get("venv"),
                "python": str(self.python),
            },
            # The rig's own network, which is not decoration: the canary's
            # radio, uplink and queue checks skip when these are unset, and
            # "why did half the health checks skip" is a question this page
            # should answer without an ssh session. The password's *path*
            # only -- the secret itself has no business in a web page, and an
            # operator needs to know which file to rotate.
            "gateway": {
                # `None` when the section is absent, so the row is dropped
                # like any other missing field and the page's "the service
                # returned no configuration" answer stays reachable. A
                # configured-but-off gateway is False, and says so.
                "enabled": gateway.get("enabled") if gateway else None,
                "ssid": gateway.get("ssid"),
                "password_file": gateway.get("password_file"),
                "endpoint": gateway.get("endpoint"),
                # The channel the AP runs on, which a board promoted to bridge
                # takes the mesh to: "why did half the mesh go missing" is
                # answerable from here.
                "channel": gateway.get("channel"),
            },
            "mqtt": {
                "enabled": mqtt.get("enabled") if mqtt else None,
                "url": mqtt.get("url"),
            },
            # Whether a board plugged into this rig registers itself.
            "inventory": {
                "auto_register": (config.get("inventory") or {}).get("auto_register")
                if config.get("inventory") is not None else None,
            },
            # Where the farm says it broke -- the webhook's file, never the
            # webhook, which is a credential -- and how the last one went.
            # Every channel this host notifies through. `notify` below is the
            # first of them, because a rig held one and everything that reads
            # this -- the setup card, an older portal during a rollout -- was
            # written against that shape.
            "notify_channels": [
                {
                    "id": channel["id"],
                    "channel": channel.get("channel", "webhook"),
                    "enabled": channel.get("enabled"),
                    "token_file": channel.get("token_file"),
                    "chat_id": channel.get("chat_id"),
                    "webhook_url_file": channel.get("webhook_url_file"),
                    "format": channel.get("format"),
                    "events": ", ".join(channel.get("events") or []) or None,
                    "last_delivery": _delivery_summary(
                        next((item.last for item in (self.__dict__.get("notifiers") or [])
                              if getattr(item, "id", None) == channel["id"]), None)),
                }
                for channel in _notify_channels_of(config)
            ],
            "notify": {
                "channel": notify.get("channel", "webhook") if notify else None,
                "token_file": notify.get("token_file"),
                "chat_id": notify.get("chat_id"),
                "enabled": notify.get("enabled") if notify else None,
                "webhook_url_file": notify.get("webhook_url_file"),
                "format": notify.get("format"),
                "events": ", ".join(notify.get("events") or []) or None,
                "last_delivery": _delivery_summary(notifier.last if notifier else None),
            },
            "backup": {
                "enabled": backup.get("enabled") if backup else None,
                "directory": backup.get("directory"),
                "keep": backup.get("keep"),
                "target": backup.get("target"),
                "last_backup": _backup_summary(last_backup),
            },
            "quarantine": {
                "enabled": quarantine.get("enabled") if quarantine else None,
                "after_failures": quarantine.get("after_failures"),
            },
            # The rig's CallMeBot provider (docs/providers.md): the link's
            # file, when a run may spend a real message, and how many it has
            # today. Never the link.
            "callmebot": {
                "url_file": callmebot.get("url_file"),
                "send": callmebot.get("send"),
                "max_per_day": callmebot.get("max_per_day"),
                "used_today": budget_used(callmebot_budget) if callmebot_budget else None,
                "link": callmebot_link,
            },
            # How this service takes part in the farm, and for a node, the
            # portal it takes runs from -- the key file's path, never the key.
            "farm": {
                # Attached: this service is standalone and a node agent runs
                # beside it (alteriom-hil-node).
                "mode": "attached" if self.__dict__.get("mode", "standalone") == "standalone"
                and os.environ.get("ALTERIOM_HIL_FARM_ATTACHED") == "1" else self.__dict__.get("mode", "standalone"),
                "portal_url": os.environ.get("ALTERIOM_HIL_PORTAL_URL"),
                "worker_name": os.environ.get("ALTERIOM_HIL_WORKER_NAME"),
                "node_key_file": os.environ.get("ALTERIOM_HIL_NODE_KEY_FILE"),
            },
            "retention": {
                "enabled": retention.get("enabled") if retention else None,
                "run_evidence_days": retention.get("run_evidence_days"),
                "log_days": retention.get("log_days"),
                "keep_newest_runs": retention.get("keep_newest_runs"),
                "workspace_days": retention.get("workspace_days"),
            },
            "health": {
                "interval_minutes": health.get("interval_minutes"),
                "minimum_boards": health.get("minimum_boards"),
                "disk_warn_percent": health.get("disk_warn_percent"),
                "disk_critical_percent": health.get("disk_critical_percent"),
            },
            "build": {
                "targets": sorted(TARGETS),
                # Names only, kept because the dashboard and any other client
                # already read this shape.
                "profiles": sorted(self.profiles),
                # What a caller needs to *offer* a profile rather than just
                # name it: the dashboard builds its picker, its default ref and
                # its source links from this, so adding a consumer stays a data
                # change instead of also being a front-end change.
                "profile_details": {
                    name: {
                        "label": spec.label,
                        "default_ref": spec.default_ref,
                        "repo": _https_repo(spec.repo),
                        "suite_path": spec.suite_path,
                        "location": spec.location,
                        "exclusive": spec.exclusive,
                        "concurrent": spec.concurrent,
                        "resources": list(spec.resources),
                        "min_boards": spec.min_boards,
                        # The families it asks the bank for, and which of
                        # them it will run without (`optional`). An operator
                        # reading the dashboard sees which board is away on
                        # purpose; a dispatcher choosing between farms can
                        # read it beside GET /api/v1/capacity.
                        "needs": [dict(need) for need in spec.needs],
                        # Where its firmware comes from: the only producer
                        # whose bundles the farm flashes for it.
                        "supply_repo": _https_repo(spec.supply_repo) if spec.supply_repo else None,
                        "supply_workflow": spec.supply_workflow,
                    }
                    for name, spec in sorted(self.profiles.items())
                },
            },
        }
        # The public half of the key a CallMeBot link is sealed to from the
        # portal's page (alteriom_hil.providers, docs/providers.md): the DER
        # SubjectPublicKeyInfo and its sha256. Absent on a host without one.
        seal_key = farm_providers.seal_key_info()
        if seal_key is not None:
            report["seal_key"] = seal_key
        return report

    # ---- partial runs and reuse ----------------------------------------------
    # A debugging iteration should not pay for a full suite: three full runs
    # are an hour and a half of rig time, and the question is usually one
    # test. A suite request may name the tests to run, and by default flashes
    # a bundle the farm already holds for the commit, and skips the flash when
    # the boards already run that image.

    def _agent_source_path(self, profile: str | None) -> str | None:
        """Where a profile says its HIL agent's source is; None when it has
        none. No profile named is the default one: what every caller meant
        when the farm had one agent."""
        spec = self.profiles.get(profile or self.default_profile)
        return spec.agent_source_path if spec is not None else None

    def agent_source_sha(self, profile: str | None = None) -> str | None:
        """The digest build_artifacts.py stamps into a manifest as
        hil_agent_sha: a profile's HIL agent source in this checkout,
        independent of the library. None for a profile with no agent."""
        source_path = self._agent_source_path(profile)
        if not source_path:
            return None
        firmware = self.repo / source_path
        return agent_digest(
            (path.relative_to(firmware).as_posix(), path.read_bytes())
            for path in firmware.rglob("*")
            if path.is_file() and ".pio" not in path.parts
        )

    def expected_agent_sha(self, profile: str | None = None) -> str | None:
        """The HIL agent a profile's bundle must be built for to run here;
        None when the profile has no agent, and its bundles are held to none.

        A portal does not flash: its nodes do, on the release it hands them,
        so a bundle must speak that release's agent -- not whatever this
        portal's own image happens to carry. Judged by its image, a portal
        refused every bundle built after an agent change until someone
        rolled the image out by hand (2026-09-14). A release that carries no
        agent, a portal with no release, a node and a standalone farm all
        judge by their own checkout.

        A release says which agent each of its profiles speaks (`agents`).
        One published before it did says one digest for the farm
        (`hil_agent_sha`), and that is read for a profile that has an agent,
        as it always was.
        """
        if self.__dict__.get("mode") == "portal":
            release = self.current_release()
            if release:
                agents = release.get("agents")
                if isinstance(agents, dict) and agents:
                    found = agents.get(profile or self.default_profile)
                    return str(found) if found else None
                if release.get("hil_agent_sha") and self._agent_source_path(profile):
                    return str(release["hil_agent_sha"])
        return self.agent_source_sha(profile)

    def reusable_artifacts(
        self,
        sha: str,
        targets: list[str],
        revision_key: str = "painlessmesh_sha",
        profile: str = DEFAULT_PROFILE,
    ) -> tuple[str, Path] | None:
        """The newest bundle the farm holds that this run can flash: same
        profile, same source commit, same agent source, every family asked
        for. Returns (bundle id, bundle directory) or None.

        Every bundle in the store is a candidate -- the ones projects' CI
        supplied as much as the ones farm builds left before the farm stopped
        building. Looking only at bundles a farm build wrote, as this once
        did, would find nothing new ever again: a re-run of a supplied run
        would be refused for want of a bundle the farm is holding.

        Matching the *profile* is what makes this safe, not the revision key
        alone. Two profiles can perfectly well name the same key -- `git_sha` is
        an obvious choice for any project -- and legitimately reference the same
        commit: a fork, a submodule bump, or two different builds of one
        repository. Keying on the manifest alone would then hand one profile the
        other's images and flash the wrong firmware under a passing report.

        Two small files per bundle and no walk: this runs on a request thread
        when a run is submitted.
        """
        agent = self.expected_agent_sha(profile)
        wanted = str(sha or "").lower()
        if not wanted:
            return None
        candidates = []
        try:
            directories = [
                entry for entry in self.artifact_root.iterdir()
                if artifact_store.BUNDLE_ID.fullmatch(entry.name)
                and not entry.is_symlink() and entry.is_dir()
            ]
        except OSError:
            return None
        for directory in directories:
            if self.imported_artifact(directory.name):
                # History a node brought: shown, never chosen to flash.
                continue
            manifest_path = directory / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                written = manifest_path.stat().st_mtime
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict) or str(manifest.get(revision_key) or "").lower() != wanted:
                continue
            # The agent digest only exists for profiles that build one; a
            # profile without it must not match every stale manifest either.
            if agent is not None and "hil_agent_sha" in manifest and manifest["hil_agent_sha"] != agent:
                continue
            families = self._manifest_targets(manifest)
            if not set(targets) <= set(families):
                continue
            if not all(
                isinstance(families[target].get("image"), str)
                and (directory / families[target]["image"]).is_file()
                for target in targets
            ):
                continue
            candidates.append((written, directory))
        if not candidates:
            return None
        # A bundle a farm build left is named for its job, whose request says
        # which profile; a supplied one says so in its provenance. Jobs
        # predating profiles carry no profile field and are painlessMesh.
        jobs = self.store.many(directory.name for _, directory in candidates)
        for _, directory in sorted(candidates, key=lambda item: item[0], reverse=True):
            job = jobs.get(directory.name)
            if job is not None:
                built_for = (job.get("request") or {}).get("profile", DEFAULT_PROFILE)
            else:
                built_for = (self.bundle_provenance(directory.name) or {}).get("profile")
            if built_for == profile:
                return directory.name, directory
        return None

    def suite_catalogue(self, profile: str | None = None) -> list[dict]:
        """A profile's test files, their tests and the capabilities each
        carries, read from the source so the dashboard's picker follows the
        suite and not a copy of it. No profile named is the farm's default.

        Only a suite this repository holds can be read without a checkout: a
        profile whose suite arrives with its consumer's checkout has no
        catalogue here, and its tests are named by hand."""
        catalogue = []
        spec = self.profiles.get(profile or self.default_profile)
        if spec is None or spec.runs_in_consumer_repo:
            return catalogue
        directory = self.repo / spec.suite_path
        try:
            files = sorted(directory.glob("test_*.py"))
        except OSError:
            files = []
        for path in files:
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            tests = []
            for match in re.finditer(r"((?:@pytest\.mark\.[^\n]*\n\s*)*)def (test_[A-Za-z0-9_]+)\(", source):
                capabilities = []
                for mark in CAPABILITY_MARK.finditer(match.group(1)):
                    capabilities.extend(re.findall(r"\"([^\"]+)\"", mark.group(1)))
                tests.append({"name": match.group(2), "capabilities": capabilities})
            catalogue.append({"file": path.name, "tests": tests})
        return catalogue

    # Where a credential for private consumer repositories is kept, if there is
    # one. Same directory as the API token, same expectation: root-owned, 0640
    # to the service group, never in this repository.
    CONSUMER_TOKEN_PATH = Path("/etc/alteriom-hil/consumer-token")

    def inventory_snapshot(self, annotate: bool = False, workers: set[str] | None = None,
                           runs: tuple[set[str] | None, str | None] | None = None) -> dict:
        # Reconciled with the registry as it is now, so a board registered
        # from the admin CLI shows up (as missing until the next discovery,
        # which the CLI also triggers) without waiting for a service job.
        if self.__dict__.get("mode") == "portal":
            snapshot = self._portal_snapshot(workers, None if runs is None else runs[0])
        else:
            snapshot = load_inventory_snapshot(self.state, self.registry)
        if annotate:
            self._annotate_states(snapshot, workers, runs)
            known = self.load_chip_details()
            health = self.load_board_health()
            for board in snapshot.get("boards") or []:
                mac = board.get("mac")
                record = known.get(normalize_mac(mac)) if mac else None
                # A portal has no chip readings of its own: the worker's stand.
                if record is not None or "details" not in board:
                    board["details"] = record
                # What the canary last said about this board, so the page can
                # show a board that started failing its radio join before it
                # fails somebody's run. The verdict and its checks are the
                # board's; the run that produced them, and the consumer
                # revision it flashed, are not, so on a rig lent to this
                # caller those two go -- the same redaction board_history
                # makes, because this is the same fact on the same page.
                board["health"] = self._health_for(
                    health.get(board.get("id")), board.get("worker"), runs)
        return snapshot

    def _annotate_states(self, snapshot: dict, workers: set[str] | None = None,
                         runs: tuple[set[str] | None, str | None] | None = None) -> dict:
        """Give every connected board a state, not just a presence.

        "Connected" answers whether a board is plugged in; it does not answer
        the question an operator actually has, which is whether the board is
        free. A board driving a validation run is connected and unavailable at
        the same time, and anything that opens its serial port meanwhile — a
        chip-details probe, say — takes it away from the run.

        The states are the vocabulary of docs/farm-allocation.md, so the
        dashboard already speaks it before a scheduler exists to hand boards
        out: only what fills `in_use` changes later.
        """
        # Two scopes here, because a grant answers two different questions.
        #
        # Is this board free? That is the board's own availability, and it is
        # exactly what sharing a rig is meant to lend: somebody deciding
        # whether to run on a shared rig has to see that its boards are
        # taken. So `workers` -- the rigs this caller may READ -- decides
        # which grants count towards `state`, `available` and `in_use`.
        #
        # Who is using it, for what? That is the run, and a run is not lent
        # with the rig. A grant names the job id, the project label, the
        # boards, the resources and the start time, so returning it here
        # would hand back through the inventory precisely what the job routes
        # refuse (see `run_scope`). So `runs` -- the narrower scope -- decides
        # which grants may be NAMED, in `held_by` and in the two reservation
        # fields. None, the default, names all of them, which is a key's
        # answer and what this has always given.
        held = self.reservations()
        if workers is not None:
            held = [grant for grant in held if grant.get("worker") in workers]
        busy = {board_id: grant for grant in held for board_id in grant["boards"]}
        store = self.__dict__.get("store")
        named = held
        if runs is not None and store is not None:
            run_rigs, submitter = runs
            jobs = store.many([grant["job_id"] for grant in held])
            named = [grant for grant in held
                     if store.mine(jobs.get(grant["job_id"]) or {"worker": grant.get("worker")},
                                   run_rigs, submitter)]
        may_name = {grant["job_id"] for grant in named}
        holds = store.holds() if store is not None else {}
        for board in snapshot.get("boards") or []:
            grant = busy.get(board.get("id"))
            hold = holds.get(board.get("id"))
            # In use wins: a quarantined board a health check is running on
            # is in use, and still quarantined.
            board["state"] = "in_use" if grant else (hold["state"] if hold else "available")
            if hold:
                # That a board is reserved or quarantined is the board's, and
                # a borrower needs it: it is why the board will not take
                # their run. Who took it out of the pool, for what, and under
                # which run is the owner's bench, so on a rig lent to this
                # caller those three go and the state and the time stay.
                board["hold"] = hold if self._may_read_bench(
                    board.get("worker"), hold.get("job_id"), runs) \
                    else {key: value for key, value in hold.items()
                          if key not in ("by", "reason", "job_id")}
            if grant and grant["job_id"] in may_name:
                board["held_by"] = {"job_id": grant["job_id"], "kind": grant["kind"], "label": grant["label"]}
        # The first holder, as before several jobs could run; all of them in
        # `reservations`.
        snapshot["reservation"] = named[0] if named else None
        snapshot["reservations"] = named
        snapshot["available"] = sum(
            1 for b in snapshot.get("boards") or [] if b.get("state") == "available"
        )
        snapshot["in_use"] = len(busy)
        return snapshot

    def capacity(self) -> dict:
        """What this farm could take right now, by family.

        The answer a dispatcher choosing between farms needs, and a caller
        deciding whether to wait (docs/farm-allocation.md, "Multiple farms"):
        boards connected, free and in use per family, with the tags the free
        ones carry, and how busy the queue is. Nothing here touches hardware.
        """
        snapshot = self._annotate_states(self.inventory_snapshot())
        families: dict[str, dict] = {}
        for board in snapshot.get("boards") or []:
            family = families.setdefault(
                board.get("target") or "unknown",
                {"connected": 0, "available": 0, "in_use": 0, "reserved": 0, "quarantined": 0,
                 "available_tags": {}},
            )
            family["connected"] += 1
            state = board.get("state")
            if state == "available":
                family["available"] += 1
                for tag in board.get("tags") or []:
                    family["available_tags"][tag] = family["available_tags"].get(tag, 0) + 1
            else:
                family[state if state in ("reserved", "quarantined") else "in_use"] += 1
        state = self.queue_state()
        return {
            "schema": 1,
            "farm": socket.gethostname(),
            "paused": state["paused"],
            "concurrency": state["concurrency"],
            "running": len(state["running_jobs"]),
            "queued": len(state["queued"]),
            "families": dict(sorted(families.items())),
            "missing": len(snapshot.get("missing") or []),
        }

    # Every artifact a run can leave behind, by name. The same table serves
    # the job detail (which are there) and the download route (which file a
    # name means), so a name that is not in it is not a file the API serves.
    ARTIFACT_TYPES = {
        "manifest": "application/json",
        "junit": "application/xml",
        "preflight": "application/json",
        "board_health": "application/json",
        "report_markdown": "text/markdown; charset=utf-8",
        "report_json": "application/json",
        "runs": "application/x-ndjson",
        "log": "text/plain; charset=utf-8",
    }

    def artifact_paths(self, job_id: str) -> dict[str, Path]:
        """Every file a run can be asked for, by name.

        The fixed names are the reports and records every suite writes. The
        rest are enumerated from the run: one serial capture per board
        (`serial:<board>`, and `serial:<board>.preflight` for the capture
        taken while the flashed board was verified) and one flash image per
        family built (`firmware:<target>`). A name not in this table is not
        a file the API serves, whatever the path in it might spell.
        """
        run_dir = self.state / "runs" / job_id
        artifact_dir = self.state / "artifacts" / job_id
        paths = {
            "manifest": artifact_dir / "manifest.json",
            "junit": run_dir / "results.xml",
            "preflight": run_dir / "preflight.json",
            "board_health": run_dir / "serial" / "board-health.json",
            "report_markdown": run_dir / "metrics" / "report.md",
            "report_json": run_dir / "metrics" / "report.json",
            "runs": run_dir / "metrics" / "runs.jsonl",
            "log": self.state / "logs" / f"{job_id}.log",
        }
        try:
            captures = sorted((run_dir / "serial").glob("*.serial.log"))
        except OSError:
            captures = []
        for capture in captures:
            paths[f"serial:{capture.name[: -len('.serial.log')]}"] = capture
        # What the suite received from the broker (`mqtt:<name>`), written
        # by alteriom_hil.mqtt as it arrives: the queue side of a run, kept
        # like a serial log, because a mesh that forms and never reaches
        # the queue is a question about what the gateway said and when.
        try:
            queues = sorted(self.mqtt_evidence_dir(job_id).glob("*.jsonl"))
        except OSError:
            queues = []
        for queue in queues:
            paths[f"mqtt:{queue.stem}"] = queue
        try:
            images = sorted(artifact_dir.glob("*/flash-image.bin"))
        except OSError:
            images = []
        for image in images:
            paths[f"firmware:{image.parent.name}"] = image
        return paths

    @classmethod
    def artifact_type(cls, name: str) -> str:
        if name.startswith("serial:"):
            return "text/plain; charset=utf-8"
        if name.startswith("mqtt:"):
            return "application/x-ndjson"
        if name.startswith("firmware:"):
            return "application/octet-stream"
        return cls.ARTIFACT_TYPES.get(name, "application/octet-stream")

    def artifact(self, job_id: str, name: str) -> tuple[Path, str, str]:
        """The file behind one artifact name, its content type, and the
        file name to hand the browser: the run's short id, the family for an
        image, and the file's own name."""
        if self.store.get(job_id) is None:
            raise KeyError(job_id)
        paths = self.artifact_paths(job_id)
        if name not in paths:
            raise LookupError(f"no artifact named {name!r}")
        path = paths[name]
        if not path.is_file():
            raise FileNotFoundError(str(path))
        prefix = f"{job_id[:8]}-{path.parent.name}-" if name.startswith("firmware:") else f"{job_id[:8]}-"
        return path, self.artifact_type(name), prefix + path.name

    def job_detail(self, job_id: str) -> dict | None:
        job = self.store.get(job_id)
        if not job:
            return None
        paths = self.artifact_paths(job_id)
        job["artifacts"] = {}
        for name, path in paths.items():
            try:
                size = path.stat().st_size if path.is_file() else None
            except OSError:
                size = None
            job["artifacts"][name] = {"available": size is not None, "path": str(path), "bytes": size}
        try:
            job["report"] = json.loads(paths["report_json"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            job["report"] = None
        try:
            job["report_markdown"] = paths["report_markdown"].read_text(encoding="utf-8")[:200_000]
        except OSError:
            job["report_markdown"] = None
        try:
            log_text = paths["log"].read_text(encoding="utf-8", errors="replace")
            job["log_tail"] = log_text[-50_000:]
        except OSError:
            log_text = ""
            job["log_tail"] = ""
        # What retention removed of this run, and when: said on the page
        # rather than left as captures that silently are not there.
        job["evidence_removed"] = self.store.evidence_removals(job_id)
        if not job["progress"] and job["kind"] in ("build", "suite"):
            stage_specs = [("build", "Build artifacts")]
            if job["kind"] == "suite":
                stage_specs.extend(
                    [
                        ("discover", "Discover hardware"),
                        ("flash", "Flash devices"),
                        ("preflight", "Verify flashed devices"),
                        ("test", "Run validation"),
                        ("report", "Generate report"),
                    ]
                )
            inferred = []
            for name, label in stage_specs:
                passed = {
                    "build": paths["manifest"].is_file(),
                    "discover": "Flashing " in log_text,
                    "flash": " -m pytest " in log_text,
                    "preflight": " -m pytest " in log_text,
                    "test": job["status"] == "passed",
                    "report": paths["report_json"].is_file(),
                }[name]
                status = "passed" if passed else "pending"
                if name == "test" and job["status"] == "failed" and paths["junit"].is_file():
                    status = "failed"
                inferred.append(
                    {
                        "name": name,
                        "label": label,
                        "status": status,
                        "summary": "Inferred from legacy run artifacts",
                    }
                )
            job["progress"] = inferred
        if job["kind"] in ("build", "suite"):
            job["bundle"] = self._job_bundle(job)
        return job

    # ---- the artifact store ------------------------------------------------
    # Every bundle on disk, listed with what an operator needs to keep it or
    # let it go, retrievable whole, and removable without leaving a run that
    # reused it pointing at nothing. The filesystem half is
    # alteriom_hil.artifact_store; what a bundle is *for* -- the job that
    # built it, its profile, who reused it, whether anything still needs it --
    # is this service's knowledge and joined in here.

    @property
    def artifact_root(self) -> Path:
        return self.state / "artifacts"

    def _artifact_guard(self) -> threading.Lock:
        lock = self.__dict__.get("_artifact_lock")
        if lock is None:
            lock = self.__dict__.setdefault("_artifact_lock", threading.Lock())
        return lock

    def _artifact_claim(self, timeout: float = 5.0) -> threading.Lock:
        """Take the artifact lock the way a request must, and return it held.

        A prune holds it for as long as it deletes, which can be
        minutes; a request that waited that long would be answered by
        the proxy's timeout instead. So a request waits a moment and
        then says the store is busy, which is something to act on.

        The lock comes back held because a confirmed prune hands it to the
        thread that does the deleting -- releasing it is that thread's to
        do, not this caller's."""
        lock = self._artifact_guard()
        if not lock.acquire(timeout=timeout):
            raise ArtifactProtected(
                "the artifact store is busy -- a prune is deleting; try again in a moment"
            )
        return lock

    @contextlib.contextmanager
    def _artifact_hold(self, timeout: float = 5.0):
        """`_artifact_claim` for the ordinary case: held for one block."""
        lock = self._artifact_claim(timeout)
        try:
            yield
        finally:
            lock.release()

    def _job_bundle(self, job: dict) -> dict:
        """Which bundle a run flashed, whether it is still on disk, and if not,
        when and why it went -- so a run whose images were pruned says so.

        A run that reused a bundle records it on its build stage when it
        chooses it, so a run that failed or was cancelled -- whose result
        carries no `reused_artifacts_from` -- still names its bundle, and
        still finds that bundle's removal record after the link to it is
        gone. A passed run also has it in its result; a run from before the
        stage recorded it has only its artifacts link, which is followed."""
        build = next((stage for stage in job.get("progress") or [] if stage.get("name") == "build"), {})
        bundle_id = (job.get("result") or {}).get("reused_artifacts_from") or build.get("bundle")
        if not bundle_id:
            bundle_id = artifact_store.link_target(self.artifact_root, job["id"]) or job["id"]
        record = self.store.artifact_record(bundle_id) or {}
        available = artifact_store.load_bundle(self.artifact_root, bundle_id) is not None
        return {
            "id": bundle_id,
            "available": available,
            "removed_at": None if available else record.get("removed_at"),
            "removed_reason": None if available else record.get("removed_reason"),
            # Where the images came from, so the run can say "supplied by
            # <repo> CI run N" instead of "the bundle run X built" about a run
            # that never existed -- which is what every supplied bundle said.
            "source": self.bundle_provenance(bundle_id) if available else None,
        }

    # ---- bundles supplied by a producer ----------------------------------

    @staticmethod
    def _link_artifacts(artifact_dir: Path, source_dir: Path) -> None:
        """Point this run's artifact directory at a bundle it did not build."""
        artifact_dir.parent.mkdir(parents=True, exist_ok=True)
        if artifact_dir.is_symlink() or artifact_dir.exists():
            if artifact_dir.is_symlink():
                artifact_dir.unlink()
            else:
                shutil.rmtree(artifact_dir)
        os.symlink(source_dir, artifact_dir, target_is_directory=True)

    def bundle_provenance(self, bundle_id: str) -> dict | None:
        """What a bundle says about where it came from, or None for a farm build."""
        try:
            record = json.loads(
                (self.artifact_root / bundle_id / "provenance.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None
        return record if isinstance(record, dict) else None

    @staticmethod
    def _supply_origin(provenance: dict) -> str:
        """One phrase naming the producer, for a stage summary."""
        repo = str(provenance.get("repo") or "").rstrip("/")
        name = repo.rsplit("/", 1)[-1] or "a producer"
        run = provenance.get("run_id")
        return f"{name} CI run {run}" if run else f"{name} CI"

    # The rules a bundle archive must obey are the manifest's business, and
    # a rig reads them too now (alteriom_hil.artifacts.extract_bundle).
    _extract_bundle = staticmethod(extract_bundle)

    def accept_bundle(self, fields: dict, body: bytes) -> dict:
        """Take a bundle a producer built, if the profile named that producer.

        The farm accepts only what it can check: the repository and workflow
        the profile declares, a manifest that passes the same verification a
        farm build's does (`load_artifacts`: every checksum, every component
        at its offset), the commit the producer says it built, and -- when the
        manifest carries one -- the HIL agent the deployed suite speaks to. A
        bundle built from another farm checkout would otherwise run one
        protocol against another.
        """
        profile = str(fields.get("profile") or self.default_profile)
        spec = self.profiles.get(profile)
        if spec is None:
            raise ValueError(f"unsupported validation profile: {profile}")
        if not spec.accepts_supplied_bundles:
            raise ValueError(f"profile {profile} declares no producer to take bundles from")
        repo = _https_repo(str(fields.get("repo") or ""))
        workflow = str(fields.get("workflow") or "").strip()
        run_id = str(fields.get("run_id") or "").strip()
        run_url = str(fields.get("run_url") or "").strip()
        commit = str(fields.get("commit") or "").strip().lower()
        # What a person looking at the bundle wants to know and a commit does
        # not say: the branch it was built from and who started the run.
        # Optional -- a producer that cannot say is still a producer -- and
        # bounded, because they are shown.
        branch = str(fields.get("branch") or "").strip() or None
        actor = str(fields.get("actor") or "").strip() or None
        if branch is not None and not REF_PATTERN.fullmatch(branch):
            raise ValueError("branch must be a branch name without shell metacharacters")
        if actor is not None and not ACTOR_PATTERN.fullmatch(actor):
            raise ValueError("actor must be a GitHub login")
        if repo != _https_repo(spec.supply_repo):
            raise ValueError(
                f"{profile} takes bundles from {spec.supply_repo}, not "
                f"{repo or 'an unnamed repository'}"
            )
        if workflow != spec.supply_workflow:
            raise ValueError(
                f"{profile} takes bundles from {spec.supply_workflow}, not "
                f"{workflow or 'an unnamed workflow'}"
            )
        if not SUPPLY_RUN_PATTERN.fullmatch(run_id):
            raise ValueError("run_id must be the producing run's numeric id")
        if run_url and not SUPPLY_URL_PATTERN.fullmatch(run_url):
            raise ValueError("run_url must be an https URL")
        if not SHA_PATTERN.fullmatch(commit):
            raise ValueError("commit must be the 40-character revision the bundle was built from")

        bundle_id = uuid.uuid4().hex
        # Staged under a name the store ignores (it lists 32-hex ids only), so
        # a half-received bundle is never listed, flashed or pruned.
        staging = self.artifact_root / f".incoming-{bundle_id}"
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        try:
            self._extract_bundle(body, staging)
            manifest = load_artifacts(staging)
            revision = str(manifest.get(spec.revision_key) or "")
            if revision.lower() != commit:
                raise ValueError(
                    f"the bundle was built from {revision or 'no recorded revision'}, not {commit}"
                )
            agent = manifest.get("hil_agent_sha")
            expected = self.expected_agent_sha(profile)
            if agent and expected and agent != expected:
                raise ValueError(
                    "the bundle was built against a different HIL agent than this farm runs"
                )
            families = sorted(self._manifest_targets(manifest))
            if not families:
                raise ValueError("the bundle carries no families")
            provenance = {
                "kind": "supplied",
                "profile": profile,
                "repo": repo,
                "workflow": workflow,
                "run_id": run_id,
                "run_url": run_url or None,
                "commit": commit,
                "branch": branch,
                "actor": actor,
                "received_at": utcnow(),
            }
            (staging / "provenance.json").write_text(
                json.dumps(provenance, indent=2, sort_keys=True), encoding="utf-8"
            )
            staging.rename(self.artifact_root / bundle_id)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self._storage_changed()
        return {
            "id": bundle_id,
            "profile": profile,
            "revision": revision,
            "families": families,
            "source": provenance,
        }

    def _check_supplied_bundle(self, bundle_id: str, spec, sha: str | None, targets: list) -> None:
        """Is this bundle the one this run may flash?"""
        bundle = artifact_store.load_bundle(self.artifact_root, bundle_id)
        if bundle is None or bundle.manifest is None:
            raise ValueError(f"no bundle {bundle_id[:8]}; upload it before naming it")
        manifest = bundle.manifest
        revision = str(manifest.get(spec.revision_key) or "")
        if sha and revision.lower() != sha.lower():
            raise ValueError(
                f"bundle {bundle_id[:8]} was built from "
                f"{revision or 'no recorded revision'}, not {sha}"
            )
        agent = manifest.get("hil_agent_sha")
        expected = self.expected_agent_sha(spec.name)
        if agent and expected and agent != expected:
            raise ValueError(
                f"bundle {bundle_id[:8]} was built against a different HIL agent "
                f"than this farm runs"
            )
        missing = sorted(set(targets) - set(self._manifest_targets(manifest)))
        if missing:
            raise ValueError(f"bundle {bundle_id[:8]} has no {', '.join(missing)}")

    @staticmethod
    def _manifest_targets(manifest: dict | None) -> dict[str, dict]:
        """The manifest's targets that are shaped like targets. A manifest is
        whatever a build wrote; one malformed entry must not take down the
        listing that is the way to delete it."""
        targets = (manifest or {}).get("targets")
        if not isinstance(targets, dict):
            return {}
        return {name: entry for name, entry in targets.items() if isinstance(entry, dict)}

    def _bundle_identity(
        self, bundle, job: dict | None, provenance: dict | None = None,
    ) -> tuple[str | None, object, str | None]:
        """The profile a bundle was built for, its spec, and the commit it
        carries under that profile's revision key.

        A farm build's bundle is named for the job that built it, and that
        job's request says which profile. A supplied bundle has no such job
        -- it is named at upload -- and until this read its provenance, every
        one of them was listed as project "unknown", with no repository and
        no branch. Once nothing built on the rig that was every new bundle:
        the canary, the reference suite and every consumer's alike.
        """
        request = (job or {}).get("request") or {}
        profile = request.get("profile", DEFAULT_PROFILE) if job else None
        if profile is None and provenance:
            named = provenance.get("profile")
            profile = named if isinstance(named, str) and named in self.profiles else None
        spec = self.profiles.get(profile) if profile else None
        manifest = bundle.manifest or {}
        revision = manifest.get(spec.revision_key) if spec else None
        if not isinstance(revision, str) or not revision:
            # A bundle whose job is gone still carries its commit; say which,
            # rather than nothing.
            revision = next(
                (value for key, value in sorted(manifest.items())
                 if key.endswith("_sha") and key != "hil_agent_sha" and isinstance(value, str)),
                None,
            )
        return profile, spec, revision

    def _bundle_entry(
        self, bundle, jobs: dict, reusers: list[str], record: dict | None,
        disk_bytes: int | None = None, agents: dict | None = None,
    ) -> dict:
        job = jobs.get(bundle.id)
        request = (job or {}).get("request") or {}
        provenance = self.bundle_provenance(bundle.id)
        profile, spec, revision = self._bundle_identity(bundle, job, provenance)
        # The run that first had a supplied bundle in hand is the nearest thing
        # it has to the job that built one: it was dispatched for a branch and
        # by somebody, and a bundle that arrived before provenance carried
        # either can still say so through it.
        first_user = None
        if job is None:
            earliest = [jobs.get(link) for link in reusers]
            earliest = sorted((item for item in earliest if item), key=lambda item: item["created_at"])
            first_user = earliest[0] if earliest else None
        first_request = (first_user or {}).get("request") or {}
        origin = provenance or {}
        families = []
        for name, entry in sorted(self._manifest_targets(bundle.manifest).items()):
            image = entry.get("image") if isinstance(entry.get("image"), str) else None
            families.append({
                "family": name,
                # As the manifest spells it, and as the bundle keys the file:
                # `spare/../firmware.bin` is held, served and archived as
                # `firmware.bin`, and `path` is what to ask the file route for.
                "image": image,
                "path": bundle.keys.get(image),
                "image_bytes": bundle.files.get(bundle.keys.get(image) or ""),
                "environment": entry.get("environment") or entry.get("platformio_env"),
                "board": entry.get("board"),
                "app": entry.get("app") if isinstance(entry.get("app"), dict) else None,
            })
        users = [item for item in [job, *(jobs.get(link) for link in reusers)] if item]
        # Last use is when a run last had the bundle in hand -- when it
        # finished with it, or started, not when it was queued: a run that sat
        # a day in a paused queue did not leave a day-old bundle behind. The
        # bundle's own write time is the floor.
        last_used = artifact_store.latest(
            bundle.modified,
            *(item.get("finished_at") or item.get("started_at") or item["created_at"] for item in users),
        )
        record = record or {}
        declared_agent = (bundle.manifest or {}).get("hil_agent_sha")
        # The agent its own profile's bundles are held to -- worked out once
        # per profile for a whole listing (`agents`), since it reads source.
        expected_agent = None
        if declared_agent:
            agents = agents if agents is not None else {}
            if profile not in agents:
                agents[profile] = self.expected_agent_sha(profile)
            expected_agent = agents[profile]
        return {
            "id": bundle.id,
            "profile": profile,
            # The profile's label as it is now, so a renamed project is
            # renamed in its history too; the recorded one for a profile gone.
            "project": (spec.label if spec else None) or request.get("project"),
            "repo": request.get("repo") or (_https_repo(spec.repo) if spec else None),
            "ref": request.get("ref") or first_request.get("ref"),
            "branch": request.get("branch") or origin.get("branch") or first_request.get("branch"),
            # Who started the run it came from. Absent on bundles received
            # before the farm asked, and never guessed.
            "actor": request.get("actor") or origin.get("actor") or first_request.get("actor"),
            "revision": revision,
            # The version its build stamped, when it stamped one.
            "version": (bundle.manifest or {}).get("version")
            if isinstance((bundle.manifest or {}).get("version"), str) else None,
            "families": families,
            # Whether a run could flash it today. A bundle built against
            # another HIL agent speaks a protocol the deployed suite does not,
            # and a run naming it is refused -- so the run form does not offer
            # it. A bundle whose firmware carries no agent is not held to
            # one, and neither is a bundle of a profile that has none.
            "agent_current": not declared_agent or expected_agent is None
            or declared_agent == expected_agent,
            # What deleting it frees, which is the whole directory: the
            # largest of what was measured and what this listing can account
            # for. Either can be short of the other and neither can overstate
            # -- a measurement is the whole tree but may have crossed a
            # bundle mid-build, while an enumeration is of now but stops at
            # the hidden and the deeply nested -- so the larger is the closer.
            # The files below are what it lists; `remove` counts what went.
            "bytes": max(bundle.disk_bytes, disk_bytes or 0, bundle.bytes),
            "file_count": len(bundle.files),
            "manifest_valid": bundle.manifest is not None,
            "created_at": job["created_at"] if job else (origin.get("received_at") or bundle.modified),
            "last_used_at": last_used,
            # Where it came from: a farm build (none since artifact-first
            # step 5), or supplied by a producer's CI run, which names it.
            "source": provenance
            or {"kind": "farm-build", "job": bundle.id if job else None},
            "built_by": {"id": job["id"], "kind": job["kind"], "status": job["status"]} if job else None,
            "reused_by": sorted(
                ({"id": item["id"], "status": item["status"], "created_at": item["created_at"]}
                 for item in (jobs.get(link) for link in reusers) if item),
                key=lambda item: item["created_at"], reverse=True,
            ),
            # A queued or running job that built or linked this bundle is
            # using it; it cannot be deleted until that job is done.
            "held": any(item["status"] in ("queued", "running") for item in users),
            "pinned": bool(record.get("pinned_at")),
            "pinned_at": record.get("pinned_at"),
            "pin_note": record.get("pin_note"),
            # Brought with a node's history (history_artifact): shown, never
            # chosen to flash a new run.
            "imported_from": self._imported_from(bundle.id),
        }

    def _imported_from(self, entry_id: str) -> str | None:
        try:
            return json.loads((self.artifact_root / ".imported" / entry_id).read_text(encoding="utf-8")).get("from")
        except (OSError, json.JSONDecodeError, AttributeError):
            return None

    def _artifact_entries(self) -> tuple[list[dict], artifact_store.Scan]:
        # Enumerating each bundle's files is what a listing is for, and that
        # walk is capped. What a bundle costs on disk is a second, uncapped
        # walk of the same tree, so it comes from the measurement instead --
        # armed here, on a thread, and read as it was last measured.
        found = artifact_store.scan(self.artifact_root)
        self._measure_if_stale()
        measured = self._measured_bundle_bytes()
        jobs = self.store.many([*found.bundles, *found.links])
        records = self.store.artifact_records()
        # Once per profile per listing, not once per bundle: it reads the
        # agent's source.
        agents: dict = {}
        reusers: dict[str, list[str]] = {}
        for link, target in found.links.items():
            reusers.setdefault(target, []).append(link)
        entries = [
            self._bundle_entry(
                bundle, jobs, reusers.get(bundle.id, []), records.get(bundle.id),
                measured.get(bundle.id), agents,
            )
            for bundle in found.bundles.values()
        ]
        entries.sort(key=lambda entry: entry["created_at"] or "", reverse=True)
        return entries, found

    # A page of bundles, and the ceiling on one. The store grows without
    # bound -- 186 bundles and 2.2 GB in the first ten days -- and a client
    # that fetched every one to filter it in the browser would get slower
    # every week and then quietly stop showing the oldest, which is exactly
    # when someone is looking for an old one. Same shape as the job history.
    ARTIFACT_PAGE_DEFAULT = 25
    ARTIFACT_PAGE_MAX = 200

    def artifact_index(self, limit: int | None = None, offset: int = 0,
                       search: str | None = None, profile: str | None = None,
                       branch: str | None = None) -> dict:
        """Every bundle the farm holds, a page at a time.

        The totals -- how many bundles, how many bytes, how many pinned --
        are of the **whole store**, not of the page or of the filter: they
        are the disk story, and a page of five saying "5 bundles, 80 MB"
        would answer a question nobody asked. `matched` is what the filter
        matched, which is what the pager counts through.

        Filtering here rather than in the browser for the same reason it is
        paged here: the client cannot filter what it has not fetched, and
        fetching everything is the thing being avoided.
        """
        entries, found = self._artifact_entries()
        limit = self.ARTIFACT_PAGE_DEFAULT if limit is None else limit
        limit = max(1, min(limit, self.ARTIFACT_PAGE_MAX))
        offset = max(0, min(int(offset), MAX_PAGE_OFFSET))
        term = (search or "").strip().lower()
        chosen = [
            entry for entry in entries
            if (not profile or entry.get("profile") == profile)
            # "-" is the builds that name no branch.
            and (branch is None or (entry.get("branch") or "-") == branch)
            and (not term or any(
                term in str(entry.get(field) or "").lower()
                for field in ("id", "profile", "project", "branch", "ref", "revision", "actor", "pin_note")
            ))
        ]
        return {
            "bundles": chosen[offset:offset + limit],
            # The page, so a client need not infer what it asked for.
            "limit": limit,
            "offset": offset,
            # What the filter matched, and what the store holds.
            "matched": len(chosen),
            "count": len(entries),
            "bytes": sum(entry["bytes"] for entry in entries),
            "profiles": sorted({
                entry["profile"] for entry in entries if entry.get("profile")
            }),
            "links": len(found.links),
            "dangling": found.dangling,
            "pinned": sum(1 for entry in entries if entry["pinned"]),
            # What a prune is doing, or what the last one did: a confirmed
            # prune answers before it has finished deleting.
            "pruning": self.prune_progress(),
        }

    # ---- the library: the store by project and branch -----------------------------------
    # A flat list of every bundle grows with every CI run and says little: what
    # an operator asks is "what is the latest build of each thing, did it
    # pass, and how much is old". Grouped here, from the same entries the
    # list pages.

    @staticmethod
    def _runnable(entry: dict) -> bool:
        return bool(entry.get("manifest_valid") and entry.get("revision") and entry.get("agent_current")
                    and not entry.get("imported_from"))

    @staticmethod
    def _last_run(entry: dict) -> dict | None:
        runs = list(entry.get("reused_by") or [])
        if entry.get("built_by"):
            runs.append({**entry["built_by"], "created_at": entry.get("created_at")})
        runs = [run for run in runs if run.get("created_at")]
        return max(runs, key=lambda run: run["created_at"]) if runs else None

    def artifact_library(self) -> dict:
        """Every project's bundles by branch: the newest build of each, the
        newest one a run could flash, the last run on any of them, what they
        hold, and how much of it is older builds nothing pins or holds."""
        entries, _ = self._artifact_entries()
        projects: dict[str, dict] = {}
        for entry in entries:  # newest first
            profile = entry.get("profile") or "unknown"
            project = projects.setdefault(profile, {
                "profile": profile, "project": entry.get("project") or profile, "repo": entry.get("repo"),
                "bundles": 0, "bytes": 0, "pinned": 0, "branches": {},
            })
            branch = entry.get("branch") or "-"
            group = project["branches"].setdefault(branch, {
                "branch": entry.get("branch"), "key": branch, "bundles": 0, "bytes": 0, "pinned": 0,
                "latest": None, "runnable": None, "last_run": None, "last_used_at": None, "families": [],
                "older_bytes": 0, "older": 0,
            })
            summary = {key: entry.get(key) for key in (
                "id", "revision", "created_at", "actor", "bytes", "pinned", "held", "imported_from",
                "agent_current", "manifest_valid", "last_used_at", "source")}
            summary["families"] = [family["family"] for family in entry.get("families") or []]
            summary["runs"] = (1 if entry.get("built_by") else 0) + len(entry.get("reused_by") or [])
            summary["last_run"] = self._last_run(entry)
            for bucket in (project, group):
                bucket["bundles"] += 1
                bucket["bytes"] += entry["bytes"]
                bucket["pinned"] += 1 if entry.get("pinned") else 0
            if group["latest"] is None:
                group["latest"] = summary
            elif not entry.get("pinned") and not entry.get("held"):
                group["older"] += 1
                group["older_bytes"] += entry["bytes"]
            if group["runnable"] is None and self._runnable(entry):
                group["runnable"] = summary
            run = summary["last_run"]
            if run and (group["last_run"] is None or run["created_at"] > group["last_run"]["created_at"]):
                group["last_run"] = run
            if entry.get("last_used_at") and (group["last_used_at"] or "") < entry["last_used_at"]:
                group["last_used_at"] = entry["last_used_at"]
            for family in summary["families"]:
                if family not in group["families"]:
                    group["families"].append(family)
        listed = []
        for project in projects.values():
            branches = sorted(project["branches"].values(),
                              key=lambda group: group["latest"]["created_at"] or "", reverse=True)
            listed.append({**project, "branches": branches,
                           "older": sum(group["older"] for group in branches),
                           "older_bytes": sum(group["older_bytes"] for group in branches),
                           "latest_at": branches[0]["latest"]["created_at"] if branches else None})
        listed.sort(key=lambda project: project["latest_at"] or "", reverse=True)
        return {
            "projects": listed,
            "count": len(entries),
            "bytes": sum(entry["bytes"] for entry in entries),
            "pinned": sum(1 for entry in entries if entry.get("pinned")),
            "older": sum(project["older"] for project in listed),
            "older_bytes": sum(project["older_bytes"] for project in listed),
            # A prune under way, as the list says it: the library tab follows
            # one without a second scan of the store.
            "pruning": self.prune_progress(),
        }

    def artifact_detail(self, bundle_id: str) -> dict:
        if not artifact_store.BUNDLE_ID.fullmatch(bundle_id or ""):
            raise ValueError("invalid bundle id")
        entries, found = self._artifact_entries()
        entry = next((item for item in entries if item["id"] == bundle_id), None)
        if entry is None:
            raise KeyError(bundle_id)
        bundle = found.bundles[bundle_id]
        checksums = {}
        for name, target in self._manifest_targets(bundle.manifest).items():
            named = target.get("image")
            image = bundle.keys.get(named) if isinstance(named, str) else None
            if image:
                checksums[image] = target.get("sha256")
            # A target may ship an OTA image beside its merged flash image;
            # load_artifacts checks that file against the manifest before
            # flashing, so the detail shows what it is checked against
            # rather than leaving it looking unverifiable.
            ota = target.get("ota")
            if isinstance(ota, dict):
                ota_named = ota.get("image")
                ota_image = bundle.keys.get(ota_named) if isinstance(ota_named, str) else None
                if ota_image:
                    checksums.setdefault(ota_image, ota.get("sha256"))
            components = target.get("files")
            for filename, meta in (components.items() if isinstance(components, dict) else ()):
                component = bundle.keys.get(f"{name}/{filename}")
                if component and isinstance(meta, dict):
                    checksums.setdefault(component, meta.get("sha256"))
        entry["files"] = [
            {"path": path, "bytes": size, "sha256": checksums.get(path)}
            for path, size in sorted(bundle.files.items())
        ]
        entry["manifest"] = bundle.manifest
        return entry

    def _refuse_unless_whole(self, bundle_id: str) -> None:
        """A build writes its images into the bundle one at a time and
        writes the manifest last, so what is there before the build stage
        finishes is a partial, unflashable bundle -- whether the build is
        still running or exited nonzero and left its half behind. Such a
        directory is listed (an operator should see it, and be able to
        delete it) but not handed over.

        What settles it is the build stage, not the job's status: a run
        that failed its tests, or was cancelled after building, has a
        whole bundle and every reason to hand it over -- that bundle is
        the evidence. A directory no job made is nobody's half-build and
        is left alone."""
        job = self.store.get(bundle_id)
        if not job:
            return
        stages = job.get("progress") or []
        build = next((stage for stage in stages if stage.get("name") == "build"), None)
        if build is None or build.get("status") in ("passed", "skipped"):
            return
        raise ArtifactProtected(
            f"bundle {bundle_id[:8]} is still being built; it is not a whole bundle yet"
            if job["status"] in ("queued", "running")
            else f"bundle {bundle_id[:8]} was left by a build that did not finish"
            f" ({build.get('status')}); it is not a whole bundle"
        )

    def artifact_file(self, bundle_id: str, relative: str) -> tuple[Path, str, str]:
        bundle = artifact_store.load_bundle(self.artifact_root, bundle_id)
        if bundle is None:
            raise KeyError(bundle_id)
        self._refuse_unless_whole(bundle_id)
        path = artifact_store.file_path(bundle, relative)
        content_type = "application/json" if relative.endswith(".json") else "application/octet-stream"
        # A manifest may name a file anything; the download header is not the
        # place for its quotes or spaces.
        filename = re.sub(r"[^A-Za-z0-9._-]", "-", f"{bundle_id[:8]}-{relative}")[:200]
        return path, content_type, filename

    def artifact_archive(self, bundle_id: str) -> tuple[bytes, str]:
        """The bundle as a .tar.gz, named for what it is: project, commit,
        bundle. Extracted, it is a directory the flasher takes as it is."""
        bundle = artifact_store.load_bundle(self.artifact_root, bundle_id)
        if bundle is None:
            raise KeyError(bundle_id)
        self._refuse_unless_whole(bundle_id)
        profile, _spec, revision = self._bundle_identity(
            bundle, self.store.get(bundle_id), self.bundle_provenance(bundle_id)
        )
        top = self._archive_top(profile, revision, bundle_id)
        return artifact_store.archive(bundle, top), f"{top}.tar.gz"

    @staticmethod
    def _archive_top(profile: str | None, revision: str | None, bundle_id: str) -> str:
        """`<profile>-<commit>-<bundle>`, always a valid archive directory name.

        A profile name may be as long as the name limit itself, so the
        profile part is what gets cut: the commit and the bundle id are what
        make the name mean one bundle, and they are kept whole."""
        def plain(text: str) -> str:
            return re.sub(r"[^A-Za-z0-9._-]", "-", text)

        suffix = "-" + "-".join(part for part in (plain(revision[:10]) if revision else "", bundle_id[:8]) if part)
        name = plain(profile or "bundle").lstrip("-._") or "bundle"
        return name[: artifact_store.FILE_NAME_MAX - len(suffix)] + suffix

    def pin_artifact(self, bundle_id: str, note: str | None = None) -> dict:
        if note is not None and (not isinstance(note, str) or not PIN_NOTE_PATTERN.fullmatch(note)):
            raise ValueError("note must be one line of at most 200 characters")
        # Under the lock a delete and a prune take: otherwise a pin could pass
        # its check while a delete that has already judged the bundle unpinned
        # removes it, and the operator's attempt to keep it would record a pin
        # on nothing.
        with self._artifact_hold():
            if artifact_store.load_bundle(self.artifact_root, bundle_id) is None:
                raise KeyError(bundle_id)
            self.store.pin_artifact(bundle_id, note or None)
        return self.artifact_detail(bundle_id)

    def unpin_artifact(self, bundle_id: str) -> dict:
        with self._artifact_hold():
            if artifact_store.load_bundle(self.artifact_root, bundle_id) is None:
                raise KeyError(bundle_id)
            self.store.unpin_artifact(bundle_id)
        return self.artifact_detail(bundle_id)

    def _remove_bundle(self, entry: dict, links: dict, reason: str) -> dict:
        removed = artifact_store.remove(self.artifact_root, entry["id"], links)
        self._storage_changed()
        try:
            self.store.record_artifact_removed(entry["id"], removed["bytes"], reason)
        except Exception as exc:  # a locked or broken job store, say
            # The images are already gone; what failed is the note saying so,
            # which is what a run that reused them shows instead of pointing
            # at nothing. That is worth reporting -- it must not read as a
            # bundle that was not deleted, because it was.
            removed["record_error"] = str(exc)
        return removed

    def delete_artifact(self, bundle_id: str, reason: str = "Deleted by an operator") -> dict:
        if not artifact_store.BUNDLE_ID.fullmatch(bundle_id or ""):
            raise ValueError("invalid bundle id")
        with self._artifact_hold():
            entries, found = self._artifact_entries()
            entry = next((item for item in entries if item["id"] == bundle_id), None)
            if entry is None:
                raise KeyError(bundle_id)
            if entry["pinned"]:
                raise ArtifactProtected(f"bundle {bundle_id[:8]} is pinned; unpin it first")
            if entry["held"]:
                raise ArtifactProtected(f"bundle {bundle_id[:8]} is in use by a queued or running job")
            return self._remove_bundle(entry, found.links, reason)

    def _prune_state(self) -> dict:
        state = self.__dict__.get("_prune")
        if state is None:
            state = self.__dict__.setdefault("_prune", {"lock": threading.Lock(), "active": None, "last": None})
        return state

    def prune_progress(self) -> dict:
        """What a prune is doing, or what the last one did: the dashboard
        follows this instead of waiting on the request that started it."""
        state = self._prune_state()
        with state["lock"]:
            return {
                "active": dict(state["active"]) if state["active"] else None,
                "last": dict(state["last"]) if state["last"] else None,
            }

    def prune_artifacts(self, request: dict, wait: bool = False) -> dict:
        """Delete the bundles a rule chooses, or say which it would.

        A dry run unless the request says otherwise: the answer to "what
        would this free?" is the one to read before anything is deleted.

        A confirmed prune should carry the `ids` its preview listed. It then
        deletes only those, and of those only the ones the rule still
        chooses: a bundle a run started using since the preview is spared,
        and one that became eligible since is not deleted unseen.

        Deleting runs on a thread and this answers what it started: a
        thousand bundles of rmtree on the Pi's SD card outlasts the sixty
        seconds nginx gives a request, and a timeout must not read as a
        prune that failed while it is in fact still deleting. Progress is
        `prune_progress()`, which the artifact index carries. `wait` deletes
        inline instead, for a caller that is not a web request.
        """
        unknown = set(request) - {"older_than_days", "keep_per_profile", "dry_run", "ids"}
        if unknown:
            raise ValueError(f"unknown request fields: {sorted(unknown)}")
        ids = request.get("ids")
        if ids is not None and (
            not isinstance(ids, list)
            or len(ids) > MAX_PRUNE_IDS
            or not all(isinstance(item, str) and artifact_store.BUNDLE_ID.fullmatch(item) for item in ids)
        ):
            raise ValueError(f"ids must be a list of at most {MAX_PRUNE_IDS} bundle ids")
        rules = {}
        for name, ceiling in (("older_than_days", 3650), ("keep_per_profile", 1000)):
            value = request.get(name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= ceiling:
                raise ValueError(f"{name} must be a whole number from 0 to {ceiling}")
            rules[name] = value
        dry_run = request.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be true or false")
        if not dry_run and self.prune_progress()["active"]:
            # Said before the lock is asked for: the running prune holds
            # it, and waiting for it would answer with a timeout.
            raise ArtifactProtected("a prune is already running")
        # Held from here until the deleting is done -- handed to the thread
        # that deletes, which releases it. Anything else leaves a gap: a run
        # that takes a chosen bundle to reuse, or an operator who pins one,
        # between the choosing and the deleting would have that firmware
        # deleted anyway and its new link left pointing at nothing.
        lock = self._artifact_claim()
        handed_over = False
        try:
            entries, found = self._artifact_entries()
            chosen = artifact_store.select_prunable(entries, **rules)
            by_id = {entry["id"]: entry for entry in entries}
            spared: list[str] = []
            gone: list[str] = []
            if ids is not None:
                shown = set(ids)
                # Still here but not deleted, as against already deleted by
                # someone else since the preview: two different answers.
                spared = sorted((shown - set(chosen)) & set(by_id))
                gone = sorted(shown - set(by_id))
                chosen = [bundle_id for bundle_id in chosen if bundle_id in shown]
            selected = [by_id[bundle_id] for bundle_id in chosen]
            if dry_run:
                # A preview lists what a confirmation may send back: at most
                # MAX_PRUNE_IDS, the oldest first (select_prunable's order).
                # Past that it says how many more match, and the operator
                # prunes again for the rest -- rather than listing ids a
                # confirmation would be refused for sending.
                listed = selected[:MAX_PRUNE_IDS]
                return {
                    "dry_run": True,
                    "rules": rules,
                    "bundles": [
                        {key: entry[key] for key in ("id", "profile", "project", "revision", "bytes", "last_used_at")}
                        for entry in listed
                    ],
                    "bytes": sum(entry["bytes"] for entry in listed),
                    "matched": len(selected),
                    "matched_bytes": sum(entry["bytes"] for entry in selected),
                    "dangling": found.dangling,
                }
            reasons = []
            if "older_than_days" in rules:
                reasons.append(f"unused for more than {rules['older_than_days']} days")
            if "keep_per_profile" in rules:
                reasons.append(f"beyond the newest {rules['keep_per_profile']} per project")
            reason = "Pruned: " + " and ".join(reasons)
            state = self._prune_state()
            started = utcnow()
            with state["lock"]:
                if state["active"]:
                    raise ArtifactProtected("a prune is already running")
                state["active"] = {
                    "started_at": started, "total": len(selected), "removed": 0,
                    "bytes": 0, "spared": list(spared), "gone": list(gone), "errors": [],
                }

            def prune(held: threading.Lock) -> None:
                """Delete what was chosen, then let the store go.

                Called with the artifact lock already held -- the one the
                choosing took -- so no run can take a chosen bundle to reuse
                and no operator can pin one while this deletes, and what was
                chosen needs no re-deciding bundle by bundle. Releasing it is
                this function's last act, whatever happens."""
                removed: list = []
                freed = 0
                errors: list = []
                unlinked: list = []
                try:
                    for entry in selected:
                        try:
                            result = self._remove_bundle(entry, found.links, reason)
                        except Exception as exc:
                            # Not only OSError: anything this one bundle
                            # raises is this one bundle's failure. Letting it
                            # out would end the thread with the rest of the
                            # prune unattempted and the panel told the prune
                            # finished with nothing wrong.
                            errors.append({"id": entry["id"], "error": str(exc)})
                        else:
                            removed.append(result)
                            freed += result["bytes"]
                            if result.get("record_error"):
                                errors.append({
                                    "id": entry["id"],
                                    "error": f"deleted, but recording it failed: {result['record_error']}",
                                })
                        with state["lock"]:
                            if state["active"]:
                                state["active"].update(removed=len(removed), bytes=freed, errors=list(errors))
                    try:
                        unlinked = artifact_store.remove_dangling(self.artifact_root, found.dangling)
                    except Exception as exc:
                        errors.append({"id": None, "error": f"tidying reuse links failed: {exc}"})
                finally:
                    try:
                        with state["lock"]:
                            state["last"] = {
                                "started_at": started, "finished_at": utcnow(), "rules": rules,
                                "removed": removed, "bytes": freed, "dangling_removed": unlinked,
                                "spared": spared, "gone": gone, "errors": errors,
                            }
                            state["active"] = None
                    finally:
                        held.release()

            if wait:
                handed_over = True
                prune(lock)
                return {"dry_run": False, "pruning": False, **self.prune_progress()["last"]}
            worker = threading.Thread(target=prune, args=(lock,), name="artifact-prune", daemon=True)
            handed_over = True
            try:
                worker.start()
            except RuntimeError:
                # No thread to be had: delete here rather than leave the panel
                # waiting on a prune that never runs. The lock is still held,
                # and `prune` releases it either way.
                prune(lock)
                return {"dry_run": False, "pruning": False, **self.prune_progress()["last"]}
            return {
                "dry_run": False,
                "pruning": True,
                "rules": rules,
                "started_at": started,
                "selected": [entry["id"] for entry in selected],
                "bytes_selected": sum(entry["bytes"] for entry in selected),
                # Named by the preview but not deleted: held, pinned, or no
                # longer chosen by the rule at the moment of the delete.
                "spared": spared,
                # Named by the preview and already gone before this request.
                "gone": gone,
            }
        finally:
            if not handed_over:
                lock.release()

    # ---- storage ---------------------------------------------------------------
    # Nothing that walks a directory happens inside a request. The bundles and
    # the run evidence and logs the farm keeps for good are measured on a
    # thread, at most every STORAGE_CACHE_SECONDS or when something changed
    # them; nginx gives an API request sixty seconds, and a cold walk of the Pi
    # outlasts it. The panel is told what is still measuring.

    # What a measurement walks, beside the bundle store. There are no
    # toolchains here to measure: the farm does not build.
    MEASURED_DIRECTORIES = (("runs", "Run evidence"), ("logs", "Job logs"), ("workspaces", "Consumer checkouts"))

    def _storage_state(self) -> dict:
        state = self.__dict__.get("_storage")
        if state is None:
            state = self.__dict__.setdefault("_storage", {
                "lock": threading.Lock(), "measured": {}, "measured_at": None,
                # Bundle id -> bytes under its directory, from the same walk
                # that measures the store: what the artifact listing reports.
                "bundles": {},
                # Directory name -> its immediate children as measured, for
                # the detail page of each kind of storage.
                "children": {},
                "measured_mono": None, "measuring": False,
                # Bumped whenever something on disk went: a measurement that
                # started before that cannot call its own figures fresh.
                "generation": 0,
            })
        return state

    def _storage_changed(self) -> None:
        """Something was deleted: the next look at the panel measures again."""
        state = self._storage_state()
        with state["lock"]:
            state["generation"] += 1
            state["measured_mono"] = None

    def _measure_disk(self) -> None:
        """Walk everything the storage panel reports. Never on a request."""
        state = self._storage_state()
        with state["lock"]:
            generation = state["generation"]
        measured: dict = {}
        bundles: dict = {}
        children: dict = {}
        try:
            bundles, size, count = artifact_store.bundle_usage(self.artifact_root)
            measured["artifacts"] = {"bytes": size, "count": count}
            for name, _label in self.MEASURED_DIRECTORIES:
                found, size, files = artifact_store.child_usage(self.state / name)
                measured[name] = {"bytes": size, "files": files}
                children[name] = found
        finally:
            with state["lock"]:
                state["measured"] = measured
                state["bundles"] = bundles
                state["children"] = children
                state["measured_at"] = utcnow()
                # A delete or prune landed while this was walking: these
                # figures are already behind, so they do not count as fresh and
                # the next look measures again.
                state["measured_mono"] = time.monotonic() if state["generation"] == generation else None
                state["measuring"] = False

    def _measure_if_stale(self, fresh: bool = False, wait: bool = False) -> None:
        """Start a measurement if the figures are stale, and never two.

        Called by every request that reads a measured figure -- the storage
        panel and the artifact listing -- so a figure is at most
        STORAGE_CACHE_SECONDS behind without any request doing the walking.
        """
        state = self._storage_state()
        with state["lock"]:
            stale = (
                fresh or state["measured_mono"] is None
                or time.monotonic() - state["measured_mono"] > STORAGE_CACHE_SECONDS
            )
            start = stale and not state["measuring"]
            if start:
                state["measuring"] = True
        if not start:
            return
        if wait:
            self._measure_disk()
        else:
            threading.Thread(target=self._measure_disk, name="storage-measure", daemon=True).start()

    def _measured_bundle_bytes(self) -> dict:
        """What the last measurement found each bundle costs on disk."""
        state = self._storage_state()
        with state["lock"]:
            return dict(state["bundles"])


    # ---- statistics ---------------------------------------------------------

    STATS_MAX_DAYS = 90

    @staticmethod
    def _percentile(values: list, fraction: float):
        """Nearest-rank percentile, or None for no values. Nearest-rank
        answers with a duration that actually happened, which is the honest
        thing to put beside "the slowest tenth of runs took"."""
        if not values:
            return None
        ordered = sorted(values)
        rank = max(1, math.ceil(fraction * len(ordered)))
        return ordered[rank - 1]

    @staticmethod
    def _firmware_source(job: dict) -> str:
        """Where a run's firmware came from, from what the run recorded rather
        than from the wording of a summary: a supplied bundle is named in the
        request, a reused one on the build stage, and a farm build is a build
        stage that passed."""
        request = job.get("request") or {}
        build = next((stage for stage in job.get("progress") or [] if stage.get("name") == "build"), None)
        if request.get("artifact"):
            return "supplied"
        if build and build.get("bundle"):
            return "reused"
        if (job.get("result") or {}).get("reused_artifacts_from"):
            return "reused"
        # A run from before the build stage named its bundle said so only in
        # its summary. That wording is this service's own, so it is read as a
        # fallback for the history, never in place of what a run recorded.
        summary = str((build or {}).get("summary") or "")
        if summary.startswith("Supplied by"):
            return "supplied"
        if summary.startswith("Reused the artifacts run"):
            return "reused"
        status = (build or {}).get("status")
        if status == "passed":
            return "built"
        if status == "failed":
            return "build_failed"
        # Cancelled while queued, or failed before firmware was chosen.
        return "not_reached"

    def farm_statistics(self, days: int = 7, tz_offset_minutes: int = 0) -> dict:
        """What the farm has done over the last `days`, from its own history.

        Nothing here is new data: every job already records when it was
        asked for, started and finished, what it was for, and each stage's
        outcome. That answered "is the farm worth it" through the release
        reports and nothing else -- not "is it healthy this week", "is it
        getting slower", or "what fails". docs/product-gaps.md named the
        gap; this is the query.

        Suite runs are the unit: a discovery or a build job is not a verdict
        about firmware. The pass rate leaves cancelled runs out, because a
        run superseded by a newer commit said nothing either way. Days are
        bucketed in the caller's time zone (`tz_offset_minutes`, east
        positive), since "yesterday" on the dashboard is the operator's.
        """
        if not isinstance(days, int) or not 1 <= days <= self.STATS_MAX_DAYS:
            raise ValueError(f"days must be between 1 and {self.STATS_MAX_DAYS}")
        if not isinstance(tz_offset_minutes, int) or not -14 * 60 <= tz_offset_minutes <= 14 * 60:
            raise ValueError("tz_offset_minutes must be within fourteen hours of UTC")
        now = datetime.now(timezone.utc)
        local = timezone(timedelta(minutes=tz_offset_minutes))
        # Whole local days, today included: a seven-day window is today and
        # the six before it, so the first bar is never a partial day.
        first_day = (now.astimezone(local) - timedelta(days=days - 1)).date()
        start = datetime.combine(first_day, datetime.min.time(), tzinfo=local).astimezone(timezone.utc)
        jobs = self.store.since(start.isoformat())

        per_day = {
            (first_day + timedelta(days=offset)).isoformat(): {
                "passed": 0, "failed": 0, "cancelled": 0, "active": 0, "busy_seconds": 0.0,
            }
            for offset in range(days)
        }
        # Counted where they were asked for: a run created before the window
        # is here only for the rig time it spent inside it.
        inside = [job for job in jobs if datetime.fromisoformat(job["created_at"]) >= start]
        suites = [job for job in inside if job["kind"] == "suite"]
        totals = {"runs": len(suites), "passed": 0, "failed": 0, "cancelled": 0, "active": 0}
        waits, durations = [], []
        projects: dict = {}
        stages: dict = {}
        firmware: dict = {}
        for job in suites:
            status = job["status"]
            bucket = "active" if status in ("queued", "running") else status
            if bucket in totals:
                totals[bucket] += 1
            created = datetime.fromisoformat(job["created_at"]).astimezone(local).date().isoformat()
            if created in per_day and bucket in per_day[created]:
                per_day[created][bucket] += 1
            request = job.get("request") or {}
            profile = request.get("profile", DEFAULT_PROFILE)
            spec = self.profiles.get(profile)
            entry = projects.setdefault(profile, {
                "profile": profile,
                "project": (spec.label if spec else None) or request.get("project") or profile,
                "runs": 0, "passed": 0, "failed": 0, "cancelled": 0, "active": 0,
                "durations": [], "last_run_at": None, "last_status": None,
            })
            entry["runs"] += 1
            if bucket in entry:
                entry[bucket] += 1
            entry["last_run_at"], entry["last_status"] = job["created_at"], status
            if job.get("queued_seconds") is not None:
                waits.append(job["queued_seconds"])
            if status in ("passed", "failed") and job.get("duration_seconds") is not None:
                durations.append(job["duration_seconds"])
                entry["durations"].append(job["duration_seconds"])
            if status == "failed":
                failed = next((stage for stage in job.get("progress") or [] if stage.get("status") == "failed"), None)
                name = (failed or {}).get("name") or (job.get("result") or {}).get("failed_stage") or "unrecorded"
                label = (failed or {}).get("label") or name
                stage = stages.setdefault(name, {"stage": name, "label": label, "count": 0})
                stage["count"] += 1
            source = self._firmware_source(job)
            firmware[source] = firmware.get(source, 0) + 1

        # The rig is busy while a job holds the lock: suites, builds and
        # discovery all take it. Clipped to the window and merged, so a run
        # that started before the window counts only the part inside it.
        window_seconds = (now - start).total_seconds()
        intervals = []
        for job in jobs:
            if job["kind"] not in ("suite", "build", "inventory") or not job.get("started_at"):
                continue
            begin = max(datetime.fromisoformat(job["started_at"]), start)
            end = datetime.fromisoformat(job["finished_at"]) if job.get("finished_at") else now
            if end > begin:
                intervals.append((begin, min(end, now)))
        intervals.sort()
        merged = []
        for begin, end in intervals:
            if merged and begin <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((begin, end))
        busy = 0.0
        for begin, end in merged:
            busy += (end - begin).total_seconds()
            # Split across local days so the chart shows when the rig worked.
            cursor = begin
            while cursor < end:
                day = cursor.astimezone(local).date()
                next_midnight = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=local)
                piece_end = min(end, next_midnight.astimezone(timezone.utc))
                key = day.isoformat()
                if key in per_day:
                    per_day[key]["busy_seconds"] += (piece_end - cursor).total_seconds()
                cursor = piece_end

        def rate(passed, failed):
            return None if passed + failed == 0 else round(passed / (passed + failed), 4)

        by_project = []
        for entry in projects.values():
            values = entry.pop("durations")
            by_project.append({
                **entry,
                "pass_rate": rate(entry["passed"], entry["failed"]),
                "median_duration": statistics.median(values) if values else None,
                "p90_duration": self._percentile(values, 0.9),
            })
        by_project.sort(key=lambda item: item["runs"], reverse=True)

        health = self.load_board_health()
        connected = [board.get("id") for board in (self.inventory_snapshot().get("boards") or []) if board.get("id")]
        verdicts = {"passed": 0, "failed": 0, "unchecked": 0}
        last_checked = None
        for board in connected:
            record = health.get(board) or {}
            verdict = record.get("verdict")
            verdicts["passed" if verdict == "passed" else "failed" if verdict == "failed" else "unchecked"] += 1
            if record.get("checked_at") and (last_checked is None or record["checked_at"] > last_checked):
                last_checked = record["checked_at"]

        return {
            "window": {
                "days": days, "from": start.isoformat(), "to": now.isoformat(),
                "tz_offset_minutes": tz_offset_minutes,
            },
            "totals": {**totals, "pass_rate": rate(totals["passed"], totals["failed"])},
            "queue_wait": {
                "median": statistics.median(waits) if waits else None,
                "p90": self._percentile(waits, 0.9),
                "max": max(waits) if waits else None,
            },
            "duration": {
                "median": statistics.median(durations) if durations else None,
                "p90": self._percentile(durations, 0.9),
            },
            "utilisation": {
                "busy_seconds": round(busy, 1),
                "window_seconds": round(window_seconds, 1),
                "fraction": round(busy / window_seconds, 4) if window_seconds > 0 else None,
            },
            "per_day": [
                {"date": day, **{key: (round(value, 1) if key == "busy_seconds" else value)
                                 for key, value in figures.items()}}
                for day, figures in per_day.items()
            ],
            "by_project": by_project,
            "failed_stages": sorted(stages.values(), key=lambda item: item["count"], reverse=True),
            "firmware": firmware,
            "boards": {"connected": len(connected), **verdicts, "last_checked": last_checked},
            "discoveries": sum(1 for job in inside if job["kind"] == "inventory"),
        }

    STORAGE_PAGE_DEFAULT = 25
    STORAGE_PAGE_MAX = 200

    def storage_detail(
        self, kind: str, limit: int | None = None, offset: int = 0, wait: bool = False,
    ) -> dict:
        """What fills one kind of storage, largest first, a page at a time.

        The panel says run evidence is 2 GB; this says which runs. Every child
        of `runs/`, `logs/` and `workspaces/` is named for the job it belongs
        to, so each entry carries that job -- its project, branch, status,
        when -- and the page can link to the run. A child whose job has left
        the history is listed as an orphan, which is the kind of thing this
        page exists to find.

        Answered from the last measurement, like the panel: nothing here
        walks a directory. Firmware bundles have their own listing
        (`/api/v1/artifacts`).
        """
        names = dict(self.MEASURED_DIRECTORIES)
        if kind not in names and kind != "database":
            raise KeyError(kind)
        limit = self.STORAGE_PAGE_DEFAULT if limit is None else limit
        if not 1 <= limit <= self.STORAGE_PAGE_MAX:
            raise ValueError(f"limit must be between 1 and {self.STORAGE_PAGE_MAX}")
        if offset < 0:
            raise ValueError("offset must not be negative")
        state = self._storage_state()
        self._measure_if_stale(False, wait)
        with state["lock"]:
            measured = dict(state["measured"])
            children = list(state["children"].get(kind) or [])
            measuring = state["measuring"]
            measured_at = state["measured_at"]

        if kind == "database":
            path = self.state / "farm.sqlite3"
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            return {
                "kind": kind, "label": "Job history", "path": str(path), "bytes": size,
                **self.store.history_summary(), "by_status": self.store.counts_by_status(),
                "entries": [], "matched": 0, "limit": limit, "offset": offset,
                "measuring": measuring, "measured_at": measured_at,
            }

        label, path = names[kind], str(self.state / kind)
        figures = measured.get(kind) or {}
        bytes_, files = figures.get("bytes"), figures.get("files")

        children.sort(key=lambda item: (item.get("bytes") or 0, item["name"]), reverse=True)
        page = children[offset:offset + limit]
        # `<job id>` for a run's evidence or a checkout, `<job id>.log`.
        ids = {item["name"]: item["name"].split(".", 1)[0] for item in page}
        jobs = self.store.many(set(ids.values()))
        for item in page:
            job = jobs.get(ids[item["name"]])
            request = (job or {}).get("request") or {}
            item["job"] = None if job is None else {
                "id": job["id"], "kind": job["kind"], "status": job["status"],
                "created_at": job["created_at"], "profile": request.get("profile"),
                "project": (self.profiles[request["profile"]].label
                            if request.get("profile") in self.profiles else request.get("project")),
                "branch": request.get("branch"),
                "ref": request.get("ref"), "actor": request.get("actor"),
            }
        return {
            "kind": kind, "label": label, "path": path, "bytes": bytes_, "files": files,
            "entries": page, "matched": len(children), "limit": limit, "offset": offset,
            "measuring": measuring, "measured_at": measured_at,
        }

    def storage(self, fresh: bool = False, wait: bool = False) -> dict:
        """What uses the disk, by kind, against the host's thresholds.

        Answered from the last measurement, with `measuring` set while a new
        one runs on a thread; a figure never measured yet is None. Only the
        job-history file's size and the filesystem totals are read here --
        one stat each. `wait` measures inline instead, for a caller that is
        not a web request.
        """
        state = self._storage_state()
        if wait:
            # A measurement already running on a thread is waited out rather
            # than raced: two walks of the same directories answer nothing twice.
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                with state["lock"]:
                    if not state["measuring"]:
                        break
                time.sleep(0.05)
        self._measure_if_stale(fresh, wait)
        with state["lock"]:
            measured = dict(state["measured"])
            measuring = state["measuring"]
            measured_at = state["measured_at"]

        artifacts = measured.get("artifacts") or {}
        categories = [{
            "name": "artifacts",
            "label": "Firmware bundles",
            "path": str(self.artifact_root),
            "bytes": artifacts.get("bytes"),
            "count": artifacts.get("count"),
        }]
        for name, label in self.MEASURED_DIRECTORIES:
            figures = measured.get(name) or {}
            categories.append({
                "name": name, "label": label, "path": str(self.state / name),
                "bytes": figures.get("bytes"), "files": figures.get("files"),
            })
        try:
            database = (self.state / "farm.sqlite3").stat().st_size
        except OSError:
            database = None
        categories.append({"name": "database", "label": "Job history", "path": str(self.state / "farm.sqlite3"), "bytes": database})
        filesystem = {}
        try:
            usage = shutil.disk_usage(self.state)
            filesystem = {
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "used_percent": round(usage.used * 100 / usage.total, 1) if usage.total else None,
            }
        except OSError:
            pass
        try:
            from alteriom_hil import hil_config

            health = (hil_config.load_config() or {}).get("health") or {}
        except Exception:
            # No host configuration (a development checkout, a test): the
            # panel shows the figures without thresholds rather than failing.
            health = {}
        return {
            "computed_at": utcnow(),
            "measured_at": measured_at,
            "measuring": measuring,
            "filesystem": filesystem,
            "warn_percent": health.get("disk_warn_percent"),
            "critical_percent": health.get("disk_critical_percent"),
            "categories": categories,
        }


def _notify_channels_of(config: dict) -> list[dict]:
    """Every channel a host configuration describes, whether it holds one or
    several (hil_config.notify_channels). Imported where it is used so this
    module still loads on a host without the runner on its path."""
    try:
        from alteriom_hil import hil_config

        return [channel for channel in hil_config.notify_channels(config) if isinstance(channel, dict)]
    except Exception:
        notify = config.get("notify")
        if isinstance(notify, dict):
            return [{**notify, "id": str(notify.get("id") or "1")}]
        return [{**item, "id": str(item.get("id") or index)}
                for index, item in enumerate(notify or [], start=1) if isinstance(item, dict)]


def _delivery_summary(outcome: dict | None) -> str | None:
    if not outcome:
        return None
    result = "delivered" if outcome.get("ok") else f"failed: {outcome.get('error')}"
    return f"{outcome.get('event')} at {outcome.get('at')}, {result}"


def _backup_summary(outcome: dict | None) -> str | None:
    if not outcome:
        return None
    pushed = outcome.get("pushed")
    copy = "" if pushed is None else (f", copied to {pushed.get('target')}" if pushed.get("ok") else f", NOT copied: {pushed.get('error')}")
    return f"{outcome.get('created_at')}, {outcome.get('files')} files, {outcome.get('bytes')} bytes{copy}"


def _with_active(recent: list[dict], active: list[dict]) -> list[dict]:
    seen = {job["id"] for job in recent}
    return recent + [job for job in active if job["id"] not in seen]


class ApiRoute(NamedTuple):
    """A route a half adds to the API, and who it is for.

    `pattern` is matched against the whole path; any named groups in it are
    passed to `answers`, which is the name of a method on the manager.
    `audience` is `account` for a signed-in person, `admin` for the farm's
    own people -- and it is the whole permission: a route declared for an
    account is reachable by one, and `ACCOUNT_ROUTES` is not consulted.

    A write is given the request body as its first argument; a read is not
    given one. Either way the identity comes last, named, so a method can
    scope its answer to whoever asked.
    """

    method: str
    pattern: object
    audience: str
    answers: str


def make_handler(manager: BaseManager, keys: KeyStore | str, web_root: Path):
    # A bare token is the farm's own key and no other: what an older caller,
    # and most tests, pass.
    if isinstance(keys, str):
        keys = KeyStore(keys)
    # Every key this store makes -- a rig's at enrolment, an operator's --
    # is refused a name that is an account's handle: the two are one
    # namespace, and a key with an account's name would be that person.
    if getattr(keys, "reserved", None) is None and hasattr(manager, "account_handles"):
        keys.reserved = manager.account_handles

    # Refused join tokens by address, for the last ENROLL_FAILURE_WINDOW
    # seconds: a token has 192 random bits, so this is about noise in the
    # audit more than about guessing.
    enroll_failures: dict[str, list[float]] = {}
    enroll_lock = threading.Lock()
    # Sign-in links asked for, and GitHub sign-ins begun, by address: a
    # link is one mail to somebody, a start is a row and a redirect, and an
    # address asking for many of either in a few minutes is not a person.
    # Bounded (AddressThrottle): a refused caller cannot grow them.
    signin_requests = AddressThrottle()
    signin_starts = AddressThrottle()

    # What this farm's halves add to the API, asked for once. The service
    # knows its own routes and nothing about a half's: a half is a
    # distribution of its own now, and one that had to edit this file to
    # add a route would wait on a release of this one to ship it.
    #
    # Asked for rather than required: a manager that is not a BaseManager --
    # a test's stand-in, most often -- adds none, which is what it means, and
    # is served the routes the service has always had.
    declares = getattr(manager, "api_routes", None)
    extra_routes = tuple(declares() if callable(declares) else ())

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(web_root), **kwargs)

        def end_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            # no-referrer everywhere, except where a page asks for something
            # else by setting _referrer_policy first. One header, from here, so
            # a page cannot end up sending two that disagree. Reset afterwards
            # because a handler instance serves every request on a keep-alive
            # connection, and one page's choice is not the next page's.
            self.send_header("Referrer-Policy", getattr(self, "_referrer_policy", "no-referrer"))
            self._referrer_policy = "no-referrer"
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'")
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def _runs(self, identity) -> tuple[set[str] | None, str | None]:
            """Which runs this caller may read, as `FarmManager.run_scope`
            gives it: the rigs whose runs are theirs, and the name their own
            runs carry while they are queued and have no rig. Both halves
            from one place, because a call site that took the rigs and forgot
            the submitter would lose the caller's own queued runs.

            A manager without rigs to scope by (a test double, an older
            build) has no `run_scope`, and then this is the whole farm --
            the same fallback `_identity` makes for `session_account`. Safe
            because such a manager has no accounts either: the callers it
            answers are keys, which read everything by design."""
            scope = getattr(manager, "run_scope", None)
            return scope(identity) if scope is not None else (None, None)

        def _identity(self) -> Identity | None:
            supplied = self.headers.get("Authorization", "")
            if supplied.startswith("Bearer "):
                self._via_session = False
                self._account = None
                return keys.identify(supplied[7:])
            # No key: a browser with a session cookie, if it has one.
            token = self._session_cookie()
            # A manager without accounts (a test double, an older build) has
            # no sessions to ask about.
            ask = getattr(manager, "session_account", None)
            account = ask(token) if token and ask else None
            self._via_session = account is not None
            # Keep the row the session lookup already fetched: whoami and the
            # status page build their `you.account` from it rather than asking
            # -- and advancing last_seen_at -- a second time.
            self._account = account
            # kind="account": a person who signed in, not a key that was
            # issued. The views read that to decide whether this caller sees
            # the whole farm or their own workspace.
            return Identity(account["handle"], account["role"], kind="account") if account else None

        def _session_cookie(self) -> str | None:
            return self._cookie(SESSION_COOKIE)

        def _session_cookie_header(self, token: str | None) -> str:
            """The cookie to set: the session, or its removal. HttpOnly, so
            a script on the page never sees it; SameSite=Lax, so another
            site's form cannot carry it; Secure when the portal is https."""
            secure = "; Secure" if (os.environ.get("ALTERIOM_HIL_PUBLIC_URL") or "").startswith("https://") else ""
            if token is None:
                return f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{secure}"
            return (f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; "
                    f"HttpOnly; SameSite=Lax{secure}")

        def _redirect(self, location: str, *cookies: str, status: HTTPStatus = HTTPStatus.FOUND):
            self.send_response(status)
            self.send_header("Location", location)
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _oauth_cookie_header(self, nonce: str | None) -> str:
            """The nonce a browser holds between starting a GitHub sign-in
            and finishing it: ten minutes, only on the callback's path,
            HttpOnly, SameSite=Lax (a top-level arrival from GitHub still
            carries it). None removes it."""
            secure = "; Secure" if (os.environ.get("ALTERIOM_HIL_PUBLIC_URL") or "").startswith("https://") else ""
            if nonce is None:
                return f"{OAUTH_COOKIE}=; Path=/auth/github; Max-Age=0; HttpOnly; SameSite=Lax{secure}"
            return (f"{OAUTH_COOKIE}={nonce}; Path=/auth/github; Max-Age={BaseManager.OAUTH_STATE_MINUTES * 60}; "
                    f"HttpOnly; SameSite=Lax{secure}")

        def _cookie(self, name: str) -> str | None:
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except http.cookies.CookieError:
                return None
            morsel = jar.get(name)
            return morsel.value if morsel else None

        def _address(self) -> str:
            return self.headers.get("X-Real-IP") or self.client_address[0]

        # ---- signing in: no key, a person ---------------------------------------
        def _auth_get(self, path: str):
            query = parse_qs(urlparse(self.path).query)
            first = lambda name: (query.get(name) or [None])[0]  # noqa: E731
            if path == "/auth/options":
                return self._json(HTTPStatus.OK, manager.signin_options())
            if path == "/auth/github":
                # The allowance is read when asked, not when the handler was
                # made: a test lowers it, and so may an operator.
                if signin_starts.too_many(self._address(), SIGNIN_STARTS_ALLOWED):
                    return self._json(HTTPStatus.TOO_MANY_REQUESTS,
                                      {"error": "too many sign-ins started from here; wait a few minutes"})
                try:
                    url, nonce = manager.begin_github(first("next"))
                except ElsewhereError as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return self._redirect(url, self._oauth_cookie_header(nonce))
            if path == "/auth/github/callback":
                try:
                    token, where = manager.finish_github(first("state"), first("code"),
                                                         self._cookie(OAUTH_COOKIE), self._address(), keys)
                except (ValueError, farm_signin.SignInError, ElsewhereError, OSError, sqlite3.IntegrityError) as exc:
                    manager._event("farm", "account", f"a GitHub sign-in failed: {exc}", level="warn")
                    return self._redirect("/app?signin=failed", self._oauth_cookie_header(None))
                return self._redirect(where, self._session_cookie_header(token), self._oauth_cookie_header(None))
            return self._json(HTTPStatus.NOT_FOUND, {"error": "no such sign-in"})

        def _signin_cookie_header(self, nonce: str | None) -> str:
            """The nonce that ties a code to the browser that asked for it:
            sent back only under /auth/email, for as long as a code lives,
            never to script, and Lax, so a form on another site cannot
            present it."""
            secure = "; Secure" if (os.environ.get("ALTERIOM_HIL_PUBLIC_URL") or "").startswith("https://") else ""
            if nonce is None:
                return f"{SIGNIN_COOKIE}=; Path=/auth/email; Max-Age=0; HttpOnly; SameSite=Lax{secure}"
            return (f"{SIGNIN_COOKIE}={nonce}; Path=/auth/email; Max-Age={BaseManager.SIGNIN_CODE_MINUTES * 60}; "
                    f"HttpOnly; SameSite=Lax{secure}")

        def _auth_post(self, path: str):
            if path == "/auth/email/code":
                # The code, typed into the page that asked for it. The page
                # sends JSON, which a form on another site cannot; and the
                # browser sends the nonce it was given when it asked, which a
                # browser that did not ask does not have. Both, or a code
                # phished out of somebody could be typed anywhere.
                try:
                    payload = self._request_json_limit(1024)
                    token, where = manager.finish_signin_code(
                        payload.get("email"), payload.get("code"),
                        farm_signin.digest(self._cookie(SIGNIN_COOKIE) or ""), self._address(), keys)
                except json.JSONDecodeError as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except ValueError as exc:
                    return self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
                except (sqlite3.IntegrityError, ElsewhereError, OSError) as exc:
                    manager._event("farm", "account", f"an email sign-in failed: {exc}", level="warn")
                    return self._json(HTTPStatus.BAD_GATEWAY, {"error": "the sign-in could not be completed; try again"})
                body = json.dumps({"ok": True, "next": where}).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", self._session_cookie_header(token))
                self.send_header("Set-Cookie", self._signin_cookie_header(None))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/auth/email":
                address = self._address()
                if signin_requests.too_many(address, SIGNIN_REQUESTS_ALLOWED):
                    return self._json(HTTPStatus.TOO_MANY_REQUESTS,
                                      {"error": "too many sign-in codes asked for from here; wait a few minutes"})
                # A fresh nonce for this browser every time it asks: the code
                # is bound to it, and a browser that did not ask holds none.
                nonce = farm_signin.new_token()
                try:
                    payload = self._request_json_limit(1024)
                    manager.request_signin_code(payload.get("email"), address, payload.get("next"),
                                                farm_signin.digest(nonce))
                except ElsewhereError as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except (ValueError, json.JSONDecodeError) as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except (farm_signin.SignInError, OSError) as exc:
                    manager._event("farm", "account", f"a sign-in code could not be sent: {exc}", level="warn")
                    return self._json(HTTPStatus.BAD_GATEWAY, {"error": "the mail could not be sent; try again in a moment"})
                # The same answer whether or not the address is known.
                body = json.dumps({"sent": True}).encode()
                self.send_response(HTTPStatus.ACCEPTED)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", self._signin_cookie_header(nonce))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/auth/signout":
                manager.sign_out(self._session_cookie())
                body = json.dumps({"signed_out": True}).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", self._session_cookie_header(None))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self._json(HTTPStatus.NOT_FOUND, {"error": "no such sign-in"})

        def _authorized(self) -> bool:
            return self._identity() is not None

        def send_response(self, code, message=None):
            # The audit row is written here, before any of the answer leaves:
            # written after it, a caller that reads the audit straight away
            # could miss the change it had just been told was made.
            self._record_audit(int(code))
            super().send_response(code, message)

        def _record_audit(self, status: int):
            pending, self._pending_audit = getattr(self, "_pending_audit", None), None
            if pending is None:
                return
            identity, method, path = pending
            if identity.role == "node" and ROUTINE_WORKER_CALL.fullmatch(path):
                return
            try:
                manager.store.record_audit(
                    identity.name, identity.role, method, path, status,
                    self.headers.get("X-Real-IP") or self.client_address[0],
                )
            except Exception as exc:  # the answer stands; the record is lost, and says so
                sys.stderr.write(f"farm-api: audit record failed for {method} {path}: {exc}\n")

        def _forbidden(self, identity: Identity, method: str, path: str):
            if identity.role == "guest":
                return self._json(HTTPStatus.FORBIDDEN, {"error": (
                    f"{identity.name} is signed in as a guest: this farm has not opened your account yet. "
                    "The person who runs it can; you are in on your next sign-in."
                )})
            return self._json(HTTPStatus.FORBIDDEN, {"error": (
                f"{method} {path} needs an {required_role(method, path)} key; "
                f"{identity.name} is a {identity.role}"
            )})

        def _cookie_write_refused(self) -> bool:
            """A signed-in browser must carry a JSON body to write: a cookie
            rides every request the browser makes, a JSON body does not ride
            another site's form. True (and already answered) when refused.
            _identity() must have run, so `_via_session` is set."""
            if getattr(self, "_via_session", False) and not (self.headers.get("Content-Type") or "").startswith("application/json"):
                self._json(HTTPStatus.FORBIDDEN, {"error": "a signed-in browser sends JSON"})
                return True
            return False

        def _mutate(self, method: str, handler):
            """Authenticate, check the role, run the handler, and record it."""
            path = self._route()
            identity = self._identity()
            if identity is None:
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "bearer token required, or a signed-in browser"})
            if self._cookie_write_refused():
                return
            self._pending_audit = (identity, method, path)
            try:
                if role_allows(identity, method, path, extra=extra_routes):
                    handler(path, identity)
                else:
                    self._forbidden(identity, method, path)
            finally:
                # A handler that raised before answering is still recorded.
                self._record_audit(500)

        def _half_answer(self, method: str, path: str, identity, body=False):
            """A route a half declared, answered by the method it named.

            Returns True when it answered. Consulted after the service's own
            routes, so a half cannot shadow one of them by accident -- and the
            body is read only once a route has matched, because reading it for
            a route that did not leaves the next reader waiting for a stream
            that has already been consumed.
            """
            route = half_route(extra_routes, method, path)
            if route is None:
                return False
            answer = getattr(manager, route.answers)
            fields = (route.pattern.fullmatch(path) or {}).groupdict()
            try:
                given = (self._request_json_limit(64 * 1024) or {}) if body else None
                result = answer(given, identity=identity, **fields) if body \
                    else answer(identity=identity, **fields)
            except ElsewhereError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except PermissionError as exc:
                self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
            except LookupError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            else:
                self._json(HTTPStatus.OK, result)
            return True

        def _json(self, status: int, payload: object):
            body = json.dumps(payload, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, body: bytes, content_type: str, filename: str, attachment: bool | None = None):
            if attachment is None:
                attachment = content_type in ("application/octet-stream", "application/gzip")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f'{"attachment" if attachment else "inline"}; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def _route(self) -> str:
            """The request path, percent-decoded, for matching routes.

            The dashboard sends an artifact name through encodeURIComponent
            and nginx forwards the path as it came, so `serial:esp32-03`
            arrives as `serial%3Aesp32-03`. Matched undecoded, every name with
            a colon -- each serial log, each firmware image -- fell through to
            the static handler's 404. The route patterns stay as strict as
            they were; they now see the name that was meant."""
            return unquote(urlparse(self.path).path)

        def _request_json(self) -> dict:
            return self._request_json_limit(65536)

        def _site_page(self, name: str):
            """One page of the public site, or the dashboard, served whole.

            Served through here rather than as a file because a page's
            link-preview tags want an absolute URL, and a page cannot know
            its own origin. `no-cache`, so a redeploy is what the next
            visit sees.
            """
            try:
                page = (web_root / name).read_text(encoding="utf-8")
            except OSError:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "no such page"})
            origin = (os.environ.get("ALTERIOM_HIL_PUBLIC_URL") or "").rstrip("/")
            if not origin:
                # Behind the ingress the farm is https; the host is the one
                # the browser asked for, reduced to what a host can be, and
                # never a value from the page.
                host = re.sub(r"[^A-Za-z0-9.:-]", "", self.headers.get("Host") or "")
                local = host.startswith(("127.", "localhost"))
                origin = f"{'http' if local else 'https'}://{host}"
            body = page.replace("__ORIGIN__", origin).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _request_json_limit(self, limit: int) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length > limit:
                raise ValueError("request body is too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def do_GET(self):
            path = self._route()
            if path.startswith("/auth/"):
                return self._auth_get(path)
            if path == "/healthz":
                return self._json(HTTPStatus.OK, {"status": "ok"})
            # The public site at the root, the dashboard at /app. A trailing
            # slash is the same page; /world was the site's first address
            # and still lands, on the page it became.
            bare = path.rstrip("/") or "/"
            if bare in SITE_PAGES:
                # The site is the portal's, and its pages are in the portal's
                # half of the web root. A rig serves the rig's half alone, so
                # the page is not there and what the caller wanted at `/` is
                # the only thing a rig has: its own dashboard
                # (docs/public-release-plan.md, step 12d).
                page = SITE_PAGES[bare]
                if not (web_root / page).is_file():
                    return self._site_page("index.html")
                return self._site_page(page)
            if bare == "/app":
                return self._site_page("index.html")
            if bare in WORLD_MOVED:
                return self._redirect(WORLD_MOVED[bare], status=HTTPStatus.MOVED_PERMANENTLY)
            match = re.fullmatch(r"/world/brand/([a-z0-9-]+\.[a-z]+)", path)
            if match and match.group(1) in WORLD_BRAND:
                # The brand at its first open address. A NorthRelay email
                # template loads the lockup from /world/brand/logo-email.png,
                # and /world is the one path the ingress leaves open until
                # go-live -- so this serves the file, rather than redirecting
                # a mail client into a sign-in it cannot pass. Retire it with
                # the ingress path (docs/brand.md).
                self.path = "/brand/" + match.group(1)
                return super().do_GET()
            if path.startswith("/world/"):
                return self._json(HTTPStatus.NOT_FOUND, {"error": "the public site is at /"})
            if path == "/api/v1/world":
                return self._json(HTTPStatus.OK, manager.world_view())
            if public_route("GET", path):
                return self._join_script()
            identity = None
            if path.startswith("/api/"):
                identity = self._identity()
                if identity is None:
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "bearer token required"})
                if not role_allows(identity, "GET", path, extra=extra_routes):
                    return self._forbidden(identity, "GET", path)
            if path == "/api/v1/sessions":
                # Where this account is signed in. A key has no session and no
                # browsers, and says so rather than answering an empty list
                # that would read as "signed out everywhere".
                sessions = manager.account_sessions(self._session_cookie()) \
                    if getattr(self, "_via_session", False) else None
                if sessions is None:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "sessions belong to a signed-in account, not a key"})
                return self._json(HTTPStatus.OK, {"sessions": sessions, "days": BaseManager.SESSION_DAYS})
            if path == "/api/v1/whoami":
                you = {"name": identity.name, "role": identity.role}
                if getattr(self, "_via_session", False):
                    you["account"] = self._account
                if identity.is_admin and keys.error:
                    you["keys_error"] = keys.error
                return self._json(HTTPStatus.OK, you)
            if path == "/api/v1/audit":
                query = parse_qs(urlparse(self.path).query)
                try:
                    page = manager.store.audit_page(
                        int(query.get("limit", ["50"])[0]), int(query.get("offset", ["0"])[0])
                    )
                except ValueError:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "limit and offset must be integers"})
                return self._json(HTTPStatus.OK, page)
            if path == "/api/v1/capacity":
                return self._json(HTTPStatus.OK, manager.capacity())
            if path == "/api/v1/workers":
                if manager.__dict__.get("mode") != "portal":
                    return self._json(HTTPStatus.OK, {"mode": manager.__dict__.get("mode", "standalone"), "workers": []})
                return self._json(HTTPStatus.OK, {"mode": "portal", "workers": manager.workers_view()})
            if path == "/api/v1/releases":
                return self._json(HTTPStatus.OK, manager.release_index())
            if path == "/api/v1/releases/current":
                release = manager.current_release()
                if release is None:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "the portal has no current release"})
                return self._json(HTTPStatus.OK, release)
            if path == "/api/v1/rigs":
                if manager.__dict__.get("mode") != "portal":
                    return self._json(HTTPStatus.OK, {"rigs": [], "release": None})
                return self._json(HTTPStatus.OK, manager.rigs_view(keys, identity))
            match = re.fullmatch(r"/api/v1/rigs/([a-z0-9][a-z0-9._-]{0,31})", path)
            if match:
                # A rig outside this caller's workspace is not found rather
                # than forbidden: a 403 confirms the rig exists, and its name
                # is the one thing a stranger can guess.
                mine = manager.visible_rigs(identity)
                if mine is not None and match.group(1) not in mine:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such rig"})
                return self._worker_call(lambda: manager.rig_detail(match.group(1), keys, identity))
            match = re.fullmatch(r"/api/v1/workers/([a-z0-9][a-z0-9._-]{0,31})/commands", path)
            if match:
                # Everything ever asked of this rig, with the arguments and
                # who asked: the owner's, and theirs to read on their own rig.
                # A rig merely lent to this caller is not found, the answer
                # its detail gives, and its page does not offer the tab.
                if getattr(identity, "is_account", False)                         and not manager.may_manage_rig(match.group(1), identity):
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such rig"})
                query = parse_qs(urlparse(self.path).query)
                limit = query.get("limit", ["25"])[0]
                offset = query.get("offset", ["0"])[0]
                try:
                    limit, offset = int(limit), int(offset)
                except ValueError:
                    return self._json(HTTPStatus.BAD_REQUEST,
                                      {"error": "limit and offset must be integers"})
                return self._worker_call(lambda: manager.worker_commands(match.group(1), limit, offset))
            match = re.fullmatch(r"/api/v1/workers/([a-z0-9][a-z0-9._-]{0,31})", path)
            if match:
                try:
                    return self._json(HTTPStatus.OK, manager.worker_detail(match.group(1)))
                except ElsewhereError as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except LookupError as exc:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            if path == "/api/v1/keys":
                # Who holds a key, by name and role: never a key, never a digest.
                return self._json(HTTPStatus.OK, {"keys": keys.entries(), "error": keys.error})
            match = re.fullmatch(r"/api/v1/releases/([0-9a-f]{40})/bundle", path)
            if match:
                try:
                    release = manager.release_path(match.group(1))
                    body = release.read_bytes()
                except (LookupError, OSError):
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such release"})
                return self._send_file(body, "application/octet-stream", f"alteriom-esp32-farm-{match.group(1)[:12]}.bundle")
            match = re.fullmatch(r"/api/v1/releases/([0-9a-f]{40})/files/([A-Za-z0-9][A-Za-z0-9._-]{0,120})", path)
            if match:
                # One of a release's packages, as published beside its bundle.
                try:
                    body = manager.release_file_path(match.group(1), match.group(2)).read_bytes()
                except (LookupError, OSError):
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such release file"})
                kind = "application/json" if match.group(2).endswith(".json") else "application/octet-stream"
                return self._send_file(body, kind, match.group(2), attachment=not match.group(2).endswith(".json"))
            # Where the farm sends events, for the whole fleet or for one rig.
            # The secret is never among them: it is set once and proven by a
            # signature after that.
            if path == "/api/v1/webhooks":
                return self._json(HTTPStatus.OK, manager.webhook_subscriptions("farm"))
            match = re.fullmatch(r"/api/v1/rigs/([a-z0-9][a-z0-9._-]{0,31})/webhooks", path)
            if match:
                # An account reads the subscriptions on its own rigs. A URL
                # somebody else pointed at their own systems is theirs, so a
                # rig this caller does not administer answers nothing -- and
                # it has to answer something, or the owner's settings page
                # could post a subscription it could never list back.
                if getattr(identity, "is_account", False) \
                        and not manager.may_manage_rig(match.group(1), identity):
                    return self._json(HTTPStatus.FORBIDDEN,
                                      {"error": f"{match.group(1)} is not yours to administer"})
                return self._json(HTTPStatus.OK, manager.webhook_subscriptions(match.group(1)))
            match = re.fullmatch(r"/api/v1/webhooks/([0-9a-f]{1,16})/deliveries", path)
            if match:
                query = parse_qs(urlparse(self.path).query)
                try:
                    limit = int(query.get("limit", ["25"])[0])
                    offset = int(query.get("offset", ["0"])[0])
                except ValueError:
                    return self._json(HTTPStatus.BAD_REQUEST,
                                      {"error": "limit and offset must be integers"})
                if manager.store.webhook_sub(match.group(1)) is None:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such subscription"})
                return self._json(HTTPStatus.OK,
                                  manager.store.webhook_deliveries(match.group(1), limit, offset))
            if path == "/api/v1/farm/notify":
                return self._json(HTTPStatus.OK, manager.farm_notify())
            if path == "/api/v1/setup":
                return self._json(HTTPStatus.OK, manager.own_setup())
            if path == "/api/v1/view":
                # This rig's own view. A portal is nobody's rig: its rigs' views
                # are at /api/v1/rigs/<name>/view.
                if manager.__dict__.get("mode") == "portal":
                    return self._json(HTTPStatus.CONFLICT,
                                      {"error": "a portal has no view of its own; ask /api/v1/rigs/<name>/view"})
                return self._json(HTTPStatus.OK, manager.rig_view())
            match = re.fullmatch(r"/api/v1/rigs/([a-z0-9][a-z0-9._-]{0,31})/view", path)
            if match:
                # A connected rig, in the rig view's shape: worker_detail plus
                # the contract's version, cut for a caller the rig is only
                # lent to exactly as its detail is.
                try:
                    detail = manager.rig_detail(match.group(1), keys, identity)
                except LookupError as exc:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                except ElsewhereError as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                if detail.get("pending"):
                    return self._json(HTTPStatus.NOT_FOUND, {"error": f"{match.group(1)} has not joined yet"})
                view = {"contract": manager.RIG_VIEW_CONTRACT}
                for key in manager.RIG_VIEW_KEYS:
                    if key != "contract":
                        view[key] = detail.get(key)
                return self._json(HTTPStatus.OK, view)
            if path == "/api/v1/console":
                query = parse_qs(urlparse(self.path).query)
                try:
                    after = int(query.get("after", ["0"])[0])
                    limit = int(query.get("limit", ["200"])[0])
                except ValueError:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "after and limit are numbers"})
                worker = (query.get("worker") or [None])[0]
                try:
                    return self._json(HTTPStatus.OK, manager.console(after, worker, limit))
                except LookupError as exc:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            if path == "/api/v1/status":
                health = {}
                portal = manager.__dict__.get("mode") == "portal"
                if portal:
                    # A portal has no host health snapshot: it runs nothing.
                    # Two scopes: which rigs appear at all, and whose
                    # diagnostics may be read -- a rig lent to this caller is
                    # in the first and not the second.
                    health = manager.portal_health(manager.visible_rigs(identity),
                                                   owned=self._runs(identity)[0])
                else:
                    try:
                        health = json.loads(Path("/var/lib/alteriom-hil/status.json").read_text())
                    except (OSError, json.JSONDecodeError):
                        health = {"status": "unknown"}
                # What this caller may see of the farm. None for a key or an
                # admin, which is every rig and the answer this has always
                # given; a set for an account, and then everything below that
                # names a rig is cut to it.
                mine = manager.visible_rigs(identity)
                # And which runs, which is a narrower question: a rig shared
                # with this caller is one they may run on, not one whose
                # runs are theirs to read.
                run_rigs, submitter = self._runs(identity)
                def of_mine(rows, key="worker"):
                    return rows if mine is None else [row for row in rows if row.get(key) in mine]

                def runs_of_mine(rows):
                    return [row for row in rows if manager.store.mine(row, run_rigs, submitter)]
                return self._json(
                    HTTPStatus.OK,
                    {
                        "health": health,
                        # standalone, portal or node: the dashboard shows a
                        # portal's workers and releases, and a node's portal.
                        "mode": manager.__dict__.get("mode", "standalone"),
                        # Which rigs, then how much of each: a rig lent to
                        # this caller is in the list and lent within it, or
                        # the drain reason `health` just withheld comes back
                        # on the row beside it.
                        "workers": manager.rows_for(
                            of_mine(manager.workers_view(), "name"), run_rigs) if portal else [],
                        # Rigs added and not joined yet: in the list beside the workers.
                        "pending_rigs": [rig for rig in manager.rigs_view(keys, identity)["rigs"] if rig.get("pending")] if portal else [],
                        "release": manager.current_release() if portal else None,
                        "portal_url": os.environ.get("ALTERIOM_HIL_PORTAL_URL") if manager.__dict__.get("mode") == "node" else None,
                        # Who this key is, so the dashboard offers only what
                        # it may do.
                        "you": {"name": identity.name, "role": identity.role,
                                **({"account": self._account}
                                   if getattr(self, "_via_session", False) else {})},
                        "inventory": manager.inventory_snapshot(
                            annotate=True, workers=mine, runs=(run_rigs, submitter)),
                        # The ten newest, and every queued or running job
                        # besides: the live panel lists all of those, and a
                        # run that has waited behind ten newer submissions is
                        # still one it must show.
                        "jobs": runs_of_mine(_with_active(
                            manager.store.recent(10, workers=run_rigs, submitted_by=submitter),
                            manager.store.active())),
                        # The dashboard builds its artifact-family picker from
                        # this, so a family added to the HAL shows up without a
                        # page change.
                        "targets": sorted(TARGETS),
                        "version": service_version(),
                        # The three below are the farm's build configuration,
                        # not anybody's workspace: where the farm's own code
                        # comes from, the suite's files and tests, and each
                        # consumer's repo, supply repo, supply workflow and
                        # suite path. Those last name private repositories --
                        # the same names that keep the artifact store shut to
                        # accounts -- so a workspace view does not carry them.
                        # What reads them is the run form's pickers, which
                        # come up empty rather than broken; and starting a run
                        # is not something an account does yet
                        # (docs/device-platform-plan.md, step 12); when it is,
                        # each becomes a projection of the consumers that
                        # account may build rather than an empty answer.
                        "repositories": repositories(manager.repo, manager.profiles) if mine is None else {},
                        "queue": manager.queue_state(run_rigs, submitter),
                        "suite_tests": manager.suite_catalogue() if mine is None else [],
                        # The profile a run is for when it names none, and
                        # whose suite `suite_tests` lists: the farm's choice,
                        # so the dashboard asks rather than knowing a name.
                        "default_profile": manager.default_profile,
                        "profiles": manager.configuration()["build"]["profile_details"] if mine is None else {},
                    },
                )
            match = re.fullmatch(r"/api/v1/jobs/([0-9a-f]{32})/artifacts/([a-z0-9][a-z0-9_.:-]{0,80})", path)
            if match:
                # A run's evidence follows the run: whoever may open the run
                # may download what it produced. One outside this caller's
                # workspace is not found, the same answer and for the same
                # reason as the run itself. This is a single run's output,
                # which is not the artifact store -- that holds firmware built
                # from private repositories and stays shut (api_keys,
                # ACCOUNT_ROUTES).
                detail = manager.job_detail(match.group(1))
                run_rigs, submitter = self._runs(identity)
                if detail is None or not manager.store.mine(detail, run_rigs, submitter):
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "job not found"})
                try:
                    file_path, content_type, filename = manager.artifact(match.group(1), match.group(2))
                except KeyError:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "job not found"})
                except LookupError as exc:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                except FileNotFoundError:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "this run did not produce that artifact"})
                return self._send_file(file_path.read_bytes(), content_type, filename)
            if path == "/api/v1/artifacts":
                query = parse_qs(urlparse(self.path).query)

                def one(name, default=None):
                    value = query.get(name, [default])[0]
                    return value if value not in ("", None) else default

                try:
                    limit = int(one("limit", str(manager.ARTIFACT_PAGE_DEFAULT)))
                    offset = int(one("offset", "0"))
                except ValueError:
                    return self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "limit and offset must be integers"},
                    )
                return self._json(HTTPStatus.OK, manager.artifact_index(
                    limit=limit, offset=offset,
                    search=one("q"), profile=one("profile"), branch=one("branch"),
                ))
            if path == "/api/v1/artifacts/library":
                return self._json(HTTPStatus.OK, manager.artifact_library())
            match = re.fullmatch(r"/api/v1/artifacts/([0-9a-f]{32})", path)
            if match:
                try:
                    return self._json(HTTPStatus.OK, manager.artifact_detail(match.group(1)))
                except KeyError:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such bundle"})
            match = re.fullmatch(r"/api/v1/artifacts/([0-9a-f]{32})/bundle", path)
            if match:
                try:
                    body, filename = manager.artifact_archive(match.group(1))
                except KeyError:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such bundle"})
                except ValueError as exc:
                    return self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                except ArtifactProtected as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except (LookupError, OSError):
                    # Deleted, pruned or changed while it was being read.
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "the bundle changed while it was being read"})
                return self._send_file(body, "application/gzip", filename)
            # Any path a manifest names may be asked for -- of whatever length a
            # request line carries, since a manifest may name a file through as
            # many directories as the filesystem allows. What is served is only
            # ever a file the bundle's enumeration holds (artifact_store.file_path).
            match = re.fullmatch(r"/api/v1/artifacts/([0-9a-f]{32})/files/([^\x00-\x1f\x7f]+)", path)
            if match:
                try:
                    file_path, content_type, filename = manager.artifact_file(match.group(1), match.group(2))
                    body = file_path.read_bytes()
                except KeyError:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such bundle"})
                except ArtifactProtected as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except LookupError as exc:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                except OSError:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "the file was removed while it was being read"})
                return self._send_file(body, content_type, filename)
            if path == "/api/v1/retention":
                return self._json(HTTPStatus.OK, manager.retention_sweep(dry_run=True))
            if path == "/api/v1/stats":
                query = parse_qs(urlparse(self.path).query)
                try:
                    days = int(query.get("days", ["7"])[0])
                    offset = int(query.get("tz_offset_minutes", ["0"])[0])
                    return self._json(HTTPStatus.OK, manager.farm_statistics(days, offset))
                except ValueError as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            if path == "/api/v1/storage":
                fresh = parse_qs(urlparse(self.path).query).get("fresh", ["0"])[0] in ("1", "true")
                return self._json(HTTPStatus.OK, manager.storage(fresh=fresh))
            detail = re.fullmatch(r"/api/v1/storage/([a-z]{1,32})", path)
            if detail:
                query = parse_qs(urlparse(self.path).query)
                try:
                    limit = int(query.get("limit", [str(manager.STORAGE_PAGE_DEFAULT)])[0])
                    offset = int(query.get("offset", ["0"])[0])
                    return self._json(
                        HTTPStatus.OK, manager.storage_detail(detail.group(1), limit, offset)
                    )
                except KeyError:
                    return self._json(
                        HTTPStatus.NOT_FOUND, {"error": f"no storage called {detail.group(1)}"}
                    )
                except ValueError as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            if path == "/api/v1/jobs":
                query = parse_qs(urlparse(self.path).query)
                def one(name, default=None):
                    value = query.get(name, [default])[0]
                    return value if value not in ("", None) else default
                try:
                    limit = int(one("limit", "25"))
                    offset = int(one("offset", "0"))
                except ValueError:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "limit and offset must be integers"})
                # An account sees the runs of the rigs in its workspace; a
                # key sees the farm's. None means no restriction.
                run_rigs, submitter = self._runs(identity)
                page = manager.store.page(
                    limit=limit,
                    offset=offset,
                    status=one("status"),
                    kind=one("kind"),
                    search=(one("q") or "")[:200],
                    worker=(one("worker") or "")[:32] or None,
                    workers=run_rigs,
                    submitted_by=submitter,
                )
                page["counts"] = manager.store.counts_by_status(run_rigs, submitter)
                return self._json(HTTPStatus.OK, page)
            if path == "/api/v1/config":
                return self._json(HTTPStatus.OK, manager.configuration())
            match = re.fullmatch(r"/api/v1/jobs/([0-9a-f]{32})", path)
            if match:
                job = manager.job_detail(match.group(1))
                # A run belongs to the rig that ran it, or -- while it waits
                # for one -- to whoever submitted it. One outside this
                # caller's workspace is not found rather than forbidden: a
                # 403 would confirm the id names something real.
                run_rigs, submitter = self._runs(identity)
                if job and not manager.store.mine(job, run_rigs, submitter):
                    job = None
                return self._json(HTTPStatus.OK if job else HTTPStatus.NOT_FOUND, job or {"error": "job not found"})
            if path == "/api/v1/inventory":
                return self._json(HTTPStatus.OK,
                                  manager.inventory_snapshot(annotate=True,
                                                             workers=manager.visible_rigs(identity),
                                                             runs=self._runs(identity)))
            match = re.fullmatch(r"/api/v1/inventory/([a-z0-9][a-z0-9._-]{0,31})/history", path)
            if match:
                # The drill-down the board list opens. Scoped like the list
                # it was opened from: a board on no rig this caller may read
                # is not found, and the verdicts come back without the run
                # ids they may not follow.
                rig = manager.rig_of_board(match.group(1))
                mine = manager.visible_rigs(identity)
                if mine is not None and rig not in mine:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such board"})
                return self._json(HTTPStatus.OK, manager.board_history(
                    match.group(1), runs=self._runs(identity), rig=rig))
            match = re.fullmatch(r"/api/v1/inventory/([a-z0-9][a-z0-9._-]{0,31})/details", path)
            if match:
                try:
                    return self._json(HTTPStatus.OK, manager.device_details(match.group(1)))
                except ValueError as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except LookupError as exc:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                except RigBusyError as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except (RuntimeError, OSError) as exc:
                    # The board did not answer. Transient by nature — a reset,
                    # a busy port, a cable — so say retryable, not broken.
                    return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            if path.startswith("/api/") and self._half_answer("GET", path, identity):
                return
            return super().do_GET()

        def do_POST(self):
            if self._route().startswith("/auth/"):
                return self._auth_post(self._route())
            if public_route("POST", self._route()):
                return self._enroll()
            self._mutate("POST", self._post)

        def _join_script(self):
            """The script a new rig runs (rig/join-rig.sh), from the portal
            it joins: no key -- it carries none, and the token it needs is in
            the command beside it."""
            script = join_script(manager.__dict__.get("repo") or FARM_REPO_ROOT)
            if manager.__dict__.get("mode") != "portal" or not script.is_file():
                return self._json(HTTPStatus.NOT_FOUND, {"error": "rigs join a portal"})
            body = script.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/x-shellscript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _enroll(self):
            """A new rig's one-time token for its node key. Audited under the
            rig's name when it works, as `unknown` when it does not."""
            path = self._route()
            address = self.headers.get("X-Real-IP") or self.client_address[0]
            now = time.monotonic()
            with enroll_lock:
                recent = [at for at in enroll_failures.get(address, []) if now - at < ENROLL_FAILURE_WINDOW]
                enroll_failures[address] = recent
            self._pending_audit = (Identity("unknown", "enroll"), "POST", path)
            if len(recent) >= ENROLL_FAILURE_LIMIT:
                return self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "too many refused tokens from here; wait a while"})

            def refused(status: HTTPStatus, error: str):
                with enroll_lock:
                    enroll_failures.setdefault(address, []).append(now)
                return self._json(status, {"error": error})

            try:
                payload = self._request_json_limit(4096)
                answer = manager.redeem_enrollment(payload, address, keys)
            except PermissionError as exc:
                return refused(HTTPStatus.FORBIDDEN, str(exc))
            except (ValueError, json.JSONDecodeError) as exc:
                return refused(HTTPStatus.BAD_REQUEST, str(exc))
            except ElsewhereError as exc:
                return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            self._pending_audit = (Identity(answer["name"], "enroll"), "POST", path)
            return self._json(HTTPStatus.OK, answer)

        def do_DELETE(self):
            path = self._route()
            webhook = re.fullmatch(r"/api/v1/webhooks/([0-9a-f]{1,16})", path)
            rig_webhook = re.fullmatch(
                r"/api/v1/rigs/([a-z0-9][a-z0-9._-]{0,31})/webhooks/([0-9a-f]{1,16})", path)
            if webhook or rig_webhook:
                identity = self._identity()
                if identity is None or not allowed(identity, "DELETE", path):
                    return self._json(HTTPStatus.FORBIDDEN,
                                      {"error": "that key cannot change this subscription"})
                if self._cookie_write_refused():
                    return
                self._pending_audit = (identity, "DELETE", path)
                scope = "farm" if webhook else rig_webhook.group(1)
                sub_id = webhook.group(1) if webhook else rig_webhook.group(2)
                return self._webhook_call(
                    lambda: manager.remove_webhook(sub_id, identity.name, scope, identity))
            if self.path.split("?")[0] == "/api/v1/farm/notify":
                identity = self._identity()
                if identity is None or not identity.is_admin:
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "an admin key turns the farm's notifications off"})
                if self._cookie_write_refused():
                    return
                # One channel by its id, or every one of them.
                wanted = parse_qs(urlparse(self.path).query).get("id", [""])[0].strip()
                try:
                    return self._json(HTTPStatus.OK, manager.clear_farm_notify(identity.name, wanted or None))
                except LookupError as exc:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            self._mutate("DELETE", self._delete)

        def do_PATCH(self):
            self._mutate("PATCH", self._patch)

        def _patch(self, path: str, identity: Identity):
            rig = re.fullmatch(r"/api/v1/rigs/([a-z0-9][a-z0-9._-]{0,31})", path)
            if not rig:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return self._worker_call(lambda: manager.update_rig(rig.group(1), self._request_json(), identity.name, keys))

        def _read_body(self, limit: int) -> bytes | None:
            length = int(self.headers.get("Content-Length", "0"))
            if length > limit or length < 0:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": f"the body may be at most {limit} bytes"})
                return None
            return self.rfile.read(length)

        def _webhook_call(self, call):
            """A webhook call, answered the way the rest of the API answers."""
            try:
                return self._json(HTTPStatus.OK, call())
            except PermissionError as exc:
                return self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
            except LookupError as exc:
                return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except ElsewhereError as exc:
                return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except (ValueError, json.JSONDecodeError) as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def _worker_call(self, call):
            """Run one worker-protocol call and answer as the protocol says."""
            try:
                return self._json(HTTPStatus.OK, call())
            except PermissionError as exc:
                return self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
            except LookupError as exc:
                return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except ElsewhereError as exc:
                return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except (ValueError, json.JSONDecodeError, tarfile.TarError) as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def _post(self, path: str, identity: Identity):
            if path == "/api/v1/sessions/revoke":
                # Signing a browser out is about the caller's own account, so
                # it is scoped to the session cookie rather than to a role: a
                # key has no browsers to sign out.
                if not getattr(self, "_via_session", False):
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "sessions belong to a signed-in account, not a key"})
                wanted = (self._request_json_limit(4096) or {}).get("id")
                if not isinstance(wanted, str) or not wanted:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "which session: an id, or \"others\""})
                result = manager.revoke_session(self._session_cookie(), wanted)
                if result is None:
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "no session"})
                if not result["revoked"]:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such session"})
                manager._event("farm", "account",
                               f"{identity.name} signed out {'every other browser' if wanted == 'others' else 'a browser'}")
                body = json.dumps(result).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                # Revoking the browser asking is a sign-out: take its cookie
                # with it, or it keeps presenting one that no longer resolves.
                if result["current"]:
                    self.send_header("Set-Cookie", self._session_cookie_header(None))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            worker = re.fullmatch(r"/api/v1/workers/([a-z0-9][a-z0-9._-]{0,31})/(hello|heartbeat|lease)", path)
            if worker:
                name, call = worker.groups()
                if name != identity.name:
                    # A worker speaks for itself: its key is named for it.
                    return self._json(HTTPStatus.FORBIDDEN, {"error": f"the key {identity.name} is not worker {name}"})
                address = self.headers.get("X-Real-IP") or self.client_address[0]
                if call == "lease":
                    wait = parse_qs(urlparse(self.path).query).get("wait", [str(LEASE_WAIT_SECONDS)])[0]
                    return self._worker_call(lambda: manager.worker_lease(name, float(wait)))
                handler = manager.worker_hello if call == "hello" else manager.worker_heartbeat
                return self._worker_call(lambda: handler(name, self._request_json_limit(1024 * 1024), address))
            # The farm's own events are an admin's; one rig's are its page's.
            # Two shapes of path rather than one with the scope in the body,
            # because which key may do this has to be decidable from the path
            # (alteriom_hil.api_keys.required_role).
            if path == "/api/v1/webhooks":
                return self._webhook_call(
                    lambda: manager.add_webhook("farm", self._request_json_limit(8192), identity.name))
            match = re.fullmatch(r"/api/v1/rigs/([a-z0-9][a-z0-9._-]{0,31})/owner", path)
            if match:
                return self._webhook_call(lambda: manager.set_rig_owner(
                    match.group(1), (self._request_json_limit(1024) or {}).get("owner"),
                    keys, identity.name))
            match = re.fullmatch(r"/api/v1/rigs/([a-z0-9][a-z0-9._-]{0,31})/visibility", path)
            if match:
                return self._webhook_call(lambda: manager.set_rig_visibility(
                    match.group(1), (self._request_json_limit(1024) or {}).get("visibility"),
                    identity, identity.name))
            match = re.fullmatch(r"/api/v1/webhooks/([0-9a-f]{1,16})/test", path)
            if match:
                return self._webhook_call(lambda: manager.test_webhook(match.group(1), "farm"))
            match = re.fullmatch(r"/api/v1/webhooks/([0-9a-f]{1,16})", path)
            if match:
                return self._webhook_call(
                    lambda: manager.change_webhook(match.group(1), self._request_json_limit(8192),
                                                   identity.name, "farm"))
            match = re.fullmatch(r"/api/v1/rigs/([a-z0-9][a-z0-9._-]{0,31})/webhooks", path)
            if match:
                return self._webhook_call(
                    lambda: manager.add_webhook(match.group(1), self._request_json_limit(8192),
                                                identity.name, identity))
            match = re.fullmatch(
                r"/api/v1/rigs/([a-z0-9][a-z0-9._-]{0,31})/webhooks/([0-9a-f]{1,16})/test", path)
            if match:
                return self._webhook_call(
                    lambda: manager.test_webhook(match.group(2), match.group(1), identity))
            match = re.fullmatch(
                r"/api/v1/rigs/([a-z0-9][a-z0-9._-]{0,31})/webhooks/([0-9a-f]{1,16})", path)
            if match:
                return self._webhook_call(
                    lambda: manager.change_webhook(match.group(2), self._request_json_limit(8192),
                                                   identity.name, match.group(1), identity))
            if path == "/api/v1/farm/notify":
                try:
                    return self._json(HTTPStatus.OK, manager.set_farm_notify(
                        self._request_json_limit(8192), identity.name))
                except LookupError as exc:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                except ElsewhereError as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except (ValueError, json.JSONDecodeError) as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            if path == "/api/v1/farm/notify/test":
                note = Notification("test", "A test from the farm",
                                    "Notifications reach this channel. Nothing is wrong.", tone="good")
                notifiers = manager.__dict__.get("farm_notifiers") or {}
                if not notifiers:
                    return self._json(HTTPStatus.CONFLICT, {"error": "the farm has no channel set"})
                try:
                    body = self._request_json_limit(1024) if self.headers.get("Content-Length") else {}
                except (ValueError, json.JSONDecodeError) as exc:
                    # What the endpoint beside it answers. Unhandled, a body
                    # that is not an object ended the request as a 500.
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                wanted = str((body or {}).get("id") or "").strip() if isinstance(body, dict) else ""
                if wanted and wanted not in notifiers:
                    return self._json(HTTPStatus.NOT_FOUND,
                                      {"error": f"no channel {wanted} is on"})
                # One channel, or every one of them: an admin who has two
                # wants to know which of them a message actually reached.
                chosen = {wanted: notifiers[wanted]} if wanted else notifiers
                results = {key: (item.send(note) or {}) for key, item in chosen.items()}
                return self._json(HTTPStatus.OK, {"results": results} if len(results) > 1
                                  else next(iter(results.values())))
            started = re.fullmatch(
                r"/api/v1/workers/([a-z0-9][a-z0-9._-]{0,31})/commands/([0-9a-f]{32})/start", path
            )
            if started:
                name, command_id = started.groups()
                if name != identity.name:
                    return self._json(HTTPStatus.FORBIDDEN, {"error": f"the key {identity.name} is not worker {name}"})
                return self._worker_call(lambda: manager.command_started(name, command_id))
            result = re.fullmatch(r"/api/v1/workers/([a-z0-9][a-z0-9._-]{0,31})/commands/([0-9a-f]{32})", path)
            if result:
                name, command_id = result.groups()
                if name != identity.name:
                    return self._json(HTTPStatus.FORBIDDEN, {"error": f"the key {identity.name} is not worker {name}"})
                return self._worker_call(lambda: manager.command_result(name, command_id, self._request_json_limit(600000)))
            if path == "/api/v1/rigs":
                try:
                    return self._json(HTTPStatus.CREATED, manager.create_rig(self._request_json(), identity.name, keys))
                except ElsewhereError as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except (ValueError, json.JSONDecodeError) as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            join = re.fullmatch(r"/api/v1/rigs/([a-z0-9][a-z0-9._-]{0,31})/join", path)
            if join:
                return self._worker_call(lambda: manager.new_join_token(join.group(1), identity.name, keys))
            control = re.fullmatch(r"/api/v1/workers/([a-z0-9][a-z0-9._-]{0,31})/(commands|drain|resume)", path)
            if control:
                name, call = control.groups()
                if call == "commands":
                    def ask():
                        payload = self._request_json_limit(262144)
                        unknown = set(payload) - {"kind", "args"}
                        if unknown:
                            raise ValueError(f"unknown request fields: {sorted(unknown)}")
                        return manager.request_command(name, payload.get("kind"), payload.get("args"), identity.name)
                    try:
                        return self._json(HTTPStatus.ACCEPTED, ask())
                    except LookupError as exc:
                        return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    except ElsewhereError as exc:
                        return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                    except (ValueError, json.JSONDecodeError) as exc:
                        return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                reason = self._request_json().get("reason") if call == "drain" else None
                return self._worker_call(lambda: manager.drain_worker(name, call == "drain", identity.name, reason))
            history = re.fullmatch(
                r"/api/v1/workers/([a-z0-9][a-z0-9._-]{0,31})/history/"
                r"(known|artifacts/[0-9a-f]{32}|jobs/[0-9a-f]{32}(?:/evidence|/log|/link)?)",
                path,
            )
            if history:
                name, rest = history.groups()
                if name != identity.name:
                    return self._json(HTTPStatus.FORBIDDEN, {"error": f"the key {identity.name} is not worker {name}"})
                parts = rest.split("/")
                if parts[0] == "known":
                    return self._worker_call(lambda: manager.history_known(name, self._request_json_limit(1024 * 1024)))
                if parts[0] == "artifacts":
                    body = self._read_body(MAX_BUNDLE_BYTES)
                    if body is None:
                        return None
                    return self._worker_call(lambda: manager.history_artifact(name, parts[1], body))
                job_id = parts[1]
                call = parts[2] if len(parts) > 2 else None
                if call == "evidence":
                    body = self._read_body(MAX_EVIDENCE_BYTES)
                    if body is None:
                        return None
                    return self._worker_call(lambda: manager.history_evidence(name, job_id, body))
                if call == "log":
                    body = self._read_body(MAX_HISTORY_LOG_BYTES)
                    if body is None:
                        return None
                    return self._worker_call(lambda: manager.history_log(name, job_id, body))
                if call == "link":
                    return self._worker_call(lambda: manager.history_link(name, job_id, self._request_json()))
                return self._worker_call(lambda: manager.history_job(name, job_id, self._request_json_limit(2 * 1024 * 1024)))
            report = re.fullmatch(r"/api/v1/jobs/([0-9a-f]{32})/(stages|log|evidence|result)", path)
            if report:
                job_id, call = report.groups()
                name = identity.name
                if call == "stages":
                    return self._worker_call(lambda: manager.worker_stages(name, job_id, self._request_json_limit(262144).get("progress")))
                if call == "result":
                    return self._worker_call(lambda: manager.worker_result(name, job_id, self._request_json_limit(524288)))
                if call == "log":
                    body = self._read_body(MAX_LOG_CHUNK_BYTES)
                    if body is None:
                        return None
                    offset = parse_qs(urlparse(self.path).query).get("offset", ["0"])[0]
                    return self._worker_call(lambda: manager.worker_log(name, job_id, int(offset), body))
                body = self._read_body(MAX_EVIDENCE_BYTES)
                if body is None:
                    return None
                return self._worker_call(lambda: manager.worker_evidence(name, job_id, body))
            hold = re.fullmatch(r"/api/v1/inventory/([a-z0-9][a-z0-9._-]{0,31})/(reserve|release)", path)
            if hold:
                try:
                    payload = self._request_json()
                    unknown = set(payload) - {"reason"}
                    if unknown:
                        raise ValueError(f"unknown request fields: {sorted(unknown)}")
                    if hold.group(2) == "reserve":
                        result = manager.reserve_board(hold.group(1), payload.get("reason"), identity.name)
                    else:
                        result = manager.release_board(hold.group(1), identity.name)
                except (ValueError, json.JSONDecodeError) as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except LookupError as exc:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return self._json(HTTPStatus.OK, result)
            if path == "/api/v1/inventory/register":
                try:
                    payload = self._request_json()
                    unknown = set(payload) - {"id", "mac"}
                    if unknown:
                        raise ValueError(f"unknown request fields: {sorted(unknown)}")
                    result = manager.register_device(payload.get("id"), payload.get("mac"))
                except RigBusyError as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except (ValueError, json.JSONDecodeError) as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except OSError as exc:
                    return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"registration failed: {exc}"})
                return self._json(HTTPStatus.CREATED, result)
            cancel = re.fullmatch(r"/api/v1/jobs/([0-9a-f]{32})/cancel", path)
            if cancel:
                if not identity.is_admin:
                    job = manager.store.get(cancel.group(1))
                    if job is None:
                        return self._json(HTTPStatus.NOT_FOUND, {"error": "no such job"})
                    # An account is told no more here than the read routes
                    # tell it: a run outside its scope is not found, which is
                    # what its detail says. The 403 below names the account
                    # that started the run, and a run id travels -- in a
                    # copied link, in a CI log. Two different answers would
                    # turn an id somebody pasted into a way to ask whether a
                    # run exists and whose handle is on it.
                    run_rigs, submitter = self._runs(identity)
                    if not manager.store.mine(job, run_rigs, submitter):
                        return self._json(HTTPStatus.NOT_FOUND, {"error": "no such job"})
                    owner = (job.get("request") or {}).get("submitted_by")
                    if owner != identity.name:
                        return self._json(HTTPStatus.FORBIDDEN, {"error": (
                            f"a user may cancel only the runs they started; this one "
                            f"was started by {owner or 'the farm or its CI'}"
                        )})
                try:
                    job = manager.cancel(cancel.group(1), "Cancelled by operator")
                except KeyError:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such job"})
                except ValueError as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return self._json(HTTPStatus.OK, job)
            promote = re.fullmatch(r"/api/v1/jobs/([0-9a-f]{32})/promote", path)
            if promote:
                try:
                    job = manager.promote(promote.group(1))
                except KeyError:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such job"})
                except ValueError as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return self._json(HTTPStatus.OK, job)
            if path == "/api/v1/queue/pause":
                return self._json(HTTPStatus.OK, manager.pause())
            if path == "/api/v1/queue/resume":
                return self._json(HTTPStatus.OK, manager.resume())
            pin = re.fullmatch(r"/api/v1/artifacts/([0-9a-f]{32})/(pin|unpin)", path)
            if pin:
                try:
                    payload = self._request_json()
                    allowed = {"note"} if pin.group(2) == "pin" else set()
                    unknown = set(payload) - allowed
                    if unknown:
                        raise ValueError(f"unknown request fields: {sorted(unknown)}")
                    if pin.group(2) == "pin":
                        result = manager.pin_artifact(pin.group(1), payload.get("note"))
                    else:
                        result = manager.unpin_artifact(pin.group(1))
                except KeyError:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such bundle"})
                except (ValueError, json.JSONDecodeError) as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except ArtifactProtected as exc:
                    # A prune is deleting and holds the store: something to
                    # act on -- pin again once it is done -- not a request
                    # that fails with nothing the panel can show for it.
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return self._json(HTTPStatus.OK, result)
            if path == "/api/v1/retention/run":
                try:
                    payload = self._request_json()
                    unknown = set(payload) - {"dry_run"}
                    if unknown:
                        raise ValueError(f"unknown request fields: {sorted(unknown)}")
                    dry_run = payload.get("dry_run", True)
                    if not isinstance(dry_run, bool):
                        raise ValueError("dry_run must be true or false")
                except (ValueError, json.JSONDecodeError) as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return self._json(HTTPStatus.OK, manager.retention_sweep(dry_run=dry_run))
            if path == "/api/v1/artifacts/prune":
                try:
                    result = manager.prune_artifacts(self._request_json())
                except (ValueError, json.JSONDecodeError) as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except ArtifactProtected as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except OSError as exc:
                    return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"prune failed: {exc}"})
                # Accepted, not done: deleting runs on a thread and the
                # artifact index carries its progress.
                return self._json(
                    HTTPStatus.ACCEPTED if result.get("pruning") else HTTPStatus.OK, result
                )
            if path == "/api/v1/releases":
                # A release of the farm for the nodes: a git bundle, with the
                # commit it is of in the query string.
                query = {name: values[0] for name, values in parse_qs(urlparse(self.path).query).items()}
                body = self._read_body(MAX_RELEASE_BYTES)
                if body is None:
                    return None
                try:
                    result = manager.publish_release(
                        query.get("commit", ""), body, identity.name, query.get("note"),
                        current=query.get("current", "1") not in ("0", "false", "no"),
                    )
                except ElsewhereError as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except ValueError as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return self._json(HTTPStatus.CREATED, result)
            attach = re.fullmatch(r"/api/v1/releases/([0-9a-f]{40})/files/([A-Za-z0-9][A-Za-z0-9._-]{0,120})", path)
            if attach:
                # A release's packages, one file per call, beside the bundle
                # already published; release.json last, since it is checked
                # against the rest.
                body = self._read_body(MAX_RELEASE_BYTES)
                if body is None:
                    return None
                try:
                    result = manager.attach_release_file(attach.group(1), attach.group(2), body, identity.name)
                except ElsewhereError as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except LookupError as exc:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                except ValueError as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return self._json(HTTPStatus.CREATED, result)
            current = re.fullmatch(r"/api/v1/releases/([0-9a-f]{40})/current", path)
            if current:
                try:
                    return self._json(HTTPStatus.OK, manager.set_current_release(current.group(1), identity.name))
                except ElsewhereError as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except LookupError as exc:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            if path == "/api/v1/artifacts":
                # The one route that reads a large body: a bundle, not JSON.
                # Its provenance rides in the query string, so the body stays
                # exactly the archive the producer built.
                length = int(self.headers.get("Content-Length", "0"))
                if length > MAX_BUNDLE_BYTES:
                    return self._json(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        {"error": f"a bundle may be at most {MAX_BUNDLE_BYTES} bytes"},
                    )
                if length <= 0:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "the bundle is empty"})
                fields = {
                    name: values[0]
                    for name, values in parse_qs(urlparse(self.path).query).items()
                }
                try:
                    result = manager.accept_bundle(fields, self.rfile.read(length))
                except (ValueError, KeyError, tarfile.TarError, json.JSONDecodeError) as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except OSError as exc:
                    return self._json(
                        HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"bundle upload failed: {exc}"}
                    )
                return self._json(HTTPStatus.CREATED, result)
            if path == "/api/v1/health":
                # The farm checking its own hardware: one board, several, or
                # the whole rig. A canary run, queued like any other.
                try:
                    job = manager.health_check(self._request_json(), submitted_by=identity.name)
                except RigBusyError as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except (ValueError, json.JSONDecodeError) as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return self._json(HTTPStatus.ACCEPTED, job)
            if path == "/api/v1/builds":
                # Gone, not missing: say what replaced it.
                return self._json(HTTPStatus.GONE, {"error": (
                    "the farm does not build firmware: a project builds its own bundle "
                    "in its CI, hands it over with POST /api/v1/artifacts, and "
                    "POST /api/v1/suites flashes and runs it"
                )})
            if path == "/api/v1/inventory/refresh" and manager.__dict__.get("mode") == "portal":
                return self._json(HTTPStatus.ACCEPTED, manager.request_rediscovery())
            if self._half_answer("POST", path, identity, body=True):
                return
            kinds = {"/api/v1/inventory/refresh": "inventory", "/api/v1/suites": "suite"}
            if path not in kinds:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            try:
                job = manager.submit(kinds[path], self._request_json(), submitted_by=identity.name)
            except RigBusyError as exc:
                return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except (ValueError, json.JSONDecodeError) as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return self._json(HTTPStatus.ACCEPTED, job)

        def _delete(self, path: str, identity: Identity):
            rig = re.fullmatch(r"/api/v1/(?:rigs|workers)/([a-z0-9][a-z0-9._-]{0,31})", path)
            if rig:
                name = rig.group(1)
                # Its key goes with it: a removed worker that comes back is
                # refused, not re-registered. /api/v1/workers/<name> is the
                # older spelling of the same deletion.
                if path.startswith("/api/v1/rigs/"):
                    return self._worker_call(lambda: manager.delete_rig(name, identity.name, keys))
                return self._worker_call(lambda: {key: value for key, value in manager.delete_rig(name, identity.name, keys).items()
                                                  if key in ("removed", "key_revoked")})
            bundle = re.fullmatch(r"/api/v1/artifacts/([0-9a-f]{32})", path)
            if bundle:
                try:
                    return self._json(HTTPStatus.OK, manager.delete_artifact(bundle.group(1)))
                except KeyError:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such bundle"})
                except ArtifactProtected as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except OSError as exc:
                    return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"delete failed: {exc}"})
            match = re.fullmatch(r"/api/v1/inventory/([a-z0-9][a-z0-9._-]{0,31})", path)
            if not match:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            try:
                result = manager.unregister_device(match.group(1))
            except RigBusyError as exc:
                return self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except ValueError as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except OSError as exc:
                return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"unregister failed: {exc}"})
            return self._json(HTTPStatus.OK, result)

        def log_message(self, fmt, *args):
            said = fmt % args
            said = _SECRET_QUERY_IN_LOG.sub(r"\1<redacted>", said)
            sys.stderr.write("farm-api: " + said + "\n")

    return Handler


# ---- running one -------------------------------------------------------------
#
# Which class a mode runs as, the arguments a farm takes, and the server. The
# base knows the shape of a farm; it does not know which halves are installed
# beside it, so whoever runs one passes them in. A rig's launcher passes its
# own half and the portal's if it has it (`alteriom_hil.launcher`,
# docs/public-release-plan.md, step 12e).


def _methods_of(mixin) -> frozenset:
    return frozenset(name for name, value in vars(mixin).items()
                     if callable(value) and not name.startswith("__"))


def compose(rig=None, portal=None) -> dict:
    """The class each mode runs as, from the halves it was given.

    A node is the rig's half on the base, a portal the portal's, and a
    standalone farm both -- which is how every farm ran before there was a
    portal, and what a rig on its own still is. A mode whose half is not
    installed is not in the mapping: `manager_for` says so rather than
    building a class that would refuse everything it was asked.
    """
    portal_only = _methods_of(portal) if portal is not None else frozenset()
    rig_only = _methods_of(rig) if rig is not None else frozenset()
    shared = {
        "_portal_only": portal_only,
        "_rig_only": rig_only,
        # What the base says when it is asked for something it has not got.
        "_portal_half": None if portal is None else portal.__module__,
    }
    classes = {}
    if rig is not None and portal is not None:
        classes["standalone"] = type("FarmManager", (rig, portal, BaseManager), {
            "__doc__": "A standalone farm: the boards, the pipeline, and the portal's side too.",
            **shared})
    elif rig is not None:
        classes["standalone"] = type("FarmManager", (rig, BaseManager), {
            "__doc__": "A standalone farm with no portal half installed: the boards and the pipeline.",
            **shared})
    if rig is not None:
        classes["node"] = type("RigManager", (rig, BaseManager), {
            "__doc__": "A node: the boards and the pipeline, taking runs from a portal.",
            **shared})
    if portal is not None:
        classes["portal"] = type("PortalManager", (portal, BaseManager), {
            "__doc__": "A portal: rigs, accounts, the worker protocol, releases, and no hardware.",
            **shared})
    return classes


def arguments(argv, web_root: Path | None) -> "argparse.Namespace":
    """What a farm is started with. Every unit and the image pass `--web-root`;
    `web_root` is what a hand-run from a checkout gets."""
    parser = argparse.ArgumentParser(prog="alteriom-hil-service", description=__doc__)
    parser.add_argument("--bind", default=os.environ.get("ALTERIOM_HIL_API_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ALTERIOM_HIL_API_PORT", "8090")))
    parser.add_argument("--repo", type=Path, default=Path(os.environ.get("ALTERIOM_HIL_REPO", Path.cwd())))
    parser.add_argument("--state", type=Path, default=Path(os.environ.get("ALTERIOM_HIL_STATE", "/var/lib/alteriom-hil")))
    parser.add_argument("--registry", type=Path, default=Path(os.environ.get("ALTERIOM_HIL_INVENTORY", "inventory.yaml")))
    parser.add_argument("--board-map", type=Path, default=Path(os.environ.get("ALTERIOM_HIL_BOARD_MAP", "board-map.yaml")))
    parser.add_argument("--token-file", type=Path, default=Path(os.environ.get("ALTERIOM_HIL_API_TOKEN_FILE", "/etc/alteriom-hil/api-token")))
    parser.add_argument(
        "--keys-file", type=Path, default=os.environ.get("ALTERIOM_HIL_API_KEYS_FILE"),
        help="named API keys (default: api-keys.yaml beside the token file)",
    )
    parser.add_argument("--web-root", type=Path, default=web_root,
                        help="the rig's dashboard bundle (rig/web in a checkout)")
    # docs/portal-plan.md: standalone, portal (no hardware), or node (hardware
    # that takes its runs from a portal).
    parser.add_argument("--mode", choices=MODES, default=os.environ.get("ALTERIOM_HIL_FARM_MODE", "standalone"))
    parser.add_argument("--portal-url", default=os.environ.get("ALTERIOM_HIL_PORTAL_URL"))
    parser.add_argument("--node-key-file", type=Path, default=os.environ.get("ALTERIOM_HIL_NODE_KEY_FILE"))
    parser.add_argument("--worker-name", default=os.environ.get("ALTERIOM_HIL_WORKER_NAME") or socket.gethostname().lower())
    return parser.parse_args(argv)


def serve(argv=None, *, classes: dict, agent=None, web_root: Path | None = None) -> int:
    """Start a farm: build the mode's manager, take the boards' inventory,
    start the node's agent if this is a node, and answer.

    `classes` is what `compose` made of the halves installed -- the launcher's,
    so that the class a test builds and the class a service runs are the same
    object. `agent` is what a node starts once its manager exists: the rig's,
    since the base has none of its own. It is called with (manager, args).
    """
    args = arguments(argv, web_root)
    if args.mode not in classes:
        missing = "alteriom-hil" if args.mode == "node" else "alteriom-hil-portal"
        raise SystemExit(f"this farm cannot run as {args.mode}: {missing} is not installed")
    token = args.token_file.read_text(encoding="utf-8").strip()
    try:
        keys = KeyStore(token, args.keys_file or keys_path_for(args.token_file))
    except ValueError as exc:
        raise SystemExit(str(exc))
    if args.mode == "node" and not (args.portal_url and args.node_key_file):
        raise SystemExit("a node needs --portal-url and --node-key-file (ALTERIOM_HIL_PORTAL_URL, ALTERIOM_HIL_NODE_KEY_FILE)")
    manager = classes[args.mode](args.repo, args.state, args.registry, args.board_map,
                                 Path(sys.executable), mode=args.mode)
    if args.mode != "portal":
        try:
            manager.submit("inventory", {})
        except RigBusyError:
            # A suite carried over from before the restart is first in line; its
            # own discover stage reads the rig, and the boards it has not seen.
            pass
    if args.mode == "node":
        if agent is None:
            raise SystemExit("a node needs the rig's agent; alteriom-hil is not installed")
        agent(manager, args)
    threading.Thread(target=manager.retention_loop, name="retention", daemon=True).start()
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(manager, keys, args.web_root))
    print(f"Alteriom farm API ({args.mode}) listening on {args.bind}:{args.port}", flush=True)
    server.serve_forever()
    return 0
