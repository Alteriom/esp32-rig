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
