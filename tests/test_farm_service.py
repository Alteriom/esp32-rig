import importlib.util
import os
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from alteriom_hil import farm_shared
import yaml

from alteriom_hil.inventory import DetectedDevice, DeviceDetails


# The launcher: `alteriom_hil.launcher`, a console script now
# (`alteriom-hil-service`), which composes the halves installed onto the
# base and publishes what the service does
# (docs/public-release-plan.md, step 12e).
from alteriom_hil import launcher as farm_service
HIL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "core" / "alteriom_hil" / "hil_config.py"
from alteriom_hil import rig_manager  # noqa: E402 -- the rig's half reads its own names
# The service itself, where those names are read: it is
# `alteriom_hil.service` now and this file is the launcher that composes
# the halves onto it, so a test that changes one changes it there.
from alteriom_hil import service as core_service  # noqa: E402
import alteriom_hil  # noqa: E402 -- the package a faked hil_config is read from


def _install_profiles(repo: Path) -> Path:
    """Give a fake repo the profile documents this repository ships.

    Copied rather than hand-written so the tests exercise the real painlessmesh
    profile: a stub here would let the shipped document drift from what the
    service is tested against, which is the one thing profiles-as-data must not
    allow.
    """
    import shutil

    source = Path(__file__).resolve().parents[1] / "profiles"
    destination = Path(repo) / "profiles"
    destination.mkdir(parents=True, exist_ok=True)
    for document in source.glob("*.yaml"):
        shutil.copy2(document, destination / document.name)
    return destination



def test_job_store_persists_structured_history(tmp_path):
    store = farm_service.JobStore(tmp_path / "farm.db")
    created = store.create("build", {"ref": "abc123"}, tmp_path / "build.log")
    store.update(created["id"], "running")
    store.update(created["id"], "passed", {"painlessmesh_sha": "abc123"})
    loaded = store.get(created["id"])
    assert loaded["status"] == "passed"
    assert loaded["request"] == {"ref": "abc123"}
    assert loaded["result"]["painlessmesh_sha"] == "abc123"


def test_job_store_recovers_pipeline_interrupted_by_restart(tmp_path):
    store = farm_service.JobStore(tmp_path / "farm.db")
    created = store.create(
        "suite",
        {"ref": "abc123"},
        tmp_path / "suite.log",
        [{"name": "test", "label": "Run validation", "status": "running"}],
    )
    store.update(created["id"], "running")
    assert store.recover_incomplete() == []
    recovered = store.get(created["id"])
    assert recovered["status"] == "failed"
    assert recovered["result"]["summary"] == "Pipeline interrupted by a farm service restart"
    assert recovered["progress"][0]["status"] == "failed"


def test_a_queued_job_survives_a_service_restart_in_its_place(tmp_path):
    # A deploy on merge restarts the service; a job that had not started
    # lost nothing and used to be failed anyway — that was a sweep's third
    # run. It is handed back to be queued again, oldest first.
    store = farm_service.JobStore(tmp_path / "farm.db")
    first = store.create("suite", {"ref": "abc123"}, tmp_path / "a.log")
    second = store.create("build", {"ref": "abc123"}, tmp_path / "b.log")
    running = store.create("suite", {"ref": "abc123"}, tmp_path / "c.log")
    store.update(running["id"], "running")
    carried = store.recover_incomplete()
    assert [job["id"] for job in carried] == [first["id"], second["id"]]
    assert [job["kind"] for job in carried] == ["suite", "build"]
    assert store.get(first["id"])["status"] == "queued"
    assert store.get(second["id"])["status"] == "queued"
    assert store.get(running["id"])["status"] == "failed"


def test_job_store_normalizes_legacy_command_errors(tmp_path):
    store = farm_service.JobStore(tmp_path / "farm.db")
    created = store.create("suite", {"ref": "abc123"}, tmp_path / "suite.log")
    store.update(created["id"], "failed", {"error": "Command [...] returned non-zero"})
    store.normalize_legacy_failures()
    result = store.get(created["id"])["result"]
    assert result["summary"] == "Validation pipeline failed"
    assert "structured stage tracking" in result["detail"]
    assert result["technical_error"].startswith("Command")


def test_job_detail_infers_legacy_pipeline_and_loads_report(tmp_path):
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.state = tmp_path
    manager.store = farm_service.JobStore(tmp_path / "farm.db")
    job = manager.store.create("suite", {"ref": "abc123"}, tmp_path / "unused.log")
    job_id = job["id"]
    (tmp_path / "artifacts" / job_id).mkdir(parents=True)
    (tmp_path / "artifacts" / job_id / "manifest.json").write_text("{}")
    run = tmp_path / "runs" / job_id
    (run / "metrics").mkdir(parents=True)
    (run / "results.xml").write_text("<testsuites/>")
    (run / "metrics" / "report.json").write_text('{"validation_gate":"incomplete"}')
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / f"{job_id}.log").write_text("Flashing 4 boards\n$ python -m pytest tests\n")
    manager.store.update(job_id, "failed", {"summary": "Validation failed"})
    detail = manager.job_detail(job_id)
    assert detail["report"]["validation_gate"] == "incomplete"
    assert [stage["status"] for stage in detail["progress"]] == [
        "passed", "passed", "passed", "passed", "failed", "passed"
    ]


def _validating_manager():
    """A manager for the validation checks alone.

    `_validate` also asks whether firmware exists for the commit, because no
    profile builds on the rig any more; that check needs a repository on disk
    and says nothing about the ref, target and evidence rules under test here.
    """
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager._check_images_exist = lambda *args, **kwargs: None
    return manager


def test_job_validation_rejects_shell_syntax_and_unknown_targets():
    manager = _validating_manager()
    with pytest.raises(ValueError, match="metacharacters"):
        manager._validate("suite", {"ref": "main; reboot", "targets": ["esp32"]})
    with pytest.raises(ValueError, match="unsupported job kind"):
        manager._validate("build", {"ref": "main"})
    with pytest.raises(ValueError, match="subset"):
        manager._validate("suite", {"ref": "main", "targets": ["esp32-h2"]})
    with pytest.raises(ValueError, match="unsupported validation profile"):
        manager._validate("suite", {"profile": "unknown", "ref": "main", "targets": ["esp32"]})
    manager._validate("suite", {"ref": "pull/383/head", "targets": ["esp32-s3"]})


def simulation_evidence():
    return {
        "schema": 1,
        "generated_at": "2026-09-02T12:00:00+00:00",
        "painlessmesh_sha": "a" * 40,
        "protocol_sim": {
            "status": "passed",
            "tests": 17,
            "capabilities": ["mesh_formation", "broadcast"],
            "summary": "17 HAL protocol scenarios passed",
        },
        "mesh_sim": {
            "status": "passed",
            "simulator_sha": "b" * 40,
            "scenarios": ["message-accounting", "partition-and-heal"],
            "summary": "Behavioural gate passed",
        },
    }


def test_suite_accepts_bounded_passed_simulator_evidence():
    manager = _validating_manager()
    evidence = simulation_evidence()
    manager._validate(
        "suite", {"ref": "a" * 40, "targets": ["esp32"], "simulation": evidence}
    )
    progress = manager._initial_progress("suite", {"simulation": evidence})
    assert [(stage["name"], stage["status"]) for stage in progress[:2]] == [
        ("protocol_sim", "passed"),
        ("mesh_sim", "passed"),
    ]
    assert progress[2]["group"] == "hardware"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda item: item.update(schema=2), "schema"),
        (lambda item: item["mesh_sim"].update(status="failed"), "must have passed"),
        (lambda item: item["mesh_sim"].update(scenarios=[]), "non-empty bounded"),
        (lambda item: item.update(extra="untrusted"), "unknown fields"),
    ],
)
def test_suite_rejects_invalid_simulator_evidence(mutation, message):
    manager = _validating_manager()
    evidence = simulation_evidence()
    mutation(evidence)
    with pytest.raises(ValueError, match=message):
        manager._validate("suite", {"ref": "main", "simulation": evidence})


def test_a_run_without_simulator_evidence_has_no_simulator_stages():
    """Simulation is a parameter of the job. A run that did not supply the
    evidence -- another consumer, or an operator from the dashboard -- has no
    simulation stages at all, rather than two marked skipped that read as gaps
    in every run of a product with no simulator."""
    stages = farm_service.FarmManager._initial_progress("suite")
    assert stages[0]["name"] == "build"
    assert not {stage["name"] for stage in stages} & {"protocol_sim", "mesh_sim"}
    assert [stage["name"] for stage in stages] == ["build", "discover", "flash", "preflight", "test", "report"]


def test_inventory_job_accepts_no_caller_controlled_arguments():
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager._validate("inventory", {})
    with pytest.raises(ValueError, match="unknown request fields"):
        manager._validate("inventory", {"port": "/dev/ttyUSB0"})


def test_register_device_derives_hardware_fields_from_live_discovery(tmp_path, monkeypatch):
    registry = tmp_path / "inventory.yaml"
    registry.write_text(
        "boards:\n"
        "- id: classic\n"
        "  port: /dev/ttyUSB0\n"
        "  target: esp32\n"
        "  chip: esp32\n"
        "  mac: aa:bb:cc:dd:ee:01\n",
        encoding="utf-8",
    )
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.registry = registry
    manager.board_map = tmp_path / "active.yaml"
    manager.state = tmp_path
    monkeypatch.setattr(farm_shared, "RIG_LOCK_PATH", tmp_path / "rig.lock")
    monkeypatch.setattr(
        rig_manager, "discover",
        lambda: (
            [
                DetectedDevice(
                    port="/dev/ttyACM4",
                    chip="esp32c3",
                    target="esp32-c3",
                    mac="aa:bb:cc:dd:ee:02",
                    usb_path="hub-4",
                )
            ],
            [],
        ),
    )
    result = manager.register_device("esp32-c3-02", "AA-BB-CC-DD-EE-02")
    stored = yaml.safe_load(registry.read_text(encoding="utf-8"))["boards"]
    assert result["registered"] == "esp32-c3-02"
    assert stored[-1]["target"] == "esp32-c3"
    assert stored[-1]["port"] == "/dev/ttyACM4"
    assert stored[-1]["mac"] == "aa:bb:cc:dd:ee:02"


def test_register_device_rejects_unknown_or_duplicate_identity(tmp_path, monkeypatch):
    registry = tmp_path / "inventory.yaml"
    registry.write_text(
        "boards:\n"
        "- id: classic\n"
        "  port: /dev/ttyUSB0\n"
        "  target: esp32\n"
        "  chip: esp32\n"
        "  mac: aa:bb:cc:dd:ee:01\n",
        encoding="utf-8",
    )
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.registry = registry
    manager.board_map = tmp_path / "active.yaml"
    manager.state = tmp_path
    monkeypatch.setattr(farm_shared, "RIG_LOCK_PATH", tmp_path / "rig.lock")
    monkeypatch.setattr(rig_manager, "discover", lambda: ([], []))
    with pytest.raises(ValueError, match="already registered"):
        manager.register_device("classic", "aa:bb:cc:dd:ee:02")
    with pytest.raises(ValueError, match="not present"):
        manager.register_device("new-board", "aa:bb:cc:dd:ee:09")


def test_unregister_device_removes_retired_identity_and_reconciles(tmp_path, monkeypatch):
    registry = tmp_path / "inventory.yaml"
    registry.write_text(
        "boards:\n"
        "- id: retired-c3\n"
        "  port: /dev/ttyACM0\n"
        "  target: esp32-c3\n"
        "  chip: esp32c3\n"
        "  mac: aa:bb:cc:dd:ee:01\n"
        "- id: active-s3\n"
        "  port: /dev/ttyACM1\n"
        "  target: esp32-s3\n"
        "  chip: esp32s3\n"
        "  mac: aa:bb:cc:dd:ee:02\n",
        encoding="utf-8",
    )
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.registry = registry
    manager.board_map = tmp_path / "active.yaml"
    manager.state = tmp_path
    monkeypatch.setattr(farm_shared, "RIG_LOCK_PATH", tmp_path / "rig.lock")
    monkeypatch.setattr(
        rig_manager, "discover",
        lambda: (
            [
                DetectedDevice(
                    port="/dev/ttyACM8",
                    chip="esp32s3",
                    target="esp32-s3",
                    mac="aa:bb:cc:dd:ee:02",
                )
            ],
            [],
        ),
    )
    result = manager.unregister_device("retired-c3")
    stored = yaml.safe_load(registry.read_text(encoding="utf-8"))["boards"]
    assert result["unregistered"] == "retired-c3"
    assert result["inventory"]["missing"] == []
    assert [board["id"] for board in stored] == ["active-s3"]


def _details_manager(tmp_path, monkeypatch, boards):
    """A manager whose inventory snapshot is fixed, with the rig lock in tmp."""
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.python = Path("/usr/bin/python3")
    manager.state = tmp_path
    monkeypatch.setattr(farm_shared, "RIG_LOCK_PATH", tmp_path / "rig.lock")
    monkeypatch.setattr(
        farm_service.FarmManager,
        "inventory_snapshot",
        lambda self: {"boards": boards, "missing": [], "unregistered": [], "probe_errors": []},
    )
    return manager


def test_device_details_reads_the_silicon_and_confirms_the_registry(tmp_path, monkeypatch):
    manager = _details_manager(
        tmp_path, monkeypatch,
        [{"id": "c6-01", "port": "/dev/ttyACM2", "target": "esp32-c6", "mac": "40:4c:ca:41:0f:7c"}],
    )
    monkeypatch.setattr(
        rig_manager, "probe_details",
        lambda port, python: DeviceDetails(
            port=port, chip="esp32c6", target="esp32-c6", mac="40:4C:CA:41:0F:7C",
            description="ESP32-C6 (QFN40)", revision="v0.1", flash_size="4MB",
        ),
    )
    record = manager.device_details("c6-01")
    assert record["id"] == "c6-01"
    assert (record["description"], record["revision"], record["flash_size"]) == ("ESP32-C6 (QFN40)", "v0.1", "4MB")
    assert record["matches_registry"], "the same MAC in a different case is the same board"


def test_device_details_reports_a_board_swapped_behind_a_registered_id(tmp_path, monkeypatch):
    manager = _details_manager(
        tmp_path, monkeypatch,
        [{"id": "c6-01", "port": "/dev/ttyACM2", "target": "esp32-c6", "mac": "40:4c:ca:41:0f:7c"}],
    )
    monkeypatch.setattr(
        rig_manager, "probe_details",
        lambda port, python: DeviceDetails(
            port=port, chip="esp32c6", target="esp32-c6", mac="40:4c:ca:99:99:99"
        ),
    )
    record = manager.device_details("c6-01")
    assert record["matches_registry"] is False
    assert record["registered_mac"] == "40:4c:ca:41:0f:7c"


def test_device_details_refuses_unknown_ids_and_boards_that_are_not_connected(tmp_path, monkeypatch):
    manager = _details_manager(tmp_path, monkeypatch, [])
    with pytest.raises(ValueError, match="invalid board id"):
        manager.device_details("Not A Board")
    with pytest.raises(LookupError, match="not a connected board"):
        manager.device_details("c6-01")


def test_device_details_never_waits_behind_a_running_job(tmp_path, monkeypatch):
    import fcntl

    manager = _details_manager(
        tmp_path, monkeypatch,
        [{"id": "c6-01", "port": "/dev/ttyACM2", "target": "esp32-c6", "mac": "40:4c:ca:41:0f:7c"}],
    )
    monkeypatch.setattr(
        rig_manager, "probe_details",
        lambda port, python: pytest.fail("must not touch the serial port while the rig is locked"),
    )
    # Stand in for the worker thread holding the rig for the length of a run.
    with (tmp_path / "rig.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(farm_service.RigBusyError, match="busy with a job"):
            manager.device_details("c6-01")


def test_service_version_reports_the_installed_version_number(tmp_path, monkeypatch):
    stamp = tmp_path / "version.json"
    stamp.write_text(
        '{"version": "1.0.122", "base": "1.0", "build": 122, "short": "abc123",'
        ' "commit": "abc123def", "installed_at": "2026-09-04T18:00:00Z"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(core_service, "VERSION_FILE", stamp)
    version = farm_service.service_version()
    assert version["version"] == "1.0.122", "the number is what an operator compares"
    assert version["build"] == 122
    assert version["short"] == "abc123", "the commit stays as provenance"


def test_service_version_says_unknown_rather_than_guessing(tmp_path, monkeypatch):
    # A host provisioned before versions were stamped, or by hand: the
    # dashboard must say so instead of showing a stale or invented version.
    unknown = {"version": "unknown", "short": "unknown", "commit": None}
    monkeypatch.setattr(core_service, "VERSION_FILE", tmp_path / "absent.json")
    assert farm_service.service_version() == unknown
    broken = tmp_path / "broken.json"
    broken.write_text("not json at all", encoding="utf-8")
    monkeypatch.setattr(core_service, "VERSION_FILE", broken)
    assert farm_service.service_version() == unknown
    listy = tmp_path / "listy.json"
    listy.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(core_service, "VERSION_FILE", listy)
    assert farm_service.service_version() == unknown, "a JSON list is not a version"
    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"short": "abc123", "commit": "abc123def"}', encoding="utf-8")
    monkeypatch.setattr(core_service, "VERSION_FILE", legacy)
    assert farm_service.service_version() == unknown, "a stamp predating version numbers is not a version"


def test_job_page_filters_and_pages_the_history(tmp_path):
    store = farm_service.JobStore(tmp_path / "farm.db")
    for i in range(7):
        job = store.create("suite", {"ref": f"branch-{i}"}, tmp_path / "x.log")
        store.update(job["id"], "passed" if i % 2 else "failed", {"summary": f"run {i}"})
    store.create("build", {"ref": "special-ref"}, tmp_path / "x.log")

    first = store.page(limit=3, offset=0)
    assert len(first["jobs"]) == 3 and first["total"] == 8
    second = store.page(limit=3, offset=3)
    assert len(second["jobs"]) == 3
    assert {j["id"] for j in first["jobs"]}.isdisjoint({j["id"] for j in second["jobs"]}), "pages must not overlap"

    assert store.page(kind="build")["total"] == 1
    assert store.page(status="failed")["total"] == 4
    assert store.page(search="special-ref")["total"] == 1, "search reaches into the request"
    assert store.page(search="run 3")["total"] == 1, "search reaches into the result summary"
    assert store.page(search="nothing here")["total"] == 0
    assert store.counts_by_status()["passed"] == 3


def test_job_page_rejects_junk_filters_rather_than_trusting_them(tmp_path):
    store = farm_service.JobStore(tmp_path / "farm.db")
    job = store.create("suite", {"ref": "main"}, tmp_path / "x.log")
    store.update(job["id"], "passed", {"summary": "ok"})
    # An unknown status or kind must not silently filter everything out, and a
    # quoted search term must not reach SQL as syntax.
    assert store.page(status="'; DROP TABLE jobs; --")["total"] == 1
    assert store.page(kind="nonsense")["total"] == 1
    assert store.page(search="' OR 1=1 --")["total"] == 0
    assert store.page(limit=99999)["limit"] == 200, "limit is capped"
    assert store.page(offset=-5)["offset"] == 0


def test_configuration_names_the_token_file_but_never_the_token(tmp_path, monkeypatch):
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.repo, manager.state = tmp_path, tmp_path
    _install_profiles(manager.repo)
    manager.registry = tmp_path / "inventory.yaml"
    manager.board_map = tmp_path / "board-map.active.yaml"
    manager.python = Path("/usr/bin/python3")
    config = manager.configuration()
    # An operator needs to know which file to rotate; nobody needs the secret
    # rendered into a page that is open on a screen all day.
    assert "token_file" in config["service"]
    assert config["build"]["targets"], "the dashboard builds its family list from this"
    assert set(config) >= {"version", "host", "service", "paths", "health", "build"}


def test_job_timings_separate_waiting_from_running(tmp_path):
    store = farm_service.JobStore(tmp_path / "farm.db")
    job = store.create("suite", {"ref": "main"}, tmp_path / "x.log")
    store.update(job["id"], "running")
    store.update(job["id"], "passed", {"summary": "ok"})
    done = store.get(job["id"])
    assert done["queued_seconds"] is not None, "how long it waited for the rig"
    assert done["duration_seconds"] is not None, "how long it actually ran"
    # A running job has no duration yet: the dashboard counts up from
    # started_at instead of being handed a number that is already stale.
    running = store.create("suite", {"ref": "main"}, tmp_path / "y.log")
    store.update(running["id"], "running")
    assert store.get(running["id"])["duration_seconds"] is None


def test_reserved_boards_are_marked_in_use_and_counted(tmp_path, monkeypatch):
    from alteriom_hil import allocation

    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager._grants = {}
    manager._reservation_lock = __import__("threading").Lock()
    snapshot = {
        "boards": [{"id": "esp32-01", "target": "esp32"}, {"id": "esp32-c6-01", "target": "esp32-c6"}],
        "missing": [], "unregistered": [], "probe_errors": [],
    }
    # A whole-bank suite holds every connected board.
    demand = allocation.Demand(job_id="job123", label="painlessMesh", whole_rig=True)
    manager._grants["job123"] = allocation.plan([demand], snapshot["boards"], []).start[0]
    annotated = manager._annotate_states(dict(snapshot, boards=[dict(b) for b in snapshot["boards"]]))
    assert [b["state"] for b in annotated["boards"]] == ["in_use", "in_use"]
    assert annotated["in_use"] == 2 and annotated["available"] == 0
    assert annotated["boards"][0]["held_by"]["job_id"] == "job123"
    assert annotated["boards"][0]["held_by"]["label"] == "painlessMesh"

    manager._release("job123")
    free = manager._annotate_states(dict(snapshot, boards=[dict(b) for b in snapshot["boards"]]))
    assert [b["state"] for b in free["boards"]] == ["available", "available"]
    assert free["available"] == 2 and free["reservation"] is None and free["reservations"] == []


def test_a_build_holds_the_rig_without_holding_boards(tmp_path, monkeypatch):
    # A build (or a discovery) occupies the rig but holds no board, so its
    # boards are not reported in use by it.
    from alteriom_hil import allocation

    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    for kind in ("build", "inventory"):
        demand = manager._demand({"id": "b" * 32, "kind": kind, "request": {}})
        grant = allocation.plan([demand], [{"id": "esp32-01", "target": "esp32"}], []).start[0]
        assert grant.boards == () and grant.shared is False


# Whose remote is asked is the profile's to say; these name one, as submit does.
A_REMOTE = "https://example.invalid/a/project.git"


def test_a_ref_the_remote_does_not_have_is_refused_at_submit(monkeypatch):
    # A branch deleted when its PR merged used to be accepted, occupy the rig,
    # and fail in the build stage with a git traceback that read like a farm
    # fault. It is a typo, and it should say so immediately.
    class Answered:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(farm_service.subprocess, "run", lambda *a, **k: Answered())
    with pytest.raises(ValueError, match="no branch or tag 'gone/branch'"):
        farm_service._reject_missing_ref("gone/branch", A_REMOTE, "a project")


def test_ref_checking_never_blocks_work_it_cannot_verify(monkeypatch):
    # A commit SHA is not listable, and an unreachable remote must not stop
    # the farm accepting work: only a definite "the remote does not have it"
    # refuses.
    sha = "a" * 40
    farm_service._reject_missing_ref(sha, A_REMOTE, "a project")  # no remote call at all

    def unreachable(*args, **kwargs):
        raise OSError("network is down")

    monkeypatch.setattr(farm_service.subprocess, "run", unreachable)
    farm_service._reject_missing_ref("main", A_REMOTE, "a project")

    class Found:
        returncode, stdout, stderr = 0, "abc123\trefs/heads/main\n", ""

    monkeypatch.setattr(farm_service.subprocess, "run", lambda *a, **k: Found())
    farm_service._reject_missing_ref("main", A_REMOTE, "a project")


def test_ref_checking_accepts_the_pull_request_refs_ci_submits(monkeypatch):
    # painlessMesh CI sends `pull/383/head`. `git ls-remote --heads --tags`
    # hides refs/pull/*, so filtering by those would refuse a ref the farm
    # documents as supported — the existing validation test caught exactly
    # that, and this pins the behaviour.
    seen = {}

    class Found:
        returncode, stdout, stderr = 0, "abc123\trefs/pull/383/head\n", ""

    def record(args, **kwargs):
        seen["args"] = args
        return Found()

    monkeypatch.setattr(farm_service.subprocess, "run", record)
    farm_service._reject_missing_ref("pull/383/head", A_REMOTE, "a project")
    assert "--heads" not in seen["args"] and "--tags" not in seen["args"]


def test_a_suite_that_overruns_is_interrupted_before_it_is_killed(tmp_path):
    # The second sweep on the channel fix overran the safety limit, and the
    # kill took the soak test, the junit summary, and every board's serial
    # log with it — the run that most needed evidence left none. pytest turns
    # SIGINT into a KeyboardInterrupt and still tears its fixtures down, so
    # the service interrupts first and kills only after a bounded grace.
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.repo = tmp_path
    child = (
        "import signal, sys, time\n"
        "def teardown(*_):\n"
        "    print('teardown ran', flush=True)\n"
        "    sys.exit(2)\n"
        "signal.signal(signal.SIGINT, teardown)\n"
        "print('working', flush=True)\n"
        "time.sleep(30)\n"
    )
    log_path = tmp_path / "job.log"
    with log_path.open("w") as log, pytest.raises(farm_service.subprocess.TimeoutExpired):
        manager._run([sys.executable, "-c", child], log, timeout=1, grace=10)
    log_text = log_path.read_text()
    assert "teardown ran" in log_text
    assert "safety limit reached" in log_text
    assert "killing" not in log_text


def test_a_wedged_suite_is_still_killed_after_the_grace(tmp_path):
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.repo = tmp_path
    child = (
        "import signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "print('wedged', flush=True)\n"
        "time.sleep(60)\n"
    )
    log_path = tmp_path / "job.log"
    with log_path.open("w") as log, pytest.raises(farm_service.subprocess.TimeoutExpired):
        manager._run([sys.executable, "-c", child], log, timeout=1, grace=1)
    assert "killing" in log_path.read_text()


FAKE_PHONE, FAKE_APIKEY = "+15557654321", "987654"
FAKE_LINK = f"https://api.callmebot.com/whatsapp.php?phone={FAKE_PHONE}&apikey={FAKE_APIKEY}"

def _idle_manager(tmp_path, monkeypatch, remote_sha):
    """A manager whose worker never picks anything up, so queued jobs stay
    queued and the queue can be inspected; the remote answers every ref with
    ``remote_sha``.

    Where its firmware comes from is not asked. The farm does not build, so
    a submit with neither a supplied bundle nor a held one is
    refused before it reaches the queue -- correct, and beside the point of
    every test here, which is what the queue does with a job once it has one.
    The check is stubbed rather than satisfied so `reusable_artifacts` stays
    the real one for the tests that are about it.
    """
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager._check_images_exist = lambda *args, **kwargs: None
    manager.state = tmp_path
    (tmp_path / "logs").mkdir(exist_ok=True)
    manager.store = farm_service.JobStore(tmp_path / "farm.sqlite3")
    manager.pending = __import__("queue").Queue()
    manager._cancel_lock = __import__("threading").Lock()
    manager._cancel_requests = {}
    manager._current_job = None

    class Found:
        returncode, stderr = 0, ""
        stdout = f"{remote_sha}\trefs/heads/topic\n"

    monkeypatch.setattr(farm_service.subprocess, "run", lambda *a, **k: Found())
    return manager


def test_a_newer_commit_on_the_same_ref_supersedes_the_queued_run(tmp_path, monkeypatch):
    # Three items sat in the queue for one branch after two pushes: the runs
    # for the older commits were rig time spent on commits nobody was waiting
    # for. The new submission cancels them.
    manager = _idle_manager(tmp_path, monkeypatch, "a" * 40)
    old = manager.submit("suite", {"ref": "topic", "targets": ["esp32"]})
    assert old["request"]["resolved_sha"] == "a" * 40, "pinned at submission"

    manager = _idle_manager(tmp_path, monkeypatch, "b" * 40)
    new = manager.submit("suite", {"ref": "topic", "targets": ["esp32"]})
    superseded = manager.store.get(old["id"])
    assert superseded["status"] == "cancelled"
    assert superseded["result"]["superseded_by"] == new["id"]
    assert "moved on to bbbbbbbbbbbb" in superseded["result"]["summary"]
    assert all(stage["status"] == "skipped" for stage in superseded["progress"]
               if stage["name"] not in ("protocol_sim", "mesh_sim"))
    assert manager.store.get(new["id"])["status"] == "queued"


def test_a_stability_sweep_is_three_runs_at_one_commit_and_all_of_them_stay(tmp_path, monkeypatch):
    # Keyed on the commit, not the name: the same branch at the same commit
    # submitted three times is three runs wanted.
    manager = _idle_manager(tmp_path, monkeypatch, "c" * 40)
    ids = [manager.submit("suite", {"ref": "topic"})["id"] for _ in range(3)]
    assert [manager.store.get(i)["status"] for i in ids] == ["queued"] * 3


def test_supersede_can_be_declined_and_never_crosses_refs_or_kinds(tmp_path, monkeypatch):
    manager = _idle_manager(tmp_path, monkeypatch, "d" * 40)
    other_ref = manager.submit("suite", {"ref": "elsewhere"})
    # A farm build still queued from before the farm stopped building: the
    # history keeps them, and a suite for the same ref is not one of them.
    build = manager.store.create("build", {"ref": "topic", "resolved_sha": "d" * 40}, tmp_path / "b.log")
    kept = manager.submit("suite", {"ref": "topic"})
    manager = _idle_manager(tmp_path, monkeypatch, "e" * 40)
    manager.submit("suite", {"ref": "topic", "supersede": False})
    assert manager.store.get(kept["id"])["status"] == "queued", "declined"
    manager.submit("suite", {"ref": "topic"})
    assert manager.store.get(kept["id"])["status"] == "cancelled"
    assert manager.store.get(build["id"])["status"] == "queued", "a build is not a suite"
    assert manager.store.get(other_ref["id"])["status"] == "queued", "another ref"
    with pytest.raises(ValueError, match="supersede must be"):
        manager.submit("suite", {"ref": "topic", "supersede": "yes"})


def test_an_unresolvable_ref_is_never_superseded(tmp_path, monkeypatch):
    # Without a commit to compare there is no "newer"; the job runs as before
    # and the build stage resolves the branch.
    manager = _idle_manager(tmp_path, monkeypatch, "f" * 40)
    monkeypatch.setattr(farm_service.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    first = manager.submit("suite", {"ref": "topic"})
    assert "resolved_sha" not in first["request"]
    second = manager.submit("suite", {"ref": "topic"})
    assert manager.store.get(first["id"])["status"] == "queued"
    assert manager.store.get(second["id"])["status"] == "queued"


def test_a_running_job_is_interrupted_on_cancel_and_keeps_its_evidence(tmp_path):
    # Cancelling a running job goes through the same interrupt as the safety
    # limit: pytest tears its fixtures down and the serial logs are written.
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.repo = tmp_path
    manager.store = farm_service.JobStore(tmp_path / "farm.sqlite3")
    manager._cancel_lock = __import__("threading").Lock()
    manager._cancel_requests = {}
    job = manager.store.create("suite", {"ref": "topic"}, tmp_path / "job.log")
    manager.store.update(job["id"], "running")
    manager._current_job = job["id"]
    child = (
        "import signal, sys, time\n"
        "def teardown(*_):\n"
        "    print('teardown ran', flush=True)\n"
        "    sys.exit(2)\n"
        "signal.signal(signal.SIGINT, teardown)\n"
        "print('working', flush=True)\n"
        "time.sleep(30)\n"
    )
    __import__("threading").Timer(1.5, lambda: manager.cancel(job["id"], "Cancelled by operator")).start()
    log_path = tmp_path / "job.log"
    with log_path.open("w") as log, pytest.raises(farm_service.JobCancelled, match="Cancelled by operator"):
        manager._run([sys.executable, "-c", child], log, timeout=60, grace=10)
    text = log_path.read_text()
    assert "teardown ran" in text and "cancelled: Cancelled by operator" in text


def test_cancel_refuses_finished_jobs_and_unknown_ids(tmp_path):
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.store = farm_service.JobStore(tmp_path / "farm.sqlite3")
    manager._cancel_lock = __import__("threading").Lock()
    manager._cancel_requests = {}
    job = manager.store.create("suite", {"ref": "topic"}, tmp_path / "job.log")
    manager.store.update(job["id"], "running")
    manager.store.update(job["id"], "passed", {"summary": "ok"})
    with pytest.raises(ValueError, match="is passed"):
        manager.cancel(job["id"], "too late")
    with pytest.raises(KeyError):
        manager.cancel("0" * 32, "nothing there")
    assert manager.store.page(status="cancelled")["total"] == 0


def test_a_promoted_job_runs_next_and_the_latest_promotion_wins(tmp_path):
    store = farm_service.JobStore(tmp_path / "farm.db")
    first = store.create("suite", {"ref": "a"}, tmp_path / "a.log")
    second = store.create("suite", {"ref": "b"}, tmp_path / "b.log")
    third = store.create("suite", {"ref": "c"}, tmp_path / "c.log")
    assert store.next_queued()["id"] == first["id"]
    store.promote(third["id"])
    assert store.next_queued()["id"] == third["id"]
    store.promote(second["id"])
    assert [job["id"] for job in store.active()] == [second["id"], third["id"], first["id"]]
    store.update(second["id"], "running")
    # The running job leads the order the dashboard shows; the rest keep theirs.
    assert [job["id"] for job in store.active()] == [second["id"], third["id"], first["id"]]
    with pytest.raises(ValueError, match="only a queued job"):
        store.promote(second["id"])
    with pytest.raises(KeyError):
        store.promote("0" * 32)


def test_pausing_the_queue_holds_the_next_job_and_resuming_wakes_the_worker(tmp_path, monkeypatch):
    manager = _idle_manager(tmp_path, monkeypatch, "b" * 40)
    manager.paused = False
    manager.paused_since = None
    manager.paused_reason = None
    manager.submit("suite", {"ref": "topic", "targets": ["esp32"]})
    assert manager.queue_state()["paused"] is False
    assert len(manager.queue_state()["queued"]) == 1
    state = manager.pause()
    assert state["paused"] is True and state["paused_since"]
    # An operator's pause has no reason to give; the farm's own does, and
    # the dashboard shows it so nobody resumes a pause without knowing what
    # it was protecting.
    assert state["paused_reason"] is None
    manager.resume()
    assert manager.pause("the canary failed on every board")["paused_reason"] == (
        "the canary failed on every board"
    )
    assert manager.resume()["paused_reason"] is None
    manager.pause()
    tokens_before = manager.pending.qsize()
    manager.resume()
    assert manager.queue_state()["paused"] is False
    assert manager.pending.qsize() == tokens_before + 1, "resume wakes the worker"


def test_rediscovery_is_refused_while_a_run_is_queued_or_running(tmp_path, monkeypatch):
    manager = _idle_manager(tmp_path, monkeypatch, "c" * 40)
    manager.paused = False
    manager.paused_since = None
    assert manager.rig_busy() is False
    queued = manager.submit("suite", {"ref": "topic", "targets": ["esp32"]})
    assert manager.rig_busy() is True
    with pytest.raises(farm_service.RigBusyError, match="idle"):
        manager.submit("inventory", {})
    manager.cancel(queued["id"], "Cancelled by operator")
    assert manager.rig_busy() is False
    assert manager.submit("inventory", {})["kind"] == "inventory"


def _details_record(port, mac, chip="esp32c6", target="esp32-c6"):
    return DeviceDetails(
        port=port, chip=chip, target=target, mac=mac,
        description="ESP32-C6 (QFN40)", revision="v0.1", flash_size="4MB",
        features=["WiFi 6", "BT 5"], probed_at="2026-09-07T00:00:00+00:00",
    )


def test_chip_details_are_read_at_discovery_kept_on_file_and_merged_into_the_snapshot(tmp_path, monkeypatch):
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.state = tmp_path
    manager.python = Path("/usr/bin/python3")
    manager.registry = tmp_path / "inventory.yaml"
    manager.board_map = tmp_path / "board-map.yaml"
    manager._grants = {}
    manager._reservation_lock = __import__("threading").Lock()
    boards = [
        {"id": "esp32-c6-01", "port": "/dev/serial/by-id/c6", "mac": "AA:BB:CC:DD:EE:01", "target": "esp32-c6"},
        {"id": "esp32-c6-02", "port": "/dev/serial/by-id/c6b", "mac": "aa:bb:cc:dd:ee:02", "target": "esp32-c6"},
    ]
    inventory = {"boards": boards, "missing": [], "unregistered": [], "probe_errors": []}
    monkeypatch.setattr(rig_manager, "publish_inventory", lambda *a, **k: inventory)
    monkeypatch.setattr(core_service, "load_inventory_snapshot", lambda *a, **k: {
        "boards": [dict(b) for b in boards], "missing": [], "unregistered": [], "probe_errors": []})
    probes = []

    def probe(port, python):
        probes.append(port)
        mac = next(b["mac"] for b in boards if b["port"] == port)
        return _details_record(port, mac.lower())

    monkeypatch.setattr(rig_manager, "probe_details", probe)
    log = __import__("io").StringIO()
    assert manager.read_chip_details(manager.refresh_inventory(), "all", log) == 2
    assert probes == ["/dev/serial/by-id/c6", "/dev/serial/by-id/c6b"]
    on_file = manager.load_chip_details()
    assert set(on_file) == {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"}
    assert on_file["aa:bb:cc:dd:ee:01"]["matches_registry"] is True
    assert "Read chip details of esp32-c6-01" in log.getvalue()
    # A suite's discover stage reads only what is not on file.
    assert manager.refresh_inventory(details="missing")["boards"] == boards
    assert probes == ["/dev/serial/by-id/c6", "/dev/serial/by-id/c6b"], "nothing re-read"
    snapshot = manager.inventory_snapshot(annotate=True)
    assert snapshot["boards"][0]["details"]["flash_size"] == "4MB"
    assert snapshot["boards"][1]["details"]["revision"] == "v0.1"
    # A board that will not answer is noted, not fatal.
    def refuse(port, python):
        raise RuntimeError("esptool flash-id failed")
    monkeypatch.setattr(rig_manager, "probe_details", refuse)
    log = __import__("io").StringIO()
    assert manager.read_chip_details(inventory, "all", log) == 0
    assert "Could not read chip details of esp32-c6-01" in log.getvalue()


def test_artifacts_are_served_by_name_only_from_the_run_they_belong_to(tmp_path):
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.state = tmp_path
    manager.store = farm_service.JobStore(tmp_path / "farm.db")
    job = manager.store.create("suite", {"ref": "abc"}, tmp_path / "x.log")
    run = tmp_path / "runs" / job["id"] / "metrics"
    run.mkdir(parents=True)
    (run / "report.md").write_text("# Report\n")
    path, content_type, filename = manager.artifact(job["id"], "report_markdown")
    assert path == run / "report.md" and content_type.startswith("text/markdown")
    assert filename == f"{job['id'][:8]}-report.md"
    with pytest.raises(LookupError):
        manager.artifact(job["id"], "passwd")
    with pytest.raises(LookupError):
        manager.artifact(job["id"], "serial:../../token")
    with pytest.raises(FileNotFoundError):
        manager.artifact(job["id"], "junit")
    with pytest.raises(KeyError):
        manager.artifact("0" * 32, "report_markdown")
    # What the run left beyond the fixed names is enumerated, never guessed:
    # every board's capture and every family's flash image, with sizes.
    serial = tmp_path / "runs" / job["id"] / "serial"
    serial.mkdir()
    (serial / "esp32-03.serial.log").write_text("{\"evt\":\"boot\"}\n")
    (serial / "esp32-03.preflight.serial.log").write_text("boot\n")
    image = tmp_path / "artifacts" / job["id"] / "esp32-c3"
    image.mkdir(parents=True)
    (image / "flash-image.bin").write_bytes(b"\xe9" * 1024)
    (image / "bootloader.bin").write_bytes(b"\x00")
    # The queue side of a run, written by alteriom_hil.mqtt as it arrives,
    # is served like a serial log; a stray file beside it is not. It lands
    # under the suite's one directory, ALTERIOM_HIL_LOG_DIR -- the run's
    # serial/ -- and the service must look there, not at the run root.
    queue = manager.mqtt_evidence_dir(job["id"])
    assert queue == tmp_path / "runs" / job["id"] / "serial" / "mqtt"
    queue.mkdir(parents=True)
    (queue / "queue.jsonl").write_text('{"topic":"alteriom/gateways/G1/status"}\n')
    (queue / "notes.txt").write_text("not served\n")
    detail = manager.job_detail(job["id"])
    assert detail["artifacts"]["report_markdown"]["available"] is True
    assert detail["artifacts"]["board_health"]["available"] is False
    assert detail["artifacts"]["serial:esp32-03"]["bytes"] == 15
    assert detail["artifacts"]["serial:esp32-03.preflight"]["available"] is True
    assert detail["artifacts"]["mqtt:queue"]["available"] is True
    assert "mqtt:notes" not in detail["artifacts"]
    assert manager.artifact(job["id"], "mqtt:queue")[1] == "application/x-ndjson"
    assert detail["artifacts"]["firmware:esp32-c3"]["bytes"] == 1024
    assert "firmware:bootloader" not in detail["artifacts"]
    path, content_type, filename = manager.artifact(job["id"], "firmware:esp32-c3")
    assert content_type == "application/octet-stream"
    assert filename == f"{job['id'][:8]}-esp32-c3-flash-image.bin"
    path, content_type, filename = manager.artifact(job["id"], "serial:esp32-03.preflight")
    assert content_type.startswith("text/plain") and filename.endswith("esp32-03.preflight.serial.log")


def test_repository_links_are_browsable_whatever_the_remote_syntax():
    https = farm_service._https_repo
    assert https("git@github.com:Alteriom/alteriom-esp32-farm.git") == "https://github.com/Alteriom/alteriom-esp32-farm"
    assert https("ssh://git@github.com/Alteriom/painlessMesh.git") == "https://github.com/Alteriom/painlessMesh"
    assert https("https://github.com/Alteriom/painlessMesh.git") == "https://github.com/Alteriom/painlessMesh"
    assert https("https://github.com/Alteriom/painlessMesh") == "https://github.com/Alteriom/painlessMesh"
    assert https("") is None
    assert https("/home/sparck/esp32-farm-src") is None


def test_a_discovery_job_logs_what_it_found_and_summarises_it(tmp_path, monkeypatch):
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.state = tmp_path
    manager.python = Path("/usr/bin/python3")
    manager.registry = tmp_path / "inventory.yaml"
    manager.board_map = tmp_path / "board-map.yaml"
    manager.store = farm_service.JobStore(tmp_path / "farm.sqlite3")
    job = manager.store.create("inventory", {}, tmp_path / "inv.log", manager._initial_progress("inventory"))
    inventory = {
        "boards": [{"id": "esp32-03", "port": "/dev/ttyUSB1", "mac": "68:25:dd:33:5c:98", "target": "esp32"}],
        "missing": ["esp32-c6-14b4"],
        "unregistered": [{"target": "esp32-c3", "mac": "50:78:7d:00:00:01", "port": "/dev/ttyACM0"}],
        "probe_errors": [{"port": "/dev/ttyUSB9", "error": "no response"}],
    }
    monkeypatch.setattr(rig_manager, "publish_inventory", lambda *a, **k: inventory)
    monkeypatch.setattr(rig_manager, "probe_details", lambda port, python: _details_record(port, "68:25:dd:33:5c:98", "esp32", "esp32"))
    log = __import__("io").StringIO()
    result = manager._execute(job["id"], "inventory", {}, log)
    text = log.getvalue()
    assert "connected  esp32-03" in text and "/dev/ttyUSB1" in text
    assert "unregistered esp32-c3" in text
    assert "missing    esp32-c6-14b4" in text
    assert "probe failed /dev/ttyUSB9: no response" in text
    assert "Read chip details of esp32-03" in text
    assert result["summary"] == "1 connected, 1 unregistered, 1 missing; 1 of 1 boards described"
    stages = {stage["name"]: stage for stage in manager.store.get(job["id"])["progress"]}
    assert stages["discover"]["summary"] == "1 connected, 1 unregistered, 1 missing"
    assert stages["details"]["status"] == "passed"


def _suite_manager(tmp_path, monkeypatch, sha="d" * 40):
    manager = _idle_manager(tmp_path, monkeypatch, sha)
    manager.paused = False
    manager.paused_since = None
    manager.repo = tmp_path / "repo"
    _install_profiles(manager.repo)
    tests_dir = manager.repo / "suites" / "painlessmesh" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_soak_stability.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.capability(\"soak.stability\", \"delivery.ack.sustained\")\n"
        "def test_sustained_round_robin_delivery(mesh):\n    pass\n"
    )
    (tests_dir / "test_ota_mesh.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.capability(\"ota.mesh\")\n"
        "def test_same_family_firmware_transfers(mesh):\n    pass\n\n"
        "def test_helper_without_marks():\n    pass\n"
    )
    firmware = manager.repo / "suites" / "painlessmesh" / "firmware" / "src"
    firmware.mkdir(parents=True)
    (firmware / "main.cpp").write_text("// agent\n")
    return manager


def test_a_partial_suite_names_tests_it_can_find_and_a_bounded_keyword(tmp_path, monkeypatch):
    manager = _suite_manager(tmp_path, monkeypatch)
    manager._validate("suite", {"ref": "topic", "targets": ["esp32"], "tests": ["test_soak_stability.py", "test_ota_mesh.py::test_same_family_firmware_transfers"], "keyword": "soak or ota", "reuse": False})
    with pytest.raises(ValueError, match="no such test file"):
        manager._validate("suite", {"ref": "topic", "targets": ["esp32"], "tests": ["test_missing.py"]})
    with pytest.raises(ValueError, match="test files or test ids"):
        manager._validate("suite", {"ref": "topic", "targets": ["esp32"], "tests": ["../conftest.py"]})
    with pytest.raises(ValueError, match="test files or test ids"):
        manager._validate("suite", {"ref": "topic", "targets": ["esp32"], "tests": "test_ota_mesh.py"})
    with pytest.raises(ValueError, match="keyword"):
        manager._validate("suite", {"ref": "topic", "targets": ["esp32"], "keyword": "soak; rm -rf /"})
    with pytest.raises(ValueError, match="reuse"):
        manager._validate("suite", {"ref": "topic", "targets": ["esp32"], "reuse": "yes"})
    suite = "suites/painlessmesh/tests"
    assert farm_service.FarmManager.pytest_selection([], "", suite) == [suite]
    assert farm_service.FarmManager.pytest_selection(["test_ota_mesh.py::test_x"], "soak", suite) == [
        "suites/painlessmesh/tests/test_ota_mesh.py::test_x", "-k", "soak"
    ]


def test_the_suite_catalogue_lists_files_tests_and_their_capabilities(tmp_path, monkeypatch):
    manager = _suite_manager(tmp_path, monkeypatch)
    catalogue = manager.suite_catalogue()
    assert [entry["file"] for entry in catalogue] == ["test_ota_mesh.py", "test_soak_stability.py"]
    ota = catalogue[0]["tests"]
    assert ota[0] == {"name": "test_same_family_firmware_transfers", "capabilities": ["ota.mesh"]}
    assert ota[1] == {"name": "test_helper_without_marks", "capabilities": []}
    assert catalogue[1]["tests"][0]["capabilities"] == ["soak.stability", "delivery.ack.sustained"]


def test_a_held_bundle_of_the_same_commit_and_agent_is_reused(tmp_path, monkeypatch):
    manager = _suite_manager(tmp_path, monkeypatch)
    sha = "d" * 40
    agent = manager.agent_source_sha()
    earlier = manager.store.create("suite", {"ref": "topic", "resolved_sha": sha}, tmp_path / "e.log")
    artifacts = tmp_path / "artifacts" / earlier["id"]
    (artifacts / "esp32").mkdir(parents=True)
    (artifacts / "esp32" / "flash-image.bin").write_bytes(b"\xe9")
    manifest = {"painlessmesh_sha": sha, "hil_agent_sha": agent, "targets": {"esp32": {"image": "esp32/flash-image.bin"}}}
    (artifacts / "manifest.json").write_text(__import__("json").dumps(manifest))
    assert manager.reusable_artifacts(sha, ["esp32"]) == (earlier["id"], artifacts)
    assert manager.reusable_artifacts(sha, ["esp32", "esp32-c3"]) is None, "a family it did not build"
    assert manager.reusable_artifacts("e" * 40, ["esp32"]) is None, "another commit"
    # The agent source changed since: that build is not this build.
    (manager.repo / "suites" / "painlessmesh" / "firmware" / "src" / "main.cpp").write_text("// agent v2\n")
    assert manager.reusable_artifacts(sha, ["esp32"]) is None


def test_one_board_is_not_plural():
    """`Validated 1 board`, not `Validated 1 boards`.

    A one-board profile is ordinary now -- alteriom-firmware's console suite
    asks for exactly one -- so this string is on the dashboard and in the run
    report for a large share of runs.
    """
    from alteriom_hil.launcher import _plural

    assert _plural(1, "board") == "1 board"
    assert _plural(0, "board") == "0 boards"
    assert _plural(2, "board") == "2 boards"


def test_a_submitted_job_records_what_it_is_for(tmp_path, monkeypatch):
    """A run used to be described by a revision and a result. The project, the
    repository the revision belongs to, and the profile -- with the default
    written in, so nothing downstream has to know what a missing one means --
    are in the job from the moment it exists."""
    manager = _idle_manager(tmp_path, monkeypatch, "a" * 40)
    job = manager.submit("suite", {"ref": "topic", "targets": ["esp32"]})
    assert job["request"]["profile"] == "painlessmesh"
    assert job["request"]["project"] == manager.profiles["painlessmesh"].label
    assert job["request"]["repo"].startswith("https://")
    assert "painlessMesh" in job["request"]["repo"]


def _secretless(text: str) -> bool:
    return not any(value in text for value in (FAKE_APIKEY, FAKE_PHONE, "15557654321", "%2B15557654321"))


def test_a_commands_output_is_scrubbed_before_it_reaches_the_job_log(tmp_path):
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.repo = tmp_path
    from alteriom_hil.providers import Redactor, parse_callmebot_link

    redactor = Redactor.for_callmebot(parse_callmebot_link(FAKE_LINK))
    child = (
        "import sys\n"
        f"print('GET https://api.callmebot.com/whatsapp.php?phone=%2B15557654321&apikey={FAKE_APIKEY}&text=hi', flush=True)\n"
        f"print('reply: message to {FAKE_PHONE} queued', file=sys.stderr, flush=True)\n"
        "print('ordinary line', flush=True)\n"
    )
    log_path = tmp_path / "job.log"
    with log_path.open("w", encoding="utf-8") as stream:
        log = farm_service.ScrubbedLog(stream, redactor)
        log.write(f"the farm itself mentions {FAKE_APIKEY}\n")
        manager._run([sys.executable, "-c", child], log, timeout=60)
    text = log_path.read_text(encoding="utf-8")
    assert _secretless(text), text
    assert "ordinary line" in text and "queued" in text and text.count("***") >= 3

    # Nothing to scrub: the command writes to the log file directly, as before.
    plain = tmp_path / "plain.log"
    with plain.open("w", encoding="utf-8") as stream:
        manager._run([sys.executable, "-c", "print('direct', flush=True)"],
                     farm_service.ScrubbedLog(stream, Redactor()), timeout=60)
    assert "direct" in plain.read_text()


def test_a_finished_runs_evidence_is_scrubbed_before_anyone_can_see_it(tmp_path, monkeypatch):
    """Before the status says finished -- a CI client downloads the evidence
    when it does -- and before the node's hook packages it for the portal."""
    import contextlib
    import threading

    link_file = tmp_path / "callmebot-url"
    link_file.write_text(FAKE_LINK + "\n", encoding="utf-8")
    monkeypatch.setenv("ALTERIOM_HIL_CALLMEBOT_URL_FILE", str(link_file))

    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.repo = tmp_path
    manager.state = tmp_path / "state"
    (manager.state / "logs").mkdir(parents=True)
    manager.store = farm_service.JobStore(tmp_path / "farm.sqlite3")
    manager._job_local = threading.local()
    manager._cancel_lock = threading.Lock()
    manager._cancel_requests = {}
    manager.pending = __import__("queue").Queue()
    manager._hold = lambda grant: contextlib.nullcontext()
    manager._release = lambda job_id: None
    manager._storage_changed = lambda: None
    manager._discard_workspace = lambda job_id: None
    job = manager.store.create("suite", {"ref": "a" * 40}, manager.state / "logs" / "x.log")
    run_dir = manager.state / "runs" / job["id"]

    def execute(job_id, kind, request, log):
        (run_dir / "serial").mkdir(parents=True)
        (run_dir / "results.xml").write_text(f'<failure message="reply to {FAKE_PHONE}"/>', encoding="utf-8")
        (run_dir / "serial" / "esp32-01.serial.log").write_text(f"apikey={FAKE_APIKEY}\n", encoding="utf-8")
        (run_dir / "metrics").mkdir()
        (run_dir / "metrics" / "runs.jsonl").write_text('{"longrepr": "%2B15557654321"}\n', encoding="utf-8")
        log.write(f"pytest said {FAKE_LINK}\n")
        return {"summary": "ok"}

    manager._execute = execute
    seen = {}
    original_update = manager.store.update

    def update(job_id, status, result=None):
        if status == "passed":
            seen["at_status"] = [path.read_text(encoding="utf-8") for path in sorted(run_dir.rglob("*.*"))]
        return original_update(job_id, status, result)

    manager.store.update = update
    manager._on_finished = lambda job_id: seen.setdefault(
        "at_hook", [path.read_text(encoding="utf-8") for path in sorted(run_dir.rglob("*.*"))])
    manager._run_job(job, grant=None)

    assert manager.store.get(job["id"])["status"] == "passed"
    assert len(seen["at_status"]) == 3 and all(_secretless(text) for text in seen["at_status"]), seen
    assert all(_secretless(text) for text in seen["at_hook"])
    log_text = (manager.state / "logs" / f"{job['id']}.log").read_text(encoding="utf-8")
    assert _secretless(log_text), log_text
    assert "Scrubbed provider secrets from 3 evidence file(s)" in log_text


def test_the_redactor_and_a_failed_job_detail(tmp_path, monkeypatch):
    """A failure's text is stored and served; it is scrubbed like the log."""
    import contextlib
    import threading

    link_file = tmp_path / "callmebot-url"
    link_file.write_text(FAKE_LINK + "\n", encoding="utf-8")
    monkeypatch.setenv("ALTERIOM_HIL_CALLMEBOT_URL_FILE", str(link_file))
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.repo = tmp_path
    manager.state = tmp_path / "state"
    (manager.state / "logs").mkdir(parents=True)
    manager.store = farm_service.JobStore(tmp_path / "farm.sqlite3")
    manager._job_local = threading.local()
    manager._cancel_lock = threading.Lock()
    manager._cancel_requests = {}
    manager.pending = __import__("queue").Queue()
    manager._hold = lambda grant: contextlib.nullcontext()
    manager._release = lambda job_id: None
    manager._storage_changed = lambda: None
    manager._discard_workspace = lambda job_id: None
    job = manager.store.create("inventory", {}, manager.state / "logs" / "x.log")

    def execute(job_id, kind, request, log):
        raise RuntimeError(f"unexpected reply from {FAKE_LINK}")

    manager._execute = execute
    manager._run_job(job, grant=None)
    stored = manager.store.get(job["id"])
    assert stored["status"] == "failed"
    assert _secretless(stored["result"]["detail"]) and "***" in stored["result"]["detail"]
    assert _secretless((manager.state / "logs" / f"{job['id']}.log").read_text(encoding="utf-8"))


def test_configuration_shows_the_callmebot_link_only_redacted_and_the_rigs_seal_key(tmp_path, monkeypatch):
    """What a node reports to its portal, and a person reads on the rig's
    page: the stored link as `providers show` prints it, never a link that
    does not load in any form, and the public key a link is sealed to."""
    import types

    spec = importlib.util.spec_from_file_location("hil_config_for_link", HIL_CONFIG_PATH)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    link_file = tmp_path / "callmebot-url"
    link_file.write_text(FAKE_LINK + "\n", encoding="utf-8")
    stub = types.ModuleType("hil_config")
    stub.load_config = lambda *a, **k: {"paths": {"state": str(tmp_path)},
                                        "providers": {"callmebot": {"url_file": str(link_file), "send": "never"}}}
    stub.callmebot_settings = real.callmebot_settings
    stub.callmebot_budget_file = real.callmebot_budget_file
    monkeypatch.setattr(alteriom_hil, "hil_config", stub)
    monkeypatch.setenv("ALTERIOM_HIL_PROVIDER_SEAL_PUB", str(tmp_path / "no-key.pub"))
    manager = _store_manager(tmp_path)
    manager.registry = tmp_path / "inventory.yaml"
    manager.board_map = tmp_path / "board-map.active.yaml"
    manager.python = Path("/usr/bin/python3")

    config = manager.configuration()
    assert config["callmebot"]["link"] == "https://api.callmebot.com/whatsapp.php?phone=***21&apikey=***"
    assert config["callmebot"]["send"] == "never" and config["callmebot"]["max_per_day"] == 5
    assert _secretless(repr(config)) and "seal_key" not in config, "no key file, no field"

    link_file.write_text(FAKE_LINK + "&text=hello\n", encoding="utf-8")
    config = manager.configuration()
    assert config["callmebot"]["link"] is None and _secretless(repr(config)), "an invalid link is not echoed"

    monkeypatch.setattr(farm_service.farm_providers, "seal_key_info",
                        lambda path=None: {"spki": "MIIBojAN", "fingerprint": "ab" * 32})
    assert manager.configuration()["seal_key"] == {"spki": "MIIBojAN", "fingerprint": "ab" * 32}


def test_simulator_stages_exist_only_on_a_job_that_carried_evidence(tmp_path, monkeypatch):
    manager = _idle_manager(tmp_path, monkeypatch, "a" * 40)
    plain = manager.submit("suite", {"ref": "topic", "targets": ["esp32"]})
    assert [stage["name"] for stage in plain["progress"]][:2] == ["build", "discover"]

    with_evidence = manager.submit("suite", {"ref": "a" * 40, "targets": ["esp32"], "simulation": simulation_evidence()})
    names = [stage["name"] for stage in with_evidence["progress"]]
    assert names[:2] == ["protocol_sim", "mesh_sim"]
    assert with_evidence["progress"][0]["status"] == "passed"
    assert with_evidence["progress"][0]["summary"] == "17 HAL protocol scenarios passed"


# ---- the artifact store -------------------------------------------------------

import contextlib as _contextlib


@_contextlib.contextmanager
def _brief_hold(self, timeout: float = 0.05):
    lock = self._artifact_guard()
    if not lock.acquire(timeout=timeout):
        raise farm_service.ArtifactProtected("the artifact store is busy -- a prune is deleting")
    try:
        yield
    finally:
        lock.release()


def _bundle(root, bundle_id, sha, families=("esp32",), modified=None, ota=False):
    """A schema-2 bundle as a farm build leaves it: manifest, one family
    directory with its merged image and a component at its offset.
    `modified` backdates the manifest, which is when the bundle was written."""
    import json
    from alteriom_hil.artifacts import sha256

    path = root / bundle_id
    targets = {}
    for family in families:
        folder = path / family
        folder.mkdir(parents=True)
        (folder / "bootloader.bin").write_bytes(b"\xe9" + family.encode())
        (folder / "flash-image.bin").write_bytes(b"\xff" * 0x1000 + b"\xe9" + family.encode())
        targets[family] = {
            "image": f"{family}/flash-image.bin",
            "sha256": sha256(folder / "flash-image.bin"),
            "platformio_env": family,
            "board": "esp32dev",
            "segments": {"bootloader.bin": "0x1000"},
            "files": {"bootloader.bin": {"sha256": sha256(folder / "bootloader.bin")}},
        }
        if ota:
            (folder / "ota.bin").write_bytes(b"\xe9ota" + family.encode())
            targets[family]["ota"] = {
                "image": f"{family}/ota.bin",
                "sha256": sha256(folder / "ota.bin"),
            }
    manifest = {"schema": 2, "painlessmesh_sha": sha, "hil_agent_sha": "0" * 64, "targets": targets}
    (path / "manifest.json").write_text(json.dumps(manifest))
    if modified:
        import os
        from datetime import datetime

        stamp = datetime.fromisoformat(modified).timestamp()
        os.utime(path / "manifest.json", (stamp, stamp))
    return path


def _store_manager(tmp_path):
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.state = tmp_path
    manager.repo = tmp_path / "repo"
    _install_profiles(manager.repo)
    (tmp_path / "logs").mkdir(exist_ok=True)
    manager.store = farm_service.JobStore(tmp_path / "farm.sqlite3")
    return manager


def _run(manager, status="passed", result=None, created_at=None, **request):
    job = manager.store.create("suite", {"profile": "painlessmesh", "ref": "topic", **request}, manager.state / "x.log")
    if status != "queued":
        manager.store.update(job["id"], "running")
    if status in ("passed", "failed", "cancelled"):
        manager.store.update(job["id"], status, result or {})
    if created_at:
        # The whole run happened then: queued, started and finished.
        with manager.store.connect() as db:
            db.execute(
                "UPDATE jobs SET created_at=?, "
                "started_at=CASE WHEN started_at IS NULL THEN NULL ELSE ? END, "
                "finished_at=CASE WHEN finished_at IS NULL THEN NULL ELSE ? END WHERE id=?",
                (created_at, created_at, created_at, job["id"]),
            )
    return job["id"]


def _reuse(manager, bundle_id, status="passed", created_at=None):
    import os

    job_id = _run(manager, status, {"reused_artifacts_from": bundle_id}, created_at)
    root = manager.state / "artifacts"
    os.symlink(root / bundle_id, root / job_id, target_is_directory=True)
    return job_id


def test_the_artifact_index_says_what_each_bundle_is_and_which_runs_used_it(tmp_path):
    manager = _store_manager(tmp_path)
    sha = "1" * 40
    built = _run(manager, resolved_sha=sha, branch="fix/c5", repo="https://github.com/Alteriom/painlessMesh")
    _bundle(tmp_path / "artifacts", built, sha, ("esp32", "esp32-c3"))
    again = _reuse(manager, built)

    index = manager.artifact_index()
    assert index["count"] == 1 and index["links"] == 1 and index["pinned"] == 0
    entry = index["bundles"][0]
    assert entry["id"] == built and entry["profile"] == "painlessmesh"
    assert entry["revision"] == sha, "read under the profile's revision_key"
    assert entry["branch"] == "fix/c5"
    assert [family["family"] for family in entry["families"]] == ["esp32", "esp32-c3"]
    assert entry["families"][0]["image_bytes"] == 0x1000 + 1 + len("esp32")
    # What the file route is asked for: the key the bundle holds the image by.
    assert entry["families"][0]["path"] == "esp32/flash-image.bin"
    assert entry["built_by"]["id"] == built
    assert [run["id"] for run in entry["reused_by"]] == [again]
    assert entry["held"] is False and entry["source"]["kind"] == "farm-build"
    assert entry["bytes"] == index["bytes"] > 0

    detail = manager.artifact_detail(built)
    paths = {item["path"]: item for item in detail["files"]}
    assert paths["esp32/flash-image.bin"]["sha256"] == detail["manifest"]["targets"]["esp32"]["sha256"]
    assert paths["esp32/bootloader.bin"]["sha256"]
    # A reuse link is not a bundle of its own.
    with pytest.raises(KeyError):
        manager.artifact_detail(again)


def test_a_bundle_a_job_is_using_or_that_is_pinned_cannot_be_deleted(tmp_path):
    manager = _store_manager(tmp_path)
    built = _run(manager, resolved_sha="2" * 40)
    _bundle(tmp_path / "artifacts", built, "2" * 40)
    running = _reuse(manager, built, status="running")
    assert manager.artifact_index()["bundles"][0]["held"] is True
    with pytest.raises(farm_service.ArtifactProtected, match="in use"):
        manager.delete_artifact(built)
    manager.store.update(running, "passed", {"reused_artifacts_from": built})

    manager.pin_artifact(built, "the v2.0.2 release gate")
    entry = manager.artifact_detail(built)
    assert entry["pinned"] and entry["pin_note"] == "the v2.0.2 release gate"
    with pytest.raises(farm_service.ArtifactProtected, match="pinned"):
        manager.delete_artifact(built)
    with pytest.raises(ValueError):
        manager.pin_artifact(built, "two\nlines")
    manager.unpin_artifact(built)
    assert not manager.artifact_detail(built)["pinned"]
    assert (tmp_path / "artifacts" / built).is_dir()


def test_deleting_a_bundle_leaves_its_runs_saying_so_not_pointing_at_nothing(tmp_path):
    import os

    manager = _store_manager(tmp_path)
    built = _run(manager, resolved_sha="3" * 40)
    _bundle(tmp_path / "artifacts", built, "3" * 40)
    again = _reuse(manager, built)
    assert manager.job_detail(again)["bundle"] == {
        "id": built, "available": True, "removed_at": None, "removed_reason": None,
        # A farm build has no provenance; only a supplied bundle names its CI.
        "source": None,
    }

    removed = manager.delete_artifact(built)
    assert removed["links_removed"] == [again] and removed["bytes"] > 0
    assert not (tmp_path / "artifacts" / built).exists()
    assert not os.path.lexists(tmp_path / "artifacts" / again)
    for job_id in (built, again):
        bundle = manager.job_detail(job_id)["bundle"]
        assert bundle["id"] == built and bundle["available"] is False
        assert bundle["removed_at"] and bundle["removed_reason"] == "Deleted by an operator"
    # Nothing left to flash: the next run of that commit needs a new bundle.
    assert manager.artifact_index()["count"] == 0
    with pytest.raises(KeyError):
        manager.delete_artifact(built)


def test_a_prune_is_a_dry_run_until_it_is_told_otherwise(tmp_path):
    manager = _store_manager(tmp_path)
    root = tmp_path / "artifacts"
    old = _run(manager, resolved_sha="4" * 40, created_at="2020-07-01T00:00:00+00:00")
    _bundle(root, old, "4" * 40, modified="2020-07-01T00:00:00+00:00")
    recent = _run(manager, resolved_sha="5" * 40)
    _bundle(root, recent, "5" * 40)
    # Built long ago but reused now: last use is what ages a bundle.
    revived = _run(manager, resolved_sha="6" * 40, created_at="2020-07-02T00:00:00+00:00")
    _bundle(root, revived, "6" * 40, modified="2020-07-02T00:00:00+00:00")
    _reuse(manager, revived)

    preview = manager.prune_artifacts({"older_than_days": 30})
    assert preview["dry_run"] is True
    assert [entry["id"] for entry in preview["bundles"]] == [old]
    assert preview["bytes"] > 0 and (root / old).is_dir(), "a dry run deletes nothing"

    done = manager.prune_artifacts({"older_than_days": 30, "dry_run": False}, wait=True)
    assert [item["id"] for item in done["removed"]] == [old]
    assert not (root / old).exists() and (root / recent).is_dir() and (root / revived).is_dir()
    assert manager.job_detail(old)["bundle"]["removed_reason"] == "Pruned: unused for more than 30 days"

    kept = manager.prune_artifacts({"keep_per_profile": 1})
    assert [entry["id"] for entry in kept["bundles"]] == [recent], "the newest by last use is kept"

    for bad in (
        {},
        {"older_than_days": -1},
        {"keep_per_profile": True},
        {"older_than_days": "30"},
        {"dry_run": "no", "keep_per_profile": 1},
        {"everything": True},
    ):
        with pytest.raises(ValueError):
            manager.prune_artifacts(bad)


def test_a_bundle_is_handed_back_whole_or_file_by_file_and_nothing_else(tmp_path):
    import io
    import tarfile

    manager = _store_manager(tmp_path)
    sha = "7" * 40
    built = _run(manager, resolved_sha=sha)
    _bundle(tmp_path / "artifacts", built, sha)
    data, filename = manager.artifact_archive(built)
    assert filename == f"painlessmesh-{sha[:10]}-{built[:8]}.tar.gz"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        assert f"{filename[:-7]}/manifest.json" in tar.getnames()

    path, content_type, name = manager.artifact_file(built, "esp32/flash-image.bin")
    assert path.is_file() and content_type == "application/octet-stream"
    assert name == f"{built[:8]}-esp32-flash-image.bin"
    assert manager.artifact_file(built, "manifest.json")[1] == "application/json"
    with pytest.raises(LookupError):
        manager.artifact_file(built, "../farm.sqlite3")
    with pytest.raises(KeyError):
        manager.artifact_file("0" * 32, "manifest.json")


def test_storage_says_what_uses_the_disk_without_walking_it_on_the_request(tmp_path, monkeypatch):
    manager = _store_manager(tmp_path)
    built = _run(manager, resolved_sha="8" * 40)
    _bundle(tmp_path / "artifacts", built, "8" * 40)
    (tmp_path / "runs" / built).mkdir(parents=True)
    (tmp_path / "runs" / built / "results.xml").write_text("<testsuites/>")

    # A web request never walks a directory itself -- not the bundles, not
    # the evidence the farm keeps for good: the first answer names what there
    # is, leaves every size unmeasured, and says a measurement is running.
    import threading

    walked_on = []
    real_tree_size = farm_service.artifact_store.tree_size
    # Held until the first answer has been read: a walk of a directory this
    # small can finish before storage() returns, and "a measurement is
    # running" would then be a race the test lost half the time.
    measured = threading.Event()

    def recording_tree_size(path):
        on_main = threading.current_thread() is threading.main_thread()
        walked_on.append(on_main)
        if not on_main:
            measured.wait(10)
        return real_tree_size(path)

    monkeypatch.setattr(farm_service.artifact_store, "tree_size", recording_tree_size)
    first = manager.storage()
    assert True not in walked_on, "a request walked a directory on its own thread"
    assert first["measuring"] is True and first["measured_at"] is None
    by_name = {item["name"]: item for item in first["categories"]}
    assert by_name["runs"]["bytes"] is None and by_name["artifacts"]["bytes"] is None
    measured.set()
    manager.storage(wait=True)
    monkeypatch.setattr(farm_service.artifact_store, "tree_size", real_tree_size)

    storage = manager.storage(fresh=True, wait=True)
    categories = {item["name"]: item for item in storage["categories"]}
    # No toolchain caches among them: the farm does not build.
    assert set(categories) == {"artifacts", "runs", "logs", "workspaces", "database"}
    assert categories["artifacts"]["count"] == 1 and categories["artifacts"]["bytes"] > 0
    assert categories["runs"]["bytes"] == len("<testsuites/>")
    assert storage["measuring"] is False and storage["filesystem"]["total"] > 0


def test_a_delete_while_the_disk_is_measured_is_not_called_fresh(tmp_path, monkeypatch):
    """A measurement walks for a while. If something is deleted before it
    finishes, its figures are already behind: they are served, but the next
    look measures again instead of calling them fresh for five minutes."""
    manager = _store_manager(tmp_path)
    built = _run(manager, resolved_sha="ee" * 20)
    _bundle(tmp_path / "artifacts", built, "ee" * 20)
    landed = []
    looked = threading.Event()
    real_usage = farm_service.artifact_store.bundle_usage

    def a_delete_lands_mid_walk(path):
        if not landed:
            landed.append(True)
            manager._storage_changed()
        elif threading.current_thread().name == "storage-measure":
            # The walk the next look starts, on its own thread, is held open
            # until that look has been read. One bundle is walked in well
            # under the time it takes the caller to read `measuring`, and on
            # a fast runner the walk had finished by then: the farm had
            # measured again, as it should, and the test said it had not.
            looked.wait(10)
        return real_usage(path)

    monkeypatch.setattr(farm_service.artifact_store, "bundle_usage", a_delete_lands_mid_walk)
    manager.storage(fresh=True, wait=True)
    assert landed and manager._storage_state()["measured_mono"] is None
    try:
        assert manager.storage()["measuring"] is True, "the next look measures again"
    finally:
        looked.set()
    settled = manager.storage(wait=True)
    assert settled["measuring"] is False and manager._storage_state()["measured_mono"] is not None


def test_a_confirmed_prune_deletes_only_what_its_preview_showed(tmp_path):
    """The operator confirms a list. A bundle that becomes eligible after the
    preview is not deleted unseen, and one a run starts using is spared."""
    manager = _store_manager(tmp_path)
    root = tmp_path / "artifacts"
    first = _run(manager, resolved_sha="a1" * 20, created_at="2020-01-01T00:00:00+00:00")
    _bundle(root, first, "a1" * 20, modified="2020-01-01T00:00:00+00:00")
    second = _run(manager, resolved_sha="a2" * 20, created_at="2020-01-02T00:00:00+00:00")
    _bundle(root, second, "a2" * 20, modified="2020-01-02T00:00:00+00:00")
    newest = _run(manager, resolved_sha="a3" * 20)
    _bundle(root, newest, "a3" * 20)

    preview = manager.prune_artifacts({"keep_per_profile": 2})
    shown = [entry["id"] for entry in preview["bundles"]]
    assert shown == [first]
    # Since the preview: a newer build pushes `second` out of the kept window,
    # and a run starts reusing `first`.
    later = _run(manager, resolved_sha="a4" * 20)
    _bundle(root, later, "a4" * 20)
    _reuse(manager, first, status="running")

    done = manager.prune_artifacts({"keep_per_profile": 2, "ids": shown + ["f" * 32], "dry_run": False}, wait=True)
    assert done["removed"] == [] and done["spared"] == [first]
    assert done["gone"] == ["f" * 32], "a bundle already deleted is not reported as spared"
    assert (root / first).is_dir() and (root / second).is_dir(), "nothing unseen, nothing held"
    for bad in (["not-an-id"], "x" * 32, [first] * 1001):
        with pytest.raises(ValueError, match="ids"):
            manager.prune_artifacts({"keep_per_profile": 2, "ids": bad})


def test_a_preview_lists_no_more_than_a_confirmation_may_send_back(tmp_path, monkeypatch):
    """A rule can match more bundles than a confirmation may name. The
    preview then lists the oldest that many and says how many more match,
    so what it shows can always be confirmed."""
    monkeypatch.setattr(core_service, "MAX_PRUNE_IDS", 2)
    manager = _store_manager(tmp_path)
    root = tmp_path / "artifacts"
    made = []
    for day in (1, 2, 3):
        stamp = f"2020-01-0{day}T00:00:00+00:00"
        job_id = _run(manager, resolved_sha=f"{day}" * 40, created_at=stamp)
        _bundle(root, job_id, f"{day}" * 40, modified=stamp)
        made.append(job_id)
    preview = manager.prune_artifacts({"older_than_days": 30})
    assert [entry["id"] for entry in preview["bundles"]] == made[:2], "the oldest, as many as may be confirmed"
    assert preview["matched"] == 3 and preview["matched_bytes"] > preview["bytes"]
    done = manager.prune_artifacts({"older_than_days": 30, "ids": made[:2], "dry_run": False}, wait=True)
    assert [item["id"] for item in done["removed"]] == made[:2]
    assert (root / made[2]).is_dir(), "the rest waits for the next preview"


def test_a_confirmation_naming_no_bundle_only_tidies_links_to_nothing(tmp_path):
    import os

    manager = _store_manager(tmp_path)
    root = tmp_path / "artifacts"
    kept = _run(manager, resolved_sha="aa" * 20)
    _bundle(root, kept, "aa" * 20)
    orphan = _run(manager, resolved_sha="bb" * 20)
    os.symlink(root / ("c" * 32), root / orphan, target_is_directory=True)
    preview = manager.prune_artifacts({"older_than_days": 30})
    assert preview["bundles"] == [] and preview["dangling"] == [orphan]
    done = manager.prune_artifacts({"older_than_days": 30, "ids": [], "dry_run": False}, wait=True)
    assert done["removed"] == [] and done["dangling_removed"] == [orphan]
    assert not os.path.lexists(root / orphan) and (root / kept).is_dir()


def test_a_confirmed_prune_answers_before_it_has_finished_deleting(tmp_path, monkeypatch):
    """rmtree of a thousand bundles on the Pi's SD card outlasts the sixty
    seconds nginx gives a request, and a timeout must not read as a prune
    that failed while it is in fact still deleting. So the request says what
    it started and the index carries the progress."""
    import threading
    import time as clock

    manager = _store_manager(tmp_path)
    root = tmp_path / "artifacts"
    doomed = _run(manager, resolved_sha="f2" * 20, created_at="2020-01-01T00:00:00+00:00")
    _bundle(root, doomed, "f2" * 20, modified="2020-01-01T00:00:00+00:00")
    gate = threading.Event()
    real_remove = farm_service.artifact_store.remove

    def remove_when_let_go(*args, **kwargs):
        assert gate.wait(10), "the prune thread was never let go"
        return real_remove(*args, **kwargs)

    monkeypatch.setattr(farm_service.artifact_store, "remove", remove_when_let_go)
    started = manager.prune_artifacts({"older_than_days": 30, "dry_run": False})
    assert started["pruning"] is True and started["selected"] == [doomed]
    assert (root / doomed).is_dir(), "still deleting"
    active = manager.artifact_index()["pruning"]["active"]
    assert active and active["total"] == 1 and active["removed"] == 0
    # A second prune is refused rather than waiting on the lock the first holds.
    with pytest.raises(farm_service.ArtifactProtected, match="already running"):
        manager.prune_artifacts({"older_than_days": 30, "dry_run": False})
    # So is a delete, once the wait would outlast a request.
    monkeypatch.setattr(farm_service.FarmManager, "_artifact_hold", _brief_hold)
    with pytest.raises(farm_service.ArtifactProtected, match="busy"):
        manager.delete_artifact(doomed)

    gate.set()
    deadline = clock.monotonic() + 10
    while manager.artifact_index()["pruning"]["active"] and clock.monotonic() < deadline:
        clock.sleep(0.02)
    progress = manager.artifact_index()["pruning"]
    assert progress["active"] is None
    assert [item["id"] for item in progress["last"]["removed"]] == [doomed]
    assert progress["last"]["bytes"] > 0 and progress["last"]["errors"] == []
    assert not (root / doomed).exists()


def test_the_prune_thread_is_given_the_lock_the_choosing_took(tmp_path, monkeypatch):
    """Nothing may happen to a chosen bundle between the choosing and the
    deleting. If the request let the store go and the thread took it again,
    a run could take a chosen bundle to reuse or an operator could pin it in
    that gap, and the thread would delete it anyway from its stale list --
    leaving the new link pointing at nothing. So the lock the choosing took
    is handed to the thread, which is the one that releases it."""
    import threading

    manager = _store_manager(tmp_path)
    root = tmp_path / "artifacts"
    doomed = _run(manager, resolved_sha="f3" * 20, created_at="2020-01-01T00:00:00+00:00")
    _bundle(root, doomed, "f3" * 20, modified="2020-01-01T00:00:00+00:00")
    handed: list = []

    class ThreadThatWaitsToBeRun:
        """A thread whose `start` does not start it: the gap, held open."""

        def __init__(self, target, args=(), name="", **kwargs):
            handed.append((name, target, args))

        def start(self):
            pass

    monkeypatch.setattr(farm_service.threading, "Thread", ThreadThatWaitsToBeRun)
    started = manager.prune_artifacts({"older_than_days": 30, "dry_run": False})
    assert started["pruning"] is True and started["selected"] == [doomed]
    guard = manager._artifact_guard()
    assert not guard.acquire(timeout=0), "the lock was let go before the thread had it"
    assert (root / doomed).is_dir(), "nothing is deleted until the thread runs"

    _name, target, args = next(item for item in handed if item[0] == "artifact-prune")
    assert args == (guard,), "the thread is given the very lock the choosing took"
    target(*args)  # what the thread would have done
    assert not (root / doomed).exists()
    assert guard.acquire(timeout=0), "the thread releases what it was given"
    guard.release()
    assert manager.artifact_index()["pruning"]["active"] is None


def test_the_detail_carries_the_checksum_of_an_ota_image_too(tmp_path):
    """A target may ship an OTA image beside its merged flash image, and
    load_artifacts checks that file against the manifest before flashing.
    The detail listed it with no checksum, which reads as a file the farm
    cannot vouch for -- so an operator who downloaded it had nothing to
    check it against."""
    import json

    from alteriom_hil.artifacts import sha256

    manager = _store_manager(tmp_path)
    root = tmp_path / "artifacts"
    bundle_id = _run(manager, resolved_sha="a7" * 20)
    path = _bundle(root, bundle_id, "a7" * 20, ota=True)
    detail = manager.artifact_detail(bundle_id)
    files = {item["path"]: item["sha256"] for item in detail["files"]}
    assert files["esp32/ota.bin"] == sha256(path / "esp32" / "ota.bin")
    assert files["esp32/flash-image.bin"] == sha256(path / "esp32" / "flash-image.bin")
    assert files["esp32/bootloader.bin"] == sha256(path / "esp32" / "bootloader.bin")
    # An `ota` that is not what the schema says is not a checksum for
    # anything, and must not take the detail down.
    for nonsense in ([], {"image": []}, {"image": "esp32/ota.bin"}):
        manifest = json.loads((path / "manifest.json").read_text())
        manifest["targets"]["esp32"]["ota"] = nonsense
        (path / "manifest.json").write_text(json.dumps(manifest))
        listed = manager.artifact_detail(bundle_id)["files"]
        assert any(item["path"] == "esp32/ota.bin" for item in listed)


def test_the_listing_never_walks_a_bundle_for_its_size(tmp_path, monkeypatch):
    """Enumerating a bundle's files is what a listing is for. Measuring the
    whole tree on top of it is a second, uncapped walk of the same directory
    for every bundle on the store, on whatever thread is answering -- which
    on the Pi is how a dashboard poll outlasts the sixty seconds nginx gives
    it. The size comes from the measurement, and the listing arms it."""
    import threading

    manager = _store_manager(tmp_path)
    root = tmp_path / "artifacts"
    built = _run(manager, resolved_sha="b4" * 20)
    path = _bundle(root, built, "b4" * 20)
    (path / ".not-listed.bin").write_bytes(b"h" * 4096)

    walked_on_a_request = []
    real_tree_size = farm_service.artifact_store.tree_size

    def recording_tree_size(target):
        walked_on_a_request.append(threading.current_thread() is threading.main_thread())
        return real_tree_size(target)

    armed = []
    monkeypatch.setattr(farm_service.artifact_store, "tree_size", recording_tree_size)
    monkeypatch.setattr(
        farm_service.FarmManager, "_measure_if_stale",
        lambda self, fresh=False, wait=False: armed.append(fresh),
    )
    index = manager.artifact_index()
    assert True not in walked_on_a_request, "a listing walked a bundle on its own thread"
    assert armed == [False], "the listing arms a measurement rather than doing the walking"
    entry = index["bundles"][0]
    # Nothing measured yet: what the bundle's own enumeration accounts for,
    # which for a bundle the farm built is every byte of it.
    assert entry["bytes"] == sum(
        item["bytes"] for item in manager.artifact_detail(built)["files"]
    ) > 0
    assert manager._measured_bundle_bytes() == {}, "nothing is measured yet"

    monkeypatch.undo()
    manager.storage(wait=True)
    measured = manager.artifact_index()["bundles"][0]
    assert measured["bytes"] == manager._measured_bundle_bytes()[built]
    assert measured["bytes"] >= entry["bytes"] + 4096, "the whole directory, once measured"


def test_a_prune_reports_the_bundle_it_could_not_delete_and_deletes_the_rest(tmp_path):
    """A failure that is not the filesystem's -- a job store that will not
    take the removal record, a bug -- used to end the prune thread with the
    rest of the selection unattempted and the panel told the prune had
    finished with nothing wrong. Every bundle's failure is reported as its
    own, and a bundle whose images are gone says so even when the note
    saying so could not be written."""
    manager = _store_manager(tmp_path)
    root = tmp_path / "artifacts"
    stamp = "2020-01-01T00:00:00+00:00"
    doomed = sorted(
        (_run(manager, resolved_sha=f"{index:02x}" * 20, created_at=stamp) for index in (1, 2, 3)),
    )
    for index, bundle_id in enumerate(doomed):
        _bundle(root, bundle_id, f"{index + 1:02x}" * 20, modified=stamp)
    unlucky, unrecorded = doomed[0], doomed[1]
    real_remove = farm_service.artifact_store.remove

    def remove(artifact_root, bundle_id, links):
        if bundle_id == unlucky:
            raise RuntimeError("not an OSError")
        return real_remove(artifact_root, bundle_id, links)

    def record_artifact_removed(bundle_id, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    farm_service.artifact_store.remove = remove
    recorder = manager.store.record_artifact_removed
    try:
        manager.store.record_artifact_removed = record_artifact_removed
        result = manager.prune_artifacts({"older_than_days": 30, "dry_run": False}, wait=True)
    finally:
        farm_service.artifact_store.remove = real_remove
        manager.store.record_artifact_removed = recorder

    assert sorted(item["id"] for item in result["removed"]) == sorted(doomed[1:])
    assert (root / unlucky).is_dir(), "the one that failed is still there"
    assert not (root / unrecorded).exists(), "the rest were deleted all the same"
    reported = {item["id"]: item["error"] for item in result["errors"]}
    assert "not an OSError" in reported[unlucky]
    assert "recording it failed" in reported[unrecorded] and "locked" in reported[unrecorded]
    assert manager.prune_progress()["active"] is None, "the thread finished, it did not die"


def test_a_bundle_measured_while_it_was_written_is_not_reported_short(tmp_path):
    """A measurement that crosses a build measures a bundle that is still
    being written, and that figure would stand for five minutes -- so a
    prune preview would understate what it frees by however much of the
    bundle arrived after the walk. Neither figure can overstate, so the
    larger of the two is the answer."""
    manager = _store_manager(tmp_path)
    root = tmp_path / "artifacts"
    built = _run(manager, resolved_sha="c4" * 20)
    (root / built).mkdir(parents=True)
    (root / built / "bootloader.bin").write_bytes(b"\x00" * 32)
    manager.storage(wait=True)  # measures a bundle that is half written
    measured = manager._measured_bundle_bytes()[built]

    _bundle(root, built, "c4" * 20)  # the build finishes writing it
    entry = manager.artifact_index()["bundles"][0]
    enumerated = sum(item["bytes"] for item in manager.artifact_detail(built)["files"])
    assert measured < enumerated, "the measurement caught it half written"
    assert entry["bytes"] == enumerated > 0, "the listing does not report the short figure"
    # And a prune preview says the same, since it is the same entry.
    preview = manager.prune_artifacts({"older_than_days": 0})
    assert preview["bytes"] == enumerated


def test_a_delete_that_could_not_be_recorded_says_so_in_its_answer(tmp_path):
    """The images are gone the moment rmtree returns. If the note saying so
    cannot be written, the runs that used the bundle have no reason or date
    to show -- so the delete answers with what failed instead of reading as
    a clean cleanup."""
    manager = _store_manager(tmp_path)
    root = tmp_path / "artifacts"
    built = _run(manager, resolved_sha="d5" * 20)
    _bundle(root, built, "d5" * 20)

    def record_artifact_removed(bundle_id, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    recorder = manager.store.record_artifact_removed
    try:
        manager.store.record_artifact_removed = record_artifact_removed
        removed = manager.delete_artifact(built)
    finally:
        manager.store.record_artifact_removed = recorder

    assert not (root / built).exists(), "the images went"
    assert "locked" in removed["record_error"]
    assert removed["bytes"] > 0
    # A delete whose record lands says nothing of the sort.
    again = _run(manager, resolved_sha="d6" * 20)
    _bundle(root, again, "d6" * 20)
    assert "record_error" not in manager.delete_artifact(again)


def test_the_bundle_listing_is_paged_and_filtered_by_the_service(tmp_path):
    """The store only grows -- 186 bundles in its first ten days -- and a
    dashboard that fetched every one to filter it in the browser would get
    slower every week and then quietly stop showing the oldest, which is
    exactly when somebody is looking for an old one.

    The totals are of the whole store either way: they are the disk story,
    and a page of two saying "2 bundles, 30 MB" would answer a question
    nobody asked. `matched` is what the filter matched, which is what a
    pager counts through."""
    manager = _store_manager(tmp_path)
    root = tmp_path / "artifacts"
    made = []
    for index in range(5):
        bundle = _run(manager, resolved_sha=f"{index:02x}" * 20,
                      created_at=f"2026-09-0{index + 1}T00:00:00+00:00")
        _bundle(root, bundle, f"{index:02x}" * 20)
        made.append(bundle)

    whole = manager.artifact_index()
    assert whole["count"] == 5 and whole["matched"] == 5
    assert whole["limit"] == manager.ARTIFACT_PAGE_DEFAULT and whole["offset"] == 0
    assert len(whole["bundles"]) == 5
    assert whole["profiles"] == ["painlessmesh"], "the filter's options, from the whole store"

    # Newest first, and a page is a window on that order.
    newest = [entry["id"] for entry in whole["bundles"]]
    first = manager.artifact_index(limit=2)
    assert [entry["id"] for entry in first["bundles"]] == newest[:2]
    assert first["count"] == 5 and first["matched"] == 5, "of the store, not of the page"
    assert first["bytes"] == whole["bytes"], "the disk story does not shrink with the page"
    second = manager.artifact_index(limit=2, offset=2)
    assert [entry["id"] for entry in second["bundles"]] == newest[2:4]
    assert second["offset"] == 2
    last = manager.artifact_index(limit=2, offset=4)
    assert len(last["bundles"]) == 1, "the short last page"
    assert manager.artifact_index(limit=2, offset=99)["bundles"] == []

    # A ceiling, and a floor: a client cannot ask for everything, or for
    # nothing, whatever it sends.
    assert manager.artifact_index(limit=10_000)["limit"] == manager.ARTIFACT_PAGE_MAX
    assert manager.artifact_index(limit=0)["limit"] == 1
    assert manager.artifact_index(offset=-5)["offset"] == 0

    # Filtering happens here for the same reason paging does.
    one = manager.artifact_index(search=made[0][:8])
    assert [entry["id"] for entry in one["bundles"]] == [made[0]]
    assert one["matched"] == 1 and one["count"] == 5
    assert manager.artifact_index(search="00" * 20)["matched"] == 1, "by revision"
    assert manager.artifact_index(search="painlessMesh")["matched"] == 5, "by project, any case"
    assert manager.artifact_index(search="nothing here")["matched"] == 0
    assert manager.artifact_index(profile="painlessmesh")["matched"] == 5
    assert manager.artifact_index(profile="nosuch")["matched"] == 0

def test_a_host_with_no_configuration_says_so_rather_than_reporting_defaults(tmp_path, monkeypatch):
    """The page has an answer for a service that cannot read its
    configuration -- "it may be running from a host provisioned before
    /etc/alteriom-hil/config.yaml existed" -- and it appears when *no*
    section rendered.

    Reporting the rig's access point and broker as booleans made that answer
    unreachable: every configuration had at least those two rows, so a farm
    with no configuration file at all showed "Access point: off, Broker: off"
    as though that were a fact about the rig. Caught in a browser against a
    fixture, which is the only place it was visible.
    """
    manager = _store_manager(tmp_path)
    # configuration() reports the paths it was started with.
    manager.registry = tmp_path / "inventory.yaml"
    manager.board_map = tmp_path / "board-map.active.yaml"
    manager.python = Path("/usr/bin/python3")
    import sys
    import types

    empty = types.ModuleType("hil_config")
    empty.load_config = lambda *a, **k: (_ for _ in ()).throw(OSError("no such file"))
    monkeypatch.setattr(alteriom_hil, "hil_config", empty)

    config = manager.configuration()
    assert config["gateway"] == {
        "enabled": None, "ssid": None, "password_file": None, "endpoint": None,
        "channel": None,
    }, "nothing known about the rig's network is nothing claimed about it"
    assert config["mqtt"] == {"enabled": None, "url": None}
    assert config["service"]["enabled"] is None
    assert config["host"]["config_schema"] is None

    # A configuration that is read and says "off" does say off, which is the
    # distinction the whole thing rests on.
    readable = types.ModuleType("hil_config")
    readable.load_config = lambda *a, **k: {
        "schema": 1, "gateway": {"enabled": False}, "mqtt": {"enabled": False},
        "service": {"enabled": False},
    }
    monkeypatch.setattr(alteriom_hil, "hil_config", readable)
    config = manager.configuration()
    assert config["gateway"]["enabled"] is False and config["mqtt"]["enabled"] is False
    assert config["service"]["enabled"] is False and config["host"]["config_schema"] == 1


def test_a_malformed_manifest_does_not_take_the_listing_down(tmp_path):
    import json

    manager = _store_manager(tmp_path)
    root = tmp_path / "artifacts"
    good = _run(manager, resolved_sha="b1" * 20)
    _bundle(root, good, "b1" * 20)
    # A lone surrogate is valid JSON (`"\ud800"`) and no valid file name:
    # every os.path call on it raises instead of answering.
    surrogate = {"image": "\ud800.bin", "ota": {"image": "a/\udfff"}, "segments": {"\ud800": "0x1000"}}
    for layout in (
        {"schema": 2, "targets": []},
        {"schema": 2, "targets": {"esp32": {"image": 7, "files": []}}},
        {"schema": 2, "targets": {"esp32": surrogate}},
    ):
        broken = _run(manager, resolved_sha="b2" * 20)
        (root / broken).mkdir(parents=True)
        (root / broken / "manifest.json").write_text(json.dumps(layout))
        index = manager.artifact_index()
        assert good in [entry["id"] for entry in index["bundles"]]
        assert manager.artifact_detail(broken)["files"]
        manager.artifact_archive(broken)
        manager.delete_artifact(broken)


def test_a_bundle_is_aged_from_when_its_run_ran_not_when_it_was_queued(tmp_path):
    """A run that sat a day in a paused queue and built just now left a
    bundle that is minutes old, whatever its queue time says."""
    manager = _store_manager(tmp_path)
    late = _run(manager, resolved_sha="e1" * 20)
    with manager.store.connect() as db:
        db.execute("UPDATE jobs SET created_at=? WHERE id=?", ("2020-01-01T00:00:00+00:00", late))
    _bundle(tmp_path / "artifacts", late, "e1" * 20)
    entry = manager.artifact_index()["bundles"][0]
    assert entry["last_used_at"] > "2021"
    assert manager.prune_artifacts({"older_than_days": 30})["bundles"] == []


def test_an_archive_name_is_valid_whatever_the_profile_is_called():
    top = farm_service.FarmManager._archive_top("p" * 64, "a" * 40, "b" * 32)
    assert len(top) <= 64 and top.endswith("-aaaaaaaaaa-bbbbbbbb")
    from alteriom_hil import artifact_store

    assert artifact_store.FILE_NAME.fullmatch(top)
    assert farm_service.FarmManager._archive_top(None, None, "b" * 32) == "bundle-bbbbbbbb"
    assert farm_service.FarmManager._archive_top("-odd name/", "x y", "b" * 32) == "odd-name--x-y-bbbbbbbb"


def test_a_pin_waits_for_a_delete_in_progress_and_never_pins_nothing(tmp_path):
    """Pinning takes the lock a delete and a prune hold, so it cannot land
    between a delete judging a bundle unpinned and removing it."""
    import threading

    manager = _store_manager(tmp_path)
    built = _run(manager, resolved_sha="f1" * 20)
    _bundle(tmp_path / "artifacts", built, "f1" * 20)
    guard = manager._artifact_guard()
    outcome = []
    guard.acquire()
    try:
        pinning = threading.Thread(target=lambda: outcome.append(manager.pin_artifact(built, "keep")))
        pinning.start()
        pinning.join(0.3)
        assert pinning.is_alive(), "the pin waits for the artifact lock"
    finally:
        guard.release()
    pinning.join(5)
    assert outcome and outcome[0]["pinned"]
    manager.unpin_artifact(built)
    manager.delete_artifact(built)
    with pytest.raises(KeyError):
        manager.pin_artifact(built, "too late")
    assert not (manager.store.artifact_record(built) or {}).get("pinned_at")


def test_a_bundle_still_being_built_is_listed_but_not_handed_over(tmp_path):
    """A build copies its images in one at a time and writes the manifest
    last. Mid-build the directory is a partial bundle: an operator should see
    it, but a download would be an unflashable snapshot."""
    manager = _store_manager(tmp_path)
    building = manager.store.create(
        "suite", {"profile": "painlessmesh", "ref": "topic"}, tmp_path / "b.log",
        [{"name": "build", "label": "Build artifacts", "status": "pending"}],
    )["id"]
    manager.store.update(building, "running")
    manager._stage(building, "build", "running")
    _bundle(tmp_path / "artifacts", building, "dd" * 20)

    assert [entry["id"] for entry in manager.artifact_index()["bundles"]] == [building]
    for attempt in (lambda: manager.artifact_archive(building),
                    lambda: manager.artifact_file(building, "manifest.json")):
        with pytest.raises(farm_service.ArtifactProtected, match="still being built"):
            attempt()

    # A build that exited nonzero left the same partial directory behind, and
    # the job is over: nothing will finish it, and it is not downloadable.
    manager._stage(building, "build", "failed", "pio run exited 1")
    manager.store.update(building, "failed", {"summary": "Build failed"})
    for attempt in (lambda: manager.artifact_archive(building),
                    lambda: manager.artifact_file(building, "manifest.json")):
        with pytest.raises(farm_service.ArtifactProtected, match="did not finish"):
            attempt()
    assert [entry["id"] for entry in manager.artifact_index()["bundles"]] == [building], "still listed"
    assert manager.delete_artifact(building)["bytes"] > 0, "and still deletable"

    # What settles it is the build stage, not the job's status: a run that
    # built and then failed its tests has a whole bundle, and that bundle is
    # the evidence.
    tested = manager.store.create(
        "suite", {"profile": "painlessmesh", "ref": "topic"}, tmp_path / "t.log",
        [{"name": "build", "label": "Build artifacts", "status": "pending"}],
    )["id"]
    _bundle(tmp_path / "artifacts", tested, "de" * 20)
    manager._stage(tested, "build", "passed", "Built 1 family artifact(s)")
    manager.store.update(tested, "failed", {"summary": "2 tests failed"})
    assert manager.artifact_archive(tested)[0][:2] == bytes.fromhex("1f8b"), "a gzip archive"
    assert manager.artifact_file(tested, "manifest.json")[0].is_file()


def test_a_running_reuse_shows_the_bundle_it_flashes(tmp_path):
    manager = _store_manager(tmp_path)
    built = _run(manager, resolved_sha="c1" * 20)
    _bundle(tmp_path / "artifacts", built, "c1" * 20)
    running = _reuse(manager, built, status="running")
    bundle = manager.job_detail(running)["bundle"]
    assert bundle["id"] == built and bundle["available"] is True


def test_a_failed_reuse_still_names_its_bundle_after_the_bundle_is_pruned(tmp_path):
    """A run that fails or is cancelled writes a result with no
    `reused_artifacts_from`, and deleting the bundle removes the run's link
    to it. The bundle it chose is recorded on its build stage instead, so
    the run still says which images it flashed, and that they were pruned."""
    import os

    manager = _store_manager(tmp_path)
    root = tmp_path / "artifacts"
    built = _run(manager, resolved_sha="d1" * 20)
    _bundle(root, built, "d1" * 20)
    failed = manager.store.create(
        "suite", {"profile": "painlessmesh", "ref": "topic"}, tmp_path / "f.log",
        [{"name": "build", "label": "Build artifacts", "status": "pending"}],
    )["id"]
    manager.store.update(failed, "running")
    os.symlink(root / built, root / failed, target_is_directory=True)
    manager._stage(failed, "build", "running", bundle=built)
    manager.store.update(failed, "failed", {"summary": "Validation failed", "failed_stage": "test"})
    assert manager.job_detail(failed)["bundle"]["id"] == built
    assert manager.job_detail(failed)["bundle"]["available"] is True

    manager.delete_artifact(built)
    bundle = manager.job_detail(failed)["bundle"]
    assert bundle["id"] == built and bundle["available"] is False
    assert bundle["removed_reason"] == "Deleted by an operator"


def test_the_artifact_routes_need_the_token_and_answer_in_http_terms(tmp_path):
    import json
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    manager = _store_manager(tmp_path)
    built = _run(manager, resolved_sha="9" * 40)
    _bundle(tmp_path / "artifacts", built, "9" * 40)
    token = "t" * 40
    server = ThreadingHTTPServer(("127.0.0.1", 0), farm_service.make_handler(manager, token, tmp_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, body=None, auth=True):
        headers = {"Authorization": f"Bearer {token}"} if auth else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(base + path, data=data, method=method, headers=headers), timeout=5) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            return error.code, error.headers, error.read()

    (tmp_path / "runs" / built / "serial").mkdir(parents=True)
    (tmp_path / "runs" / built / "serial" / "esp32-03.serial.log").write_bytes(b"boot\n")
    try:
        # The dashboard sends artifact names through encodeURIComponent and
        # nginx forwards them as they came: `serial:esp32-03` arrives as
        # `serial%3Aesp32-03`, and used to fall through to a 404.
        status, _, body = call("GET", f"/api/v1/jobs/{built}/artifacts/serial%3Aesp32-03")
        assert status == 200 and body == b"boot\n"
        assert call("GET", f"/api/v1/jobs/{built}/artifacts/firmware%3Aesp32")[0] == 200
        assert call("GET", f"/api/v1/jobs/{built}/artifacts/serial%3A..%2F..%2Ffarm.sqlite3")[0] == 404
        assert call("GET", "/api/v1/artifacts", auth=False)[0] == 401
        assert call("DELETE", f"/api/v1/artifacts/{built}", auth=False)[0] == 401
        status, _, body = call("GET", "/api/v1/artifacts")
        assert status == 200 and json.loads(body)["bundles"][0]["id"] == built
        status, headers, body = call("GET", f"/api/v1/artifacts/{built}/bundle")
        assert status == 200 and headers["Content-Type"] == "application/gzip"
        assert headers["Content-Disposition"].startswith("attachment;") and body[:2] == b"\x1f\x8b"
        status, headers, _ = call("GET", f"/api/v1/artifacts/{built}/files/esp32/flash-image.bin")
        assert status == 200 and headers["Content-Type"] == "application/octet-stream"
        assert call("GET", f"/api/v1/artifacts/{built}/files/esp32/../../farm.sqlite3")[0] == 404
        # A manifest may name a file through more directories than any cap the
        # route might have invented; what is served is still only what the
        # bundle holds. (Windows caps a path at 260 characters by default.)
        if os.name == "posix":
            import json as json_module

            parts = ["d" * 60 for _ in range(20)]
            deep = tmp_path / "artifacts" / built
            deep.joinpath(*parts).mkdir(parents=True)
            (deep.joinpath(*parts) / "fw.bin").write_bytes(b"\xe9deep")
            image = "/".join(parts) + "/fw.bin"
            assert len(image) > 1000
            manifest = json_module.loads((deep / "manifest.json").read_text())
            manifest["targets"]["deep"] = {"image": image, "sha256": "0" * 64}
            (deep / "manifest.json").write_text(json_module.dumps(manifest))
            status, _, body = call("GET", f"/api/v1/artifacts/{built}/files/{image}")
            assert status == 200 and body == b"\xe9deep"
        assert call("POST", f"/api/v1/artifacts/{built}/pin", {"note": "keep", "extra": 1})[0] == 400
        assert call("POST", f"/api/v1/artifacts/{built}/pin", {"note": "keep"})[0] == 200
        assert call("DELETE", f"/api/v1/artifacts/{built}")[0] == 409
        assert call("POST", f"/api/v1/artifacts/{built}/unpin", {})[0] == 200
        status, _, body = call("POST", "/api/v1/artifacts/prune", {"keep_per_profile": 0})
        assert status == 200 and json.loads(body)["dry_run"] is True
        assert call("POST", "/api/v1/artifacts/prune", {})[0] == 400
        assert call("DELETE", f"/api/v1/artifacts/{built}")[0] == 200
        assert call("GET", f"/api/v1/artifacts/{built}")[0] == 404
        # The farm builds nothing: a build is gone, and says what replaced it,
        # and there are no toolchain caches to clear.
        status, _, body = call("POST", "/api/v1/builds", {"ref": "main"})
        assert status == 410 and "does not build firmware" in json.loads(body)["error"]
        assert call("DELETE", "/api/v1/storage/platformio/esp32")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Bundles supplied by a producer (docs/artifact-first-plan.md, step 2)
# ---------------------------------------------------------------------------


def _supply_manager(tmp_path, monkeypatch=None, remote_sha="9" * 40):
    """A store manager whose repo carries a HIL agent, so `agent_source_sha`
    answers with something a manifest can agree with."""
    manager = _store_manager(tmp_path)
    agent = manager.repo / "suites" / "painlessmesh" / "firmware" / "src"
    agent.mkdir(parents=True)
    (agent / "main.cpp").write_text("void setup() {}\n", encoding="utf-8")
    if monkeypatch is not None:
        class Found:
            returncode, stderr = 0, ""
            stdout = f"{remote_sha}\trefs/heads/topic\n"

        monkeypatch.setattr(farm_service.subprocess, "run", lambda *a, **k: Found())
    return manager


def _built_elsewhere(tmp_path, manager, sha, families=("esp32", "esp32-c3")):
    """A bundle as a producer's CI leaves it, agreeing with this farm's agent."""
    import json

    path = _bundle(tmp_path, "e" * 32, sha, families=families)
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["hil_agent_sha"] = manager.agent_source_sha()
    (path / "manifest.json").write_text(json.dumps(manifest))
    return path


def _archive(path, top="hil-firmware"):
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(path, arcname=top)
    return buffer.getvalue()


def _supply_fields(commit, **overrides):
    fields = {
        "profile": "painlessmesh",
        "repo": "https://github.com/Alteriom/alteriom-esp32-farm",
        "workflow": ".github/workflows/hil-painlessmesh.yml",
        "run_id": "34670860918",
        "run_url": "https://github.com/Alteriom/alteriom-esp32-farm/actions/runs/34670860918",
        "commit": commit,
    }
    fields.update(overrides)
    return fields


def test_a_bundle_is_taken_only_from_the_producer_its_profile_names(tmp_path):
    import json

    sha = "9" * 40
    manager = _supply_manager(tmp_path)
    body = _archive(_built_elsewhere(tmp_path, manager, sha))

    accepted = manager.accept_bundle(_supply_fields(sha), body)
    assert farm_service.artifact_store.BUNDLE_ID.fullmatch(accepted["id"])
    assert accepted["revision"] == sha
    assert accepted["families"] == ["esp32", "esp32-c3"]
    stored = tmp_path / "artifacts" / accepted["id"]
    assert (stored / "manifest.json").is_file(), "laid out as flashed, top directory dropped"
    assert (stored / "esp32" / "flash-image.bin").is_file()
    provenance = json.loads((stored / "provenance.json").read_text())
    assert provenance["run_id"] == "34670860918" and provenance["commit"] == sha
    assert not list(tmp_path.glob("artifacts/.incoming-*")), "nothing staged is left behind"

    # Everything the farm cannot check for itself is refused.
    for overrides, message in (
        ({"repo": "https://github.com/someone/else"}, "takes bundles from"),
        ({"workflow": ".github/workflows/anything.yml"}, "takes bundles from"),
        ({"run_id": ""}, "numeric id"),
        ({"run_url": "http://insecure.example"}, "https URL"),
        ({"commit": "a" * 40}, "not"),
        ({"commit": "nope"}, "40-character revision"),
        ({"profile": "nosuch"}, "unsupported validation profile"),
    ):
        with pytest.raises(ValueError, match=message):
            manager.accept_bundle({**_supply_fields(sha), **overrides}, body)

    # A bundle built against another farm checkout speaks another protocol.
    (manager.repo / "suites" / "painlessmesh" / "firmware" / "src" / "main.cpp").write_text(
        "void setup() { moved(); }\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="different HIL agent"):
        manager.accept_bundle(_supply_fields(sha), body)

    # A profile that names no producer takes no bundle at all. Every profile
    # this repository ships declares one now, so the case is made here rather
    # than borrowed from whichever happens not to -- the borrowed one turned
    # into a different error the day it grew a `supply:` section. Last,
    # because it edits the registry every check above reads.
    #
    # A profile without one cannot even be loaded -- nothing could ever give
    # it firmware -- so the producer is taken off a loaded one: the service's
    # own check is the second line, and must hold on its own.
    import dataclasses

    manager.profiles["painlessmesh"] = dataclasses.replace(
        manager.profiles["painlessmesh"], supply_repo=None, supply_workflow=None
    )
    assert not manager.profiles["painlessmesh"].accepts_supplied_bundles
    with pytest.raises(ValueError, match="declares no producer"):
        manager.accept_bundle(_supply_fields(sha), body)


def test_a_supplied_bundle_is_verified_the_way_a_farm_build_is(tmp_path):
    sha = "9" * 40
    manager = _supply_manager(tmp_path)
    path = _built_elsewhere(tmp_path, manager, sha, families=("esp32",))
    (path / "esp32" / "flash-image.bin").write_bytes(b"\xff" * 0x1000 + b"\xe9tampered")

    with pytest.raises(ValueError, match="checksum mismatch"):
        manager.accept_bundle(_supply_fields(sha), _archive(path))
    assert not list((tmp_path / "artifacts").glob("*")), "a bundle that fails is not kept"


def test_a_bundle_archive_cannot_name_its_way_out_of_the_store(tmp_path):
    import io
    import tarfile

    manager = _supply_manager(tmp_path)
    sha = "9" * 40

    def archive_of(*entries):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, kind in entries:
                info = tarfile.TarInfo(name)
                if kind == "link":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/etc/passwd"
                    archive.addfile(info)
                    continue
                payload = b"x" * 16
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        return buffer.getvalue()

    for entries, message in (
        ((("hil-firmware/../escape.bin", "file"),), "outside itself"),
        ((("/etc/passwd", "file"),), "outside itself"),
        ((("hil-firmware/link", "link"),), "regular files only"),
        ((("one/manifest.json", "file"), ("two/manifest.json", "file")), "exactly one directory"),
        ((("beside.bin", "file"),), "beside it"),
    ):
        with pytest.raises(ValueError, match=message):
            manager.accept_bundle(_supply_fields(sha), archive_of(*entries))

    assert not (tmp_path / "escape.bin").exists()
    assert not list((tmp_path / "artifacts").glob("*")), "nothing is kept from a refused archive"


def test_an_archive_that_expands_past_the_limit_is_refused(tmp_path, monkeypatch):
    import io
    import tarfile

    from alteriom_hil import artifacts

    manager = _supply_manager(tmp_path)
    # The limit lives with the extractor that enforces it -- both ends of a
    # bundle read it now (docs/public-release-plan.md, step 13d).
    monkeypatch.setattr(artifacts, "MAX_BUNDLE_BYTES", 4096)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("hil-firmware/flash-image.bin")
        info.size = 8192
        archive.addfile(info, io.BytesIO(b"\0" * 8192))
    # Compressed it is a few hundred bytes; the limit is on what it becomes.
    with pytest.raises(ValueError, match="expands past"):
        manager.accept_bundle(_supply_fields("9" * 40), buffer.getvalue())


def test_a_run_that_names_a_bundle_is_held_to_it(tmp_path, monkeypatch):
    sha = "9" * 40
    manager = _supply_manager(tmp_path, monkeypatch, remote_sha=sha)
    accepted = manager.accept_bundle(
        _supply_fields(sha), _archive(_built_elsewhere(tmp_path, manager, sha))
    )

    request = {"ref": "topic", "targets": ["esp32", "esp32-c3"], "artifact": accepted["id"]}
    assert manager._validate("suite", dict(request)) == sha

    for overrides, message in (
        ({"artifact": "f" * 32}, "no bundle"),
        ({"artifact": "not-a-bundle-id"}, "must be the id of a bundle"),
        ({"targets": ["esp32", "esp32-s3"]}, "has no esp32-s3"),
    ):
        with pytest.raises(ValueError, match=message):
            manager._validate("suite", {**request, **overrides})

    # A bundle built from another commit is not this run's bundle, however
    # well formed it is.
    older = _built_elsewhere(tmp_path / "older", manager, "a" * 40, families=("esp32",))
    stale = manager.accept_bundle(_supply_fields("a" * 40), _archive(older))
    with pytest.raises(ValueError, match="was built from"):
        manager._validate("suite", {**request, "artifact": stale["id"], "targets": ["esp32"]})


def test_a_supplied_bundle_says_where_it_came_from(tmp_path):
    manager = _supply_manager(tmp_path)
    sha = "9" * 40
    accepted = manager.accept_bundle(
        _supply_fields(sha), _archive(_built_elsewhere(tmp_path, manager, sha))
    )

    entry = next(
        bundle
        for bundle in manager.artifact_index()["bundles"]
        if bundle["id"] == accepted["id"]
    )
    assert entry["source"]["kind"] == "supplied"
    assert entry["source"]["run_id"] == "34670860918"
    assert entry["source"]["repo"].endswith("/alteriom-esp32-farm")
    # What the build stage says instead of "Built 6 family artifact(s)".
    assert manager._supply_origin(entry["source"]) == "alteriom-esp32-farm CI run 34670860918"


def test_the_bundle_upload_needs_the_token_and_answers_in_http_terms(tmp_path):
    import json
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    sha = "9" * 40
    manager = _supply_manager(tmp_path)
    body = _archive(_built_elsewhere(tmp_path, manager, sha))
    token = "t" * 40
    server = ThreadingHTTPServer(("127.0.0.1", 0), farm_service.make_handler(manager, token, tmp_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def upload(payload, auth=True, **overrides):
        query = urlencode({**_supply_fields(sha), **overrides})
        headers = {"Content-Type": "application/gzip"}
        if auth:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(f"{base}/api/v1/artifacts?{query}", data=payload, method="POST", headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        assert upload(body, auth=False)[0] == 401
        status, accepted = upload(body)
        assert status == 201 and accepted["revision"] == sha
        assert (tmp_path / "artifacts" / accepted["id"] / "manifest.json").is_file()

        status, answer = upload(body, repo="https://github.com/someone/else")
        assert status == 400 and "takes bundles from" in answer["error"]

        status, answer = upload(b"")
        assert status == 400 and "empty" in answer["error"]

        status, answer = upload(b"not a tarball at all")
        assert status == 400, answer
    finally:
        server.shutdown()
        server.server_close()


# ---- what a bundle is, and whose -------------------------------------------


def test_a_supplied_bundle_is_listed_under_its_project_not_as_unknown(tmp_path):
    """Every bundle the farm has taken since nothing built on the rig was
    listed as project "unknown", with no repository and no branch -- the
    canary, painlessMesh and alteriom-firmware alike. Identity was read from
    the job that built a bundle, and a supplied bundle has none: it is named
    at upload. Its provenance says which profile, and now which branch and
    whose run."""
    sha = "9" * 40
    manager = _supply_manager(tmp_path)
    path = _built_elsewhere(tmp_path, manager, sha)
    accepted = manager.accept_bundle(
        _supply_fields(sha, branch="fix/rejoin-459", actor="sparck75"), _archive(path)
    )

    entry = next(item for item in manager.artifact_index()["bundles"] if item["id"] == accepted["id"])
    assert entry["profile"] == "painlessmesh"
    assert entry["project"] == manager.profiles["painlessmesh"].label, "not unknown"
    assert entry["repo"] == "https://github.com/Alteriom/painlessMesh"
    assert entry["branch"] == "fix/rejoin-459"
    assert entry["actor"] == "sparck75"
    assert entry["revision"] == sha
    # When it arrived, not when a file in it was last touched.
    assert entry["created_at"] == entry["source"]["received_at"]
    assert entry["source"]["kind"] == "supplied"

    # And the detail and the download say the same thing.
    assert manager.artifact_detail(accepted["id"])["project"] == entry["project"]
    _body, name = manager.artifact_archive(accepted["id"])
    assert name.startswith("painlessmesh-"), f"the archive is named for its project, not {name}"


def test_a_bundle_that_arrived_before_provenance_said_whose_asks_the_run_that_flashed_it(tmp_path):
    """Bundles received before the farm asked for a branch and a user carry
    neither. The earliest run that had one in hand was dispatched for a
    branch, by somebody -- and that is the nearest thing a supplied bundle
    has to the job that built it. The earliest, because later runs reusing
    it may have been asked for by anyone."""
    import json

    sha = "9" * 40
    manager = _supply_manager(tmp_path)
    path = _built_elsewhere(tmp_path, manager, sha)
    accepted = manager.accept_bundle(_supply_fields(sha), _archive(path))
    provenance = manager.artifact_root / accepted["id"] / "provenance.json"
    old = json.loads(provenance.read_text(encoding="utf-8"))
    old.pop("branch", None)
    old.pop("actor", None)
    provenance.write_text(json.dumps(old), encoding="utf-8")

    first = _run(manager, created_at="2026-09-12T01:00:00+00:00",
                 branch="main", actor="sparck75", resolved_sha=sha)
    later = _run(manager, created_at="2026-09-13T01:00:00+00:00",
                 branch="release/2.x", actor="someone-else", resolved_sha=sha)
    import os
    root = manager.artifact_root
    for job_id in (later, first):
        os.symlink(root / accepted["id"], root / job_id, target_is_directory=True)

    entry = manager.artifact_detail(accepted["id"])
    assert entry["branch"] == "main" and entry["actor"] == "sparck75"
    assert {run["id"] for run in entry["reused_by"]} == {first, later}


def test_a_branch_or_user_a_bundle_claims_is_checked_where_it_arrives(tmp_path):
    """Both are shown, so both are bounded: an upload cannot put markup or a
    shell fragment on the dashboard by calling it a branch."""
    sha = "9" * 40
    manager = _supply_manager(tmp_path)
    path = _built_elsewhere(tmp_path, manager, sha)
    with pytest.raises(ValueError, match="branch must be"):
        manager.accept_bundle(_supply_fields(sha, branch="main; rm -rf /"), _archive(path))
    with pytest.raises(ValueError, match="actor must be"):
        manager.accept_bundle(_supply_fields(sha, actor="<script>"), _archive(path))
    # A bot is somebody too, and an unnamed producer is still a producer.
    bot = manager.accept_bundle(_supply_fields(sha, actor="github-actions[bot]"), _archive(path))
    assert manager.bundle_provenance(bot["id"])["actor"] == "github-actions[bot]"
    plain = manager.accept_bundle(_supply_fields(sha), _archive(path))
    assert manager.bundle_provenance(plain["id"])["actor"] is None


def test_a_run_records_who_started_it(tmp_path, monkeypatch):
    manager = _validating_manager()
    manager._validate("suite", {"ref": "main", "targets": ["esp32"], "actor": "sparck75"})
    with pytest.raises(ValueError, match="actor must be a GitHub login"):
        manager._validate("suite", {"ref": "main", "targets": ["esp32"], "actor": "a b"})


# ---- what fills each kind of storage ---------------------------------------


def test_storage_detail_lists_what_fills_a_directory_largest_first_and_names_its_run(tmp_path):
    """The panel says run evidence is 2 GB; this is which runs. Every child
    of runs/, logs/ and workspaces/ is named for its job, so each entry says
    whose it is -- and one whose job has left the history is an orphan,
    which is exactly what someone freeing space wants to find."""
    manager = _store_manager(tmp_path)
    (tmp_path / "artifacts").mkdir()

    small = _run(manager, branch="main", actor="sparck75")
    large = _run(manager, branch="fix/c5")
    runs = tmp_path / "runs"
    for job_id, size in ((small, 10), (large, 5000), ("0" * 32, 300)):
        (runs / job_id).mkdir(parents=True)
        (runs / job_id / "results.xml").write_bytes(b"x" * size)
    (tmp_path / "logs" / f"{large}.log").write_bytes(b"y" * 70)

    detail = manager.storage_detail("runs", wait=True)
    assert detail["label"] == "Run evidence" and detail["matched"] == 3
    assert [item["name"] for item in detail["entries"]] == [large, "0" * 32, small], "largest first"
    by_name = {item["name"]: item for item in detail["entries"]}
    assert by_name[large]["bytes"] == 5000 and by_name[large]["files"] == 1
    assert by_name[large]["job"]["branch"] == "fix/c5"
    assert by_name[small]["job"]["actor"] == "sparck75"
    assert by_name["0" * 32]["job"] is None, "an orphan says so"
    assert detail["bytes"] == 5310

    # A log is named <job>.log, and is its job's too.
    logs = manager.storage_detail("logs", wait=True)
    assert logs["entries"][0]["job"]["id"] == large

    # A page at a time.
    paged = manager.storage_detail("runs", limit=2, offset=2, wait=True)
    assert [item["name"] for item in paged["entries"]] == [small] and paged["matched"] == 3

    # The job database has nothing to list, and says what it holds instead.
    database = manager.storage_detail("database", wait=True)
    assert database["jobs"] == 2 and database["entries"] == []

    with pytest.raises(KeyError):
        manager.storage_detail("etc")
    with pytest.raises(ValueError, match="limit"):
        manager.storage_detail("runs", limit=0)


def test_looking_at_what_fills_a_directory_never_walks_it_on_the_request(tmp_path, monkeypatch):
    """The Pi answers through nginx in sixty seconds, and a walk of every run
    directory has taken longer. The detail comes from the measurement."""
    manager = _store_manager(tmp_path)
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "runs" / ("a" * 32)).mkdir(parents=True)
    manager.storage_detail("runs", wait=True)

    walked = []
    monkeypatch.setattr(farm_service.artifact_store, "child_usage", lambda *a: walked.append(a) or ([], 0, 0))
    monkeypatch.setattr(farm_service.artifact_store, "tree_size", lambda *a: walked.append(a) or (0, 0))
    manager.storage_detail("runs")
    assert walked == [], "a fresh measurement is read, not repeated"


# ---- statistics -------------------------------------------------------------


def _history(manager, kind, status, created, started=None, finished=None, progress=None, **request):
    """A job as the history holds it, at the times given (UTC datetimes)."""
    import json

    job = manager.store.create(kind, {"profile": "painlessmesh", "ref": "main", **request}, manager.state / "x.log")
    manager.store.update(job["id"], status, {})
    with manager.store.connect() as db:
        db.execute(
            "UPDATE jobs SET created_at=?, started_at=?, finished_at=?, progress_json=? WHERE id=?",
            (created.isoformat(), started.isoformat() if started else None,
             finished.isoformat() if finished else None, json.dumps(progress or []), job["id"]),
        )
    return job["id"]


def test_statistics_are_the_farms_own_history_counted_the_way_an_owner_asks(tmp_path, monkeypatch):
    """Suite runs are the unit, a cancelled run is no verdict, and each
    duration and wait is one that actually happened (nearest rank)."""
    from datetime import datetime, timedelta, timezone

    manager = _store_manager(tmp_path)
    manager.inventory_snapshot = lambda annotate=False: {"boards": [{"id": "esp32-03"}, {"id": "esp32-c6-14b4"}]}
    (tmp_path / "board-health.json").write_text(
        '{"esp32-03": {"verdict": "passed", "checked_at": "2026-09-13T10:00:00+00:00"}}', encoding="utf-8"
    )
    now = datetime.now(timezone.utc)
    hour = timedelta(hours=1)
    built = [{"name": "build", "label": "Build artifacts", "status": "passed"}]
    for index, (status, wait, run) in enumerate([("passed", 0, 600), ("passed", 10, 900), ("failed", 20, 1200), ("cancelled", 5, None)]):
        created = now - (index + 1) * hour
        started = created + timedelta(seconds=wait)
        progress = built + ([{"name": "test", "label": "Run validation", "status": "failed"}] if status == "failed" else [])
        _history(manager, "suite", status, created, started, started + timedelta(seconds=run) if run else started, progress)
    # A discovery holds the rig too, and is not a suite run.
    _history(manager, "inventory", "passed", now - 30 * timedelta(minutes=1), now - 30 * timedelta(minutes=1), now - 29 * timedelta(minutes=1))

    stats = manager.farm_statistics(7)
    assert stats["totals"] == {"runs": 4, "passed": 2, "failed": 1, "cancelled": 1, "active": 0, "pass_rate": round(2 / 3, 4)}
    assert stats["queue_wait"] == {"median": 7.5, "p90": 20, "max": 20}
    assert stats["duration"] == {"median": 900, "p90": 1200}, "a cancelled run has no run time worth counting"
    assert stats["failed_stages"] == [{"stage": "test", "label": "Run validation", "count": 1}]
    assert stats["firmware"] == {"built": 4}
    assert stats["discoveries"] == 1
    assert stats["boards"] == {"connected": 2, "passed": 1, "failed": 0, "unchecked": 1,
                               "last_checked": "2026-09-13T10:00:00+00:00"}
    (project,) = stats["by_project"]
    assert project["runs"] == 4 and project["median_duration"] == 900 and project["p90_duration"] == 1200
    assert sum(day["passed"] + day["failed"] + day["cancelled"] for day in stats["per_day"]) == 4
    assert len(stats["per_day"]) == 7, "whole days, today included"

    with pytest.raises(ValueError, match="days must be between"):
        manager.farm_statistics(0)
    with pytest.raises(ValueError, match="days must be between"):
        manager.farm_statistics(91)
    with pytest.raises(ValueError, match="tz_offset_minutes"):
        manager.farm_statistics(7, 15 * 60)


def test_where_firmware_came_from_is_read_from_what_each_run_recorded(tmp_path):
    """Supplied is named in the request, reused on the build stage; a run from
    before the stage named its bundle says so only in its summary, and that
    is read as a fallback; a run that never got as far as firmware says so."""
    from datetime import datetime, timedelta, timezone

    manager = _store_manager(tmp_path)
    manager.inventory_snapshot = lambda annotate=False: {"boards": []}
    now = datetime.now(timezone.utc)
    at = lambda minutes: now - timedelta(minutes=minutes)
    _history(manager, "suite", "passed", at(60), at(60), at(50),
             [{"name": "build", "status": "skipped", "summary": "Supplied by x CI run 1", "bundle": "b" * 32}],
             artifact="b" * 32)
    _history(manager, "suite", "passed", at(49), at(49), at(40),
             [{"name": "build", "status": "skipped", "bundle": "c" * 32}])
    _history(manager, "suite", "failed", at(39), at(39), at(30),
             [{"name": "build", "status": "skipped", "summary": "Reused the artifacts run 14dd648f built for this commit"},
              {"name": "test", "status": "failed"}])
    _history(manager, "suite", "failed", at(29), at(29), at(20),
             [{"name": "build", "label": "Build artifacts", "status": "failed"}])
    _history(manager, "suite", "cancelled", at(19), None, at(18),
             [{"name": "build", "status": "skipped", "summary": "Cancelled by operator"}])

    # Two days, not one: a one-day window opens at today's midnight, and in
    # the first hour of a day runs made twenty to sixty minutes ago were
    # yesterday's -- this failed every night between 00:00 and 01:00 UTC.
    assert manager.farm_statistics(2)["firmware"] == {
        "supplied": 1, "reused": 2, "build_failed": 1, "not_reached": 1,
    }


def test_rig_busy_is_the_time_a_job_held_the_lock_inside_the_window(tmp_path):
    """Clipped to the window, merged where jobs overlap, split across the days
    it fell on -- and a running job counts up to now. Two days, so the window
    opens at yesterday's midnight: at least a day before now, whatever the
    hour this runs."""
    from datetime import datetime, timedelta, timezone

    manager = _store_manager(tmp_path)
    manager.inventory_snapshot = lambda annotate=False: {"boards": []}
    now = datetime.now(timezone.utc)
    opens = datetime.fromisoformat(manager.farm_statistics(2)["window"]["from"])
    minute = timedelta(minutes=1)
    # Started an hour before the window opened: only its last half hour counts.
    _history(manager, "suite", "passed", opens - 60 * minute, opens - 60 * minute, opens + 30 * minute)
    # A build and a suite overlapping by ten minutes: fifty minutes, not sixty.
    begin = now - 180 * minute
    _history(manager, "build", "passed", begin, begin, begin + 30 * minute)
    _history(manager, "suite", "passed", begin + 20 * minute, begin + 20 * minute, begin + 50 * minute)

    stats = manager.farm_statistics(2)
    busy = stats["utilisation"]["busy_seconds"]
    assert abs(busy - 80 * 60) < 2, busy
    assert abs(sum(day["busy_seconds"] for day in stats["per_day"]) - busy) < 2, "every second lands on a day"

    running = manager.store.create("suite", {"profile": "painlessmesh"}, manager.state / "y.log")
    manager.store.update(running["id"], "running")
    with manager.store.connect() as db:
        db.execute("UPDATE jobs SET created_at=?, started_at=? WHERE id=?",
                   ((now - 2 * minute).isoformat(), (now - 2 * minute).isoformat(), running["id"]))
    later = manager.farm_statistics(2)
    assert later["totals"]["active"] == 1
    assert later["utilisation"]["busy_seconds"] >= busy + 110, "a running job counts up to now"


def test_a_day_is_the_operators_day(tmp_path):
    """Buckets follow the caller's time zone: a run at 02:00 UTC is yesterday
    evening in Quebec, and "yesterday" on the dashboard is the operator's."""
    from datetime import datetime, timedelta, timezone

    manager = _store_manager(tmp_path)
    manager.inventory_snapshot = lambda annotate=False: {"boards": []}
    now = datetime.now(timezone.utc)
    today_utc = now.replace(hour=2, minute=0, second=0, microsecond=0)
    if today_utc > now:
        today_utc -= timedelta(days=1)
    _history(manager, "suite", "passed", today_utc, today_utc, today_utc + timedelta(minutes=5))
    in_utc = {day["date"]: day["passed"] for day in manager.farm_statistics(3, 0)["per_day"]}
    in_quebec = {day["date"]: day["passed"] for day in manager.farm_statistics(3, -240)["per_day"]}
    assert in_utc[today_utc.date().isoformat()] == 1
    assert in_quebec[(today_utc.date() - timedelta(days=1)).isoformat()] == 1


def test_the_statistics_route_answers_and_refuses_what_it_cannot_count(tmp_path):
    manager = _store_manager(tmp_path)
    manager.inventory_snapshot = lambda annotate=False: {"boards": []}
    source = Path(core_service.__file__).read_text(encoding="utf-8")
    assert 'if path == "/api/v1/stats":' in source
    assert manager.farm_statistics(90)["window"]["days"] == 90


def test_the_library_is_the_store_by_project_and_branch_newest_first(tmp_path):
    """What an operator asks of a growing store: the latest build of each
    branch, whether a run could flash it, the last run on it, and how much is
    older builds nothing keeps."""
    manager = _store_manager(tmp_path)
    old = _run(manager, resolved_sha="4" * 40, branch="main", created_at="2026-09-01T00:00:00+00:00")
    _bundle(tmp_path / "artifacts", old, "4" * 40)
    pinned = _run(manager, resolved_sha="5" * 40, branch="main", created_at="2026-09-02T00:00:00+00:00")
    _bundle(tmp_path / "artifacts", pinned, "5" * 40)
    manager.pin_artifact(pinned, "release gate")
    newest = _run(manager, resolved_sha="6" * 40, branch="main", created_at="2026-09-03T00:00:00+00:00")
    _bundle(tmp_path / "artifacts", newest, "6" * 40, ("esp32", "esp32-c3"))
    failed = _reuse(manager, newest, status="failed", created_at="2026-09-04T00:00:00+00:00")
    topic = _run(manager, resolved_sha="7" * 40, branch="fix/c5", created_at="2026-09-05T00:00:00+00:00")
    _bundle(tmp_path / "artifacts", topic, "7" * 40)

    library = manager.artifact_library()
    assert library["count"] == 4 and library["pinned"] == 1
    (project,) = library["projects"]
    assert project["profile"] == "painlessmesh" and project["bundles"] == 4
    assert [group["branch"] for group in project["branches"]] == ["fix/c5", "main"], "newest branch first"
    main = project["branches"][1]
    assert main["bundles"] == 3 and main["latest"]["id"] == newest
    assert main["latest"]["families"] == ["esp32", "esp32-c3"] and main["latest"]["runs"] == 2
    assert main["last_run"]["id"] == failed and main["last_run"]["status"] == "failed"
    # Older and neither pinned nor held: what a prune could take.
    assert main["older"] == 1 and main["older_bytes"] == next(
        entry["bytes"] for entry in manager.artifact_index()["bundles"] if entry["id"] == old)
    assert library["older"] == 1 and library["pruning"] == manager.prune_progress()
    # The test bundles declare another HIL agent: none could be flashed now.
    assert main["runnable"] is None
    # The flat list, one branch of it.
    listed = manager.artifact_index(branch="main")
    assert listed["matched"] == 3 and {entry["id"] for entry in listed["bundles"]} == {old, pinned, newest}


def test_a_session_lookup_advances_last_seen_at_most_once_a_minute(tmp_path):
    """Every authenticated request looks its session up, and a browser polls
    the live dashboard every few seconds. last_seen_at is advanced at most once
    a minute, so a burst of those lookups is not a burst of write locks on the
    session and account rows -- connect() opens a fresh connection with no WAL,
    so each such write would serialize against every reader.
    """
    from datetime import datetime, timedelta

    store = farm_service.JobStore(tmp_path / "farm.db")
    account = store.create_account("poller", role="user")
    digest = "d" * 64
    store.create_session(digest, account["id"], "2999-01-01T00:00:00+00:00", None)

    base = datetime.fromisoformat(account["last_seen_at"])

    def account_seen():
        return store.account("id", account["id"])["last_seen_at"]

    def session_seen():
        with store.connect() as db:
            return db.execute("SELECT last_seen_at FROM sessions WHERE digest=?", (digest,)).fetchone()[0]

    at_start = account_seen()
    session_at_start = session_seen()

    # A burst of lookups a few seconds apart, all inside the window: not one of
    # them writes, so both rows still say what they said at the start.
    for offset in (1, 5, 30, 59):
        found = store.session_account(digest, (base + timedelta(seconds=offset)).isoformat())
        assert found["handle"] == "poller"
    assert account_seen() == at_start and session_seen() == session_at_start

    # A lookup past the window advances both rows, once.
    past = (base + timedelta(seconds=90)).isoformat()
    assert store.session_account(digest, past)["handle"] == "poller"
    assert account_seen() == past and session_seen() == past

    # And the lookups that follow within a minute of that leave them alone.
    for offset in (95, 120, 149):
        store.session_account(digest, (base + timedelta(seconds=offset)).isoformat())
    assert account_seen() == past and session_seen() == past


def test_a_half_adds_its_own_routes_and_the_service_answers_them(tmp_path):
    """`BaseManager.api_routes()`: what a half adds to the API.

    A half is a distribution of its own now, and the service is the core's --
    so a portal that had to edit the service to add a route would wait on a
    release of the rig to ship it (docs/public-release-plan.md, step 16a).
    It declares its routes instead, beside the methods that answer them.

    The service has none of its own to declare: the base returns nothing, and
    a farm with no half installed answers exactly what it always answered.
    """
    import json
    import re
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    manager = _store_manager(tmp_path)
    assert farm_service.BaseManager.api_routes(manager) == (), "the base adds nothing"

    asked = []

    def workspaces(identity=None):
        asked.append(("list", identity.name))
        return {"workspaces": ["firmware"]}

    def one(identity=None, name=None):
        asked.append(("one", name))
        if name == "gone":
            raise LookupError("no such workspace")
        return {"name": name}

    def make(body, identity=None):
        asked.append(("make", body.get("name")))
        if not body.get("name"):
            raise ValueError("a workspace needs a name")
        return {"created": body["name"]}

    manager.workspaces_list = workspaces
    manager.workspaces_one = one
    manager.workspaces_make = make
    manager.api_routes = lambda: (
        farm_service.ApiRoute("GET", re.compile(r"/api/v1/workspaces"), "account", "workspaces_list"),
        farm_service.ApiRoute("GET", re.compile(r"/api/v1/workspaces/(?P<name>[a-z]+)"), "account", "workspaces_one"),
        farm_service.ApiRoute("POST", re.compile(r"/api/v1/workspaces"), "account", "workspaces_make"),
    )

    token = "t" * 40
    server = ThreadingHTTPServer(("127.0.0.1", 0), farm_service.make_handler(manager, token, tmp_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {token}"}
        if data:
            headers["Content-Type"] = "application/json"
        def said(raw):
            # An unknown /api/ GET is served by the static handler, which
            # answers in HTML: the status is the whole answer there.
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None

        try:
            with urlopen(Request(base + path, data=data, method=method, headers=headers), timeout=5) as answer:
                return answer.status, said(answer.read())
        except HTTPError as error:
            return error.code, said(error.read())

    try:
        assert call("GET", "/api/v1/workspaces") == (200, {"workspaces": ["firmware"]})
        # A named group in the pattern is what the method is told.
        assert call("GET", "/api/v1/workspaces/firmware") == (200, {"name": "firmware"})
        # A write is given the body.
        assert call("POST", "/api/v1/workspaces", {"name": "bootloader"}) == (200, {"created": "bootloader"})
        # And what the method raises is the answer the caller gets, in the
        # service's own terms rather than a traceback and a 500.
        status, said = call("GET", "/api/v1/workspaces/gone")
        assert status == 404 and said == {"error": "no such workspace"}
        status, said = call("POST", "/api/v1/workspaces", {})
        assert status == 400 and said == {"error": "a workspace needs a name"}
        # A path no half declared is still not found. Only the read is asked
        # for: a POST to an unknown path is answered without its body being
        # read, so the connection closes on the unread bytes -- which the
        # service has always done and is not this to change.
        assert call("GET", "/api/v1/nothing")[0] == 404
        assert [kind for kind, _ in asked] == ["list", "one", "make", "one", "make"]

        # The service's own routes still win: a half cannot shadow one by
        # declaring the same path, because its routes are consulted after.
        manager.api_routes = lambda: (
            farm_service.ApiRoute("GET", re.compile(r"/api/v1/status"), "account", "workspaces_list"),
        )
        shadowing = ThreadingHTTPServer(("127.0.0.1", 0), farm_service.make_handler(manager, token, tmp_path))
        threading.Thread(target=shadowing.serve_forever, daemon=True).start()
        try:
            with urlopen(Request(f"http://127.0.0.1:{shadowing.server_address[1]}/api/v1/status",
                                 headers={"Authorization": f"Bearer {token}"}), timeout=5) as answer:
                assert "workspaces" not in json.loads(answer.read()), "the service answered its own route"
        finally:
            shadowing.shutdown()
    finally:
        server.shutdown()
