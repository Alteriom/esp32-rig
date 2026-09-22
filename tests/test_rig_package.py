"""The boundary the rig package is being separated along, held by a test.

The rig is to become its own package and then its own public repository
(docs/device-platform-plan.md, step 10). A repository that is separated only
in intent drifts back together in a fortnight: one convenient import from the
portal into the rig, and the extraction stops being a move and becomes a
rescue.

So the boundary is written here, and checked, before a file is moved. Nothing
below requires the split to have happened -- it requires that it stays
possible. The manifest is also the extraction list when the time comes.

Three groups, because the third is why this is not a rename:

  RIG     what a rig runs and a portal never touches
  PORTAL  what serves the dashboard and the farm's own API
  CORE    what both hold: identity, notifications, webhooks, artifact storage

The rules are the ones that would actually break the extraction:

  1. The portal does not import RIG.
  2. The rig does not import PORTAL.
  3. CORE does not import RIG.

CORE is imported by both by definition, which is what makes it core; it is
listed so that a module added to it is added on purpose rather than by an
import nobody noticed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "core" / "alteriom_hil"
RIG_DIR = ROOT / "rig" / "alteriom_hil"
HAL_DIRS = (CORE_DIR, RIG_DIR)
RUNNER = ROOT / "runner"

# What a rig runs: the hardware half. No portal module may import these -- a
# portal has no boards, no serial ports and no power relay.
RIG_ONLY = {
    "flash", "power", "serial_capture", "protocol",
    "plugins", "connectors", "pytest_plugin", "sim",
    "inventory", "instrument", "report", "mqtt",
}

# What both halves hold. Identity and keys above all: the farm decides who a
# caller is the same way wherever it runs.
#
# And descriptions: what a bundle's manifest says, what a profile is, what a
# test's outcome is recorded as and how a JUnit report becomes those records.
# So too what a board is: the families and their pins (`devices`, `pins`), a
# board and a board map (`board`), what a rig registers and what a discovery
# is reconciled into (`board_registry`), what an instrument is and how it may
# be wired (`instrument_registry`). `inventory` and `instrument` are what is
# left when those are taken out: esptool and udev, and a client on a port.
# A portal schedules by them and reads results in them with no board in
# reach, and none of them opens a port -- rule 3 is what keeps that true.
CORE = {
    "api_keys", "signin", "notify", "webhooks", "providers",
    "artifact_store", "allocation", "rig_setup", "backup",
    "artifacts", "profiles", "run_record", "junit",
    "devices", "pins", "board", "board_registry", "instrument_registry",
    # The job store: what a rig and a portal both keep, in one SQLite file.
    "jobstore",
    # What both halves of the manager say the same way: the errors, the shapes.
    "farm_shared",
}

# The portal's own. A rig never imports these; after the split they are not
# in its repository to import.
PORTAL_MODULES = {"farm_service", "portal_manager"}


def hal_name(path) -> str:
    """The name this file belongs under, for the split: its own stem, or the
    package it lives in. A subpackage is placed once, as a unit, because the
    boundary is drawn between things a rig carries and things it does not --
    and `alteriom_hil.devices` is carried or left behind whole."""
    root = next(d for d in HAL_DIRS if d in path.parents or d == path.parent)
    inside = path.relative_to(root).parts
    return inside[0][:-3] if len(inside) == 1 else inside[0]


def hal_contents() -> set:
    """Every module and package under the HAL, by that name.

    Not `glob("*.py")`: that saw the top-level modules only, so a package --
    `alteriom_hil.devices` today, any other tomorrow -- was on neither side
    of the boundary and nobody was asked which. A manifest with a hole in it
    passes and says nothing, which is the one thing a ratchet must not do.
    """
    names = set()
    for directory in HAL_DIRS:
        names |= {path.stem for path in directory.glob("*.py")} - {"__init__"}
        names |= {child.name for child in directory.iterdir()
                  if child.is_dir() and (child / "__init__.py").exists()}
    return names

# The rig's entry points, outside the HAL.
RIG_SCRIPTS = {"farm_node", "health_check", "rig_manager"}


def portal_files() -> list:
    """Every file the portal is made of: the modules named above, and
    anything under `portal/` once the split gives it a directory.

    A module named in PORTAL_MODULES that is not where it is looked for is a
    failure, not a skip: the rule this feeds reads the portal's imports, and
    a file that moved would otherwise leave it reading nothing and passing.
    """
    named = [RUNNER / f"{name}.py" for name in sorted(PORTAL_MODULES)]
    moved = [path for path in named if not path.exists()]
    assert not moved, (
        f"portal modules not found under runner/: {[path.name for path in moved]}. "
        "If they moved, say where in portal_files() -- the boundary is checked "
        "by reading them."
    )
    return named + sorted((ROOT / "portal").rglob("*.py"))


def imports_of(path: Path) -> set[str]:
    """Every module this file imports, by top-level and dotted name."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def relative_imports_of(path: Path) -> set[str]:
    """The HAL's own modules a file reaches by `from .x import y` or
    `from . import x` -- which is how the HAL imports itself, and which
    `imports_of` reports without the package's name in front."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.module:
                found.add(node.module.split(".")[0])
            else:
                found.update(alias.name for alias in node.names)
    return found


def hal_modules(names: set[str]) -> set[str]:
    """The alteriom_hil modules named by a set of imports."""
    seen = set()
    for name in names:
        match = re.fullmatch(r"alteriom_hil\.([a-z_]+)(?:\..*)?", name)
        if match:
            seen.add(match.group(1))
        elif name == "alteriom_hil":
            continue
    return seen


def from_import_names(path: Path) -> set[str]:
    """`from alteriom_hil import board, flash` names modules too."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "alteriom_hil":
            seen.update(alias.name for alias in node.names)
    return seen


def test_the_manifest_covers_every_hal_module():
    """A module in neither group is a decision nobody made.

    This is the test that will fail when somebody adds a HAL module, and it
    is meant to: which side of the split it lands on is a choice, and the
    cheapest time to make it is while writing it.
    """
    present = hal_contents() - {"mcp_server", "farm_client"}
    unplaced = present - RIG_ONLY - CORE
    assert not unplaced, (
        f"HAL modules on neither side of the rig/portal boundary: {sorted(unplaced)}. "
        "Add each to RIG_ONLY or CORE in this file, and to the table in "
        "docs/device-platform-plan.md step 10."
    )


# What the portal reaches into the rig for TODAY, and why. The test holds the
# list exactly, so the set can shrink and cannot grow.
#
#   rig_manager  farm_service.py is the base, the launcher, and the three
#                classes the mode picks between -- and the standalone one is
#                composed from the rig's mixin and the portal's, so the file
#                imports both. This goes when the base and the launcher are
#                a file of their own and the standalone class is assembled
#                where the rig is (docs/public-release-plan.md, step 12).
#   farm_node    the same file is the launcher, and in node mode starts the
#                agent in-process. Goes with the above.
#
# Six HAL modules were here once. Each was the portal wanting a *description*
# that lived beside a driver, and each is in CORE now; the last, probing,
# left with the rig's methods into rig_manager.
KNOWN_COUPLING = {"farm_node", "rig_manager"}


def test_the_portal_reaches_into_the_rig_exactly_this_much_and_no_more():
    """Rule 1, as a ratchet rather than a wish.

    The portal has no boards, and every import here is one the extraction
    must undo. Asserting the set exactly means a new one fails the build, and
    removing one fails it too -- so the list stays honest in both directions
    and nobody has to remember to celebrate.
    """
    used = set()
    for path in portal_files():
        used |= (hal_modules(imports_of(path)) | from_import_names(path)) & RIG_ONLY
        # The rig's own modules count the same as its HAL half: a portal file
        # importing `rig_manager` has the drivers in reach through it.
        used |= {name.split(".")[0] for name in imports_of(path) if name.split(".")[0] in RIG_SCRIPTS}
    new = used - KNOWN_COUPLING
    assert not new, (
        f"the portal reaches into the rig for something new: {sorted(new)}. "
        "The rig is being separated (docs/device-platform-plan.md step 10) and this "
        "is an import that would have to be undone. Take it from CORE, or move what "
        "the portal needs into CORE, rather than adding to the list."
    )
    gone = KNOWN_COUPLING - used
    assert not gone, (
        f"the portal no longer needs {sorted(gone)} from the rig -- remove it from "
        "KNOWN_COUPLING. The list is the remaining work, so it should shrink."
    )


@pytest.mark.parametrize("script", sorted(RIG_SCRIPTS))
def test_the_rig_does_not_reach_into_the_portal(script):
    """Rule 2. The agent and the health check are what a rig runs; after the
    split the portal is not in the repository they live in."""
    path = RUNNER / f"{script}.py"
    if not path.exists():
        pytest.skip(f"{script} has moved; update RIG_SCRIPTS")
    used = imports_of(path)
    trespass = {name for name in used if set(name.split(".")) & PORTAL_MODULES}
    assert not trespass, (
        f"runner/{script}.py imports the portal: {sorted(trespass)}. "
        "A rig runs this with no portal beside it."
    )


def test_core_does_not_import_the_rig():
    """Rule 3, and what makes CORE mean something.

    A module is core because a portal can hold it: no board, no serial port,
    no relay. Calling a module core does not make it so -- one import of a
    driver and the portal's image carries pyserial and esptool again, by way
    of the package that was meant to be what it could take alone.
    """
    offenders = {}
    for path in sorted(p for d in HAL_DIRS for p in d.rglob("*.py")):
        if hal_name(path) not in CORE:
            continue
        local = {name.lstrip(".") for name in relative_imports_of(path)}
        reached = (hal_modules(imports_of(path)) | from_import_names(path) | local) & RIG_ONLY
        if reached:
            offenders[path.name] = sorted(reached)
    assert not offenders, (
        f"core modules importing the rig: {offenders}. Core is what the portal "
        "holds with no hardware beside it; move the description the module "
        "wants into core, or the module out of it."
    )


def test_the_hal_does_not_import_the_portal():
    """Neither does anything the rig carries with it."""
    offenders = {}
    for path in sorted(p for d in HAL_DIRS for p in d.rglob("*.py")):
        top = hal_name(path)
        if top not in RIG_ONLY and top not in CORE:
            continue
        trespass = {name for name in imports_of(path) if set(name.split(".")) & PORTAL_MODULES}
        if trespass:
            offenders[path.name] = sorted(trespass)
    assert not offenders, (
        f"HAL modules importing the portal: {offenders}. The HAL is what the rig "
        "runs; the portal is a caller of it, never the other way round."
    )


# painlessMesh is the rig's first consumer, and it is to be no more than that:
# a profile and a suite, beside any other (docs/public-release-plan.md). Today
# it is also named in code that every consumer runs -- the default profile,
# the agent a bundle is checked against, the wire protocol. A rig somebody
# else installs for their own project would carry all of it.
#
# So the generic code is counted, as lines naming it, and the count is held
# exactly: a new mention fails, and so does a removed one, which is how the
# table stays the list of work left rather than a record of a day. What is
# painlessMesh's own -- suites/, profiles/ -- is not generic and not counted;
# neither are the simulation host's scripts, which are this farm's operations
# and stay with it.
GENERIC_SUFFIXES = {".py", ".js", ".sh", ".html", ".json", ".yaml"}
NAMES_PAINLESSMESH = {
    "core/alteriom_hil/api_keys.py": 1,
    "core/alteriom_hil/artifact_store.py": 1,
    "core/alteriom_hil/artifacts.py": 2,
    "core/alteriom_hil/board.py": 1,
    "rig/alteriom_hil/connectors.py": 2,
    "core/alteriom_hil/farm_shared.py": 2,
    "rig/alteriom_hil/flash.py": 1,
    "core/alteriom_hil/profiles.py": 3,
    "core/alteriom_hil/providers.py": 5,
    "rig/alteriom_hil/pytest_plugin.py": 1,
    "rig/alteriom_hil/report.py": 3,
    "core/alteriom_hil/run_record.py": 1,
    "runner/ci_farm_client.py": 5,
    "runner/farm_service.py": 9,
    "runner/portal_manager.py": 1,
    "runner/rig_manager.py": 4,
    "runner/flash_artifacts.py": 1,
    "runner/hil_config.py": 1,
    # The two installers name suites/painlessmesh/ because the gateway probe
    # lives there now, and a unit on the rig runs it. That the rig installs
    # a consumer's service is what step 4's `services:` in the profile ends.
    "runner/install-health-service.sh": 2,
    "runner/join-rig.sh": 1,
    "runner/setup-gateway-network.sh": 2,
    "runner/verify-rig.sh": 2,
    "runner/web/app.js": 3,
}


def lines_naming_painlessmesh() -> dict:
    found = {}
    for top in (*HAL_DIRS, RUNNER):
        for path in sorted(top.rglob("*")):
            if path.suffix not in GENERIC_SUFFIXES or "sim-host" in path.name:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            count = sum("painlessmesh" in line.lower() for line in text.splitlines())
            if count:
                found[path.relative_to(ROOT).as_posix()] = count
    return found


def test_generic_code_names_painlessmesh_only_where_it_is_known_to():
    """A ratchet, as KNOWN_COUPLING is: the counts shrink and cannot grow."""
    found = lines_naming_painlessmesh()
    grown = {name: (NAMES_PAINLESSMESH.get(name, 0), count)
             for name, count in found.items() if count > NAMES_PAINLESSMESH.get(name, 0)}
    assert not grown, (
        f"generic code names painlessMesh more than it did (was, is): {grown}. "
        "What is painlessMesh's belongs in its profile or under suites/painlessmesh; "
        "generic code reads it from the profile (docs/public-release-plan.md)."
    )
    shrunk = {name: (count, found.get(name, 0))
              for name, count in NAMES_PAINLESSMESH.items() if found.get(name, 0) < count}
    assert not shrunk, (
        f"generic code names painlessMesh less than the table says (was, is): {shrunk}. "
        "Lower the count in NAMES_PAINLESSMESH, or remove the line at zero."
    )


def test_the_manifest_and_the_directories_say_the_same_thing():
    """The split is in the tree now, so the manifest above is checkable
    against it: `core/alteriom_hil` is CORE and `rig/alteriom_hil` is RIG,
    and a module put in the wrong directory is the boundary broken however
    the manifest reads (docs/public-release-plan.md, step 12)."""
    def placed(directory):
        names = {path.stem for path in directory.glob("*.py")} - {"__init__"}
        names |= {child.name for child in directory.iterdir()
                  if child.is_dir() and (child / "__init__.py").exists()}
        return names
    # `mcp_server` and `farm_client` are a client of the farm's API, neither
    # half's: they are in the core because a client needs no hardware.
    assert placed(CORE_DIR) - {"mcp_server", "farm_client"} == CORE
    assert placed(RIG_DIR) == RIG_ONLY
    assert not (CORE_DIR / "__init__.py").exists() and not (RIG_DIR / "__init__.py").exists(), (
        "`alteriom_hil` is a namespace package: neither distribution owns its __init__.py, "
        "or installing both would have one shadow the other"
    )
