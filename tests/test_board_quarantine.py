"""Boards held out of the pool: reserved by an operator, quarantined by the canary.

A board that fails checks of its own poisons every run it lands in
(docs/farm-allocation.md, "Flakiness is an allocation problem"). The canary is
the one piece of evidence about the board and nobody's code, so it decides:
red on its own checks run after run, and the board is left out of every run
until a clean health check releases it. These pin what counts, what does not,
and what a held board is still allowed.
"""

from __future__ import annotations

import importlib.util
import queue
import threading
from pathlib import Path

import pytest
import yaml

from alteriom_hil.api_keys import required_role

SERVICE_PATH = Path(__file__).resolve().parents[1] / "runner" / "farm_service.py"
SPEC = importlib.util.spec_from_file_location("farm_service_quarantine", SERVICE_PATH)
farm_service = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(farm_service)

BOARDS = [
    {"id": "esp32-01", "target": "esp32", "port": "/dev/esp32-farm-01", "mac": "aa:bb:cc:dd:ee:01"},
    {"id": "esp32-02", "target": "esp32", "port": "/dev/esp32-farm-02", "mac": "aa:bb:cc:dd:ee:02"},
    {"id": "c3-01", "target": "esp32-c3", "port": "/dev/esp32-farm-03", "mac": "aa:bb:cc:dd:ee:03"},
]
RADIO = "test_the_radio_joins_the_rig_ap"
BROKER = "test_the_queue_reaches_the_broker"
WIRE = "test_every_wire_carries_a_level_both_ways"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.state = tmp_path
    (tmp_path / "logs").mkdir()
    manager.store = farm_service.JobStore(tmp_path / "farm.sqlite3")
    manager.pending = queue.Queue()
    manager._grants = {}
    manager._reservation_lock = threading.Lock()
    manager.registry = tmp_path / "inventory.yaml"
    manager.registry.write_text(yaml.safe_dump({"boards": [
        {"id": board["id"], "port": board["port"], "target": board["target"], "mac": board["mac"],
         "chip": board["target"].replace("-", "")}
        for board in BOARDS
    ]}))
    manager.board_map = tmp_path / "board-map.active.yaml"
    manager.board_map.write_text(yaml.safe_dump({"boards": BOARDS}))
    monkeypatch.setattr(
        farm_service.FarmManager, "inventory_snapshot",
        lambda self, annotate=False: {"boards": [dict(b) for b in BOARDS], "missing": [],
                                      "unregistered": [], "probe_errors": []},
    )
    manager.notes = []
    manager._notify = manager.notes.append
    manager.settings = {"enabled": True, "after_failures": 2}
    manager.quarantine_settings = lambda: dict(manager.settings)
    return manager


def canary(manager, job_id: str, checks: dict[str, dict[str, str]], farm_wide=()):
    """Record one canary run: per board, check -> verdict."""
    per_board = {board_id: {"checks": verdicts} for board_id, verdicts in checks.items()}
    return manager._write_board_health(job_id, per_board, list(farm_wide), farm_service.utcnow(), "a" * 40)


def job(n: int) -> str:
    return f"{n:032x}"


def test_a_board_red_on_its_own_checks_twice_running_is_quarantined_and_a_clean_check_releases_it(manager):
    passing = {RADIO: "passed", BROKER: "passed"}
    first = canary(manager, job(1), {"c3-01": {RADIO: "failed", BROKER: "passed"}, "esp32-01": passing})
    assert first["quarantine"] == {"quarantined": [], "released": []}, "one red is a board to watch"

    # A check red on every board is the farm's: it neither counts against the
    # board nor clears it.
    canary(manager, job(2), {"c3-01": {RADIO: "passed", BROKER: "failed"}, "esp32-01": {RADIO: "passed", BROKER: "failed"}},
           farm_wide=[BROKER])
    assert manager.store.holds() == {}

    third = canary(manager, job(3), {"c3-01": {RADIO: "failed", BROKER: "passed"}, "esp32-01": passing})
    assert third["quarantine"]["quarantined"] == ["c3-01"]
    hold = manager.store.holds()["c3-01"]
    assert hold["state"] == "quarantined" and hold["by"] == "canary" and hold["job_id"] == job(3)
    assert hold["reason"] == f"failed its Rig Health Check 2 runs in a row: {RADIO}"
    assert manager.notes[-1].title == "Quarantined c3-01"

    history = manager.board_history("c3-01")
    assert [row["outcome"] for row in history["verdicts"]] == ["board_failed", "inconclusive", "board_failed"]
    assert history["hold"]["state"] == "quarantined"

    fourth = canary(manager, job(4), {"c3-01": passing})
    assert fourth["quarantine"]["released"] == ["c3-01"]
    assert manager.store.holds() == {}
    assert manager.notes[-1].title == "released c3-01" and manager.notes[-1].tone == "good"


def test_a_red_wire_is_the_jumper_not_the_board(manager):
    for n in (1, 2, 3):
        canary(manager, job(n), {"c3-01": {RADIO: "passed", WIRE: "failed"}})
    assert manager.store.holds() == {}
    assert manager.store.board_verdicts("c3-01")[0]["outcome"] == "inconclusive"


def test_off_by_default_it_only_records_and_a_clean_check_still_releases(manager):
    manager.settings = {"enabled": False, "after_failures": 2}
    for n in (1, 2, 3):
        canary(manager, job(n), {"c3-01": {RADIO: "failed"}})
    assert manager.store.holds() == {} and len(manager.store.board_verdicts("c3-01")) == 3
    manager.store.set_hold("c3-01", "quarantined", "from before", "canary")
    canary(manager, job(4), {"c3-01": {RADIO: "passed"}})
    assert manager.store.holds() == {}, "a quarantine set while it was on still clears"


def test_an_operator_reserves_a_board_for_the_bench_and_releases_it(manager):
    with pytest.raises(LookupError, match="not a registered board"):
        manager.reserve_board("esp32-99", None, "sparck")
    with pytest.raises(ValueError, match="up to 200"):
        manager.reserve_board("esp32-01", "x" * 201, "sparck")
    result = manager.reserve_board("esp32-01", " reflowing the USB connector ", "sparck")
    assert result["hold"]["state"] == "reserved" and result["hold"]["reason"] == "reflowing the USB connector"
    assert result["hold"]["by"] == "sparck" and result["in_use_by"] is None
    assert manager.pending.get_nowait()[0] == "reserved", "the dispatcher looks again"

    snapshot = manager._annotate_states(manager.inventory_snapshot())
    states = {board["id"]: board["state"] for board in snapshot["boards"]}
    assert states == {"esp32-01": "reserved", "esp32-02": "available", "c3-01": "available"}
    assert snapshot["available"] == 2

    # A canary does not quarantine a board somebody has on the bench.
    canary(manager, job(1), {"esp32-01": {RADIO: "failed"}})
    canary(manager, job(2), {"esp32-01": {RADIO: "failed"}})
    assert manager.store.holds()["esp32-01"]["state"] == "reserved"

    assert manager.release_board("esp32-01", "sparck")["released"]["state"] == "reserved"
    with pytest.raises(LookupError, match="not reserved or quarantined"):
        manager.release_board("esp32-01", "sparck")
    # Changing the pool is an admin's; a user key reads it.
    assert required_role("POST", "/api/v1/inventory/esp32-01/reserve") == "admin"
    assert required_role("POST", "/api/v1/inventory/esp32-01/release") == "admin"
    assert required_role("GET", "/api/v1/inventory/esp32-01/history") == "user"


def test_a_run_may_name_a_quarantined_board_only_to_check_it_and_a_reserved_one_never(manager):
    manager.store.set_hold("c3-01", "quarantined", "radio", "canary")
    manager.store.set_hold("esp32-02", "reserved", "bench", "sparck")
    assert manager._check_named_boards(["c3-01"], "canary") == ["c3-01"]
    with pytest.raises(ValueError, match="c3-01 is quarantined: radio; only a health check may use it"):
        manager._check_named_boards(["c3-01"], "alteriom-firmware")
    with pytest.raises(ValueError, match="esp32-02 is reserved: bench; release it first"):
        manager._check_named_boards(["esp32-02"], "canary")

    submitted = []
    manager.submit = lambda kind, request, submitted_by=None: submitted.append(request) or request
    manager.current_canary = lambda: None
    manager.health_check({})
    assert submitted[-1]["boards"] == ["c3-01", "esp32-01"], "every board but the one on the bench"


def test_a_whole_bank_run_leaves_held_boards_out_of_its_map(manager, tmp_path):
    from alteriom_hil import profiles

    doc = {
        "schema": 1, "name": "mesh", "label": "Mesh",
        "source": {"location": "farm", "repo": "https://example.invalid/x.git"},
        "build": {"revision_key": "sha"},
        "supply": {"repo": "https://example.invalid/x.git", "workflow": ".github/workflows/build.yml"},
        "flash": {"command": ["{python}", "flash.py"]},
        "suite": {"path": "suites/x/tests", "min_boards": 2},
    }
    spec = profiles.parse_profile(doc, "mesh")
    log = (tmp_path / "run.log").open("w")
    assert manager._scoped_board_map(spec, job(1), log) == manager.board_map, "no holds: the active map, as ever"

    manager.store.set_hold("c3-01", "quarantined", "radio", "canary")
    scoped = manager._scoped_board_map(spec, job(2), log)
    assert scoped != manager.board_map
    assert [board["id"] for board in yaml.safe_load(scoped.read_text())["boards"]] == ["esp32-01", "esp32-02"]

    manager.store.set_hold("esp32-02", "reserved", None, "sparck")
    with pytest.raises(farm_service.PipelineError) as refused:
        manager._scoped_board_map(spec, job(3), log)
    assert "requires at least 2 board(s); 1 left once boards held out of the pool are set aside" in refused.value.detail
    log.close()
    assert "Left out c3-01: quarantined (radio)" in (tmp_path / "run.log").read_text()


def test_capacity_counts_held_boards_apart(manager):
    manager.max_runs = 1
    manager.paused = False
    manager.paused_since = manager.paused_reason = None
    manager.store.set_hold("c3-01", "quarantined", "radio", "canary")
    manager.store.set_hold("esp32-02", "reserved", None, "sparck")
    capacity = manager.capacity()
    assert capacity["families"]["esp32"] == {
        "connected": 2, "available": 1, "in_use": 0, "reserved": 1, "quarantined": 0, "available_tags": {},
    }
    assert capacity["families"]["esp32-c3"]["quarantined"] == 1 and capacity["families"]["esp32-c3"]["available"] == 0
