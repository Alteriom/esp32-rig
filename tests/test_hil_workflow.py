import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "hil-painlessmesh.yml"
)


def test_combined_workflow_orders_simulation_before_physical_hil():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    # `runner` picks where the no-hardware jobs run; the ordering that matters
    # is that simulation gates the physical run, and that the firmware the rig
    # flashes is built before it -- off the rig (artifact-first-plan, step 2).
    assert list(workflow["jobs"]) == ["runner", "resolve", "simulate", "firmware", "hil"]
    assert workflow["jobs"]["simulate"]["needs"] == ["runner", "resolve"]
    assert workflow["jobs"]["firmware"]["needs"] == ["runner", "resolve"]
    assert workflow["jobs"]["hil"]["needs"] == ["runner", "resolve", "simulate", "firmware"]
    # The bundle is built for the revision this run resolved, not a profile
    # default: a run validating a branch must flash that branch.
    firmware = "\n".join(step.get("run", "") for step in workflow["jobs"]["firmware"]["steps"])
    assert "build_artifacts.py" in firmware
    assert "needs.resolve.outputs.sha" in firmware
    steps = workflow["jobs"]["simulate"]["steps"]
    assert any("painlessMesh-simulator" in step.get("with", {}).get("repository", "") for step in steps)
    assert any("simulator_evidence.py" in step.get("run", "") for step in steps)


def test_physical_job_submits_normalized_evidence_to_farm_service():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    commands = "\n".join(step.get("run", "") for step in workflow["jobs"]["hil"]["steps"])
    assert "ci_farm_client.py" in commands
    assert "--simulation-evidence" in commands
    # The rig flashes what the firmware job built; the Pi's own build command
    # is the fallback for a run that supplies nothing.
    assert "--artifacts hil-firmware" in commands
    downloads = [
        step.get("with", {}).get("name", "")
        for step in workflow["jobs"]["hil"]["steps"]
        if "download-artifact" in step.get("uses", "")
    ]
    assert "hil-firmware-matrix" in downloads
    # Every family the farm can host: the service refuses a run whose artifact
    # selection does not cover a connected board.
    from alteriom_hil.board import SUPPORTED_TARGETS

    for target in SUPPORTED_TARGETS:
        assert f"--target {target}" in commands, target


def test_simulator_gate_cannot_be_masked_by_tee():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    commands = "\n".join(
        step.get("run", "") for step in workflow["jobs"]["simulate"]["steps"]
    )
    assert "set -o pipefail" in commands
    assert "ci_integration_check.sh" in commands
    assert "exit 1" in commands


# ---- the generic trigger, and what the farm's own CI no longer does ----

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_the_generic_run_workflow_takes_its_profile_from_the_request():
    """hil-run.yml is the workflow a consumer's CI dispatches. Nothing in it
    may name a project: the profile decides everything project-specific."""
    text = (WORKFLOWS / "hil-run.yml").read_text(encoding="utf-8")
    assert "--profile \"$PROFILE\"" in text
    assert "inputs.profile" in text
    assert "client_payload.profile" in text, "repository_dispatch callers need the same fields"
    body = text.split("jobs:", 1)[1]
    assert "painlessmesh" not in body.lower(), "the generic trigger must not know one consumer's name"


def test_a_bundle_that_cannot_be_had_fails_the_run_rather_than_warning():
    """No artifact is a failure.

    The fetch used to warn and carry on, because the farm would compile the
    commit itself. Nothing builds on the rig now, so carrying on means
    submitting a run the service refuses -- with a message about the farm not
    building rather than about the bundle -- and while the fallback existed it
    turned a broken hand-off into a slower run that passed. The reason the
    bundle could not be had is known here and nowhere later, so this is where
    the run has to stop.
    """
    workflow = yaml.load((WORKFLOWS / "hil-run.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["hil"]["steps"]
    fetch = next(
        step for step in steps
        if step.get("name") == "Fetch the bundle the consumer built"
    )
    assert "continue-on-error" not in fetch, "a refused bundle must fail the job"

    script = fetch["run"]
    assert "::warning::" not in script, "a hand-off that broke is not a warning"
    assert "::error::" in script and "exit 1" in script, "no token is a failure, with what to add"
    # The fetch itself is run bare rather than in an `if`, so its own non-zero
    # exit -- and the message it printed saying what was wrong with the run,
    # the commit or the manifest -- ends the job.
    invocation = next(
        line for line in script.splitlines() if "runner/fetch_supplied_bundle.py" in line
    )
    assert not invocation.strip().startswith("if "), "the fetch is not allowed to fail quietly"
    assert "else" not in script, "and no branch carries on without the bundle"


def test_a_hardware_run_waits_for_the_rig_and_is_never_cancelled_by_another():
    """A concurrency group does not queue runs: GitHub keeps one pending run
    per group and cancels the rest. With the hardware workflows and the host
    deploy sharing one, two deploys died with zero jobs on 2026-09-10 as the
    next run arrived. The queue is the single self-hosted runner plus the
    farm service's own queue, so the hardware workflows declare no group."""
    for name in ("hil-run.yml", "hil-painlessmesh.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert re.search(r"^concurrency:", text, re.M) is None, f"{name} must not declare a concurrency group"
        assert "esp32-farm-hardware" not in re.sub(r"#.*", "", text), f"{name} still names the old group"


def test_a_dispatching_workflow_can_find_its_run_by_dispatch_id():
    """`gh workflow run` returns before the run exists. The id a caller passes
    has to land in the run name, because the run list is the only place to
    look for it afterwards."""
    text = (WORKFLOWS / "hil-run.yml").read_text(encoding="utf-8")
    assert "dispatch_id" in text
    assert text.index("run-name:") < text.index("jobs:")
    run_name = text.split("run-name:", 1)[1].split("on:", 1)[0]
    assert "dispatch_id" in run_name


def test_the_generic_run_workflow_carries_the_consumers_branch_for_display():
    """A consumer dispatches by commit, so ref is a SHA; the branch it was
    running for goes to the service beside it, and only when given."""
    text = (WORKFLOWS / "hil-run.yml").read_text(encoding="utf-8")
    assert "inputs.branch" in text
    assert "client_payload.branch" in text, "repository_dispatch callers need the same fields"
    assert '[ -n "$BRANCH" ] && branch_args=(--branch "$BRANCH")' in text


def test_the_generic_run_workflow_hands_suite_settings_through():
    """Which family plays the gateway is the dispatch's choice: a
    suite_env input, KEY=VALUE by commas, becomes --suite-env each, and only
    when given."""
    text = (WORKFLOWS / "hil-run.yml").read_text(encoding="utf-8")
    assert "inputs.suite_env" in text
    assert "client_payload.suite_env" in text, "repository_dispatch callers need the same fields"
    assert 'env_args+=(--suite-env "$setting")' in text


# ---- where a hardware workflow submits: the Pi's service or the portal ----

CONNECT = Path(__file__).resolve().parents[1] / "runner" / "ci-farm-connect.sh"


@pytest.mark.parametrize("name", ["hil-run.yml", "hil-painlessmesh.yml"])
def test_a_hardware_workflow_submits_where_the_repository_says_and_nowhere_else(name):
    """FARM_SUBMIT=portal moves submission from the Pi's loopback service to
    the portal (docs/portal-plan.md, phase 3); unset, nothing changes. One
    script decides, and every farm call after it uses what it decided -- no
    step still talks to the Pi's service, reads the Pi's token or runs the
    Pi's virtualenv behind the switch."""
    workflow = yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["hil"]
    assert job["env"]["FARM_VIA_PORTAL"] == "${{ vars.FARM_SUBMIT == 'portal' }}"
    assert job["env"]["FARM_PORTAL_URL"] == "${{ vars.FARM_PORTAL_URL }}"
    steps = job["steps"]
    names = [step.get("name") for step in steps]
    connect = steps[names.index("Connect to the farm")]
    assert connect["run"] == "runner/ci-farm-connect.sh"
    # The key reaches the one step that writes it to a file, and no other.
    assert connect["env"] == {"FARM_PORTAL_CI_KEY": "${{ secrets.FARM_PORTAL_CI_KEY }}"}
    assert not [step.get("name") for step in steps if step is not connect and "FARM_PORTAL_CI_KEY" in json.dumps(step)]

    after = steps[names.index("Connect to the farm") + 1:]
    submit = "\n".join(step.get("run", "") for step in after if "ci_farm_client.py" in step.get("run", ""))
    assert '--base-url "$FARM_BASE_URL"' in submit and '--token-file "$FARM_TOKEN_FILE"' in submit
    for step in after:
        script = step.get("run", "")
        assert "127.0.0.1:8090" not in script and "/etc/alteriom-hil/api-token" not in script, step.get("name")
        # The host's own health snapshots are the Pi's to take, and only
        # when the job is on the Pi.
        if "ALTERIOM_HIL_VENV" in script:
            assert "alteriom-hil-health" in script, step.get("name")
            assert "env.FARM_VIA_PORTAL != 'true'" in step.get("if", ""), step.get("name")
    python = next(step for step in steps if str(step.get("uses", "")).startswith("actions/setup-python"))
    assert python["if"] == "env.FARM_VIA_PORTAL == 'true'", "the Pi's job keeps the host's interpreter"


def _connect(tmp_path: Path, **env) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the connect script in portal mode with python and curl stubbed."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    # `python -m venv DIR` leaves DIR/bin/python, which takes the pip install.
    (stubs / "python").write_text(
        "#!/bin/sh\n"
        'mkdir -p "$3/bin"\n'
        "printf '#!/bin/sh\\nexit 0\\n' > \"$3/bin/python\"\n"
        'chmod +x "$3/bin/python"\n'
    )
    (stubs / "curl").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CURL_LOG"\n')
    for stub in stubs.iterdir():
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    github_env = tmp_path / "github-env"
    github_env.write_text("")
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    result = subprocess.run(
        ["bash", str(CONNECT)],
        cwd=tmp_path,
        env={
            "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}",
            "GITHUB_ENV": str(github_env),
            "RUNNER_TEMP": str(runner_temp),
            "CURL_LOG": str(tmp_path / "curl.log"),
            "FARM_VIA_PORTAL": "true",
            **env,
        },
        capture_output=True,
        text=True,
    )
    return result, github_env


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="runs the connect script with POSIX stubs")


@posix_only
def test_connecting_to_the_portal_puts_its_key_in_a_file_only_the_job_can_read(tmp_path):
    key = "k" * 64
    result, github_env = _connect(tmp_path, FARM_PORTAL_URL="https://espfarm.example/", FARM_PORTAL_CI_KEY=key + "\r\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert key not in result.stdout + result.stderr, "the key is never printed"
    written = dict(line.split("=", 1) for line in github_env.read_text().splitlines())
    assert written["FARM_BASE_URL"] == "https://espfarm.example"
    key_file = Path(written["FARM_TOKEN_FILE"])
    assert key_file.parent == tmp_path / "runner-temp", "the runner empties its temp directory after the job"
    assert key_file.read_text() == key, "no line ending a header would carry"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert written["FARM_PYTHON"] == str(tmp_path / "runner-temp" / "farm-venv" / "bin" / "python")
    assert (tmp_path / "curl.log").read_text().split()[-1] == "https://espfarm.example/healthz"


@posix_only
@pytest.mark.parametrize(
    "env, says",
    [
        ({"FARM_PORTAL_URL": "https://espfarm.example"}, "FARM_PORTAL_CI_KEY"),
        ({"FARM_PORTAL_CI_KEY": "k" * 64}, "FARM_PORTAL_URL"),
        ({"FARM_PORTAL_URL": "http://espfarm.example", "FARM_PORTAL_CI_KEY": "k" * 64}, "must be an https URL"),
    ],
)
def test_a_portal_the_job_cannot_reach_safely_fails_the_job_saying_what_is_missing(tmp_path, env, says):
    result, github_env = _connect(tmp_path, **env)
    assert result.returncode == 1
    assert "::error::" in result.stdout and says in result.stdout
    assert github_env.read_text() == "", "no later step submits anywhere"
    assert not (tmp_path / "runner-temp" / "farm-portal-key").exists()


def test_farm_ci_no_longer_builds_one_consumers_firmware_on_every_change():
    """The painlessMesh firmware matrix took ten minutes on every farm PR and
    made every PR's checks read as though the farm were painlessMesh's test
    suite. It runs when painlessMesh's suite, build script or profile change."""
    ci = (WORKFLOWS / "hal-ci.yml").read_text(encoding="utf-8")
    assert "build_artifacts.py" not in ci
    assert "Build HIL firmware" not in ci

    build = (WORKFLOWS / "painlessmesh-firmware-build.yml").read_text(encoding="utf-8")
    assert "build_artifacts.py" in build
    assert "suites/painlessmesh/**" in build
    assert "profiles/painlessmesh.yaml" in build
    assert "workflow_dispatch" in build


def test_the_canary_is_compiled_before_a_release_installs_it():
    """The canary is the farm's own firmware and nothing compiled it until a
    deploy did: its first build was on the release path, so an ArduinoJson
    type error in it turned three releases red instead of one pull request.

    It gets the same shape painlessMesh's matrix has -- its own workflow,
    filtered to the paths that can break it -- rather than a job in hal-ci,
    because a firmware matrix on every pull request means a one-line
    dashboard change spends ten minutes building firmware.
    """
    canary = (WORKFLOWS / "canary-build.yml").read_text(encoding="utf-8")
    assert "canary/build_artifacts.py" in canary
    assert "canary/**" in canary and "workflow_dispatch" in canary
    # Compiling is not enough: the bundle must load the way the farm loads it.
    assert "load_artifacts" in canary
    # Two firmware builds on one machine race in PlatformIO's package
    # directories, and the loser fails somewhere unrelated.
    assert "flock /tmp/alteriom-platformio.lock" in canary
    deploy = (WORKFLOWS / "deploy-farm-host.yml").read_text(encoding="utf-8")
    assert "flock /tmp/alteriom-platformio.lock" in deploy, "the deploy shares that machine"
