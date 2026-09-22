"""How the service behaves once the profile, not the code, decides what runs.

These cover the seams the profile refactor introduced. The existing
test_farm_service.py suite guards painlessMesh's behaviour; this one guards the
parts that only matter once a *second* consumer exists -- above all that the
two cannot be confused for each other.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

# The launcher: `alteriom_hil.launcher`, a console script now
# (`alteriom-hil-service`), which composes the halves installed onto the
# base and publishes what the service does
# (docs/public-release-plan.md, step 12e).
from alteriom_hil import launcher as farm_service

REPO = Path(__file__).resolve().parents[1]


class FakeLog:
    def __init__(self):
        self.text = ""

    def write(self, chunk):
        self.text += chunk

    def flush(self):
        pass


def manager_at(repo: Path):
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.repo = repo
    return manager


def repo_with_profiles(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "profiles").mkdir(parents=True)
    for document in (REPO / "profiles").glob("*.yaml"):
        shutil.copy2(document, repo / "profiles" / document.name)
    return repo


# ---- workspace ----


def test_a_farm_profile_runs_in_the_farm_checkout(tmp_path):
    """painlessMesh must not start cloning anything: its suite is right here,
    and a clone would be both slower and a different tree."""
    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    spec = manager.profiles["painlessmesh"]
    calls = []
    manager._run = lambda *a, **k: calls.append(a)

    workspace = manager._workspace(spec, "main", "job1", FakeLog())

    assert workspace == manager.repo
    assert calls == [], "a farm profile should run no git commands"


def test_a_consumer_profile_is_cloned_per_job_at_the_requested_ref(tmp_path):
    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    # Isolate the credential: on a provisioned farm host the real token file
    # exists, and the clone URL is then rewritten to carry a username. Without
    # this the test passes or fails depending on the machine it runs on.
    manager.CONSUMER_TOKEN_PATH = tmp_path / "absent-token"
    spec = manager.profiles["alteriom-firmware"]
    calls = []
    manager._run = lambda args, *a, **k: calls.append(args)

    log = FakeLog()
    workspace = manager._workspace(spec, "abc123", "job2", log)

    assert workspace == manager.state / "workspaces" / "job2"
    assert workspace != manager.repo, "a consumer must never build in the farm checkout"
    # Initialised, then the ref fetched one commit deep, then FETCH_HEAD
    # checked out: the only shape that takes a branch, a tag or a SHA alike.
    fetch = next(c for c in calls if "fetch" in c)
    assert fetch[-3:] == ["--depth", "1", "origin"] or fetch[-4:-1] == ["--depth", "1", "origin"]
    assert fetch[-1] == "abc123"
    assert any(spec.repo in c for c in calls if "remote" in c), "the remote must be the profile's repository"
    assert any("FETCH_HEAD" in c for c in calls if "checkout" in c)


def test_two_jobs_do_not_share_a_consumer_workspace(tmp_path):
    """Otherwise a second job silently tests whatever the first left behind."""
    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    manager.CONSUMER_TOKEN_PATH = tmp_path / "absent-token"
    spec = manager.profiles["alteriom-firmware"]
    manager._run = lambda *a, **k: None

    first = manager._workspace(spec, "main", "job-a", FakeLog())
    second = manager._workspace(spec, "main", "job-b", FakeLog())
    assert first != second


def test_a_stale_workspace_is_removed_before_reuse(tmp_path):
    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    manager.CONSUMER_TOKEN_PATH = tmp_path / "absent-token"
    spec = manager.profiles["alteriom-firmware"]
    manager._run = lambda *a, **k: None

    stale = manager.state / "workspaces" / "job-c"
    stale.mkdir(parents=True)
    (stale / "leftover.txt").write_text("from a previous run", encoding="utf-8")

    manager._workspace(spec, "main", "job-c", FakeLog())
    assert not (stale / "leftover.txt").exists()


# ---- selection and reuse ----


def test_the_suite_directory_follows_the_profile(tmp_path):
    manager = manager_at(repo_with_profiles(tmp_path))
    consumer = manager.profiles["alteriom-firmware"]
    painless = manager.profiles["painlessmesh"]

    assert manager.pytest_selection([], "", consumer.suite_path) == ["tests/hil"]
    assert manager.pytest_selection([], "", painless.suite_path) == [
        "suites/painlessmesh/tests"
    ]
    assert manager.pytest_selection(["test_boot.py"], "smoke", consumer.suite_path) == [
        "tests/hil/test_boot.py",
        "-k",
        "smoke",
    ]


def test_artifacts_are_not_reused_across_profiles(tmp_path, monkeypatch):
    """Two projects can legitimately share a commit SHA -- a fork, a
    submodule bump, coincidence. Reusing one's images for the other would
    flash the wrong firmware and report it as a pass."""
    import json

    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    manager.store = farm_service.JobStore(tmp_path / "farm.db")
    monkeypatch.setattr(type(manager), "agent_source_sha", lambda self, profile=None: "agent-sha")

    sha = "d" * 40
    job = manager.store.create("build", {"ref": sha}, tmp_path / "b.log")
    artifacts = manager.state / "artifacts" / job["id"]
    (artifacts / "esp32").mkdir(parents=True)
    (artifacts / "esp32" / "flash-image.bin").write_bytes(b"x")
    (artifacts / "manifest.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "painlessmesh_sha": sha,
                "hil_agent_sha": "agent-sha",
                "targets": {"esp32": {"image": "esp32/flash-image.bin"}},
            }
        ),
        encoding="utf-8",
    )

    assert (
        manager.reusable_artifacts(sha, ["esp32"], "painlessmesh_sha", "painlessmesh")
        is not None
    )
    # A different profile names a different key, so this build is not a match.
    assert manager.reusable_artifacts(sha, ["esp32"], "git_sha", "painlessmesh") is None
    # ...and neither is one that shares the key but belongs to another project.
    assert (
        manager.reusable_artifacts(sha, ["esp32"], "painlessmesh_sha", "alteriom-firmware")
        is None
    )


def test_artifacts_are_not_reused_across_profiles_sharing_a_revision_key(tmp_path, monkeypatch):
    """The revision key alone is not enough to tell two projects apart.

    `git_sha` is the obvious key for any project, so two profiles can name the
    same one and legitimately reference the same commit -- a fork, a submodule
    bump, two builds of one repository. Matching on the manifest alone would
    hand one profile the other's images and flash the wrong firmware under a
    passing report.
    """
    import json

    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    manager.store = farm_service.JobStore(tmp_path / "farm.db")
    monkeypatch.setattr(type(manager), "agent_source_sha", lambda self, profile=None: "agent-sha")

    sha = "e" * 40
    job = manager.store.create("build", {"ref": sha, "profile": "project-a"}, tmp_path / "a.log")
    artifacts = manager.state / "artifacts" / job["id"]
    (artifacts / "esp32").mkdir(parents=True)
    (artifacts / "esp32" / "flash-image.bin").write_bytes(b"x")
    (artifacts / "manifest.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "git_sha": sha,
                "targets": {"esp32": {"image": "esp32/flash-image.bin"}},
            }
        ),
        encoding="utf-8",
    )

    # Same key, same commit, same families -- and still not reusable, because a
    # different project built it.
    assert manager.reusable_artifacts(sha, ["esp32"], "git_sha", "project-b") is None
    assert manager.reusable_artifacts(sha, ["esp32"], "git_sha", "project-a") is not None


def test_a_job_predating_profiles_counts_as_painlessmesh(tmp_path, monkeypatch):
    """Its request carries no profile field; it must stay reusable for the
    consumer it was actually built for, and for no one else."""
    import json

    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    manager.store = farm_service.JobStore(tmp_path / "farm.db")
    monkeypatch.setattr(type(manager), "agent_source_sha", lambda self, profile=None: "agent-sha")

    sha = "f" * 40
    job = manager.store.create("build", {"ref": sha}, tmp_path / "b.log")
    artifacts = manager.state / "artifacts" / job["id"]
    (artifacts / "esp32").mkdir(parents=True)
    (artifacts / "esp32" / "flash-image.bin").write_bytes(b"x")
    (artifacts / "manifest.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "painlessmesh_sha": sha,
                "hil_agent_sha": "agent-sha",
                "targets": {"esp32": {"image": "esp32/flash-image.bin"}},
            }
        ),
        encoding="utf-8",
    )

    assert (
        manager.reusable_artifacts(sha, ["esp32"], "painlessmesh_sha", "painlessmesh")
        is not None
    )
    assert (
        manager.reusable_artifacts(sha, ["esp32"], "painlessmesh_sha", "alteriom-firmware")
        is None
    )


# ---- preflight ----


def test_a_profile_without_preflight_runs_no_preflight_command(tmp_path):
    """preflight_cmd is [] for such a profile, and Popen([]) raises IndexError.

    Reaching _run() at all would fail every consumer suite immediately after a
    successful flash, before pytest was ever started.
    """
    manager = manager_at(repo_with_profiles(tmp_path))
    spec = manager.profiles["alteriom-firmware"]
    assert spec.has_preflight is False
    # The command is empty, which is exactly why it must never reach Popen.
    assert list(spec.preflight_command) == []


def test_the_configured_preflight_timeout_is_used(tmp_path):
    """A profile setting a timeout other than the default must be honoured;
    a hardcoded one either cuts the check short or holds the rig too long."""
    doc = {
        "schema": 1,
        "name": "example",
        "source": {"location": "farm", "repo": "https://example.invalid/x.git"},
        "build": {"revision_key": "k"},
        "supply": {"repo": "https://example.invalid/x.git", "workflow": ".github/workflows/b.yml"},
        "flash": {"command": ["{python}", "f.py"]},
        "preflight": {"command": ["{python}", "p.py"], "timeout_seconds": 42},
        "suite": {"path": "suites/example/tests"},
    }
    from alteriom_hil import profiles as profile_module

    spec = profile_module.parse_profile(doc, "test")
    assert spec.preflight_timeout == 42
    assert manager_at(repo_with_profiles(tmp_path)).profiles["painlessmesh"].preflight_timeout == 180


# ---- validation ----


def test_an_unknown_profile_is_refused_at_submit(tmp_path):
    manager = manager_at(repo_with_profiles(tmp_path))
    with pytest.raises(ValueError, match="unsupported validation profile"):
        manager._validate("suite", {"profile": "not-a-project", "ref": "main"})


def test_test_selection_is_not_checked_for_a_consumer_suite(tmp_path):
    """Its files arrive with the checkout, so the names cannot be verified at
    submit; refusing here would refuse every valid selection."""
    manager = manager_at(repo_with_profiles(tmp_path))
    # Where the firmware comes from is not the question here. The farm does
    # not build, so `_validate` also asks whether a bundle exists for
    # this commit -- and with an unresolved ref it does not even reach the
    # lookup, so stubbing the answer is not enough: the check itself goes.
    manager._check_images_exist = lambda *args, **kwargs: None
    manager._validate(
        "suite",
        {
            "profile": "alteriom-firmware",
            "ref": "main",
            "targets": ["esp32"],
            "tests": ["test_boot_and_console.py"],
        },
    )


def test_repositories_lists_every_profile_remote(tmp_path):
    manager = manager_at(repo_with_profiles(tmp_path))
    found = farm_service.repositories(manager.repo, manager.profiles)
    assert found["profiles"]["alteriom-firmware"].endswith("/alteriom-firmware")
    assert found["profiles"]["painlessmesh"].endswith("/painlessMesh")
    # Every project by its profile's name and none by its own: the dashboard
    # used to read painlessMesh's from a key of its own.
    assert set(found) == {"farm", "profiles"}


def test_the_default_profile_is_the_farms_to_say(tmp_path, monkeypatch):
    """Which project a rig mostly serves is a fact about the rig. What is
    asked without naming a profile is for that one; with nothing said it is
    what it always was."""
    manager = manager_at(repo_with_profiles(tmp_path))
    monkeypatch.delenv(farm_service.DEFAULT_PROFILE_ENV, raising=False)
    assert manager.default_profile == farm_service.DEFAULT_PROFILE

    monkeypatch.setenv(farm_service.DEFAULT_PROFILE_ENV, "canary")
    assert manager.default_profile == "canary"
    # A run that names no profile is the default one's, and says so from then
    # on: the stored job carries the name, so no later reader has to default.
    assert manager.expected_agent_sha() is None, "the health check has no agent"
    assert manager.suite_catalogue() == manager.suite_catalogue("canary")

    # A name that is not a profile is not a default; the service refuses to
    # start on one (below), and a manager that is already up falls back.
    monkeypatch.setenv(farm_service.DEFAULT_PROFILE_ENV, "no-such-project")
    assert manager.default_profile == farm_service.DEFAULT_PROFILE


def test_a_default_profile_that_is_not_a_profile_stops_the_service_starting(tmp_path, monkeypatch):
    repo = repo_with_profiles(tmp_path)
    monkeypatch.setenv(farm_service.DEFAULT_PROFILE_ENV, "no-such-project")
    with pytest.raises(ValueError, match="no-such-project"):
        farm_service.FarmManager(repo, tmp_path / "state", tmp_path / "inventory.yaml",
                                 tmp_path / "board-map.yaml", Path("python"))


def test_a_catalogue_is_a_profiles_and_a_consumers_suite_has_none_here(tmp_path, monkeypatch):
    manager = manager_at(REPO)
    monkeypatch.delenv(farm_service.DEFAULT_PROFILE_ENV, raising=False)
    mesh = {entry["file"] for entry in manager.suite_catalogue("painlessmesh")}
    health = {entry["file"] for entry in manager.suite_catalogue("canary")}
    assert mesh and health and not mesh & health, "each profile's own suite"
    assert manager.suite_catalogue() == manager.suite_catalogue("painlessmesh")
    # Its suite arrives with its checkout; there is nothing here to read.
    assert manager.suite_catalogue("alteriom-firmware") == []
    assert manager.suite_catalogue("no-such-project") == []


def test_simulator_evidence_names_its_commit_by_the_profiles_revision_key():
    """The evidence and the bundle say which commit under one name, the
    profile's, and the firmware stage compares them. It was painlessMesh's
    key, whoever's run it was."""
    stage = {"status": "passed", "summary": "ok"}
    evidence = {"schema": 1, "git_sha": "a" * 40, "protocol_sim": stage,
                "mesh_sim": {**stage, "simulator_sha": "b" * 40, "scenarios": ["one"]}}
    farm_service.FarmManager._validate_simulation(evidence, "git_sha")
    with pytest.raises(ValueError, match="unknown fields"):
        farm_service.FarmManager._validate_simulation(evidence, "painlessmesh_sha")
    with pytest.raises(ValueError, match="git_sha is invalid"):
        farm_service.FarmManager._validate_simulation({**evidence, "git_sha": "main"}, "git_sha")


# ---- board-map scoping ----


def _active_map(path: Path):
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                # The rig as it stands: every family the profiles can ask
                # for, plus one they do not, so "only what it asked for"
                # is a real assertion. This held four boards and broke the
                # moment the profile asked for a C6 and a C5 it did not have.
                "boards": [
                    {"id": "esp32-s3-01", "target": "esp32-s3", "port": "/dev/a"},
                    {"id": "esp32-03", "target": "esp32", "port": "/dev/b"},
                    {"id": "esp32-c3-02", "target": "esp32-c3", "port": "/dev/c"},
                    {"id": "esp32-04", "target": "esp32", "port": "/dev/d"},
                    {"id": "esp32-c6-14b4", "target": "esp32-c6", "port": "/dev/e"},
                    {"id": "esp32-c5-1c10", "target": "esp32-c5", "port": "/dev/f"},
                    {"id": "esp8266-01", "target": "esp8266", "port": "/dev/g"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_an_exclusive_profile_gets_the_whole_bank_unchanged(tmp_path):
    """painlessMesh forms one mesh from every board; narrowing the map would
    break the suite it is there to run."""
    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    manager.board_map = _active_map(tmp_path / "board-map.active.yaml")

    chosen = manager._scoped_board_map(manager.profiles["painlessmesh"], "job1", FakeLog())
    assert chosen == manager.board_map


def test_a_shared_profile_gets_only_the_boards_it_asked_for(tmp_path):
    """The whole point of the one-board profile: an unscoped run would demand
    artifacts for every connected family and flash the entire rig."""
    import yaml

    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    manager.board_map = _active_map(tmp_path / "board-map.active.yaml")

    spec = manager.profiles["alteriom-firmware"]
    scoped = manager._scoped_board_map(spec, "job2", FakeLog())
    assert scoped != manager.board_map
    boards = yaml.safe_load(scoped.read_text(encoding="utf-8"))["boards"]
    wanted = sum(need["count"] for need in spec.needs)
    assert len(boards) == wanted, f"expected {wanted} board(s), got {len(boards)}"
    # The families the profile asked for, read from the profile: this used to
    # say {"esp32"}, which pinned the profile's content rather than the
    # scoping rule, and broke the moment the profile asked for a C3 and an S3.
    assert {b["target"] for b in boards} == {need["target"] for need in spec.needs}, "only what it asked for"
    # Distinct boards: allocating the same one twice would look like two nodes
    # and mesh with itself, which is not a network.
    assert len({b["id"] for b in boards}) == len(boards)


def test_a_family_the_profile_marks_optional_may_be_missing_from_the_rig(tmp_path):
    """The S3 is off the rig and the run covers the five families that are on
    it, instead of failing at discover and covering none."""
    import yaml
    from alteriom_hil import allocation

    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    path = tmp_path / "board-map.active.yaml"
    full = yaml.safe_load(_active_map(tmp_path / "full.yaml").read_text(encoding="utf-8"))
    path.write_text(
        yaml.safe_dump({"boards": [b for b in full["boards"] if b["target"] != "esp32-s3"]}),
        encoding="utf-8",
    )
    manager.board_map = path

    spec = manager.profiles["alteriom-firmware"]
    log = FakeLog()
    scoped = manager._scoped_board_map(spec, "job-s3-away", log)
    boards = yaml.safe_load(scoped.read_text(encoding="utf-8"))["boards"]
    assert "esp32-s3" not in {b["target"] for b in boards}
    assert {b["target"] for b in boards} == {"esp32", "esp32-c3", "esp32-c6", "esp32-c5"}
    assert len(boards) == sum(
        need["count"] for need in spec.needs if need["target"] != "esp32-s3"
    )
    assert "Not covered: esp32-s3" in log.text, "the run log says what it went without"
    # And the run can state it: this is what reaches the discover stage, the
    # result and the report, so a pass is never read as the whole profile.
    assert allocation.coverage(spec.needs, boards) == [
        {"target": "esp32-s3", "wanted": 1, "got": 0, "optional": True}
    ]


def test_scoping_fails_loudly_when_the_rig_cannot_satisfy_the_profile(tmp_path):
    import yaml

    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    path = tmp_path / "board-map.active.yaml"
    path.write_text(
        yaml.safe_dump({"boards": [{"id": "esp32-c3-02", "target": "esp32-c3"}]}),
        encoding="utf-8",
    )
    manager.board_map = path

    with pytest.raises(farm_service.PipelineError, match="Not enough boards"):
        manager._scoped_board_map(manager.profiles["alteriom-firmware"], "job3", FakeLog())


# ---- workspace cleanup ----


def test_a_consumer_workspace_is_removed_when_the_job_ends(tmp_path):
    """Otherwise every consumer run leaves a repository behind for good, and
    the first symptom is an unrelated job failing to write."""
    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    workspace = manager.state / "workspaces" / "job4"
    workspace.mkdir(parents=True)
    (workspace / "src.c").write_text("x", encoding="utf-8")

    manager._discard_workspace("job4")
    assert not workspace.exists()


def test_discarding_a_workspace_that_was_never_made_is_harmless(tmp_path):
    """A farm profile never creates one, and cleanup runs for every job."""
    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    manager._discard_workspace("never-existed")


# ---- the revision under test must be the one the build reported ----


def test_a_manifest_without_the_profiles_revision_key_is_a_build_failure(tmp_path):
    """No fallback to the checkout's HEAD.

    For a farm-located profile the workspace is the farm repository, so
    substituting HEAD would record and gate a painlessMesh build under an
    unrelated farm commit. A misconfigured key must fail the run rather than
    quietly rename the thing under test.
    """
    manager = manager_at(repo_with_profiles(tmp_path))
    spec = manager.profiles["painlessmesh"]
    # The helper that used to provide the fallback is gone; nothing should
    # reintroduce it without also reintroducing that failure.
    assert not hasattr(manager, "_consumer_revision")
    assert spec.revision_key == "painlessmesh_sha"


def test_each_stage_supplies_every_placeholder_it_advertises():
    """The loader's per-stage vocabulary and the pipeline's renders must agree.

    A stage that accepts a placeholder it cannot substitute lets a profile pass
    startup validation and then raise ProfileError mid-run -- possibly after
    flashing and testing, with the rig held. Asserting the two sides against
    each other means widening one without the other fails here instead.
    """
    import ast
    import inspect
    import textwrap

    from alteriom_hil.profiles import STAGE_PLACEHOLDERS

    tree = ast.parse(textwrap.dedent(inspect.getsource(farm_service.FarmManager._execute)))
    # Which render call belongs to which stage, by what it renders.
    by_first_arg = {
        "flash_command": "flash",
        "preflight_command": "preflight",
        "test_command": "test",
        "report_title": "report",
        "value": "env",  # the env dict comprehension renders each value
    }
    seen: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "render"
            and node.args
        ):
            continue
        first = node.args[0]
        name = first.attr if isinstance(first, ast.Attribute) else getattr(first, "id", None)
        stage = by_first_arg.get(name)
        if stage:
            seen[stage] = {kw.arg for kw in node.keywords}

    assert set(seen) == set(STAGE_PLACEHOLDERS), (
        f"stages rendered {sorted(seen)} but declared {sorted(STAGE_PLACEHOLDERS)}"
    )
    for stage, declared in STAGE_PLACEHOLDERS.items():
        missing = declared - seen[stage]
        assert not missing, f"{stage} accepts {sorted(missing)} but does not supply them"


# ---- what the dashboard is told, and what it must not assume ----


def test_the_service_reports_enough_to_build_a_profile_picker(tmp_path):
    """Names alone are not enough to *offer* a profile, only to name one.

    The dashboard needs a label to show, a default ref to hint, and a source
    link per profile. Without them the picker has to hardcode its options,
    which is the thing profiles exist to stop.
    """
    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    manager.registry = tmp_path / "inventory.yaml"
    manager.board_map = tmp_path / "board-map.active.yaml"
    manager.python = Path("/usr/bin/python3")

    build = manager.configuration()["build"]
    # Every profile this repository ships, whatever it ships: a picker built
    # from a list in a test is the thing profiles exist to stop.
    shipped = sorted(path.stem for path in (REPO / "profiles").glob("*.yaml"))
    assert sorted(build["profiles"]) == shipped
    assert {"painlessmesh", "alteriom-firmware", "canary"} <= set(shipped)

    details = build["profile_details"]
    for name in shipped:
        entry = details[name]
        assert entry["label"], f"{name} has no label to show"
        assert entry["default_ref"], f"{name} has no default ref to offer"
        assert entry["repo"].startswith("https://"), f"{name} has no browsable source link"
        assert entry["suite_path"], f"{name} has no suite path"
        # Where its firmware comes from: the farm builds none of it.
        assert entry["supply_workflow"].startswith(".github/workflows/"), f"{name} names no producer"
        assert entry["supply_repo"].startswith("https://"), f"{name} names no producer repository"
        assert "builds_on_farm" not in entry
    assert details["painlessmesh"]["exclusive"] is True
    assert details["alteriom-firmware"]["exclusive"] is False
    # Which families a shared profile asks for, and which of them it will run
    # without. "3 board(s)" said how many and never which, so a family marked
    # optional -- a board off the rig on purpose -- was visible only in the
    # YAML on the host.
    needs = {need["target"]: need for need in details["alteriom-firmware"]["needs"]}
    assert needs["esp32"]["count"] == 2
    assert needs["esp32-s3"]["optional"] is True
    assert "optional" not in needs["esp32-c6"]
    assert details["painlessmesh"]["needs"] == [], "a whole-bank profile asks for no family in particular"


def test_the_dashboard_does_not_hardcode_a_profile_or_a_ref():
    """The run form drifted behind the abstraction once already: its profile
    picker listed painlessMesh as the only option and its ref field defaulted
    to a branch that had since been released, so a second consumer could not be
    selected and the default pointed at superseded code. Both must come from
    the service."""
    web = REPO / "rig" / "web"
    page = (web / "index.html").read_text(encoding="utf-8")
    script = (web / "app.js").read_text(encoding="utf-8")

    assert 'value="painlessmesh"' not in page, "the profile picker hardcodes an option"
    assert "Feat/next-release" not in page, "the ref field hardcodes a branch"
    assert 'id="profile-select"' in page, "the picker must be populated at runtime"
    assert "data.profiles" in script, "the page never reads the service's profile list"


# ---- cloning a private consumer ----


def test_a_public_consumer_clones_with_no_credential(tmp_path):
    """No token file means no credential, which is right for a public repo."""
    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    manager.state.mkdir(parents=True, exist_ok=True)
    spec = manager.profiles["alteriom-firmware"]
    manager.CONSUMER_TOKEN_PATH = tmp_path / "absent-token"

    url, env = manager._clone_credentials(spec, FakeLog())
    assert url == spec.repo
    assert env is None


def test_a_private_consumer_clones_without_the_token_reaching_the_url(tmp_path):
    """The farm holds no credential for a private consumer by default.

    painlessMesh is public, so this never came up; alteriom-firmware is an
    internal repository and its clone failed with "could not read Username".
    The token must reach git, but not through the URL: that lands in the
    process table, the log, and git's own error messages.
    """
    manager = manager_at(repo_with_profiles(tmp_path))
    manager.state = tmp_path / "state"
    manager.state.mkdir(parents=True, exist_ok=True)
    spec = manager.profiles["alteriom-firmware"]
    token = tmp_path / "consumer-token"
    token.write_text("ghp_notarealtoken", encoding="utf-8")
    manager.CONSUMER_TOKEN_PATH = token

    log = FakeLog()
    url, env = manager._clone_credentials(spec, log)

    assert "ghp_notarealtoken" not in url, "the token must not reach the URL"
    assert "ghp_notarealtoken" not in log.text, "the token must not reach the log"
    assert url.startswith("https://x-access-token@"), "git needs a username to skip prompting"
    assert env["GIT_TERMINAL_PROMPT"] == "0", "a prompt would hang the run forever"

    askpass = Path(env["GIT_ASKPASS"])
    assert askpass.is_file()
    assert str(token) in askpass.read_text(encoding="utf-8")
    # Readable only by the service user; the helper names the token file.
    assert askpass.stat().st_mode & 0o077 == 0, "the askpass helper must not be world-readable"


# ---- the test stage belongs to the profile ----


CUSTOM_RUNNER = """
schema: 1
name: custom-runner
label: A suite that is not pytest
source:
  location: consumer
  repo: https://example.invalid/custom.git
build:
  revision_key: git_sha
supply:
  repo: https://example.invalid/custom.git
  workflow: .github/workflows/build.yml
flash:
  command: ["{python}", "{farm_runner}/flash_artifacts.py", "--artifacts", "{artifact_dir}"]
needs:
  - target: esp32
    count: 1
suite:
  path: hil
  exclusive: false
test:
  command: ["{python}", "hil/run.py", "--board-map", "{board_map}", "--junit", "{results}"]
"""


def test_a_profile_with_its_own_runner_refuses_test_selection(tmp_path):
    """tests and keyword are pytest's node ids and -k. A profile that runs its
    own command has nothing to hand them to, and running the whole suite in
    reply to a request for one test would report a pass for a selection that
    never ran. Refused at submit, before the ref is even resolved."""
    repo = repo_with_profiles(tmp_path)
    (repo / "profiles" / "custom-runner.yaml").write_text(CUSTOM_RUNNER, encoding="utf-8")
    manager = manager_at(repo)
    assert manager.profiles["custom-runner"].has_test_command

    with pytest.raises(ValueError, match="runs its own test command"):
        manager._validate("suite", {"profile": "custom-runner", "ref": "main", "tests": ["test_x.py"]})
    with pytest.raises(ValueError, match="runs its own test command"):
        manager._validate("suite", {"profile": "custom-runner", "ref": "main", "keyword": "boot"})


def test_the_shipped_profiles_still_use_the_default_runner(tmp_path):
    """Both consumers are pytest suites and must keep the plugin's records:
    a test command would replace them with JUnit's thinner ones."""
    manager = manager_at(repo_with_profiles(tmp_path))
    assert not manager.profiles["painlessmesh"].has_test_command
    assert not manager.profiles["alteriom-firmware"].has_test_command


def test_the_dashboard_describes_a_run_by_its_project_not_by_painlessmesh():
    """The runs table showed a revision and a result, the detail said
    "Reference pipeline", every commit linked to painlessMesh's repository,
    and every run opened with two painlessMesh simulator stages marked
    skipped. A run is described by its project, repository, branch, revision
    and targets, and simulation appears only when the run carried it."""
    web = REPO / "rig" / "web"
    page = (web / "index.html").read_text(encoding="utf-8")
    script = (web / "app.js").read_text(encoding="utf-8")

    for column in ("<th>Project</th>", "<th>Branch</th>", "<th>Revision</th>", "<th>Targets</th>"):
        assert column in page, column
    assert "Reference pipeline" not in script
    assert "painlessMesh simulator" not in script, "the simulation card names one consumer"
    assert "shaLink(sha, jobRepo(job))" in script, "a revision must link into its own repository"
    assert "No hosted simulator evidence" not in script


# ---- the checkout, against real git ----


def _consumer_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A local repository with a commit on main and a second on a branch.
    Configured the way GitHub is, to allow fetching an unadvertised commit."""
    import subprocess

    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    run = lambda *args, cwd=None: subprocess.run(["git", *args], cwd=cwd or work, check=True, capture_output=True, text=True)
    work.mkdir()
    run("init", "--quiet", "-b", "main")
    run("config", "user.email", "farm@example.invalid")
    run("config", "user.name", "farm")
    (work / "README").write_text("main", encoding="utf-8")
    run("add", "README"); run("commit", "--quiet", "-m", "main")
    run("checkout", "--quiet", "-b", "topic")
    (work / "README").write_text("topic", encoding="utf-8")
    run("commit", "--quiet", "-am", "topic")
    topic_sha = run("rev-parse", "HEAD").stdout.strip()
    run("checkout", "--quiet", "main")
    run("clone", "--quiet", "--bare", str(work), str(origin), cwd=tmp_path)
    run("config", "uploadpack.allowAnySHA1InWant", "true", cwd=origin)
    return origin, topic_sha, run("rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize("which", ["branch", "sha", "default"])
def test_a_consumer_is_checked_out_at_a_branch_a_sha_or_its_default(tmp_path, which):
    """Against real git, not a recorded command list: the previous shape passed
    its unit test and failed on the host for every branch but the default,
    because `git checkout --detach <name>` cannot resolve origin/<name>."""
    import subprocess

    origin, topic_sha, main_sha = _consumer_repo(tmp_path)
    repo = repo_with_profiles(tmp_path)
    manager = manager_at(repo)
    manager.state = tmp_path / "state"
    manager.CONSUMER_TOKEN_PATH = tmp_path / "absent-token"
    spec = manager.profiles["alteriom-firmware"]
    spec = type(spec)(**{**spec.__dict__, "repo": str(origin)})
    ref, expected = {"branch": ("topic", topic_sha), "sha": (topic_sha, topic_sha), "default": ("main", main_sha)}[which]

    with (tmp_path / "job.log").open("w+", encoding="utf-8") as log:
        workspace = manager._workspace(spec, ref, f"job-{which}", log)

    head = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    assert head == expected, (which, (tmp_path / "job.log").read_text(encoding="utf-8"))
    assert (workspace / "README").read_text(encoding="utf-8").strip() == ("main" if which == "default" else "topic")
