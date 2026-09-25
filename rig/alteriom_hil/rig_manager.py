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
import hashlib
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
from alteriom_hil import github_access, updates
from alteriom_hil.profiles import NAME_PATTERN, parse_profile
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


# The manifest key that holds the commit a bundle was built from, unless the
# project names another: what the documented build scripts write.
DEFAULT_REVISION_KEY = "git_sha"

class RigMixin:
    # The rig's own routes, declared beside the methods that answer them
    # (the portal's are PortalMixin.WORKSPACE_ROUTES). Projects: what this
    # rig runs, which its operator adds to from Settings -> Projects. The
    # public farm: the portal's world page, read by the rig and shown on
    # its overview, so a rig on its own still sees where it could connect.
    PROJECT_ROUTES = (
        ("GET", r"/api/v1/projects", "user", "projects_view"),
        ("POST", r"/api/v1/projects", "admin", "create_project"),
        ("POST", r"/api/v1/projects/(?P<name>[a-z0-9][a-z0-9-]{0,63})", "admin", "update_project"),
        ("POST", r"/api/v1/projects/(?P<name>[a-z0-9][a-z0-9-]{0,63})/delete", "admin", "delete_project"),
        ("POST", r"/api/v1/projects/(?P<name>[a-z0-9][a-z0-9-]{0,63})/restore", "admin", "restore_project"),
        # A project's runs, gone together: the evidence, the logs, the records.
        ("POST", r"/api/v1/projects/(?P<name>[a-z0-9][a-z0-9-]{0,63})/runs/delete", "admin", "delete_project_runs"),
        # One finished run, gone.
        ("POST", r"/api/v1/jobs/(?P<job_id>[0-9a-f]{32})/delete", "admin", "delete_run"),
        # The token, given from the page: kept in the state directory, checked
        # with GitHub first, never read back.
        ("POST", r"/api/v1/github", "admin", "set_github_token"),
        ("POST", r"/api/v1/github/remove", "admin", "remove_github_token"),
        # Ask GitHub again now: who the token is, what it reaches.
        ("POST", r"/api/v1/github/check", "admin", "check_github"),
        # The newest bundle the project's supply workflow uploaded, fetched
        # by the rig from GitHub: how a rig no CI can reach gets its firmware.
        ("POST", r"/api/v1/projects/(?P<name>[a-z0-9][a-z0-9-]{0,63})/fetch", "admin", "fetch_project_bundle"),
        # Who this rig is to GitHub (never the token), and whether it can add
        # a project at all.
        ("GET", r"/api/v1/github", "user", "github_view"),
        # The repository looked up on GitHub, so the form starts filled in.
        ("POST", r"/api/v1/projects/inspect", "admin", "inspect_repository"),
        # What this rig calls itself: a name, a description, a location.
        ("GET", r"/api/v1/rig/details", "user", "rig_details_view"),
        ("POST", r"/api/v1/rig/details", "admin", "set_rig_details"),
        ("GET", r"/api/v1/farm/public", "user", "farm_public_view"),
        # Updates on the owner's terms: what is installed and what is newer,
        # a look now, an install now, and whether installs happen on their own.
        ("GET", r"/api/v1/update", "user", "update_view"),
        ("POST", r"/api/v1/update/check", "admin", "check_update"),
        ("POST", r"/api/v1/update/install", "admin", "install_update"),
        ("POST", r"/api/v1/update/auto", "admin", "set_update_auto"),
    )

    # Where a rig looks when nothing names a farm: the public farm the rig
    # software comes from. `farm.public_url` in the host configuration
    # points it elsewhere, or "off" shows none.
    PUBLIC_FARM_URL = "https://espfarm.alteriom.net"
    PUBLIC_FARM_TTL = 600        # a good answer is kept this long
    PUBLIC_FARM_RETRY = 120      # a failed read is not retried sooner
    PUBLIC_FARM_TIMEOUT = 6

    def api_routes(self) -> tuple:
        from alteriom_hil.service import ApiRoute

        mine = tuple(ApiRoute(method, re.compile(pattern), audience, answers)
                     for method, pattern, audience, answers in self.PROJECT_ROUTES)
        # Cooperative: a standalone farm is this half and the portal's on
        # one base, and the portal's routes come after these.
        inherited = super().api_routes() if hasattr(super(), "api_routes") else ()
        return mine + tuple(inherited)

    # ---- GitHub: the token this rig holds, and what it lets it do ------------------
    #
    # A project is a GitHub repository whose CI builds the firmware this rig
    # flashes. Without a token the rig could neither check the repository out
    # at a run nor take a bundle from the workflow that built it, so it adds
    # no project until it has one. The token is the consumer credential git
    # is already answered with (`_clone_credentials`); `alteriom-hil-admin
    # github set` writes it. The rig says who the token is, never what it is.

    def github_token_path(self) -> Path:
        """Where this rig's GitHub token is: the one the page stored under
        the state directory when there is one, else the host's consumer
        credential file, which `alteriom-hil-admin github set` writes."""
        own = Path(self.state) / "github-token"
        return own if own.is_file() else Path(self.CONSUMER_TOKEN_PATH)

    # ---- updates, on the owner's terms ----------------------------------------------------
    UPDATE_CHECK_DELAY = 180          # seconds after the service starts before its first look
    UPDATE_CHECK_EVERY = 6 * 3600     # and then this often

    def _update_dir(self) -> Path:
        return Path(self.state) / "update"

    def _update_source(self) -> str:
        """Where newer releases come from: the farm this rig is a node of,
        or GitHub's releases of the rig software."""
        return "portal" if self.__dict__.get("mode") == "node" else "github"

    def update_auto(self) -> bool:
        return updates.settings(Path(self.state))["auto"]

    def update_view(self, identity=None) -> dict:
        """What this rig runs, what is newer and where from, what was last
        done about it, and whether installs happen on their own."""
        from alteriom_hil.service import service_version
        installed = service_version()
        checked = self.__dict__.get("_update_checked") or {}
        agent = self.__dict__.get("_node_agent")
        if self._update_source() == "portal":
            offered = getattr(agent, "_last_release", None) or {}
            available = None
            if offered.get("commit") and offered.get("commit") != installed.get("commit"):
                available = {"version": offered.get("version"), "commit": offered.get("commit"), "from": "the farm"}
            status = getattr(agent, "_update", None) or updates.status(self._update_dir())
            checked_at, error = None, None
        else:
            available = checked.get("available")
            status = updates.status(self._update_dir())
            checked_at, error = checked.get("at"), checked.get("error")
        return {"installed": {"version": installed.get("version"), "commit": installed.get("commit")},
                "source": self._update_source(), "available": available, "checked_at": checked_at,
                "error": error, "auto": self.update_auto(), "status": status,
                "staging": bool(self.__dict__.get("_update_staging"))}

    def _latest_release(self) -> dict:
        """GitHub's newest release of the rig software; its own method so a
        test stands in for GitHub."""
        return updates.latest_release(token=self._github().token())

    def _fetch_release_file(self, url: str) -> bytes:
        return updates.download(url)

    def _check_update_now(self) -> dict:
        from alteriom_hil.service import service_version
        installed = service_version()
        try:
            latest = self._latest_release()
            available = latest if updates.newer(installed.get("version"), latest["version"]) else None
            checked = {"at": updates.utcnow(), "available": available, "latest": latest, "error": None}
        except updates.UpdateError as error:
            checked = {"at": updates.utcnow(), "available": None, "latest": None, "error": str(error)}
        self.__dict__["_update_checked"] = checked
        return checked

    def check_update(self, body=None, identity=None) -> dict:
        """Look now rather than at the watch's pace."""
        if self._update_source() == "portal":
            agent = self.__dict__.get("_node_agent")
            if agent is not None:
                agent.look_again()
        else:
            self._check_update_now()
        return {"update": self.update_view()}

    def install_update(self, body=None, identity=None) -> dict:
        """Install the newer release: staged by this rig, installed by the
        update unit, which restarts the service. Refused while runs are in
        progress -- the restart would end them."""
        if self._update_source() == "portal":
            agent = self.__dict__.get("_node_agent")
            if agent is None:
                raise ValueError("this rig takes releases from its farm, and its agent is not running")
            agent.install_offered()
            return {"update": self.update_view()}
        if self.__dict__.get("_update_staging"):
            raise ValueError("a release is being downloaded already")
        checked = self.__dict__.get("_update_checked") or self._check_update_now()
        available = checked.get("available")
        if not available:
            raise ValueError(checked.get("error") or "no newer release: this rig runs the newest one")
        if self.running_job_ids():
            raise ValueError("runs are in progress; install when the rig is idle (the install restarts the service)")
        self._stage_update(available)
        return {"update": self.update_view()}

    def _stage_update(self, available: dict) -> None:
        self.__dict__["_update_staging"] = True
        updates.write_status(self._update_dir(), "downloading", available.get("version"), None,
                             f"downloading {available.get('version')}")

        def run():
            try:
                updates.stage(self._update_dir(), available, fetch=self._fetch_release_file)
            except Exception as error:  # noqa: BLE001 -- said in status.json, where the page reads it
                updates.write_status(self._update_dir(), "failed", available.get("version"), None,
                                     f"could not stage the release: {error}")
            finally:
                self.__dict__["_update_staging"] = False

        threading.Thread(target=run, name="rig-update-stage", daemon=True).start()

    def set_update_auto(self, body, identity=None) -> dict:
        auto = (body or {}).get("auto")
        if not isinstance(auto, bool):
            raise ValueError("auto is true or false")
        updates.set_auto(Path(self.state), auto)
        return {"update": self.update_view()}

    def _update_watch(self) -> None:
        """Looks for a newer release now and then. Installs it on its own
        only when the owner turned that on and the rig is idle."""
        time.sleep(self.UPDATE_CHECK_DELAY)
        while True:
            try:
                if self._update_source() == "github":
                    available = (self._check_update_now() or {}).get("available")
                    last = updates.status(self._update_dir()) or {}
                    if (available and self.update_auto() and not self.running_job_ids()
                            and not self.__dict__.get("_update_staging")
                            and last.get("version") != available.get("version")):
                        self._stage_update(available)
            except Exception:  # noqa: BLE001 -- one bad look never ends the watch
                pass
            time.sleep(self.UPDATE_CHECK_EVERY)

    def start_update_watch(self) -> None:
        """Started by the service once it serves; never by a test's manager
        (ALTERIOM_HIL_UPDATE_WATCH=0 keeps it off)."""
        if os.environ.get("ALTERIOM_HIL_UPDATE_WATCH", "1") == "0" or self.__dict__.get("_update_watching"):
            return
        self.__dict__["_update_watching"] = True
        threading.Thread(target=self._update_watch, name="rig-update-watch", daemon=True).start()

    def _github(self) -> "github_access.Status":
        status = self.__dict__.get("_github_status")
        path = self.github_token_path()
        if status is None or status.path != path:
            status = github_access.Status(path)
            self.__dict__["_github_status"] = status
        return status

    def set_github_token(self, body, identity=None) -> dict:
        """The token from the page. Checked with GitHub before it is kept,
        written 0600 under the state directory (the service's own, which is
        why it can be written from here), and answered as who it is."""
        token = str((body or {}).get("token") or "").strip()
        if not token or any(ch.isspace() for ch in token) or len(token) > 512:
            raise ValueError("a GitHub token is one line, without spaces")
        try:
            who = github_access.whoami(token)
        except github_access.GitHubError as error:
            raise ValueError(f"not stored: {error}") from None
        path = Path(self.state) / "github-token"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(token + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        self.__dict__.pop("_github_status", None)
        return {"stored": True, "login": who["login"], "path": str(path), "github": self._github().view()}

    def remove_github_token(self, body, identity=None) -> dict:
        """Forget the token the page stored. The host's own file, if any, is
        the administrator's to remove (`alteriom-hil-admin github remove`)."""
        path = Path(self.state) / "github-token"
        removed = path.is_file()
        if removed:
            path.unlink()
        self.__dict__.pop("_github_status", None)
        return {"removed": removed, "github": self._github().view()}

    def github_view(self, identity=None) -> dict:
        """The token as the page may know it: who it is, what kind, when it
        expires, where it came from (this page, or the host's file), and --
        when GitHub accepts it -- what it reaches and what it may do with
        each project's repository. Never the token."""
        view = dict(self._github().view())
        view["source"] = "page" if Path(view.get("path") or "") == Path(self.state) / "github-token" else "host"
        if view.get("connected"):
            view["access"] = self._github_access_report(self._github().token() or "")
        return view

    def check_github(self, body=None, identity=None) -> dict:
        """Ask GitHub again now rather than at the cache's own pace: after a
        token was changed on GitHub, or a repository added to it."""
        self._github().forget()
        self.__dict__.pop("_github_access", None)
        return {"github": self.github_view()}

    def _github_reach(self, token: str) -> dict:
        return github_access.reachable_repositories(token)

    def _github_repo_access(self, token: str, url: str) -> dict:
        return github_access.repository_access(token, url)

    def _project_repositories(self) -> list:
        from alteriom_hil.service import HEALTH_CHECK_PROFILE
        rows = []
        for name, spec in sorted(self.profiles.items()):
            if name == HEALTH_CHECK_PROFILE or not spec.repo:
                continue
            rows.append({"name": name, "label": spec.label, "repo": spec.repo,
                         "supply_repo": spec.supply_repo or spec.repo})
        return rows

    def _project_extras_for(self, names, identity=None) -> dict:
        """The description GitHub gives each project's repository, from the
        access report already held -- never a request of its own: the
        library draws without waiting on GitHub."""
        held = self.__dict__.get("_github_access") or {}
        described = {}
        for entry in ((held.get("report") or {}).get("projects") or []):
            access = entry.get("repo_access") or {}
            if access.get("description"):
                described[entry.get("name")] = access["description"]
        return {name: {"description": described.get(name)} for name in names}

    def _github_access_report(self, token: str) -> dict:
        """What the token reaches, and what it may do with each project's
        repository (read it, read its code, list its bundles). Asked of
        GitHub rarely -- kept github_access.STATUS_TTL, keyed by the token
        and the projects -- and again on request (check_github)."""
        rows = self._project_repositories()
        key = (hashlib.sha256(token.encode("utf-8")).hexdigest(),
               tuple((row["repo"], row["supply_repo"]) for row in rows))
        held = self.__dict__.get("_github_access")
        if held and held["key"] == key and time.time() - held["at"] < github_access.STATUS_TTL:
            return held["report"]
        try:
            reach = self._github_reach(token)
        except github_access.GitHubError as error:
            reach = {"repositories": [], "more": False, "error": str(error)}
        seen: dict = {}
        projects = []
        for row in rows:
            entry = dict(row)
            for field in ("repo", "supply_repo"):
                url = row[field]
                if url not in seen:
                    try:
                        seen[url] = self._github_repo_access(token, url)
                    except github_access.GitHubError as error:
                        seen[url] = {"repo": url, "metadata": False, "contents": False, "actions": False,
                                     "private": None, "error": str(error), "refused": {}}
                entry[f"{field}_access"] = seen[url]
            projects.append(entry)
        report = {"repositories": reach.get("repositories", []), "more": bool(reach.get("more")),
                  "error": reach.get("error"), "projects": projects,
                  "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.__dict__["_github_access"] = {"key": key, "at": time.time(), "report": report}
        return report

    def _cannot_read(self, token: str, repo: str, error, what: str = "") -> str:
        """Why a repository was refused, said with what would fix it: a
        fine-grained token reaches the repositories it was given and no
        other, so the refusal names them, and where on GitHub the list is."""
        words = f"this rig's GitHub token cannot read {what}{repo}: {error}"
        if self._github().view().get("kind") != "fine-grained":
            return words
        try:
            reach = self._github_reach(token)
        except github_access.GitHubError:
            reach = {"repositories": [], "more": False}
        # The private ones: GitHub lists every public repository the user can
        # see for any token, so only the private ones say what the token was given.
        names = sorted(row["name"] for row in reach.get("repositories", []) if row.get("private"))
        listed = ", ".join(names[:12]) + (", …" if len(names) > 12 else "")
        short = repo.split("github.com/", 1)[-1]
        return (f"{words}. It is a fine-grained token given {len(names)} private "
                f"repositor{'y' if len(names) == 1 else 'ies'}{': ' + listed if listed else ''}. "
                f"On GitHub, add {short} to the token's repository access (Settings → Developer settings → "
                f"Personal access tokens → the token), or give this rig a token that reaches it (Settings → Rig → GitHub).")

    def github_summary(self) -> dict:
        """What a farm may know about this rig's GitHub: connected or not,
        as whom, what kind, when it expires. Never the token, never its path."""
        status = self._github().view()
        return {key: status.get(key) for key in ("configured", "connected", "login", "kind", "expires_at", "expires_in_days")}

    def rig_view(self) -> dict:
        view = super().rig_view()
        view["github"] = self.github_summary()
        return view

    def _require_github(self) -> str:
        """The token, when GitHub is connected; otherwise why a project cannot
        be added, as a refusal (403) that names the command."""
        view = self._github().view()
        if not view["configured"]:
            raise PermissionError(
                f"GitHub is not connected on this rig, so it cannot take a project: a project is a GitHub "
                f"repository whose CI builds what the rig flashes. Connect it with `{github_access.SET_COMMAND}` "
                f"(a fine-grained token with Contents: read on the project's repositories, and Actions: read "
                f"to fetch its bundles).")
        if not view["connected"]:
            raise PermissionError(f"GitHub does not accept this rig's token: {view['error']}. Replace it with `{github_access.SET_COMMAND}`.")
        return self._github().token() or ""

    def _github_repository(self, token: str, url: str) -> dict:
        """The repository as GitHub shows it to this rig's token. Its own
        method so a test can stand in for GitHub."""
        return github_access.repository(token, url)

    def _github_look_around(self, token: str, url: str) -> dict:
        return github_access.look_around(token, url)

    def _connected_families(self) -> list:
        """The families of the boards connected right now: what a project
        added here most likely wants, when nothing says otherwise."""
        try:
            boards = (self.inventory_snapshot() or {}).get("boards") or []
        except Exception:  # noqa: BLE001 -- a suggestion, never a refusal
            return []
        return sorted({str(board.get("target")) for board in boards if isinstance(board, dict) and board.get("target")})

    def inspect_repository(self, body, identity=None) -> dict:
        """The repository as GitHub shows it to this rig's token, and what a
        project made of it would look like: the default branch, a suite
        directory, a supply workflow, families -- found in the project's own
        `.alteriom-hil.yaml` when it has one, guessed and said so otherwise,
        and the families of the boards connected right now when nothing
        says. The person confirms; the rig writes nothing here."""
        token = self._require_github()
        repo = github_access.normalise_repo(str((body or {}).get("repo") or ""))
        try:
            found = self._github_look_around(token, repo)
        except github_access.GitHubError as error:
            raise ValueError(self._cannot_read(token, repo, error)) from None
        owner, name = github_access.parse_repo(found["repo"])
        suggested = {
            "name": re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:64] or "project",
            "label": found.get("label") or name,
            "repo": found["repo"],
            "default_ref": found.get("default_ref") or "main",
            "suite_path": found.get("suite_path") or "tests",
            "families": found.get("families") or self._connected_families(),
            "supply_repo": found["repo"],
            "supply_workflow": found.get("supply_workflow") or ".github/workflows/hil.yml",
            "supply_artifact": found.get("supply_artifact") or "hil-artifacts",
            "revision_key": found.get("revision_key") or DEFAULT_REVISION_KEY,
        }
        return {"repo": found["repo"], "private": found.get("private"), "default_ref": found.get("default_ref"),
                "found": found.get("found", []), "guessed": found.get("guessed", []),
                "workflows": found.get("workflows", []), "suggested": suggested,
                "taken": suggested["name"] in self.profiles}

    # ---- the rig's own details: what it calls itself ---------------------------------

    def _rig_details_path(self) -> Path:
        return Path(self.state) / "rig.json"

    def rig_details(self) -> dict:
        try:
            saved = json.loads(self._rig_details_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        return {key: (str(saved.get(key)).strip() or None) if saved.get(key) is not None else None
                for key in ("name", "description", "location")}

    def rig_details_view(self, identity=None) -> dict:
        return {**self.rig_details(), "host": self._own_name()}

    def set_rig_details(self, body, identity=None) -> dict:
        """A name of its own (else the host's), a description, a location:
        what the rig's page and, once connected, a portal's page for it
        show. Kept in the state directory; a portal never overwrites it."""
        fields = body or {}
        name = str(fields.get("name") or "").strip()
        if name and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", name):
            raise ValueError("a rig's name is 1-32 lowercase letters, digits, dots, underscores or hyphens (or empty for the host's)")
        description = str(fields.get("description") or "").strip()[:200]
        location = str(fields.get("location") or "").strip()[:120]
        path = self._rig_details_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"name": name or None, "description": description or None,
                                   "location": location or None}, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return self.rig_details_view()

    # ---- projects: what this rig runs -----------------------------------------------

    def _local_profiles_dir(self) -> Path:
        return Path(self.state) / "profiles"

    def _project_row(self, name: str) -> dict:
        spec = self.profiles[name]
        return {
            "name": name,
            "label": spec.label,
            "shipped": name in self.shipped_profiles and name not in self.overridden_profiles,
            # shipped: the release's, as it came; changed: the release's, with
            # the operator's document over it; own: the operator's alone.
            "origin": "changed" if name in self.overridden_profiles else "shipped" if name in self.shipped_profiles else "own",
            "location": spec.location,
            "repo": spec.repo,
            "default_ref": spec.default_ref,
            "suite_path": spec.suite_path,
            "revision_key": spec.revision_key,
            "min_boards": spec.min_boards,
            "exclusive": spec.exclusive,
            "timeout_seconds": spec.suite_timeout,
            "needs": [dict(need) for need in spec.needs],
            "supply_repo": spec.supply_repo,
            "supply_workflow": spec.supply_workflow,
            "supply_artifact": spec.supply_artifact,
        }

    def projects_view(self, identity=None) -> dict:
        """Every project this rig can run: the ones its release ships and
        the ones its operator added, told apart, because only the latter are
        changed from here."""
        from alteriom_hil.service import HEALTH_CHECK_PROFILE
        health = self.profiles.get(HEALTH_CHECK_PROFILE)
        return {
            "projects": [self._project_row(name) for name in sorted(self.profiles) if name != HEALTH_CHECK_PROFILE],
            # Shipped projects the operator removed from this rig; each can be
            # restored.
            "removed": sorted(self.removed_profile_names),
            # The rig's own firmware: run from Boards, installed with a
            # release. Said here so the page can say why it is not a project.
            "health_check": {"name": HEALTH_CHECK_PROFILE, "label": health.label} if health else None,
            "directory": str(self._local_profiles_dir()),
            "default_profile": self.default_profile,
            # Whether a project can be added here at all, and as whom.
            "github": self._github().view(),
        }

    def _project_document(self, fields: dict) -> dict:
        """A profile document from the few things a person knows about their
        project. Everything else is the generic shape: the suite lives in
        the project's repository and is checked out per run; its firmware
        is a bundle the project's own CI built and handed over; the rig's
        flasher flashes it by the manifest. Validated as any profile is, so
        a document that would stop the service starting is refused here."""
        text = lambda key, default="": str(fields.get(key) if fields.get(key) is not None else default).strip()
        name = text("name").lower()
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError("a project's name is lowercase letters, digits and dashes, up to 64")
        repo = github_access.normalise_repo(text("repo"))   # GitHubError is a ValueError: a 400 with the reason
        label = text("label", name)[:80] or name
        default_ref = text("default_ref", "main")[:120]
        if not default_ref or any(ch.isspace() for ch in default_ref):
            raise ValueError("default_ref is a branch, tag or commit, without spaces")
        suite_path = text("suite_path", "tests").strip("/")
        # The key a schema-2 manifest records the commit under. A project
        # that writes it under another name says so in its .alteriom-hil.yaml
        # or in the form; nothing is guessed from the project's name.
        revision_key = text("revision_key", DEFAULT_REVISION_KEY)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", revision_key):
            raise ValueError("revision_key is the manifest key that holds the commit: letters, digits and underscores")
        supply_repo = github_access.normalise_repo(text("supply_repo", repo))
        supply_workflow = text("supply_workflow", ".github/workflows/hil.yml")
        supply_artifact = text("supply_artifact", "hil-artifacts")
        try:
            min_boards = int(fields.get("min_boards") or 1)
            timeout = int(fields.get("timeout_seconds") or 1800)
        except (TypeError, ValueError):
            raise ValueError("min_boards and timeout_seconds are whole numbers") from None
        families = fields.get("families") or []
        if isinstance(families, str):
            families = [part.strip() for part in families.split(",") if part.strip()]
        if not isinstance(families, list):
            raise ValueError("families is a list of chip families")
        unknown = sorted(set(families) - set(TARGETS))
        if unknown:
            raise ValueError(f"not a chip family this rig knows: {', '.join(unknown)} (one of {', '.join(sorted(TARGETS))})")
        # Families named: the run takes one board of each and leaves the rest
        # free. None named: it takes the whole bench, as the shipped suites do.
        exclusive = fields.get("exclusive", not families)
        if not isinstance(exclusive, bool):
            raise ValueError("exclusive is true or false")
        doc = {
            "schema": 1,
            "name": name,
            "label": label,
            "source": {"location": "consumer", "repo": repo, "default_ref": default_ref},
            "build": {"revision_key": revision_key},
            "flash": {"command": ["{python}", "-m", "alteriom_hil.flash_artifacts",
                                  "--artifacts", "{artifact_dir}", "--board-map", "{board_map}",
                                  "--revision-key", revision_key]},
            "supply": {"repo": supply_repo, "workflow": supply_workflow, "artifact": supply_artifact},
            "suite": {"path": suite_path, "min_boards": min_boards, "exclusive": exclusive,
                      "timeout_seconds": timeout},
            "report": {"title": f"HIL {label} {{revision}}"},
        }
        if families:
            doc["needs"] = [{"target": family, "count": 1} for family in dict.fromkeys(families)]
        parse_profile(doc, f"project {name}")   # ProfileError is a ValueError: a 400 with the reason
        return doc

    def _own_project(self, name: str) -> Path:
        """The document a change or removal of this project touches: the
        operator's own under the state directory -- for a shipped project,
        the override that will stand in for it. The health check is the
        rig's own and is not a project to change."""
        from alteriom_hil.service import HEALTH_CHECK_PROFILE
        if name not in self.profiles:
            raise LookupError(f"no project named {name} on this rig")
        if name == HEALTH_CHECK_PROFILE:
            raise ValueError(f"{name} is the rig's own health check, not a project; it comes with the release")
        return self._local_profiles_dir() / f"{name}.yaml"

    def _write_project(self, doc: dict) -> dict:
        directory = self._local_profiles_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{doc['name']}.yaml"
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        os.replace(tmp, path)
        try:
            self.reload_profiles()
        except Exception:
            # Whatever was written is not what the running rig will run on;
            # take it back rather than leave a service that cannot restart.
            path.unlink(missing_ok=True)
            self.reload_profiles()
            raise
        return {"project": self._project_row(doc["name"]), "path": str(path),
                "projects": self.projects_view()["projects"]}

    def _checked_with_github(self, fields: dict) -> dict:
        """The fields, once GitHub has been asked about the repository with
        this rig's token: refused when the token cannot read it, and given
        the repository's own default branch when none was named."""
        token = self._require_github()
        fields = dict(fields or {})
        repo = github_access.normalise_repo(str(fields.get("repo") or ""))
        try:
            seen = self._github_repository(token, repo)
        except github_access.GitHubError as error:
            raise ValueError(self._cannot_read(token, repo, error)) from None
        fields["repo"] = seen.get("url") or repo
        if not str(fields.get("default_ref") or "").strip():
            fields["default_ref"] = seen.get("default_branch") or "main"
        supply = str(fields.get("supply_repo") or "").strip()
        if supply and github_access.normalise_repo(supply) != fields["repo"]:
            try:
                self._github_repository(token, github_access.normalise_repo(supply))
            except github_access.GitHubError as error:
                raise ValueError(self._cannot_read(token, github_access.normalise_repo(supply), error, what="the supply repository ")) from None
        return fields

    def create_project(self, body, identity=None) -> dict:
        from alteriom_hil.service import HEALTH_CHECK_PROFILE
        fields = self._checked_with_github(body or {})
        doc = self._project_document(fields)
        if doc["name"] == HEALTH_CHECK_PROFILE:
            raise ValueError(f"{doc['name']} is the rig's own health check; choose another name")
        if doc["name"] in self.profiles:
            shipped = doc["name"] in self.shipped_profiles
            raise ValueError(f"{doc['name']} is already a project on this rig"
                             + (", shipped with it" if shipped else "") + "; choose another name")
        if doc["name"] in self.removed_profile_names:
            # A removed shipped name, taken for a project of one's own: the
            # document stands in for the shipped one; the tombstone goes.
            (self._local_profiles_dir() / f"{doc['name']}.removed").unlink(missing_ok=True)
        return self._write_project(doc)

    def update_project(self, body, name, identity=None) -> dict:
        self._own_project(name)
        fields = self._checked_with_github({**(body or {}), "name": name})
        return self._write_project(self._project_document(fields))

    # ---- the newest bundle a project's CI built -------------------------------------

    def _github_newest_artifact(self, token: str, spec) -> dict:
        return github_access.newest_artifact(token, spec.supply_repo, spec.supply_artifact or "hil-artifacts",
                                             spec.supply_workflow)

    def _github_download(self, token: str, url: str) -> bytes:
        return github_access.download_artifact(token, url)

    def fetch_project_bundle(self, body, name, identity=None) -> dict:
        """Take the newest bundle the project's supply workflow uploaded to
        GitHub, as if that CI had handed it over: the same acceptance, the
        same provenance, so a run can flash it. A bundle the rig already
        holds for that run is not taken twice."""
        spec = self.profiles.get(name)
        if spec is None:
            raise LookupError(f"no project named {name} on this rig")
        if not spec.accepts_supplied_bundles:
            raise ValueError(f"{name} declares no supply workflow to fetch a bundle from")
        token = self._require_github()
        try:
            found = self._github_newest_artifact(token, spec)
            held = self._held_bundle(name, found["commit"], found["run_id"])
            if held is not None:
                return {"fetched": False, "held": True, "bundle": held, "artifact": found}
            zipped = self._github_download(token, found["download_url"])
            body = github_access.tarball_from_zip(zipped)
        except github_access.GitHubError as error:
            message = str(error)
            if "(403)" in message:
                message += (". Listing a repository's Actions artifacts needs Actions: read on it; what the token "
                            "may do with each project is on Settings → Rig → GitHub.")
            raise ValueError(message) from None
        accepted = self.accept_bundle({
            "profile": name, "repo": found["repo"], "workflow": spec.supply_workflow,
            "run_id": found["run_id"], "run_url": found["run_url"], "commit": found["commit"],
            "branch": found["branch"], "actor": found["actor"],
        }, body)
        return {"fetched": not accepted.get("reused", False), "held": bool(accepted.get("reused", False)),
                "bundle": accepted, "artifact": found}

    def _held_bundle(self, profile: str, commit: str, run_id: str) -> dict | None:
        """A bundle already in the store from that run of that project."""
        from alteriom_hil import artifact_store
        for bundle in artifact_store.scan(self.artifact_root).bundles.values():
            provenance = self._provenance_of(bundle)
            if (provenance.get("profile") == profile and str(provenance.get("run_id") or "") == run_id
                    and str(provenance.get("commit") or "").lower() == commit.lower()):
                return {"id": bundle.id, "profile": profile, "revision": commit, "source": provenance}
        return None

    @staticmethod
    def _provenance_of(bundle) -> dict:
        try:
            return json.loads((bundle.path / "provenance.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def delete_project(self, body, name, identity=None) -> dict:
        """The operator's own document goes; a shipped project is hidden by a
        tombstone instead, and can be restored. Its runs stay in the history
        as what they were."""
        path = self._own_project(name)
        path.unlink(missing_ok=True)
        if name in self.shipped_profiles:
            self._local_profiles_dir().mkdir(parents=True, exist_ok=True)
            (self._local_profiles_dir() / f"{name}.removed").write_text("", encoding="utf-8")
        self.reload_profiles()
        view = self.projects_view()
        return {"removed": name, "projects": view["projects"], "removed_shipped": view["removed"]}

    def restore_project(self, body, name, identity=None) -> dict:
        """A shipped project back as the release ships it: the tombstone and
        any override of it go."""
        if name not in self.shipped_profiles:
            raise LookupError(f"{name} is not a project the release ships, so there is nothing to restore")
        directory = self._local_profiles_dir()
        (directory / f"{name}.removed").unlink(missing_ok=True)
        (directory / f"{name}.yaml").unlink(missing_ok=True)
        self.reload_profiles()
        view = self.projects_view()
        return {"restored": name, "project": self._project_row(name), "projects": view["projects"],
                "removed_shipped": view["removed"]}

    # ---- a run, or a project's runs, deleted --------------------------------------------

    def delete_run(self, body, job_id, identity=None) -> dict:
        """A finished run, gone: its evidence and captures, its log, the
        artifact link that was its firmware, and its record. What a run
        left is the operator's to keep or not; retention removes evidence by
        age, this removes a run by choice."""
        job = self.store.get(job_id)
        if job is None:
            raise LookupError("no such run")
        if job.get("status") in ("queued", "running"):
            raise ValueError(f"run {job_id[:8]} is {job['status']}; cancel it before deleting it")
        freed = 0
        for path in (self.state / "runs" / job_id, self.state / "artifacts" / job_id):
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                freed += sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())
                shutil.rmtree(path, ignore_errors=True)
        log_file = self.state / "logs" / f"{job_id}.log"
        if log_file.is_file():
            freed += log_file.stat().st_size
            log_file.unlink()
        self.store.delete_job(job_id)
        self._storage_changed()
        return {"deleted": job_id, "bytes": freed, "profile": (job.get("request") or {}).get("profile")}

    def delete_project_runs(self, body, name, identity=None) -> dict:
        """Every finished run of this project, deleted; the queued or running
        ones are left and named."""
        if name not in self.profiles and name not in self.removed_profile_names:
            raise LookupError(f"no project named {name} on this rig")
        deleted, kept, freed = [], [], 0
        for job in self.store.recent(limit=100000):
            request = job.get("request") or {}
            if job.get("kind") != "suite" or request.get("profile", DEFAULT_PROFILE) != name:
                continue
            if job.get("status") in ("queued", "running"):
                kept.append(job["id"])
                continue
            answer = self.delete_run(None, job["id"])
            deleted.append(job["id"])
            freed += answer["bytes"]
        return {"project": name, "deleted": deleted, "kept": kept, "bytes": freed}

    # ---- the farm this rig shows ---------------------------------------------------

    def _public_farm_url(self) -> str | None:
        """The farm whose public page this rig shows: the portal it reports
        to, else `farm.public_url`, else the Alteriom farm; "off" is none."""
        mode = self.__dict__.get("mode", "standalone")
        if mode == "node" or os.environ.get("ALTERIOM_HIL_FARM_ATTACHED") == "1":
            portal = (os.environ.get("ALTERIOM_HIL_PORTAL_URL") or "").strip()
            if portal:
                return portal.rstrip("/")
        chosen = os.environ.get("ALTERIOM_HIL_FARM_PUBLIC_URL")
        if chosen is None:
            return self.PUBLIC_FARM_URL
        chosen = chosen.strip()
        return None if chosen in ("", "off") else chosen.rstrip("/")

    def _read_public_farm(self, url: str) -> dict:
        """One read of a portal's world page, trimmed to the fields the
        overview shows. Split out so a test can stand in for the network."""
        import urllib.request
        request = urllib.request.Request(f"{url}/api/v1/world", headers={
            "Accept": "application/json", "User-Agent": "alteriom-hil-rig"})
        with urllib.request.urlopen(request, timeout=self.PUBLIC_FARM_TIMEOUT) as response:
            payload = json.loads(response.read(512 * 1024).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("not a world page")
        rigs = []
        for rig in (payload.get("rigs") or [])[:50]:
            if isinstance(rig, dict) and rig.get("name"):
                rigs.append({key: rig.get(key) for key in (
                    "name", "description", "location", "online", "version", "health",
                    "boards", "families", "runs", "setup")})
        return {"rigs": rigs, "stats": payload.get("stats") or {},
                "window_days": payload.get("window_days"), "software": payload.get("software") or {}}

    def farm_public_view(self, identity=None) -> dict:
        """The public farm as its world page tells it, read by this rig and
        kept a while: a rig asks once in ten minutes, however many browsers
        are open on it, and a farm that cannot be reached is said so, with
        the last good answer if there was one."""
        url = self._public_farm_url()
        if not url:
            return {"url": None, "ok": False, "error": None, "fetched_at": None, "world": None}
        cache = self.__dict__.setdefault("_public_farm_cache", {})
        now = time.time()
        held = cache.get(url)
        if held and now - held["at"] < (self.PUBLIC_FARM_TTL if held["answer"]["ok"] else self.PUBLIC_FARM_RETRY):
            return held["answer"]
        try:
            world = self._read_public_farm(url)
            answer = {"url": url, "ok": True, "error": None, "fetched_at": utcnow(), "world": world}
        except Exception as exc:  # the network, the portal, the shape: all one thing to the page
            reason = f"{exc.__class__.__name__}: {exc}"[:200]
            answer = {"url": url, "ok": False, "error": reason, "fetched_at": utcnow(),
                      "world": held["answer"]["world"] if held else None}
        cache[url] = {"at": now, "answer": answer}
        return answer

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
        token_path = self.github_token_path()
        if not token_path.is_file():
            return spec.repo, None
        if not os.access(token_path, os.R_OK):
            # Present but unreadable is its own failure, and a silent one
            # otherwise: the askpass helper would return nothing, git would see
            # an empty password, and the run would fail as "authentication
            # failed" against a token that is perfectly valid. The service does
            # not run as root, so the file has to be group-readable by the
            # service account, exactly like the API token beside it.
            raise PipelineError(
                "build",
                "The consumer credential cannot be read",
                f"{token_path} exists but is not readable by this "
                f"service. Match the API token's permissions: "
                f"chmod 640 and chgrp to the service group.",
            )
        askpass = self.state / "git-askpass.sh"
        askpass.write_text(
            "#!/bin/sh\n"
            "# Answers git's password prompt from the token file. Written here\n"
            "# so the token never reaches argv, a URL, or this log.\n"
            f"cat {token_path}\n",
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
