"""The farm runs firmware; it does not build it.

Building an ESP image is the business of the project that ships it: its
toolchain, its flags, its pipeline. The farm takes the bundle that pipeline
made -- verified on arrival, tied to the producer its profile names -- and
flashes and tests it. The simulation host is where this repository's own CI
builds; the Pi is four cores and an SD card holding the rig lock, and a minute
it spends compiling is a minute no project can touch a board.

The farm used to be able to build, one profile at a time, while the projects
moved their builds into their own CI. Every one of them has, and a farm that
can still build absorbs a broken hand-off by quietly compiling -- which is how
a painlessMesh run with no bundle once passed while building six families on
the rig. So the capability is gone, not merely unused, and this module keeps
it gone: a profile cannot ask for a build, the service takes no build job, the
host installs no toolchain, and a run with no bundle is refused at submit.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "rig"))
sys.path.insert(0, str(REPO / "core"))

from alteriom_hil.profiles import STAGE_PLACEHOLDERS, ProfileError, load_profiles, parse_profile  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "farm_service_artifacts_only", REPO / "runner" / "farm_service.py"
)
farm_service = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(farm_service)
# The service itself, where those names are read: it is
# `alteriom_hil.service` now and runner/farm_service.py is the launcher
# that composes the halves onto it, so a test changes it there.
from alteriom_hil import service as core_service  # noqa: E402


def profile_document(name: str) -> dict:
    return yaml.safe_load((REPO / "profiles" / f"{name}.yaml").read_text(encoding="utf-8"))


def test_a_profile_that_asks_the_farm_to_build_is_refused_where_it_is_written():
    """Refused, not ignored: a profile carrying a build command would read as
    though the farm compiled it, and the operator who wrote it would learn
    otherwise from a run refused for want of a bundle."""
    for field, value in (
        ("command", ["{python}", "suites/painlessmesh/build_artifacts.py"]),
        ("target_option", "--target"),
    ):
        document = profile_document("painlessmesh")
        document["build"][field] = value
        with pytest.raises(ProfileError) as refused:
            parse_profile(document, "painlessmesh.yaml")
        message = str(refused.value)
        assert f"build.{field} is not accepted" in message
        assert "does not build firmware" in message and "`supply:`" in message, "and what to do instead"

    # The revision key stays: it names the manifest field saying which commit
    # a bundle is for, which the farm reads whoever built it.
    spec = parse_profile(profile_document("painlessmesh"), "painlessmesh.yaml")
    assert spec.revision_key == "painlessmesh_sha"
    assert not hasattr(spec, "build_command") and not hasattr(spec, "builds_on_farm")
    assert "build" not in STAGE_PLACEHOLDERS, "no stage renders a build command"

    # A profile with no producer could never be given firmware at all.
    document = profile_document("painlessmesh")
    document.pop("supply")
    with pytest.raises(ProfileError, match="must declare a `supply:` producer"):
        parse_profile(document, "painlessmesh.yaml")

    # Every profile this repository ships names the workflow that builds it.
    shipped = load_profiles(REPO)
    assert {"canary", "painlessmesh", "alteriom-firmware"} <= set(shipped)
    assert all(spec.accepts_supplied_bundles for spec in shipped.values())


def _manager(tmp_path, reusable="real"):
    """A manager over copies of the shipped profiles."""
    repo = tmp_path / "repo"
    (repo / "profiles").mkdir(parents=True)
    for document in (REPO / "profiles").glob("*.yaml"):
        shutil.copy2(document, repo / "profiles" / document.name)
    manager = farm_service.FarmManager(
        repo=repo,
        state=tmp_path / "state",
        registry=tmp_path / "inventory.yaml",
        board_map=tmp_path / "board-map.active.yaml",
        python=Path("/usr/bin/python3"),
    )
    if reusable != "real":
        manager.reusable_artifacts = (
            lambda sha, targets, revision_key=None, profile=None: reusable
        )
    return manager


def test_the_service_takes_no_build_job(tmp_path):
    """Refused by name rather than as an unknown kind, so an old client learns
    what changed instead of what it got wrong."""
    manager = _manager(tmp_path, reusable=None)
    with pytest.raises(ValueError, match="does not build firmware"):
        manager.submit("build", {"profile": "canary", "targets": ["esp32-c6"]})
    with pytest.raises(ValueError, match="unsupported job kind"):
        manager._validate("build", {"profile": "canary", "targets": ["esp32-c6"]})
    source = (REPO / "core" / "alteriom_hil" / "service.py").read_text(encoding="utf-8")
    assert "HTTPStatus.GONE" in source.split('path == "/api/v1/builds"', 1)[1][:200]


def test_a_run_with_no_bundle_is_refused_at_submit_naming_the_workflow(tmp_path, monkeypatch):
    """It would queue, take the rig and fail with nothing to flash. The
    refusal names the workflow that builds and dispatches, because that is
    what the operator has to run instead."""
    manager = _manager(tmp_path, reusable=None)
    monkeypatch.setattr(core_service, "_reject_missing_ref", lambda ref, remote, project: "a" * 40)

    with pytest.raises(ValueError) as refused:
        manager._validate("suite", {"profile": "canary", "targets": ["esp32-c6"]})
    message = str(refused.value)
    assert "does not build firmware" in message
    assert "holds no canary bundle for aaaaaaaaaaaa" in message
    assert ".github/workflows/deploy-farm-host.yml" in message, "what to run instead"
    assert "esp32-c6" in message, "and for which family"


def test_a_run_that_supplies_or_names_a_held_bundle_is_taken(tmp_path, monkeypatch):
    """The two ways a run gets firmware, and neither compiles anything."""
    manager = _manager(tmp_path, reusable=("j" * 32, Path("artifacts/j")))
    monkeypatch.setattr(core_service, "_reject_missing_ref", lambda ref, remote, project: "a" * 40)

    # The farm holds a bundle for this commit.
    manager._validate("suite", {"profile": "canary", "targets": ["esp32-c6"]})
    # ... unless the run said not to take one, and then there is nothing.
    with pytest.raises(ValueError, match="does not build firmware"):
        manager._validate("suite", {"profile": "canary", "targets": ["esp32-c6"], "reuse": False})

    # Or the run names the bundle its CI handed over. The bundle's own checks
    # are _check_supplied_bundle's; what matters here is that it is enough.
    checked = []
    monkeypatch.setattr(
        farm_service.FarmManager, "_check_supplied_bundle",
        lambda self, bundle_id, spec, sha, targets: checked.append(bundle_id),
    )
    manager = _manager(tmp_path / "second", reusable=None)
    manager._validate("suite", {"profile": "canary", "targets": ["esp32-c6"], "artifact": "b" * 32})
    assert checked == ["b" * 32]


def _held_bundle(manager, profile: str | None, sha: str, families=("esp32-c6",), provenance=True,
                 agent: str | None = None) -> Path:
    bundle = manager.artifact_root / uuid.uuid4().hex
    targets = {}
    for family in families:
        (bundle / family).mkdir(parents=True)
        (bundle / family / "flash-image.bin").write_bytes(b"\xe9image")
        targets[family] = {"image": f"{family}/flash-image.bin"}
    manifest = {"schema": 2, "farm_sha": sha, "targets": targets}
    if agent is not None:
        manifest["hil_agent_sha"] = agent
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    if provenance:
        (bundle / "provenance.json").write_text(json.dumps({"kind": "supplied", "profile": profile}))
    return bundle


def test_a_held_bundle_is_any_bundle_the_store_holds_supplied_ones_included(tmp_path):
    """Once nothing builds on the rig, a bundle a farm build left is the one
    kind that never appears again. Looking only at those, a re-run of a
    supplied run -- the debugging iteration, which skips the flash when the
    boards already run the commit -- would be refused for want of a bundle
    the farm is holding."""
    manager = _manager(tmp_path)
    sha = "c" * 40
    older = _held_bundle(manager, "canary", sha)
    os.utime(older / "manifest.json", (1_600_000_000, 1_600_000_000))
    newer = _held_bundle(manager, "canary", sha)

    found = manager.reusable_artifacts(sha, ["esp32-c6"], "farm_sha", "canary")
    assert found == (newer.name, newer), "the newest bundle for the commit"
    assert manager.reusable_artifacts(sha.upper(), ["esp32-c6"], "farm_sha", "canary") == found
    assert manager.reusable_artifacts(sha, ["esp32-c6", "esp32"], "farm_sha", "canary") is None, "a family it lacks"
    assert manager.reusable_artifacts("d" * 40, ["esp32-c6"], "farm_sha", "canary") is None, "another commit"

    # Another profile's bundle at the same commit and key is not this one's,
    # and neither is a bundle that does not say whose it is.
    shutil.rmtree(older)
    shutil.rmtree(newer)
    _held_bundle(manager, "painlessmesh", sha)
    _held_bundle(manager, None, sha, provenance=False)
    assert manager.reusable_artifacts(sha, ["esp32-c6"], "farm_sha", "canary") is None

    # A reuse link is a run's view of a bundle, not a bundle of its own.
    if os.name == "posix":
        mine = _held_bundle(manager, "canary", sha)
        link = manager.artifact_root / uuid.uuid4().hex
        os.symlink(mine, link, target_is_directory=True)
        os.utime(mine / "manifest.json", (1_600_000_000, 1_600_000_000))
        assert manager.reusable_artifacts(sha, ["esp32-c6"], "farm_sha", "canary") == (mine.name, mine)


def test_the_listing_says_which_bundles_a_run_could_flash_today(tmp_path):
    """A bundle built against another HIL agent is refused when a run names
    it. The run form offers bundles from the listing, so the listing says
    which those are rather than letting the form offer a refusal."""
    manager = _manager(tmp_path)
    current = _held_bundle(manager, "painlessmesh", "c" * 40, agent=manager.agent_source_sha("painlessmesh"))
    older = _held_bundle(manager, "painlessmesh", "c" * 40, agent="0" * 64)
    agentless = _held_bundle(manager, "painlessmesh", "c" * 40)
    # The health check's profile names no agent, so whatever digest a bundle
    # of it carries is not painlessMesh's to be compared with.
    another_projects = _held_bundle(manager, "canary", "c" * 40, agent="0" * 64)
    by_id = {entry["id"]: entry for entry in manager.artifact_index()["bundles"]}
    assert by_id[current.name]["agent_current"] is True
    assert by_id[older.name]["agent_current"] is False
    assert by_id[agentless.name]["agent_current"] is True, "firmware with no agent is not held to one"
    assert by_id[another_projects.name]["agent_current"] is True, "a profile with no agent holds its bundles to none"


def test_a_bundle_that_went_between_submit_and_the_rig_fails_the_stage():
    """The submit check can be overtaken -- a prune between the two -- and
    then the firmware stage is where it has to be said, with no fallback."""
    source = (REPO / "rig" / "alteriom_hil" / "rig_manager.py").read_text(encoding="utf-8")
    stage = source.split("if not (reused_from or supplied):", 1)[1].split("manifest = json.loads", 1)[0]
    assert "No firmware bundle for this run" in stage
    assert "no longer on disk" in stage, "which is the only way to reach it"
    assert "spec.supply_workflow" in stage, "and what to run again"


def test_nothing_on_the_farm_host_can_build(tmp_path):
    """No toolchain installed, required, granted write access or measured;
    the flash script flashes."""
    setup = (REPO / "runner" / "setup-runner.sh").read_text(encoding="utf-8")
    assert "install --upgrade esptool" in setup and "platformio" not in setup
    assert "platformio" not in (REPO / "runner" / "verify-rig.sh").read_text(encoding="utf-8")
    assert ".platformio-cores" not in (REPO / "runner" / "install-health-service.sh").read_text(encoding="utf-8")

    # The launcher and the two halves, each where its distribution keeps it.
    for half in (REPO / "runner" / "farm_service.py",
                 REPO / "rig" / "alteriom_hil" / "rig_manager.py",
                 REPO / "portal" / "alteriom_hil" / "portal_manager.py"):
        service = half.read_text(encoding="utf-8")
        for gone in ("PLATFORMIO_CORES", "_build_env", "clear_platformio_cache", "/api/v1/storage/platformio", "build_command"):
            assert gone not in service, (half.name, gone)

    flash_all = (REPO / "suites" / "painlessmesh" / "flash_all.py").read_text(encoding="utf-8")
    assert "build_artifacts" not in flash_all and "--skip-build" not in flash_all
    assert "--skip-build" not in (REPO / "profiles" / "painlessmesh.yaml").read_text(encoding="utf-8")

    manager = _manager(tmp_path)
    panel = manager.storage(wait=True)
    assert {item["name"] for item in panel["categories"]} == {"artifacts", "runs", "logs", "workspaces", "database"}
    assert not {"rig_busy", "clearing", "last_clear"} & set(panel)
    with pytest.raises(KeyError):
        manager.storage_detail("platformio")


def test_the_dashboard_starts_a_run_from_a_bundle_the_farm_holds():
    """A form that took a ref would send runs the service refuses, and the
    refusal would read as the farm being broken. It offers what the farm can
    run: the bundles it holds for the chosen profile."""
    script = (REPO / "rig" / "web" / "app.js").read_text(encoding="utf-8")
    page = (REPO / "rig" / "web" / "index.html").read_text(encoding="utf-8")
    form = page.split('id="suite-form"', 1)[1].split("</form>", 1)[0]
    assert 'id="bundle-select"' in form
    assert 'name="ref"' not in form and 'name="reuse"' not in form
    assert "Flash this bundle and test" in form and "Build" not in form
    assert "/api/v1/artifacts?profile=${encodeURIComponent(profile)}" in script
    assert "artifact: bundle.id" in script and "ref: bundle.revision" in script
    # A bundle's page is where one is found, so it runs from there.
    assert "Run with this bundle" in script and "function runWithBundle" in script
    # What the service would refuse is not offered: a bundle for an older agent.
    assert "bundle.agent_current !== false" in script
    # A bundle's page opened from a link renders before the profiles arrive,
    # and whether it can run depends on them: it is drawn again once they do.
    assert "if (!hadProfiles && bundlePage.entry) renderBundlePage();" in script
    for gone in ("builds_on_farm", "cacheNote", "platformio", "cache-clear", "Build once"):
        assert gone not in script, gone
