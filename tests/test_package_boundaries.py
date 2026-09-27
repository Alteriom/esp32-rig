"""What may import what: the boundary between core/ and rig/, as a test.

`alteriom_hil` is one import name split across two distributions:
`alteriom-hil-core` (core/) is what a portal holds with no hardware beside
it, and `alteriom-hil` (rig/) is everything a rig carries besides. A portal
extends the core with a half of its own, which this repository does not
contain. So:

1. core never imports a module that lives only in rig/ -- one import of a
   driver and a portal's image carries pyserial and esptool again;
2. neither half imports a portal's modules, except the launcher asking
   whether one is installed, inside a try/except ImportError;
3. every module lives on exactly one side.

The rules read the directories, so a new module is on a side the moment it
exists; nobody keeps a list. (The farm that consumes this repository keeps
a fuller ratchet of its own; this is the part that belongs here, where a
change to core/ or rig/ is made first.)
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "core" / "alteriom_hil"
RIG_DIR = ROOT / "rig" / "alteriom_hil"
PORTAL_MODULES = {"portal_manager", "workspaces"}
# The one file allowed to ask for the portal's half, and only guarded.
MAY_ASK_FOR_THE_PORTAL = {"launcher.py"}


def modules_in(directory: Path) -> set[str]:
    names = {path.stem for path in directory.glob("*.py")} - {"__init__"}
    names |= {child.name for child in directory.iterdir()
              if child.is_dir() and child.name != "__pycache__" and any(child.rglob("*.py"))}
    return names


def reached_modules(path: Path) -> set[str]:
    """The alteriom_hil modules a file imports: absolute, `from alteriom_hil
    import x`, and relative (`from .x import y`, `from . import x`)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                match = re.fullmatch(r"alteriom_hil\.([a-z_0-9]+)(?:\..*)?", alias.name)
                if match:
                    found.add(match.group(1))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.module:
                    found.add(node.module.split(".")[0])
                else:
                    found.update(alias.name for alias in node.names)
            elif node.module == "alteriom_hil":
                found.update(alias.name for alias in node.names)
            elif node.module:
                match = re.fullmatch(r"alteriom_hil\.([a-z_0-9]+)(?:\..*)?", node.module)
                if match:
                    found.add(match.group(1))
    return found


def guarded_modules(path: Path) -> set[str]:
    """Modules imported inside a `try:` whose handlers catch ImportError."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches = {ast.unparse(handler.type) for handler in node.handlers if handler.type is not None}
        if not any("ImportError" in caught or "ModuleNotFoundError" in caught for caught in catches):
            continue
        for inner in node.body:
            for child in ast.walk(inner):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    found |= reached_modules_of_node(child)
    return found


def reached_modules_of_node(node) -> set[str]:
    names: set[str] = set()
    if isinstance(node, ast.Import):
        names |= {alias.name.split(".")[-1] for alias in node.names}
    elif isinstance(node, ast.ImportFrom):
        names |= {alias.name for alias in node.names}
        if node.module:
            names.add(node.module.split(".")[-1])
    return names


def test_every_module_lives_on_exactly_one_side():
    both = modules_in(CORE_DIR) & modules_in(RIG_DIR)
    assert not both, f"modules on both sides of the split: {sorted(both)}; one import name, one home"


def test_core_does_not_import_the_rig():
    rig_only = modules_in(RIG_DIR) - modules_in(CORE_DIR)
    offenders = {}
    for path in sorted(CORE_DIR.rglob("*.py")):
        reached = reached_modules(path) & rig_only
        if reached:
            offenders[str(path.relative_to(ROOT))] = sorted(reached)
    assert not offenders, (
        f"core imports the rig: {offenders}. Core is what a portal holds with no hardware "
        "beside it; move the description the module wants into core, or the module out of it."
    )


def test_neither_half_imports_a_portal_except_the_launchers_guarded_question():
    offenders, unguarded = {}, {}
    for path in sorted(CORE_DIR.rglob("*.py")) + sorted(RIG_DIR.rglob("*.py")):
        trespass = reached_modules(path) & PORTAL_MODULES
        if not trespass:
            continue
        if path.name not in MAY_ASK_FOR_THE_PORTAL:
            offenders[str(path.relative_to(ROOT))] = sorted(trespass)
        elif not trespass <= guarded_modules(path):
            unguarded[str(path.relative_to(ROOT))] = sorted(trespass - guarded_modules(path))
    assert not offenders, f"imports of a portal's modules: {offenders}. A rig runs with no portal beside it."
    assert not unguarded, f"a portal's modules imported outside try/except ImportError: {unguarded}"


def test_the_rules_would_notice_a_crossing(tmp_path):
    """The scanner reads the three ways the package imports itself -- a rule
    that reads nothing passes and says nothing."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import alteriom_hil.flash\n"
        "from alteriom_hil import serial_capture\n"
        "from .inventory import x\n"
        "from . import power\n"
        "try:\n    from alteriom_hil import portal_manager\nexcept ImportError:\n    portal_manager = None\n",
        encoding="utf-8")
    assert {"flash", "serial_capture", "inventory", "power", "portal_manager"} <= reached_modules(sample)
    assert "portal_manager" in guarded_modules(sample)
