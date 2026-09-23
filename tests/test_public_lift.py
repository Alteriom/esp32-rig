"""What the lift carries, held against the tree it carries it out of.

`runner/ci/lift-rig.sh` is the decision about what becomes the public rig
repository (docs/public-release-plan.md, step 15), and a decision written in a
script is one that can go quietly out of date: a directory is renamed, a path
in the list stops existing, and the lift silently carries less than it meant
to. The real rehearsal runs in CI (`.github/workflows/lift-rehearsal.yml`),
which filters the history and scans it; this is the part that can be checked
without git-filter-repo, on every run of the suite.

This is the third of three ratchets on what goes public. `test_rig_package.py`
holds the boundary between the halves; `test_public_scrub.py` holds what may be
written in the shipped code; this one holds which files go at all.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIFT = ROOT / "runner" / "ci" / "lift-rig.sh"


def carried() -> list[str]:
    """The path set, read out of the script rather than copied from it."""
    text = LIFT.read_text(encoding="utf-8")
    block = text.split("PATHS=(", 1)[1].split("\n)", 1)[0]
    return [line.strip() for line in block.splitlines() if line.strip() and not line.strip().startswith("#")]


def renames() -> dict:
    text = LIFT.read_text(encoding="utf-8")
    block = text.split("RENAMES=(", 1)[1].split("\n)", 1)[0]
    found = {}
    for line in block.splitlines():
        match = re.search(r'"([^":]+):([^"]+)"', line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def test_every_path_the_lift_carries_is_in_the_tree():
    """A path that is not here is carried as nothing, and the first anybody
    would know is a public repository that does not build. The script refuses
    to run in that state; this says so before anybody runs it."""
    missing = [path for path in carried() if not (ROOT / path).exists()]
    assert not missing, f"the lift names {missing}, which this tree does not have"


def test_the_lift_carries_what_the_two_distributions_need_to_build_and_be_tested():
    """The distributions, the firmware the release carries, the hardware a rig
    is written against, the tests, and the one build and workflow that cut a
    release. A public repository that cannot cut a release is not one."""
    paths = set(carried())
    for needed in ("core", "rig", "canary", "suites", "devices", "instruments", "hardware",
                   "tests", "VERSION", "runner/ci/build-release.sh",
                   ".github/workflows/release.yml"):
        assert needed in paths, f"the lift leaves {needed} behind"


def test_the_lift_carries_none_of_what_must_not_go():
    """The scrub list, by path. The portal, the farm's own operations, the
    documents about our hosts, and our consumer's profile -- a profile is a
    consumer, and ours is not public."""
    paths = carried()
    for forbidden in ("portal", "deploy", "docs/hosts.md", "docs/runbook.md",
                      "docs/portal-plan.md", "docs/sim-host.md", "docs/farm-service.md",
                      "profiles/alteriom-firmware.yaml", "docs/adding-a-consumer.md",
                      "runner/northrelay_templates.py", ".github/workflows/portal-image.yml"):
        for path in paths:
            assert not (forbidden == path or forbidden.startswith(path + "/")), \
                f"the lift carries {forbidden} through {path}"
    # `profiles/` goes file by file for exactly that reason.
    assert "profiles" not in paths, "a profile is a consumer; name the ones that go"
    # And `runner/` is the farm's own operations; only the release build goes.
    assert all(not path.startswith("runner/") or path.startswith("runner/ci/") for path in paths)


def test_the_opening_documents_land_at_the_new_root():
    """They are written under docs/public/ here because this repository's root
    has its own README; the public repository's root is where a person looks
    first, and the lift is what puts them there."""
    moves = renames()
    for name in ("README.md", "CONTRIBUTING.md", "SECURITY.md"):
        assert moves.get(f"docs/public/{name}") == name
        assert (ROOT / "docs" / "public" / name).is_file()
    # The licence rides with each distribution (both pyprojects name it) and
    # belongs at the root as well, which is where a licence is looked for.
    assert moves.get("core/LICENSE") == "LICENSE"
    assert (ROOT / "core" / "LICENSE").is_file()


def test_the_lift_script_parses_and_can_say_what_it_would_carry():
    subprocess.run(["bash", "-n", str(LIFT)], check=True)
    printed = subprocess.run(["bash", str(LIFT), "--paths-only"],
                             capture_output=True, text=True, check=True).stdout.split()
    assert printed == carried(), "the script and its own list disagree"
