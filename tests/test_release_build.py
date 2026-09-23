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
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runner" / "ci" / "build-release.sh"
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
