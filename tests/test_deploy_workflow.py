"""The farm-host deploy path: workflow shape and the update script's contract.

The Pi runs code from three places (its clone, the HIL virtualenv, and the
service snapshots under /usr/local/lib). These tests pin the invariants that
keep a deploy from breaking a running hardware job or the runner executing
the deploy itself.
"""

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / ".github" / "workflows" / "deploy-farm-host.yml"
HIL = ROOT / ".github" / "workflows" / "hil-painlessmesh.yml"
PORTAL_IMAGE = ROOT / ".github" / "workflows" / "portal-image.yml"
UPDATE = ROOT / "runner" / "update-runner.sh"
INSTALL = ROOT / "runner" / "install-health-service.sh"
LIB = ROOT / "runner" / "deploy-lib.sh"


def _load(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_deploy_runs_on_the_farm_runner_and_queues_only_behind_other_deploys():
    """The runner serialises a deploy against hardware jobs (one job at a
    time, in order). A concurrency group shared with the hardware workflows
    did not queue anything: it cancelled every pending run but the newest,
    and deploys were the ones lost. The deploy keeps a group of its own so a
    superseded pending deploy is dropped and a running one is never
    cancelled."""
    deploy, hil = _load(DEPLOY), _load(HIL)
    job = deploy["jobs"]["deploy"]
    assert job["runs-on"] == ["self-hosted", "esp32-farm"]
    assert deploy["concurrency"]["group"] == "esp32-farm-deploy"
    assert deploy["concurrency"]["cancel-in-progress"] == "false"
    assert "concurrency" not in hil, "a hardware workflow in a group gets cancelled instead of queued"


def test_deploy_job_outlives_the_scripts_wait_for_the_rig_lock():
    """update-runner.sh waits for a hand run to release the rig lock. If the
    job limit is shorter than that wait, the runner kills the deploy mid-wait
    with a timeout message instead of the script's own "rig still busy"."""
    deploy = _load(DEPLOY)
    wait = re.search(r'LOCK_WAIT="\$\{HIL_UPDATE_LOCK_WAIT:-(\d+)\}"', UPDATE.read_text(encoding="utf-8"))
    assert wait, "update-runner.sh no longer declares its lock wait"
    limit = int(deploy["jobs"]["deploy"]["timeout-minutes"]) * 60
    assert limit >= int(wait.group(1)) + 300, "the job limit must cover the lock wait plus the deploy itself"


def test_deploy_triggers_on_runtime_paths_and_on_demand():
    deploy = _load(DEPLOY)
    on = deploy[True] if True in deploy else deploy["on"]
    assert on["push"]["branches"] == ["main"]
    for prefix in ("core/**", "rig/**", "runner/**", "suites/**"):
        assert prefix in on["push"]["paths"]
    assert "ref" in on["workflow_dispatch"]["inputs"]


def test_deploy_never_restarts_the_runner_that_executes_it():
    deploy = _load(DEPLOY)
    commands = "\n".join(step.get("run", "") for step in deploy["jobs"]["deploy"]["steps"])
    assert "update-runner.sh --from-ci" in commands
    assert "set -o pipefail" in commands, "a failed deploy must fail the job, not just tee"
    for step in deploy["jobs"]["deploy"]["steps"]:
        if "pipefail" in step.get("run", "") or "deploy.log" in step.get("run", ""):
            assert step.get("shell") == "bash", f"step {step['name']!r} must run under bash"
    install = INSTALL.read_text(encoding="utf-8")
    assert "HIL_SKIP_RUNNER_RESTART" in install, "installer must honour the from-CI guard"
    assert "/etc/sudoers.d/alteriom-hil" in install and "visudo -cf" in install, \
        "installer must grant the runner user non-interactive sudo for automated deploys"
    update = UPDATE.read_text(encoding="utf-8")
    assert re.search(r'HIL_SKIP_RUNNER_RESTART="\$FROM_CI"', update)


def test_every_self_hosted_checkout_starts_from_an_empty_workspace():
    """A self-hosted runner keeps its workspace between jobs, and
    actions/checkout cannot recover one it decides is not a repository: it
    deletes the contents, then runs its own auth cleanup against the hole it
    made and exits 128. Two deploys died that way -- on a sim-host runner
    instance whose workspace was already in that state -- and a root-run step
    leaving root-owned files is the same failure from the other end.

    Asserted per job rather than trusted, because the guard has to be the
    step *before* the checkout to be worth anything.
    """
    for path in (DEPLOY, DEPLOY.parent / "canary-build.yml"):
        workflow = _load(path)
        for name, job in workflow["jobs"].items():
            steps = job.get("steps")
            if not steps:
                continue  # a reusable workflow call has none
            runs_on = job.get("runs-on")
            if "self-hosted" not in (runs_on if isinstance(runs_on, list) else [runs_on]):
                continue  # a hosted runner starts from a fresh machine
            checkout = next(
                (index for index, step in enumerate(steps)
                 if str(step.get("uses", "")).startswith("actions/checkout")),
                None,
            )
            assert checkout is not None, f"{path.name}/{name} checks nothing out"
            assert checkout > 0, f"{path.name}/{name} checks out before clearing the workspace"
            guard = steps[checkout - 1].get("run") or ""
            assert "chown -R" in guard, f"{path.name}/{name} does not take the workspace back"
            assert "rm -rf" in guard, f"{path.name}/{name} does not clear the workspace"


def test_deploy_ships_the_job_checkout_not_a_github_fetch():
    """The repository is private and the host holds no GitHub credentials."""
    deploy = _load(DEPLOY)
    steps = deploy["jobs"]["deploy"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == "0"
    update = next(s for s in steps if "update-runner.sh" in s.get("run", ""))
    assert "HIL_DEPLOY_SOURCE" in update.get("env", {})
    assert 'git rev-parse HEAD' in update["run"]
    assert 'sync_clone "$REPO" "$SOURCE" "$REF" "$ORIGIN_URL"' in UPDATE.read_text(encoding="utf-8")
    assert 'fetch --quiet "$source" HEAD' in LIB.read_text(encoding="utf-8")


def test_update_script_bootstraps_a_missing_clone_but_never_deletes():
    lib = LIB.read_text(encoding="utf-8")
    assert "git clone" in lib
    assert "is not a git clone; moving it aside" in lib
    for script in (UPDATE, LIB):
        text = script.read_text(encoding="utf-8")
        assert "rm -rf" not in text and "rm -r" not in text


def test_update_script_takes_the_rig_lock_and_verifies():
    update = UPDATE.read_text(encoding="utf-8")
    assert "/run/lock/alteriom-hil.lock" in update
    assert "flock -w" in update
    assert "install-health-service.sh" in update
    assert "verify-rig.sh --quick" in update
    assert "/healthz" in update
    assert "alteriom-hil-health" in update and "--fail-unhealthy" in update
    install = INSTALL.read_text(encoding="utf-8")
    assert "if ! sudo systemctl start alteriom-hil-health.service" in install, \
        "an unhealthy snapshot is a report, not an install failure"


def test_shell_scripts_parse():
    for script in (UPDATE, INSTALL, LIB, ROOT / "runner" / "setup-runner.sh", ROOT / "runner" / "node-update.sh",
                   ROOT / "runner" / "node-control.sh", ROOT / "runner" / "join-rig.sh"):
        subprocess.run(["bash", "-n", str(script)], check=True)
        assert script.stat().st_mode & 0o111, f"{script.name} must be executable"


def test_installer_refreshes_every_snapshot_a_unit_executes():
    """Every file a systemd unit runs from /usr/local/lib must be installed by
    the installer, not only by the one-time bring-up script that created it.

    `gateway_probe_server.py` was written only by `setup-gateway-network.sh`,
    so the `/delay` route the transport-error suite row needs never reached the
    rig: a deploy refreshed the service and left a months-old probe running,
    and the row failed for a reason that was not the library under test.
    """
    install = INSTALL.read_text(encoding="utf-8")
    executed = set()
    for script in (ROOT / "runner").glob("*.sh"):
        for match in re.finditer(
            r"ExecStart=\S+ /usr/local/lib/alteriom-hil/([A-Za-z0-9_.-]+\.py)",
            script.read_text(encoding="utf-8"),
        ):
            executed.add(match.group(1))
    assert "gateway_probe_server.py" in executed, "the probe unit's ExecStart moved"
    for name in sorted(executed):
        # Installed from wherever it lives -- beside the service, or beside
        # the suite it belongs to -- under the name the unit runs.
        assert re.search(rf'"\$HERE/(?:[^"]*/)?{re.escape(name)}"', install), (
            f"a unit runs /usr/local/lib/alteriom-hil/{name}, but the installer "
            "never writes it, so a deploy leaves the running copy stale"
        )


def test_a_release_also_runs_its_canary_through_the_portal_without_gating_on_it():
    deploy = DEPLOY.read_text(encoding="utf-8")
    step = deploy.split("- name: Install the canary on the portal and check the rig through it", 1)[1]
    step = step.split("- name: Judge the portal canary", 1)[0]
    assert "if: ${{ vars.FARM_PORTAL_URL != '' }}" in step
    assert "continue-on-error: true" in step, "the local run decides the release while CI moves"
    assert '--base-url "$FARM_PORTAL_URL"' in step and "--pin" in step
    # The key is a file only this step can read, removed when it ends, and
    # never an argument or an echoed value.
    assert "umask 077" in step and "trap 'rm -f \"$key_file\"' EXIT" in step
    assert '--token-file "$key_file"' in step and "printf '%s' \"$FARM_PORTAL_KEY\"" in step


def test_a_node_is_released_through_the_portal_and_gated_on_the_canary_there():
    """FARM_DEPLOY=portal: no job runs on the Pi. The release is a bundle of
    the whole history, published to the portal, installed by every node, and
    green only once the canary has passed through the portal."""
    workflow = _load(DEPLOY)
    jobs = workflow["jobs"]
    assert jobs["deploy"]["if"] == "vars.FARM_DEPLOY != 'portal'"
    release = jobs["release"]
    assert release["if"] == "vars.FARM_DEPLOY == 'portal'"
    assert release["needs"] == "canary"
    assert release["runs-on"] == "ubuntu-latest", "a node has no runner, and the release job needs none"
    steps = release["steps"]
    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == "0", "a shallow bundle needs history a node may not have"
    names = [step.get("name") for step in steps]
    order = ["Connect to the portal", "Make the release", "Publish the release to the portal",
             "Wait for every node to run it", "Install the canary on the portal and check every board",
             "Judge the canary"]
    assert [name for name in names if name in order] == order
    script = {step.get("name"): step.get("run", "") for step in steps}
    assert "git bundle create release.bundle HEAD" in script["Make the release"]
    assert "ci_farm_release.py publish" in script["Publish the release to the portal"]
    assert "ci_farm_release.py wait" in script["Wait for every node to run it"]
    connect = next(step for step in steps if step.get("name") == "Connect to the portal")
    assert connect["env"] == {"FARM_PORTAL_KEY": "${{ secrets.FARM_PORTAL_KEY }}"}
    assert "umask 077" in connect["run"] and '"$RUNNER_TEMP/farm-portal-key"' in connect["run"]
    assert '--base-url "$FARM_PORTAL_URL"' in script["Install the canary on the portal and check every board"]
    assert "--pin" in script["Install the canary on the portal and check every board"]
    judge = next(step for step in steps if step.get("name") == "Judge the canary")
    assert "continue-on-error" not in judge, "the canary through the portal is the gate now"


def test_a_node_installs_releases_through_a_unit_of_its_own_and_needs_no_runner():
    install = INSTALL.read_text(encoding="utf-8")
    assert "exit 1" not in install.split("if [ -z \"$RUNNER_UNIT\" ]; then", 1)[1].split("fi", 1)[0], \
        "no runner is a node, not an error"
    assert "/etc/systemd/system/alteriom-hil-update.service" in install
    assert "PathExists=/var/lib/alteriom-hil/update/request.json" in install
    assert "ExecStart=/usr/local/lib/alteriom-hil/node-update.sh" in install
    assert '"$HERE/node-update.sh" /usr/local/lib/alteriom-hil/node-update.sh' in install
    assert "grep -qx 'ALTERIOM_HIL_FARM_MODE=node'" in install
    assert "systemctl disable --now alteriom-hil-update.path" in install, "only a node installs releases"
    service = install.split("/etc/systemd/system/alteriom-hil-update.service", 1)[1].split("EOF", 2)[1]
    assert "NoNewPrivileges" not in service, "the install needs sudo"
    update = UPDATE.read_text(encoding="utf-8")
    assert "--unattended) UNATTENDED=1" in update
    assert 'git bundle list-heads "$SOURCE"' in update, "a bundle is a source like a checkout"


def test_a_node_carries_out_what_needs_sudo_through_a_control_unit_of_its_own():
    install = INSTALL.read_text(encoding="utf-8")
    assert '"$HERE/node-control.sh" /usr/local/lib/alteriom-hil/node-control.sh' in install
    assert "PathExists=/var/lib/alteriom-hil/update/control.json" in install
    assert "ExecStart=/usr/local/lib/alteriom-hil/node-control.sh" in install
    assert "enable --now alteriom-hil-update.path alteriom-hil-control.path" in install
    service = install.split("/etc/systemd/system/alteriom-hil-control.service", 1)[1].split("EOF", 2)[1]
    assert "NoNewPrivileges" not in service


def test_a_host_with_no_runner_unit_gets_past_looking_for_one(tmp_path):
    """systemctl list-unit-files exits 1 when no unit matches. Under
    `set -euo pipefail` the lookup's assignment failed and the installer
    stopped there, silently, on the first node install after the Pi's runner
    was removed."""
    import os
    import subprocess
    import sys

    import pytest

    if sys.platform == "win32":
        pytest.skip("runs the installer's lines with a POSIX stub")
    install = INSTALL.read_text(encoding="utf-8")
    start = install.index('RUNNER_UNIT="${HIL_RUNNER_UNIT:-')
    end = install.index("fi\n", install.index('if [ -z "$RUNNER_UNIT" ]; then')) + 3
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "systemctl").write_text("#!/bin/sh\nexit 1\n")
    (stubs / "systemctl").chmod(0o755)
    script = "set -euo pipefail\n" + install[start:end] + 'echo "reached [$RUNNER_UNIT]"\n'
    env = {key: value for key, value in os.environ.items() if key != "HIL_RUNNER_UNIT"}
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            env={**env, "PATH": f"{stubs}:{os.environ.get('PATH', '')}"})
    assert result.returncode == 0, result.stderr
    assert "reached []" in result.stdout and "installing without one (a node)" in result.stdout


def test_an_attached_host_runs_a_node_agent_beside_the_service_on_its_own_state():
    install = INSTALL.read_text(encoding="utf-8")
    assert "/etc/systemd/system/alteriom-hil-node.service" in install
    # Arguments, not Environment=: the runtime file would override the state
    # directory and the node would share the main service's job store.
    assert "alteriom-hil-service --mode node --state /var/lib/alteriom-hil/node --port 8091" in install
    assert "grep -qx 'ALTERIOM_HIL_FARM_ATTACHED=1'" in install
    assert "systemctl disable --now alteriom-hil-node.service" in install, "a host not attached runs no agent"


def test_installer_refreshes_the_node_agent_the_service_imports():
    """The agent is the installed package now, not a file beside the service.

    A deploy that refreshed only the copied files would have run a new
    service with an old agent, or none; what refreshes it is the pip install
    the update script does, and what the installer must no longer do is leave
    an older deploy's copy where somebody would read it.
    """
    install = INSTALL.read_text(encoding="utf-8")
    assert '"$HERE/farm_node.py"' not in install, "the agent is not copied any more"
    removed = install.split("sudo rm -f \\", 1)[1][:600]
    assert "/usr/local/lib/alteriom-hil/farm_node.py" in removed, (
        "an older deploy's copy is left where the next reader believes it"
    )
    launcher = (ROOT / "rig" / "alteriom_hil" / "launcher.py").read_text(encoding="utf-8")
    assert "from alteriom_hil import farm_node" in launcher, "the launcher is what starts it"
    assert "alteriom-hil-service" in install, "and the unit runs the launcher by name"


def test_installer_restarts_the_gateway_probe_it_refreshes():
    install = INSTALL.read_text(encoding="utf-8")
    assert "systemctl restart alteriom-hil-gateway-probe.service" in install
    assert "/etc/systemd/system/alteriom-hil-gateway-probe.service" in install, \
        "a host without the gateway test network has no probe unit to restart"


def test_a_rig_joins_with_one_command_that_keeps_its_secrets_off_command_lines():
    join = (ROOT / "runner" / "join-rig.sh").read_text(encoding="utf-8")
    install = INSTALL.read_text(encoding="utf-8")
    setup = (ROOT / "runner" / "setup-runner.sh").read_text(encoding="utf-8")
    # Piped into bash: nothing runs until the whole script has been read.
    assert join.rstrip().endswith('if [ "${JOIN_RIG_LIB:-0}" != 1 ]; then\n  main "$@"\nfi')
    assert 'die "run this as the user the rig runs as (it uses sudo), not as root"' in join
    # The token on curl's stdin, the key in curl's configuration from stdin.
    assert "--data-binary @-" in join and "--config -" in join
    assert "Bearer $key" not in join.replace('printf \'header = "Authorization: Bearer %s"\\n\' "$key"', "")
    assert "sha256sum --check" in join, "the release is checked against the digest the portal gave"
    assert 'install -m 0640 -o root -g "$group" /dev/stdin "$KEY_FILE"' in join
    assert 'HIL_JOIN_PORTAL_URL="$portal" HIL_JOIN_WORKER_NAME="$name"' in join
    # The installer makes the host a node before it applies the configuration.
    joined = install.index("config join --portal")
    assert joined < install.index("config apply --no-restart")
    assert '[ "${HIL_JOINING:-0}" = 1 ] && exit 0' in setup, "no GitHub runner steps for a joining rig"


def test_the_portal_follows_main_by_the_digest_just_pushed():
    """The rigs follow the portal's release; nothing else updated the portal
    itself, and on 2026-09-14 it took a hand rollout, twice, before a rig run
    could be submitted. main now rolls the portal onto the image it pushed."""
    workflow = _load(PORTAL_IMAGE)
    image, rollout = workflow["jobs"]["image"], workflow["jobs"]["rollout"]
    assert image["outputs"]["digest"] == "${{ steps.build.outputs.digest }}"
    assert rollout["needs"] == "image"
    # Never from a pull request: a branch must not replace the running portal.
    assert "pull_request" in rollout["if"] and "refs/heads/main" in rollout["if"]
    # By digest, not by a moving tag the node already has cached.
    assert rollout["env"]["IMAGE"].endswith("@${{ needs.image.outputs.digest }}")
    assert rollout["concurrency"]["cancel-in-progress"] == "false"
    steps = {step.get("name"): step for step in rollout["steps"]}
    key = steps["Key"]["run"]
    assert "INFRA_VPS_SSH_KEY" in key and "exit 0" in key, "no key: say so, do not fail every merge"
    remote = steps["Set the image and wait for the rollout"]["run"]
    assert "kubectl -n espfarm set image deploy/espfarm-portal portal=" in remote
    assert "rollout status" in remote
    assert "$INFRA_VPS_SSH_KEY" not in remote, "the key is a file, never an argument"


def test_the_promoted_portal_digest_is_recorded_in_the_cluster_repository():
    """vps-infra-cluster describes the cluster and refuses a full apply while
    an application-owned image differs from its manifest. On 2026-09-14 the
    pin there was bumped by hand and never applied, and the live portal was
    then set by hand to an image the pin never named. The release pipeline
    now records what it promoted, and only after the rollout was verified."""
    rollout = _load(PORTAL_IMAGE)["jobs"]["rollout"]
    names = [step.get("name") for step in rollout["steps"]]
    order = [
        "Set the image and wait for the rollout",
        "Every running pod is on the digest",
        "The portal answers",
        "Reconcile the promoted digest to vps-infra-cluster",
    ]
    assert [name for name in names if name in order] == order, "reconcile last, after the checks"
    steps = {step.get("name"): step for step in rollout["steps"]}
    reconcile = steps["Reconcile the promoted digest to vps-infra-cluster"]
    assert reconcile["env"]["GH_TOKEN"] == "${{ secrets.INFRA_REPO_TOKEN }}"
    script = reconcile["run"]
    assert "k8s/espfarm/deployment.yaml" in script
    assert "^sha256:[0-9a-f]{64}$" in script, "a malformed digest is never written"
    assert "gh pr create --repo Alteriom/vps-infra-cluster" in script
    assert "release/espfarm-portal-" in script
    # An older pin PR still open conflicts and would pin a digest the cluster
    # left behind: the newer rollout closes it (vps-infra-cluster#169).
    closing = script.split("Infrastructure reconciliation:", 1)[1]
    assert "gh pr close" in closing and "--delete-branch" in closing and "Superseded by" in closing
    assert 'select(.headRefName != \\"${branch}\\")' in closing, "never the PR this run opened"
    # The comment above the pin names the commit it now is.
    assert '-v note="$note"' in script and 'held = substr(held, 1, RLENGTH) "# " note' in script
    checkout = [step for step in rollout["steps"] if step.get("uses", "").startswith("actions/checkout")]
    assert checkout and checkout[0]["with"]["repository"] == "Alteriom/vps-infra-cluster"


# ---- a release build, which a rig's providers act on (docs/providers.md) ----


def test_the_painlessmesh_workflow_can_say_a_run_is_a_release_build():
    import json

    hil = _load(HIL)
    on = hil[True] if True in hil else hil["on"]
    release = on["workflow_dispatch"]["inputs"]["release"]
    assert release["type"] == "boolean" and release["default"] == "false"
    resolve = hil["jobs"]["resolve"]
    assert resolve["outputs"]["release"] == "${{ steps.ref.outputs.release }}"
    step = next(step for step in resolve["steps"] if step.get("id") == "ref")
    assert "inputs.release" in step["env"]["RELEASE_REQUESTED"]
    assert "github.event.client_payload.release" in step["env"]["RELEASE_REQUESTED"]

    submit = next(step for step in hil["jobs"]["hil"]["steps"] if "ci_farm_client.py" in step.get("run", ""))
    assert submit["env"]["HIL_RELEASE"] == "${{ needs.resolve.outputs.release }}"
    script = submit["run"]
    assert 'env_args+=(--suite-env "ALTERIOM_HIL_RUN_KIND=release")' in script
    assert '"${env_args[@]}"' in script
    # The only suite setting it hands over: a provider's link, policy and
    # budget are the rig's, and the farm refuses them from a dispatch.
    assert "CALLMEBOT" not in json.dumps(hil)
    assert script.count("--suite-env") == 1


def _resolve_release(tmp_path, ref: str, requested: str) -> str:
    """Run the resolve step's script with git stubbed: a v2.0.3 tag and a
    release/2.0 branch exist, nothing else does."""
    import os
    import shutil

    import pytest

    if not shutil.which("bash"):
        pytest.skip("needs bash")
    step = next(step for step in _load(HIL)["jobs"]["resolve"]["steps"] if step.get("id") == "ref")
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    git = stubs / "git"
    git.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do last="$a"; done\n'
        'case "$*" in\n'
        '  *--tags*) [ "$last" = "refs/tags/v2.0.3" ] && printf "%s\\t%s\\n" aaaa "$last" ;;\n'
        '  *--heads*) [ "$last" = "refs/heads/release/2.0" ] && printf "%s\\t%s\\n" bbbb "$last" ;;\n'
        '  *) printf "%s\\t%s\\n" cccc refs/heads/any ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    git.chmod(0o755)
    output = tmp_path / "github-output"
    output.write_text("")
    env = {**os.environ, "PATH": f"{stubs}:{os.environ.get('PATH', '')}", "REF": ref,
           "RELEASE_REQUESTED": requested, "GITHUB_OUTPUT": str(output)}
    result = subprocess.run(["bash", "-c", step["run"]], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return outputs["release"]


def test_a_release_is_asked_for_or_is_a_version_tag_or_a_release_branch(tmp_path):
    assert _resolve_release(tmp_path, "main", "false") == "false"
    assert _resolve_release(tmp_path, "main", "true") == "true"
    assert _resolve_release(tmp_path, "v2.0.3", "false") == "true"
    assert _resolve_release(tmp_path, "v9.9.9", "false") == "false", "a name like a tag is not one"
    assert _resolve_release(tmp_path, "release/2.0", "false") == "true"
    assert _resolve_release(tmp_path, "release/none", "false") == "false"
    assert _resolve_release(tmp_path, "feature/v2", "false") == "false"
    assert _resolve_release(tmp_path, "a" * 40, "false") == "false"

