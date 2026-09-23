"""The simulation host: the x86 self-hosted runner that takes every job
that needs no hardware off GitHub-hosted minutes.

These tests pin the routing (which job runs where, and how that is decided),
the deploy workflow's contract, the no-root rule of the host's update path,
and the shared clone helper both hosts deploy with.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
RUNNER = ROOT / "runner"
# The deploy helpers the sim host shares with the rig are the rig's
# (docs/public-release-plan.md, step 12g).
LIB = ROOT / "rig" / "deploy-lib.sh"
UPDATE = RUNNER / "update-sim-host.sh"
VERIFY = RUNNER / "verify-sim-host.sh"
SETUP = RUNNER / "setup-sim-host.sh"
HARDEN = RUNNER / "harden-sim-host.sh"
DEPLOY = WORKFLOWS / "deploy-sim-host.yml"

# Runner choice is measured per run by the reusable runner-label workflow, not
# declared in a variable: a simulation host that is offline, busy or broken must
# cost a slower job, never a failed one.
CI_RUNNER = "${{ needs.runner.outputs.label }}"
SELECTOR = "./.github/workflows/runner-label.yml"
# A hardware job: the Pi, unless the repository submits to the portal.
HARDWARE_RUNNER = (
    "${{ vars.FARM_SUBMIT == 'portal' && needs.runner.outputs.label"
    " || fromJSON('[\"self-hosted\", \"esp32-farm\"]') }}"
)


def _load(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _on(workflow):
    return workflow[True] if True in workflow else workflow["on"]


def test_no_hardware_jobs_take_the_selected_runner_and_hardware_jobs_do_not():
    hal_ci = _load(WORKFLOWS / "hal-ci.yml")["jobs"]
    hil = _load(WORKFLOWS / "hil-painlessmesh.yml")["jobs"]
    # painlessMesh's firmware build has its own path-gated workflow now; it is
    # still a no-hardware job and still follows the selection.
    build = _load(WORKFLOWS / "painlessmesh-firmware-build.yml")["jobs"]
    for jobs in (hal_ci, hil, build):
        assert jobs["runner"]["uses"] == SELECTOR
    for job_id, jobs in (("test", hal_ci), ("firmware", build), ("resolve", hil), ("simulate", hil)):
        job = jobs[job_id]
        assert job["runs-on"] == CI_RUNNER, job_id
        # Without the dependency the expression resolves to nothing and the job
        # fails to start, so the two have to be asserted together.
        needs = job["needs"]
        assert "runner" in ([needs] if isinstance(needs, str) else needs), job_id
    # The hardware job waits for its run where the run is submitted. To the
    # Pi's own service it is on the Pi -- only the Pi carries esp32-farm, and
    # the service listens on loopback. To the portal (FARM_SUBMIT=portal,
    # docs/portal-plan.md phase 3) the job holds no hardware and follows the
    # selection like any other; the portal leases the run to the boards.
    assert hil["hil"]["runs-on"] == HARDWARE_RUNNER
    assert "runner" in hil["hil"]["needs"]
    # The generic trigger is a hardware job and nothing else: it has no
    # simulation stage to place elsewhere, only the runner choice for when it
    # submits to the portal.
    generic = _load(WORKFLOWS / "hil-run.yml")["jobs"]
    assert list(generic) == ["runner", "hil"]
    assert generic["runner"]["uses"] == SELECTOR
    assert generic["hil"]["runs-on"] == HARDWARE_RUNNER
    assert generic["hil"]["needs"] == "runner"
    assert _load(WORKFLOWS / "deploy-farm-host.yml")["jobs"]["deploy"]["runs-on"] == ["self-hosted", "esp32-farm"]
    assert _load(DEPLOY)["jobs"]["deploy"]["runs-on"] == ["self-hosted", "esp32-sim"]


def test_jobs_on_a_persistent_host_install_into_a_job_virtualenv():
    """Editable installs into a shared tool cache would leak between jobs."""
    hal_ci = _load(WORKFLOWS / "hal-ci.yml")["jobs"]
    hil = _load(WORKFLOWS / "hil-painlessmesh.yml")["jobs"]
    build = _load(WORKFLOWS / "painlessmesh-firmware-build.yml")["jobs"]
    for job in (hal_ci["test"], build["firmware"], hil["simulate"]):
        names = [s.get("name", "") for s in job["steps"]]
        first_pip = next(i for i, s in enumerate(job["steps"]) if "pip install" in s.get("run", ""))
        assert "Create job virtualenv" in names[:first_pip], job["name"]
    cache = next(s for s in build["firmware"]["steps"] if str(s.get("uses", "")).startswith("actions/cache"))
    assert cache["if"] == "runner.environment == 'github-hosted'"


def test_deploy_sim_host_ships_both_checkouts_and_needs_no_credentials():
    deploy = _load(DEPLOY)
    on = _on(deploy)
    assert on["push"]["branches"] == ["main"]
    assert "runner/**" in on["push"]["paths"]
    assert set(on["workflow_dispatch"]["inputs"]) == {"ref", "simulator_ref"}
    assert deploy["concurrency"]["group"] == "esp32-sim-host"
    steps = deploy["jobs"]["deploy"]["steps"]
    farm, sim = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")]
    assert farm["with"]["fetch-depth"] == "0"
    assert sim["with"]["repository"] == "Alteriom/painlessMesh-simulator"
    assert sim["with"]["submodules"] == "recursive"
    update = next(s for s in steps if "update-sim-host.sh" in s.get("run", ""))
    assert update["shell"] == "bash" and "set -o pipefail" in update["run"]
    assert "--from-ci" in update["run"] and "git rev-parse HEAD" in update["run"]
    assert set(update["env"]) == {"HIL_DEPLOY_SOURCE", "HIL_SIMULATOR_SOURCE"}


def test_update_path_never_needs_root():
    """Jobs run repository code as the runner user; nothing it can reach may
    escalate. The farm host needs sudo for systemd units; this host does not."""
    update = UPDATE.read_text(encoding="utf-8")
    code = "\n".join(l for l in update.splitlines() if not l.lstrip().startswith("#"))
    assert "sudo" not in code
    assert "docker build --pull --target builder" in update
    assert "docker builder prune" in update and "docker image prune" in update
    assert "verify-sim-host.sh" in update and "pytest" in update
    verify = VERIFY.read_text(encoding="utf-8")
    assert "sudo rm /etc/sudoers.d/alteriom-hil" in verify, "passwordless sudo is reported as a finding"
    # The preflight is read-only: sudo appears only in non-interactive probes
    # and in the hint text it prints, never as a command of its own.
    assert "sudo -n ufw status" in verify and "sudo -v" not in verify
    assert not any(l.strip().startswith("sudo") for l in verify.splitlines())


def test_harden_sim_host_closes_the_known_holes():
    harden = HARDEN.read_text(encoding="utf-8")
    assert "Refusing to disable password SSH" in harden
    for line in ("PasswordAuthentication no", "PermitRootLogin no", "AllowUsers $SSH_USER"):
        assert line in harden
    assert "ufw default deny incoming" in harden
    assert '"ip": "127.0.0.1"' in harden, "Docker published ports must not bypass ufw"
    assert '"no-new-privileges": True' in harden
    assert "rfkill block wlan" in harden
    assert "NOPASSWD: ALL" not in harden, "the sim host never grants passwordless sudo"
    assert "unattended-upgrades" in harden
    code = [l.strip() for l in harden.splitlines() if not l.lstrip().startswith("#")]
    assert not any(l.startswith(("sudo reboot", "reboot", "sudo shutdown")) for l in code), "reboots stay manual"
    # current security fixes first, then the public-SSH mode and the root lock
    assert "full-upgrade" in harden
    assert 'SSH_PUBLIC="${HIL_SSH_PUBLIC:-0}"' in harden
    assert "ufw limit 22/tcp" in harden and "fail2ban" in harden
    assert "sudo passwd -l root" in harden and "HIL_KEEP_ROOT_PASSWORD" in harden
    assert "Refusing to lock root" in harden, "never lock root without a sudo-capable admin"
    verify = VERIFY.read_text(encoding="utf-8")
    for probe in ("PermitRootLogin no", "passwd -S root", "fail2ban", "-security"):
        assert probe in verify, probe
    # A wrong LAN default locks new logins out: detect the host's subnet
    # instead, and let the preflight catch a rule that excludes it.
    assert "detect_lan_cidr" in harden and 'LAN_CIDR="${HIL_LAN_CIDR:-$(detect_lan_cidr' in harden
    assert "subnet_of" in verify and "new logins from your LAN are refused" in verify
    # The installer's NOPASSWD rule is removed, validated, and restored on rejection.
    assert "90-cloud-init-users" in harden and "visudo -cf" in harden and 'sudo cp "$backup" "$rule"' in harden
    assert "rev-list --count HEAD..origin/main" in verify


def test_host_inventory_documents_every_forward_without_secrets():
    hosts = (ROOT / "docs" / "hosts.md").read_text(encoding="utf-8")
    assert "alteriom03" in hosts and "192.168.5.167" in hosts and "2224" in hosts
    assert "HIL_SSH_PUBLIC=1" in hosts
    assert "esp32-sim" in hosts and "esp32-farm" in hosts
    lowered = hosts.lower()
    assert "password:" not in lowered and "passwd:" not in lowered, "never record a credential"


def test_setup_sim_host_records_the_host_settings_the_other_scripts_read():
    setup = SETUP.read_text(encoding="utf-8")
    assert "docker.io docker-buildx" in setup
    assert "usermod -aG docker" in setup
    assert "esp32-sim" in setup and "esp32-farm" in setup
    assert "ALTERIOM_CI_ON_SIM_HOST=true" in setup
    for script in (UPDATE, VERIFY):
        text = script.read_text(encoding="utf-8")
        assert 'env_value ALTERIOM_SIM_REPO "$ENV_FILE"' in text
        assert 'env_value ALTERIOM_SIM_VENV "$ENV_FILE"' in text


def test_shell_scripts_parse_and_are_executable():
    for script in (LIB, UPDATE, VERIFY, SETUP, HARDEN):
        subprocess.run(["bash", "-n", str(script)], check=True)
        assert script.stat().st_mode & 0o111, f"{script.name} must be executable"


# ---------------------------------------------------------------- sync_clone


def _git(*args, cwd):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """A bare 'origin' with one commit on main, plus a working clone of it."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "rig").mkdir()
    (work / "rig" / "pyproject.toml").write_text("[project]\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "one", cwd=work)
    bare = tmp_path / "origin.git"
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    return bare, work


def _sync(tmp_path, target, source, ref, origin_url):
    script = f'''
set -euo pipefail
say() {{ echo "SAY $1"; }}
. "{LIB}"
sync_clone "{target}" "{source}" "{ref}" "{origin_url}"
git -C "{target}" rev-parse HEAD
'''
    return subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True,
                          env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(tmp_path)})


def test_sync_clone_creates_the_clone_from_a_local_source_and_points_origin_at_github(origin, tmp_path):
    bare, work = origin
    head = _git("rev-parse", "HEAD", cwd=work)
    target = tmp_path / "deploy"
    r = _sync(tmp_path, target, work, head, "https://example.invalid/farm")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == head
    assert _git("remote", "get-url", "origin", cwd=target) == "https://example.invalid/farm"
    assert "SAY no clone at" in r.stdout


def test_sync_clone_moves_a_non_git_directory_aside_instead_of_deleting_it(origin, tmp_path):
    bare, work = origin
    target = tmp_path / "deploy"
    target.mkdir()
    (target / "keep-me.txt").write_text("operator data\n")
    r = _sync(tmp_path, target, work, "main", str(bare))
    assert r.returncode == 0, r.stderr
    aside = [p for p in tmp_path.iterdir() if p.name.startswith("deploy.pre-deploy-")]
    assert len(aside) == 1 and (aside[0] / "keep-me.txt").read_text() == "operator data\n"
    assert (target / "rig" / "pyproject.toml").exists()


def test_sync_clone_refuses_a_source_that_is_not_at_the_requested_sha(origin, tmp_path):
    bare, work = origin
    r = _sync(tmp_path, tmp_path / "deploy", work, "0" * 40, str(bare))
    assert r.returncode != 0
    assert "not the requested" in r.stderr


def test_sync_clone_fast_forwards_a_branch_from_origin_without_a_source(origin, tmp_path):
    bare, work = origin
    target = tmp_path / "deploy"
    assert _sync(tmp_path, target, "", "main", str(bare)).returncode == 0
    (work / "two.txt").write_text("2\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "two", cwd=work)
    _git("push", "-q", str(bare), "main", cwd=work)
    r = _sync(tmp_path, target, "", "main", str(bare))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == _git("rev-parse", "HEAD", cwd=work)
    r = _sync(tmp_path, target, "", "no-such-branch", str(bare))
    assert r.returncode != 0 and "unknown ref" in r.stderr


def test_sync_clone_deploys_over_hand_edits_and_keeps_them_as_a_patch(origin, tmp_path):
    # Two deploys from main failed with "local changes would be overwritten"
    # after a fix had been copied into the farm clone by hand. A deploy must
    # not be blocked by that, and must not throw the operator's work away.
    bare, work = origin
    target = tmp_path / "deploy"
    assert _sync(tmp_path, target, "", "main", str(bare)).returncode == 0
    (target / "rig" / "pyproject.toml").write_text("[project]\nname = 'edited-by-hand'\n")
    (work / "two.txt").write_text("2\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "two", cwd=work)
    _git("push", "-q", str(bare), "main", cwd=work)
    r = _sync(tmp_path, target, "", "main", str(bare))
    assert r.returncode == 0, r.stderr
    assert "uncommitted changes" in r.stdout
    assert (target / "two.txt").exists()
    assert (target / "rig" / "pyproject.toml").read_text() == "[project]\n"
    patches = [p for p in tmp_path.iterdir() if p.name.startswith("deploy.local-changes-")]
    assert len(patches) == 1 and "edited-by-hand" in patches[0].read_text()


def test_the_runner_selector_falls_back_to_hosted_on_every_uncertainty():
    """The asymmetry that makes the simulation host optional.

    Guessing "hosted" costs a slower job; guessing "sim host" costs a job that
    queues and never starts. So the label must default to hosted and only be
    raised to esp32-sim on positive evidence that a runner is online and idle.
    """
    selector = _load(WORKFLOWS / "runner-label.yml")
    pick = selector["jobs"]["pick"]
    assert pick["runs-on"] == "ubuntu-latest", "the selector itself must not need the host it checks for"
    script = "\n".join(step.get("run", "") for step in pick["steps"])
    assert "label=ubuntu-latest" in script, "hosted must be the starting value"
    assert 'status == "online"' in script and "busy == false" in script
    # A registered-but-busy or registered-but-offline runner must not win.
    assert "idle" in script
    assert selector["on"]["workflow_call"]["outputs"]["label"]["value"]
