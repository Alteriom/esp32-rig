"""Several runs on one rig, each on its own boards.

The allocator's rules are pinned in test_allocation.py; these are what the
service does with its decisions: a started job holds its boards' locks and
the rig lock shared, a run that has the rig to itself holds it exclusively,
a job ending wakes what waited for its boards, a cancellation reaches the job
it names and no other, and a job sharing the rig never rediscovers it.
"""

from __future__ import annotations

import fcntl
import importlib.util
import queue
import sys
import threading
import time
from pathlib import Path

import pytest
from alteriom_hil import farm_shared

from alteriom_hil import allocation, profiles

# The launcher: `alteriom_hil.launcher`, a console script now
# (`alteriom-hil-service`), which composes the halves installed onto the
# base and publishes what the service does
# (docs/public-release-plan.md, step 12e).
from alteriom_hil import launcher as farm_service

BOARDS = [
    {"id": "esp32-01", "target": "esp32", "port": "/dev/esp32-farm-01"},
    {"id": "c3-01", "target": "esp32-c3", "port": "/dev/esp32-farm-02"},
    {"id": "c6-01", "target": "esp32-c6", "port": "/dev/esp32-farm-03"},
    {"id": "esp8266-01", "target": "esp8266", "port": "/dev/esp32-farm-04"},
]


def _profile(name: str, *, concurrent: bool, needs=None, exclusive=False, resources=()) -> profiles.Profile:
    doc = {
        "schema": 1, "name": name, "label": name.title(),
        "source": {"location": "farm", "repo": "https://example.invalid/x.git"},
        "build": {"revision_key": "sha"},
        "supply": {"repo": "https://example.invalid/x.git", "workflow": ".github/workflows/build.yml"},
        "flash": {"command": ["{python}", "flash.py"]},
        "suite": {"path": "suites/x/tests", "exclusive": exclusive, "concurrent": concurrent,
                  "resources": list(resources)},
    }
    if needs:
        doc["needs"] = [{"target": target, "count": count} for target, count in needs]
    return profiles.parse_profile(doc, name)


PROFILES = {
    "c3": _profile("c3", concurrent=True, needs=[("esp32-c3", 1)]),
    "c6": _profile("c6", concurrent=True, needs=[("esp32-c6", 1)]),
    "c3-again": _profile("c3-again", concurrent=True, needs=[("esp32-c3", 1)]),
    "solo": _profile("solo", concurrent=False, needs=[("esp8266", 1)]),
    "mesh": _profile("mesh", concurrent=False, exclusive=True),
}


class Farm:
    """A manager with a real store, dispatcher and locks, and a fake pipeline
    that holds each job until the test lets it finish."""

    def __init__(self, tmp_path: Path, monkeypatch, limit: int = 2):
        manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
        manager.state = tmp_path
        manager.repo = tmp_path
        (tmp_path / "logs").mkdir(exist_ok=True)
        manager.store = farm_service.JobStore(tmp_path / "farm.sqlite3")
        manager.pending = queue.Queue()
        manager._cancel_lock = threading.Lock()
        manager._cancel_requests = {}
        manager._current_job = None
        manager._grants = {}
        manager._reservation_lock = threading.Lock()
        manager._waiting = {}
        manager._dispatch_lock = threading.Lock()
        manager._job_local = threading.local()
        manager.max_runs = limit
        manager.paused = False
        manager.paused_since = None
        manager.paused_reason = None
        manager._profiles = PROFILES
        manager._discard_workspace = lambda job_id: None
        manager._storage_changed = lambda: None
        monkeypatch.setattr(farm_shared, "RIG_LOCK_PATH", tmp_path / "rig.lock")
        monkeypatch.setattr(
            farm_service.FarmManager, "inventory_snapshot",
            lambda self, annotate=False: {"boards": [dict(b) for b in BOARDS], "missing": [],
                                          "unregistered": [], "probe_errors": []},
        )
        self.manager = manager
        self.gates: dict[str, threading.Event] = {}
        self.started: "queue.Queue[str]" = queue.Queue()
        self.behaviour = {}
        manager._execute = self._execute

    def _execute(self, job_id, kind, request, log):
        self.started.put(job_id)
        action = self.behaviour.get(job_id)
        if action:
            return action(job_id, log)
        assert self.gates[job_id].wait(20), "the test never let the job finish"
        return {"summary": "ok"}

    def submit(self, profile: str, **request) -> str:
        job = self.manager.store.create("suite", {"profile": profile, **request}, self.manager.state / "logs" / "x.log")
        self.gates[job["id"]] = threading.Event()
        return job["id"]

    def wait_started(self, count: int) -> set[str]:
        return {self.started.get(timeout=10) for _ in range(count)}

    def finish(self, job_id: str) -> None:
        tokens = self.manager.pending.qsize()
        self.gates[job_id].set()
        deadline = time.monotonic() + 10
        # Released, and the wake-up for the dispatcher sent: the last thing a
        # job does.
        while job_id in self.manager.running_job_ids() or self.manager.pending.qsize() <= tokens:
            assert time.monotonic() < deadline, "the job did not release its boards"
            time.sleep(0.02)


def _locked(path: Path, mode: int) -> bool:
    """Whether taking ``mode`` on ``path`` would block right now."""
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def test_two_concurrent_runs_start_together_each_holding_its_own_board(tmp_path, monkeypatch):
    farm = Farm(tmp_path, monkeypatch, limit=2)
    first, second = farm.submit("c3"), farm.submit("c6")
    assert set(farm.manager._dispatch()) == {first, second}
    assert farm.wait_started(2) == {first, second}

    # Each holds its board exclusively and the rig lock shared: a deploy (an
    # exclusive rig lock) waits for both, a free board is still free.
    assert _locked(farm_shared.board_lock_path("c3-01"), fcntl.LOCK_EX)
    assert _locked(farm_shared.board_lock_path("c6-01"), fcntl.LOCK_EX)
    assert not _locked(farm_shared.board_lock_path("esp8266-01"), fcntl.LOCK_EX)
    assert _locked(farm_shared.RIG_LOCK_PATH, fcntl.LOCK_EX)
    assert not _locked(farm_shared.RIG_LOCK_PATH, fcntl.LOCK_SH)

    state = farm.manager.queue_state()
    assert set(state["running_jobs"]) == {first, second} and state["concurrency"] == 2
    snapshot = farm.manager._annotate_states({"boards": [dict(b) for b in BOARDS]})
    held = {board["id"]: board.get("held_by", {}).get("job_id") for board in snapshot["boards"]}
    assert held == {"esp32-01": None, "c3-01": first, "c6-01": second, "esp8266-01": None}
    assert len(snapshot["reservations"]) == 2 and snapshot["in_use"] == 2

    farm.finish(first)
    farm.finish(second)
    assert farm.manager.store.get(first)["status"] == "passed"
    assert not _locked(farm_shared.RIG_LOCK_PATH, fcntl.LOCK_EX), "everything released"


def test_a_run_waiting_for_a_held_board_starts_when_it_is_released(tmp_path, monkeypatch):
    farm = Farm(tmp_path, monkeypatch, limit=2)
    first, second = farm.submit("c3"), farm.submit("c3-again")
    assert farm.manager._dispatch() == [first]
    farm.wait_started(1)
    waiting = farm.manager.queue_state()["waiting"][second]
    assert waiting.startswith("waiting for 1 x esp32-c3: in use by run ") and "(C3)" in waiting

    farm.finish(first)
    # The job's end put a token in the queue: the dispatcher's next turn
    # starts what waited.
    assert farm.manager.pending.get_nowait()[0] == "finished"
    assert farm.manager._dispatch() == [second]
    farm.finish(second)


def test_at_concurrency_one_a_run_has_the_rig_to_itself(tmp_path, monkeypatch):
    farm = Farm(tmp_path, monkeypatch, limit=1)
    first, second = farm.submit("c3"), farm.submit("c6")
    assert farm.manager._dispatch() == [first]
    farm.wait_started(1)
    assert _locked(farm_shared.RIG_LOCK_PATH, fcntl.LOCK_SH), "the rig lock exclusively, as before"
    assert not farm.manager._grant(first).shared
    assert "has it to itself" in farm.manager.queue_state()["waiting"][second]
    farm.finish(first)


def test_a_whole_bank_run_waits_for_shared_runs_and_nothing_starts_beside_it(tmp_path, monkeypatch):
    farm = Farm(tmp_path, monkeypatch, limit=3)
    shared = farm.submit("c3")
    assert farm.manager._dispatch() == [shared]
    farm.wait_started(1)
    mesh, late = farm.submit("mesh"), farm.submit("c6")
    assert farm.manager._dispatch() == []
    waiting = farm.manager.queue_state()["waiting"]
    assert waiting[mesh].startswith("waiting for the rig to be idle")
    assert "needs the rig to itself" in waiting[late]

    farm.finish(shared)
    assert farm.manager._dispatch() == [mesh]
    farm.wait_started(1)
    assert set(farm.manager._grant(mesh).boards) == {board["id"] for board in BOARDS}
    assert farm.manager._dispatch() == []
    farm.finish(mesh)
    assert farm.manager._dispatch() == [late]
    farm.finish(late)


def test_a_cancellation_stops_the_job_it_names_and_no_other(tmp_path, monkeypatch):
    farm = Farm(tmp_path, monkeypatch, limit=2)
    first, second = farm.submit("c3"), farm.submit("c6")
    child = "import time\ntime.sleep(30)\n"

    def long_running(job_id, log):
        farm.manager._run([sys.executable, "-c", child], log, timeout=60, grace=2)
        return {"summary": "ok"}

    farm.behaviour = {first: long_running, second: long_running}
    farm.manager._dispatch()
    farm.wait_started(2)
    time.sleep(1.5)
    farm.manager.cancel(first, "Cancelled by operator")
    deadline = time.monotonic() + 15
    while farm.manager.store.get(first)["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert farm.manager.store.get(first)["status"] == "cancelled"
    assert farm.manager.store.get(second)["status"] == "running", "the other run is untouched"
    farm.manager.cancel(second, "done")
    while farm.manager.store.get(second)["status"] == "running":
        assert time.monotonic() < deadline + 15
        time.sleep(0.1)


def test_a_job_cancelled_before_it_starts_is_never_started(tmp_path, monkeypatch):
    farm = Farm(tmp_path, monkeypatch, limit=2)
    job_id = farm.submit("c3")
    farm.manager.cancel(job_id, "changed my mind")
    assert farm.manager.pending.get_nowait()[0] == "cancelled", "a cancellation wakes the dispatcher"
    assert farm.manager.store.claim(job_id) is False
    assert farm.manager._dispatch() == []


def test_a_free_board_is_readable_while_other_boards_run(tmp_path, monkeypatch):
    farm = Farm(tmp_path, monkeypatch, limit=2)
    read = []
    farm.manager._record_chip_details = lambda board_id, port, mac: read.append(board_id) or {"id": board_id}
    shared = farm.submit("c3")
    farm.manager._dispatch()
    farm.wait_started(1)
    assert farm.manager.device_details("esp8266-01") == {"id": "esp8266-01"}
    with pytest.raises(farm_service.RigBusyError, match=r"c3-01 is in use by run .* \(C3\)"):
        farm.manager.device_details("c3-01")
    farm.finish(shared)

    solo = farm.submit("solo")
    farm.manager._dispatch()
    farm.wait_started(1)
    with pytest.raises(farm_service.RigBusyError, match="rig is busy"):
        farm.manager.device_details("c6-01")
    farm.finish(solo)
    assert read == ["esp8266-01"]


def test_a_job_sharing_the_rig_never_rediscovers_it(tmp_path, monkeypatch):
    farm = Farm(tmp_path, monkeypatch, limit=2)
    manager = farm.manager
    probed, described = [], []
    manager.refresh_inventory = lambda details=None, log=None: probed.append(details) or {"boards": BOARDS}
    manager.read_chip_details = lambda inventory, which="all", log=None: described.append(
        sorted(board["id"] for board in inventory["boards"])
    ) or 0
    log = (tmp_path / "log.txt").open("w")
    shared = allocation.Grant(job_id="a" * 32, label="C3", kind="suite", boards=("c3-01",),
                              resources=frozenset(), shared=True)
    inventory = manager._discover_for(shared, log)
    assert probed == [] and described == [["c3-01"]], "only its own board's silicon, no port opened"
    assert [board["id"] for board in inventory["boards"]] == [board["id"] for board in BOARDS]
    alone = allocation.Grant(job_id="b" * 32, label="Mesh", kind="suite", boards=(),
                             resources=frozenset(allocation.RESOURCES), shared=False, whole_rig=True)
    manager._discover_for(alone, log)
    assert probed == ["missing"], "a job with the rig to itself rediscovers, as runs always did"
    log.close()


def test_capacity_counts_free_boards_by_family(tmp_path, monkeypatch):
    farm = Farm(tmp_path, monkeypatch, limit=2)
    shared = farm.submit("c3")
    farm.manager._dispatch()
    farm.wait_started(1)
    farm.submit("c3-again")
    farm.manager._dispatch()
    capacity = farm.manager.capacity()
    assert capacity["families"]["esp32-c3"] == {
        "connected": 1, "available": 0, "in_use": 1, "reserved": 0, "quarantined": 0, "available_tags": {},
    }
    assert capacity["families"]["esp32"]["available"] == 1
    assert (capacity["running"], capacity["queued"], capacity["concurrency"]) == (1, 1, 2)
    farm.finish(shared)


def test_the_dispatcher_gives_no_run_a_board_held_out_of_the_pool(tmp_path, monkeypatch):
    farm = Farm(tmp_path, monkeypatch, limit=2)
    farm.manager.store.set_hold("c6-01", "quarantined", "radio", "canary")
    farm.manager.store.set_hold("c3-01", "reserved", "bench", "sparck")
    mesh = farm.submit("mesh")
    assert farm.manager._dispatch() == [mesh]
    farm.wait_started(1)
    assert set(farm.manager._grant(mesh).boards) == {"esp32-01", "esp8266-01"}
    farm.finish(mesh)
    shared = farm.submit("c3")
    assert farm.manager._dispatch() == [shared], "idle: it starts alone to rediscover"
    farm.wait_started(1)
    assert farm.manager._grant(shared).boards == () and not farm.manager._grant(shared).shared
    farm.finish(shared)


def test_the_host_sets_how_many_runs_may_be_in_progress():
    assert farm_service.max_runs({}) == 1
    assert farm_service.max_runs({"ALTERIOM_HIL_MAX_RUNS": "3"}) == 3
    assert farm_service.max_runs({"ALTERIOM_HIL_MAX_RUNS": "0"}) == 1
    assert farm_service.max_runs({"ALTERIOM_HIL_MAX_RUNS": "lots"}) == 1
    assert farm_service.max_runs({"ALTERIOM_HIL_MAX_RUNS": "99"}) == 16


def test_the_overview_is_given_every_active_job_not_only_the_newest():
    recent = [{"id": "new"}, {"id": "run"}]
    active = [{"id": "run"}, {"id": "old-queued"}]
    assert [job["id"] for job in farm_service._with_active(recent, active)] == ["new", "run", "old-queued"]
