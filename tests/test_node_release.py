"""How a node gets a release onto its host, and how a deploy knows it did.

rig/node-update.sh installs what the node agent staged, with the update
script from the release itself; runner/ci_farm_release.py publishes a release
and waits for every node to run it. docs/portal-plan.md.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE_UPDATE = ROOT / "rig" / "node-update.sh"
SPEC = importlib.util.spec_from_file_location("ci_farm_release", ROOT / "runner" / "ci_farm_release.py")
ci_farm_release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(ci_farm_release)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="runs node-update.sh with git and bash")
COMMIT = "c" * 40


# ---- node-update.sh -----------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "ci", "GIT_AUTHOR_EMAIL": "ci@example.invalid",
           "GIT_COMMITTER_NAME": "ci", "GIT_COMMITTER_EMAIL": "ci@example.invalid"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def _staged(tmp_path: Path, update_script: str) -> dict:
    """A release whose update-runner.sh is `update_script`, staged as the agent stages one."""
    source = tmp_path / "source"
    (source / "rig").mkdir(parents=True)
    script = source / "rig" / "update-runner.sh"
    script.write_text(update_script)
    script.chmod(0o755)
    _git(source, "init", "-q")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "release")
    commit = _git(source, "rev-parse", "HEAD")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(source), str(clone))
    update = tmp_path / "update"
    update.mkdir()
    bundle = update / f"{commit}.bundle"
    _git(source, "bundle", "create", str(bundle), "HEAD")
    request = {"commit": commit, "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
               "bundle": str(bundle), "requested_at": "2026-09-14T00:00:00+00:00"}
    (update / "request.json").write_text(json.dumps(request))
    return {"commit": commit, "clone": clone, "update": update, "bundle": bundle, "request": request}


def _run(staged: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(NODE_UPDATE)], capture_output=True, text=True,
        env={**os.environ, "ALTERIOM_HIL_UPDATE_DIR": str(staged["update"]), "ALTERIOM_HIL_REPO": str(staged["clone"])},
    )


RECORDING_UPDATE = """#!/usr/bin/env bash
printf '%s\\n' "$*" > "$ALTERIOM_HIL_UPDATE_DIR/ran-with"
printf '%s\\n' "$HIL_DEPLOY_SOURCE" > "$ALTERIOM_HIL_UPDATE_DIR/ran-from"
echo "==> deployed"
"""


@posix_only
def test_a_staged_release_is_installed_by_its_own_update_script_from_the_bundle(tmp_path):
    staged = _staged(tmp_path, RECORDING_UPDATE)
    result = _run(staged)
    assert result.returncode == 0, result.stderr
    update = staged["update"]
    assert (update / "ran-with").read_text().split() == ["--unattended", "--ref", staged["commit"]]
    assert (update / "ran-from").read_text().strip() == str(staged["bundle"]), "fetched from the bundle, not GitHub"
    status = json.loads((update / "status.json").read_text())
    assert status["state"] == "installed" and status["commit"] == staged["commit"]
    assert status["detail"] == "==> deployed"
    assert not (update / "request.json").exists() and not (update / "request.taken.json").exists()
    assert not staged["bundle"].exists(), "the installed release is in the clone now"


@posix_only
def test_a_failed_install_is_said_and_not_started_again(tmp_path):
    staged = _staged(tmp_path, "#!/usr/bin/env bash\necho 'rig still busy after 1800s'\nexit 1\n")
    result = _run(staged)
    assert result.returncode == 1
    status = json.loads((staged["update"] / "status.json").read_text())
    assert status["state"] == "failed" and "rig still busy" in status["detail"]
    assert not (staged["update"] / "request.json").exists(), "PathExists= would start it again in a loop"


@posix_only
@pytest.mark.parametrize("tamper, says", [
    (lambda staged: {**staged["request"], "sha256": "0" * 64}, "does not match the digest"),
    (lambda staged: {**staged["request"], "bundle": "/tmp/elsewhere.bundle"}, "outside"),
    (lambda staged: {**staged["request"], "commit": "not-a-commit"}, "names no commit"),
])
def test_only_the_bundle_the_agent_staged_and_checked_is_installed(tmp_path, tamper, says):
    staged = _staged(tmp_path, RECORDING_UPDATE)
    (staged["update"] / "request.json").write_text(json.dumps(tamper(staged)))
    result = _run(staged)
    assert result.returncode == 1
    assert says in json.loads((staged["update"] / "status.json").read_text())["detail"]
    assert not (staged["update"] / "ran-with").exists(), "nothing was run"


@posix_only
def test_no_request_is_nothing_to_do(tmp_path):
    update = tmp_path / "update"
    update.mkdir()
    result = subprocess.run(["bash", str(NODE_UPDATE)], capture_output=True, text=True,
                            env={**os.environ, "ALTERIOM_HIL_UPDATE_DIR": str(update)})
    assert result.returncode == 0 and not (update / "status.json").exists()


# ---- ci_farm_release.py ----------------------------------------------------------------------

def _worker(name, commit=None, update=None, online=True, kind="hardware"):
    return {"name": name, "kind": kind, "online": online, "commit": commit, "update": update}


def test_a_node_counts_as_on_the_release_once_it_runs_it_installed():
    index = {"workers": [
        _worker("a", COMMIT, {"state": "installed", "commit": COMMIT}),
        _worker("b", "a" * 40, {"state": "staged", "commit": COMMIT}),
        _worker("c", COMMIT, {"state": "installing", "commit": COMMIT}),
        _worker("d", COMMIT),  # installed some other way
        _worker("e", "a" * 40),
        _worker("gone", "a" * 40, online=False),
        _worker("sim", "a" * 40, kind="simulation"),
    ]}
    states, failures = ci_farm_release.progress(index, COMMIT)
    assert states == {"a": "installed", "b": "staged", "c": "installing", "d": "installed", "e": "on aaaaaaaaaaaa"}
    assert failures == []
    states, failures = ci_farm_release.progress(
        {"workers": [_worker("a", "a" * 40, {"state": "failed", "commit": COMMIT, "detail": "verify-rig failed"})]}, COMMIT)
    assert states == {"a": "failed"} and failures == ["a: verify-rig failed"]


def _waiter(monkeypatch, answers, timeout=600.0):
    calls = iter(answers)
    monkeypatch.setattr(ci_farm_release, "call", lambda *args, **kwargs: next(calls))
    ticks = iter(range(0, 100000, 15))
    args = ci_farm_release.argparse.Namespace(base_url="https://portal", commit=COMMIT, timeout=timeout, poll_seconds=15)
    return lambda: ci_farm_release.wait(args, "k", clock=lambda: float(next(ticks)), sleep=lambda _: None)


def test_the_deploy_waits_for_every_node_and_stops_at_a_failure(monkeypatch, capsys):
    current = {"commit": COMMIT}
    staged = {"current": current, "workers": [_worker("a", "a" * 40, {"state": "staged", "commit": COMMIT})]}
    installed = {"current": current, "workers": [_worker("a", COMMIT, {"state": "installed", "commit": COMMIT})]}
    assert _waiter(monkeypatch, [staged, staged, installed])() == 0
    assert "Every node runs cccccccccccc" in capsys.readouterr().out

    failed = {"current": current, "workers": [_worker("a", "a" * 40, {"state": "failed", "commit": COMMIT, "detail": "boom"})]}
    assert _waiter(monkeypatch, [staged, failed])() == 1
    assert "::error::a: boom" in capsys.readouterr().out

    superseded = {"current": {"commit": "d" * 40}, "workers": []}
    assert _waiter(monkeypatch, [superseded])() == 1
    assert "another deploy published after this one" in capsys.readouterr().out


def test_nobody_online_to_install_it_fails_rather_than_waiting_out_the_timeout(monkeypatch, capsys):
    nobody = {"current": {"commit": COMMIT}, "workers": [_worker("a", "a" * 40, online=False)]}
    assert _waiter(monkeypatch, [nobody] * 1000, timeout=3600)() == 1
    assert "no hardware node has been online for ten minutes" in capsys.readouterr().out


def _dist(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "alteriom_hil-1.0.1-py3-none-any.whl").write_bytes(b"PK rig")
    (dist / "alteriom_hil_core-1.0.1-py3-none-any.whl").write_bytes(b"PK core")
    (dist / "alteriom-hil-dashboard-1.0.1.tar.gz").write_bytes(b"\x1f\x8b web")
    (dist / "SHA256SUMS").write_text("sums")
    (dist / "release.json").write_text('{"schema": 1}')
    return dist


def _publisher(monkeypatch, tmp_path, answers):
    """`call` answers from `answers` in turn: a dict is an answer, a string
    is the error the portal gave. Returns (run, posted paths, sleeps)."""
    posted, slept = [], []
    queue = iter(answers)

    def fake_call(base_url, token, path, body=None, **kwargs):
        posted.append(path)
        answer = next(queue)
        if isinstance(answer, str):
            raise RuntimeError(answer)
        return answer

    monkeypatch.setattr(ci_farm_release, "call", fake_call)
    ticks = iter(range(0, 100000, 5))
    args = ci_farm_release.argparse.Namespace(base_url="https://portal", commit=COMMIT, dist=_dist(tmp_path))
    run = lambda: ci_farm_release.publish_files(args, "k", sleep=slept.append, clock=lambda: float(next(ticks)))
    return run, posted, slept


def _sealed(name):
    return {"name": name, "bytes": 6, "sha256": "f" * 64, "packages": [{"name": "alteriom_hil-1.0.1-py3-none-any.whl"}]}


def test_the_packages_are_published_after_the_bundle_and_the_manifest_last(monkeypatch, tmp_path, capsys):
    ok = _sealed("x")
    run, posted, slept = _publisher(monkeypatch, tmp_path, [ok, ok, ok, ok, ok])
    assert run()["packages"]
    assert [path.rsplit("/", 1)[1] for path in posted] == [
        "SHA256SUMS", "alteriom-hil-dashboard-1.0.1.tar.gz", "alteriom_hil-1.0.1-py3-none-any.whl",
        "alteriom_hil_core-1.0.1-py3-none-any.whl", "release.json",
    ], "every file, release.json last: the portal checks it against the rest"
    assert not slept
    assert "Packages sealed: alteriom_hil-1.0.1-py3-none-any.whl" in capsys.readouterr().out


def test_a_portal_without_the_route_yet_is_waited_for_not_failed(monkeypatch, tmp_path, capsys):
    """The deploy and the portal-image rollout start on the same push, and
    the deploy is faster: it reached a portal whose old image had no files
    route and failed on the 404 (2026-09-22). A 404 here is a rollout in
    progress, and is waited for."""
    ok = _sealed("x")
    not_yet = "the portal answered HTTP 404: {\"error\": \"not found\"}"
    run, posted, slept = _publisher(monkeypatch, tmp_path, [not_yet, not_yet, ok, ok, ok, ok, ok])
    assert run()["packages"]
    assert len(posted) == 7, "the first file was tried three times, then the rest once"
    assert slept == [5.0, 10.0], "backing off, as the rest of the client does"
    assert "no release-files route yet" in capsys.readouterr().out


def test_a_portal_that_never_gets_the_route_fails_the_deploy_with_the_reason(monkeypatch, tmp_path):
    not_yet = "the portal answered HTTP 404: {\"error\": \"not found\"}"
    run, posted, slept = _publisher(monkeypatch, tmp_path, [not_yet] * 400)
    monkeypatch.setattr(ci_farm_release, "ROLLOUT_SECONDS", 30.0)
    with pytest.raises(RuntimeError, match="still has no release-files route after 30s"):
        run()
    assert sum(slept) <= 30.0 + 60.0, "bounded by the rollout deadline"


def test_any_other_refusal_of_a_file_fails_at_once(monkeypatch, tmp_path):
    bad = "the portal answered HTTP 400: {\"error\": \"release.json is not the file\"}"
    run, posted, slept = _publisher(monkeypatch, tmp_path, [bad])
    with pytest.raises(RuntimeError, match="HTTP 400"):
        run()
    assert posted and not slept, "a real refusal is not a rollout"

