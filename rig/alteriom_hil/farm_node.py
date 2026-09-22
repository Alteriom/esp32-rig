"""A farm node: hardware that takes its runs from a portal.

docs/portal-plan.md. The node is the farm service in ``node`` mode -- it
discovers its boards, flashes and runs suites exactly as a standalone farm
does -- plus this agent, which is the only thing that talks to the portal, and
only outward:

- **hello**, then a **heartbeat** every few seconds: what the node has (its
  inventory, its health, the profiles it can run, how many runs at once), and
  which jobs it is still running or still reporting. The answer carries what
  the portal wants done here: cancel a job, rediscover, and the holds on this
  node's boards (reserved, quarantined), which the node applies locally.
- **lease**: a long poll for the next job this node was granted. The node says
  at once that it took it, fetches the bundle if it does not hold it --
  verifying every checksum, and that it was built for the HIL agent this node
  runs -- and runs the job with the grant the portal decided.
- **stages** and **log** as the job runs; at the end the **evidence** (the
  run's directory) and the **result**. The end of a job is written to an
  outbox on disk first and sent from there, so a portal that is unreachable,
  or a node that restarts, loses no verdict.
- **release**: every heartbeat answer names the commit of this repository the
  portal wants its nodes on. A node running another stops taking work, and
  once idle downloads that release (a git bundle), checks it against the
  digest the portal gave, and leaves a request for alteriom-hil-update.path,
  which installs it with the same update script a deploy always ran
  (rig/node-update.sh). The node restarts on the new commit and says so.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import queue
import re
import shutil
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from alteriom_hil import allocation
from alteriom_hil.artifacts import load_artifacts
from alteriom_hil.providers import ENV_BUDGET_FILE, ENV_URL_FILE, budget_file_for

USER_AGENT = "alteriom-farm-node"
LOG_CHUNK_BYTES = 64 * 1024
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
# What a node may leave out of its hello and heartbeat for a portal older than
# itself. A node installs a release minutes before the portal's image catches
# up, and a portal refuses a field it does not know: the node that says hello
# with a new field is refused, and the farm has no worker until the portal is
# updated (2026-09-14, the `commit` field). These are what the node can do
# without; everything else a portal has always taken.
OPTIONAL_FIELDS = frozenset({"commit", "update", "config"})
UNKNOWN_FIELDS = re.compile(r"unknown (?:hello|heartbeat) fields: \[([^\]]*)\]")
# What needs the host's sudo goes through the node's control unit
# (alteriom-hil-control.path, rig/node-control.sh); the rest the agent does.
CONTROL_COMMANDS = frozenset({"restart", "logs", "configure", "provider_set", "provider_remove",
                              "provider_test", "notify_set", "notify_tune",
                              "notify_remove", "notify_test"})
# How often a node repeats its host configuration to the portal: at hello,
# and then this often, so a changed setting shows without a restart.
CONFIG_REPORT_SECONDS = 600
# A request the update service has not taken in this long was never going to
# be: the node gives up draining and takes work again.
UPDATE_TIMEOUT_SECONDS = 45 * 60
# A release that failed to install is tried again after this long, or at once
# when the portal names another.
UPDATE_RETRY_SECONDS = 60 * 60


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(stamp: str | None) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(str(stamp))).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


class PortalError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        self.status = status
        super().__init__(message)


class PortalClient:
    """The node's calls to its portal, with the node's key."""

    def __init__(self, base_url: str, key: str, timeout: float = 30.0):
        parsed = urlparse(base_url)
        loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            # A node key sent over plain http to another host is a key given away.
            raise ValueError("the portal URL must be https (http only to loopback)")
        if not key:
            raise ValueError("a node key is required")
        self.base_url = base_url.rstrip("/")
        self._key = key
        self.timeout = timeout

    def _call(self, method: str, path: str, body: bytes | None, content_type: str | None,
              timeout: float | None) -> bytes:
        headers = {"Authorization": f"Bearer {self._key}", "User-Agent": USER_AGENT}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base_url + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error") or detail
            except (json.JSONDecodeError, AttributeError):
                pass
            raise PortalError(exc.code, f"the portal answered {exc.code}: {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise PortalError(None, f"cannot reach the portal at {self.base_url}: {getattr(exc, 'reason', exc)}") from None

    def post_json(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        body = self._call("POST", path, json.dumps(payload).encode("utf-8"), "application/json", timeout)
        return json.loads(body or b"{}")

    def post_bytes(self, path: str, body: bytes, content_type: str, timeout: float | None = None) -> dict:
        return json.loads(self._call("POST", path, body, content_type, timeout) or b"{}")

    def get_bytes(self, path: str, timeout: float | None = None) -> bytes:
        return self._call("GET", path, None, None, timeout)


class NodeAgent:
    def __init__(self, manager, client: PortalClient, name: str, kind: str = "hardware",
                 heartbeat_seconds: float = 15.0, lease_wait: float = 25.0, retry_seconds: float = 5.0,
                 releases: Path | None = None):
        self.manager = manager
        self.client = client
        self.name = name
        self.kind = kind
        self.heartbeat_seconds = heartbeat_seconds
        self.lease_wait = lease_wait
        self.retry_seconds = retry_seconds
        # The service, for its helpers. It is `alteriom_hil.service` now --
        # the base and its own module -- rather than whichever module the
        # manager's class happened to be composed in (the launcher, or a
        # test's copy of it): one module, found by name.
        from alteriom_hil import service

        self.service = service
        self.outbox = Path(manager.state) / "outbox"
        self.outbox.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Jobs taken from the portal and not yet fully reported; of those,
        # the ones still running here.
        self._leased: set[str] = {entry.stem for entry in self.outbox.glob("*.json")}
        self._running: set[str] = set()
        self._stages: dict[str, list] = {}
        self._offsets: dict[str, int] = {}
        self.registered = threading.Event()
        self._stop = threading.Event()
        self._wake = threading.Event()
        manager._on_stage = self._stage_changed
        manager._on_finished = self._finished
        self.errors: list[str] = []
        # Where releases are staged for alteriom-hil-update.path; None for a
        # node that is not its host's to update (attached beside a standalone
        # service, which its own deploy updates).
        self.releases = Path(releases) if releases else None
        commit = (self.service.service_version() or {}).get("commit")
        self.commit = commit if isinstance(commit, str) and COMMIT_PATTERN.fullmatch(commit) else None
        self._update: dict | None = self._update_status()
        self._staging = False
        self._config_sent = 0.0
        # The configuration report is periodic, but some of what it carries
        # changes when something happens here: a test message or a run spends
        # the CallMeBot budget, a command stores a link. Those send the report
        # on the next heartbeat instead of up to CONFIG_REPORT_SECONDS later --
        # the portal's "Used today" sat on a stale count after a test message.
        self._config_due = False
        self._budget_seen = self._budget_stamp()
        # Fields this portal refused as unknown: left out until the node restarts.
        self._unsupported: set[str] = set()
        # Commands from the portal, carried out one at a time off the
        # heartbeat thread; ids already taken, so one is never done twice.
        self._commands: queue.Queue = queue.Queue()
        self._command_ids: set[str] = set()
        self._last_release: dict | None = None

    # ---- life ---------------------------------------------------------------------

    def start(self) -> "NodeAgent":
        for target, label in ((self._heartbeat_loop, "heartbeat"), (self._lease_loop, "lease"),
                              (self._report_loop, "report"), (self._command_loop, "commands")):
            threading.Thread(target=target, name=f"farm-node-{label}", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _note(self, message: str) -> None:
        self.errors = (self.errors + [message])[-20:]
        print(f"farm-node: {message}", file=sys.stderr, flush=True)

    # ---- what the node reports ------------------------------------------------------

    def _inventory(self) -> dict:
        snapshot = self.manager.inventory_snapshot(annotate=True)
        boards = [
            {key: value for key, value in board.items() if key not in ("state", "held_by", "hold", "health")}
            for board in snapshot.get("boards") or []
        ]
        return {
            "boards": boards,
            "missing": list(snapshot.get("missing") or []),
            "unregistered": list(snapshot.get("unregistered") or []),
            "instruments": list(snapshot.get("instruments") or []),
            "missing_instruments": list(snapshot.get("missing_instruments") or []),
            "probe_errors": list(snapshot.get("probe_errors") or []),
            "updated_at": snapshot.get("updated_at"),
        }

    def _health(self) -> dict | None:
        # A node attached beside a standalone service keeps its own state
        # directory inside the host's; the host's health check writes beside
        # the host's.
        state = Path(self.manager.state)
        for path in (state / "status.json", state.parent / "status.json"):
            try:
                status = json.loads(path.read_text(encoding="utf-8"))
                break
            except (OSError, json.JSONDecodeError):
                continue
        else:
            return None
        return {"status": status.get("status"), "checks": (status.get("checks") or [])[:40],
                # When the host last checked: a verdict hours old reads differently.
                "timestamp": status.get("timestamp")}

    def _supported(self, payload: dict) -> dict:
        return {key: value for key, value in payload.items() if key not in self._unsupported}

    def _drop_unknown(self, message: str) -> bool:
        """A portal older than this node refused fields it does not know:
        leave out those the node can do without, and say so once."""
        found = UNKNOWN_FIELDS.search(message)
        if not found:
            return False
        refused = set(re.findall(r"'([a-z_]+)'", found.group(1)))
        dropped = (refused & OPTIONAL_FIELDS) - self._unsupported
        if not dropped or refused - OPTIONAL_FIELDS:
            return False
        self._unsupported |= dropped
        self._note(f"the portal is older than this node: leaving out {', '.join(sorted(dropped))} until it is updated")
        return True

    def _config(self) -> dict | None:
        """This host's configuration as its own /api/v1/config says it --
        paths of secrets, never secrets -- without the profiles, which are the
        portal's to describe."""
        try:
            config = dict(self.manager.configuration())
        except Exception as exc:  # the node still works; the portal shows nothing
            self._note(f"could not read the host configuration: {exc!r}")
            return None
        config.pop("build", None)
        config.pop("portal", None)
        return config

    def _reported(self) -> list[str]:
        with self._lock:
            return sorted(self._leased)

    # ---- heartbeat ---------------------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.registered.is_set():
                    hello = {
                        "kind": self.kind,
                        "version": self._version(),
                        "commit": self.commit,
                        "max_runs": self.manager.max_runs,
                        "profiles": sorted(self.manager.profiles),
                        "inventory": self._inventory(),
                        "health": self._health(),
                    }
                    if self._update is not None:
                        hello["update"] = self._update
                    config = self._config()
                    if config is not None:
                        hello["config"] = config
                    answer = self.client.post_json(f"/api/v1/workers/{self.name}/hello", self._supported(hello))
                    self.registered.set()
                    self._config_sent = time.monotonic()
                else:
                    beat = {
                        "inventory": self._inventory(),
                        "health": self._health(),
                        "running": self._reported(),
                    }
                    if self._update is not None:
                        beat["update"] = self._update
                    budget = self._budget_stamp()
                    if budget != self._budget_seen:
                        self._config_due = True
                    if self._config_due or time.monotonic() - self._config_sent > CONFIG_REPORT_SECONDS:
                        config = self._config()
                        if config is not None:
                            beat["config"] = config
                        self._config_sent = time.monotonic()
                        self._config_due = False
                        self._budget_seen = budget
                    answer = self.client.post_json(f"/api/v1/workers/{self.name}/heartbeat", self._supported(beat))
                self._apply(answer)
            except PortalError as exc:
                if exc.status == 404:
                    # The portal does not know this worker (yet, or any more).
                    self.registered.clear()
                self._note(str(exc))
                if exc.status == 400 and self._drop_unknown(str(exc)):
                    continue  # at once, without what it refused
            except Exception as exc:  # a heartbeat that failed is retried, never fatal
                self._note(f"heartbeat failed: {exc!r}")
            self._stop.wait(self.heartbeat_seconds)

    def _apply(self, answer: dict) -> None:
        for item in answer.get("cancel") or []:
            job = self.manager.store.get(item.get("job_id") or "")
            if job and job["status"] == "running":
                try:
                    self.manager.cancel(job["id"], item.get("summary") or "Cancelled on the portal", item.get("detail"))
                except (KeyError, ValueError):
                    pass
        if answer.get("rediscover") and not self.manager.running_job_ids():
            try:
                self.manager.submit("inventory", {})
            except (self.service.RigBusyError, ValueError):
                pass
        if answer.get("holds") is not None:
            self._sync_holds(answer["holds"])
        for conflict in answer.get("conflicts") or []:
            self._note(f"the portal refused a board: {conflict}")
        for command in answer.get("commands") or []:
            if isinstance(command, dict) and isinstance(command.get("id"), str) and command["id"] not in self._command_ids:
                self._command_ids.add(command["id"])
                self._commands.put(command)
        if isinstance(answer.get("release"), dict):
            self._last_release = answer["release"]
        self._consider_release(answer.get("release"))

    def _version(self) -> str | None:
        """The release this node runs, as a person reads it -- 1.0.277 -- not
        a commit; the commit is reported beside it."""
        version = self.service.service_version() or {}
        number = version.get("version")
        return number if number and number != "unknown" else version.get("short")

    # ---- commands from the portal ---------------------------------------------------------

    def _command_loop(self) -> None:
        self._report_control_results()
        while not self._stop.is_set():
            try:
                command = self._commands.get(timeout=1.0)
            except queue.Empty:
                self._report_control_results()
                continue
            # Said before the work, not after: a command that takes a
            # minute should look like one, not like a portal that lost it.
            self._report_started(command["id"])
            try:
                status, result, detail = self._carry_out(command)
            except Exception as exc:  # reported, never fatal
                status, result, detail = "failed", None, f"{type(exc).__name__}: {exc}"
            if status is not None:
                self._report_command(command["id"], status, result, detail)

    def _carry_out(self, command: dict) -> tuple[str | None, dict | None, str | None]:
        kind, args = command.get("kind"), command.get("args") or {}
        manager = self.manager
        if kind == "rediscover":
            job = manager.submit("inventory", {})
            return "done", {"job": job["id"]}, "rediscovery queued"
        if kind == "read_details":
            return "done", manager.device_details(args["board"]), None
        if kind == "register":
            return "done", manager.register_device(args["id"], args["mac"]), f"registered {args['id']}"
        if kind == "unregister":
            return "done", manager.unregister_device(args["id"]), f"unregistered {args['id']}"
        if kind == "update_now":
            if self.releases is None:
                return "failed", None, "this node is updated with its host, not by its portal"
            failed = self._update_status()
            if failed and failed.get("state") == "failed":
                (self.releases / "status.json").unlink(missing_ok=True)
            self._update = None
            self._consider_release(self._last_release)
            return "done", {"update": self._update}, "looking for the current release now"
        if kind in CONTROL_COMMANDS:
            if self.releases is None:
                return "failed", None, "an attached node is managed through its host, not its portal"
            self.releases.mkdir(parents=True, exist_ok=True)
            staging = self.releases / "control.json.tmp"
            staging.unlink(missing_ok=True)
            # Owner-only: a provider_set carries a sealed link. Only the rig's
            # key opens it, but nobody else on the host needs to read it, and
            # node-control.sh deletes the request once it has taken it.
            descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"id": command["id"], "action": kind, "args": args,
                                         "requested_at": _utcnow()}))
            staging.replace(self.releases / "control.json")
            # alteriom-hil-control writes the result; a restart reports it
            # from the node that comes back.
            return None, None, None
        return "failed", None, f"this node does not know the command {kind!r}"

    def _report_control_results(self) -> None:
        if self.releases is None or not self.releases.is_dir():
            return
        for path in sorted(self.releases.glob("control-result-*.json")):
            try:
                outcome = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if self._report_command(str(outcome.get("id") or ""), outcome.get("status") or "failed",
                                    outcome.get("result"), outcome.get("detail")):
                path.unlink(missing_ok=True)
                # A control command may have changed what the report says.
                self.report_config_soon()

    def report_config_soon(self) -> None:
        """Send the configuration report on the next heartbeat."""
        self._config_due = True

    @staticmethod
    def _budget_stamp() -> tuple | None:
        """When and how the rig's CallMeBot budget file last changed, or None
        on a rig with no provider or no budget spent yet."""
        if not (os.environ.get(ENV_URL_FILE) or os.environ.get(ENV_BUDGET_FILE)):
            return None
        try:
            stat = budget_file_for(os.environ).stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _report_started(self, command_id: str) -> None:
        """Tell the portal the rig has this command in hand.

        Between the portal handing a command over and a result there can be a
        service restart or a board being read, and a console that says nothing
        in between cannot be told from one that lost the command. Best effort:
        a portal that refuses this still gets the result.
        """
        try:
            self.client.post_json(f"/api/v1/workers/{self.name}/commands/{command_id}/start", {})
        except PortalError:
            pass

    def _report_command(self, command_id: str, status: str, result: object, detail: object) -> bool:
        payload = {"status": status if status in ("done", "failed") else "failed"}
        if isinstance(result, dict):
            payload["result"] = result
        if detail:
            payload["detail"] = str(detail)[-8000:]
        try:
            self.client.post_json(f"/api/v1/workers/{self.name}/commands/{command_id}", payload)
        except PortalError as exc:
            if exc.status is None:
                return False  # the portal is not there; the result is kept
            self._note(f"the portal refused the result of command {command_id[:8]}: {exc}")
        return True

    # ---- releases -------------------------------------------------------------------------

    def updating(self) -> bool:
        """Finishing its runs for a release, or about to restart into one."""
        return self._update is not None and self._update.get("state") in ("pending", "staged", "installing")

    def _update_status(self) -> dict | None:
        """What alteriom-hil-update wrote last, as the portal is told it."""
        if self.releases is None:
            return None
        try:
            status = json.loads((self.releases / "status.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(status, dict) or status.get("state") not in ("staged", "installing", "installed", "failed"):
            return None
        commit = status.get("commit")
        if not (isinstance(commit, str) and COMMIT_PATTERN.fullmatch(commit)):
            return None
        return {"state": status["state"], "commit": commit,
                "detail": str(status.get("detail") or "")[-4000:] or None, "at": str(status.get("at") or "")[:40] or None}

    def _set_update(self, state: str, commit: str, detail: str | None) -> None:
        self._update = {"state": state, "commit": commit, "detail": detail, "at": _utcnow()}
        if self.releases is not None and state in ("staged", "failed"):
            self.releases.mkdir(parents=True, exist_ok=True)
            staging = self.releases / "status.json.tmp"
            staging.write_text(json.dumps(self._update), encoding="utf-8")
            staging.replace(self.releases / "status.json")

    def _consider_release(self, release: object) -> None:
        if self.releases is None or not isinstance(release, dict):
            return
        commit = release.get("commit")
        if not (isinstance(commit, str) and COMMIT_PATTERN.fullmatch(commit)):
            return
        written = self._update_status()
        if written is not None and (self._update is None or (written.get("at") or "") >= (self._update.get("at") or "")):
            self._update = written
        update = self._update or {}
        if commit == self.commit:
            if update.get("commit") != commit:
                # On the release already (installed by hand, or before this
                # portal said so): nothing to report but the commit.
                self._update = None
            return
        if update.get("commit") == commit:
            state = update.get("state")
            if state == "installing":
                return
            if state == "staged":
                if _age_seconds(update.get("at")) > UPDATE_TIMEOUT_SECONDS:
                    (self.releases / "request.json").unlink(missing_ok=True)
                    self._set_update("failed", commit, (
                        "alteriom-hil-update did not take the request; is alteriom-hil-update.path "
                        "enabled (farm.mode: node)?"
                    ))
                return
            if state in ("failed", "installed") and _age_seconds(update.get("at")) < UPDATE_RETRY_SECONDS:
                # Installed and yet not running it is a failure too: retrying
                # at once would loop.
                return
        with self._lock:
            busy = bool(self._running)
        if busy or any(self.outbox.glob("*.json")):
            if update.get("commit") != commit or update.get("state") != "pending":
                self._update = {"state": "pending", "commit": commit,
                                "detail": "finishing the runs in progress", "at": _utcnow()}
            return
        if self._staging:
            return
        self._staging = True
        self._update = {"state": "pending", "commit": commit, "detail": "downloading the release", "at": _utcnow()}
        threading.Thread(target=self._stage_release, args=(dict(release),),
                         name="farm-node-release", daemon=True).start()

    def _stage_release(self, release: dict) -> None:
        commit = release["commit"]
        try:
            body = self.client.get_bytes(f"/api/v1/releases/{commit}/bundle", timeout=600)
            if len(body) != release.get("bytes") or hashlib.sha256(body).hexdigest() != release.get("sha256"):
                raise ValueError("the release is not the one the portal announced (size or digest differ)")
            self.releases.mkdir(parents=True, exist_ok=True)
            bundle = self.releases / f"{commit}.bundle"
            staging = self.releases / f".{commit}.bundle.incoming"
            staging.write_bytes(body)
            staging.replace(bundle)
            request = self.releases / "request.json.tmp"
            request.write_text(json.dumps({
                "commit": commit, "sha256": release["sha256"], "bundle": str(bundle), "requested_at": _utcnow(),
            }), encoding="utf-8")
            self._set_update("staged", commit, "waiting for alteriom-hil-update to install it")
            # Last: the path unit starts the install the moment this exists.
            request.replace(self.releases / "request.json")
        except Exception as exc:  # said to the portal, retried later
            self._set_update("failed", commit, f"could not stage the release: {exc}")
            self._note(f"could not stage release {commit[:12]}: {exc}")
        finally:
            self._staging = False

    def _sync_holds(self, holds: dict) -> None:
        """The portal decides holds; the node's allocation must see them."""
        store = self.manager.store
        local = store.holds()
        for board_id in set(local) - set(holds):
            store.release_hold(board_id)
        for board_id, hold in holds.items():
            current = local.get(board_id)
            if current is None or current.get("state") != hold.get("state") or current.get("reason") != hold.get("reason"):
                store.set_hold(board_id, hold.get("state"), hold.get("reason"), hold.get("by"), hold.get("job_id"))

    # ---- lease --------------------------------------------------------------------------------

    def _lease_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                busy = len(self._running)
            if not self.registered.is_set() or busy >= self.manager.max_runs or self.updating():
                self._stop.wait(0.5)
                continue
            try:
                answer = self.client.post_json(
                    f"/api/v1/workers/{self.name}/lease?wait={self.lease_wait:g}", {},
                    timeout=self.lease_wait + 15,
                )
            except PortalError as exc:
                if exc.status == 404:
                    self.registered.clear()
                self._note(str(exc))
                self._stop.wait(self.retry_seconds)
                continue
            if answer.get("job"):
                try:
                    self._take(answer)
                except Exception as exc:  # the job's failure is reported; the loop goes on
                    self._note(f"could not start job {answer['job'].get('id')}: {exc!r}")

    def _take(self, answer: dict) -> None:
        job = answer["job"]
        job_id = job["id"]
        with self._lock:
            self._leased.add(job_id)
            self._running.add(job_id)
        try:
            # Taken, said before a bundle download can outlast the lease.
            self.client.post_json(f"/api/v1/jobs/{job_id}/stages", {"progress": job.get("progress") or []})
        except PortalError as exc:
            self._note(str(exc))
        try:
            if answer.get("bundle"):
                self._ensure_bundle(answer["bundle"])
        except Exception as exc:
            self._fail_before_start(job, "The worker could not take the firmware bundle", str(exc))
            return
        manager = self.manager
        grant = allocation.Grant.from_dict(answer["grant"])
        with manager._dispatch_lock:
            # Under the dispatch lock, so the node's own dispatcher never sees
            # the job queued between being recorded and being started.
            if manager.store.get(job_id) is None:
                manager.store.create(
                    job["kind"], job["request"], Path(manager.state) / "logs" / f"{job_id}.log",
                    job.get("progress") or [], job_id=job_id,
                )
            manager.store.claim(job_id)
            with manager._reservation_lock:
                manager._grants[job_id] = allocation.with_since(grant, self.service.utcnow())
        threading.Thread(
            target=manager._run_job, args=(manager.store.get(job_id), grant),
            name=f"farm-job-{job_id[:8]}", daemon=True,
        ).start()

    def _ensure_bundle(self, bundle: dict) -> None:
        manager = self.manager
        root = manager.artifact_root
        target = root / bundle["id"]
        if (target / "manifest.json").is_file():
            return
        body = self.client.get_bytes(f"/api/v1/artifacts/{bundle['id']}/bundle", timeout=600)
        root.mkdir(parents=True, exist_ok=True)
        staging = root / f".incoming-{bundle['id']}"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            manager._extract_bundle(body, staging)
            # Every checksum, every component at its offset.
            manifest = load_artifacts(staging)
            agent = manifest.get("hil_agent_sha")
            # By the profile the bundle was supplied for; one that does not
            # say is the default profile's, as every bundle once was.
            own = manager.agent_source_sha((bundle.get("provenance") or {}).get("profile"))
            if agent and own and agent != own:
                raise ValueError(
                    "the bundle was built for a different HIL agent than this node runs; "
                    "deploy the farm commit the portal runs"
                )
            provenance = dict(bundle.get("provenance") or {})
            provenance["fetched_from"] = self.client.base_url
            (staging / "provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True), encoding="utf-8")
            staging.rename(target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _fail_before_start(self, job: dict, summary: str, detail: str) -> None:
        progress = job.get("progress") or []
        for stage in progress:
            if stage.get("status") in ("pending", "running"):
                stage["status"] = "skipped"
        self._write_outbox(job["id"], {
            "status": "failed", "progress": progress,
            "result": {"summary": summary, "detail": detail, "failed_stage": "build"},
        }, None)
        with self._lock:
            self._running.discard(job["id"])
        self._wake.set()

    # ---- reporting ------------------------------------------------------------------------------

    def _stage_changed(self, job_id: str, progress: list) -> None:
        with self._lock:
            if job_id not in self._leased:
                return
            self._stages[job_id] = progress
        self._wake.set()

    def _finished(self, job_id: str) -> None:
        with self._lock:
            if job_id not in self._leased:
                return  # a discovery of the node's own
        job = self.manager.store.get(job_id) or {}
        runs = Path(self.manager.state) / "runs" / job_id
        evidence = None
        if runs.is_dir():
            evidence = self.outbox / f"{job_id}.tar.gz"
            with tarfile.open(evidence, "w:gz") as archive:
                archive.add(str(runs), arcname="evidence", recursive=False)
                for path in sorted(runs.rglob("*")):
                    if path.is_symlink() or not (path.is_file() or path.is_dir()):
                        continue
                    archive.add(str(path), arcname="evidence/" + path.relative_to(runs).as_posix(), recursive=False)
        self._write_outbox(job_id, {
            "status": job.get("status") or "failed",
            "progress": job.get("progress") or [],
            "result": job.get("result") or {},
        }, evidence)
        with self._lock:
            self._running.discard(job_id)
            self._stages.pop(job_id, None)
        # A run can spend a real CallMeBot message.
        self.report_config_soon()
        self._wake.set()

    def _write_outbox(self, job_id: str, report: dict, evidence: Path | None) -> None:
        report = {**report, "evidence": bool(evidence), "evidence_sent": False}
        temporary = self.outbox / f"{job_id}.json.tmp"
        temporary.write_text(json.dumps(report), encoding="utf-8")
        temporary.replace(self.outbox / f"{job_id}.json")

    def _report_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            try:
                self._report_once()
            except Exception as exc:  # retried on the next turn
                self._note(f"reporting failed: {exc!r}")

    def _report_once(self) -> None:
        with self._lock:
            stages, self._stages = self._stages, {}
            leased = sorted(self._leased)
        for job_id, progress in stages.items():
            try:
                self.client.post_json(f"/api/v1/jobs/{job_id}/stages", {"progress": progress})
            except PortalError as exc:
                self._note(str(exc))
                if exc.status is None:
                    with self._lock:
                        self._stages.setdefault(job_id, progress)
        for job_id in leased:
            try:
                self._ship_log(job_id)
            except PortalError as exc:
                self._note(str(exc))
        for entry in sorted(self.outbox.glob("*.json")):
            self._deliver(entry)

    def _ship_log(self, job_id: str, until_done: bool = False) -> None:
        path = Path(self.manager.state) / "logs" / f"{job_id}.log"
        if not path.is_file():
            return
        while True:
            offset = self._offsets.get(job_id, 0)
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read(LOG_CHUNK_BYTES)
            if not chunk:
                return
            answer = self.client.post_bytes(
                f"/api/v1/jobs/{job_id}/log?offset={offset}", chunk, "text/plain; charset=utf-8"
            )
            self._offsets[job_id] = int(answer.get("size", offset))
            if not until_done and len(chunk) < LOG_CHUNK_BYTES:
                return

    def _deliver(self, entry: Path) -> None:
        job_id = entry.stem
        report = json.loads(entry.read_text(encoding="utf-8"))
        evidence = self.outbox / f"{job_id}.tar.gz"
        try:
            self._ship_log(job_id, until_done=True)
            if report.get("evidence") and not report.get("evidence_sent") and evidence.is_file():
                self.client.post_bytes(f"/api/v1/jobs/{job_id}/evidence", evidence.read_bytes(),
                                       "application/gzip", timeout=600)
                report["evidence_sent"] = True
                entry.write_text(json.dumps(report), encoding="utf-8")
            self.client.post_json(f"/api/v1/jobs/{job_id}/result", {
                "status": report["status"], "result": report.get("result") or {},
                "progress": report.get("progress") or [],
            })
        except PortalError as exc:
            if exc.status in (403, 404, 409):
                # The portal no longer has this job as running here: it ended
                # it (lost, cancelled) or never gave it. Keeping the report
                # would retry forever.
                self._note(f"the portal refused the report of {job_id[:8]}: {exc}")
            else:
                self._note(str(exc))
                return
        entry.unlink(missing_ok=True)
        evidence.unlink(missing_ok=True)
        with self._lock:
            self._leased.discard(job_id)
            self._offsets.pop(job_id, None)


# ---- a farm's history, brought to its portal ------------------------------------------------
# A farm that ran standalone before it joined a portal keeps what it ran: every
# finished job, its evidence, its log, and the bundles it flashed, sent with
# the node's key to the portal, which files them under this worker. Whatever
# the portal already holds is skipped, so the export can be run again after an
# interruption, or after the node has taken runs of its own.
#
#   $VENV/bin/alteriom-hil-agent export-history [--dry-run] [--no-artifacts]

HISTORY_STATUSES = ("passed", "failed", "cancelled")
MAX_HISTORY_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_HISTORY_LOG_BYTES = 64 * 1024 * 1024
BUNDLE_ID = re.compile(r"[0-9a-f]{32}\Z")


def pack_directory(directory: Path, top: str) -> bytes:
    """One directory as a .tar.gz under `top`: regular files only, links left out."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(str(directory), arcname=top, recursive=False)
        for current, folders, files in os.walk(directory, followlinks=False):
            base = Path(current)
            folders[:] = sorted(folder for folder in folders if not (base / folder).is_symlink())
            for folder in folders:
                archive.add(str(base / folder), arcname=f"{top}/{(base / folder).relative_to(directory).as_posix()}", recursive=False)
            for file in sorted(files):
                path = base / file
                if path.is_symlink() or not path.is_file():
                    continue
                archive.add(str(path), arcname=f"{top}/{path.relative_to(directory).as_posix()}", recursive=False)
    return buffer.getvalue()


def export_history(state: Path, client: PortalClient, name: str, artifacts: bool = True,
                   dry_run: bool = False, out=print) -> int:
    import sqlite3

    state = Path(state)
    database = state / "farm.sqlite3"
    if not database.is_file():
        out(f"no job history at {database}")
        return 1
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    marks = ",".join("?" * len(HISTORY_STATUSES))
    jobs = [dict(row) for row in db.execute(
        f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY created_at", HISTORY_STATUSES)]
    verdicts: dict[str, list[dict]] = {}
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "board_verdicts" in tables:
        for row in db.execute("SELECT * FROM board_verdicts"):
            try:
                failed = json.loads(row["failed_json"] or "[]")
            except json.JSONDecodeError:
                failed = []
            verdicts.setdefault(row["job_id"], []).append({
                "board_id": row["board_id"], "checked_at": row["checked_at"],
                "verdict": row["verdict"], "outcome": row["outcome"], "failed": failed,
            })
    db.close()

    store = state / "artifacts"
    entries = sorted(entry for entry in store.iterdir() if BUNDLE_ID.fullmatch(entry.name)) if store.is_dir() else []
    directories = [entry for entry in entries if not entry.is_symlink() and entry.is_dir()]
    links: dict[str, str] = {}
    for entry in entries:
        if entry.is_symlink():
            target = Path(os.path.realpath(entry))
            if target.parent == Path(os.path.realpath(store)) and BUNDLE_ID.fullmatch(target.name) and target.is_dir():
                links[entry.name] = target.name

    known = client.post_json(f"/api/v1/workers/{name}/history/known", {
        "jobs": [job["id"] for job in jobs], "artifacts": [entry.name for entry in directories],
    })
    known_jobs, known_artifacts = set(known.get("jobs") or []), set(known.get("artifacts") or [])
    new_jobs = [job for job in jobs if job["id"] not in known_jobs]
    new_directories = [entry for entry in directories if entry.name not in known_artifacts] if artifacts else []
    out(f"{len(jobs)} finished jobs, {len(known_jobs)} already on the portal, {len(new_jobs)} to bring; "
        f"{len(directories)} bundles and build directories, "
        f"{len(new_directories) if artifacts else 'none'} to bring")
    if dry_run:
        return 0

    failures = 0
    brought = set(known_artifacts)
    for number, entry in enumerate(new_directories, 1):
        try:
            body = pack_directory(entry, "bundle")
            if len(body) > MAX_HISTORY_UPLOAD_BYTES:
                raise ValueError(f"{len(body)} bytes packed is more than a bundle may be")
            client.post_bytes(f"/api/v1/workers/{name}/history/artifacts/{entry.name}", body,
                              "application/gzip", timeout=900)
            brought.add(entry.name)
            out(f"bundle {number}/{len(new_directories)} {entry.name[:8]} ({len(body)} bytes)")
        except (PortalError, OSError, ValueError, tarfile.TarError) as exc:
            failures += 1
            out(f"bundle {entry.name[:8]} not brought: {exc}")

    for number, row in enumerate(new_jobs, 1):
        job_id = row["id"]
        try:
            runs = state / "runs" / job_id
            if runs.is_dir() and not runs.is_symlink():
                client.post_bytes(f"/api/v1/workers/{name}/history/jobs/{job_id}/evidence",
                                  pack_directory(runs, "evidence"), "application/gzip", timeout=900)
            log = state / "logs" / f"{job_id}.log"
            if log.is_file():
                with log.open("rb") as handle:
                    size = log.stat().st_size
                    handle.seek(max(0, size - MAX_HISTORY_LOG_BYTES))
                    client.post_bytes(f"/api/v1/workers/{name}/history/jobs/{job_id}/log", handle.read(),
                                      "text/plain; charset=utf-8", timeout=300)
            if job_id in links and links[job_id] in brought:
                client.post_json(f"/api/v1/workers/{name}/history/jobs/{job_id}/link", {"bundle": links[job_id]})
            client.post_json(f"/api/v1/workers/{name}/history/jobs/{job_id}", {
                "job": {
                    "id": job_id, "kind": row["kind"], "status": row["status"],
                    "created_at": row["created_at"], "started_at": row.get("started_at"),
                    "finished_at": row.get("finished_at"),
                    "request": json.loads(row["request_json"] or "{}"),
                    "result": json.loads(row["result_json"]) if row.get("result_json") else None,
                    "progress": json.loads(row.get("progress_json") or "[]"),
                },
                "verdicts": verdicts.get(job_id, []),
            })
            if number % 25 == 0 or number == len(new_jobs):
                out(f"job {number}/{len(new_jobs)}")
        except (PortalError, OSError, ValueError, json.JSONDecodeError, tarfile.TarError) as exc:
            failures += 1
            out(f"job {job_id[:8]} not brought: {exc}")
    out(f"done: {failures} not brought; run it again to retry them" if failures else "done: everything brought")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alteriom-hil-agent", description="A farm node's own commands.")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser(
        "export-history",
        help="bring this farm's finished jobs, their evidence and logs, and its bundles to its portal",
    )
    export.add_argument("--state", type=Path, default=Path(os.environ.get("ALTERIOM_HIL_STATE") or "/var/lib/alteriom-hil"))
    export.add_argument("--portal-url", default=os.environ.get("ALTERIOM_HIL_PORTAL_URL"))
    export.add_argument("--worker-name", default=os.environ.get("ALTERIOM_HIL_WORKER_NAME"))
    export.add_argument("--node-key-file", type=Path,
                        default=Path(os.environ["ALTERIOM_HIL_NODE_KEY_FILE"]) if os.environ.get("ALTERIOM_HIL_NODE_KEY_FILE") else None)
    export.add_argument("--no-artifacts", action="store_true", help="the jobs, evidence and logs only")
    export.add_argument("--dry-run", action="store_true", help="say what would be brought")
    args = parser.parse_args(argv)
    if not (args.portal_url and args.worker_name and args.node_key_file):
        raise SystemExit("needs --portal-url, --worker-name and --node-key-file (or ALTERIOM_HIL_PORTAL_URL, "
                         "ALTERIOM_HIL_WORKER_NAME and ALTERIOM_HIL_NODE_KEY_FILE from the runtime file)")
    client = PortalClient(args.portal_url, args.node_key_file.read_text(encoding="utf-8").strip(), timeout=120)
    return export_history(args.state, client, args.worker_name, artifacts=not args.no_artifacts, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
