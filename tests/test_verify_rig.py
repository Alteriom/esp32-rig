"""The rig preflight: what it calls a fault, and what it calls a step that has
not been taken yet."""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_the_preflight_excuses_the_boards_a_new_rig_has_not_registered(tmp_path):
    """A rig nobody has registered a board on is in bring-up, not broken: its
    udev names and its board map are steps still ahead of it. Failing them
    failed the release install a new rig was in the middle of (rig02,
    2026-09-16). A host fault is still a fault."""
    script = REPO / "rig" / "verify-rig.sh"
    registry = tmp_path / "inventory.yaml"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "ALTERIOM_HIL_INVENTORY": str(registry),
        "ALTERIOM_HIL_BOARD_MAP": str(tmp_path / "board-map.yaml"),
    }
    ran = subprocess.run(["bash", str(script), "--quick"], capture_output=True, text=True, env=env)
    assert "no board is registered on this rig" in ran.stdout, ran.stdout
    assert "WARN  no board map yet" in ran.stdout, ran.stdout
    assert "no boards registered yet" in ran.stdout or "rig NOT ready" in ran.stdout

    # With a board registered, the same two are faults again.
    registry.write_text(
        "boards:\n  - {id: esp32-01, port: /dev/esp32-farm-01, target: esp32, chip: esp32}\n",
        encoding="utf-8",
    )
    ran = subprocess.run(["bash", str(script), "--quick"], capture_output=True, text=True, env=env)
    assert "FAIL  board map not found" in ran.stdout, ran.stdout
    assert ran.returncode == 1

def test_an_empty_board_map_is_not_a_map_that_cannot_be_read(tmp_path):
    """What a rig publishes before anything is registered is `boards: []`.
    Reading that as a map that does not parse failed the release install of a
    rig in the middle of bring-up (rig02, 2026-09-16) -- hourly, and only
    visible in the rig's own journal."""
    script = REPO / "rig" / "verify-rig.sh"
    registry = tmp_path / "inventory.yaml"
    board_map = tmp_path / "board-map.active.yaml"
    board_map.write_text("boards: []\n", encoding="utf-8")
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path),
        "ALTERIOM_HIL_INVENTORY": str(registry),
        "ALTERIOM_HIL_BOARD_MAP": str(board_map),
    }
    ran = subprocess.run(["bash", str(script), "--quick"], capture_output=True, text=True, env=env)
    assert "WARN  the board map" in ran.stdout and "is empty" in ran.stdout, ran.stdout

    # With boards registered, an empty map is a real inconsistency: the rig
    # knows about hardware its suites would not be given.
    registry.write_text(
        "boards:\n  - {id: esp32-01, port: /dev/esp32-farm-01, target: esp32, chip: esp32}\n",
        encoding="utf-8",
    )
    ran = subprocess.run(["bash", str(script), "--quick"], capture_output=True, text=True, env=env)
    assert "FAIL  the board map" in ran.stdout and "but boards are registered" in ran.stdout
    assert ran.returncode == 1

    # And a map that really cannot be read is still that.
    board_map.write_text("boards: [ {id: }\n", encoding="utf-8")
    ran = subprocess.run(["bash", str(script), "--quick"], capture_output=True, text=True, env=env)
    assert "does not parse" in ran.stdout, ran.stdout

def test_only_an_empty_boards_list_is_excused(tmp_path):
    """`boards: []` is the document a rig publishes before anything is
    registered. An empty file, `{}`, `boards: null` or a mapping with no
    `boards` key are a map somebody got wrong, and a rig that exits 0 on one
    of those installs a release onto a host nobody has configured."""
    script = REPO / "rig" / "verify-rig.sh"
    registry = tmp_path / "inventory.yaml"
    board_map = tmp_path / "board-map.active.yaml"
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path),
        "ALTERIOM_HIL_INVENTORY": str(registry),
        "ALTERIOM_HIL_BOARD_MAP": str(board_map),
    }
    for document in ("", "{}\n", "boards: null\n", "other: 1\n"):
        board_map.write_text(document, encoding="utf-8")
        ran = subprocess.run(["bash", str(script), "--quick"], capture_output=True, text=True, env=env)
        assert "does not parse" in ran.stdout, (document, ran.stdout)
        assert ran.returncode == 1


def test_a_registry_is_read_by_the_loader_not_by_a_regular_expression(tmp_path):
    """`boards: [{id: esp32-01, ...}]` is a registry an operator may well
    write. Read line by line it looked like no registry at all, and then a
    missing board map was excused on a rig that has hardware registered."""
    script = REPO / "rig" / "verify-rig.sh"
    registry = tmp_path / "inventory.yaml"
    registry.write_text(
        "boards: [{id: esp32-01, port: /dev/esp32-farm-01, target: esp32, chip: esp32}]\n",
        encoding="utf-8",
    )
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path),
        "ALTERIOM_HIL_INVENTORY": str(registry),
        "ALTERIOM_HIL_BOARD_MAP": str(tmp_path / "board-map.active.yaml"),
    }
    ran = subprocess.run(["bash", str(script), "--quick"], capture_output=True, text=True, env=env)
    assert "FAIL  board map not found" in ran.stdout, ran.stdout
    assert ran.returncode == 1
