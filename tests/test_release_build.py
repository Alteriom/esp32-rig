"""A release is packages, and it is built from the tree by one script.

`runner/ci/build-release.sh` (docs/public-release-plan.md, step 13). What is
held here: the version is the farm's own and both distributions carry it; the
script stamps it in for the build and leaves the tree as it found it; and what
it writes is what a portal, a node and PyPI are each going to read.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runner" / "ci" / "build-release.sh"
CANARY_BUILD = ROOT / "canary" / "build_artifacts.py"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def project_version(name: str) -> str:
    # Read as a line rather than parsed: the rig supports Python 3.9 and
    # tomllib arrived in 3.11, and this is the line the release build stamps.
    text = (ROOT / name / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', text, flags=re.M)
    assert match, f"{name}/pyproject.toml has no version line"
    return match.group(1)


def test_both_distributions_carry_the_farms_version():
    """MAJOR.MINOR from VERSION on the trunk, in both project files and the
    same in each: one number, moved by hand in one place. A release stamps the
    commit count on as PATCH, which is what the installer writes and the
    dashboard shows -- one scheme, not two."""
    assert re.fullmatch(r"\d+\.\d+", VERSION), VERSION
    assert project_version("core") == project_version("rig") == VERSION


def _bash() -> str | None:
    return shutil.which("bash")


@pytest.mark.skipif(not _bash(), reason="the release is built with bash")
def test_the_release_is_the_version_the_installer_would_write():
    """The script's number is VERSION plus the commit count -- exactly what
    install-health-service.sh stamps into version.json."""
    printed = subprocess.run([_bash(), str(SCRIPT), "--version-only"], capture_output=True, text=True,
                             cwd=str(ROOT), check=True).stdout.strip()
    count = subprocess.run(["git", "-C", str(ROOT), "rev-list", "--count", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert printed == f"{VERSION}.{count}"


@pytest.mark.skipif(not _bash(), reason="the release is built with bash")
def test_a_release_is_built_whole_and_the_tree_is_left_as_it_was(tmp_path):
    """Both wheels at the release's version, the dashboard bundle, checksums
    that match, and a release.json naming each -- and afterwards the project
    files read exactly as before, uncommitted edits included, because the
    script puts back a copy and does not ask git."""
    pytest.importorskip("build")
    before = {name: (ROOT / name / "pyproject.toml").read_bytes() for name in ("core", "rig")}
    done = subprocess.run([_bash(), str(SCRIPT), "--out", str(tmp_path / "dist")],
                          capture_output=True, text=True, cwd=str(ROOT))
    assert done.returncode == 0, done.stderr
    after = {name: (ROOT / name / "pyproject.toml").read_bytes() for name in ("core", "rig")}
    assert after == before, "the build left the project files changed"

    out = tmp_path / "dist"
    release = json.loads((out / "release.json").read_text(encoding="utf-8"))
    assert release["schema"] == 1 and re.fullmatch(rf"{re.escape(VERSION)}\.\d+", release["version"])
    assert re.fullmatch(r"[0-9a-f]{40}", release["commit"])
    names = {entry["name"] for entry in release["packages"]}
    version = release["version"]
    assert names == {f"alteriom_hil_core-{version}-py3-none-any.whl", f"alteriom_hil-{version}-py3-none-any.whl"}
    assert release["dashboard"]["name"] == f"alteriom-hil-dashboard-{version}.tar.gz"
    assert release["dashboard"]["contract"] == 1, "the rig-view contract the bundle draws"
    assert "firmware" not in release, (
        "a build given no firmware must not claim one: a release.json that names a "
        "bundle nobody built is a release a rig refuses whole")
    # Every file named is there, and its digest is its digest.
    for entry in [*release["packages"], release["dashboard"]]:
        path = out / entry["name"]
        assert path.is_file(), entry["name"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        assert path.stat().st_size == entry["bytes"]
    sums = (out / "SHA256SUMS").read_text(encoding="utf-8")
    for entry in [*release["packages"], release["dashboard"]]:
        assert entry["sha256"] in sums and entry["name"] in sums


RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


@pytest.mark.skipif(not _bash(), reason="the release is built with bash")
def test_a_tag_attaches_everything_the_build_makes(tmp_path):
    """The workflow lists what it uploads, and the build decides what exists.

    Those are two lists, so they can disagree -- and the way they disagree is
    a release missing a file nobody notices until somebody installs it. So:
    build one, and every file in it must match something the workflow
    attaches (docs/public-release-plan.md, step 13).
    """
    pytest.importorskip("build")
    import fnmatch

    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    attach = workflow.split("gh release create", 1)[1]
    globs = [word for word in attach.split() if word.startswith("dist/")]
    assert globs, "the workflow attaches nothing"

    out = tmp_path / "dist"
    done = subprocess.run([_bash(), str(SCRIPT), "--out", str(out)],
                          capture_output=True, text=True, cwd=str(ROOT))
    assert done.returncode == 0, done.stderr
    built = sorted(path.name for path in out.iterdir())
    unattached = [name for name in built
                  if not any(fnmatch.fnmatch(f"dist/{name}", pattern) for pattern in globs)]
    assert not unattached, (
        f"the build makes {unattached}, which the release does not attach. "
        f"It attaches {globs}."
    )
    # And the other way: a pattern that matches nothing is a file that was
    # renamed and a line nobody updated.
    empty = [pattern for pattern in globs
             if not any(fnmatch.fnmatch(f"dist/{name}", pattern) for name in built)]
    assert not empty, f"the release attaches {empty}, which the build does not make"


def test_the_release_notes_do_not_promise_pypi():
    """The wheels are on the GitHub release and not on PyPI (2026-09-23), and
    the notes are where somebody reads how to install one. `pip install
    alteriom-hil` would send them to an index that has never heard of it."""
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    notes = workflow.split("> notes.md", 1)[0].split("### Install", 1)[1]
    assert "releases/download" in workflow, "the notes point at the release's own files"
    assert "$base/alteriom_hil_core-" in notes and "$base/alteriom_hil-" in notes
    assert "pip install alteriom-hil" not in notes, "that index has never heard of it"
    readme = (ROOT / "docs" / "public" / "README.md").read_text(encoding="utf-8")
    assert "Not PyPI yet" in readme
    assert "pip install alteriom-hil" not in readme, "the README makes the same promise or none"


# ---- the health check firmware the release carries -----------------------------
#
# A rig installed from a release has the health check it was built with, and
# nobody has to build firmware to bring one up (docs/public-release-plan.md,
# step 14b). Building it for real needs six PlatformIO toolchains, so what is
# held here is the seam: the keys the release build reads out of the bundle the
# canary build writes, and what it makes of them.

TARGETS = ("esp32", "esp32-c3", "esp32-c5", "esp32-c6", "esp32-s3", "esp8266")


def _canary_bundle(directory: Path, version: str = "1.0.12") -> Path:
    """A directory shaped like `canary/build_artifacts.py --out`."""
    directory.mkdir(parents=True, exist_ok=True)
    targets = {}
    for family in TARGETS:
        (directory / family).mkdir(exist_ok=True)
        image = (directory / family / "flash-image.bin")
        image.write_bytes(f"{family} image".encode())
        targets[family] = {"image": f"{family}/flash-image.bin",
                           "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                           "chip": family.replace("-", "")}
    (directory / "manifest.json").write_text(json.dumps({
        "schema": 2, "producer": "canary", "farm_sha": "c" * 40, "canary_sha": "d" * 64,
        "version": version, "targets": targets}), encoding="utf-8")
    return directory


def test_the_release_build_reads_the_keys_the_canary_build_writes():
    """Two scripts, one manifest. The release names the firmware by the
    bundle's own version, source digest and families, so a key renamed on one
    side and not the other is a release that says the wrong thing about what
    it carries -- or, here, a test that goes red first."""
    build = CANARY_BUILD.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    for key in ("version", "canary_sha", "targets"):
        assert f'"{key}"' in build, f"the canary build no longer writes {key}"
        assert f'built["{key}"]' in script, f"the release build no longer reads {key}"


@pytest.mark.skipif(not _bash(), reason="the release is built with bash")
def test_a_release_carries_the_firmware_as_one_file_it_names(tmp_path):
    """One bundle, every family, named in release.json with the digests a rig
    checks it by -- and unpacking to the layout the artifact loader reads,
    under a top directory of its own so it cannot land anywhere else."""
    pytest.importorskip("build")
    bundle = _canary_bundle(tmp_path / "hil-canary")
    out = tmp_path / "dist"
    done = subprocess.run([_bash(), str(SCRIPT), "--out", str(out), "--firmware", str(bundle)],
                          capture_output=True, text=True, cwd=str(ROOT))
    assert done.returncode == 0, done.stderr

    release = json.loads((out / "release.json").read_text(encoding="utf-8"))
    firmware = release["firmware"]
    assert firmware["name"] == "alteriom-hil-canary-1.0.12.tar.gz"
    assert firmware["version"] == "1.0.12" and firmware["revision"] == "d" * 64
    assert firmware["families"] == sorted(TARGETS)

    carried = out / firmware["name"]
    assert hashlib.sha256(carried.read_bytes()).hexdigest() == firmware["sha256"]
    assert carried.stat().st_size == firmware["bytes"]
    assert firmware["sha256"] in (out / "SHA256SUMS").read_text(encoding="utf-8")

    with tarfile.open(carried) as archive:
        names = archive.getnames()
    assert {name.split("/")[0] for name in names} == {"hil-canary"}, "one top directory, and that one"
    assert "hil-canary/manifest.json" in names
    for family in TARGETS:
        assert f"hil-canary/{family}/flash-image.bin" in names


@pytest.mark.skipif(not _bash(), reason="the release is built with bash")
def test_a_firmware_directory_that_is_not_a_bundle_is_said_not_guessed(tmp_path):
    """A release built from an empty directory would name a bundle with no
    firmware in it, and nothing downstream would know until a rig flashed
    nothing. It fails here instead, saying what makes one."""
    (tmp_path / "empty").mkdir()
    done = subprocess.run([_bash(), str(SCRIPT), "--out", str(tmp_path / "dist"),
                           "--firmware", str(tmp_path / "empty")],
                          capture_output=True, text=True, cwd=str(ROOT))
    assert done.returncode != 0
    assert "holds no manifest.json" in done.stderr and "build_artifacts.py" in done.stderr


def test_a_tag_builds_the_firmware_before_it_builds_the_release():
    """The workflow that cuts a release is the one that builds the firmware:
    the toolchains, the cache and the disk it needs are all there, and the
    build is handed the bundle rather than looking for one."""
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "canary/build_artifacts.py --out hil-canary" in workflow
    assert "build-release.sh --out dist --firmware hil-canary" in workflow
    assert (workflow.index("canary/build_artifacts.py --out hil-canary")
            < workflow.index("build-release.sh --out dist --firmware hil-canary")), \
        "the bundle has to exist before the release that carries it"
    assert "platformio" in workflow and "esptool" in workflow, "the firmware needs its tools"
    assert "~/.platformio-cores" in workflow, "six toolchains on a hosted runner want a cache"
