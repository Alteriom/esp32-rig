"""A portal and a node, as two farm services talking over HTTP.

docs/portal-plan.md, phase 1: a portal with no hardware leases a run to a node,
the node runs it with the pipeline every farm runs, and the run's page on the
portal has its stages, its log, its evidence and its board verdicts. These run
both services for real -- the dispatcher, the HTTP routes, the keys, the
agent's threads -- with only the hardware pipeline replaced.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
import uuid
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from alteriom_hil import farm_shared
import yaml

from alteriom_hil.api_keys import KeyStore, add_key, allowed, keys_document

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "runner"


def portal_web_root() -> Path:
    """A portal's web root: the rig's dashboard with the portal's site over
    it, which is how the image builds one (two COPYs into the same directory,
    docs/public-release-plan.md, step 12d). Built once, in a temporary
    directory, so neither tree is written to."""
    import shutil
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="portal-web-"))
    for tree in (REPO / "rig" / "web", REPO / "portal" / "web"):
        shutil.copytree(tree, root, dirs_exist_ok=True)
    return root


PORTAL_WEB_ROOT = portal_web_root()
sys.path.insert(0, str(RUNNER))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


farm_service = _load("farm_service_portal", RUNNER / "farm_service.py")
# The portal's half reads its own constants; a test that changes one for a
# run changes it where it is read.
from alteriom_hil import portal_manager as portal_half  # noqa: E402
# The service itself, where those names are read: it is
# `alteriom_hil.service` now and this file is the launcher that composes
# the halves onto it, so a test that changes one changes it there.
from alteriom_hil import service as core_service  # noqa: E402
farm_node = _load("farm_node_portal", RUNNER / "farm_node.py")

SHA = "c" * 40
BOARDS = [{"id": "b-esp32-01", "target": "esp32", "port": "/dev/esp32-farm-01", "mac": "aa:bb:cc:dd:ee:01"}]
TOKEN = "t" * 40


def _wait(predicate, timeout: float = 30.0, what: str = "the farm"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _bundle(root: Path, bundle_id: str, agent: str) -> Path:
    from alteriom_hil.artifacts import sha256

    path = root / bundle_id
    folder = path / "esp32"
    folder.mkdir(parents=True)
    (folder / "bootloader.bin").write_bytes(b"\xe9esp32")
    (folder / "flash-image.bin").write_bytes(b"\xff" * 0x1000 + b"\xe9esp32")
    manifest = {
        "schema": 2, "canary_sha": SHA, "painlessmesh_sha": SHA, "hil_agent_sha": agent,
        "targets": {"esp32": {
            "image": "esp32/flash-image.bin", "sha256": sha256(folder / "flash-image.bin"),
            "platformio_env": "esp32", "board": "esp32dev", "segments": {"bootloader.bin": "0x1000"},
            "files": {"bootloader.bin": {"sha256": sha256(folder / "bootloader.bin")}},
        }},
    }
    (path / "manifest.json").write_text(json.dumps(manifest))
    (path / "provenance.json").write_text(json.dumps({"kind": "supplied", "profile": "canary", "commit": SHA}))
    return path


class Farm:
    """A portal on loopback with keys for two workers and a person, and a node."""

    def __init__(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(farm_shared, "RIG_LOCK_PATH", tmp_path / "rig.lock")
        etc = tmp_path / "etc"
        etc.mkdir()
        (etc / "api-token").write_text(TOKEN)
        entries, self.node_key = add_key([], "node-a", "node")
        entries, self.other_node_key = add_key(entries, "node-b", "node")
        entries, self.user_key = add_key(entries, "sparck", "user")
        (etc / "api-keys.yaml").write_text(keys_document(entries))
        self.portal = farm_service.manager_for("portal")(
            REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
            Path(sys.executable), mode="portal",
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            farm_service.make_handler(self.portal, KeyStore(TOKEN, etc / "api-keys.yaml"), PORTAL_WEB_ROOT),
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.node = farm_service.manager_for("node")(
            REPO, tmp_path / "node", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
            Path(sys.executable), mode="node",
        )
        self.node.inventory_snapshot = lambda annotate=False: {
            "boards": [dict(board) for board in BOARDS], "missing": [], "unregistered": [],
            "probe_errors": [], "updated_at": "2026-09-14T00:00:00+00:00",
        }
        self.agent = None

    def start_agent(self, execute, releases: Path | None = None) -> None:
        self.node._execute = execute
        client = farm_node.PortalClient(self.url, self.node_key)
        self.agent = farm_node.NodeAgent(self.node, client, "node-a", heartbeat_seconds=0.2, lease_wait=1,
                                         releases=releases)
        self.agent.start()
        _wait(lambda: self.portal.online_workers(), what="the node to say hello")

    def bundle(self) -> str:
        bundle_id = "b" * 32
        _bundle(self.portal.artifact_root, bundle_id, self.node.agent_source_sha())
        return bundle_id

    def canary(self, bundle_id: str) -> str:
        request = {"profile": "canary", "boards": ["b-esp32-01"], "targets": ["esp32"], "artifact": bundle_id,
                   "resolved_sha": SHA, "supersede": False, "project": "Farm canary"}
        job = self.portal.store.create(
            "suite", request, self.portal.state / "logs" / "x.log", self.portal._initial_progress("suite", request),
        )
        self.portal.pending.put(("test", job["id"], {}))
        return job["id"]

    def call(self, method: str, path: str, key: str, body: bytes | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(self.url + path, data=body, method=method, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def close(self) -> None:
        if self.agent:
            self.agent.stop()
        self.server.shutdown()


@pytest.fixture
def farm(tmp_path, monkeypatch):
    farm = Farm(tmp_path, monkeypatch)
    yield farm
    farm.close()


def _canary_pipeline(node, gate: threading.Event | None = None):
    def execute(job_id, kind, request, log):
        assert (node.artifact_root / request["artifact"] / "manifest.json").is_file(), "the bundle came from the portal"
        node._stage(job_id, "build", "running")
        node._stage(job_id, "build", "passed", "Supplied by the portal")
        log.write("flashing b-esp32-01\n")
        log.flush()
        runs = node.state / "runs" / job_id
        (runs / "serial").mkdir(parents=True)
        (runs / "serial" / "b-esp32-01.serial.log").write_text("boot ok\n")
        (runs / "results.xml").write_text(
            '<testsuite><testcase classname="tests.test_rig_health" '
            'name="test_the_radio_joins_the_rig_ap[b-esp32-01]"/></testsuite>'
        )
        if gate is not None:
            assert gate.wait(20)
        for stage in ("discover", "flash", "preflight", "test", "report"):
            node._stage(job_id, stage, "passed")
        return {"summary": "Validated 1 board", "revision": SHA, "results": str(runs / "results.xml")}

    return execute


def test_a_portal_leases_a_canary_run_to_a_node_and_shows_all_of_it(farm):
    bundle_id = farm.bundle()
    gate = threading.Event()
    farm.start_agent(_canary_pipeline(farm.node, gate))
    job_id = farm.canary(bundle_id)

    # Running on the node, not only recorded there: the node records a job a
    # moment before it claims it.
    running = _wait(lambda: (farm.portal.store.get(job_id) or {}).get("worker") == "node-a"
                    and (farm.node.store.get(job_id) or {}).get("status") == "running"
                    and farm.node.store.get(job_id), what="the node to take the run")
    assert running["status"] == "running"
    held = farm.portal.inventory_snapshot(annotate=True)["boards"][0]
    assert held["worker"] == "node-a" and held["state"] == "in_use", "the portal shows the board as the run's"
    _wait(lambda: "flashing b-esp32-01" in (farm.portal.state / "logs" / f"{job_id}.log").read_text()
          if (farm.portal.state / "logs" / f"{job_id}.log").exists() else False, what="the log")
    gate.set()

    done = _wait(lambda: (job := farm.portal.store.get(job_id))["status"] == "passed" and job, what="the verdict")
    assert done["result"]["summary"] == "Validated 1 board"
    runs = farm.portal.state / "runs" / job_id
    assert done["result"]["results"] == str(runs / "results.xml"), "paths point at what arrived on the portal"
    assert (runs / "serial" / "b-esp32-01.serial.log").read_text() == "boot ok\n", "the evidence"
    assert [stage["status"] for stage in done["progress"]] == ["passed"] * 6
    assert done["result"]["health"]["boards"] == {"b-esp32-01": "passed"}, "the portal judged the boards"
    assert farm.portal.store.board_verdicts("b-esp32-01")[0]["outcome"] == "passed"
    assert (farm.portal.state / "artifacts" / job_id).is_symlink(), "the run holds its bundle on the portal"

    _wait(lambda: not farm.portal.running_job_ids(), what="the portal to release the board")
    _wait(lambda: not list(farm.agent.outbox.glob("*.json")), what="the node's outbox to empty")
    provenance = json.loads((farm.node.artifact_root / bundle_id / "provenance.json").read_text())
    assert provenance["fetched_from"] == farm.url
    workers = farm.portal.workers_view()
    assert workers[0]["name"] == "node-a" and workers[0]["online"] and workers[0]["boards"] == 1
    assert "canary" in workers[0]["profiles"]
    # And what it can do, as chips: the list of rigs says "1 board" under the
    # name without a page per rig, and carries only what a chip says.
    chips = {chip["key"]: chip for chip in workers[0]["setup"]}
    assert chips["boards"]["label"] == "1 board" and chips["boards"]["state"] == "on"
    assert all(set(chip) == {"key", "title", "label", "state", "summary"} for chip in workers[0]["setup"])

    # What CI takes home from the portal: the job and its evidence over the
    # API, with a person's key -- no path on the portal's disk is read.
    import ci_farm_client

    detail = ci_farm_client.request_json(farm.url, farm.user_key, f"/api/v1/jobs/{job_id}")
    evidence = farm.portal.state.parent / "ci-evidence"
    ci_farm_client.copy_evidence(detail, evidence, ci_farm_client.artifact_fetcher(farm.url, farm.user_key, job_id))
    assert (evidence / "serial" / "b-esp32-01.serial.log").read_text() == "boot ok\n"
    assert "testcase" in (evidence / "results.xml").read_text()
    assert "flashing b-esp32-01" in (evidence / "farm-job.log").read_text()


def test_a_run_cancelled_on_the_portal_is_interrupted_on_the_node(farm):
    bundle_id = farm.bundle()

    def execute(job_id, kind, request, log):
        farm.node._stage(job_id, "build", "running")
        farm.node._run([sys.executable, "-c", "import time; time.sleep(30)"], log, timeout=60, grace=2)
        return {"summary": "should not get here"}

    farm.start_agent(execute)
    job_id = farm.canary(bundle_id)
    _wait(lambda: farm.node.store.get(job_id) and farm.node.store.get(job_id)["status"] == "running", what="the run to start")
    time.sleep(1.2)
    farm.portal.cancel(job_id, "Cancelled by operator")
    done = _wait(lambda: (job := farm.portal.store.get(job_id))["status"] == "cancelled" and job, what="the cancellation")
    assert done["result"]["summary"] == "Cancelled by operator"


def test_a_worker_speaks_only_for_itself_and_a_person_cannot_pose_as_one(farm):
    hello = json.dumps({"kind": "hardware", "max_runs": 1, "profiles": ["canary"],
                        "inventory": {"boards": BOARDS}}).encode()
    assert farm.call("POST", "/api/v1/workers/node-b/hello", farm.node_key, hello)[0] == 403, "not its name"
    assert farm.call("POST", "/api/v1/workers/node-a/hello", farm.user_key, hello)[0] == 403, "a person's key"
    assert farm.call("POST", "/api/v1/workers/node-a/hello", TOKEN, hello)[0] == 403, "not even the farm's own"
    status, answer = farm.call("POST", "/api/v1/workers/node-a/hello", farm.node_key, hello)
    assert status == 200 and answer["worker"] == "node-a"
    assert farm.call("GET", "/api/v1/status", farm.node_key)[0] == 403, "a worker key reads nothing else"

    job_id = farm.canary(farm.bundle())
    _wait(lambda: (farm.portal.store.get(job_id) or {}).get("worker") == "node-a", what="the lease")
    stages = json.dumps({"progress": []}).encode()
    status, answer = farm.call("POST", f"/api/v1/jobs/{job_id}/stages", farm.other_node_key, stages)
    assert status == 403 and "not leased to node-b" in answer["error"]
    # Another worker's board id is refused, not merged.
    other = json.dumps({"kind": "hardware", "max_runs": 1, "profiles": ["canary"],
                        "inventory": {"boards": BOARDS}}).encode()
    status, answer = farm.call("POST", "/api/v1/workers/node-b/hello", farm.other_node_key, other)
    assert status == 200 and answer["conflicts"] == ["b-esp32-01 is already on node-a"]


def test_a_lease_nobody_starts_goes_back_to_the_queue_and_a_quiet_worker_loses_its_run(farm, monkeypatch):
    hello = json.dumps({"kind": "hardware", "max_runs": 1, "profiles": ["canary"],
                        "inventory": {"boards": BOARDS}}).encode()
    assert farm.call("POST", "/api/v1/workers/node-a/hello", farm.node_key, hello)[0] == 200
    job_id = farm.canary(farm.bundle())
    _wait(lambda: (farm.portal.store.get(job_id) or {}).get("worker") == "node-a", what="the lease")

    # The worker never takes it and goes quiet: the lease comes back, and with
    # no worker online the run waits rather than running on the portal.
    monkeypatch.setattr(portal_half, "LEASE_ACK_SECONDS", -1)
    monkeypatch.setattr(portal_half, "WORKER_STALE_SECONDS", -1)
    farm.portal._sweep_workers()
    back = farm.portal.store.get(job_id)
    assert back["status"] == "queued" and back["worker"] is None
    farm.portal._dispatch()
    assert farm.portal.queue_state()["waiting"][job_id] == "no worker is connected"
    assert not farm.portal.running_job_ids()

    # It comes back, takes the run, reports a stage, and goes quiet for good.
    monkeypatch.setattr(portal_half, "WORKER_STALE_SECONDS", 90)
    monkeypatch.setattr(portal_half, "LEASE_ACK_SECONDS", 90)
    assert farm.call("POST", "/api/v1/workers/node-a/heartbeat", farm.node_key,
                     json.dumps({"inventory": {"boards": BOARDS}, "running": []}).encode())[0] == 200
    farm.portal._dispatch()
    status, lease = farm.call("POST", "/api/v1/workers/node-a/lease?wait=1", farm.node_key, b"{}")
    assert status == 200 and lease["job"]["id"] == job_id and lease["bundle"]["id"] == "b" * 32
    assert lease["job"]["request"]["artifact"] == "b" * 32
    assert farm.call("POST", f"/api/v1/jobs/{job_id}/stages", farm.node_key,
                     json.dumps({"progress": lease["job"]["progress"]}).encode())[0] == 200
    monkeypatch.setattr(portal_half, "WORKER_LOST_SECONDS", -1)
    farm.portal._sweep_workers()
    lost = farm.portal.store.get(job_id)
    assert lost["status"] == "failed" and lost["result"]["summary"] == "The worker running this job stopped answering"
    status, answer = farm.call("POST", f"/api/v1/jobs/{job_id}/result", farm.node_key,
                               json.dumps({"status": "passed", "result": {}}).encode())
    assert status == 409, "a verdict for a run the portal already ended is refused, not recorded"


def test_a_portal_runs_no_hardware_and_a_node_takes_no_runs_of_its_own(farm):
    with pytest.raises(farm_service.ElsewhereError):
        farm.portal.submit("inventory", {})
    with pytest.raises(farm_service.ElsewhereError):
        farm.portal.register_device("b-esp32-02", "aa:bb:cc:dd:ee:02")
    with pytest.raises(farm_service.ElsewhereError):
        farm.node.submit("suite", {"profile": "canary"}, submitted_by="sparck")
    status, answer = farm.call("POST", "/api/v1/inventory/refresh", TOKEN, b"{}")
    assert status == 202 and answer == {"requested": []}
    status, answer = farm.call("GET", "/api/v1/workers", farm.user_key)
    assert status == 200 and answer["mode"] == "portal"


def test_a_restarted_portal_keeps_the_runs_its_workers_are_still_doing(farm, tmp_path):
    hello = json.dumps({"kind": "hardware", "max_runs": 1, "profiles": ["canary"],
                        "inventory": {"boards": BOARDS}}).encode()
    farm.call("POST", "/api/v1/workers/node-a/hello", farm.node_key, hello)
    job_id = farm.canary(farm.bundle())
    _wait(lambda: (farm.portal.store.get(job_id) or {}).get("worker") == "node-a", what="the lease")
    restarted = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    assert restarted.store.get(job_id)["status"] == "running"
    assert job_id in restarted.running_job_ids() and restarted._grant(job_id).worker == "node-a"


# ---- releases: what a node runs is the portal's to say ----------------------------------

def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "ci", "GIT_AUTHOR_EMAIL": "ci@example.invalid",
           "GIT_COMMITTER_NAME": "ci", "GIT_COMMITTER_EMAIL": "ci@example.invalid"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def _release(tmp_path: Path, commits: int = 1) -> tuple[Path, str, bytes]:
    """A repository with `commits` commits and a bundle of its whole history."""
    repo = tmp_path / "farm-src"
    repo.mkdir()
    _git(repo, "init", "-q")
    for number in range(commits):
        (repo / "VERSION").write_text(f"1.{number}\n")
        _git(repo, "add", "VERSION")
        _git(repo, "commit", "-qm", f"release {number}")
    _git(repo, "bundle", "create", str(tmp_path / "release.bundle"), "HEAD")
    return repo, _git(repo, "rev-parse", "HEAD"), (tmp_path / "release.bundle").read_bytes()


def test_a_portal_takes_only_a_whole_bundle_of_the_commit_it_names(farm, tmp_path):
    repo, commit, body = _release(tmp_path, commits=2)
    with pytest.raises(ValueError, match="not a git bundle"):
        farm.portal.publish_release(commit, b"PK\x03\x04 a zip", "ci")
    with pytest.raises(ValueError, match="HEAD is"):
        farm.portal.publish_release("d" * 40, body, "ci")
    # A range needs history a node may not have.
    _git(repo, "bundle", "create", str(tmp_path / "range.bundle"), "HEAD~1..HEAD")
    with pytest.raises(ValueError, match="history it does not carry"):
        farm.portal.publish_release(commit, (tmp_path / "range.bundle").read_bytes(), "ci")
    with pytest.raises(farm_service.ElsewhereError):
        farm.node.publish_release(commit, body, "ci")
    status, _ = farm.call("POST", f"/api/v1/releases?commit={commit}", farm.user_key, body)
    assert status == 403, "a user does not decide what the nodes run"
    assert farm.portal.current_release() is None

    status, published = farm.call("POST", f"/api/v1/releases?commit={commit}&note=deploy", TOKEN, body)
    assert status == 201 and published["current"] is True
    assert published["sha256"] == hashlib.sha256(body).hexdigest() and published["bytes"] == len(body)
    status, index = farm.call("GET", "/api/v1/releases", farm.user_key)
    assert index["current"]["commit"] == commit and index["releases"][0]["note"] == "deploy"
    request = urllib.request.Request(f"{farm.url}/api/v1/releases/{commit}/bundle",
                                     headers={"Authorization": f"Bearer {farm.node_key}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.read() == body, "a node fetches it with its own key"
    status, _ = farm.call("POST", f"/api/v1/releases/{'e' * 40}/current", TOKEN)
    assert status == 404, "only a release the portal holds can be made current"


def test_a_node_installs_the_portals_release_once_idle_and_is_given_no_work_meanwhile(farm, tmp_path, monkeypatch):
    _, commit, body = _release(tmp_path)
    assert farm.call("POST", f"/api/v1/releases?commit={commit}", TOKEN, body)[0] == 201
    releases = tmp_path / "update"
    farm.start_agent(_canary_pipeline(farm.node), releases=releases)

    request = _wait(lambda: (releases / "request.json").is_file()
                    and json.loads((releases / "request.json").read_text()), what="the node to stage the release")
    assert request["commit"] == commit and request["sha256"] == hashlib.sha256(body).hexdigest()
    assert Path(request["bundle"]) == releases / f"{commit}.bundle"
    assert Path(request["bundle"]).read_bytes() == body, "exactly the release the portal holds"
    _wait(lambda: any((worker.get("update") or {}).get("state") == "staged" for worker in farm.portal.workers_view()),
          what="the portal to hear the node is updating")

    # Work waits for it, and says why.
    job_id = farm.canary(farm.bundle())
    _wait(lambda: "node-a to update to" in (farm.portal.queue_state()["waiting"].get(job_id) or ""),
          what="the queue to say the node is updating")
    assert farm.portal.store.get(job_id)["status"] == "queued"
    assert not farm.node.store.get(job_id), "the node took no work while it had a release to install"

    # alteriom-hil-update installs it; the node restarts on the new commit.
    farm.agent.stop()
    time.sleep(1.5)  # the old agent's last long poll ends
    (releases / "request.json").unlink()
    (releases / "status.json").write_text(json.dumps({
        "state": "installed", "commit": commit, "detail": "deployed", "at": farm_node._utcnow(),
    }))
    monkeypatch.setattr(core_service, "service_version", lambda: {"version": "1.0.1", "short": commit[:7], "commit": commit})
    farm.start_agent(_canary_pipeline(farm.node), releases=releases)

    done = _wait(lambda: (job := farm.portal.store.get(job_id))["status"] == "passed" and job, what="the run, after the update")
    assert done["worker"] == "node-a"
    status, index = farm.call("GET", "/api/v1/releases", farm.user_key)
    worker = index["workers"][0]
    assert worker["commit"] == commit and worker["update"]["state"] == "installed"


def test_a_release_that_fails_to_install_does_not_keep_a_node_from_work(farm, tmp_path):
    _, commit, body = _release(tmp_path)
    assert farm.call("POST", f"/api/v1/releases?commit={commit}", TOKEN, body)[0] == 201
    releases = tmp_path / "update"
    farm.start_agent(_canary_pipeline(farm.node), releases=releases)
    _wait(lambda: (releases / "request.json").is_file(), what="the node to stage the release")
    (releases / "request.json").unlink()
    (releases / "status.json").write_text(json.dumps({
        "state": "failed", "commit": commit, "detail": "update-runner.sh failed: rig still busy", "at": farm_node._utcnow(),
    }))
    job_id = farm.canary(farm.bundle())
    done = _wait(lambda: (job := farm.portal.store.get(job_id))["status"] == "passed" and job, what="the run")
    assert done["worker"] == "node-a"
    # The run finishing and the node reporting how the install went are two
    # heartbeats, not one: read once the moment the run passed, the update
    # was still `staged` on a slow runner. Wait for the report.
    update = _wait(lambda: (item := next(worker for worker in farm.portal.workers_view() if worker["name"] == "node-a")["update"])
                   and item.get("state") == "failed" and item, what="the node to report the failed install")
    assert "rig still busy" in update["detail"], "the failure is the portal's to show"
    assert not (releases / "request.json").exists(), "and it is not tried again at once"


# ---- history: a farm that joins brings what it ran --------------------------------------

def _standalone_history(tmp_path: Path, agent: str) -> tuple[Path, dict]:
    """A standalone farm's state: a canary that flashed a bundle it reused,
    and a painlessMesh run whose build directory is its own."""
    state = tmp_path / "standalone"
    farm = farm_service.manager_for("portal")(
        REPO, state, tmp_path / "none.yaml", tmp_path / "none-map.yaml", Path(sys.executable), mode="standalone",
    )
    bundle_id = "a" * 32
    _bundle(farm.artifact_root, bundle_id, agent)
    ids = {}
    for label, profile in (("canary", "canary"), ("mesh", "painlessmesh")):
        job = farm.store.create("suite", {"profile": profile, "resolved_sha": SHA}, state / "logs" / "x.log",
                                [{"name": "test", "status": "passed"}])
        runs = state / "runs" / job["id"]
        (runs / "serial").mkdir(parents=True)
        (runs / "serial" / "b-esp32-01.serial.log").write_text(f"{label} boot ok\n")
        (runs / "results.xml").write_text("<testsuite/>")
        (state / "logs").mkdir(exist_ok=True)
        (state / "logs" / f"{job['id']}.log").write_text(f"{label} log\n")
        farm.store.update(job["id"], "passed", {"summary": f"{label} passed", "results": str(runs / "results.xml"),
                                                "manifest": str(farm.artifact_root / job["id"] / "manifest.json")})
        ids[label] = job["id"]
    os.symlink(farm.artifact_root / bundle_id, farm.artifact_root / ids["canary"], target_is_directory=True)
    _bundle(farm.artifact_root, ids["mesh"], agent)
    farm.store.record_board_verdict(ids["canary"], "b-esp32-01", "2026-09-10T00:00:00+00:00", "healthy", "passed", [])
    farm.store.create("suite", {"profile": "canary"}, state / "logs" / "q.log")  # still queued: not history
    return state, {**ids, "bundle": bundle_id}


def test_a_farm_that_joins_brings_its_finished_jobs_evidence_logs_and_bundles(farm, tmp_path):
    state, ids = _standalone_history(tmp_path, farm.node.agent_source_sha())
    farm.start_agent(_canary_pipeline(farm.node))
    client = farm_node.PortalClient(farm.url, farm.node_key)
    lines = []
    assert farm_node.export_history(state, client, "node-a", out=lines.append) == 0, lines
    assert lines[0].startswith("2 finished jobs, 0 already on the portal, 2 to bring; 2 bundles")

    portal = farm.portal
    for label in ("canary", "mesh"):
        job = portal.store.get(ids[label])
        assert job["status"] == "passed" and job["worker"] == "node-a"
        assert job["request"]["imported_from"] == "node-a"
        runs = portal.state / "runs" / ids[label]
        assert job["result"]["results"] == str(runs / "results.xml"), "paths point at what arrived"
        assert (runs / "serial" / "b-esp32-01.serial.log").read_text() == f"{label} boot ok\n"
        assert (portal.state / "logs" / f"{ids[label]}.log").read_text() == f"{label} log\n"
        assert job["result"]["manifest"] == str(portal.artifact_root / ids[label] / "manifest.json")
    assert (portal.artifact_root / ids["canary"]).is_symlink(), "the reused bundle is linked as it was"
    assert os.path.realpath(portal.artifact_root / ids["canary"]) == os.path.realpath(portal.artifact_root / ids["bundle"])
    assert portal.store.board_verdicts("b-esp32-01")[0]["job_id"] == ids["canary"]
    # A bundle brought is history: never chosen to flash a new run.
    assert portal.imported_artifact(ids["bundle"]) and portal.imported_artifact(ids["mesh"])
    assert portal.reusable_artifacts(SHA, ["esp32"], profile="painlessmesh") is None

    # Again: nothing is brought twice.
    lines.clear()
    assert farm_node.export_history(state, client, "node-a", out=lines.append) == 0
    assert lines[0].startswith("2 finished jobs, 2 already on the portal, 0 to bring; 2 bundles and build directories, 0 to bring")


def test_a_node_brings_only_its_own_history_and_never_rewrites_the_portals(farm, tmp_path):
    farm.start_agent(_canary_pipeline(farm.node))
    job_id = farm.canary(farm.bundle())
    _wait(lambda: farm.portal.store.get(job_id)["status"] == "passed", what="a run of the portal's own")
    evidence = farm_node.pack_directory(tmp_path, "evidence")
    status, answer = farm.call("POST", f"/api/v1/workers/node-a/history/jobs/{job_id}/evidence", farm.node_key, evidence)
    assert status == 403 and "not rewritten" in answer["error"]
    status, _ = farm.call("POST", f"/api/v1/workers/node-b/history/known", farm.node_key, b'{"jobs": []}')
    assert status == 403, "a node speaks only for itself"
    status, _ = farm.call("POST", f"/api/v1/workers/node-a/history/known", farm.user_key, b'{"jobs": []}')
    assert status == 403, "and a person cannot pose as one"
    status, answer = farm.call("POST", f"/api/v1/workers/node-a/history/jobs/{'f' * 32}", farm.node_key,
                               json.dumps({"job": {"id": "f" * 32, "kind": "suite", "status": "running",
                                                   "created_at": "2026-09-10T00:00:00+00:00", "request": {}}}).encode())
    assert status == 400 and "only a finished job" in answer["error"]


# ---- what the portal's page is built from --------------------------------------------------

def test_a_node_reports_its_host_configuration_and_the_portal_shows_it_whole(farm):
    farm.node.configuration = lambda: {
        "version": {"version": "1.0.275"}, "host": {"hostname": "esp32-hil"},
        "gateway": {"enabled": True, "ssid": "Alteriom-HIL", "password_file": "/etc/alteriom-hil/gateway-wifi-password"},
        "build": {"profiles": ["canary"]},
    }
    farm.start_agent(_canary_pipeline(farm.node))
    detail = _wait(lambda: (item := farm.portal.worker_detail("node-a")).get("config") and item,
                   what="the node's configuration")
    assert detail["config"]["gateway"]["ssid"] == "Alteriom-HIL"
    assert "build" not in detail["config"], "profiles are the portal's to describe"
    assert detail["config_at"] and detail["online"]
    # And what that configuration means the rig can do, beside it: the rig's
    # page answers "why did the uplink rows skip" without a login.
    setup = {row["key"]: row for row in detail["setup"]}
    assert setup["network.ap"]["state"] in ("on", "broken"), setup["network.ap"]
    assert "Alteriom-HIL" in " ".join(setup["network.ap"]["details"])
    assert setup["provider.callmebot"]["state"] == "off"
    status, answer = farm.call("GET", "/api/v1/workers/node-a", farm.user_key)
    assert status == 200 and answer["config"]["host"]["hostname"] == "esp32-hil"
    assert farm.call("GET", "/api/v1/workers/nobody", farm.user_key)[0] == 404


def test_a_spent_callmebot_message_reaches_the_portals_used_today_at_once(farm, tmp_path, monkeypatch):
    """The report is periodic (every CONFIG_REPORT_SECONDS). A test message or
    a run changes "Used today" at once, and the portal's card sat on the old
    count for up to ten minutes (2026-09-15)."""
    budget = tmp_path / "callmebot-budget.json"
    monkeypatch.setenv("ALTERIOM_HIL_CALLMEBOT_BUDGET_FILE", str(budget))
    used = {"today": 0}
    farm.node.configuration = lambda: {"callmebot": {"url_file": "/etc/alteriom-hil/providers/callmebot-url",
                                                    "send": "release", "max_per_day": 25,
                                                    "used_today": used["today"]}}
    farm.start_agent(_canary_pipeline(farm.node))
    _wait(lambda: (farm.portal.worker_detail("node-a").get("config") or {}).get("callmebot"),
          what="the first configuration report")

    # A message spent on this rig: the budget file changes.
    used["today"] = 1
    budget.write_text('{"date": "2026-09-15", "used": 1}', encoding="utf-8")
    _wait(lambda: farm.portal.worker_detail("node-a")["config"]["callmebot"]["used_today"] == 1,
          what="the new count, well before the periodic report", timeout=5)

    # A control command finished (e.g. a test message that refused): reported too.
    used["today"] = 2
    farm.agent.report_config_soon()
    _wait(lambda: farm.portal.worker_detail("node-a")["config"]["callmebot"]["used_today"] == 2,
          what="the count after a command", timeout=5)
    assert farm_node.CONFIG_REPORT_SECONDS >= 60, "not simply a shorter period"


def test_a_workers_routine_calls_are_not_audited_and_old_ones_are_not_shown(farm):
    farm.start_agent(_canary_pipeline(farm.node))
    job_id = farm.canary(farm.bundle())
    _wait(lambda: farm.portal.store.get(job_id)["status"] == "passed", what="a run")
    _wait(lambda: not list(farm.agent.outbox.glob("*.json")), what="the report")
    with farm.portal.store.connect() as db:
        recorded = [row[0] for row in db.execute("SELECT path FROM audit WHERE role='node'")]
    assert recorded and all(path.endswith(("/evidence", "/result")) for path in recorded), recorded
    # Rows an older portal wrote are passed over, not deleted.
    farm.portal.store.record_audit("node-a", "node", "POST", "/api/v1/workers/node-a/heartbeat", 200)
    farm.portal.store.record_audit("sparck", "admin", "POST", "/api/v1/queue/pause", 200)
    page = farm.portal.store.audit_page(limit=500)
    assert not [entry for entry in page["entries"] if entry["path"].endswith("/heartbeat")]
    assert page["entries"][0]["path"] == "/api/v1/queue/pause" and page["total"] == len(recorded) + 1


def test_a_portals_health_is_its_nodes(farm, tmp_path):
    assert farm.portal.portal_health()["status"] == "unhealthy", "no node: nothing can run"
    farm.start_agent(_canary_pipeline(farm.node))
    _wait(lambda: farm.portal.portal_health()["status"] == "ok", what="a healthy node")
    status, answer = farm.call("GET", "/api/v1/status", farm.user_key)
    assert answer["mode"] == "portal" and answer["health"]["status"] == "ok"
    assert [worker["name"] for worker in answer["workers"]] == ["node-a"]
    _, commit, body = _release(tmp_path)
    farm.portal.publish_release(commit, body, "ci")
    # The node (no release updates here) runs another commit and is not on its way.
    farm.agent.releases = None
    health = _wait(lambda: (h := farm.portal.portal_health())["status"] == "degraded" and h, what="a node behind")
    assert "not the current release" in health["checks"][0]["message"]
    assert farm.call("GET", "/api/v1/status", farm.user_key)[1]["release"]["commit"] == commit


def test_the_portal_lists_who_holds_a_key_and_never_a_key(farm):
    status, answer = farm.call("GET", "/api/v1/keys", TOKEN)
    assert status == 200
    assert {(key["name"], key["role"]) for key in answer["keys"]} == {("node-a", "node"), ("node-b", "node"), ("sparck", "user")}
    assert not any("sha256" in key for key in answer["keys"])
    assert farm.call("GET", "/api/v1/keys", farm.user_key)[0] == 403


def test_a_portal_says_what_it_is_configured_with_even_with_no_file(farm, monkeypatch):
    from alteriom_hil import hil_config

    monkeypatch.setattr(hil_config, "CONFIG_PATH", Path("/nonexistent/config.yaml"))
    monkeypatch.setattr(hil_config, "load_config", lambda path=Path("/nonexistent/config.yaml"): (_ for _ in ()).throw(hil_config.ConfigError("no file")))
    config = farm.portal.configuration()
    assert config["portal"]["config_file"] is None
    assert config["portal"]["worker_stale_seconds"] == portal_half.WORKER_STALE_SECONDS
    # What it acts on: off, and said so, rather than nothing.
    assert config["quarantine"]["enabled"] is False and config["retention"]["enabled"] is False
    assert farm.node.configuration()["portal"] is None


def test_a_node_newer_than_its_portal_still_joins_without_what_the_portal_does_not_know(farm, monkeypatch):
    """A node installs a release before the portal's image catches up. A
    portal that refused the node's new hello field left the farm with no
    worker until someone updated the portal (2026-09-14)."""
    real_hello = farm.portal.worker_hello

    def older_portal(name, payload, address=None):
        unknown = sorted(set(payload) - {"kind", "version", "max_runs", "profiles", "inventory", "health"})
        if unknown:
            raise ValueError(f"unknown hello fields: {unknown}")
        return real_hello(name, payload, address)

    monkeypatch.setattr(farm.portal, "worker_hello", older_portal)
    farm.start_agent(_canary_pipeline(farm.node))
    _wait(lambda: farm.portal.online_workers(), what="the node to join an older portal")
    assert farm.agent._unsupported <= {"commit", "update", "config"} and "commit" in farm.agent._unsupported
    job_id = farm.canary(farm.bundle())
    assert _wait(lambda: farm.portal.store.get(job_id)["status"] == "passed", what="a run on it")
    # A field the node cannot do without is not quietly dropped.
    assert not farm.agent._drop_unknown("unknown hello fields: ['inventory']")


# ---- managing a rig from the portal ------------------------------------------------------

def _ask(farm, name, kind, args=None, key=TOKEN):
    return farm.call("POST", f"/api/v1/workers/{name}/commands", key,
                     json.dumps({"kind": kind, "args": args or {}}).encode())


def test_a_command_given_on_the_portal_is_carried_out_on_the_node_and_reported(farm):
    farm.node.device_details = lambda board: {"id": board, "description": "ESP32-D0WD-V3", "revision": "v3.1"}
    farm.start_agent(_canary_pipeline(farm.node))
    status, command = _ask(farm, "node-a", "read_details", {"board": "b-esp32-01"})
    assert status == 202 and command["status"] == "queued" and command["requested_by"] == "farm"
    done = _wait(lambda: (item := farm.portal.store.command(command["id"]))["status"] == "done" and item,
                 what="the node to carry it out")
    assert done["result"]["description"] == "ESP32-D0WD-V3" and done["sent_at"] and done["finished_at"]
    status, rediscover = _ask(farm, "node-a", "rediscover")
    finished = _wait(lambda: (item := farm.portal.store.command(rediscover["id"]))["status"] in ("done", "failed") and item,
                     what="rediscovery")
    assert finished["status"] == "done" and farm.node.store.get(finished["result"]["job"])["kind"] == "inventory"
    listed = farm.call("GET", "/api/v1/workers/node-a/commands", farm.user_key)[1]["commands"]
    assert [item["kind"] for item in listed[:2]] == ["rediscover", "read_details"]
    assert farm.portal.worker_detail("node-a")["commands"][0]["id"] == rediscover["id"]


def test_only_an_admin_gives_commands_and_only_those_the_node_knows(farm):
    farm.start_agent(_canary_pipeline(farm.node))
    assert _ask(farm, "node-a", "rediscover", key=farm.user_key)[0] == 403
    assert _ask(farm, "node-a", "rediscover", key=farm.node_key)[0] == 403
    assert _ask(farm, "node-a", "format_disk")[0] == 400
    assert _ask(farm, "node-a", "read_details", {})[0] == 400
    assert _ask(farm, "node-a", "logs", {"lines": 1_000_000})[0] == 400
    status, answer = _ask(farm, "node-a", "configure", {"settings": {"farm.portal_url": "https://evil.example"}})
    assert status == 400 and "cannot be changed remotely" in answer["error"]
    assert _ask(farm, "node-a", "configure", {"settings": {"queue.concurrency": 2}})[0] == 202
    assert _ask(farm, "nobody", "rediscover")[0] == 404
    # A node reports only on its own commands.
    command = farm.portal.store.add_command("node-b", "rediscover", {}, "farm")
    status, _ = farm.call("POST", f"/api/v1/workers/node-a/commands/{command['id']}", farm.node_key, b'{"status": "done"}')
    assert status == 404


def test_what_needs_the_hosts_sudo_goes_through_the_control_unit_and_its_result_comes_back(farm, tmp_path):
    releases = tmp_path / "update"
    farm.start_agent(_canary_pipeline(farm.node), releases=releases)
    status, command = _ask(farm, "node-a", "restart")
    request = _wait(lambda: (releases / "control.json").is_file() and json.loads((releases / "control.json").read_text()),
                    what="the control request")
    assert request["id"] == command["id"] and request["action"] == "restart"
    # The rig has it -- it says so as soon as it begins -- and it is not done
    # until the control unit reports the outcome.
    assert farm.portal.store.command(command["id"])["status"] in ("sent", "running"),         "not done until the control unit says so"
    # alteriom-hil-control carries it out and leaves the outcome.
    (releases / "control.json").unlink()
    (releases / f"control-result-{command['id']}.json").write_text(json.dumps(
        {"id": command["id"], "status": "done", "detail": "restarting alteriom-hil-farm"}))
    done = _wait(lambda: (item := farm.portal.store.command(command["id"]))["status"] == "done" and item, what="the result")
    assert done["detail"] == "restarting alteriom-hil-farm"
    # Taken once the portal has it -- a moment after the portal records it.
    _wait(lambda: not (releases / f"control-result-{command['id']}.json").exists(), what="the result file to be taken")


def test_a_drained_rig_is_given_no_new_run_until_it_is_resumed(farm):
    farm.start_agent(_canary_pipeline(farm.node))
    status, worker = farm.call("POST", "/api/v1/workers/node-a/drain", TOKEN, b'{"reason": "moving the hub"}')
    assert status == 200 and worker["drained"]["reason"] == "moving the hub" and worker["drained"]["by"] == "farm"
    job_id = farm.canary(farm.bundle())
    _wait(lambda: "node-a, drained" in (farm.portal.queue_state()["waiting"].get(job_id) or ""), what="the reason")
    assert farm.portal.store.get(job_id)["status"] == "queued"
    health = farm.portal.portal_health()
    assert health["status"] == "ok" and "drained by farm: moving the hub" in health["checks"][0]["message"]
    assert farm.call("POST", "/api/v1/workers/node-a/resume", TOKEN, b"{}")[0] == 200
    assert _wait(lambda: farm.portal.store.get(job_id)["status"] == "passed", what="the run after resuming")


def test_a_rig_is_removed_only_drained_and_idle_and_its_key_goes_with_it(farm):
    farm.start_agent(_canary_pipeline(farm.node))
    status, answer = farm.call("DELETE", "/api/v1/workers/node-a", TOKEN)
    assert status == 409 and "drain it first" in answer["error"]
    farm.call("POST", "/api/v1/workers/node-a/drain", TOKEN, b"{}")
    farm.agent.stop()
    status, answer = farm.call("DELETE", "/api/v1/workers/node-a", TOKEN)
    assert status == 200 and answer == {"removed": "node-a", "key_revoked": True}
    assert not any(worker["name"] == "node-a" for worker in farm.portal.workers_view())
    status, _ = farm.call("POST", "/api/v1/workers/node-a/hello", farm.node_key, b"{}")
    assert status == 401, "the removed rig's key is refused"


def test_a_command_nobody_reports_on_expires(farm):
    farm.start_agent(_canary_pipeline(farm.node))
    command = farm.portal.store.add_command("node-a", "restart", {}, "farm")
    with farm.portal.store.connect() as db:
        db.execute("UPDATE worker_commands SET status='sent', created_at='2026-01-01T00:00:00+00:00' WHERE id=?", (command["id"],))
    farm.portal._sweep_workers()
    assert farm.portal.store.command(command["id"])["status"] == "expired"


def test_a_node_says_its_release_as_a_version_and_its_commit_beside_it(farm, monkeypatch):
    monkeypatch.setattr(core_service, "service_version",
                        lambda: {"version": "1.0.277", "short": "3c9b46429", "commit": "3c9b" + "0" * 36})
    farm.start_agent(_canary_pipeline(farm.node))
    worker = _wait(lambda: next((item for item in farm.portal.workers_view() if item["name"] == "node-a"), None),
                   what="the hello")
    assert worker["version"] == "1.0.277" and worker["commit"].startswith("3c9b")


# ---- a provider's link, sealed on the rig's page ---------------------------------------------------
# The portal relays a ciphertext it cannot read, to the key the rig reported,
# and keeps nothing of it once the rig has it (docs/providers.md).


def _der(tag: int, content: bytes) -> bytes:
    length = len(content)
    encoded = bytes([length]) if length < 0x80 else bytes([0x80 | 2]) + length.to_bytes(2, "big")
    return bytes([tag]) + encoded + content


def _seal_key_pem() -> tuple[str, str]:
    """An RSA-3072 SubjectPublicKeyInfo -- shaped like one, which is all the
    portal and the node's report look at -- and its fingerprint."""
    import base64

    modulus = b"\x00\xc1" + bytes(range(256)) + bytes(127)  # 384 bytes after the sign byte: 3072 bits
    key = _der(0x30, _der(0x02, modulus) + _der(0x02, b"\x01\x00\x01"))
    algorithm = _der(0x30, _der(0x06, bytes.fromhex("2a864886f70d010101")) + b"\x05\x00")
    spki = _der(0x30, algorithm + _der(0x03, b"\x00" + key))
    body = base64.b64encode(spki).decode()
    pem = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64)) + "\n-----END PUBLIC KEY-----\n"
    return pem, hashlib.sha256(spki).hexdigest()


def _sealed(fill: int = 7) -> str:
    import base64

    return base64.b64encode(bytes([fill]) * 384).decode()


def test_a_sealed_link_is_relayed_to_the_key_the_rig_reported_and_never_returned(farm, tmp_path, monkeypatch):
    pem, fingerprint = _seal_key_pem()
    (tmp_path / "provider-seal.pub").write_text(pem)
    monkeypatch.setenv("ALTERIOM_HIL_PROVIDER_SEAL_PUB", str(tmp_path / "provider-seal.pub"))
    releases = tmp_path / "update"
    farm.start_agent(_canary_pipeline(farm.node), releases=releases)
    # The node reports its public key with its configuration; a person sees it on the rig's page.
    rig = farm.call("GET", "/api/v1/rigs/node-a", farm.user_key)[1]
    assert rig["config"]["seal_key"]["fingerprint"] == fingerprint and rig["config"]["seal_key"]["spki"]

    good = {"provider": "callmebot", "sealed": _sealed(), "fingerprint": fingerprint}
    for args, code, says in (
        ({**good, "provider": "telegram"}, 400, "provider must be one of callmebot"),
        ({**good, "sealed": _sealed()[:-8]}, 400, "384-byte RSA-OAEP ciphertext"),
        ({**good, "sealed": "!" * 512}, 400, "384-byte RSA-OAEP ciphertext"),
        ({**good, "sealed": __import__("base64").b64encode(b"x" * 385).decode()}, 400, "384-byte"),
        ({**good, "fingerprint": fingerprint.upper()}, 400, "64 lowercase hex"),
        ({**good, "fingerprint": "0" * 64}, 409, "sealed for a different key; reload the page"),
        ({"provider": "callmebot", "sealed": _sealed()}, 400, "provider_set needs fingerprint"),
        ({**good, "text": "hi"}, 400, "provider_set takes no text"),
    ):
        status, answer = _ask(farm, "node-a", "provider_set", args)
        assert status == code and says in answer["error"], (args, status, answer)
    assert _ask(farm, "node-a", "provider_set", good, key=farm.user_key)[0] == 403, "an admin's to give"

    status, command = _ask(farm, "node-a", "provider_set", good)
    assert status == 202 and command["args"] == {"provider": "callmebot", "fingerprint": fingerprint, "sealed": "(sealed)"}
    # The rig gets the ciphertext, in a request only its user reads.
    request = _wait(lambda: (releases / "control.json").is_file() and json.loads((releases / "control.json").read_text()),
                    what="the control request")
    assert request["action"] == "provider_set" and request["args"]["sealed"] == good["sealed"]
    assert (releases / "control.json").stat().st_mode & 0o777 == 0o600
    # Delivered, the portal keeps no copy, and never showed one.
    with farm.portal.store.connect() as db:
        stored = json.loads(db.execute("SELECT args_json FROM worker_commands WHERE id=?", (command["id"],)).fetchone()[0])
    assert stored["sealed"] == "(sealed)"
    assert farm.portal.store.command(command["id"])["status"] in ("sent", "running")
    for path, key in (("/api/v1/workers/node-a/commands", farm.user_key), ("/api/v1/rigs/node-a", farm.user_key),
                      ("/api/v1/workers/node-a/commands?limit=50", TOKEN)):
        text = json.dumps(farm.call("GET", path, key)[1])
        assert good["sealed"] not in text and good["sealed"][:40] not in text, path
    (releases / "control.json").unlink()
    (releases / f"control-result-{command['id']}.json").write_text(json.dumps(
        {"id": command["id"], "status": "done",
         "detail": "stored https://api.callmebot.com/whatsapp.php?phone=***34&apikey=*** in /etc/alteriom-hil/providers/callmebot-url; restarting alteriom-hil-farm"}))
    done = _wait(lambda: (item := farm.portal.store.command(command["id"]))["status"] == "done" and item, what="the result")
    assert "phone=***34" in done["detail"]

    status, removed = _ask(farm, "node-a", "provider_remove", {"provider": "callmebot"})
    assert status == 202 and removed["args"] == {"provider": "callmebot"}
    assert _ask(farm, "node-a", "provider_remove", {"provider": "callmebot", "sealed": "x"})[0] == 400


def test_a_test_message_is_an_admins_to_ask_for_and_reaches_the_control_unit(farm, tmp_path):
    releases = tmp_path / "update"
    farm.start_agent(_canary_pipeline(farm.node), releases=releases)
    for args, code, says in (
        ({"provider": "telegram"}, 400, "provider must be one of callmebot"),
        ({}, 400, "provider_test needs provider"),
        ({"provider": "callmebot", "text": "hi"}, 400, "provider_test takes no text"),
    ):
        status, answer = _ask(farm, "node-a", "provider_test", args)
        assert status == code and says in answer["error"], (args, status, answer)
    assert _ask(farm, "node-a", "provider_test", {"provider": "callmebot"}, key=farm.user_key)[0] == 403

    status, command = _ask(farm, "node-a", "provider_test", {"provider": "callmebot"})
    assert status == 202 and command["kind"] == "provider_test" and command["args"] == {"provider": "callmebot"}
    # Given to the node's control unit, which runs `providers test` with the host's sudo.
    request = _wait(lambda: (releases / "control.json").is_file() and json.loads((releases / "control.json").read_text()),
                    what="the control request")
    assert request["action"] == "provider_test" and request["args"] == {"provider": "callmebot"}
    (releases / "control.json").unlink()
    (releases / f"control-result-{command['id']}.json").write_text(json.dumps(
        {"id": command["id"], "status": "done", "detail": "test message queued by CallMeBot (HTTP 200)"}))
    done = _wait(lambda: (item := farm.portal.store.command(command["id"]))["status"] == "done" and item, what="the result")
    assert done["detail"] == "test message queued by CallMeBot (HTTP 200)"
    rig = farm.call("GET", "/api/v1/rigs/node-a", farm.user_key)[1]
    assert any(item["kind"] == "provider_test" and item["status"] == "done" for item in rig["commands"]), \
        "the rig's page shows the last test and how it went"


def test_the_portal_forgets_a_sealed_link_it_never_delivered_and_refuses_one_under_a_run(farm, monkeypatch):
    pem, fingerprint = _seal_key_pem()
    import base64 as _base64

    der = _base64.b64decode("".join(pem.splitlines()[1:-1]))
    seal_key = {"spki": _base64.b64encode(der).decode(), "fingerprint": fingerprint}
    farm.portal.worker_hello("node-a", {"config": {"seal_key": seal_key}})
    good = {"provider": "callmebot", "sealed": _sealed(9), "fingerprint": fingerprint}
    store = farm.portal.store

    def raw(command_id):
        with store.connect() as db:
            return json.loads(db.execute("SELECT args_json FROM worker_commands WHERE id=?", (command_id,)).fetchone()[0])

    queued = farm.portal.request_command("node-a", "provider_set", good, "farm")
    assert queued["args"]["sealed"] == "(sealed)" and raw(queued["id"])["sealed"] == good["sealed"], "kept until delivered"
    assert all(item["args"].get("sealed") in (None, "(sealed)") for item in store.recent_commands("node-a"))
    # Expired without ever being delivered: gone too.
    with store.connect() as db:
        db.execute("UPDATE worker_commands SET created_at='2026-01-01T00:00:00+00:00' WHERE id=?", (queued["id"],))
    assert store.expire_commands("2026-06-01T00:00:00+00:00") == 1
    assert raw(queued["id"])["sealed"] == "(sealed)"
    # Delivered in a heartbeat answer: that answer is the only place it is whole.
    second = farm.portal.request_command("node-a", "provider_set", good, "farm")
    given = [item for item in store.take_commands("node-a") if item["id"] == second["id"]]
    assert given[0]["args"]["sealed"] == good["sealed"] and raw(second["id"])["sealed"] == "(sealed)"
    # A command finished before it was sent (a worker deleted, say) keeps nothing either.
    third = farm.portal.request_command("node-a", "provider_set", good, "farm")
    store.finish_command(third["id"], "failed", None, "given up")
    assert raw(third["id"])["sealed"] == "(sealed)"

    # Storing a link restarts the node's service, which would end a run.
    monkeypatch.setattr(store, "running_on", lambda worker: [{"id": "j" * 32}])
    for kind, args in (("provider_set", good), ("provider_remove", {"provider": "callmebot"})):
        with pytest.raises(farm_service.ElsewhereError, match="drain it and let the run end first"):
            farm.portal.request_command("node-a", kind, args, "farm")
    # A test message is refused under a run too: it takes from the budget the
    # run's CallMeBot row may be about to spend.
    with pytest.raises(farm_service.ElsewhereError, match="drain it and let the run end first"):
        farm.portal.request_command("node-a", "provider_test", {"provider": "callmebot"}, "farm")
    # A rig that reported no key cannot be sent one.
    monkeypatch.setattr(store, "running_on", lambda worker: [])
    farm.portal.worker_heartbeat("node-a", {"config": {"callmebot": {}}})
    with pytest.raises(farm_service.ElsewhereError, match="has not reported a seal key"):
        farm.portal.request_command("node-a", "provider_set", good, "farm")



def test_a_rig_is_not_restarted_under_a_run_and_its_page_lists_its_own_runs_and_boards(farm):
    gate = threading.Event()
    farm.start_agent(_canary_pipeline(farm.node, gate))
    job_id = farm.canary(farm.bundle())
    _wait(lambda: (farm.node.store.get(job_id) or {}).get("status") == "running", what="the run")
    # Restarting the service -- or configuring, which restarts it -- would end the run.
    for kind, args in (("restart", {}), ("configure", {"settings": {"queue.concurrency": 2}})):
        status, answer = _ask(farm, "node-a", kind, args)
        assert status == 409 and "drain it and let the run end first" in answer["error"], kind
    assert _ask(farm, "node-a", "logs", {"lines": 50})[0] == 202, "reading its logs ends nothing"
    gate.set()
    _wait(lambda: farm.portal.store.get(job_id)["status"] == "passed", what="the verdict")
    _wait(lambda: not farm.portal.running_job_ids(), what="the portal to release the run")
    assert _ask(farm, "node-a", "restart")[0] == 202

    status, page = farm.call("GET", "/api/v1/jobs?worker=node-a", farm.user_key)
    assert status == 200 and job_id in [job["id"] for job in page["jobs"]]
    assert farm.call("GET", "/api/v1/jobs?worker=node-b", farm.user_key)[1]["total"] == 0
    detail = farm.portal.worker_detail("node-a")
    assert set(detail["inventory"]) == {"missing", "unregistered", "probe_errors", "instruments", "missing_instruments"}
    assert all(isinstance(items, list) for items in detail["inventory"].values())


# ---- adding a rig ---------------------------------------------------------------------------

def _enroll(farm, body: dict) -> tuple[int, dict]:
    """The public call: no key at all."""
    request = urllib.request.Request(farm.url + "/api/v1/enroll", data=json.dumps(body).encode(), method="POST",
                                      headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _rigs(farm, key=TOKEN) -> dict:
    return {rig["name"]: rig for rig in farm.call("GET", "/api/v1/rigs", key)[1]["rigs"]}


def test_a_rig_is_added_with_a_token_that_works_once_for_a_node_key_of_its_own(farm, tmp_path):
    body = json.dumps({"name": "rig-2", "description": "bench Pi 5", "location": "lab shelf 2"}).encode()
    assert farm.call("POST", "/api/v1/rigs", farm.user_key, body)[0] == 403
    status, added = farm.call("POST", "/api/v1/rigs", TOKEN, body)
    assert status == 201 and added["pending"] and added["join"]["status"] == "waiting" and added["join"]["created_by"] == "farm"
    assert added["description"] == "bench Pi 5" and added["location"] == "lab shelf 2"
    token = added["token"]
    assert farm_service.ENROLLMENT_TOKEN_PATTERN.fullmatch(token)
    listed = farm.call("GET", "/api/v1/rigs", farm.user_key)[1]
    assert [rig["name"] for rig in listed["rigs"] if rig.get("pending")] == ["rig-2"] and listed["release"] is None
    assert token not in json.dumps(listed) and "token_sha256" not in json.dumps(listed), "the token is shown once"
    # A name already a key's, the farm's own, or already being added.
    assert farm.call("POST", "/api/v1/rigs", TOKEN, b'{"name": "node-a"}')[0] == 409
    assert farm.call("POST", "/api/v1/rigs", TOKEN, b'{"name": "farm"}')[0] == 400
    assert farm.call("POST", "/api/v1/rigs", TOKEN, b'{"name": "rig-2"}')[0] == 409

    # No release for it to install: refused, and the token is not spent.
    status, answer = _enroll(farm, {"token": token, "hostname": "rig-2"})
    assert status == 409 and "no release" in answer["error"]
    _, commit, bundle = _release(tmp_path)
    assert farm.call("POST", f"/api/v1/releases?commit={commit}", TOKEN, bundle)[0] == 201

    assert _enroll(farm, {"token": "afj_" + "0" * 48})[0] == 403
    assert _enroll(farm, {"token": "not-a-token"})[0] == 403
    status, joined = _enroll(farm, {"token": token, "hostname": "raspberrypi"})
    assert status == 200 and joined["name"] == "rig-2" and joined["release"]["commit"] == commit
    key = joined["key"]
    status, answer = _enroll(farm, {"token": token, "hostname": "raspberrypi"})
    assert status == 403 and "used" in answer["error"], "once"
    assert farm.call("GET", "/api/v1/rigs/rig-2", TOKEN)[1]["join"]["status"] == "installing"
    assert farm.call("PATCH", "/api/v1/rigs/rig-2", TOKEN, b'{"name": "rig-9"}')[0] == 409, "its key is named for it"

    # A node key named for the rig: the release it is to run, and nothing a person reads.
    assert farm.call("GET", "/api/v1/releases/current", key)[1]["commit"] == commit
    assert farm.call("GET", "/api/v1/status", key)[0] == 403
    assert {"name": "rig-2", "role": "node"}.items() <= next(
        entry for entry in farm.call("GET", "/api/v1/keys", TOKEN)[1]["keys"] if entry["name"] == "rig-2").items()
    agent = farm_node.NodeAgent(farm.node, farm_node.PortalClient(farm.url, key), "rig-2",
                                heartbeat_seconds=0.2, lease_wait=1)
    agent.start()
    try:
        _wait(lambda: any(worker["name"] == "rig-2" for worker in farm.portal.online_workers()), what="rig-2's hello")
        rig = farm.call("GET", "/api/v1/rigs/rig-2", TOKEN)[1]
        assert not rig.get("pending") and rig["online"] and rig["description"] == "bench Pi 5", "the worker it became"
        assert _rigs(farm)["rig-2"]["location"] == "lab shelf 2"
    finally:
        agent.stop()
    audit = farm.portal.store.audit_page(50)["entries"]
    assert any(row["key_name"] == "rig-2" and row["role"] == "enroll" and row["status"] == 200 for row in audit)
    assert any(row["key_name"] == "unknown" and row["role"] == "enroll" and row["status"] == 403 for row in audit)
    assert token not in json.dumps(audit) and key not in json.dumps(audit)


def test_a_rig_is_edited_given_a_new_join_command_and_deleted_leaving_nothing(farm, tmp_path):
    _, commit, bundle = _release(tmp_path)
    farm.call("POST", f"/api/v1/releases?commit={commit}", TOKEN, bundle)
    added = farm.call("POST", "/api/v1/rigs", TOKEN, b'{"name": "rig-3"}')[1]
    # Still being added: renamed, described, placed.
    assert farm.call("PATCH", "/api/v1/rigs/rig-3", farm.user_key, b'{"location": "x"}')[0] == 403
    status, edited = farm.call("PATCH", "/api/v1/rigs/rig-3", TOKEN,
                               b'{"name": "bench-3", "description": "Pi 4, six C3s", "location": "desk"}')
    assert status == 200 and edited["name"] == "bench-3" and edited["description"] == "Pi 4, six C3s"
    assert "rig-3" not in _rigs(farm) and _rigs(farm)["bench-3"]["location"] == "desk"
    assert farm.call("PATCH", "/api/v1/rigs/bench-3", TOKEN, b'{"name": "node-a"}')[0] == 409
    assert farm.call("PATCH", "/api/v1/rigs/bench-3", TOKEN, b'{"colour": "red"}')[0] == 400
    assert farm.call("PATCH", "/api/v1/rigs/nobody", TOKEN, b'{"location": "x"}')[0] == 404

    # Its token ran out: a new join command, and the old token is no use.
    with farm.portal.store.connect() as db:
        db.execute("UPDATE enrollments SET expires_at='2026-01-01T00:00:00+00:00' WHERE name='bench-3'")
    assert _rigs(farm)["bench-3"]["join"]["status"] == "expired"
    status, answer = _enroll(farm, {"token": added["token"]})
    assert status == 403 and "expired" in answer["error"]
    status, renewed = farm.call("POST", "/api/v1/rigs/bench-3/join", TOKEN, b"{}")
    assert status == 200 and renewed["join"]["status"] == "waiting" and renewed["token"] != added["token"]
    assert _enroll(farm, {"token": added["token"]})[0] == 403
    # Taken and never joined: a new command revokes the key it took.
    key = _enroll(farm, {"token": renewed["token"], "hostname": "pi"})[1]["key"]
    again = farm.call("POST", "/api/v1/rigs/bench-3/join", TOKEN, b"{}")[1]
    assert again["key_revoked"] and farm.call("GET", "/api/v1/releases/current", key)[0] == 401
    assert _enroll(farm, {"token": again["token"], "hostname": "pi"})[0] == 200

    # Deleted before it joined: gone from the list, its key and its name free again.
    status, deleted = farm.call("DELETE", "/api/v1/rigs/bench-3", TOKEN)
    assert status == 200 and deleted["deleted"] == "bench-3" and deleted["key_revoked"]
    assert "bench-3" not in _rigs(farm) and farm.call("GET", "/api/v1/rigs/bench-3", TOKEN)[0] == 404
    with farm.portal.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM rig_details").fetchone()[0] == 0
    assert farm.call("POST", "/api/v1/rigs", TOKEN, b'{"name": "bench-3"}')[0] == 201

    # A rig that joined: edited, not renamed; deleted under the rules for removing a worker.
    farm.start_agent(_canary_pipeline(farm.node))
    assert farm.call("PATCH", "/api/v1/rigs/node-a", TOKEN, b'{"location": "rack 1"}')[1]["location"] == "rack 1"
    assert farm.call("PATCH", "/api/v1/rigs/node-a", TOKEN, b'{"name": "node-z"}')[0] == 409
    assert farm.call("POST", "/api/v1/rigs/node-a/join", TOKEN, b"{}")[0] == 409
    status, answer = farm.call("DELETE", "/api/v1/rigs/node-a", TOKEN)
    assert status == 409 and "drain it first" in answer["error"]
    farm.call("POST", "/api/v1/workers/node-a/drain", TOKEN, b"{}")
    farm.agent.stop()
    assert farm.call("DELETE", "/api/v1/rigs/node-a", TOKEN)[1]["key_revoked"] is True
    assert "node-a" not in _rigs(farm)


def test_a_portal_from_before_keeps_its_rigs_notes_and_forgets_the_rigs_it_removed(farm):
    """What a portal from before rigs were one resource left: each join's note
    (now the rig's description), and the used join of a rig removed with its
    key -- which is not a rig being added."""
    with farm.portal.store.connect() as db:
        db.execute("DROP TABLE rig_details")
        db.execute("INSERT INTO enrollments (id, name, token_sha256, note, created_by, created_at, expires_at, status) "
                   "VALUES ('b', 'bench', 'y', 'Pi 4 on the bench', 'farm', '2026-09-14T10:00:00+00:00', '2099-01-01', 'waiting')")
        db.execute("INSERT INTO enrollments (id, name, token_sha256, note, created_at, expires_at, status, used_at) "
                   "VALUES ('c', 'gone', 'z', 'the old rig', '2026-09-13T10:00:00+00:00', '2026-09-13', 'used', '2026-09-13T10:05:00+00:00')")
    farm_service.JobStore(farm.portal.store.path)  # the next start
    rigs = _rigs(farm)
    assert rigs["bench"]["description"] == "Pi 4 on the bench", "the note is its description"
    assert "gone" not in rigs, "removed with its key before rigs were deleted whole"
    assert farm.call("GET", "/api/v1/rigs/gone", TOKEN)[0] == 404
    assert farm.call("POST", "/api/v1/rigs", TOKEN, b'{"name": "gone"}')[0] == 201, "its name is free"
    # Changing only where it is keeps what it is.
    edited = farm.call("PATCH", "/api/v1/rigs/bench", TOKEN, b'{"location": "shelf 3"}')[1]
    assert edited["description"] == "Pi 4 on the bench" and edited["location"] == "shelf 3"


def test_a_cancelled_join_from_before_is_not_kept_and_too_many_wrong_tokens_are_turned_away(farm):
    with farm.portal.store.connect() as db:
        db.execute("INSERT INTO enrollments (id, name, token_sha256, created_at, expires_at, status) "
                   "VALUES ('a', 'rig-2', 'x', '2026-09-14', '2026-09-14', 'cancelled')")
    farm_service.JobStore(farm.portal.store.path)  # the next start
    assert "rig-2" not in _rigs(farm)
    for _ in range(farm_service.ENROLL_FAILURE_LIMIT):
        _enroll(farm, {"token": "afj_" + "1" * 48})
    assert _enroll(farm, {"token": "afj_" + "1" * 48})[0] == 429


def _release_with_agent(tmp_path: Path, source: str, profiles: dict | None = None) -> tuple[Path, str, bytes]:
    """A release whose HIL agent source is `source`, with an ignored build tree.

    `profiles` is profile name to the `agent.source_path` it declares (None
    for a profile with no agent); without it the release has no profiles at
    all, as one published before profiles named their agents."""
    repo = tmp_path / "farm-agent-src"
    firmware = repo / "suites" / "painlessmesh" / "firmware"
    (firmware / "src").mkdir(parents=True)
    (firmware / "platformio.ini").write_text("[env:esp32]\n")
    (firmware / "src" / "main.cpp").write_text(source)
    for name, source_path in (profiles or {}).items():
        (repo / "profiles").mkdir(exist_ok=True)
        doc = {"schema": 1, "name": name}
        if source_path:
            doc["agent"] = {"source_path": source_path}
        (repo / "profiles" / f"{name}.yaml").write_text(yaml.safe_dump(doc))
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "agent")
    (firmware / ".pio" / "build").mkdir(parents=True)
    (firmware / ".pio" / "build" / "firmware.bin").write_bytes(b"\xe9 not source")
    _git(repo, "bundle", "create", str(tmp_path / "agent.bundle"), "HEAD")
    return repo, _git(repo, "rev-parse", "HEAD"), (tmp_path / "agent.bundle").read_bytes()


def test_a_release_carries_the_agent_digest_a_build_of_it_stamps(tmp_path):
    repo, commit, _ = _release_with_agent(tmp_path, "void setup() { release(); }\n")
    # The digest a checkout of the same commit gives -- build_artifacts.py's.
    checkout = farm_service.FarmManager.__new__(farm_service.FarmManager)
    checkout.repo = repo
    # Where the agent is, is a profile's to say; this release carries none,
    # so the checkout is told by the farm's own.
    checkout._profiles = farm_service.load_profiles(REPO)
    assert farm_service.release_agent_sha(tmp_path / "agent.bundle", commit) == checkout.agent_source_sha()
    # A release with no agent source has no agent to hold bundles to.
    bare_root = tmp_path / "bare"
    bare_root.mkdir()
    _, bare_commit, _ = _release(bare_root)
    assert farm_service.release_agent_sha(bare_root / "release.bundle", bare_commit) is None


def test_a_portal_holds_bundles_to_its_releases_agent_not_its_own_image(farm, tmp_path):
    """2026-09-14: an agent change reached the rigs as a release, and the
    portal -- still on the image before it -- refused every bundle built for
    that release until its image was rolled out by hand."""
    own = farm.portal.agent_source_sha()
    assert farm.portal.expected_agent_sha() == own, "no release yet: its own checkout"

    _, commit, body = _release_with_agent(tmp_path, "void setup() { newer(); }\n")
    farm.portal.publish_release(commit, body, "ci")
    released = farm.portal.current_release()["hil_agent_sha"]
    assert released and released != own
    assert farm.portal.expected_agent_sha() == released

    spec = farm.portal.profiles["painlessmesh"]
    for_release = uuid.uuid4().hex
    _bundle(farm.portal.artifact_root, for_release, released)
    farm.portal._check_supplied_bundle(for_release, spec, None, ["esp32"])
    for_image = uuid.uuid4().hex
    _bundle(farm.portal.artifact_root, for_image, own)
    with pytest.raises(ValueError, match="different HIL agent"):
        farm.portal._check_supplied_bundle(for_image, spec, None, ["esp32"])
    # This release is from before profiles named their agents: it says one
    # digest, and that is painlessMesh's -- never the health check's, which
    # has no agent and whose bundles are held to none.
    assert farm.portal.current_release()["agents"] == {}
    assert farm.portal.expected_agent_sha("canary") is None
    farm.portal._check_supplied_bundle(for_image, farm.portal.profiles["canary"], None, ["esp32"])

    # A node still judges by what it has installed.
    assert farm.node.expected_agent_sha() == farm.node.agent_source_sha()


def test_a_release_published_before_agents_were_recorded_gets_one_when_read(farm, tmp_path):
    _, commit, body = _release_with_agent(tmp_path, "void setup() { older(); }\n")
    farm.portal.publish_release(commit, body, "ci")
    record = farm.portal.release_root / f"{commit}.json"
    payload = json.loads(record.read_text())
    expected = payload.pop("hil_agent_sha")
    record.write_text(json.dumps(payload))
    assert farm.portal.current_release()["hil_agent_sha"] == expected
    assert json.loads(record.read_text())["hil_agent_sha"] == expected


def test_a_release_is_numbered_as_the_rig_that_installs_it_will_say(farm, tmp_path):
    _, commit, bundle = _release(tmp_path, commits=3)
    published = farm.call("POST", f"/api/v1/releases?commit={commit}", TOKEN, bundle)[1]
    assert published["version"] == "1.2.3", "VERSION at the commit, then the commits to it"
    assert farm.portal.current_release()["version"] == "1.2.3"
    assert farm.call("GET", "/api/v1/releases", farm.user_key)[1]["releases"][0]["version"] == "1.2.3"
    # One published before releases were numbered is numbered when next read.
    record = farm.portal.release_root / f"{commit}.json"
    payload = json.loads(record.read_text())
    payload.pop("version")
    record.write_text(json.dumps(payload))
    assert farm.portal.current_release()["version"] == "1.2.3" and "version" in json.loads(record.read_text())


@pytest.mark.skipif(sys.platform == "win32" or not all(shutil.which(tool) for tool in ("bash", "curl", "sha256sum")),
                    reason="the join script runs on a Linux host")
def test_the_join_script_comes_from_the_portal_and_trades_its_token_for_the_release(farm, tmp_path):
    request = urllib.request.Request(farm.url + "/api/v1/join.sh")
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.headers["Content-Type"].startswith("text/x-shellscript")
        assert response.read() == (RUNNER / "join-rig.sh").read_bytes(), "no key needed to fetch it"
    _, commit, body = _release(tmp_path)
    farm.call("POST", f"/api/v1/releases?commit={commit}", TOKEN, body)
    token = farm.call("POST", "/api/v1/rigs", TOKEN, b'{"name": "rig-4"}')[1]["token"]
    work = tmp_path / "join"
    work.mkdir()
    script = f"""
set -euo pipefail
JOIN_RIG_LIB=1 . {RUNNER / "join-rig.sh"}
redeem "$PORTAL" "$TOKEN" "$WORK/enroll.json"
json_field "$WORK/enroll.json" name
json_field "$WORK/enroll.json" key > "$WORK/node-key"
fetch_release "$PORTAL" "$WORK/node-key" "$WORK"
"""
    env = {**os.environ, "PORTAL": farm.url, "TOKEN": token, "WORK": str(work)}
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["rig-4", commit]
    assert (work / f"{commit}.bundle").read_bytes() == body, "the release, checked against its digest"
    # The same token again: the script stops, and says why.
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode != 0 and "the portal refused to add this rig (403)" in result.stderr


# ---- the console -------------------------------------------------------------------------


def test_the_console_keeps_what_happened_in_order_and_hands_it_over_from_a_cursor(tmp_path):
    """A rig is a machine somewhere else. Between pressing a button and a
    result there is a portal, a heartbeat and a rig doing the work, and with
    nothing on screen an operator cannot tell a slow command from a lost one.
    The console is that middle, kept in order and read from a cursor so a
    reader who looks away sees what happened rather than a fresh silence."""
    store = farm_service.JobStore(tmp_path / "farm.db")
    first = store.record_event("farm", "command", "restart queued for rig02", worker="rig02")
    store.record_event("rig02", "command", "restart started", worker="rig02")
    store.record_event("farm", "job", "queued discovery (abcd1234)", job_id="a" * 32)

    page = store.events_since(0)
    assert [event["text"] for event in page["events"]] == [
        "restart queued for rig02", "restart started", "queued discovery (abcd1234)",
    ]
    assert page["cursor"] == page["newest"] and page["missed"] == 0

    # From a cursor: only what is new, which is what a tail asks for.
    assert [event["text"] for event in store.events_since(page["cursor"])["events"]] == []
    store.record_event("rig02", "command", "restart done in 2.4s", worker="rig02")
    later = store.events_since(page["cursor"])
    assert [event["text"] for event in later["events"]] == ["restart done in 2.4s"]

    # One rig's console is that rig's lines, plus the farm's own.
    store.record_event("farm", "queue", "queue paused")
    only = store.events_since(first - 1, worker="rig02")
    assert "queue paused" in [event["text"] for event in only["events"]]
    assert all(event["worker"] in (None, "rig02") for event in only["events"])
    other = store.record_event("farm", "command", "restart queued for rig01", worker="rig01")
    assert other not in [event["id"] for event in store.events_since(0, worker="rig02")["events"]]


def test_a_reader_who_fell_behind_is_told_rather_than_shown_a_gap(tmp_path):
    """The console is a tail, not a second history: old lines go. A reader
    whose cursor is older than anything kept is told how much it missed,
    because a silent gap reads as "nothing happened"."""
    store = farm_service.JobStore(tmp_path / "farm.db")
    for index in range(5):
        store.record_event("farm", "command", f"line {index}")
    with store.connect() as db:
        db.execute("DELETE FROM events WHERE id <= 3")
    page = store.events_since(1)
    assert page["missed"] == 2, page
    assert [event["text"] for event in page["events"]] == ["line 3", "line 4"]


def test_a_command_says_when_the_rig_began_it(tmp_path):
    """`sent` is the last thing the portal knows by itself. A command that
    restarts a service or reads a board can take a minute, and until the rig
    says it has begun, a console cannot tell work from a command that fell on
    the floor."""
    manager = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    manager.worker_hello("rig02", {"kind": "hardware", "version": "1.0.1"})
    command = manager.request_command("rig02", "restart", {}, by="an-operator")
    assert command["status"] == "queued" and command["started_at"] is None

    manager.worker_heartbeat("rig02", {})  # the rig collects it
    assert manager.store.command(command["id"])["status"] == "sent"

    manager.command_started("rig02", command["id"])
    begun = manager.store.command(command["id"])
    assert begun["status"] == "running" and begun["started_at"]

    manager.command_result("rig02", command["id"], {"status": "done", "detail": "service restarted"})
    lines = [event["text"] for event in manager.console()["events"]]
    assert "restart queued for rig02 by an-operator" in lines
    assert "restart sent to rig02" in lines
    assert "restart started" in lines
    assert any(line.startswith("restart done") for line in lines)
    # And every one of them names the rig, so one rig's console is its own.
    assert {event["worker"] for event in manager.console(worker="rig02")["events"]} <= {"rig02", None}


def test_a_portal_records_the_stages_its_rigs_report(tmp_path):
    """A portal runs nothing itself: every stage of a leased run happens on a
    rig and arrives as a report. Without recording those, the console showed a
    run being queued and then silence -- which is exactly the silence it was
    built to end."""
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    portal.worker_hello("rig02", {"kind": "hardware", "version": "1.0.1"})
    # The run itself is not the subject: a leased job in the store, as the
    # portal has one while a rig works on it.
    job = portal.store.create("suite", {"profile": "canary", "ref": "main"},
                              tmp_path / "run.log", [])
    with portal.store.connect() as db:
        db.execute("UPDATE jobs SET worker='rig02', status='running' WHERE id=?", (job["id"],))

    portal.worker_stages("rig02", job["id"], [
        {"name": "flash", "status": "running"},
        {"name": "test", "status": "pending"},
    ])
    portal.worker_stages("rig02", job["id"], [
        {"name": "flash", "status": "passed", "summary": "6 boards flashed"},
        {"name": "test", "status": "running"},
    ])
    lines = [event["text"] for event in portal.console()["events"]]
    assert "flash: running" in lines
    assert "flash: passed -- 6 boards flashed" in lines
    assert "test: running" in lines

    # A report carries every stage each time; only what changed is a line.
    before = len(portal.console()["events"])
    portal.worker_stages("rig02", job["id"], [
        {"name": "flash", "status": "passed", "summary": "6 boards flashed"},
        {"name": "test", "status": "running"},
    ])
    assert len(portal.console()["events"]) == before, "a repeat is not news"

    # And the lines are the rig's, so its own console shows its run.
    said = portal.console(worker="rig02")["events"]
    assert any(event["text"] == "test: running" and event["source"] == "rig02" for event in said)


def test_a_channel_is_set_from_the_portal_without_the_portal_reading_it(tmp_path):
    """The portal relays a credential it cannot open, and checks everything
    about it that does not need opening: the channel, the settings that
    channel takes, and that it was sealed for the key this rig reported."""
    import base64

    manager = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    manager.worker_hello("rig02", {"kind": "hardware", "version": "1.0.1"})
    fingerprint = "a" * 64
    manager.worker_heartbeat("rig02", {"config": {"seal_key": {"fingerprint": fingerprint, "spki": "x"}}})
    sealed = base64.b64encode(b"s" * 384).decode()

    command = manager.request_command("rig02", "notify_set", {
        "channel": "telegram", "settings": {"chat_id": "-1001234567890"},
        "sealed": sealed, "fingerprint": fingerprint,
    }, by="an-operator")
    assert command["kind"] == "notify_set"
    assert command["args"]["settings"] == {"chat_id": "-1001234567890"}

    # A chat id is what a Telegram channel needs; a webhook does not take one.
    with pytest.raises(ValueError, match="chat id"):
        manager.request_command("rig02", "notify_set", {
            "channel": "telegram", "settings": {}, "sealed": sealed, "fingerprint": fingerprint}, by=None)
    with pytest.raises(ValueError, match="takes format"):
        manager.request_command("rig02", "notify_set", {
            "channel": "webhook", "settings": {"chat_id": "1"}, "sealed": sealed,
            "fingerprint": fingerprint}, by=None)
    with pytest.raises(ValueError, match="channel must be one of"):
        manager.request_command("rig02", "notify_set", {
            "channel": "smoke-signal", "settings": {}, "sealed": sealed,
            "fingerprint": fingerprint}, by=None)

    # Sealed for a key the rig no longer has: a page loaded before it changed.
    with pytest.raises(farm_service.ElsewhereError, match="different key"):
        manager.request_command("rig02", "notify_set", {
            "channel": "telegram", "settings": {"chat_id": "42"}, "sealed": sealed,
            "fingerprint": "b" * 64}, by=None)

    # The portal keeps no copy once the rig has it.
    manager.worker_heartbeat("rig02", {})
    assert manager.store.command(command["id"])["args"]["sealed"] == farm_service.SEALED_PLACEHOLDER


def test_the_farm_tells_its_admin_what_no_rig_can_say(tmp_path):
    """A rig tells its owner about itself. Nobody was telling the person who
    looks after the farm that a rig had gone quiet -- and the rig that went
    quiet is in no position to mention it."""
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    sent = []
    portal.set_farm_notify({
        "channel": "telegram", "credential": "123456:AA-the-bot-token", "chat_id": "-1001234567890",
    }, by="an-admin")
    for item in portal.__dict__["farm_notifiers"].values():
        item._post = lambda url, body: sent.append((url, json.loads(body))) or 200

    # On the portal's own volume, 0600, and never handed back.
    view = portal.farm_notify()
    assert len(view["channels"]) == 1
    channel = view["channels"][0]
    secret = portal.farm_secret_path(channel["id"])
    assert secret.read_text(encoding="utf-8").strip() == "123456:AA-the-bot-token"
    assert oct(secret.stat().st_mode)[-3:] == "600"
    assert channel["channel"] == "telegram" and channel["chat_id"] == "-1001234567890"
    assert channel["updated_by"] == "an-admin" and str(secret) == channel["secret_file"]
    assert "AA-the-bot-token" not in json.dumps(view), "the credential is named by its file, never returned"

    # A rig that stops answering is said once, and its return is said once.
    portal.worker_hello("rig02", {"kind": "hardware", "version": "1.0.1"})
    with portal.store.connect() as db:
        db.execute("UPDATE workers SET seen_at=? WHERE name=?",
                   ("2020-01-01T00:00:00+00:00", "rig02"))
    for _ in range(3):
        portal._sweep_workers()
    for item in (portal.__dict__.get("farm_notifiers") or {}).values():
        for thread in list(getattr(item, "_threads", []) or []):
            thread.join(timeout=5)
    import time as _time
    _time.sleep(0.4)
    quiet = [body for _, body in sent if "gone quiet" in body.get("text", "")]
    assert len(quiet) == 1, [body.get("text") for _, body in sent]
    assert "rig02" in quiet[0]["text"] and quiet[0]["chat_id"] == "-1001234567890"


def test_the_farm_channel_is_checked_before_it_is_stored(tmp_path):
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    with pytest.raises(ValueError, match="channel must be one of"):
        portal.set_farm_notify({"channel": "smoke-signal", "credential": "x" * 12}, by=None)
    with pytest.raises(ValueError, match="chat id"):
        portal.set_farm_notify({"channel": "telegram", "credential": "123456:AA-token"}, by=None)
    with pytest.raises(ValueError, match="bot token looks like"):
        portal.set_farm_notify({"channel": "telegram", "credential": "not-a-token", "chat_id": "42"}, by=None)
    with pytest.raises(ValueError, match="must be https"):
        portal.set_farm_notify({"channel": "webhook", "credential": "http://example.invalid/hook"}, by=None)
    assert portal.farm_notify()["channels"] == [], "nothing refused is written"

    # Turned off: the credential goes with it.
    view = portal.set_farm_notify({"channel": "webhook", "credential": "https://example.invalid/hook"}, by="an-admin")
    kept = Path(view["channels"][0]["secret_file"])
    assert kept.exists()
    assert portal.clear_farm_notify("an-admin")["channels"] == []
    assert not kept.exists()

def test_a_rigs_page_is_built_from_one_read_of_its_row(tmp_path):
    """The view and the stored row were read separately, so a heartbeat
    landing between them showed a configuration with no time beside it --
    `config_at` empty under a page that had just displayed the configuration.
    It failed CI on the loaded runner and passed everywhere else, which is how
    a read-consistency bug reads."""
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    portal.worker_hello("rig02", {"kind": "hardware", "version": "1.0.1"})
    portal.worker_heartbeat("rig02", {"config": {"gateway": {"enabled": True, "ssid": "Alteriom-HIL"}}})

    # Only this thread's reads: the portal's dispatcher sweeps its workers on
    # its own, and a loaded runner lands one of those sweeps inside the call.
    mine = threading.get_ident()
    reads = []
    original = portal.store.workers

    def counted():
        rows = original()
        if threading.get_ident() == mine:
            reads.append(len(rows))
        return rows

    portal.store.workers = counted
    try:
        detail = portal.worker_detail("rig02")
    finally:
        portal.store.workers = original
    assert detail["config"]["gateway"]["ssid"] == "Alteriom-HIL"
    assert detail["config_at"], "the configuration and its time come from the same read"
    assert len(reads) == 1, f"the row was read {len(reads)} times"


def test_what_was_asked_of_a_rig_is_read_back_a_page_at_a_time(tmp_path):
    """The card on a rig's page shows the last few commands. "What has been
    done to this rig, and by whom" is a different question, asked of the whole
    history -- and a card that grew without end was the only answer to it."""
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    portal.worker_hello("rig02", {"kind": "hardware", "version": "1.0.1"})
    for _ in range(30):
        portal.request_command("rig02", "rediscover", {}, by="an-operator")

    first = portal.worker_commands("rig02", limit=25, offset=0)
    assert first["total"] == 30 and first["offset"] == 0 and len(first["commands"]) == 25
    second = portal.worker_commands("rig02", limit=25, offset=25)
    assert second["total"] == 30 and len(second["commands"]) == 5
    # Newest first, and no command is on both pages or on neither.
    ids = [command["id"] for command in first["commands"] + second["commands"]]
    assert len(set(ids)) == 30
    stamps = [command["created_at"] for command in first["commands"]]
    assert stamps == sorted(stamps, reverse=True)
    # The page a caller following one command reads is the same shape.
    assert set(portal.worker_commands("rig02", limit=10)) == {"commands", "total", "limit", "offset"}


def test_a_rig_is_told_which_channel_should_say_what(tmp_path):
    """Its owner chooses what wakes them, per channel: a red board down one, a
    paused queue down none. Neither the choice nor turning a channel off is a
    credential, so neither asks for the bot token again -- and both name the
    channel, because a rig can hold several."""
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    portal.worker_hello("rig02", {"kind": "hardware", "version": "1.0.1"})
    queued = portal.request_command(
        "rig02", "notify_tune", {"settings": {"id": "2", "events": "board_red,queue_paused"}},
        by="an-admin")
    assert queued["args"]["settings"]["id"] == "2"
    assert queued["kind"] == "notify_tune"
    # Off and on again is the same command, so putting a channel back never
    # asks for the credential a second time.
    assert portal.request_command(
        "rig02", "notify_tune", {"settings": {"id": "2", "enabled": True}},
        by="an-admin")["kind"] == "notify_tune"

    # A test and a removal name the channel too.
    for kind in ("notify_test", "notify_remove"):
        assert portal.request_command(
            "rig02", kind, {"settings": {"id": "2"}}, by="an-admin")["kind"] == kind
def test_a_callmebot_channel_needs_a_link_that_is_still_there(tmp_path):
    """Removing a link deletes the credential file and leaves `url_file` in
    the configuration, so the path is not the question -- the rig reports the
    usable link as `callmebot.link`. Asked over a path with nothing behind it,
    the portal refused nothing and the rig turned notifications on against a
    file that was gone."""
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    portal.worker_hello("rig02", {"kind": "hardware", "version": "1.0.1"})
    removed = {"callmebot": {"url_file": "/etc/alteriom-hil/providers/callmebot-url", "link": None}}
    portal.worker_heartbeat("rig02", {"config": removed})
    with pytest.raises(farm_service.ElsewhereError, match="no usable CallMeBot link"):
        portal.request_command("rig02", "notify_set", {"channel": "callmebot", "settings": {}}, by="an-admin")

    stored = {"callmebot": {"url_file": "/etc/alteriom-hil/providers/callmebot-url",
                            "link": "whatsapp to …76 (key ***)"}}
    portal.worker_heartbeat("rig02", {"config": stored})
    queued = portal.request_command("rig02", "notify_set", {"channel": "callmebot", "settings": {}}, by="an-admin")
    assert queued["kind"] == "notify_set" and queued["args"]["channel"] == "callmebot"
    # And it is accepted without one: asking for a sealed credential the
    # channel does not have refused every attempt to choose it.
    assert "sealed" not in queued["args"], "the link it already has, not a second credential"


def test_every_command_the_page_sends_is_one_the_portal_accepts(tmp_path):
    """The CallMeBot channel shipped unable to work: `notify_set` asked for a
    sealed credential whatever the channel, so the portal refused the one
    channel that has nothing to seal before the rig ever saw it. It had tests
    -- of the notifier, and of the rig script -- and none of them sent the
    command the page sends.

    So each row here is a call site in rig/web/app.js, and each is put
    through `request_command`. A refusal for its own reasons is fine: a
    ciphertext that is not one, a rig with no seal key. Being refused for the
    shape of its arguments is the bug this is here for.
    """
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    portal.worker_hello("rig02", {"kind": "hardware", "version": "1.0.1"})
    portal.worker_heartbeat("rig02", {"config": {"callmebot": {
        "url_file": "/etc/alteriom-hil/providers/callmebot-url", "link": "whatsapp to …76 (key ***)"}}})

    script = (REPO / "rig" / "web" / "app.js").read_text(encoding="utf-8")
    sent = [
        ("rediscover", {}, 'data-kind="rediscover"'),
        ("restart", {}, 'data-kind="restart"'),
        ("update_now", {}, 'data-kind="update_now"'),
        ("logs", {"lines": 300}, '{lines: 300}'),
        ("configure", {"settings": {"queue.concurrency": 2}}, 'rigCommand(name, "configure", {settings})'),
        ("read_details", {"board": "esp32-01"}, '"read_details", {board: id}'),
        ("register", {"id": "esp32-01", "mac": "aa:bb:cc:dd:ee:01"}, '"register", {id:'),
        ("unregister", {"id": "esp32-01"}, '"unregister", {id}'),
        ("provider_remove", {"provider": "callmebot"}, '"provider_remove", {provider: "callmebot"}'),
        ("provider_test", {"provider": "callmebot"}, '"provider_test", {provider: "callmebot"}'),
        ("provider_set", {"provider": "callmebot", "sealed": "x", "fingerprint": "y"},
         '"provider_set", {provider: "callmebot", sealed:'),
        # Both name the channel: a rig can hold several.
        ("notify_test", {"settings": {"id": "1"}}, 'rigCommand(name, "notify_test",'),
        ("notify_remove", {"settings": {"id": "1"}}, 'rigCommand(name, "notify_tune",'),
        ("notify_tune", {"settings": {"id": "1", "enabled": False}},
         'rigCommand(name, "notify_tune",'),
        # The one that was refused every time it was chosen.
        ("notify_set", {"channel": "callmebot", "settings": {}},
         'rigCommand(name, "notify_set", {channel: kind, settings: {}})'),
        ("notify_set", {"channel": "telegram", "settings": {"chat_id": "8339907776"},
                        "sealed": "x", "fingerprint": "y"},
         'rigCommand(name, "notify_set", {channel: kind, settings, ...sealed})'),
    ]
    for kind, args, call in sent:
        assert call in script, f"{kind}: the page no longer sends this; update the row"
        try:
            portal.request_command("rig02", kind, args, by="an-admin")
        except (farm_service.ElsewhereError, ValueError) as exc:
            said = str(exc)
            assert not said.startswith(f"{kind} needs "), f"{kind}: {said}"
            assert not said.startswith(f"{kind} takes no "), f"{kind}: {said}"


def test_a_page_asked_to_start_past_the_end_of_the_world_is_answered_not_dropped(tmp_path):
    """`?offset=9223372036854775808` is an ordinary authenticated request. It
    reached SQLite as a Python integer too large to bind, and OverflowError is
    not a ValueError: the request ended with no answer at all instead of the
    empty page it was asking for. Every list the farm pages is bounded the same
    way its limit already was."""
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    portal.worker_hello("rig02", {"kind": "hardware", "version": "1.0.1"})
    portal.request_command("rig02", "rediscover", {}, by="an-operator")

    beyond = 2 ** 63  # one past what SQLite can bind
    page = portal.worker_commands("rig02", limit=25, offset=beyond)
    assert page["commands"] == [] and page["total"] == 1
    assert page["offset"] == farm_service.MAX_PAGE_OFFSET, "clamped, and it says where it started"

    # The lists beside it are asked the same question, and none of them is the
    # one that answers with a traceback.
    assert portal.store.audit_page(limit=10, offset=beyond)["entries"] == []
    assert portal.store.page(limit=10, offset=beyond)["jobs"] == []
    assert portal.artifact_index(limit=10, offset=beyond)["bundles"] == []


def test_the_farm_sends_through_every_channel_its_admin_adds(tmp_path):
    """It held one. An admin who had set Telegram and wanted the webhook their
    own ingestion listens on had nowhere to put it: setting a channel replaced
    the one that was there. A channel added is a channel added, each with its
    own credential file, and what the farm has to say goes down all of them."""
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    portal.set_farm_notify({"channel": "telegram", "credential": "123456:AA-the-bot-token",
                            "chat_id": "-1001234567890"}, by="an-admin")
    view = portal.set_farm_notify({"channel": "webhook", "format": "json",
                                   "credential": "https://ingest.example.invalid/hook"}, by="an-admin")
    kinds = [item["channel"] for item in view["channels"]]
    assert kinds == ["telegram", "webhook"], "the second is added, not substituted"
    ids = [item["id"] for item in view["channels"]]
    assert len(set(ids)) == 2
    files = [Path(item["secret_file"]) for item in view["channels"]]
    assert all(item.exists() for item in files) and len(set(files)) == 2
    assert "AA-the-bot-token" not in json.dumps(view) and "ingest.example.invalid" not in json.dumps(view)

    # One message, both channels.
    import time as _time

    sent = []
    for item in portal.__dict__["farm_notifiers"].values():
        item._post = lambda url, body: sent.append(url) or 200
    note = farm_service.Notification("rig_offline", "rig02 has gone quiet", "Last heard from a while ago.")
    portal.notify_farm(note)
    # Each channel sends on a thread of its own, so this waits for what was
    # asked for rather than for a moment and a hope.
    deadline = _time.monotonic() + 5
    while len(sent) < 2 and _time.monotonic() < deadline:
        _time.sleep(0.05)
    assert len(sent) == 2, sent

    # Removed one at a time, each taking its own credential and leaving the
    # other sending.
    left = portal.clear_farm_notify("an-admin", ids[0])
    assert [item["id"] for item in left["channels"]] == [ids[1]]
    assert not files[0].exists() and files[1].exists()
    with pytest.raises(LookupError):
        portal.clear_farm_notify("an-admin", "nosuch")


def test_a_farm_that_had_one_channel_keeps_it_when_it_can_have_many(tmp_path):
    """The setting written by the version that held one channel is read as a
    list of one, and the credential it was already sending with is moved to the
    name that says which channel it belongs to -- an admin does not re-enter a
    bot token because the portal learned to hold two."""
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    # Exactly what the older portal stored.
    portal.store.set_farm_setting(portal.FARM_NOTIFY, {
        "channel": "telegram", "enabled": True, "chat_id": "-1001234567890",
        "updated_at": "2026-09-01T00:00:00+00:00", "updated_by": "an-admin",
    }, "an-admin")
    old = portal.state / "notify-credential"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("123456:AA-the-bot-token\n", encoding="utf-8")

    view = portal.farm_notify()
    assert len(view["channels"]) == 1
    kept = view["channels"][0]
    assert kept["channel"] == "telegram" and kept["chat_id"] == "-1001234567890"
    assert kept["updated_by"] == "an-admin"
    moved = Path(kept["secret_file"])
    assert moved.read_text(encoding="utf-8").strip() == "123456:AA-the-bot-token"
    assert not old.exists(), "moved, not copied: one credential, in one place"
    # And it still sends.
    portal._build_farm_notifier()
    assert list(portal.__dict__["farm_notifiers"]) == [kept["id"]]


def test_two_admins_adding_a_channel_at_once_both_get_one(tmp_path):
    """The API is a ThreadingHTTPServer. The channel list was read, given an
    id and written back without a lock, so two additions at the same moment
    chose the same id, wrote to the same credential file, and each stored a
    list without the other's channel -- one admin's bot token silently
    replaced by another's webhook."""
    import threading

    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    start = threading.Barrier(4)
    failures: list[BaseException] = []

    def add(index):
        try:
            start.wait(timeout=5)
            portal.set_farm_notify({"channel": "webhook", "format": "json",
                                    "credential": f"https://ingest.example.invalid/hook-{index}"}, by="an-admin")
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            failures.append(exc)

    threads = [threading.Thread(target=add, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not failures, failures

    channels = portal.farm_notify()["channels"]
    assert len(channels) == 4, [item["id"] for item in channels]
    ids = [item["id"] for item in channels]
    assert len(set(ids)) == 4, ids
    files = [Path(item["secret_file"]) for item in channels]
    assert len(set(files)) == 4 and all(item.exists() for item in files)
    # Four credentials, each its own: none overwrote another.
    written = sorted(item.read_text(encoding="utf-8").strip() for item in files)
    assert written == sorted(f"https://ingest.example.invalid/hook-{index}" for index in range(4))


def test_editing_one_farm_channel_does_not_clear_what_another_last_did(tmp_path):
    """Every notifier was rebuilt whenever any channel changed, and a new one
    has sent nothing: a failed delivery on the Telegram channel vanished when
    the webhook beside it was edited, and the page turned "not working" back
    into "on" without a message having been delivered."""
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    portal.set_farm_notify({"channel": "telegram", "credential": "123456:AA-the-bot-token",
                            "chat_id": "-1001234567890"}, by="an-admin")
    first = portal.farm_notify()["channels"][0]["id"]

    # It tried, and it failed: that is what the page reports.
    def refuse(url, body):
        raise OSError("failed to reach api.telegram.org: connection refused")

    portal.__dict__["farm_notifiers"][first]._post = refuse
    portal.__dict__["farm_notifiers"][first].send(
        farm_service.Notification("rig_offline", "rig02 has gone quiet", "Last heard from a while ago."))
    failed = next(item for item in portal.farm_notify()["channels"] if item["id"] == first)
    assert failed["last_delivery"] and failed["last_delivery"]["ok"] is False

    # A second channel is added beside it. The first was not touched.
    portal.set_farm_notify({"channel": "webhook", "format": "json",
                            "credential": "https://ingest.example.invalid/hook"}, by="an-admin")
    after = next(item for item in portal.farm_notify()["channels"] if item["id"] == first)
    assert after["last_delivery"] and after["last_delivery"]["ok"] is False, \
        "the channel nobody touched still reports the message that did not arrive"
    assert portal.__dict__["farm_notifiers"][first] is portal.__dict__["farm_notifiers"][first]

    # Removing the other one leaves it alone too.
    other = next(item["id"] for item in portal.farm_notify()["channels"] if item["id"] != first)
    portal.clear_farm_notify("an-admin", other)
    left = portal.farm_notify()["channels"]
    assert [item["id"] for item in left] == [first]
    assert left[0]["last_delivery"]["ok"] is False

    # But a channel that is changed starts again: its credential is new, and
    # what the old one did is not a report about the new one.
    portal.set_farm_notify({"id": first, "channel": "telegram", "chat_id": "-1009999999999",
                            "credential": "123456:AA-a-different-token"}, by="an-admin")
    changed = portal.farm_notify()["channels"][0]
    assert changed["last_delivery"] is None


def test_a_test_message_asked_for_with_a_bad_body_is_refused_not_a_crash(farm):
    """An admin's client sends `{}`; anything else is still an authenticated
    request, and `POST /api/v1/farm/notify/test` read its body with nothing to
    catch a body that is not an object. The endpoint beside it answers 400
    with what was wrong; this one ended as an internal error."""
    from alteriom_hil.api_keys import add_key, keys_document

    entries, admin_key = add_key([], "an-admin", "admin")
    (farm.portal.state.parent / "etc" / "api-keys.yaml").write_text(keys_document(entries))

    farm.portal.set_farm_notify({"channel": "telegram", "credential": "123456:AA-the-bot-token",
                                 "chat_id": "-1001234567890"}, by="an-admin")
    for body in (b"{not json}", b"[]", b'"a string"', b"12"):
        status, answer = farm.call("POST", "/api/v1/farm/notify/test", admin_key, body)
        assert status == 400, (body, status, answer)
        assert answer.get("error"), body
    # A channel that is not there is still a 404, and no body at all is fine.
    assert farm.call("POST", "/api/v1/farm/notify/test", admin_key, b'{"id": "nope"}')[0] == 404


def test_a_migration_that_cannot_move_the_credential_is_tried_again(tmp_path, monkeypatch):
    """Moving the one channel's credential to the name that says which channel
    it belongs to can fail -- a permission, a full disk, a filesystem that is
    momentarily unhappy. Writing the new shape anyway would mean never trying
    again, with the secret in a file nothing reads: the farm silent until an
    admin typed the bot token in a second time."""
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    portal.store.set_farm_setting(portal.FARM_NOTIFY, {
        "channel": "telegram", "enabled": True, "chat_id": "-1001234567890",
        "updated_at": "2026-09-01T00:00:00+00:00", "updated_by": "an-admin",
    }, "an-admin")
    legacy = portal.state / "notify-credential"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("123456:AA-the-bot-token\n", encoding="utf-8")

    refused = Path.replace

    def refuse(self, target):
        if self.name == "notify-credential":
            raise PermissionError(13, "Permission denied")
        return refused(self, target)

    monkeypatch.setattr(Path, "replace", refuse)
    view = portal.farm_notify()
    # Still one channel, still reading the credential it has.
    assert len(view["channels"]) == 1
    assert Path(view["channels"][0]["secret_file"]) == legacy
    assert legacy.read_text(encoding="utf-8").strip() == "123456:AA-the-bot-token"
    # And the old shape is left alone, so this is tried again rather than
    # recorded as done.
    assert portal.store.farm_setting(portal.FARM_NOTIFY).get("channels") is None
    portal._build_farm_notifier()
    assert list(portal.__dict__["farm_notifiers"]) == ["1"], "it still sends meanwhile"

    # A channel added while the move is outstanding gets a file of its own.
    # The fallback used to be "any channel with no file of its own reads the
    # old name", so the second channel resolved to the first one's credential
    # and wrote over it.
    added = portal.set_farm_notify({"channel": "webhook", "format": "json",
                                    "credential": "https://ingest.example.invalid/hook"}, by="an-admin")
    files = {item["id"]: Path(item["secret_file"]) for item in added["channels"]}
    assert files["1"] == legacy, "the one that has not moved still says where it is"
    assert files["2"].name == "notify-credential-2" and files["2"] != legacy
    assert legacy.read_text(encoding="utf-8").strip() == "123456:AA-the-bot-token",         "the bot token is not written over by the webhook beside it"
    assert files["2"].read_text(encoding="utf-8").strip() == "https://ingest.example.invalid/hook"
    # And both still send, each from its own file.
    portal._build_farm_notifier()
    assert sorted(portal.__dict__["farm_notifiers"]) == ["1", "2"]
    portal.clear_farm_notify("an-admin", "2")

    # Something is sent down it while the move is still outstanding, so what
    # the channel last did is on the notifier that is reading the old path.
    def refuse_post(url, body):
        raise OSError("failed to reach api.telegram.org: connection refused")

    portal.__dict__["farm_notifiers"]["1"]._post = refuse_post
    portal.__dict__["farm_notifiers"]["1"].send(
        farm_service.Notification("rig_offline", "rig02 has gone quiet", "Last heard from a while ago."))

    # The next read, with the move working, finishes it.
    monkeypatch.setattr(Path, "replace", refused)
    view = portal.farm_notify()
    moved = Path(view["channels"][0]["secret_file"])
    assert moved.name == "notify-credential-1" and not legacy.exists()
    assert moved.read_text(encoding="utf-8").strip() == "123456:AA-the-bot-token"
    assert portal.store.farm_setting(portal.FARM_NOTIFY)["channels"][0]["id"] == "1"
    # And whatever was already sending -- built when the move had not worked
    # -- is told where the credential went, rather than going on reading a
    # file that has gone and failing every message until something rebuilt it.
    living = portal.__dict__["farm_notifiers"]["1"]
    assert Path(living.url_file) == moved
    assert living.last and living.last["ok"] is False, "and it keeps what it last did"


def test_a_url_and_a_secret_are_all_it_takes_for_events_to_flow(tmp_path):
    """The whole point: define where events go and what signs them, and what
    the farm sees arrives there. The envelope and the signature are the
    webhook connector's, so a consumer already ingesting from that reads these
    without being taught a second format."""
    import time as _time

    from alteriom_hil import webhooks

    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    portal.worker_hello("rig02", {"kind": "hardware", "version": "1.0.1"})

    made = portal.add_webhook("farm", {"name": "our ingestion",
                                       "url": "https://ingest.example.invalid/hook"}, by="an-admin")
    # A secret is returned once, when there was none to bring, and never again.
    secret = made["secret"]
    assert len(secret) == 64
    listed = portal.webhook_subscriptions("farm")["subscriptions"]
    assert len(listed) == 1 and "secret" not in listed[0]
    assert listed[0]["events"] == ["*"] and listed[0]["active"] is True
    assert secret not in json.dumps(listed)

    # What the farm sees goes there, signed.
    sent = []
    portal._deliver_webhook = lambda sub, event, delivery=None: sent.append(
        (sub, event, webhooks.serialize(webhooks.envelope(event, delivery or "d")))) or {"ok": True}
    portal.emit(webhooks.Event("rig", "offline", rig="rig02", summary="rig02 has gone quiet"))
    deadline = _time.monotonic() + 5
    while not sent and _time.monotonic() < deadline:
        _time.sleep(0.02)
    assert sent, "an event with a subscription for it is delivered"
    subscription, event, body = sent[0]
    assert subscription.url == "https://ingest.example.invalid/hook"
    assert subscription.secret == secret
    assert webhooks.verify(body, secret, webhooks.sign(body, secret))
    envelope = json.loads(body)
    assert envelope["event"] == "rig" and envelope["action"] == "offline"
    assert envelope["rig"] == "rig02" and envelope["summary"]

    # A rig's own subscription gets that rig's events and not the fleet's.
    portal.add_webhook("rig02", {"name": "the owner's", "url": "https://owner.example.invalid/hook",
                                 "secret": "x" * 32, "events": ["run.failed"]}, by="sparck")
    mine = portal.webhook_subscriptions("rig02")["subscriptions"]
    assert [item["name"] for item in mine] == ["the owner's"]
    assert mine[0]["events"] == ["run.failed"]
    # And what it may ask for is what the farm knows about a rig.
    assert "queue.paused" not in portal.webhook_subscriptions("rig02")["events"]
    with pytest.raises(ValueError, match="cannot ask for"):
        portal.add_webhook("rig02", {"name": "greedy", "url": "https://x.invalid/h",
                                     "secret": "x" * 32, "events": ["queue.paused"]}, by="sparck")


def test_a_subscription_is_https_with_a_secret_and_nothing_else_will_do(tmp_path):
    """A signature says who sent an event; it does not keep what is in one off
    the wire, and an event carries which rig is failing and which boards. So
    the destination is https, and a secret is not optional -- one is made when
    nobody brings one."""
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    for refused, says in (
        ({"name": "n", "url": "http://ingest.example.invalid/hook"}, "https"),
        ({"name": "n", "url": "ftp://ingest.example.invalid/hook"}, "https"),
        ({"name": "n", "url": "not a url"}, "https"),
        ({"name": "", "url": "https://x.invalid/h"}, "name must be"),
        ({"name": "n", "url": "https://x.invalid/h", "secret": "short"}, "16 to 200"),
        ({"name": "n", "url": "https://x.invalid/h", "events": []}, "non-empty list"),
        ({"name": "n", "url": "https://x.invalid/h", "events": ["run.exploded"]}, "not an event"),
        ({"name": "n", "url": "https://x.invalid/h", "max_retries": 99}, "from 1 to 10"),
        ({"name": "n", "url": "https://x.invalid/h", "surprise": True}, "unknown fields"),
    ):
        with pytest.raises(ValueError, match=says):
            portal.add_webhook("farm", refused, by="an-admin")
    assert portal.webhook_subscriptions("farm")["subscriptions"] == [], "nothing refused is stored"

    # A secret brought is kept; one not brought is made rather than skipped.
    brought = portal.add_webhook("farm", {"name": "mine", "url": "https://x.invalid/h",
                                          "secret": "a" * 40}, by="an-admin")
    assert brought["secret"] == "a" * 40
    made = portal.add_webhook("farm", {"name": "theirs", "url": "https://y.invalid/h"}, by="an-admin")
    assert made["secret"] and made["secret"] != brought["secret"]


def test_every_run_event_a_subscription_may_ask_for_is_one_the_farm_sends(farm):
    """`run.queued` and `run.started` could be subscribed to through the API
    and the page, and never arrived: the only place a run was reported was
    after a rig sent back a final result. A cancellation from the queue was
    never reported at all. An action a subscription may name is an action the
    farm sends."""
    from alteriom_hil import webhooks

    portal, said = farm.portal, []
    # The run must still be queued when it is cancelled. Left running, the
    # node's worker could take an inventory in the moment between submit and
    # cancel -- and a running run is only asked to stop, its `run.cancelled`
    # coming later from the run itself. It passed on a fast machine and lost
    # the race on a CI runner. Paused, nothing starts.
    farm.node.pause()
    # Both managers report through the same helper; the recorder stands in for
    # the dispatch so the test is about which transitions speak, not delivery.
    portal.emit = farm.node.emit = lambda event: said.append(event.name)

    # A discovery, because it needs no bundle: which run this is does not
    # matter to what is being asserted, and a suite would tie the test to a
    # bundle's recorded revision matching the checkout's -- true on a
    # developer's machine, false in CI.
    job = farm.node.submit("inventory", {}, submitted_by="sparck")
    assert "run.queued" in said, "a run exists the moment it is queued"

    said.clear()
    farm.node.cancel(job["id"], "not wanted after all")
    assert "run.cancelled" in said, "a run cancelled from the queue reported nothing at all"

    # Every action the farm advertises is one something emits: a subscription
    # that can be accepted for it must be able to receive it.
    source = (REPO / "core" / "alteriom_hil" / "service.py").read_text(encoding="utf-8")
    for status in webhooks.EVENTS["run"]:
        assert f'_emit_run(' in source and f'"{status}"' in source, status
    # And the helper refuses one that is not advertised.
    farm.node._emit_run(job["id"], "exploded", job)
    assert "run.exploded" not in said


def test_a_user_administers_their_own_rigs_and_the_farm_has_one_admin(tmp_path):
    """A `user` key is the admin of their own account -- their rigs, what
    those rigs say, where their events go -- and of nothing else. The farm
    itself has one admin. Rig-scoped writes were open to any user key, which
    made every user an admin of every rig."""
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    portal.worker_hello("rig02", {"kind": "hardware", "version": "1.0.1"})
    portal.worker_hello("rig07", {"kind": "hardware", "version": "1.0.1"})

    sparck = farm_service.Identity("sparck", "admin")
    ada = farm_service.Identity("ada", "user")
    grace = farm_service.Identity("grace", "user")

    # Until somebody is named, a rig is the farm's: an admin's alone.
    assert portal.may_manage_rig("rig02", sparck)
    assert not portal.may_manage_rig("rig02", ada)
    with pytest.raises(PermissionError, match="no owner yet"):
        portal.add_webhook("rig02", {"name": "ada's", "url": "https://ada.example.invalid/h"},
                           by="ada", identity=ada)

    # Given to somebody, it is theirs to administer -- and nobody else's.
    portal.set_rig_owner("rig02", "ada", keys=None, by="sparck")
    assert portal.rig_owner("rig02") == "ada"
    assert portal.may_manage_rig("rig02", ada)
    assert not portal.may_manage_rig("rig02", grace)
    made = portal.add_webhook("rig02", {"name": "ada's", "url": "https://ada.example.invalid/h"},
                              by="ada", identity=ada)
    assert made["subscriptions"][0]["name"] == "ada's"

    with pytest.raises(PermissionError, match="belongs to ada"):
        portal.add_webhook("rig02", {"name": "grace's", "url": "https://grace.example.invalid/h"},
                           by="grace", identity=grace)
    # Nor may another user touch what is already there.
    sub_id = made["id"]
    for call in (lambda: portal.change_webhook(sub_id, {"active": False}, "grace", "rig02", grace),
                 lambda: portal.remove_webhook(sub_id, "grace", "rig02", grace),
                 lambda: portal.test_webhook(sub_id, "rig02", grace)):
        with pytest.raises(PermissionError):
            call()
    # The farm's admin may, because the farm is theirs.
    assert portal.change_webhook(sub_id, {"active": False}, "sparck", "rig02", sparck)

    # And the farm's own events are never a user's, whoever owns what.
    with pytest.raises(farm_service.ElsewhereError):
        farm_service.manager_for("node")(
            REPO, tmp_path / "node", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
            Path(sys.executable), mode="node",
        ).add_webhook("farm", {"name": "x", "url": "https://x.invalid/h"}, by="ada", identity=ada)


def test_a_rig_is_private_until_its_owner_shows_it_and_the_world_sees_nothing_that_is_anybodys(tmp_path):
    """The world page is a separate query with its own fields, not the
    private page with things hidden in the browser: a public rig is its name,
    what its owner wrote, whether it is up, its release, its board families
    and its public chips -- never its owner, its configuration, its
    addresses, its boards' identities or its runs. A private rig is not
    there at all."""
    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    boards = [{"id": "esp32-01", "target": "esp32", "mac": "6c:c8:40:34:1e:cc", "port": "/dev/ttyUSB0"},
              {"id": "c6-01", "target": "esp32-c6", "mac": "6c:c8:40:34:1e:dd", "port": "/dev/ttyUSB1"}]
    config = {"gateway": {"enabled": True, "ssid": "Alteriom-HIL", "channel": 1,
                          "password_file": "/etc/alteriom-hil/gateway-wifi-password"},
              "mqtt": {"enabled": True, "url": "mqtt://10.42.0.1:1883"},
              "notify_channels": [{"id": "tg", "channel": "telegram", "enabled": True, "chat_id": "4242"}]}
    portal.worker_hello("rig02", {"kind": "hardware", "version": "1.0.1", "inventory": {"boards": boards},
                                  "config": config})
    # Its own board: a board another rig already claims is not counted twice.
    portal.worker_hello("rig07", {"kind": "hardware", "version": "1.0.1", "config": config, "inventory": {"boards": [
        {"id": "esp8266-01", "target": "esp8266", "mac": "6c:c8:40:34:1e:ee", "port": "/dev/ttyUSB0"}]}})
    portal.store.set_rig_details("rig02", "the bench by the window", "Montréal", "sparck")

    sparck = farm_service.Identity("sparck", "admin")
    ada = farm_service.Identity("ada", "user")
    grace = farm_service.Identity("grace", "user")
    portal.set_rig_owner("rig02", "ada", keys=None, by="sparck")

    # Private until its owner says otherwise, and only its owner (or an admin) says.
    assert portal.rig_visibility("rig02") == "private"
    assert portal.world_view()["rigs"] == []
    # It is still counted, though. The world page shows how much hardware the
    # farm has and how busy it is without showing whose any of it is: a
    # private rig is in the totals and absent from the list.
    hidden = portal.world_view()["stats"]
    assert hidden["shown"] == 0, "nothing named while every rig is private"
    assert hidden["rigs"] >= 1 and hidden["boards"] >= 1, "and still counted"
    assert hidden["workspaces"] >= 1, "how many workspaces, never whose"
    with pytest.raises(PermissionError, match="belongs to ada"):
        portal.set_rig_visibility("rig02", "public", grace, "grace")
    with pytest.raises(ValueError, match="one of private, public, shared"):
        portal.set_rig_visibility("rig02", "everyone", ada, "ada")
    assert portal.set_rig_visibility("rig02", "public", ada, "ada") == {"name": "rig02", "visibility": "public"}
    # Sharing is the farm's to decide: at launch the shared rigs are its own.
    with pytest.raises(PermissionError, match="only the farm's admin shares"):
        portal.set_rig_visibility("rig02", "shared", ada, "ada")
    portal.set_rig_visibility("rig07", "shared", sparck, "sparck")
    assert portal.rig_visibility("rig07") == "shared"
    assert next(w for w in portal.workers_view() if w["name"] == "rig02")["visibility"] == "public"

    world = portal.world_view()
    assert [rig["name"] for rig in world["rigs"]] == ["rig02", "rig07"]
    rig = world["rigs"][0]
    assert rig["location"] == "Montréal" and rig["description"] == "the bench by the window"
    assert rig["online"] and rig["version"] == "1.0.1" and rig["boards"] == 2
    assert rig["families"] == {"esp32": 1, "esp32-c6": 1}
    assert [chip["key"] for chip in rig["setup"]] == ["boards", "network.ap", "network.broker"]
    assert rig["runs"] == {"runs": 0, "passed": 0}
    assert world["stats"] == {"rigs": 2, "online": 2, "boards": 3,
                              "families": {"esp32": 1, "esp32-c6": 1, "esp8266": 1},
                              "runs": 0, "passed": 0, "pass_rate": None,
                              # Every rig is shown by now, so counted and shown agree.
                              "shown": 2, "workspaces": 1}
    # And nothing that is anybody's, anywhere in the answer.
    said = json.dumps(world)
    for private in ("ada", "sparck", "6c:c8", "10.42", "Alteriom-HIL", "4242", "ttyUSB", "password", "owner", "config"):
        assert private not in said, private

    # Taking it back is its owner's too.
    portal.set_rig_visibility("rig02", "private", ada, "ada")
    assert [rig["name"] for rig in portal.world_view()["rigs"]] == ["rig07"]


def test_the_world_is_read_with_no_key_and_a_rigs_visibility_is_set_with_one(farm, monkeypatch):
    """`GET /api/v1/world` is the one read that carries no key -- it is for
    anyone -- and it answers before the handler looks for one."""
    import urllib.request

    with urllib.request.urlopen(farm.url + "/api/v1/world", timeout=10) as response:
        assert response.status == 200
        world = json.loads(response.read())
    assert world["rigs"] == [] and world["stats"]["rigs"] == 0 and world["window_days"] == 7
    # The public site is four pages at the root, each served through the
    # farm so its link-preview tags can carry an absolute URL: the farm's
    # public URL when it has one, else the host it was asked for. The
    # dashboard sits beside them at /app, and is the same document it was.
    pages = {"/": "home", "/rigs": "rigs", "/software": "software", "/how-it-works": "how"}
    for path, name in pages.items():
        status, _, raw = _raw(farm, "GET", path)
        page = raw.decode("utf-8")
        assert status == 200 and f'data-page="{name}"' in page, path
        assert "__ORIGIN__" not in page and '<script src="/site.js"></script>' in page, path
        assert f'content="{farm.url}{path.rstrip("/")}"' in page or f'content="{farm.url}/"' in page,             f"{path}: on loopback, the host as asked, plain http"
    status, _, raw = _raw(farm, "GET", "/how-it-works/")
    assert status == 200 and 'data-page="how"' in raw.decode("utf-8"), "a trailing slash is the same page"
    for path in ("/app", "/app/"):
        status, _, raw = _raw(farm, "GET", path)
        assert status == 200 and 'id="dashboard"' in raw.decode("utf-8"), path
    monkeypatch.setenv("ALTERIOM_HIL_PUBLIC_URL", "https://farm.example/")
    assert 'content="https://farm.example/brand/og.png"' in _raw(farm, "GET", "/")[2].decode("utf-8")
    monkeypatch.delenv("ALTERIOM_HIL_PUBLIC_URL")
    # The site's first address still lands, on the page it became -- a link
    # somebody kept is not broken by a move. Permanently, so it is not kept
    # alive by being followed.
    for old_path, now in (("/world", "/rigs"), ("/world/", "/rigs"), ("/world/software", "/software")):
        status, headers, _ = _raw(farm, "GET", old_path)
        assert (status, headers["Location"]) == (301, now), old_path
    # The rig software: what it is and which version, never the bundle. A
    # release bundle is a git bundle of this private repository, so a
    # download would publish the source (docs/device-platform-plan.md, 11).
    say = world.get("software") or {}
    assert set(say) == {"rig", "health_check"}, say
    for part in say.values():
        assert part["download"] is None, "nothing is downloadable until the package is"
        assert "sha256" not in part and "bytes" not in part, "nor anything that describes a bundle"
    # What the pages load, at the root, as files; and the brand at its first
    # open address, because a mail template points at it (docs/brand.md).
    for asset in ("site.js", "site.css", "chips.js", "app.css", "brand/favicon.svg", "brand/mark.svg",
                  "brand/og.png", "brand/site.webmanifest", "brand/app.webmanifest", "brand/logo-email.png",
                  "brand/hero-rig.webp", "brand/board-under-test.webp", "brand/fleet-rigs.webp",
                  "world/brand/logo-email.png", "world/brand/og.png"):
        status, _, raw = _raw(farm, "GET", f"/{asset}")
        assert status == 200 and len(raw) > 100, asset
    # And nothing else under the old prefix: not the pages' scripts, which
    # moved, and not a file that merely sits beside the brand.
    for refused_path in ("/world/world.js", "/world/site.js", "/world/app.js", "/world/index.html",
                         "/world/brand/notes.txt", "/world/brand/site.webmanifest"):
        assert _raw(farm, "GET", refused_path)[0] == 404, refused_path
    # A user key sets visibility on the rig it owns, through the API; on a
    # rig it does not own the answer is 403, not 500.
    farm.start_agent(_canary_pipeline(farm.node))
    _wait(lambda: any(w["name"] == "node-a" for w in farm.portal.workers_view()), what="the node to say hello")
    body = json.dumps({"visibility": "public"}).encode()
    status, answer = farm.call("POST", "/api/v1/rigs/node-a/visibility", farm.user_key, body)
    assert status == 403 and "no owner yet" in answer["error"], answer
    farm.portal.set_rig_owner("node-a", "sparck", keys=None, by="test")
    status, answer = farm.call("POST", "/api/v1/rigs/node-a/visibility", farm.user_key, body)
    assert (status, answer) == (200, {"name": "node-a", "visibility": "public"})
    status, answer = farm.call("POST", "/api/v1/rigs/node-a/visibility", farm.user_key,
                               json.dumps({"visibility": "shared"}).encode())
    assert status == 403 and "admin shares" in answer["error"], answer
    with urllib.request.urlopen(farm.url + "/api/v1/world", timeout=10) as response:
        assert [rig["name"] for rig in json.loads(response.read())["rigs"]] == ["node-a"]


# ---- accounts: a person signs in ---------------------------------------------------

def _raw(farm, method: str, path: str, *, headers: dict | None = None, body: bytes | None = None):
    """One request without a key and without following redirects: what a
    browser does on the way in. Answers (status, headers, body); every
    Set-Cookie is kept, as a list, because a sign-in sets two at once."""
    import http.client

    parsed = urllib.parse.urlparse(farm.url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    raw = response.read()
    got: dict = {"Set-Cookie": []}
    for name, value in response.getheaders():
        if name.lower() == "set-cookie":
            got["Set-Cookie"].append(value)
        else:
            got[name] = value
    connection.close()
    return response.status, got, raw


def _cookie_of(headers: dict, name: str = "farm_session") -> str:
    """The cookie a redirect set, as `name=value`, checked for the flags a
    cookie a page must never read carries."""
    for set_cookie in headers["Set-Cookie"]:
        if set_cookie.startswith(f"{name}=") and "Max-Age=0" not in set_cookie:
            assert "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie, set_cookie
            return set_cookie.split(";", 1)[0]
    raise AssertionError(f"no {name} cookie in {headers['Set-Cookie']}")


def _github_start(farm, next_path: str = "/") -> tuple[str, str]:
    """Begin a GitHub sign-in: the state GitHub will send back, and the
    cookie this browser now holds."""
    status, headers, _ = _raw(farm, "GET", f"/auth/github?next={urllib.parse.quote(next_path)}")
    assert status == 302 and headers["Location"].startswith("https://github.com/login/oauth/authorize")
    state = urllib.parse.parse_qs(urllib.parse.urlparse(headers["Location"]).query)["state"][0]
    return state, _cookie_of(headers, "farm_oauth")


def test_a_person_signs_in_with_github_or_by_email_and_is_one_account_either_way(farm, monkeypatch, tmp_path):
    """Two ways in, one account: the developer whose CI lives on GitHub clicks
    once; everyone else is sent a link that signs them in once and verifies
    the address as it does. The same verified address on both is the same
    person -- even when GitHub only says the address the second time. The
    session is a cookie the page never sees; the account's handle is what
    owns rigs and what the audit names; the farm's own people are named in
    the environment, and stop being admins when they are taken off it."""
    from alteriom_hil import signin as farm_signin

    monkeypatch.setenv("ALTERIOM_HIL_PUBLIC_URL", farm.url)
    monkeypatch.setenv("ALTERIOM_HIL_GITHUB_CLIENT_ID", "gh-id")
    monkeypatch.setenv("ALTERIOM_HIL_GITHUB_CLIENT_SECRET", "gh-secret")
    monkeypatch.setenv("ALTERIOM_HIL_NORTHRELAY_KEY", "nr_live_test")
    monkeypatch.setenv("ALTERIOM_HIL_MAIL_FROM", "farm@example.org")
    monkeypatch.setenv("ALTERIOM_HIL_SIGNIN_TEMPLATE", "tmpl_signin")
    monkeypatch.setenv("ALTERIOM_HIL_ADMINS", "Dominic, boss@example.org")
    monkeypatch.setenv("ALTERIOM_HIL_USERS", "Ada-Lovelace")
    # One address asking for a sixth link in ten minutes is not a person;
    # this test is not a person either, and asks for more than that.
    monkeypatch.setattr(core_service, "SIGNIN_REQUESTS_ALLOWED", 50)
    monkeypatch.setattr(core_service, "SIGNIN_STARTS_ALLOWED", 50)
    monkeypatch.setattr(portal_half, "SIGNIN_CODES_PER_ADDRESS", 50)

    mails = []
    # What GitHub says about Ada's addresses: nothing, until it does.
    github = {"emails": []}

    def fake_http(url, *, method="GET", body=None, headers=None, timeout=15.0):
        if url == farm_signin.GITHUB_TOKEN:
            return {"access_token": "gho_t"} if body["code"] == "good-code" else {"error": "bad_verification_code"}
        if url == farm_signin.GITHUB_API + "/user":
            return {"id": 4242, "login": "Ada-Lovelace", "name": "Ada"}
        if url == farm_signin.GITHUB_API + "/user/emails":
            return github["emails"]
        if url.endswith("/api/v1/emails/send"):
            mails.append(body)
            return {"success": True, "data": {"messageId": f"m{len(mails)}"}}
        raise AssertionError(url)

    monkeypatch.setattr(farm_signin, "http_json", fake_http)

    def by_email(address: str, next_path: str = "/") -> str:
        """The code flow, end to end, from the browser's side."""
        status, ask_headers, raw = _raw(farm, "POST", "/auth/email", headers={"Content-Type": "application/json"},
                                        body=json.dumps({"email": address, "next": next_path}).encode())
        assert (status, json.loads(raw)) == (202, {"sent": True})
        asked = _cookie_of(ask_headers, "farm_signin")
        assert "HttpOnly" in "".join(ask_headers["Set-Cookie"]) and "Path=/auth/email" in "".join(ask_headers["Set-Cookie"]), \
            "the nonce is for the sign-in routes only, and never for script"
        lowered = address.lower()
        variables = mails[-1]["variables"]
        assert mails[-1]["to"] == [{"email": lowered}], "the address as it was stored"
        assert re.fullmatch(r"[0-9]{6}", variables["code"]) and variables["expires_minutes"] == "10", variables
        assert "link" not in variables, "nothing to follow"
        code = variables["code"]
        redeem = lambda **extra: _raw(farm, "POST", "/auth/email/code", **extra)  # noqa: E731
        body = json.dumps({"email": address, "code": code}).encode()
        # The right code from a browser that did not ask is a wrong code: a
        # code phished out of somebody is no use anywhere but where they were.
        status, _, raw = redeem(headers={"Content-Type": "application/json"}, body=body)
        assert status == 403 and "not right, or has expired" in json.loads(raw)["error"], raw
        # A form on another site cannot send JSON, and it is JSON or nothing.
        status, _, raw = redeem(headers={"Cookie": asked, "Content-Type": "application/x-www-form-urlencoded"},
                                body=f"email={address}&code={code}".encode())
        assert status == 400, raw
        # A wrong code from the right browser is counted, and is the same
        # answer as every other way of being wrong.
        status, _, raw = redeem(headers={"Content-Type": "application/json", "Cookie": asked},
                                body=json.dumps({"email": address, "code": "000000" if code != "000000" else "111111"}).encode())
        assert status == 403 and "not right, or has expired" in json.loads(raw)["error"], raw
        # The right code, from the browser that asked: a session, and where
        # to go. The nonce is cleared with it.
        status, headers, raw = redeem(headers={"Content-Type": "application/json", "Cookie": asked}, body=body)
        assert status == 200, raw
        answer = json.loads(raw)
        assert answer["ok"] is True and answer["next"] == farm.portal._safe_next(next_path), answer
        assert any(c.startswith("farm_signin=;") for c in headers["Set-Cookie"]), "the nonce is spent"
        # And once only.
        status, _, raw = redeem(headers={"Content-Type": "application/json", "Cookie": asked}, body=body)
        assert status == 403, "a code is good once"
        return _cookie_of(headers)

    def whoami(cookie: str) -> dict:
        status, _, raw = _raw(farm, "GET", "/api/v1/whoami", headers={"Cookie": cookie})
        assert status == 200, raw
        return json.loads(raw)

    def sessions(cookie: str) -> list[dict]:
        status, _, raw = _raw(farm, "GET", "/api/v1/sessions", headers={"Cookie": cookie})
        assert status == 200, raw
        return json.loads(raw)["sessions"]

    def revoke(cookie: str, wanted: str):
        return _raw(farm, "POST", "/api/v1/sessions/revoke",
                    headers={"Cookie": cookie, "Content-Type": "application/json"},
                    body=json.dumps({"id": wanted}).encode())

    status, _, raw = _raw(farm, "GET", "/auth/options")
    assert (status, json.loads(raw)) == (200, {"github": True, "email": True})

    # GitHub: the browser is sent away with a state in the URL and a nonce
    # in a cookie, and comes back with a code. The sign-in needs all three:
    # a forged state, a state without its browser's cookie, and a state
    # presented twice are each refused -- and a state is spent by being
    # presented, right or wrong.
    state, held = _github_start(farm, "/#rigs")
    status, headers, _ = _raw(farm, "GET", "/auth/github/callback?code=good-code&state=forged",
                              headers={"Cookie": held})
    assert (status, headers["Location"]) == (302, "/app?signin=failed")
    status, headers, _ = _raw(farm, "GET", f"/auth/github/callback?code=good-code&state={state}")
    assert headers["Location"] == "/app?signin=failed", "a code put in front of another browser opens nothing"
    status, headers, _ = _raw(farm, "GET", f"/auth/github/callback?code=good-code&state={state}",
                              headers={"Cookie": held})
    assert headers["Location"] == "/app?signin=failed", "and that presentation spent the state"
    state, held = _github_start(farm, "/#rigs")
    status, headers, _ = _raw(farm, "GET", f"/auth/github/callback?code=good-code&state={state}",
                              headers={"Cookie": held})
    assert (status, headers["Location"]) == (302, "/#rigs"), headers
    ada = _cookie_of(headers)
    assert any(c.startswith("farm_oauth=;") for c in headers["Set-Cookie"]), "the nonce is done with"
    status, headers, _ = _raw(farm, "GET", f"/auth/github/callback?code=good-code&state={state}",
                              headers={"Cookie": held})
    assert headers["Location"] == "/app?signin=failed", "a state is good once"

    you = whoami(ada)
    assert you["name"] == "ada-lovelace" and you["role"] == "user", "named in ALTERIOM_HIL_USERS by her login"
    assert you["account"]["github_login"] == "Ada-Lovelace" and you["account"]["email"] is None
    assert "id" not in you["account"]
    assert _raw(farm, "GET", "/api/v1/whoami")[0] == 401, "no cookie, no key: nobody"

    # GitHub says the address the next time: it is attached to the account
    # that exists, so that signing in by mail later is the same person and
    # not a second account.
    # Attached under the lock account creation holds, so that a first
    # email-link sign-in for the same address cannot slip between the look
    # and the write and make a second account for the same person.
    github["emails"] = [{"email": "ada@example.org", "verified": True, "primary": True}]
    state, held = _github_start(farm)
    from alteriom_hil.api_keys import namespace_lock
    finished: list = []

    def callback():
        finished.append(_raw(farm, "GET", f"/auth/github/callback?code=good-code&state={state}",
                             headers={"Cookie": held}))

    with namespace_lock(farm.portal.state.parent / "etc" / "api-keys.yaml"):
        attaching = threading.Thread(target=callback)
        attaching.start()
        attaching.join(0.5)
        assert attaching.is_alive() and not finished, "the callback waits for the namespace"
    attaching.join(10)
    status, headers, _ = finished[0]
    assert whoami(_cookie_of(headers))["account"]["email"] == "ada@example.org"

    # Email: the same answer whether the address is known or not, a code in
    # the mail, and typing the code is the sign-in. Ada's address is Ada.
    ada_again = by_email("Ada@Example.org", "/#runs")
    assert mails[-1]["to"] == [{"email": "ada@example.org"}] and mails[-1]["content"] == {"templateId": "tmpl_signin"}
    assert mails[-1]["from"]["email"] == "farm@example.org"
    assert ada_again != ada, "a new session, not the old cookie"
    assert whoami(ada_again)["name"] == "ada-lovelace", "the same person"
    # The link routes are gone with the link: nothing under /auth/email/<x>.
    assert _raw(farm, "GET", "/auth/email/" + "x" * 40)[0] == 404
    assert _raw(farm, "POST", "/auth/email", headers={"Content-Type": "application/json"},
                body=b'{"email": "not-an-address"}')[0] == 400
    # Asking again replaces the code: the earlier one stops working, so a
    # code somebody else asked for cannot outlive the one you asked for.
    _, first_ask, _ = _raw(farm, "POST", "/auth/email", headers={"Content-Type": "application/json"},
                           body=b'{"email": "ada@example.org"}')
    stale = mails[-1]["variables"]["code"]
    _, second_ask, _ = _raw(farm, "POST", "/auth/email", headers={"Content-Type": "application/json"},
                            body=b'{"email": "ada@example.org"}')
    status, _, raw = _raw(farm, "POST", "/auth/email/code",
                          headers={"Content-Type": "application/json", "Cookie": _cookie_of(first_ask, "farm_signin")},
                          body=json.dumps({"email": "ada@example.org", "code": stale}).encode())
    assert status == 403, "the first code died when the second was asked for"
    # Five wrong guesses and the code is gone, before a sixth right one.
    asked = _cookie_of(second_ask, "farm_signin")
    real = mails[-1]["variables"]["code"]
    for guess in range(5):
        status, _, _ = _raw(farm, "POST", "/auth/email/code", headers={"Content-Type": "application/json", "Cookie": asked},
                            body=json.dumps({"email": "ada@example.org", "code": f"{(int(real) + 1 + guess) % 1000000:06d}"}).encode())
        assert status == 403
    status, _, raw = _raw(farm, "POST", "/auth/email/code", headers={"Content-Type": "application/json", "Cookie": asked},
                          body=json.dumps({"email": "ada@example.org", "code": real}).encode())
    assert status == 403, "a guesser is not given a million tries: the code went after five"

    # Where to go afterwards is a path on this site and nothing else: a
    # backslash is a slash to a browser, a control character is a second
    # header, a host is a redirect off the site. Each lands on the front page.
    for bad in ("/\\evil.example/x", "//evil.example/x", "https://evil.example/x", "/ok%0d%0aX-Injected: 1"):
        status, ask_headers, raw = _raw(farm, "POST", "/auth/email", headers={"Content-Type": "application/json"},
                                        body=json.dumps({"email": "ada@example.org", "next": urllib.parse.unquote(bad)}).encode())
        assert status == 202, raw
        status, _, raw = _raw(farm, "POST", "/auth/email/code",
                              headers={"Content-Type": "application/json", "Cookie": _cookie_of(ask_headers, "farm_signin")},
                              body=json.dumps({"email": "ada@example.org", "code": mails[-1]["variables"]["code"]}).encode())
        assert (status, json.loads(raw)["next"]) == (200, "/app"), bad
    # A path a URL may carry, in characters a header may not: the Location
    # header is Latin-1, and `/résultats/💥` sent as it was raised inside
    # send_header after the link was spent and the session made, so the
    # browser got neither. It goes percent-encoded; what a URL keeps, stays.
    assert farm.portal._safe_next("/résultats/💥?q=a b#top") == "/r%C3%A9sultats/%F0%9F%92%A5?q=a%20b#top"
    assert farm.portal._safe_next("/#rigs") == "/#rigs" and farm.portal._safe_next("/%23rigs?a=1&b=2") == "/%23rigs?a=1&b=2"
    # And when it was not told, or was told somewhere off the site: the
    # dashboard. The root is the public site now, which nobody signs in for.
    for elsewhere in (None, "", "https://evil.example/", "//evil.example", r"/\evil.example", "x" * 500):
        assert farm.portal._safe_next(elsewhere) == "/app", repr(elsewhere)
    ada_utf8 = by_email("ada@example.org", "/r%C3%A9sultats/%F0%9F%92%A5")
    assert whoami(ada_utf8)["name"] == "ada-lovelace"

    # Nor a rig's -- including one that has been added and not joined yet,
    # which has no key until it redeems its token: an account that took the
    # name first would have stranded that join for good.
    status, answer = farm.call("POST", "/api/v1/rigs", TOKEN, b'{"name": "alice"}')
    assert status in (200, 201), answer
    assert whoami(by_email("alice@example.org"))["name"] == "alice-2"
    # However old that join is, and its token expired or not: the listings
    # look at the newest five hundred, and a join older than five hundred
    # newer ones was left out of the namespace, and stranded when its
    # still-valid token was used; one whose token had expired was left out
    # too, and is renewed under the same name by a new join command.
    far = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    gone = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    farm.portal.store.add_enrollment("zed", "a" * 64, None, "admin", gone)
    with farm.portal.store.connect() as db:
        db.execute("UPDATE enrollments SET created_at='2000-01-01T00:00:00+00:00' WHERE name='zed'")
    newer = [farm.portal.store.add_enrollment(f"filler-{n}", f"{n:064d}", None, "admin", far)["id"] for n in range(501)]
    try:
        assert whoami(by_email("zed@example.org"))["name"] == "zed-2"
    finally:
        with farm.portal.store.connect() as db:
            db.execute("DELETE FROM enrollments WHERE name='zed' OR name LIKE 'filler-%'")
    # And the other way: a rig cannot be named after an account.
    status, answer = farm.call("POST", "/api/v1/rigs", TOKEN, b'{"name": "ada-lovelace"}')
    assert status == 409 and "account's handle" in answer["error"], answer
    # Nor a name a revoked key left behind: on a rig it owned, or on a run
    # still going. Who may manage the rig and who may cancel the run go by
    # the name, and an account given it would inherit both.
    farm.portal.store.set_rig_owner("old-rig", "victor", "admin")
    assert whoami(by_email("victor@example.org"))["name"] == "victor-2"
    left = farm.portal.store.create("health", {"submitted_by": "wanda"}, farm.portal.state / "logs" / "left.log")
    try:
        assert whoami(by_email("wanda@example.org"))["name"] == "wanda-2"
    finally:
        with farm.portal.store.connect() as db:
            db.execute("DELETE FROM jobs WHERE id=?", (left["id"],))

    # A keys file the farm cannot read is not an empty one: while it cannot
    # be read, nobody is named, because a handle given out on that guess is
    # a key's name the moment the file is back.
    keys_file = farm.portal.state.parent / "etc" / "api-keys.yaml"
    intact = keys_file.read_bytes()
    keys_file.write_text("keys: [this is: not: yaml\n", encoding="utf-8")
    try:
        status, asked, raw = _raw(farm, "POST", "/auth/email", headers={"Content-Type": "application/json"},
                                  body=json.dumps({"email": "late@example.org"}).encode())
        assert status == 202
        status, _, raw = _raw(farm, "POST", "/auth/email/code",
                              headers={"Content-Type": "application/json", "Cookie": _cookie_of(asked, "farm_signin")},
                              body=json.dumps({"email": "late@example.org", "code": mails[-1]["variables"]["code"]}).encode())
        assert status == 502 and "could not be completed" in json.loads(raw)["error"], raw
    finally:
        keys_file.write_bytes(intact)
    # And a file that is there, unchanged, and cannot be opened -- its
    # permissions gone while its mtime and size stayed -- is the same fact,
    # found on the second read when the first saw no reason to reopen it.
    # An empty answer there let an account take the name of a key still
    # cached and active.
    import alteriom_hil.api_keys as api_keys_module
    real_load = api_keys_module.load_keys

    def refused(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(api_keys_module, "load_keys", refused)
    status, asked, raw = _raw(farm, "POST", "/auth/email", headers={"Content-Type": "application/json"},
                              body=json.dumps({"email": "late@example.org"}).encode())
    assert status == 202
    status, _, raw = _raw(farm, "POST", "/auth/email/code",
                          headers={"Content-Type": "application/json", "Cookie": _cookie_of(asked, "farm_signin")},
                          body=json.dumps({"email": "late@example.org", "code": mails[-1]["variables"]["code"]}).encode())
    assert status == 502 and "could not be completed" in json.loads(raw)["error"], raw
    status, answer = farm.call("POST", "/api/v1/rigs", TOKEN, b'{"name": "unreadable"}')
    assert status == 409 and "cannot read its keys" in answer["error"], answer
    monkeypatch.setattr(api_keys_module, "load_keys", real_load)
    assert whoami(by_email("late@example.org"))["name"] == "late", "readable again: named again"
    assert whoami(by_email("late@example.org"))["name"] == "late", "readable again, named again"

    # Every browser signed in as you, and ending one of them. Two sign-ins by
    # the same person are two sessions of one account, which is the whole
    # reason the page exists: a phone left signed in is revoked from here.
    first = by_email("sessions@example.org")
    second = by_email("sessions@example.org")
    seen = sessions(second)
    assert len(seen) == 2, seen
    assert [row["current"] for row in seen].count(True) == 1, "only the asking browser is this one"
    assert all(len(row["id"]) == farm_service.FarmManager.SESSION_REF for row in seen), "a session is named by a digest prefix"
    assert not any("digest" in row for row in seen), "never the digest itself"

    # Another account's session id is not this account's to revoke.
    stranger = sessions(by_email("stranger@example.org"))[0]["id"]
    status, _, raw = revoke(second, stranger)
    assert status == 404, raw
    assert len(sessions(second)) == 2, "and nothing of theirs was touched"

    # Signing out everywhere else keeps the browser asking.
    status, headers, raw = revoke(second, "others")
    assert (status, json.loads(raw)["revoked"]) == (200, 1), raw
    assert not headers["Set-Cookie"], "the asking browser keeps its cookie"
    still = sessions(second)
    assert len(still) == 1 and still[0]["current"], still
    status, _, raw = _raw(farm, "GET", "/api/v1/whoami", headers={"Cookie": first})
    assert status == 401, "the other browser is signed out at once"

    # Revoking the browser asking is a sign-out, cookie and all.
    status, headers, raw = revoke(second, still[0]["id"])
    assert (status, json.loads(raw)) == (200, {"revoked": 1, "current": True}), raw
    assert any(c.startswith("farm_session=;") for c in headers["Set-Cookie"]), headers["Set-Cookie"]
    status, _, _ = _raw(farm, "GET", "/api/v1/whoami", headers={"Cookie": second})
    assert status == 401, "and that session is gone"

    # A key is not a person: it has no browsers to list or sign out.
    status, answer = farm.call("GET", "/api/v1/sessions", TOKEN)
    assert status == 400 and "not a key" in answer["error"], answer

    # A handle is never a key's name: `sparck` is the user key this farm
    # already has, and sparck@attacker.example signing up does not become
    # the principal that owns sparck's rigs and may cancel sparck's runs.
    stranger = by_email("sparck@attacker.example")
    assert (whoami(stranger)["name"], whoami(stranger)["role"]) == ("sparck-2", "user")
    # A stranger is a user of their own workspace, which is empty. This is
    # the whole of the isolation: they are answered, and what they are
    # answered with is nothing of anybody else's. The farm has rigs and runs
    # at this point in the test, and none of them are theirs.
    status, _, raw = _raw(farm, "GET", "/api/v1/rigs", headers={"Cookie": stranger})
    assert (status, json.loads(raw)["rigs"]) == (200, []), "somebody else's rigs are not theirs to list"
    status, _, raw = _raw(farm, "GET", "/api/v1/jobs", headers={"Cookie": stranger})
    page = json.loads(raw)
    assert (status, page["jobs"], page["total"]) == (200, [], 0), "nor anybody's runs, nor a count of them"
    status, _, raw = _raw(farm, "GET", "/api/v1/status", headers={"Cookie": stranger})
    dashboard = json.loads(raw)
    assert status == 200
    assert (dashboard["workers"], dashboard["jobs"], dashboard["pending_rigs"]) == ([], [], [])
    assert dashboard["inventory"]["boards"] == [], "nor the boards on them"
    assert dashboard["inventory"]["workers"] == []
    assert dashboard["queue"]["running_jobs"] == [] and dashboard["queue"]["queued"] == []
    # Whether the farm is paused is not somebody else's business to hide: it
    # is why a run of theirs would not start.
    assert "paused" in dashboard["queue"]
    # Scoping the lists is not enough: the same answer carried the farm's
    # build configuration beside them -- its own remote, each consumer's repo
    # and supply workflow, and the suite's test names. Those name the private
    # repositories that keep the artifact store shut to accounts, so a
    # workspace answer carries none of them.
    assert (dashboard["repositories"], dashboard["profiles"], dashboard["suite_tests"]) == ({}, {}, []), \
        "the farm's build configuration is not part of anybody's workspace"
    status, operator = farm.call("GET", "/api/v1/status", TOKEN)
    assert status == 200 and operator["repositories"] and operator["profiles"], \
        "and a key still gets it: this is who is asking, not a field dropped from the answer"
    status, _, raw = _raw(farm, "GET", "/api/v1/inventory", headers={"Cookie": stranger})
    assert (status, json.loads(raw)["boards"]) == (200, []), "and the board list is theirs, which is none"
    # A rig they cannot see is not found, rather than forbidden: a 403 would
    # confirm it exists, and the name is the one thing a stranger can guess.
    assert _raw(farm, "GET", "/api/v1/rigs/node-a", headers={"Cookie": stranger})[0] == 404
    status, _, raw = _raw(farm, "POST", "/api/v1/rigs/node-a/visibility",
                          headers={"Cookie": stranger, "Content-Type": "application/json"},
                          body=b'{"visibility": "public"}')
    assert status in (403, 404), "and is not theirs to show the world"
    # Publishing a rig puts it on the world page; it does not put it in
    # everybody's workspace. The world view is redacted on purpose -- no
    # configuration, no health, no board identities -- and a workspace read
    # is not, so "public" must not be a way to read what the world page
    # withholds.
    portal_manager = farm.portal
    them = farm_service.Identity(whoami(stranger)["name"], "user", kind="account")
    portal_manager.store.set_rig_details("show-me", "a bench", "somewhere", "sparck")
    portal_manager.store.set_rig_visibility("show-me", "public", "sparck")
    assert "show-me" not in (portal_manager.visible_rigs(them) or set()),         "published to the world page is not published into everybody's workspace"
    # Shared is different, and is an admin's to grant: it means others may run
    # on the rig, so they need its boards and its health.
    portal_manager.store.set_rig_visibility("show-me", "shared", "sparck")
    assert "show-me" in (portal_manager.visible_rigs(them) or set()), "shared is a rig you may use"
    # But seeing a rig and reading the runs on it are different permissions.
    # Shared means others may run ON it, so what they need is its boards and
    # its health; the runs already there are somebody's -- the farm's own CI
    # among them -- and each carries a ref, a branch, an actor, a log tail
    # and evidence from a repository the reader may have no part in.
    rigs, submitter = portal_manager.run_scope(them)
    assert "show-me" not in rigs, "a rig shared with you is not a rig whose runs are yours"
    assert submitter == them.name, "your own runs stay yours wherever they ran"
    on_the_shared_rig = uuid.uuid4().hex
    portal_manager.store.import_job(
        {"id": on_the_shared_rig, "kind": "suite", "status": "passed",
         "created_at": "2026-01-03T00:00:00+00:00", "finished_at": "2026-01-03T00:02:00+00:00",
         "request": {"profile": "painlessmesh", "submitted_by": "ci", "branch": "private-work"},
         "result": {"ok": True}},
        "show-me", tmp_path / "ci.log")
    for path in (f"/api/v1/jobs/{on_the_shared_rig}",
                 f"/api/v1/jobs/{on_the_shared_rig}/artifacts/junit"):
        assert _raw(farm, "GET", path, headers={"Cookie": stranger})[0] == 404, path
    listed = json.loads(_raw(farm, "GET", "/api/v1/jobs", headers={"Cookie": stranger})[2])
    assert on_the_shared_rig not in [run["id"] for run in listed["jobs"]] and listed["total"] == 0, \
        "nor in the list, nor in the count above it"
    # And not through the inventory either, which is the same run wearing a
    # different hat. A shared rig's boards must still read as taken -- that
    # availability is the thing sharing lends -- while the grant that took
    # them stays unnamed, or the inventory hands back what the job routes
    # just refused: the job id, the project label, the boards and the hour.
    from alteriom_hil import allocation as farm_allocation

    borrowed = farm_allocation.Grant(
        job_id=on_the_shared_rig, label="Alteriom firmware", kind="suite",
        boards=("esp32-shared",), resources=frozenset({"radio"}), shared=False,
        since="2026-01-03T00:00:00+00:00", worker="show-me")
    portal_manager._grants[borrowed.job_id] = borrowed
    try:
        seen = json.loads(_raw(farm, "GET", "/api/v1/inventory", headers={"Cookie": stranger})[2])
        assert (seen["reservations"], seen["reservation"]) == ([], None), \
            "a run on a rig lent to you is still not yours to read"
        assert not any("held_by" in board for board in seen.get("boards") or []), \
            "nor named on the board it holds"
        _, operator = farm.call("GET", "/api/v1/inventory", TOKEN)
        assert [grant["job_id"] for grant in operator["reservations"]] == [borrowed.job_id], \
            "and a key still sees it: this is who is asking"
    finally:
        portal_manager._grants.pop(borrowed.job_id, None)
    with portal_manager.store.connect() as db:
        db.execute("DELETE FROM jobs WHERE id=?", (on_the_shared_rig,))
    portal_manager.store.set_rig_visibility("show-me", "private", "sparck")

    # A run belongs to the rig that ran it -- but a queued one has no rig
    # yet: allocation gives it a worker when it starts, and `worker` is NULL
    # until then. Scoping a workspace on rig names alone therefore hid the
    # account's own runs for exactly as long as they were waiting, which is
    # when the id matters most, because cancelling needs it.
    waiting = uuid.uuid4().hex
    somebody_elses = uuid.uuid4().hex
    for job_id, who in ((waiting, them.name), (somebody_elses, "another-account")):
        portal_manager.store.import_job(
            {"id": job_id, "kind": "suite", "status": "queued",
             "created_at": "2026-01-02T00:00:00+00:00",
             "request": {"profile": "painlessmesh", "submitted_by": who}},
            None, tmp_path / f"{job_id}.log")
    page = json.loads(_raw(farm, "GET", "/api/v1/jobs", headers={"Cookie": stranger})[2])
    assert [run["id"] for run in page["jobs"]] == [waiting], \
        "their own run, waiting for a rig, is theirs to see"
    assert page["total"] == 1 and page["counts"] == {"queued": 1}, \
        "and the total and the chips count it, or paging reports somebody else's runs"
    assert _raw(farm, "GET", f"/api/v1/jobs/{waiting}", headers={"Cookie": stranger})[0] == 200
    live = json.loads(_raw(farm, "GET", "/api/v1/status", headers={"Cookie": stranger})[2])
    assert live["queue"]["queued"] == [waiting] and [run["id"] for run in live["jobs"]] == [waiting], \
        "the live panel explains the run it is there to explain"
    # And it stays theirs once it starts. Allocation gives the run whatever
    # rig the farm chose, which for an account that owns none is always
    # somebody else's -- so a rule that read "mine while it has no rig"
    # would lose them the run at the exact moment it began to matter.
    with portal_manager.store.connect() as db:
        db.execute("UPDATE jobs SET worker='node-a', status='running' WHERE id=?", (waiting,))
    assert _raw(farm, "GET", f"/api/v1/jobs/{waiting}", headers={"Cookie": stranger})[0] == 200, \
        "their run, dispatched to a rig they do not own, is still their run"
    started = json.loads(_raw(farm, "GET", "/api/v1/jobs", headers={"Cookie": stranger})[2])
    assert [run["id"] for run in started["jobs"]] == [waiting] and started["counts"] == {"running": 1}
    # Which is what makes the cancel route worth having: the dashboard
    # offers the button on exactly these runs, and the handler checks whose
    # run it is before stopping anything.
    from alteriom_hil.api_keys import allowed as may_call

    assert may_call(farm_service.Identity(them.name, "user", kind="account"),
                    "POST", f"/api/v1/jobs/{waiting}/cancel"), "and theirs to stop"
    # And a run that is not theirs is not found, rather than refused with the
    # name of whoever started it. A run id travels -- in a copied link, in a
    # CI log -- so two different answers would turn one somebody pasted into
    # a way to ask whether a run exists and whose handle is on it.
    refused = _raw(farm, "POST", f"/api/v1/jobs/{somebody_elses}/cancel",
                   headers={"Cookie": stranger, "Content-Type": "application/json"}, body=b"{}")
    assert refused[0] == 404 and "another-account" not in refused[2].decode(), \
        "a run outside the scope is not found, and the answer names nobody"
    # The other half, and the one that makes this a scoping rule rather than
    # a hole: a queued run nobody has allocated is not everybody's.
    assert _raw(farm, "GET", f"/api/v1/jobs/{somebody_elses}", headers={"Cookie": stranger})[0] == 404
    with portal_manager.store.connect() as db:
        db.execute("DELETE FROM jobs WHERE id IN (?,?)", (waiting, somebody_elses))

    # An account is not read-only: it cancels its own runs, because that
    # handler already asks whose run it is. What it may not do is the
    # farm-wide half of a user key's writes -- rediscovery commands every
    # rig, and a run is allocated boards from the whole farm -- so those stay
    # the farm's until the allocator knows whose workspace asked.
    assert allowed(farm_service.Identity("someone", "user", kind="account"),
                   "POST", f"/api/v1/jobs/{'a' * 32}/cancel"), "their own runs"
    for farm_wide in ("/api/v1/suites", "/api/v1/health", "/api/v1/inventory/refresh"):
        assert not allowed(farm_service.Identity("someone", "user", kind="account"), "POST", farm_wide), farm_wide
        assert allowed(farm_service.Identity("ci", "user"), "POST", farm_wide), f"{farm_wide}: a key still may"

    # A board's state is scoped with the board, and for the same reason. The
    # snapshot is cut to the workspace, but a grant is not a board: it names
    # the rig, the run, the project label, the boards, the resources and when
    # it started. Annotating a workspace's boards with the whole farm's
    # grants handed an account with no rigs a live account of everybody's
    # work -- from an answer whose board list was correctly empty.
    from alteriom_hil import allocation as farm_allocation

    elsewhere = farm_allocation.Grant(
        job_id=uuid.uuid4().hex, label="Alteriom firmware", kind="suite",
        boards=("esp32-aabbcc",), resources=frozenset({"radio"}), shared=False,
        since="2026-01-04T00:00:00+00:00", worker="node-a")
    portal_manager._grants[elsewhere.job_id] = elsewhere
    try:
        assert portal_manager.reservations(), "the farm is holding boards for somebody"
        for path in ("/api/v1/inventory", "/api/v1/status"):
            body = json.loads(_raw(farm, "GET", path, headers={"Cookie": stranger})[2])
            snapshot = body if path.endswith("inventory") else body["inventory"]
            assert (snapshot["reservations"], snapshot["reservation"], snapshot["in_use"]) == ([], None, 0), \
                f"{path}: somebody else's run is not a field on this caller's inventory"
        _, operator_view = farm.call("GET", "/api/v1/inventory", TOKEN)
        assert [grant["job_id"] for grant in operator_view["reservations"]] == [elsewhere.job_id], \
            "and a key still sees it: this is who is asking, not a field dropped"
    finally:
        portal_manager._grants.pop(elsewhere.job_id, None)

    # The board list opens a drill-down, and it is scoped like the list it
    # was opened from: a board on no rig of theirs is not found. It has to be
    # on the account list at all, though -- it was not, so an owner clicking
    # a board on their own rig got a 403 and a failure panel.
    assert _raw(farm, "GET", "/api/v1/inventory/esp32-nobodys/history",
                headers={"Cookie": stranger})[0] == 404, "a board on no rig of theirs"
    assert may_call(farm_service.Identity("someone", "user", kind="account"),
                    "GET", "/api/v1/inventory/esp32-aabbcc/history"), \
        "but the drill-down is a thing an account may ask for"

    # The farm's own workings stay the farm's: a read nobody has scoped to a
    # workspace is closed to an account, whatever it is (api_keys.account_route).
    # The artifact store is on this list deliberately: it holds consumer
    # firmware built from private repositories, named by repository, ref and
    # actor, so it needs an owner to filter on before an account may read it.
    for closed in ("/api/v1/keys", "/api/v1/audit", "/api/v1/config", "/api/v1/artifacts",
                   "/api/v1/stats", "/api/v1/capacity", "/api/v1/webhooks"):
        assert _raw(farm, "GET", closed, headers={"Cookie": stranger})[0] == 403, closed
    assert _raw(farm, "GET", "/api/v1/whoami", headers={"Cookie": stranger})[0] == 200
    assert _raw(farm, "GET", "/api/v1/world")[0] == 200, "the world page is for anyone"

    # What a run produced follows the run. Somebody else's is not found --
    # the same answer its detail gives, so the download is not a way round it.
    theirs = uuid.uuid4().hex
    portal_manager.store.import_job(
        {"id": theirs, "kind": "suite", "status": "passed",
         "created_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:01:00+00:00",
         "request": {"profile": "painlessmesh"}, "result": {"ok": True}},
        "node-a", tmp_path / "somebody-elses.log")
    _, every_run = farm.call("GET", "/api/v1/jobs", TOKEN)
    assert theirs in [run["id"] for run in every_run["jobs"]], "the farm has the run"
    assert _raw(farm, "GET", f"/api/v1/jobs/{theirs}", headers={"Cookie": stranger})[0] == 404
    assert _raw(farm, "GET", f"/api/v1/jobs/{theirs}/artifacts/junit",
                headers={"Cookie": stranger})[0] == 404, "nor what it produced"

    # A rig's subscriptions are its owner's: the URL one points at is whoever
    # set it up's. node-a is not theirs, so they neither read one nor add one.
    # The owner doing both is further down, and is a 200.
    assert _raw(farm, "GET", "/api/v1/rigs/node-a/webhooks", headers={"Cookie": stranger})[0] == 403
    assert _raw(farm, "POST", "/api/v1/rigs/node-a/webhooks",
                headers={"Cookie": stranger, "Content-Type": "application/json"},
                body=json.dumps({"name": "theirs", "url": "https://x.example.invalid/h"}).encode(),
                )[0] in (403, 404), "and node-a's events are not theirs to redirect"

    # A new address is a new account, named after it; the farm's own people
    # are admins by being named -- and only while they are.
    boss = by_email("boss@example.org")
    assert (whoami(boss)["name"], whoami(boss)["role"]) == ("boss", "admin")
    monkeypatch.setenv("ALTERIOM_HIL_ADMINS", "ada@example.org")
    assert whoami(by_email("boss@example.org"))["role"] == "user",         "taken off the admins list, an ordinary user of their own workspace -- the list is the fact"
    assert whoami(by_email("ada@example.org"))["role"] == "admin", "put on it, an admin from the next sign-in"
    assert whoami(boss)["role"] == "user", "the session that was open sees the demotion too"
    # There is no second list. ALTERIOM_HIL_USERS was read once and never
    # consulted, which is worse than absent: an operator who set it, as the
    # deployment and the docs told them to, believed a door was locked that
    # had no lock in it. The plumbing is gone, and setting it changes nothing
    # -- in either direction, which is what makes this an assertion and not
    # a coincidence.
    monkeypatch.setenv("ALTERIOM_HIL_USERS", "Ada-Lovelace, boss@example.org")
    assert whoami(by_email("boss@example.org"))["role"] == "user", "on the old list: a user"
    assert "users" not in farm.portal.signin_setup(), "and nothing reads it any more"
    monkeypatch.setenv("ALTERIOM_HIL_USERS", "nobody@example.org")
    assert whoami(by_email("boss@example.org"))["role"] == "user", \
        "off it: still a user, because signing in is the whole of it"
    monkeypatch.setenv("ALTERIOM_HIL_ADMINS", "boss@example.org")
    boss = by_email("boss@example.org")

    # A session administers what its handle owns, through the API, sending
    # JSON -- which is what tells the farm the request is the page's and not
    # another site's form.
    farm.start_agent(_canary_pipeline(farm.node))
    _wait(lambda: any(w["name"] == "node-a" for w in farm.portal.workers_view()), what="the node to say hello")

    # Nor is it a look inside somebody else's bench. A lent rig's page shows
    # what the rig list shows -- is it up, what can it run, are its boards
    # free -- and not the owner's host configuration, their recent commands
    # with the arguments they passed, the rig's diagnostics or its faults.
    portal_manager.store.set_rig_visibility("node-a", "shared", "sparck")
    try:
        lent = json.loads(_raw(farm, "GET", "/api/v1/rigs/node-a", headers={"Cookie": stranger})[2])
        assert lent.get("lent") is True and lent["name"] == "node-a"
        for withheld in ("config", "commands", "setup", "inventory"):
            assert withheld not in lent, f"{withheld} is the owner's, not the borrower's"
        assert not isinstance(lent.get("health"), dict), \
            "health comes back as the one word the rig list already carries"
        _, whole = farm.call("GET", "/api/v1/rigs/node-a", TOKEN)
        assert "config" in whole and "commands" in whole, \
            "an operator's key still gets the rig whole: this is who is asking"

        # The health block is a second door onto the same diagnostics. A
        # drained rig's check says who drained it and why, and an account
        # that may run on the rig needs only to know it will not take work.
        portal_manager.store.set_drained("node-a", {"by": "sparck", "reason": "bench work on the antenna"})
        borrowed = json.loads(_raw(farm, "GET", "/api/v1/status", headers={"Cookie": stranger})[2])
        said = [c for c in borrowed["health"]["checks"] if c["name"] == "worker node-a"]
        assert said and "sparck" not in said[0]["message"] and "antenna" not in said[0]["message"], \
            f"who drained a lent rig and why is the owner's: {said}"
        assert said[0]["message"] == "not taking work", said
        _, operator_health = farm.call("GET", "/api/v1/status", TOKEN)
        assert any("sparck" in c["message"] for c in operator_health["health"]["checks"]), \
            "and a key is still told, or this is a field dropped rather than a caller scoped"

        # And the board annotations beside it. A canary record names the run
        # that wrote it and the consumer revision it flashed; a hold names
        # who took the board out of the pool and for what. The verdict and
        # the state stay, because they are why the board will not take a run.
        board = next(b["id"] for b in json.loads(
            _raw(farm, "GET", "/api/v1/inventory", headers={"Cookie": stranger})[2])["boards"])
        portal_manager.board_health_path.write_text(json.dumps({board: {
            "id": board, "verdict": "failed", "checked_at": "2026-01-05T00:00:00+00:00",
            "job_id": "f" * 32, "canary_revision": "deadbeefcafe", "checks": {}}}), encoding="utf-8")
        portal_manager.store.set_hold(board, "quarantined", "radio join keeps failing", "sparck", "f" * 32)
        try:
            seen = next(b for b in json.loads(
                _raw(farm, "GET", "/api/v1/inventory", headers={"Cookie": stranger})[2])["boards"]
                if b["id"] == board)
            assert seen["health"]["verdict"] == "failed" and seen["state"] == "quarantined", \
                "what is wrong with the board is the board's, and a borrower needs it"
            assert "job_id" not in seen["health"] and "canary_revision" not in seen["health"], seen["health"]
            assert seen["hold"]["state"] == "quarantined" and "since" in seen["hold"]
            for withheld in ("by", "reason", "job_id"):
                assert withheld not in seen["hold"], f"{withheld} is the owner's bench"
            drill = json.loads(_raw(farm, "GET", f"/api/v1/inventory/{board}/history",
                                    headers={"Cookie": stranger})[2])
            assert all(f not in drill["hold"] for f in ("by", "reason", "job_id")), drill["hold"]
            _, operator_boards = farm.call("GET", "/api/v1/inventory", TOKEN)
            his = next(b for b in operator_boards["boards"] if b["id"] == board)
            assert his["hold"]["by"] == "sparck" and his["health"]["job_id"] == "f" * 32, \
                "a key still reads the bench whole"

            # A rig that has gone quiet still has an owner and still has its
            # boards. The live snapshot rightly lists none of them -- an
            # offline rig's boards cannot be given to anyone -- but that is
            # an availability answer, and it was being asked whose board this
            # is. So the history 404'd exactly when a rig went down, which is
            # when it is most worth reading.
            was = portal_half.WORKER_STALE_SECONDS
            monkeypatch.setattr(portal_half, "WORKER_STALE_SECONDS", -1)
            try:
                assert not any(w["online"] for w in portal_manager.workers_view()), "the rig is quiet"
                assert not json.loads(_raw(farm, "GET", "/api/v1/inventory",
                                           headers={"Cookie": stranger})[2])["boards"], \
                    "and its boards are listed nowhere, which is the right availability answer"
                assert _raw(farm, "GET", f"/api/v1/inventory/{board}/history",
                            headers={"Cookie": stranger})[0] == 200, \
                    "a board does not change hands when its rig goes quiet"
            finally:
                monkeypatch.setattr(portal_half, "WORKER_STALE_SECONDS", was)
        finally:
            portal_manager.store.release_hold(board)
            portal_manager.board_health_path.write_text("{}", encoding="utf-8")
    finally:
        portal_manager.store.set_drained("node-a", None)
        portal_manager.store.set_rig_visibility("node-a", "private", "sparck")
    payload = json.dumps({"owner": "ada-lovelace"}).encode()
    status, _, raw = _raw(farm, "POST", "/api/v1/rigs/node-a/owner", headers={"Cookie": boss}, body=payload)
    assert status == 403 and "sends JSON" in json.loads(raw)["error"]
    status, _, raw = _raw(farm, "POST", "/api/v1/rigs/node-a/owner",
                          headers={"Cookie": boss, "Content-Type": "application/json"}, body=payload)
    assert (status, json.loads(raw)) == (200, {"name": "node-a", "owner": "ada-lovelace"})
    status, _, raw = _raw(farm, "POST", "/api/v1/rigs/node-a/webhooks",
                          headers={"Cookie": ada_again, "Content-Type": "application/json"},
                          body=json.dumps({"name": "ada's", "url": "https://ada.example.invalid/h"}).encode())
    assert status == 200, raw
    entries = farm.portal.store.audit_page(5, 0)["entries"]
    assert any(entry["key_name"] == "ada-lovelace" and entry["path"].endswith("/webhooks") for entry in entries), \
        "the audit names the account, not the cookie"

    # Signing out ends the session; the cookie is worth nothing after.
    status, headers, _ = _raw(farm, "POST", "/auth/signout", headers={"Cookie": ada_again})
    assert status == 200 and any(c.startswith("farm_session=;") for c in headers["Set-Cookie"])
    assert _raw(farm, "GET", "/api/v1/whoami", headers={"Cookie": ada_again})[0] == 401

    # A key still works exactly as before, beside all of this.
    assert farm.call("GET", "/api/v1/whoami", farm.user_key)[1]["name"] == "sparck"


def test_a_signed_in_poll_reads_its_session_once_and_does_not_rewrite_last_seen_every_time(farm, monkeypatch):
    """A browser polls the live dashboard every few seconds (web/app.js). Each
    poll identifies the caller and builds `you.account` from the same row, so a
    poll is one session lookup and not two; and last_seen_at is advanced at most
    once a minute, so a burst of polls is not a burst of write locks on the
    session and account rows.
    """
    from alteriom_hil import signin as farm_signin

    store = farm.portal.store
    account = store.create_account("poller", role="user")
    token = farm_signin.new_token()
    until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    store.create_session(farm_signin.digest(token), account["id"], until, None)
    cookie = f"farm_session={token}"

    lookups = {"n": 0}
    real = store.session_account

    def counting(digest, now):
        lookups["n"] += 1
        return real(digest, now)

    monkeypatch.setattr(store, "session_account", counting)

    # whoami and the status page each authenticate and then answer `you.account`
    # from the row the authentication already fetched -- one lookup, not two.
    for path in ("/api/v1/whoami", "/api/v1/status"):
        lookups["n"] = 0
        status, _, raw = _raw(farm, "GET", path, headers={"Cookie": cookie})
        assert status == 200, raw
        you = json.loads(raw)["you"] if path.endswith("status") else json.loads(raw)
        assert you["name"] == "poller" and you["account"]["handle"] == "poller"
        assert "id" not in you["account"], "the account id never leaves the store"
        assert lookups["n"] == 1, f"{path} reads the session once, not once to authenticate and again for the payload"

    # A burst of polls inside the minute does not rewrite last_seen_at each time.
    before = store.account("id", account["id"])["last_seen_at"]
    for _ in range(10):
        assert _raw(farm, "GET", "/api/v1/whoami", headers={"Cookie": cookie})[0] == 200
    assert store.account("id", account["id"])["last_seen_at"] == before, \
        "a burst of polls inside the minute leaves last_seen_at where it was"


def test_a_link_and_a_state_are_each_spent_by_exactly_one_request(tmp_path):
    """Two requests following the same link at once both read it unused
    and both got in: the update now carries the test, and only the request
    whose update changed the row has the link. A state, likewise, is spent
    by whoever presents it first, and only in the browser that began it."""
    import threading
    from datetime import datetime, timedelta, timezone

    from alteriom_hil import signin as farm_signin

    utcnow = farm_service.utcnow

    portal = farm_service.manager_for("portal")(
        REPO, tmp_path / "portal", tmp_path / "none.yaml", tmp_path / "none-map.yaml",
        Path(sys.executable), mode="portal",
    )
    later = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    portal.store.create_signin_code("code-digest", "ada@example.org", "browser-digest", later, None, "/")
    got, starting = [], threading.Barrier(8)

    def follow():
        starting.wait()
        got.append(portal.store.consume_signin_code("ada@example.org", "code-digest", "browser-digest", utcnow()))

    threads = [threading.Thread(target=follow) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(1 for row in got if row) == 1, got
    assert portal.store.consume_signin_code("ada@example.org", "code-digest", "browser-digest", utcnow()) is None

    # What was started and never finished does not pile up: the next start
    # sweeps every expired state and link before it writes its own.
    earlier = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    portal.store.create_oauth_state("stale", farm_signin.digest("x"), earlier, "/")
    portal.store.create_signin_code("stale-code", "old@example.org", "browser-digest", earlier, None, "/")
    monkeypatch_env = {"ALTERIOM_HIL_PUBLIC_URL": "https://farm.example",
                       "ALTERIOM_HIL_GITHUB_CLIENT_ID": "id", "ALTERIOM_HIL_GITHUB_CLIENT_SECRET": "s"}
    import os
    saved = {name: os.environ.get(name) for name in monkeypatch_env}
    os.environ.update(monkeypatch_env)
    try:
        portal.begin_github("/")
    finally:
        for name, value in saved.items():
            os.environ.pop(name, None) if value is None else os.environ.__setitem__(name, value)
    with portal.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM oauth_states WHERE state='stale'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM signin_codes WHERE digest='stale-code'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM oauth_states").fetchone()[0] == 1, "only the one just begun"

    portal.store.create_oauth_state("st", farm_signin.digest("nonce"), later, "/")
    assert portal.store.consume_oauth_state("st", farm_signin.digest("other"), utcnow()) is None
    assert portal.store.consume_oauth_state("st", farm_signin.digest("nonce"), utcnow()) is None, \
        "presented from the wrong browser, the state was spent all the same"
    portal.store.create_oauth_state("st2", farm_signin.digest("nonce"), later, "/#rigs")
    assert portal.store.consume_oauth_state("st2", farm_signin.digest("nonce"), utcnow())["next"] == "/#rigs"
    assert portal.store.consume_oauth_state("st2", farm_signin.digest("nonce"), utcnow()) is None

    # Two people with the same local part following their links at once
    # both saw `admin` free; the second insert failed on the unique handle
    # after its one-time link was spent. Each gets an account now.
    made, starting = [], threading.Barrier(6)

    def sign_up(n):
        starting.wait()
        made.append(portal._create_account("admin", None, email=f"admin@{n}.example", email_verified=True,
                                           role="guest"))

    threads = [threading.Thread(target=sign_up, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(made) == 6 and all(created for _, created in made)
    assert len({account["handle"] for account, _ in made}) == 6, made
    assert all(account["handle"].startswith("admin") for account, _ in made)

    # And the same person twice at once -- one link followed in two tabs,
    # two GitHub round trips for one new login -- is one account: the
    # insert that loses on the email or the GitHub id finds the account the
    # other made and signs in with it, rather than dying after the one-time
    # credential was spent.
    same, starting = [], threading.Barrier(4)

    def same_person():
        starting.wait()
        same.append(portal._create_account("grace", None, email="grace@example.org", email_verified=True,
                                           github_id=777, github_login="grace", role="guest"))

    threads = [threading.Thread(target=same_person) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({account["id"] for account, _ in same}) == 1, same
    assert sum(1 for _, created in same if created) == 1

    # An email-only sign-up that wins against a GitHub callback for the
    # same address keeps what the loser knew: the account has the GitHub
    # id, login and name afterwards, whichever insert went first.
    first, _ = portal._create_account("hal", None, email="hal@example.org", email_verified=True, role="guest")
    merged, created = portal._create_account("hal", None, email="hal@example.org", email_verified=True,
                                             github_id=9001, github_login="HAL-9000", display_name="Hal", role="guest")
    assert not created and merged["id"] == first["id"]
    assert (merged["github_id"], merged["github_login"], merged["display_name"]) == (9001, "HAL-9000", "Hal")

    # And the namespace across both stores at once: sign-ups for `bob` and
    # keys named `bob` racing each other end with exactly one principal
    # called bob, because the check and the write share one file lock.
    pytest.importorskip("fcntl")
    from alteriom_hil.api_keys import KeyStore, write_keys

    keys_file = tmp_path / "api-keys.yaml"
    write_keys(keys_file, [])
    keys = KeyStore("t" * 40, keys_file, reserved=portal.account_handles)
    outcomes, starting = [], threading.Barrier(6)

    def make_key():
        starting.wait()
        try:
            keys.create("bob", "user")
            outcomes.append("key")
        except ValueError as refused:
            outcomes.append(f"key refused: {refused}")

    def sign_up_bob(n):
        starting.wait()
        account, _ = portal._create_account("bob", keys, email=f"bob@{n}.example", email_verified=True, role="guest")
        outcomes.append(f"account {account['handle']}")

    threads = [threading.Thread(target=make_key) for _ in range(3)] + \
        [threading.Thread(target=sign_up_bob, args=(n,)) for n in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    named_bob = [o for o in outcomes if o in ("key", "account bob")]
    assert len(named_bob) == 1, outcomes

    # A rig being added under a name, and a sign-up for the same name, at
    # once: the enrolment's check and write hold the same lock, so exactly
    # one of them is `carol` and the other is refused or renamed -- a rig
    # that kept a handle's name could never redeem its token.
    outcomes, starting = [], threading.Barrier(6)

    def add_rig():
        starting.wait()
        try:
            portal.create_rig({"name": "carol"}, "sparck", keys)
            outcomes.append("rig")
        except farm_service.ElsewhereError as refused:
            outcomes.append(f"rig refused: {refused}")

    def sign_up_carol(n):
        starting.wait()
        account, _ = portal._create_account("carol", keys, email=f"carol@{n}.example", email_verified=True, role="guest")
        outcomes.append(f"account {account['handle']}")

    threads = [threading.Thread(target=add_rig) for _ in range(3)] +         [threading.Thread(target=sign_up_carol, args=(n,)) for n in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len([o for o in outcomes if o in ("rig", "account carol")]) == 1, outcomes

    # A rig redeeming its join token, and a sign-up for its name, at once:
    # the claim moves the join from waiting to used before its key exists,
    # so without the lock a sign-up in that gap would see no waiting join
    # and no key and take the name, leaving the token unable to make a key.
    # The claim, the key and the check are one critical section.
    import hashlib

    _, release_commit, release_body = _release(tmp_path, commits=1)
    portal.publish_release(release_commit, release_body, "ci")
    dave_token = portal.create_rig({"name": "dave"}, "sparck", keys)["token"]
    outcomes, starting = [], threading.Barrier(4)

    def redeem_dave():
        starting.wait()
        try:
            portal.redeem_enrollment({"token": dave_token, "hostname": "h"}, "1.2.3.4", keys)
            outcomes.append("rig")
        except (farm_service.ElsewhereError, PermissionError) as refused:
            outcomes.append(f"rig refused: {refused}")

    def sign_up_dave(n):
        starting.wait()
        account, _ = portal._create_account("dave", keys, email=f"dave@{n}.example", email_verified=True, role="guest")
        outcomes.append(f"account {account['handle']}")

    threads = [threading.Thread(target=redeem_dave)] +         [threading.Thread(target=sign_up_dave, args=(n,)) for n in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len([o for o in outcomes if o in ("rig", "account dave")]) == 1, outcomes
    view = portal._enrollment_view(portal.store.enrollment_by_token(hashlib.sha256(dave_token.encode()).hexdigest()))
    if "rig" in outcomes:
        assert view["status"] in ("installing", "joined") and "dave" in {e["name"] for e in keys.entries()}
    else:
        assert view["status"] == "waiting", "a redeem that lost the name leaves its token good, not stranded used"

    # And a rig getting a fresh join token races a sign-up the same way:
    # renewing revokes the key and sets the join back to waiting, and the
    # gap between is held shut by the same lock.
    portal.create_rig({"name": "erin"}, "sparck", keys)
    outcomes, starting = [], threading.Barrier(4)

    def renew_erin():
        starting.wait()
        try:
            portal.new_join_token("erin", "sparck", keys)
            outcomes.append("rig")
        except farm_service.ElsewhereError as refused:
            outcomes.append(f"rig refused: {refused}")

    def sign_up_erin(n):
        starting.wait()
        account, _ = portal._create_account("erin", keys, email=f"erin@{n}.example", email_verified=True, role="guest")
        outcomes.append(f"account {account['handle']}")

    threads = [threading.Thread(target=renew_erin)] +         [threading.Thread(target=sign_up_erin, args=(n,)) for n in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len([o for o in outcomes if o in ("rig", "account erin")]) == 1, outcomes


def test_a_portal_with_no_sign_in_set_up_says_so_and_offers_a_key(farm, monkeypatch):
    for name in ("ALTERIOM_HIL_PUBLIC_URL", "ALTERIOM_HIL_GITHUB_CLIENT_ID", "ALTERIOM_HIL_NORTHRELAY_KEY"):
        monkeypatch.delenv(name, raising=False)
    status, _, raw = _raw(farm, "GET", "/auth/options")
    assert (status, json.loads(raw)) == (200, {"github": False, "email": False})
    assert _raw(farm, "GET", "/auth/github")[0] == 409
    # Starting a sign-in is free to the caller and writes a row: an address
    # that starts one after another is stopped, well before the table minds.
    monkeypatch.setattr(core_service, "SIGNIN_STARTS_ALLOWED", 3)
    assert [_raw(farm, "GET", "/auth/github")[0] for _ in range(4)][-1] == 429
    status, _, raw = _raw(farm, "POST", "/auth/email", headers={"Content-Type": "application/json"},
                          body=b'{"email": "ada@example.org"}')
    assert status == 409 and "not set up" in json.loads(raw)["error"]
    assert _raw(farm, "GET", "/auth/email/" + "x" * 40)[0] == 404, "there is no link to follow any more"


def test_the_sign_in_throttle_forgets_refused_attempts_and_quiet_addresses():
    """A limiter that remembers every refused request is a limiter the
    refused can grow: an address past its allowance that keeps asking would
    lengthen its own list for the whole window, and addresses that stopped
    asking would stay in the book for good. At most the allowance is kept
    per address, refusals add nothing, and the quiet are swept."""
    throttle = farm_service.AddressThrottle(window=600.0, sweep_every=10)
    for n in range(5):
        assert not throttle.too_many("1.2.3.4", 5, now=100.0 + n)
    for _ in range(500):
        assert throttle.too_many("1.2.3.4", 5, now=110.0)
    assert len(throttle._book["1.2.3.4"]) == 5, "five hundred refusals added nothing"
    # The window passing frees the address; a lowered allowance is read at the call.
    assert not throttle.too_many("1.2.3.4", 5, now=800.0)
    assert throttle.too_many("1.2.3.4", 1, now=801.0)
    # Addresses that went quiet are swept out on the callers' time.
    for n in range(30):
        throttle.too_many(f"10.0.0.{n}", 5, now=1000.0)
    assert throttle.tracked() >= 30
    for n in range(20):
        throttle.too_many("5.5.5.5", 5, now=2000.0 + n)
    assert throttle.tracked() <= 2, "everything older than the window is gone; only the live address stays"


def _github_env(farm, monkeypatch):
    """The environment a GitHub-and-email portal runs with, and an http
    double whose GitHub identity a test sets: what id and login the callback
    reports, and what addresses /user/emails returns."""
    from alteriom_hil import signin as farm_signin

    monkeypatch.setenv("ALTERIOM_HIL_PUBLIC_URL", farm.url)
    monkeypatch.setenv("ALTERIOM_HIL_GITHUB_CLIENT_ID", "gh-id")
    monkeypatch.setenv("ALTERIOM_HIL_GITHUB_CLIENT_SECRET", "gh-secret")
    monkeypatch.setenv("ALTERIOM_HIL_NORTHRELAY_KEY", "nr_live_test")
    monkeypatch.setenv("ALTERIOM_HIL_MAIL_FROM", "farm@example.org")
    monkeypatch.setenv("ALTERIOM_HIL_SIGNIN_TEMPLATE", "tmpl_signin")
    monkeypatch.setattr(core_service, "SIGNIN_REQUESTS_ALLOWED", 50)
    monkeypatch.setattr(core_service, "SIGNIN_STARTS_ALLOWED", 50)
    monkeypatch.setattr(portal_half, "SIGNIN_CODES_PER_ADDRESS", 50)

    who = {"id": 1, "login": "octo", "name": "Octo", "emails": []}
    mails = []

    def fake_http(url, *, method="GET", body=None, headers=None, timeout=15.0):
        if url == farm_signin.GITHUB_TOKEN:
            return {"access_token": "gho_t"} if body["code"] == "good-code" else {"error": "bad_verification_code"}
        if url == farm_signin.GITHUB_API + "/user":
            return {"id": who["id"], "login": who["login"], "name": who["name"]}
        if url == farm_signin.GITHUB_API + "/user/emails":
            return who["emails"]
        if url.endswith("/api/v1/emails/send"):
            mails.append(body)
            return {"success": True, "data": {"messageId": f"m{len(mails)}"}}
        raise AssertionError(url)

    monkeypatch.setattr(farm_signin, "http_json", fake_http)
    return who, mails


def _github_signin(farm):
    """One whole GitHub sign-in with the double's current identity: the
    session cookie it hands back."""
    state, held = _github_start(farm)
    status, headers, _ = _raw(farm, "GET", f"/auth/github/callback?code=good-code&state={state}",
                              headers={"Cookie": held})
    assert status == 302, headers
    return _cookie_of(headers)


def _whoami(farm, cookie: str) -> dict:
    status, _, raw = _raw(farm, "GET", "/api/v1/whoami", headers={"Cookie": cookie})
    assert status == 200, raw
    return json.loads(raw)


def test_a_github_role_is_bound_to_a_stable_id_not_a_recyclable_login(farm, monkeypatch):
    """A GitHub login is a name its owner can change and another can then
    register, so admin is named by a stable id (github:<id>) or an email,
    which an operator pins knowing who they are -- never by a bare login,
    which a person who never signed in cannot be told apart from the one who
    took their freed name. A bare login lets a person read (a `user`), and
    only while the id that signs in owns it."""
    who, _ = _github_env(farm, monkeypatch)
    # Admin by the stable id: the login may be anything, even one recycled.
    monkeypatch.setenv("ALTERIOM_HIL_ADMINS", "github:1000")
    monkeypatch.setenv("ALTERIOM_HIL_USERS", "root-dev")

    who.update(id=1000, login="root-dev", name="The Admin")
    assert _whoami(farm, _github_signin(farm))["role"] == "admin", "admin by the id it is pinned to"

    # Someone else registers the freed login. Same name, different id: not
    # the admin (the id is not theirs), and not even the user the login would
    # grant, because the login is the first id's now.
    who.update(id=2000, login="root-dev", name="Impostor")
    stranger = _whoami(farm, _github_signin(farm))
    assert stranger["role"] == "user", "signing in is enough to own a workspace"
    assert stranger["role"] != "admin", "but a recycled login inherits nothing: admin is named by id"
    assert stranger["name"] == "root-dev-2"
    assert farm.portal.store.account("handle", "root-dev-2")["github_id"] == 2000

    # A bare login on the admins list is not admin -- it is the softer role,
    # a farm-wide read, not the farm's own keys.
    monkeypatch.setenv("ALTERIOM_HIL_ADMINS", "chief-by-name")
    monkeypatch.delenv("ALTERIOM_HIL_USERS", raising=False)
    who.update(id=3000, login="chief-by-name", name="Named, not pinned")
    assert _whoami(farm, _github_signin(farm))["role"] == "user", "a bare login never reaches admin"

    # There is no users list to be on any more: whoever signs in is a user of
    # their own workspace, and the login they used decides nothing.
    monkeypatch.delenv("ALTERIOM_HIL_ADMINS", raising=False)
    who.update(id=4000, login="reader", name="First Reader")
    assert _whoami(farm, _github_signin(farm))["role"] == "user"
    who.update(id=5000, login="reader", name="Second Comer")
    assert _whoami(farm, _github_signin(farm))["role"] == "user", "and so is whoever takes the name next"


def test_a_github_account_and_an_email_account_for_one_person_are_merged(farm, monkeypatch):
    """A GitHub sign-in with no address scope makes an account with no email;
    an email link for that same person then makes a second. When GitHub next
    vouches the address, the two are one person and are folded together: the
    email account's rig, its running run and its session all come across to
    the GitHub account, and the address is attached, leaving one handle."""
    who, mails = _github_env(farm, monkeypatch)
    monkeypatch.setenv("ALTERIOM_HIL_USERS", "grace@example.org")

    # GitHub first, no address: a GitHub-only account.
    who.update(id=42, login="grace", name="Grace", emails=[])
    gh = _github_signin(farm)
    gh_handle = _whoami(farm, gh)["name"]
    gh_account = farm.portal.store.account("handle", gh_handle)
    assert gh_account["email"] is None and gh_account["github_id"] == 42

    # An email code for the same person: a second, email-only account, and
    # it comes to own a rig, start a run, and hold its own session.
    email_cookie = _sign_in_by_email(farm, mails, "grace@example.org")
    email_account = farm.portal.store.account("handle", _whoami(farm, email_cookie)["name"])
    assert email_account["id"] != gh_account["id"] and email_account["email"] == "grace@example.org"
    farm.portal.store.set_rig_owner("her-rig", email_account["handle"], "admin")
    left = farm.portal.store.create("health", {"submitted_by": email_account["handle"]},
                                    farm.portal.state / "logs" / "hers.log")

    # GitHub vouches the address this time: one person, and one account after.
    who["emails"] = [{"email": "grace@example.org", "verified": True, "primary": True}]
    merged = farm.portal.store.account("handle", _whoami(farm, _github_signin(farm))["name"])
    assert merged["id"] == gh_account["id"] and merged["email"] == "grace@example.org"
    assert farm.portal.store.account("email", "grace@example.org")["id"] == gh_account["id"]
    assert farm.portal.store.account("id", email_account["id"]) is None, "the second row is gone"
    # The rig, the run and the session the email account held are the GitHub
    # account's now, not stranded under a handle no one can sign in as.
    assert (farm.portal.store.rig_details().get("her-rig") or {}).get("owner") == gh_account["handle"]
    assert (farm.portal.store.get(left["id"])["request"]).get("submitted_by") == gh_account["handle"]
    assert farm.portal.store.account("handle", _whoami(farm, email_cookie)["name"])["id"] == gh_account["id"],         "the old session follows the person"
    with farm.portal.store.connect() as db:
        db.execute("DELETE FROM jobs WHERE id=?", (left["id"],))


def _post_email(farm, address: str, next_path: str = "/", real_ip: str | None = None):
    """Ask for a link: the same 202 whatever the answer. `real_ip` sets the
    caller the request appears to come from, so a test can vary the caller
    (the IP throttle) independently of the recipient (the address bucket)."""
    headers = {"Content-Type": "application/json"}
    if real_ip is not None:
        headers["X-Real-IP"] = real_ip
    status, _, raw = _raw(farm, "POST", "/auth/email", headers=headers,
                          body=json.dumps({"email": address, "next": next_path}).encode())
    assert (status, json.loads(raw)) == (202, {"sent": True}), "always the same answer"
    return status


def _sign_in_by_email(farm, mails, address: str) -> str:
    """Ask for a code, read it from the captured mail, and type it back from
    the browser that asked -- the nonce cookie the ask set is what makes it
    that browser. The session cookie is the answer."""
    _, ask_headers, _ = _raw(farm, "POST", "/auth/email", headers={"Content-Type": "application/json"},
                             body=json.dumps({"email": address, "next": "/app"}).encode())
    asked = _cookie_of(ask_headers, "farm_signin")
    code = mails[-1]["variables"]["code"]
    status, headers, raw = _raw(farm, "POST", "/auth/email/code",
                                headers={"Content-Type": "application/json", "Cookie": asked},
                                body=json.dumps({"email": address, "code": code}).encode())
    assert status == 200 and json.loads(raw)["ok"] is True, raw
    return _cookie_of(headers)


def test_a_changed_github_email_is_reconciled_not_split(farm, monkeypatch):
    """When GitHub changes the primary verified address it reports, the
    account keeps up with it: a second account made by an email link for the
    new address is folded in, the account's address becomes the new one, and
    the address GitHub dropped no longer signs this account in -- so a mailbox
    its owner gave up cannot be reassigned into their rigs and role."""
    who, mails = _github_env(farm, monkeypatch)
    monkeypatch.setattr(portal_half, "SIGNIN_CODES_PER_ADDRESS", 50)
    store = farm.portal.store

    who.update(id=9, login="dev", name="Dev", emails=[{"email": "old@corp.example", "verified": True, "primary": True}])
    _github_signin(farm)
    account_id = store.account("github_id", 9)["id"]
    assert store.account("github_id", 9)["email"] == "old@corp.example"

    # The person signs in by a link to their new address before GitHub says
    # it: a second, email-only account for the new address.
    new_cookie = _sign_in_by_email(farm, mails, "new@corp.example")
    assert store.account("email", "new@corp.example")["id"] != account_id

    # GitHub now reports the new verified primary: one account after.
    who["emails"] = [{"email": "new@corp.example", "verified": True, "primary": True}]
    _github_signin(farm)
    assert store.account("github_id", 9)["id"] == account_id, "still the same stable account"
    assert store.account("github_id", 9)["email"] == "new@corp.example", "its address kept up"
    assert store.account("email", "old@corp.example") is None, "the dropped address is nobody's now"
    # A link to the old address makes a fresh account, and does not reach the
    # one it used to: the credential the owner gave up is not a way in.
    stray = _sign_in_by_email(farm, mails, "old@corp.example")
    assert _whoami(farm, stray)["account"]["email"] == "old@corp.example"
    assert _whoami(farm, stray)["name"] != store.account("id", account_id)["handle"]


def test_linking_github_settles_the_role_in_the_same_session(farm, monkeypatch):
    """A person signs in by email first -- a guest -- then links GitHub, whose
    login is on the admins list. The role is settled from the account as it is
    after the id is linked, not the stale one before, so they are an admin in
    that same session, not a guest until they sign in again."""
    who, mails = _github_env(farm, monkeypatch)
    monkeypatch.setattr(portal_half, "SIGNIN_CODES_PER_ADDRESS", 50)
    monkeypatch.setenv("ALTERIOM_HIL_ADMINS", "github:55")

    first = _sign_in_by_email(farm, mails, "chief@corp.example")
    assert _whoami(farm, first)["role"] == "user", "an address confirms itself; admin still wants the id"

    who.update(id=55, login="chief", name="The Chief",
               emails=[{"email": "chief@corp.example", "verified": True, "primary": True}])
    linked = _whoami(farm, _github_signin(farm))
    assert linked["role"] == "admin", "the id's role settles in the session that links it"
    assert farm.portal.store.account("email", "chief@corp.example")["github_id"] == 55


def test_a_recipient_is_sent_only_so_many_links(farm, monkeypatch):
    """The caller throttle counts who asks; five callers is five mails to one
    address. A second bucket counts the recipient, so a person is not buried
    in sign-in mail however many callers ask -- and the answer is the same
    202 whether a mail went out or not."""
    _, mails = _github_env(farm, monkeypatch)
    cap = portal_half.SIGNIN_CODES_PER_ADDRESS
    # Each request from a different caller, so the caller throttle never
    # trips: what limits the mail here is the recipient bucket alone.
    for n in range(cap + 4):
        _post_email(farm, "victim@example.org", real_ip=f"203.0.113.{n}")
    assert len(mails) == cap, "past the allowance the address is spared, however many callers ask"
    _post_email(farm, "someone-else@example.org", real_ip="203.0.113.200")
    assert len(mails) == cap + 1, "another recipient has its own allowance"


def test_the_oauth_code_and_state_are_scrubbed_from_the_request_log(farm, monkeypatch):
    """The GitHub code and state ride in the callback's query, and request
    lines are logged; a log reader who could lift a live code and state
    would be that person. So the handler scrubs them where it writes the
    line. (A sign-in used to carry a one-time token in its path and was
    scrubbed too; a code is typed, never in a URL, so there is nothing of
    it to scrub.)"""
    said = farm_service._SECRET_QUERY_IN_LOG.sub(r"\1<redacted>",
                                                 'GET /auth/github/callback?code=good-code&state=abc123 HTTP/1.1')
    assert "code=<redacted>" in said and "state=<redacted>" in said and "good-code" not in said
    assert not hasattr(farm_service, "_TOKEN_IN_LOG"), "nothing rides in a path any more"


def test_a_deleting_browser_must_send_json_like_any_other_write(farm, monkeypatch):
    """The JSON-body gate that stops another site's form from riding a cookie
    covers DELETE too, including the routes the handler answers before the
    shared writer: a cookie DELETE without a JSON content type is refused."""
    _, mails = _github_env(farm, monkeypatch)
    monkeypatch.setattr(portal_half, "SIGNIN_CODES_PER_ADDRESS", 50)
    monkeypatch.setenv("ALTERIOM_HIL_ADMINS", "boss@corp.example")
    cookie = _sign_in_by_email(farm, mails, "boss@corp.example")
    # No JSON content type: refused before it deletes, on a fast-path route.
    status, _, raw = _raw(farm, "DELETE", "/api/v1/farm/notify", headers={"Cookie": cookie})
    assert status == 403 and "sends JSON" in json.loads(raw)["error"], raw
    # With it, the gate lets the request through to its own answer.
    status, _, _ = _raw(farm, "DELETE", "/api/v1/farm/notify",
                        headers={"Cookie": cookie, "Content-Type": "application/json"})
    assert status in (200, 404), status


def test_nothing_of_another_account_reaches_an_account_through_any_read_it_may_make(farm, monkeypatch, tmp_path):
    """One sweep over the whole account-readable surface, looking for leaks.

    Eight rounds of review on this change found the same defect eight times:
    a list was scoped and something hanging off it was not -- the grants
    beside the boards, the diagnostics beside the health, the reason beside
    the queue key, the owner beside the rig row. Each was found by reading
    one endpoint and noticing one field, which is why there were eight.

    This asks the question the other way round. Plant facts that belong to
    somebody else -- an owner's handle, a drain reason, a private run's id
    and project, a board's quarantine note -- then call every route an
    account may call and assert none of those strings comes back from any of
    them. A new endpoint added to ACCOUNT_ROUTES is swept by this without
    anybody remembering to sweep it, and a new field on an existing answer
    fails here rather than in the next review.
    """
    _, mails = _github_env(farm, monkeypatch)
    monkeypatch.setattr(portal_half, "SIGNIN_CODES_PER_ADDRESS", 50)
    portal = farm.portal
    stranger = _sign_in_by_email(farm, mails, "nobody@example.org")
    me = json.loads(_raw(farm, "GET", "/api/v1/whoami", headers={"Cookie": stranger})[2])["name"]

    farm.start_agent(_canary_pipeline(farm.node))
    _wait(lambda: any(w["name"] == "node-a" for w in portal.workers_view()), what="the node to say hello")

    # Somebody else's farm: a rig they own, lent out, with a run on it and a
    # board they took out of the pool.
    secrets = {
        "owner handle": "ada-lovelace",
        "drain reason": "recalibrating the anechoic box",
        "quarantine note": "radio join keeps failing",
        "usb path": "/dev/serial/by-path/ada-bench-port-3",
        "hardware address": "AA:BB:CC:DD:EE:FF",
        "probe error": "ttyUSB7 would not open: permission denied on ada's host",
        "project label": "Someone Elses Firmware",
        "consumer revision": "c0ffeec0ffee",
    }
    theirs = uuid.uuid4().hex
    portal.store.set_rig_owner("node-a", "ada-lovelace", "sparck")
    portal.store.set_rig_visibility("node-a", "shared", "sparck")
    portal.store.set_drained("node-a", {"by": "ada-lovelace", "reason": secrets["drain reason"]})
    portal.store.import_job(
        {"id": theirs, "kind": "suite", "status": "passed",
         "created_at": "2026-02-01T00:00:00+00:00", "finished_at": "2026-02-01T00:03:00+00:00",
         "request": {"profile": "painlessmesh", "submitted_by": "ada-lovelace",
                     "project": secrets["project label"]},
         "result": {"ok": True}},
        "node-a", tmp_path / "theirs.log")
    # The node has said hello; its boards come with its first heartbeat, a
    # moment later. Reading the snapshot in between found no board at all,
    # and CI on 3.9 lost that race once. Wait for the board, not the hello.
    _wait(lambda: portal.inventory_snapshot().get("boards"), what="the node to report its boards")
    board = next(b["id"] for b in portal.inventory_snapshot()["boards"])
    # The bench around the boards, which is the owner's: where a board is
    # plugged in, what its silicon is, and what a probe printed when it
    # would not read. A sweep is only worth the facts it plants, and the
    # round before this one planted none of these.
    stored = [w for w in portal.store.workers() if w["name"] == "node-a"][0]
    bench = dict(stored.get("inventory") or {})
    bench["boards"] = [{**entry, "port": secrets["usb path"], "mac": secrets["hardware address"]}
                       if entry.get("id") == board else entry
                       for entry in (bench.get("boards") or [])]
    bench["probe_errors"] = [{"port": secrets["usb path"], "error": secrets["probe error"]}]
    # The node is live and heartbeats five times a second here, and a
    # heartbeat carries its whole inventory, which replaces the stored one.
    # Planted into the store alone, the bench survived only until the next
    # beat: "the sweep never planted the usb path it claims to look for",
    # on the 3.9 lane first and then on 3.12, where the CI log showed the
    # one beat of the whole test landing in the gap between the store
    # write and the patch that came after it. So the node reports the bench
    # from here on, patched *before* anything is written, and the store is
    # written as well so no read has to wait for a beat -- every beat after
    # this carries the bench, the way an owner's bench facts really arrive.
    monkeypatch.setattr(farm.agent, "_inventory", lambda: bench)
    portal.store.touch_worker("node-a", bench, None, None)
    portal.store.set_hold(board, "quarantined", secrets["quarantine note"], "ada-lovelace", theirs)
    portal.board_health_path.write_text(json.dumps({board: {
        "id": board, "verdict": "failed", "checked_at": "2026-02-01T00:03:00+00:00",
        "job_id": theirs, "canary_revision": secrets["consumer revision"], "checks": {}}}),
        encoding="utf-8")
    # And a run of their own, waiting, so the queue has a reason to explain.
    mine = uuid.uuid4().hex
    portal.store.import_job(
        {"id": mine, "kind": "suite", "status": "queued", "created_at": "2026-02-01T00:04:00+00:00",
         "request": {"profile": "painlessmesh", "submitted_by": me}},
        None, tmp_path / "mine.log")
    # The farm pausing itself writes a sentence naming the run that failed,
    # and that run may be one this caller cannot open.
    # Resume first: `pause` records a reason only on the transition, so a
    # farm that had already paused itself would keep its own sentence and
    # this test would be asserting about somebody else's.
    portal.resume()
    portal.pause(f"the Rig Health Check failed on every board: test_every_wire (run {theirs[:8]})")
    portal._waiting = {mine: f"queued behind run {theirs[:8]} ({secrets['project label']}), "
                             f"which was queued first and needs the rig to itself"}

    swept = [
        "/api/v1/whoami", "/api/v1/sessions", "/api/v1/status", "/api/v1/rigs",
        "/api/v1/rigs/node-a", "/api/v1/inventory", f"/api/v1/inventory/{board}/history",
        "/api/v1/jobs", f"/api/v1/jobs/{mine}", f"/api/v1/rigs/node-a/webhooks", "/api/v1/world",
        "/api/v1/workers/node-a/commands",
    ]
    try:
        seen = {}
        for path in swept:
            status, _, raw = _raw(farm, "GET", path, headers={"Cookie": stranger})
            assert status in (200, 403, 404), f"{path}: {status}"
            seen[path] = raw.decode("utf-8", "replace") if status == 200 else ""
        for path, body in seen.items():
            for what, secret in secrets.items():
                assert secret not in body, f"{path} carries somebody else's {what}: {secret!r}"
            assert theirs not in body and theirs[:8] not in body, \
                f"{path} names a run this account cannot open"
        # The sweep is only worth what it reads, so prove it read something:
        # the account's own run and the rig lent to it are both in there.
        assert mine in seen["/api/v1/jobs"], "the sweep read a real workspace"
        # Why the farm is paused is everybody's business -- it is why their
        # own run has not started -- so the sentence survives; the run it
        # names does not, because that run's detail answers this caller 404.
        live = json.loads(seen["/api/v1/status"])["queue"]
        assert "the Rig Health Check failed on every board" in (live["paused_reason"] or ""), live
        assert "another run" in live["paused_reason"], live
        # Not `waiting`: the dispatcher owns that map and recomputes it on
        # its own clock, so a value planted here is the farm's to overwrite
        # -- "waiting for node-a, drained" once this test drains the rig.
        # Asserting on it raced, and the leak it was meant to catch is
        # already covered above: `theirs[:8]` appears in no answer at all,
        # whatever sentence the dispatcher last wrote. The anonymiser itself
        # is tested directly, where nothing can rewrite its input.
        assert "node-a" in seen["/api/v1/rigs"], "including the rig lent to it"
        # And that the same strings are there for an operator, or this test
        # would pass just as well against an empty farm.
        #
        # Every planted fact, or the sweep above is asserting the absence of
        # something it never put there. This is the half that makes the
        # other half mean anything, and it is also the whole claim: a key
        # reads the farm whole, so what the account did not get was scoped
        # away from them and not deleted from the farm. Waited for rather
        # than read once: the bench arrives by heartbeat, which is
        # asynchronous by design, and a beat computed before the patch above
        # can still land after the store write and hold the floor for one
        # interval. What must never be true is the account seeing a fact;
        # what must eventually be true is the key seeing every one.
        def whole_farm() -> str:
            _, whole = farm.call("GET", "/api/v1/status", TOKEN)
            _, all_boards = farm.call("GET", "/api/v1/inventory", TOKEN)
            return json.dumps(whole) + json.dumps(all_boards)

        missing = lambda: [what for what, secret in secrets.items() if secret not in whole_farm()]
        _wait(lambda: not missing(), timeout=5.0,
              what=f"every planted fact to be readable by a key; still missing: {missing()}")
    finally:
        portal.resume()
        portal._waiting = {}
        portal.store.release_hold(board)
        portal.board_health_path.write_text("{}", encoding="utf-8")
        portal.store.set_drained("node-a", None)
        portal.store.set_rig_visibility("node-a", "private", "sparck")
        portal.store.set_rig_owner("node-a", None, "sparck")


def test_a_handle_a_run_was_submitted_under_is_never_given_to_somebody_else(farm, monkeypatch, tmp_path):
    """A finished run is its submitter's, so its submitter's name is spent.

    `submitted_by` is what makes a run somebody's -- its detail, its log,
    its artifacts are all read by matching that name. So a handle retired
    from a revoked key and handed to the next person to sign up would give
    them that key's whole history, which is the one thing the workspace
    scoping is for. Reserving names the history refers to is the cheap half;
    an immutable principal id a display handle cannot impersonate is the
    durable one, and is not this change.
    """
    _, mails = _github_env(farm, monkeypatch)
    monkeypatch.setattr(portal_half, "SIGNIN_CODES_PER_ADDRESS", 50)
    portal = farm.portal

    # A key called `ada` ran something, and was then revoked: no key, no
    # rig, no running job -- nothing but history with its name on it.
    finished = uuid.uuid4().hex
    portal.store.import_job(
        {"id": finished, "kind": "suite", "status": "passed",
         "created_at": "2026-03-01T00:00:00+00:00", "finished_at": "2026-03-01T00:01:00+00:00",
         "request": {"profile": "painlessmesh", "submitted_by": "ada"}, "result": {"ok": True}},
        "node-a", tmp_path / "ada.log")
    assert "ada" in portal.store.submitters()

    # Somebody signs up who would otherwise be named `ada`.
    cookie = _sign_in_by_email(farm, mails, "ada@somewhere-else.example")
    who = json.loads(_raw(farm, "GET", "/api/v1/whoami", headers={"Cookie": cookie})[2])
    assert who["name"] != "ada", "a name the history answers to is not handed out again"
    assert who["name"].startswith("ada-"), who

    # And so they do not inherit the run.
    assert _raw(farm, "GET", f"/api/v1/jobs/{finished}", headers={"Cookie": cookie})[0] == 404
    page = json.loads(_raw(farm, "GET", "/api/v1/jobs", headers={"Cookie": cookie})[2])
    assert finished not in [run["id"] for run in page["jobs"]] and page["total"] == 0


def test_merging_two_sign_ins_keeps_the_history_of_both(farm, monkeypatch, tmp_path):
    """One person, two ways in, and the runs of both stay theirs.

    The merge moved rigs and runs still going, and left finished runs under
    the handle it was deleting -- so a person who signed in by GitHub, then
    by the same address, lost their own past work: not in their list, and
    their artifacts unreachable, because `submitted_by` still named a handle
    that no longer belonged to anybody.
    """
    _, mails = _github_env(farm, monkeypatch)
    monkeypatch.setattr(portal_half, "SIGNIN_CODES_PER_ADDRESS", 50)
    portal = farm.portal

    first = _sign_in_by_email(farm, mails, "grace@example.org")
    handle = json.loads(_raw(farm, "GET", "/api/v1/whoami", headers={"Cookie": first})[2])["name"]
    done = uuid.uuid4().hex
    portal.store.import_job(
        {"id": done, "kind": "suite", "status": "passed",
         "created_at": "2026-03-02T00:00:00+00:00", "finished_at": "2026-03-02T00:01:00+00:00",
         "request": {"profile": "painlessmesh", "submitted_by": handle}, "result": {"ok": True}},
        "node-a", tmp_path / "grace.log")
    assert _raw(farm, "GET", f"/api/v1/jobs/{done}", headers={"Cookie": first})[0] == 200

    # The same person arrives by GitHub with the same verified address, and
    # the two rows become one.
    keep = portal.store.create_account("grace-by-github", role="user")
    drop = portal.store.account("handle", handle)
    portal.store.merge_accounts(keep["id"], drop["id"], farm_service.utcnow())
    rewritten = portal.store.get(done)
    assert (rewritten["request"]["submitted_by"]) == "grace-by-github", \
        "a finished run follows the person, not the handle that was dropped"


def test_the_dashboard_offers_an_account_no_destination_the_farm_would_refuse():
    """Every page whose reads are closed to accounts is marked `farm-wide`.

    Hiding the buttons that 403 was done for the controls inside pages and
    not for the nav, so a new account saw Firmware, Insights and Settings,
    clicked one, and got an error panel -- an advertised destination that
    cannot work. The three read `/api/v1/artifacts`, `/api/v1/stats` and
    `/api/v1/config`, each deliberately absent from ACCOUNT_ROUTES, so the
    rule is: closed to an account means not offered to one.

    A ratchet on the markup rather than a screenshot: the CSS gate
    (`body[data-caller="account"] .farm-wide`) and both routers key off this
    class, so if a destination loses it the dashboard silently offers it
    back. It asks `account_route` whether each endpoint is really closed, so
    a panel that gains a scoped endpoint later fails this test rather than
    staying hidden for a reason that has gone away.
    """
    from alteriom_hil.api_keys import account_route

    page = (REPO / "rig" / "web" / "index.html").read_text(encoding="utf-8")
    css = (REPO / "rig" / "web" / "app.css").read_text(encoding="utf-8")
    assert 'body[data-caller="account"]:not([data-role="admin"]) .farm-wide{display:none!important}' in css, \
        "the class the markup is marked with has to style something"
    assert 'dataset.caller = "account"' in (REPO / "rig" / "web" / "app.js").read_text(encoding="utf-8"), \
        "and something has to set the attribute it keys off"
    # And it must not hide them from an admin. An admin signs in as an
    # account like anybody else, and `allowed()` admits them before it ever
    # asks about a workspace -- so a gate keyed on "is an account" alone
    # took the whole farm-wide dashboard from the people it is mainly for.
    from alteriom_hil.api_keys import allowed as may_reach

    assert may_reach(farm_service.Identity("boss", "admin", kind="account"),
                     "GET", "/api/v1/stats"), "the farm authorizes an admin account"

    closed = {"artifacts": "/api/v1/artifacts", "statistics": "/api/v1/stats",
              "configuration": "/api/v1/config"}
    for panel, endpoint in closed.items():
        assert not account_route("GET", endpoint), \
            f"{endpoint} is readable by an account now; {panel} need not be hidden"
        assert f'<button class="nav-item farm-wide" data-panel="{panel}">' in page, \
            f"the {panel} nav item is offered to an account that cannot open it"
        assert f'<section class="page farm-wide" data-page="{panel}"' in page, \
            f"the {panel} page itself is not marked, so an address still reaches it"
    # The pages Firmware leads on to, the same store under another name.
    for onward in ("artifact", "storage"):
        assert f'<section class="page farm-wide" data-page="{onward}"' in page, onward
    # The nested one: Releases sits inside the Rigs page, so hiding the
    # top-level destinations left it reachable by its tab and its hash.
    assert not account_route("GET", "/api/v1/releases")
    assert '<a href="#releases" class="farm-wide" data-tab="releases">' in page
    assert '<section id="releases" class="card farm-wide"' in page
    # And starting a run, which is the farm's until the allocator knows whose
    # workspace asked: both controls that submit one.
    assert not account_route("POST", "/api/v1/suites")
    assert 'id="overview-new-run" class="secondary farm-wide"' in page
    assert 'id="run-card" class="card new-run farm-wide"' in page
    # The rig page's own tabs, which are gated in script rather than markup:
    # both are drawn from what `rig_detail` withholds from a borrower, so a
    # tab that loses `owner: true` starts drawing an empty page and offering
    # writes that answer 404.
    js = (REPO / "rig" / "web" / "app.js").read_text(encoding="utf-8")
    # The script gates ask the same question the stylesheet does, or an
    # admin keeps their nav and loses their deep links.
    assert 'dataset.role !== "admin"' in js, \
        "the script gates must exempt an admin as the stylesheet does"
    for tab in ("activity", "setup"):
        assert f'{{id: "{tab}", label: ' in js and f'"{tab}", label: ' in js
    assert js.count("owner: true") >= 2,         "Activity and Setup are both the owner's; a lent rig sends neither"
    # Rediscovery commands every rig at once, and the overview's stats band
    # is a farm-wide read: both are withheld from an account, so neither is
    # put in front of one. The stats poll is skipped in script as well --
    # the band being hidden does not stop `refresh()` asking for it, which
    # made a 403 once a minute that nothing showed and nothing stopped.
    assert not account_route("POST", "/api/v1/inventory/refresh")
    assert not account_route("GET", "/api/v1/stats")
    assert 'id="refresh" class="secondary farm-wide"' in page,         "Rediscover is farm-wide and is not offered to an account"
    assert 'id="overview-stats" class="card stats-band farm-wide"' in page
    assert "if (workspaceOnly()) return;" in js, \
        "the stats poll has to be skipped, not merely hidden"

    # The farm console is the fleet's own log and `/api/v1/console` is
    # closed to accounts, so it is not left on screen for one.
    assert not account_route("GET", "/api/v1/console")
    assert '<aside id="console" class="console farm-wide"' in page,         "the console is offered to an account the farm would refuse"
    # A verdict whose run was redacted has no id to link, and linking it
    # anyway rendered a "null" pointing at #run/ -- a dead link offering to
    # open what the service had just declined to name. Held by the
    # placeholder's own words, which is a weak ratchet but an honest one:
    # what it really asserts is that somebody thought about the empty case.
    assert "not yours to open" in js,         "a verdict with no run id must render a placeholder, not a null link"

    # And a rig withheld is not a rig unowned: saying "the farm" to a
    # borrower states the opposite of the truth.
    assert "another workspace" in js,         "a lent rig's owner is withheld, not absent, and the page must say so"

    # What an account must keep: its own workspace.
    for open_panel in ("overview", "runs", "rigs", "account"):
        assert f'data-panel="{open_panel}">' in page, f"{open_panel} is an account's own"
        assert f'class="nav-item farm-wide" data-panel="{open_panel}"' not in page, \
            f"{open_panel} is the caller's own workspace and must stay offered"


def test_a_board_reserved_by_hand_still_says_who_reserved_it_and_why(farm, monkeypatch):
    """A hold with no run behind it is the case the run scope gets wrong.

    Board metadata is redacted by asking whether the run that wrote it is
    this caller's. A hand reservation has no run at all, so that question
    has no good answer, and the rule has to fall back on whose bench the
    board is on. It did -- but only for `runs=None`, while a key and an
    admin reach the same code as the `(None, None)` that `run_scope` gives
    them. So an operator who reserved a board by hand was shown their own
    reservation with the who and the why stripped out of it.
    """
    portal = farm.portal
    farm.start_agent(_canary_pipeline(farm.node))
    _wait(lambda: any(w["name"] == "node-a" for w in portal.workers_view()), what="the node to say hello")
    # The node has said hello; its boards come with its first heartbeat, a
    # moment later. Reading the snapshot in between found no board at all,
    # and CI on 3.9 lost that race once. Wait for the board, not the hello.
    _wait(lambda: portal.inventory_snapshot().get("boards"), what="the node to report its boards")
    board = next(b["id"] for b in portal.inventory_snapshot()["boards"])
    portal.store.set_hold(board, "reserved", "bench work on the antenna", "sparck")
    try:
        _, seen = farm.call("GET", "/api/v1/inventory", TOKEN)
        held = next(b for b in seen["boards"] if b["id"] == board)
        assert held["hold"]["by"] == "sparck" and held["hold"]["reason"] == "bench work on the antenna", \
            "a key reserved it and a key may read why"
        assert held["state"] == "reserved"
        drill = farm.call("GET", f"/api/v1/inventory/{board}/history", TOKEN)[1]
        assert drill["hold"]["by"] == "sparck" and drill["hold"]["reason"], drill["hold"]
    finally:
        portal.store.release_hold(board)


def test_a_blocking_runs_whole_name_goes_even_when_its_label_has_brackets(farm, monkeypatch, tmp_path):
    """The label is delimited by counting brackets, not by the first one.

    A waiting reason names the run in front as `run 1a2b3c4d (project)`, and
    a project may be called `Acme (private) nightly`. Ending the name at the
    first `)` left ` nightly)` sitting in a sentence handed to somebody whose
    own detail route answers 404 for that run -- half a label is still a leak,
    and a wrong one, because the sentence also stops making sense.
    """
    _, mails = _github_env(farm, monkeypatch)
    monkeypatch.setattr(portal_half, "SIGNIN_CODES_PER_ADDRESS", 50)
    portal = farm.portal
    cookie = _sign_in_by_email(farm, mails, "onlooker@example.org")
    me = json.loads(_raw(farm, "GET", "/api/v1/whoami", headers={"Cookie": cookie})[2])["name"]

    blocker = uuid.uuid4().hex
    mine = uuid.uuid4().hex
    portal.store.import_job(
        {"id": blocker, "kind": "suite", "status": "running",
         "created_at": "2026-04-01T00:00:00+00:00",
         "request": {"profile": "painlessmesh", "submitted_by": "someone-else"}},
        "node-a", tmp_path / "blocker.log")
    portal.store.import_job(
        {"id": mine, "kind": "suite", "status": "queued", "created_at": "2026-04-01T00:01:00+00:00",
         "request": {"profile": "painlessmesh", "submitted_by": me}},
        None, tmp_path / "mine.log")
    reason = (f"queued behind run {blocker[:8]} (Acme (private) nightly), "
              f"which was queued first and needs the rig to itself")
    try:
        # The anonymiser directly, not through `/api/v1/status`: the
        # dispatcher owns the waiting map and rewrites it on its own clock,
        # so a reason planted there and read back over HTTP is racing a
        # thread that has every right to replace it. What is under test here
        # is where a label ends, and that has no clock in it.
        rigs, submitter = portal.run_scope(
            farm_service.Identity(me, "user", kind="account"))
        said = portal._unnamed_blockers(reason, portal.store.active(), rigs, submitter)
        assert "Acme" not in said and "private" not in said and "nightly" not in said, said
        assert blocker[:8] not in said, said
        # The sentence still explains itself, which is the whole point of
        # anonymising the run rather than dropping the reason.
        assert said == "queued behind another run, which was queued first and needs the rig to itself", said
        # And a run of their own keeps its name, or this passes by redacting
        # everything, which would be a different bug wearing the same green.
        ours = portal._unnamed_blockers(
            f"queued behind run {mine[:8]} (Mine), which was queued first",
            portal.store.active(), rigs, submitter)
        assert mine[:8] in ours and "(Mine)" in ours, ours
        # A label that never closes ends the sentence. Nothing validates a
        # profile label against brackets, so `(Acme (private)` is a name
        # somebody can choose -- and "only the id goes" failed open on
        # exactly the malformed input that would be picked on purpose.
        # Over-redacting a clause the farm wrote is the cheaper mistake.
        ragged = portal._unnamed_blockers(
            f"queued behind run {blocker[:8]} (Acme (private), which was queued first",
            portal.store.active(), rigs, submitter)
        assert ragged == "queued behind another run", ragged
        assert "Acme" not in ragged and "private" not in ragged, ragged
    finally:
        with portal.store.connect() as db:
            db.execute("DELETE FROM jobs WHERE id IN (?,?)", (blocker, mine))


def test_the_dashboard_scripts_parse():
    """A file that does not parse is a dashboard that does not load.

    This was not caught by anything: a merge brought the same block in from
    two branches, `const here` was declared twice in one scope, and
    `app.js` stopped parsing entirely. Every test still passed, because the
    suite exercises the service and reads the markup as text -- nobody was
    asking whether the script the browser has to run is a script.

    Skipped where node is absent rather than faked: a syntax check needs a
    parser for the language, and a test that pretends to do it is worse than
    one that says it did not run.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node to parse with")
    broken = {}
    for path in sorted((REPO / "rig" / "web").glob("*.js")):
        done = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        if done.returncode != 0:
            # The line naming the fault, not node's version banner at the end.
            said = [line for line in (done.stderr or "").splitlines()
                    if "Error" in line or "^" in line]
            broken[path.name] = said[:2] or (done.stderr or "").strip().splitlines()[:2]
    assert not broken, f"dashboard scripts that do not parse: {broken}"


def test_a_release_says_which_agent_each_of_its_profiles_speaks(farm, tmp_path):
    """The agent is the profile's, not the farm's: a release is read for the
    profiles it carries, and each that names an agent gets that source's
    digest -- the same one a checkout of the commit gives."""
    repo, commit, body = _release_with_agent(
        tmp_path, "void setup() { per_profile(); }\n",
        profiles={"painlessmesh": "suites/painlessmesh/firmware", "canary": None,
                  "elsewhere": "suites/nothing/here"},
    )
    digest = farm_service.release_agent_sha(tmp_path / "agent.bundle", commit)
    assert digest
    assert farm_service.release_agents(tmp_path / "agent.bundle", commit) == {"painlessmesh": digest}, (
        "a profile with no agent, and one whose agent the release does not carry, are not in it"
    )

    farm.portal.publish_release(commit, body, "ci")
    release = farm.portal.current_release()
    assert release["agents"] == {"painlessmesh": digest}
    assert release["hil_agent_sha"] == digest, "still written, for a portal image from before this"
    assert farm.portal.expected_agent_sha("painlessmesh") == digest
    assert farm.portal.expected_agent_sha() == digest, "no profile named is the default one"
    assert farm.portal.expected_agent_sha("canary") is None

    # And a record written before `agents` existed gets it when it is read.
    record = farm.portal.release_root / f"{commit}.json"
    payload = json.loads(record.read_text())
    payload.pop("agents")
    record.write_text(json.dumps(payload))
    assert farm.portal.current_release()["agents"] == {"painlessmesh": digest}
    assert json.loads(record.read_text())["agents"] == {"painlessmesh": digest}


def test_the_agent_the_farm_holds_painlessmesh_to_is_the_one_its_build_stamps(monkeypatch):
    """The digest is compared with what `build_artifacts.py` writes into a
    manifest, by a service that no longer knows where the agent lives: it
    reads the profile. If the two ever disagree every bundle is refused, on
    every rig, so they are held together here."""
    from suites.painlessmesh import build_artifacts

    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.repo = REPO
    declared = manager.profiles["painlessmesh"].agent_source_path
    assert declared and (REPO / declared).is_dir()
    # The same directory, each within its own checkout. `suites` is imported
    # from wherever Python was started, and that need not be the checkout
    # this file is in: the simulation host's deploy runs these tests from its
    # clone with a runner's workspace as the working directory, and comparing
    # the two absolute paths failed that deploy on the day this was written.
    # The builder's own checkout: `suites/painlessmesh/build_artifacts.py`, so
    # two levels up -- not this file's, which is one.
    builders_checkout = Path(build_artifacts.__file__).resolve().parents[2]
    assert build_artifacts.FIRMWARE_DIR.resolve().relative_to(builders_checkout).as_posix() == declared
    # And the same digest of the same files: the builder's, over this
    # checkout's agent, so that two checkouts a commit apart compare the two
    # ways of digesting and not two commits.
    monkeypatch.setattr(build_artifacts, "FIRMWARE_DIR", REPO / declared)
    assert manager.agent_source_sha("painlessmesh") == build_artifacts.agent_source_sha()
    assert manager.agent_source_sha() == build_artifacts.agent_source_sha()
    assert declared == farm_service.LEGACY_AGENT_SOURCE_PATH, (
        "releases from before profiles named their agents are read by this path"
    )
    assert manager.agent_source_sha("canary") is None


def test_the_mode_picks_a_class_that_carries_only_its_own_half():
    """A node runs no portal code and a portal no driver: the class is picked
    by mode, and what each half reaches for in the other is behind a mode
    guard or a base default that does nothing (docs/public-release-plan.md,
    step 8c)."""
    from alteriom_hil import rig_manager

    assert farm_service.manager_for("node") is farm_service.RigManager
    assert farm_service.manager_for("portal") is farm_service.PortalManager
    assert farm_service.manager_for("standalone") is farm_service.FarmManager
    assert not hasattr(farm_service.RigManager, "worker_hello"), "a node has no workers"
    assert not hasattr(farm_service.RigManager, "finish_signin_code"), "or accounts"
    assert not hasattr(farm_service.PortalManager, "_execute"), "a portal runs no pipeline"
    assert not hasattr(farm_service.PortalManager, "register_device"), "and has no boards"
    assert issubclass(farm_service.FarmManager, (rig_manager.RigMixin, portal_half.PortalMixin))
    # What the base says when the other half is not there.
    bare = farm_service.RigManager.__new__(farm_service.RigManager)
    bare.emit(None)
    bare._emit_run("x", "queued", {})
    assert bare.imported_artifact("x") is False
    with pytest.raises(farm_service.ElsewhereError):
        bare._require_worker("anything")


def test_the_rig_view_is_one_shape_whether_a_rig_or_its_portal_serves_it(farm):
    """The dashboard draws one page for "this rig": a rig serves the rig view
    about itself, and the portal serves the same shape about each rig it
    has, from what the rig reported. Both are held to the contract's schema,
    so the farm cannot drift from the rig (docs/public-release-plan.md, step 10)."""
    import jsonschema

    schema = json.loads((RUNNER / "rig-view.schema.json").read_text(encoding="utf-8"))
    farm.start_agent(_canary_pipeline(farm.node))

    # The node, about itself: served by the node's own handler, which the
    # harness does not run, so ask the manager the handler would.
    own = farm.node.rig_view()
    jsonschema.validate(own, schema)
    assert own["contract"] == 1 and own["name"] == "local", "a node not told its worker name is `local` to itself"
    assert own["boards"] == len(BOARDS) and own["profiles"] == sorted(farm.node.profiles)

    # The portal, about the same rig, over HTTP as the dashboard asks.
    status, theirs = farm.call("GET", "/api/v1/rigs/node-a/view", TOKEN)
    assert status == 200, theirs
    jsonschema.validate(theirs, schema)
    assert theirs["contract"] == 1 and theirs["name"] == "node-a" and theirs["boards"] == len(BOARDS)
    assert set(theirs) == set(farm.node.RIG_VIEW_KEYS), "the same keys, whichever side answers"
    assert set(own) == set(farm.node.RIG_VIEW_KEYS)
    assert set(theirs["inventory"]) == set(own["inventory"])

    # A portal is nobody's rig; a rig that has not joined has no view yet.
    assert farm.call("GET", "/api/v1/view", TOKEN)[0] == 409
    assert farm.call("GET", "/api/v1/rigs/nobody/view", TOKEN)[0] == 404
