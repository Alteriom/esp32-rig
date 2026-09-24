"""The rig's half of the farm manager: what a host with boards on it does,
and a portal never does.

The pipeline a run goes through (`_execute`, and the command runner and
workspace under it), discovery and the chip details, registering and
unregistering a board, the suite selection, and the guards that say
"not on a portal". Everything here opens a port, runs a process on the
host, or reads the rig's own files; `FarmManager` is this mixed into the
portal's half, and picks by mode (docs/public-release-plan.md, step 8).

Module-level: the constants and helpers only this half reads. Nothing here
imports the service -- the service imports this.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import yaml

from alteriom_hil import allocation, farm_shared
from alteriom_hil.board import SUPPORTED_TARGETS, Board
from alteriom_hil.board_registry import (
    load_registry,
    normalize_mac,
    reconcile,
    write_active_map,
    write_inventory_snapshot,
    write_registry,
)
from alteriom_hil.instrument_registry import instruments_path_for, load_instruments, wired_to
from alteriom_hil.inventory import discover, probe_details, publish_inventory
from alteriom_hil.jobstore import JobCancelled, utcnow
from alteriom_hil.providers import Redactor, scrub_tree
from alteriom_hil.farm_shared import BOARD_ID_PATTERN, DEFAULT_PROFILE, ElsewhereError, PipelineError, RigBusyError, TARGETS
from alteriom_hil.farm_shared import (  # noqa: F401 -- re-exported: found here before core had them
    FARM_OWNED_ENV_KEYS,
    RIG_OWNED_ENV_PREFIXES,
    RUN_KINDS,
    RUN_KIND_ENV_KEY,
    _STATE_FILE_LOCK,
    _runtime_env_keys,
    refused_suite_env,
)


# How long an interrupted suite gets to tear down: restore the gateway, put
# the mesh back, and dump every board's serial log.
TEARDOWN_GRACE_SECONDS = 300


class ScrubbedLog:
    """A job log that never holds a provider's secret.

    Everything the farm writes goes through ``write``; a command's own output
    is read line by line and written the same way (FarmManager._run) whenever
    there is something to scrub. The log is scrubbed as it is written rather
    than afterwards because a node streams it to its portal while the run is
    still going.
    """

    def __init__(self, stream, redactor: Redactor):
        self._stream = stream
        self.redactor = redactor
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            return self._stream.write(self.redactor.scrub(text))

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _pump_output(stream, log) -> None:
    """Copy a command's output into a scrubbing log, a line at a time: a
    secret is one token of one line, never split across two."""
    for raw in iter(stream.readline, b""):
        log.write(raw.decode("utf-8", errors="replace"))
        log.flush()
    stream.close()


def _plural(count: int, noun: str) -> str:
    """`3 boards`, but `1 board`.

    A one-board profile is ordinary -- a console suite asks for exactly one --
    so "Validated 1 boards" is on the dashboard and in the run report for a
    large share of runs.
    """
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


class RigMixin:
    def _scrub_run_evidence(self, job_id: str, redactor: Redactor, log) -> list:
        """The last pass over a run's evidence before anything reads it.

        The job log was scrubbed as it was written and the suite scrubs the
        serial captures it dumps; this catches what neither sees -- JUnit
        failure text, run records, reports, anything a test or a tool wrote
        on its own. Never fails the job: a scrub that cannot rewrite a file
        says so in the log.
        """
        if not redactor.active:
            return []
        run_dir = self.state / "runs" / job_id
        try:
            changed = scrub_tree(run_dir, redactor)
        except OSError as exc:
            log.write(f"{utcnow()} Could not scrub the run's evidence: {exc.__class__.__name__}\n")
            return []
        if changed:
            log.write(
                f"{utcnow()} Scrubbed provider secrets from {len(changed)} evidence file(s): "
                + ", ".join(path.relative_to(run_dir).as_posix() for path in changed)
                + "\n"
            )
            log.flush()
        return changed

    def _run_job(self, job: dict, grant: allocation.Grant):
        job_id, kind, request = job["id"], job["kind"], job["request"]
        self._job_local.job_id = job_id
        self._current_job = job_id
        log_path = self.state / "logs" / f"{job_id}.log"
        # Read per job, so a link stored or removed since the service started
        # is what this run is scrubbed of (alteriom_hil.providers).
        redactor = Redactor.from_env(os.environ)
        try:
            try:
                with self._hold(grant):
                    with log_path.open("a", encoding="utf-8") as stream:
                        log = ScrubbedLog(stream, redactor)
                        try:
                            result = self._execute(job_id, kind, request, log)
                        finally:
                            # Before the job is marked finished: a CI client
                            # downloads the evidence as soon as it sees the
                            # status, and a node packages it for its portal.
                            self._scrub_run_evidence(job_id, redactor, log)
                self.store.update(job_id, "passed", result)
                self._emit_run(job_id, "passed", self.store.get(job_id) or {}, result)
            except JobCancelled as exc:
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(redactor.scrub(f"\nCANCELLED: {exc.summary}\n"))
                job = self.store.get(job_id) or {"progress": []}
                for stage in job["progress"]:
                    if stage["status"] == "running":
                        stage.update(status="skipped", summary=exc.summary, finished_at=utcnow())
                    elif stage["status"] == "pending":
                        stage["status"] = "skipped"
                self.store.update_progress(job_id, job["progress"])
                self.store.update(
                    job_id,
                    "cancelled",
                    {"summary": exc.summary, "detail": exc.detail, "cancelled": True},
                )
            except Exception as exc:
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(redactor.scrub(f"\nFAILED: {exc}\n"))
                if isinstance(exc, PipelineError):
                    self._stage(job_id, exc.stage, "failed", exc.summary)
                    result = {
                        # What the stage gathered before it failed, first, so
                        # a stage cannot overwrite the failure it is reporting.
                        **exc.result,
                        "summary": exc.summary,
                        "failed_stage": exc.stage,
                        "detail": redactor.scrub(exc.detail) if isinstance(exc.detail, str) else exc.detail,
                    }
                else:
                    result = {
                        "summary": "Farm job failed unexpectedly",
                        "detail": redactor.scrub(str(exc)),
                    }
                candidates = {
                    "manifest": self.state / "artifacts" / job_id / "manifest.json",
                    "results": self.state / "runs" / job_id / "results.xml",
                    "preflight": self.state / "runs" / job_id / "preflight.json",
                    "report": self.state / "runs" / job_id / "metrics" / "report.md",
                    "report_json": self.state / "runs" / job_id / "metrics" / "report.json",
                }
                result.update(
                    {name: str(path) for name, path in candidates.items() if path.is_file()}
                )
                self.store.update(job_id, "failed", result)
                self._emit_run(job_id, "failed", self.store.get(job_id) or {}, result)
        finally:
            hook = self.__dict__.get("_on_finished")
            if hook is not None:
                try:
                    hook(job_id)
                except Exception as exc:  # the node's report is retried from its outbox
                    print(f"farm-node: could not queue the report of {job_id}: {exc!r}", file=sys.stderr, flush=True)
            with self._cancel_lock:
                self._cancel_requests.pop(job_id, None)
            self._job_local.job_id = None
            if self.__dict__.get("_current_job") == job_id:
                self._current_job = None
            self._discard_workspace(job_id)
            self._release(job_id)
            # It wrote a bundle, its run evidence and its log, and a
            # measurement that crossed the build measured a bundle that
            # was still being written. Those figures are behind: the next
            # look measures again rather than calling them fresh.
            self._storage_changed()
            # Its boards are free: whatever was waiting for them may start.
            self.pending.put(("finished", job_id, {}))

    def _discover_for(self, grant: allocation.Grant | None, log) -> dict:
        """The inventory a run's discover stage works from.

        A job with the rig to itself rediscovers, as every run always did. A
        job sharing it must not: discovery opens every serial port with
        esptool, which resets the board on it, and would reset boards other
        runs hold mid-test. It runs against the last published inventory --
        the one it was allocated from -- and reads the silicon of its own
        boards only.
        """
        if grant is None or not grant.shared:
            return self.refresh_inventory(details="missing", log=log)
        inventory = self.inventory_snapshot()
        log.write(
            f"{utcnow()} Sharing the rig: not rediscovering, which would reset boards other "
            f"runs hold; using the inventory of {inventory.get('updated_at') or 'the last discovery'}\n"
        )
        log.flush()
        self.read_chip_details(
            {"boards": [board for board in inventory.get("boards") or [] if board.get("id") in grant.boards]},
            "missing", log,
        )
        return inventory

    def refresh_inventory(self, details: str | None = None, log=None) -> dict:
        """Rediscover the rig, and read the chips too.

        ``details`` says which connected boards get their silicon read after
        the discovery: ``"all"`` on an operator's rediscover and at service
        start, ``"missing"`` on a suite's own discover stage — that stage is
        on the run's clock, and a board whose reading is on file needs no
        second one. None reads nothing.
        """
        inventory = publish_inventory(self.registry, self.board_map, self.state,
                                      auto_register=self.auto_register())
        if details:
            self.read_chip_details(inventory, details, log)
        return inventory

    @staticmethod
    def auto_register() -> bool:
        """Whether a board found on a port is registered by the finding.

        `inventory.auto_register` in the host configuration, which is true
        unless somebody turned it off. hil_config writes it into runtime.env
        as ALTERIOM_HIL_AUTO_REGISTER and every unit sources that file; the
        admin CLI reads the same setting from the file itself. The service
        used to read neither and passed nothing, so a rig whose operator
        never ran `boards discover` listed its boards as unregistered for
        ever -- while verify-rig told them the boards register themselves.
        """
        return os.environ.get("ALTERIOM_HIL_AUTO_REGISTER", "1") != "0"

    def read_chip_details(self, inventory: dict, which: str = "all", log=None) -> int:
        """Read the silicon of the connected boards; how many answered.

        ``which`` is ``"all"`` or ``"missing"`` (only boards without a
        reading on file for this port). A board that will not answer esptool
        right now is noted in the log and skipped: it is not a discovery
        failure, and the rest are still read.
        """
        known = self.load_chip_details()
        read = 0
        for board in inventory.get("boards") or []:
            mac = normalize_mac(board["mac"]) if board.get("mac") else ""
            if which == "missing" and mac in known and known[mac].get("port") == board.get("port"):
                read += 1
                continue
            try:
                self._record_chip_details(board["id"], board["port"], board.get("mac"))
                read += 1
                if log:
                    log.write(f"Read chip details of {board['id']} on {board['port']}\n")
            except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
                if log:
                    log.write(f"Could not read chip details of {board['id']}: {exc}\n")
        return read

    def _record_chip_details(self, board_id: str, port: str, registered_mac: str | None) -> dict:
        details = probe_details(port, python=str(self.python))
        record = asdict(details)
        record["id"] = board_id
        record["registered_mac"] = registered_mac
        # A mismatch means the board on that port is not the registered one.
        # Surfacing it is the point: it is exactly what a stale board map
        # looks like from the outside.
        record["matches_registry"] = bool(registered_mac) and normalize_mac(
            registered_mac
        ) == normalize_mac(details.mac)
        with _STATE_FILE_LOCK:
            known = self.load_chip_details()
            known[normalize_mac(details.mac)] = record
            tmp = self.chip_details_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(known, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.chip_details_path)
        return record

    def _not_on_portal(self, what: str) -> None:
        if self.__dict__.get("mode") == "portal":
            raise ElsewhereError(f"{what} is done on the worker that has the board, not on the portal")

    def register_device(self, board_id: str, mac: str) -> dict:
        self._not_on_portal("registering a board")
        if not isinstance(board_id, str) or not BOARD_ID_PATTERN.fullmatch(board_id):
            raise ValueError(
                "id must be 1-32 lowercase letters, digits, dots, underscores, or hyphens"
            )
        if not isinstance(mac, str):
            raise ValueError("mac must be a string")
        normalized_mac = normalize_mac(mac)
        with farm_shared.RIG_LOCK_PATH.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            registrations = load_registry(self.registry)
            instruments = load_instruments(instruments_path_for(self.registry), registrations)
            if any(board.id == board_id for board in registrations):
                raise ValueError(f"board id already registered: {board_id}")
            if any(normalize_mac(board.mac) == normalized_mac for board in registrations):
                raise ValueError(f"MAC already registered: {normalized_mac}")
            # An instrument is an ESP like any board; registered as one it
            # would be flashed with a suite's firmware and meshed with the rig.
            instrument = next((item for item in instruments if item.mac == normalized_mac), None)
            if instrument is not None:
                raise ValueError(f"MAC {normalized_mac} is the instrument {instrument.id}, not a board")
            if any(item.id == board_id for item in instruments):
                raise ValueError(f"{board_id} is an instrument's id")
            devices, errors = discover()
            device = next(
                (item for item in devices if normalize_mac(item.mac) == normalized_mac),
                None,
            )
            if device is None:
                raise ValueError("MAC is not present in the current USB discovery")
            registrations.append(
                Board(
                    id=board_id,
                    port=device.port,
                    chip=device.chip,
                    target=device.target,
                    mac=device.mac,
                    tags=["mesh"],
                )
            )
            write_registry(self.registry, registrations)
            result = reconcile(registrations, devices, instruments)
            write_active_map(self.board_map, result)
            snapshot = write_inventory_snapshot(self.state, result, errors)
        return {"registered": board_id, "mac": normalized_mac, "inventory": snapshot}

    def device_details(self, board_id: str) -> dict:
        """Ask one connected board what it is, right now.

        The registry records identity; this reads the silicon. It answers the
        questions the inventory cannot: which die revision, how much flash and
        from which vendor, what the crystal is, whether the console runs over
        the chip's own USB or a bridge — and whether the part on that port is
        still the one that was registered there.

        Serial access is exclusive, so this takes the board's lock and the rig
        lock shared, but never waits for either: a validation run holds them
        for minutes, and a dashboard request that blocks that long is
        indistinguishable from a hang. A busy board says so, and who has it,
        which is something an operator can act on. A board no running job
        holds is readable while other boards run -- unless a job has the rig
        to itself.
        """
        if not isinstance(board_id, str) or not BOARD_ID_PATTERN.fullmatch(board_id):
            raise ValueError("invalid board id")
        snapshot = self.inventory_snapshot()
        board = next(
            (item for item in snapshot.get("boards", []) if item.get("id") == board_id),
            None,
        )
        if board is None:
            raise LookupError(
                f"{board_id} is not a connected board; only connected boards can be probed"
            )
        self._not_on_portal("reading a chip")
        holder = next(
            (held for held in self.reservations() if board_id in held["boards"]), None
        ) if "_reservation_lock" in self.__dict__ else None
        if holder is not None:
            raise RigBusyError(
                f"{board_id} is in use by run {holder['job_id'][:8]} ({holder['label']}); "
                f"its details are readable once that run ends"
            )
        with farm_shared.RIG_LOCK_PATH.open("w") as lock, farm_shared.board_lock_path(board_id).open("w") as own:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(own, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RigBusyError(
                    "the rig is busy with a job; board details are readable once it is idle"
                ) from exc
            return self._record_chip_details(board_id, board["port"], board.get("mac"))

    def unregister_device(self, board_id: str) -> dict:
        self._not_on_portal("unregistering a board")
        if not isinstance(board_id, str) or not BOARD_ID_PATTERN.fullmatch(board_id):
            raise ValueError("invalid board id")
        with farm_shared.RIG_LOCK_PATH.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            registrations = load_registry(self.registry)
            remaining = [board for board in registrations if board.id != board_id]
            if len(remaining) == len(registrations):
                raise ValueError(f"board id is not registered: {board_id}")
            instruments = load_instruments(instruments_path_for(self.registry), registrations)
            # A wire to a board that is no longer registered would leave the
            # instrument registry invalid, and every later discovery with it.
            wired = wired_to(instruments, board_id)
            if wired:
                raise ValueError(f"{board_id} is wired to {', '.join(wired)}; unwire it first")
            devices, errors = discover()
            write_registry(self.registry, remaining)
            result = reconcile(remaining, devices, instruments)
            write_active_map(self.board_map, result)
            snapshot = write_inventory_snapshot(self.state, result, errors)
        return {"unregistered": board_id, "inventory": snapshot}

    def _run(
        self,
        args: list[str],
        log,
        env: dict | None = None,
        timeout: int | None = None,
        grace: int = TEARDOWN_GRACE_SECONDS,
        cwd: Path | None = None,
    ):
        # Defaults to the farm checkout; a consumer profile passes its own
        # per-job workspace so the project's scripts see their own repository.
        workdir = cwd or self.repo
        log.write("$ " + " ".join(args) + "\n")
        log.flush()
        redactor = getattr(log, "redactor", None)
        pump = None
        if redactor is not None and redactor.active:
            # The rig holds a provider secret, so the command's output cannot
            # go straight to the log file: it is read and scrubbed first.
            proc = subprocess.Popen(
                args, cwd=workdir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            pump = threading.Thread(target=_pump_output, args=(proc.stdout, log), daemon=True)
            pump.start()
        else:
            proc = subprocess.Popen(
                args, cwd=workdir, env=env, stdout=log, stderr=subprocess.STDOUT
            )
        try:
            self._wait_for(proc, args, log, timeout, grace)
        finally:
            if pump is not None:
                # A grandchild that kept the pipe open must not hold the job.
                pump.join(timeout=10)

    def _wait_for(self, proc, args, log, timeout, grace):
        """Wait for a command _run started, honouring cancellation and the
        safety limit."""

        def interrupt(why: str):
            # Interrupt before killing. pytest turns SIGINT into a
            # KeyboardInterrupt and still runs fixture teardown, which is
            # where the gateway is put back and the boards' serial logs are
            # written; a kill ended the run with no evidence of what it had
            # been doing, which is the one thing a run that overran needed
            # to leave behind. The grace period is bounded so a fixture that
            # is truly wedged still cannot hold the rig.
            log.write(f"\n! {why}; interrupting so teardown can run (up to {grace}s)\n")
            log.flush()
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                log.write("! teardown did not finish in time; killing\n")
                log.flush()
                proc.kill()
                proc.wait()

        deadline = None if timeout is None else time.monotonic() + timeout
        # Waiting in short slices is what lets a cancellation reach a run
        # that has twenty minutes left on its safety limit.
        while True:
            try:
                proc.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                pass
            running = getattr(getattr(self, "_job_local", None), "job_id", None)
            cancel = self._cancel_requested(running or getattr(self, "_current_job", None))
            if cancel is not None:
                interrupt(f"cancelled: {cancel[0]}")
                raise JobCancelled(*cancel)
            if deadline is not None and time.monotonic() >= deadline:
                interrupt(f"{timeout}s safety limit reached")
                raise subprocess.TimeoutExpired(args, timeout)
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, args)

    @staticmethod
    def pytest_selection(tests: list[str], keyword: str, suite_dir: str | Path) -> list[str]:
        """The pytest arguments a selection means: the named files or tests
        under the suite directory, or the whole directory, plus -k.

        `suite_dir` is the running profile's suite path, relative to that
        profile's workspace."""
        directory = Path(suite_dir).as_posix()
        args = [f"{directory}/{item}" for item in tests] or [directory]
        if keyword:
            args.extend(("-k", keyword))
        return args

    def _clone_credentials(self, spec, log) -> tuple[str, dict | None]:
        """The URL and environment to clone this profile's repository with.

        The reference suite is public, so a rig needed no credential and had
        none. A first-party consumer's repository usually is *not* public, and
        a clone without a credential fails at the first stage of an
        already-scheduled run with "could not read Username for
        'https://github.com'".

        The token is passed through GIT_ASKPASS rather than embedded in the
        URL: a URL reaches the process table, the log line above it, and git's
        own error messages. Only a username goes in the URL, so git knows not
        to prompt for one.

        No token file means no credential -- correct for a public repository,
        and a clear failure for a private one.
        """
        if not self.CONSUMER_TOKEN_PATH.is_file():
            return spec.repo, None
        if not os.access(self.CONSUMER_TOKEN_PATH, os.R_OK):
            # Present but unreadable is its own failure, and a silent one
            # otherwise: the askpass helper would return nothing, git would see
            # an empty password, and the run would fail as "authentication
            # failed" against a token that is perfectly valid. The service does
            # not run as root, so the file has to be group-readable by the
            # service account, exactly like the API token beside it.
            raise PipelineError(
                "build",
                "The consumer credential cannot be read",
                f"{self.CONSUMER_TOKEN_PATH} exists but is not readable by this "
                f"service. Match the API token's permissions: "
                f"chmod 640 and chgrp to the service group.",
            )
        askpass = self.state / "git-askpass.sh"
        askpass.write_text(
            "#!/bin/sh\n"
            "# Answers git's password prompt from the token file. Written here\n"
            "# so the token never reaches argv, a URL, or this log.\n"
            f"cat {self.CONSUMER_TOKEN_PATH}\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        url = spec.repo
        host = url.split("//", 1)[1].split("/", 1)[0] if "//" in url else ""
        if url.startswith("https://") and "@" not in host:
            url = "https://x-access-token@" + url[len("https://"):]
        log.write(f"{utcnow()} Using the configured consumer credential for this clone\n")
        log.flush()
        return url, dict(os.environ, GIT_ASKPASS=str(askpass), GIT_TERMINAL_PROMPT="0")

    def _workspace(self, spec, ref: str, job_id: str, log) -> Path:
        """The directory a profile's commands run in.

        A `farm` profile runs in this checkout, as painlessMesh always has. A
        `consumer` profile is checked out fresh per job: its build script and
        its suite are versioned with the firmware, so the run has to use the
        pair that were committed together, not a working copy left behind by
        whatever ran last.

        Cloned shallow at the requested ref -- the farm needs one tree, not the
        project's history -- into the job's own directory, so two jobs cannot
        share a checkout and quietly test each other's code.
        """
        if not spec.runs_in_consumer_repo:
            return self.repo
        workspace = self.state / "workspaces" / job_id
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
        workspace.parent.mkdir(parents=True, exist_ok=True)
        log.write(f"{utcnow()} Cloning {spec.repo} at {ref} into {workspace}\n")
        log.flush()
        url, env = self._clone_credentials(spec, log)
        # Initialise, fetch exactly the requested ref one commit deep, and
        # check out what was fetched. Works for a branch, a tag or a commit
        # SHA, on any git.
        #
        # It used to be `git clone --depth 1 --revision <ref>`, which is git
        # 2.49 and newer -- the farm host runs 2.47 -- with a fallback of a
        # full clone and `git checkout --detach <ref>`. That fallback resolves
        # a SHA, a tag, and the default branch, and nothing else: after a
        # clone every other branch exists only as origin/<name>. So every run
        # of a consumer at any branch but its default failed at checkout, and
        # the first fix branch pushed for hardware validation never reached
        # the boards. A pull request's head is a branch or a SHA; both have to
        # work, because that is what a consumer's CI sends.
        try:
            workspace.mkdir(parents=True, exist_ok=True)
            self._run(["git", "init", "--quiet", str(workspace)], log, env)
            self._run(["git", "-C", str(workspace), "remote", "add", "origin", url], log, env)
            self._run(["git", "-C", str(workspace), "fetch", "--depth", "1", "origin", ref], log, env)
            self._run(["git", "-C", str(workspace), "checkout", "--quiet", "--detach", "FETCH_HEAD"], log, env)
        except subprocess.CalledProcessError as exc:
            raise PipelineError(
                "build",
                f"Could not check out {spec.label} at {ref}",
                f"git fetch/checkout failed: {exc}",
            ) from exc
        return workspace

    def _scoped_board_map(self, spec, job_id: str, log, named: list[str] | None = None,
                          exact: bool = False) -> Path:
        """The board map this run is allowed to touch.

        A request that named boards gets exactly those, whatever the profile
        says: "check this board" is an allocation the profile cannot express,
        and the canary is per-board by nature -- one board is a complete
        answer about that board.

        Otherwise an exclusive profile takes the bank and gets the active map
        unchanged -- painlessMesh forms one mesh from every board, and
        narrowing it would break the suite -- and a shared profile gets a map
        containing only the boards its `needs` ask for. That is what makes a
        one-board profile possible at all: the pipeline's coverage check, the
        flasher and the suite all read the map they are given, so an unscoped
        run would demand artifacts for every connected family and flash the
        whole rig for a suite that drives one board. This is step 1 of
        docs/farm-allocation.md.

        ``exact``: the boards named are the whole allocation, needs included
        -- a job sharing the rig was given its boards by the dispatcher, and
        may not take one more.
        """
        store = self.__dict__.get("store")
        holds = store.holds() if store is not None else {}
        if spec.exclusive and not named and not holds:
            return self.board_map
        document = yaml.safe_load(self.board_map.read_text(encoding="utf-8")) or {}
        available = list(document.get("boards") or [])
        chosen: list[dict] = []
        if not named:
            # Never a board held out of the pool: reserved for bench work, or
            # quarantined for failing its own canary checks. A run that named
            # its boards was checked for this when it was submitted.
            left_out = [board for board in available if board.get("id") in holds]
            available = [board for board in available if board.get("id") not in holds]
            for board in left_out:
                hold = holds[board["id"]]
                log.write(
                    f"{utcnow()} Left out {board['id']}: {hold['state']}"
                    + (f" ({hold['reason']})" if hold.get("reason") else "") + "\n"
                )
            if spec.exclusive:
                chosen, available = available, []
        if named:
            wanted = set(named)
            chosen = [board for board in available if board.get("id") in wanted]
            gone = sorted(wanted - {board.get("id") for board in chosen})
            if gone:
                raise PipelineError(
                    "discover",
                    "A board this run named is not connected",
                    f"{', '.join(gone)} was named by this run and is not in the "
                    f"active board map; it was connected when the run was "
                    f"submitted.",
                )
            available = [board for board in available if board.get("id") not in wanted]
        for need in () if exact else spec.needs:
            matching = [
                b for b in available
                if b.get("target") == need["target"] and set(need.get("tags") or ()) <= set(b.get("tags") or ())
            ]
            if len(matching) < need["count"]:
                if not need.get("optional"):
                    raise PipelineError(
                        "discover",
                        "Not enough boards for this profile",
                        f"{spec.label} needs {need['count']} x {need['target']}; "
                        f"{len(matching)} connected.",
                    )
                # Wanted, not required: take what is there and go on. What
                # the run went without is not lost here -- the pipeline reads
                # it back off this map (allocation.coverage) and the run's
                # result and report name it, so this is a smaller run and not
                # a quieter one.
                log.write(
                    f"{utcnow()} Not covered: {need['target']} "
                    f"({len(matching)} of {need['count']} wanted, optional); "
                    f"the run goes without it\n"
                )
            for board in matching[: need["count"]]:
                chosen.append(board)
                available.remove(board)
        scoped = self.state / "runs" / job_id / "board-map.yaml"
        scoped.parent.mkdir(parents=True, exist_ok=True)
        if holds and not named and len(chosen) < spec.min_boards:
            raise PipelineError(
                "discover",
                "Not enough boards for this profile",
                f"{spec.label} requires at least {spec.min_boards} board(s); "
                f"{len(chosen)} left once boards held out of the pool are set aside"
                + (f" ({', '.join(sorted(holds))})" if holds else "") + ".",
            )
        payload: dict = {"boards": chosen}
        # The instruments wired to what this run was given, with only those
        # wires: a run may drive a jumper to its own boards, and nobody
        # else's.
        ids = {board.get("id") for board in chosen}
        wired = []
        for item in document.get("instruments") or []:
            wires = [wire for wire in item.get("wiring") or [] if wire.get("board") in ids]
            if wires:
                wired.append({**item, "wiring": wires})
        if wired:
            payload["instruments"] = wired
        scoped.write_text(
            "# Scoped to this run's allocation; see farm_service._scoped_board_map.\n"
            + yaml.safe_dump(payload, sort_keys=False),
            encoding="utf-8",
        )
        log.write(
            f"{utcnow()} Scoped board map: {[b['id'] for b in chosen]} of "
            f"{len(document.get('boards') or [])} connected\n"
        )
        log.flush()
        return scoped

    def _discard_workspace(self, job_id: str):
        """Remove a consumer job's checkout once the job is over.

        Only the source tree goes: artifacts, serial logs, metrics and the run
        log live elsewhere under state/ and are the evidence a failed run is
        read from. The checkout is reproducible from the ref and is not.

        Without this every consumer run leaves a repository behind for good.
        On a host whose git predates `clone --revision` the fallback is a
        full-history clone, so routine validation would fill the Pi's disk one
        run at a time -- and the first symptom would be an unrelated job
        failing to write.
        """
        workspace = self.state / "workspaces" / job_id
        if not workspace.exists():
            return
        try:
            shutil.rmtree(workspace)
        except OSError as exc:
            # Never fail a finished job over cleanup; the run's verdict is
            # already decided and a leaked directory is a disk-space problem,
            # not a correctness one.
            log_path = self.state / "logs" / f"{job_id}.log"
            try:
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(f"{utcnow()} Could not remove workspace {workspace}: {exc}\n")
            except OSError:
                pass

    def _execute(self, job_id: str, kind: str, request: dict, log) -> dict:
        if kind == "inventory":
            # The discovery's log is its evidence: which ports answered as
            # what, which devices are not registered, which ports failed. It
            # used to write nothing, so an operator opening the run saw "No
            # log output available" for a job that had just read the rig.
            self._stage(job_id, "discover", "running")
            log.write(f"{utcnow()} Discovering serial devices on the USB tree\n")
            log.flush()
            inventory = self.refresh_inventory()
            for board in inventory["boards"]:
                log.write(
                    f"  connected  {board['id']:<16} {board.get('target', '?'):<10} "
                    f"{board.get('mac', '?')}  {board.get('port', '?')}\n"
                )
            for device in inventory.get("unregistered") or []:
                log.write(
                    f"  unregistered {device.get('target', '?'):<10} {device.get('mac', '?')}  "
                    f"{device.get('port', '?')}\n"
                )
            for board_id in inventory.get("missing") or []:
                log.write(f"  missing    {board_id} (registered, not present)\n")
            for error in inventory.get("probe_errors") or []:
                log.write(f"  probe failed {error.get('port', '?')}: {error.get('error', '')}\n")
            summary = (
                f"{len(inventory['boards'])} connected, {len(inventory['unregistered'])} unregistered, "
                f"{len(inventory['missing'])} missing"
            )
            log.write(f"{utcnow()} Discovery: {summary}\n")
            log.flush()
            self._stage(job_id, "discover", "passed", summary)
            self._stage(job_id, "details", "running")
            log.write(f"{utcnow()} Reading chip details with esptool\n")
            log.flush()
            read = self.read_chip_details(inventory, "all", log)
            described = f"{read} of {len(inventory['boards'])} boards described"
            log.write(f"{utcnow()} Chip details: {described}\n")
            log.flush()
            self._stage(
                job_id,
                "details",
                "passed" if read == len(inventory["boards"]) else "failed",
                described,
            )
            return {"summary": f"{summary}; {described}", **inventory}
        # The commit the ref named at submission, when the remote could say:
        # a branch that moved while this job waited is still validated at
        # the commit that was asked for, and the newer commit's own job is
        # what supersedes this one.
        ref = request.get("resolved_sha") or request.get("ref", "main")
        profile = request.get("profile", DEFAULT_PROFILE)
        spec = self.profiles[profile]
        if kind == "build":
            # Queued before the upgrade that took building out of the farm.
            raise PipelineError(
                "build", "The farm does not build firmware",
                "This build job was queued before the farm stopped accepting builds. "
                "Build the bundle in the project's CI and run the suite with it.",
            )
        # For a farm profile this is the farm checkout and costs nothing; for a
        # consumer profile it clones the project at `ref`, which is where its
        # suite comes from.
        workspace = self._workspace(spec, ref, job_id, log)
        targets = request.get("targets", sorted(TARGETS))
        artifact_dir = self.state / "artifacts" / job_id
        selection = {"tests": list(request.get("tests") or []), "keyword": request.get("keyword") or ""}
        partial = bool(selection["tests"] or selection["keyword"])
        # A run may name the bundle it flashes. Its producer built it, the
        # service verified it at upload, and _validate has already held it to
        # this run's commit, agent and families; here it is simply linked in
        # place of a build, exactly as a reused build is.
        supplied = request.get("artifact") if kind == "suite" else None
        supplied_origin = None
        if supplied:
            with self._artifact_guard():
                source_dir = self.artifact_root / supplied
                if not (source_dir / "manifest.json").is_file():
                    raise PipelineError(
                        "build",
                        "The supplied bundle is gone",
                        f"Bundle {supplied[:8]} was removed between submission and this run.",
                    )
                self._link_artifacts(artifact_dir, source_dir)
            provenance = self.bundle_provenance(supplied) or {}
            supplied_origin = self._supply_origin(provenance)
            log.write(
                f"{utcnow()} Flashing the bundle {supplied[:8]} supplied by {supplied_origin}\n"
            )
            log.flush()
        reuse = bool(request.get("reuse", True)) and kind == "suite" and not supplied
        reused_from = None
        if reuse and request.get("resolved_sha"):
            # Under the artifact lock: once the link exists this run holds the
            # bundle and the store refuses to delete it, but until then an
            # operator's delete could remove what was just chosen.
            with self._artifact_guard():
                found = self.reusable_artifacts(
                    request["resolved_sha"], targets, spec.revision_key, profile
                )
                if found:
                    reused_from, source_dir = found
                    self._link_artifacts(artifact_dir, source_dir)
            if found:
                log.write(f"{utcnow()} Flashing the held bundle {reused_from[:8]} for {request['resolved_sha']}\n")
                log.flush()
        self._stage(job_id, "build", "running", bundle=reused_from or supplied)
        if not (reused_from or supplied):
            # Refused at submit, so reaching here means the bundle that was
            # found then is gone now -- pruned, or deleted between the two.
            # There is no fallback: the farm does not build.
            raise PipelineError(
                "build",
                "No firmware bundle for this run",
                f"The farm does not build firmware, and the {spec.label} bundle this "
                f"run was going to flash is no longer on disk. Run "
                f"{spec.supply_workflow or 'its build workflow'} again.",
            )
        manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
        # Required, with no fallback. Substituting the checkout's HEAD looked
        # forgiving and was silently wrong: for a farm-located profile the
        # workspace is *this* repository, so a painlessMesh build whose key was
        # misspelled would have been recorded, reported and gated under an
        # unrelated farm commit. A misconfigured key must fail the run, not
        # rename the thing under test.
        revision = manifest.get(spec.revision_key)
        if not revision:
            raise PipelineError(
                "build",
                "The bundle names no revision",
                f"manifest.json has no {spec.revision_key!r}; the profile's "
                f"build.revision_key must name a key the project's build writes "
                f"(found: {sorted(k for k in manifest if k != 'targets')}).",
            )
        # The version a person reads, when the build stamped one (schema-2
        # `version`, optional): the run's title and result carry it beside
        # the revision, which stays the record. A bundle without one is
        # titled by its short revision, as before.
        version = manifest.get("version") if isinstance(manifest.get("version"), str) else None
        simulation = request.get("simulation")
        if simulation and simulation.get(spec.revision_key) != revision:
            self._stage(
                job_id,
                "mesh_sim",
                "failed",
                "Simulator evidence does not match the bundle's commit",
            )
            raise PipelineError(
                "mesh_sim",
                "Simulator evidence revision mismatch",
                f"Simulator tested {simulation.get(spec.revision_key)}; the bundle is {revision}.",
            )
        # A verified bundle in place is this stage passing: it is the first
        # thing a run does now, not a build it was spared.
        if supplied:
            self._stage(job_id, "build", "passed", f"Supplied by {supplied_origin}", bundle=supplied)
        else:
            self._stage(
                job_id, "build", "passed",
                f"Held bundle {reused_from[:8]} for this commit", bundle=reused_from,
            )

        self._stage(job_id, "discover", "running")
        grant = self._grant(job_id)
        inventory = self._discover_for(grant, log)
        if len(inventory["boards"]) < spec.min_boards:  # rig-wide floor, checked before scoping
            raise PipelineError(
                "discover",
                "Not enough connected boards",
                f"{spec.label} requires at least {spec.min_boards} board(s); "
                f"{len(inventory['boards'])} connected.",
            )
        if grant is not None and grant.shared:
            board_map = self._scoped_board_map(spec, job_id, log, list(grant.boards), exact=True)
        else:
            board_map = self._scoped_board_map(
                spec, job_id, log,
                self._check_named_boards(request["boards"], profile) if request.get("boards") else None,
            )
        scoped = yaml.safe_load(board_map.read_text(encoding="utf-8")) or {}
        scoped_boards = scoped.get("boards") or []
        self._hold_boards(job_id, [board.get("id") for board in scoped_boards])
        # Coverage is judged against the boards this run may touch, not against
        # everything plugged into the rig: a one-board profile must not be
        # required to ship artifacts for families it will never flash.
        needed_targets = {board["target"] for board in scoped_boards}
        missing_artifacts = needed_targets - set(targets)
        if missing_artifacts:
            raise PipelineError(
                "discover",
                "Artifact selection does not cover this run's boards",
                f"Missing artifact families: {sorted(missing_artifacts)}",
            )
        # What this run's allocation does not meet: a family the profile asks
        # for that was not on the rig, was held out, or was in use by another
        # run. Read off the boards it actually got, so it is right both for a
        # run scoped here and for one handed its boards by the dispatcher.
        not_covered = allocation.coverage(spec.needs, scoped_boards)
        shortfall = allocation.coverage_text(not_covered)
        if not_covered:
            log.write(f"{utcnow()} Not covered by this run: {shortfall}\n")
            log.flush()
        self._stage(
            job_id, "discover", "passed",
            f"{len(scoped_boards)} of {len(inventory['boards'])} boards allocated"
            + (f"; not covered: {shortfall}" if not_covered else ""),
        )
        rendered = {
            key: spec.render(value, revision=revision, ref=ref, artifact_dir=str(artifact_dir),
                             board_map=str(board_map), workspace=str(workspace),
                             python=str(self.python), targets_csv=",".join(targets),
                             farm_repo=str(self.repo), farm_runner=str(self.repo / "runner"))
            for key, value in spec.env.items()
        }
        # The dispatch's own settings for the suite go under everything the
        # farm sets: a run may choose which family plays the gateway, never
        # where the board map or the log directory is. Checked at submission;
        # checked again here because a node runs what its portal leased it,
        # and a portal from before the check would have let a rig's own name
        # through.
        base_env = dict(os.environ)
        for key, value in (request.get("env") or {}).items():
            refused = refused_suite_env(key, str(value))
            if refused:
                log.write(f"{utcnow()} Ignoring the dispatch's {key}: {refused}\n")
                continue
            base_env[key] = value
        env = dict(base_env, ALTERIOM_HIL_MODE="hardware", ALTERIOM_HIL_BOARD_MAP=str(board_map),
                   ALTERIOM_HIL_ARTIFACT_DIR=str(artifact_dir), **rendered,
                   HIL_FIRMWARE_SHA=revision,
                   HIL_FIRMWARE_VERSION=version or "",
                   # Absent for a profile whose build emits no agent of its own.
                   HIL_AGENT_SHA=str(manifest.get("hil_agent_sha", "")),
                   ALTERIOM_HIL_LOG_DIR=str(self.state / "runs" / job_id / "serial"),
                   ALTERIOM_HIL_RUN_LOG=str(self.state / "runs" / job_id / "metrics" / "runs.jsonl"))
        preflight = self.state / "runs" / job_id / "preflight.json"
        preflight_cmd = spec.render(
            spec.preflight_command,
            python=str(self.python),
            board_map=str(board_map),
            manifest=str(artifact_dir / "manifest.json"),
            preflight=str(preflight),
            log_dir=str(self.state / "runs" / job_id / "serial"),
            artifact_dir=str(artifact_dir),
            workspace=str(workspace),
            revision=revision,
            ref=ref,
            targets_csv=",".join(targets),
            farm_repo=str(self.repo),
            farm_runner=str(self.repo / "runner"),
        ) if spec.has_preflight else []
        already_flashed = False
        if reuse and spec.has_preflight:
            # The preflight asks every board what it runs. Asked before the
            # flash, a pass means the boards already carry this image — the
            # usual case in a debugging iteration — and the flash is skipped.
            self._stage(job_id, "preflight", "running", "Checking whether the boards already run this revision")
            log.write(f"{utcnow()} Checking whether the boards already run {revision[:10]}\n")
            log.flush()
            try:
                self._run(preflight_cmd, log, env, timeout=spec.preflight_timeout, cwd=workspace)
                already_flashed = True
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                log.write(f"{utcnow()} At least one board does not run it; flashing\n")
                log.flush()
        if already_flashed:
            self._stage(job_id, "flash", "skipped", f"Boards already run {revision[:10]}; not flashed")
            self._stage(job_id, "preflight", "passed", f"Verified {len(scoped_boards)} board identities (already flashed)")
        else:
            self._stage(job_id, "flash", "running")
            try:
                flash_cmd = spec.render(
                    spec.flash_command,
                    python=str(self.python),
                    artifact_dir=str(artifact_dir),
                    board_map=str(board_map),
                    workspace=str(workspace),
                    revision=revision,
                    ref=ref,
                    manifest=str(artifact_dir / "manifest.json"),
                    targets_csv=",".join(targets),
                    farm_repo=str(self.repo),
                    farm_runner=str(self.repo / "runner"),
                )
                self._run(flash_cmd, log, env, cwd=workspace)
            except subprocess.CalledProcessError as exc:
                raise PipelineError("flash", "One or more devices failed to flash", str(exc)) from exc
            self._stage(job_id, "flash", "passed", f"Flashed {len(scoped_boards)} boards")
            if not spec.has_preflight:
                # Nothing to ask the boards: this profile runs stock firmware
                # with no identity protocol. Recorded as skipped rather than
                # passed, so a report never claims a check that did not run.
                #
                # The run must not reach _run() here: preflight_cmd is [] for
                # such a profile, and Popen([]) raises IndexError -- which would
                # fail every consumer suite immediately after a successful
                # flash, without ever reaching pytest.
                self._stage(job_id, "preflight", "skipped", "Profile has no board identity protocol")
            else:
                self._stage(job_id, "preflight", "running")
                try:
                    self._run(preflight_cmd, log, env, timeout=spec.preflight_timeout, cwd=workspace)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                    raise PipelineError(
                        "preflight",
                        "Flashed device protocol verification failed",
                        "At least one board did not boot the expected artifact; see preflight.json and serial logs.",
                    ) from exc
                self._stage(job_id, "preflight", "passed", f"Verified {len(scoped_boards)} board identities")
        results = self.state / "runs" / job_id / "results.xml"
        results.parent.mkdir(parents=True, exist_ok=True)
        test_failure = None
        chosen = self.pytest_selection(selection["tests"], selection["keyword"], spec.suite_path)
        described = (
            f"{len(selection['tests'])} selected test file(s)/test(s)" if selection["tests"] else "the suite"
        ) + (f" matching '{selection['keyword']}'" if selection["keyword"] else "")
        self._stage(job_id, "test", "running", f"Running {described}" if partial else None)
        # The profile's own runner when it has one. It is given {results} and
        # owes the farm a JUnit document there: that is the whole contract, and
        # it is what lets a suite that is not pytest be built, run and reported
        # by the same pipeline. Without one, pytest over suite.path -- which is
        # what every profile did before, and what a pytest suite still wants,
        # since the HAL's plugin then writes the richer per-test records.
        if spec.has_test_command:
            test_cmd = spec.render(
                spec.test_command,
                python=str(self.python),
                board_map=str(board_map),
                results=str(results),
                log_dir=str(self.state / "runs" / job_id / "serial"),
                manifest=str(artifact_dir / "manifest.json"),
                artifact_dir=str(artifact_dir),
                workspace=str(workspace),
                revision=revision,
                ref=ref,
                targets_csv=",".join(targets),
                farm_repo=str(self.repo),
                farm_runner=str(self.repo / "runner"),
            )
        else:
            test_cmd = [str(self.python), "-m", "pytest", *chosen, "-v", f"--junitxml={results}"]
        try:
            suite_timeout = int(os.environ.get("ALTERIOM_HIL_SUITE_TIMEOUT", spec.suite_timeout))
            self._run(
                test_cmd,
                log,
                env,
                timeout=suite_timeout,
                cwd=workspace,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            test_failure = exc
        else:
            self._stage(job_id, "test", "passed", f"Partial run passed: {described}" if partial else "Validation suite passed")
        metrics = self.state / "runs" / job_id / "metrics"
        report = metrics / "report.md"
        report_json = metrics / "report.json"
        self._stage(job_id, "report", "running")
        try:
            report_cmd = [
                str(self.python), "-m", "alteriom_hil.report", str(metrics),
                "--title", spec.render(spec.report_title, revision=revision, ref=ref,
                                       version=version or str(revision)[:12]),
                "--out", str(report), "--json-out", str(report_json),
                # What the report reads when the HAL's pytest plugin wrote no
                # records: the JUnit the test stage required, plus the run facts
                # the plugin would have stamped on each record itself.
                "--junit", str(results), "--suite", profile, "--mode", "hardware",
                "--boards", str(len(scoped_boards)), "--firmware-sha", revision,
            ]
            # The families this run did not exercise, so the report says so
            # at the top instead of reading as the whole gate. A report that
            # cannot be told is a report that quietly claims more than it ran.
            for entry in not_covered:
                report_cmd += [
                    "--not-covered",
                    f"{entry['target']}={entry['got']}/{entry['wanted']}",
                ]
            if spec.capabilities:
                # The consumer's own catalog, from its own checkout. Without one
                # the report has no coverage table -- not someone else's.
                report_cmd += ["--capabilities", str(workspace / spec.capabilities)]
            # The images this run flashed, so the report can say how much of
            # its app slot each family's image takes: the number a consumer
            # choosing a cheaper part needs per commit, not after the choice.
            manifest = artifact_dir / "manifest.json"
            if manifest.is_file():
                report_cmd += ["--manifest", str(manifest)]
            # What the suite's uplink tests measured with one board as the
            # gateway, when the run had any: the report's gateway matrix.
            matrix = self.mqtt_evidence_dir(job_id) / "gateway-matrix.jsonl"
            if matrix.is_file():
                report_cmd += ["--gateway-matrix", str(matrix)]
            self._run(report_cmd, log, env)
        except subprocess.CalledProcessError as exc:
            raise PipelineError(
                "report", "Result report could not be generated", str(exc)
            ) from exc
        self._stage(job_id, "report", "passed", "Machine and human-readable reports generated")
        health = None
        if profile == self.CANARY_PROFILE:
            # Before the failure is raised, because a canary that failed is
            # exactly when each board's verdict matters.
            try:
                health = self._record_board_health(job_id, results, revision, version) or None
            except OSError as exc:
                log.write(f"{utcnow()} Could not record board health: {exc}\n")
            if health and health["farm_wide"] and self.__dict__.get("mode") != "node":
                # Red on every board is the farm, and the farm is what runs
                # everything queued behind this. Pausing is the farm's own
                # decision rather than CI's: whatever asked for the check --
                # a deploy, an operator, a timer -- the next consumer's run
                # must not flash boards whose rig cannot join a network or
                # reach its broker and then report that as their failure.
                # An operator resumes it, and the reason says what to fix.
                checks = ", ".join(health["farm_wide"])
                self.pause(
                    f"the Rig Health Check failed on every board: {checks} (run {job_id[:8]})"
                )
                log.write(
                    f"{utcnow()} Queue paused: the Rig Health Check failed farm-wide ({checks})\n"
                )
            if health and self.__dict__.get("mode") != "node":
                self._notify_boards_red(job_id, health)
        if test_failure:
            detail = (
                f"Validation exceeded the {suite_timeout}-second safety limit."
                if isinstance(test_failure, subprocess.TimeoutExpired)
                else f"{'the test command' if spec.has_test_command else 'pytest'} exited with status {test_failure.returncode}; see the report and log for evidence."
            )
            raise PipelineError(
                "test",
                "Validation tests did not complete successfully",
                detail,
                # A health check that failed is exactly when its per-board
                # verdicts matter: the deploy's gate decides between "the
                # farm" and "one board" from them, and without them it can
                # only fail the release for lack of evidence. The version
                # goes with them for the same reason -- which firmware
                # answered is most worth knowing about a run that failed.
                result={
                    key: value
                    for key, value in (("version", version), ("health", health))
                    if value
                } or None,
            ) from test_failure
        return {
            "profile": profile,
            "project": spec.label,
            "revision": revision,
            "version": version,
            "manifest": str(artifact_dir / "manifest.json"),
            "results": str(results),
            "report": str(report),
            "report_json": str(report_json),
            # The allocation, not the rig: a one-board run must not report
            # that it flashed and validated the whole bank. This number is
            # persisted evidence and is read back by the metrics reports.
            "boards": len(scoped_boards),
            # Which boards, so a failure can be read against each board's
            # canary history rather than re-run blindly.
            "board_ids": [board.get("id") for board in scoped_boards],
            "simulation": simulation,
            # The profile's needs this run's boards did not meet. A pass with
            # this set covered less than the profile asks for, and every
            # reader of a result -- the dashboard, the consumer's CI, the
            # notifier -- can name the families instead of guessing.
            "not_covered": not_covered or None,
            "partial": partial,
            "selection": selection,
            "reused_artifacts_from": reused_from or supplied,
            "flashed": not already_flashed,
            # A canary run's verdicts, per board, and which of its failures
            # were the farm's rather than a board's.
            "health": health,
            "summary": (
                (
                    f"Partial run on {_plural(len(scoped_boards), 'board')}: {described}"
                    if partial
                    else f"Validated {_plural(len(scoped_boards), 'board')}"
                )
                # Never a bare "Validated 5 boards" for a run asked to cover
                # six families: the one line most readers see is the line
                # that has to carry the gap.
                + (f"; did not cover {shortfall}" if not_covered else "")
            ),
        }
