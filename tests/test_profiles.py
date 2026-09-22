"""Profile loading and validation.

The point of these is that a bad profile fails at *load*, loudly, naming the
file -- not twenty minutes into a run with a traceback that reads like a farm
fault. Every malformed-document test here exists because that distinction is
the whole value of validating profiles as data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from alteriom_hil import profiles

REPO = Path(__file__).resolve().parents[1]


def minimal(**overrides) -> dict:
    doc = {
        "schema": 1,
        "name": "example",
        "label": "Example validation",
        "source": {"location": "farm", "repo": "https://example.invalid/x.git"},
        # The farm builds nothing: a profile names the manifest field that
        # carries the commit, and the workflow that builds its bundles.
        "build": {"revision_key": "example_sha"},
        "supply": {"repo": "https://example.invalid/x.git", "workflow": ".github/workflows/build.yml"},
        # Required: an empty flash command reaches Popen([]) deep into a run.
        "flash": {"command": ["{python}", "flash.py", "--artifacts", "{artifact_dir}"]},
        "suite": {"path": "suites/example/tests"},
    }
    doc.update(overrides)
    return doc


# ---- the profiles this repository actually ships ----


def test_shipped_profiles_load():
    found = profiles.load_profiles(REPO)
    assert "painlessmesh" in found
    assert "alteriom-firmware" in found


def test_painlessmesh_profile_preserves_service_behaviour():
    """The pre-profile service's constants, now expressed as data.

    If one of these drifts, the production release gate changes behaviour
    silently, so they are pinned rather than left to the document.
    """
    p = profiles.load_profiles(REPO)["painlessmesh"]
    assert p.suite_path == "suites/painlessmesh/tests"
    assert p.revision_key == "painlessmesh_sha"
    assert p.suite_timeout == 1800
    assert p.min_boards == 2
    assert p.exclusive is True
    assert p.location == "farm"
    assert p.runs_in_consumer_repo is False
    assert p.has_preflight is True
    assert p.env["PAINLESSMESH_REF"] == "{revision}"


def test_alteriom_firmware_profile_runs_from_the_consumer_repo():
    p = profiles.load_profiles(REPO)["alteriom-firmware"]
    assert p.runs_in_consumer_repo is True
    assert p.suite_path == "tests/hil"
    # Two boards: the suite proves the nodes mesh, which one node cannot show.
    # min_boards and needs must agree, or a run is allocated fewer boards than
    # its floor demands and fails at discovery instead of at load.
    assert p.min_boards == 2
    assert sum(need["count"] for need in p.needs) >= p.min_boards
    # Stock product firmware cannot be asked what it runs, so every run flashes.
    assert p.has_preflight is False
    assert p.flash_command, "a consumer profile still needs something to flash it"


# ---- validation ----


def test_unknown_placeholder_is_refused_at_load():
    doc = minimal()
    doc["flash"]["command"] = ["{python}", "flash.py", "--artifacts", "{nowhere}"]
    with pytest.raises(profiles.ProfileError, match="unknown placeholder"):
        profiles.parse_profile(doc, "test")


def test_command_must_be_a_list_not_a_shell_string():
    doc = minimal()
    doc["flash"]["command"] = "python flash.py --artifacts /tmp/x"
    with pytest.raises(profiles.ProfileError, match="non-empty list"):
        profiles.parse_profile(doc, "test")


def test_wrong_schema_is_refused():
    with pytest.raises(profiles.ProfileError, match="unsupported profile schema"):
        profiles.parse_profile(minimal(schema=99), "test")


def test_suite_path_cannot_escape_the_workspace():
    for bad in ("/etc/passwd", "../../elsewhere/tests"):
        doc = minimal()
        doc["suite"]["path"] = bad
        with pytest.raises(profiles.ProfileError, match="relative path"):
            profiles.parse_profile(doc, "test")


def test_missing_revision_key_is_refused():
    doc = minimal()
    del doc["build"]["revision_key"]
    with pytest.raises(profiles.ProfileError, match="revision_key"):
        profiles.parse_profile(doc, "test")


def test_bad_location_is_refused():
    doc = minimal()
    doc["source"]["location"] = "somewhere-else"
    with pytest.raises(profiles.ProfileError, match="source.location"):
        profiles.parse_profile(doc, "test")


def test_filename_must_match_profile_name(tmp_path):
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "misnamed.yaml").write_text(
        yaml.safe_dump(minimal()), encoding="utf-8"
    )
    with pytest.raises(profiles.ProfileError, match="does not match its filename"):
        profiles.load_profiles(tmp_path)


def test_malformed_profile_raises_rather_than_being_skipped(tmp_path):
    """A skipped profile would surface as 'unsupported validation profile' on a
    submitted run, pointing the operator at the request instead of the typo."""
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "example.yaml").write_text(
        yaml.safe_dump(minimal()), encoding="utf-8"
    )
    (tmp_path / "profiles" / "broken.yaml").write_text("{ not: [valid", encoding="utf-8")
    with pytest.raises(profiles.ProfileError):
        profiles.load_profiles(tmp_path)


# ---- rendering ----


def test_render_substitutes_and_does_not_split_on_spaces():
    p = profiles.parse_profile(minimal(), "test")
    rendered = p.render(p.flash_command, python="/v/bin/python", artifact_dir="/a dir/x")
    assert rendered == ["/v/bin/python", "flash.py", "--artifacts", "/a dir/x"]
    # One argv entry, so a path with a space cannot become two arguments.
    assert len(rendered) == 4


def test_render_refuses_a_missing_value():
    p = profiles.parse_profile(minimal(), "test")
    with pytest.raises(profiles.ProfileError, match="no value supplied"):
        p.render(p.flash_command, python="/v/bin/python")


def test_a_profile_that_cannot_flash_is_refused():
    """An empty flash command is only discovered by Popen([]) raising
    IndexError -- after the hardware discovery, with the rig
    held. A profile that cannot flash cannot run a suite; say so at load."""
    doc = minimal()
    del doc["flash"]
    with pytest.raises(profiles.ProfileError, match="flash"):
        profiles.parse_profile(doc, "test")

    doc = minimal()
    doc["flash"] = {"command": []}
    with pytest.raises(profiles.ProfileError, match="non-empty list"):
        profiles.parse_profile(doc, "test")


def test_a_shared_profile_must_declare_what_it_needs():
    """Its board map is built from `needs`. Without them there is nothing to
    scope to, and an unscoped shared run would flash the whole rig."""
    doc = minimal()
    doc["suite"]["exclusive"] = False
    with pytest.raises(profiles.ProfileError, match="must declare needs"):
        profiles.parse_profile(doc, "test")

    doc["needs"] = [{"target": "esp32", "count": 1}]
    spec = profiles.parse_profile(doc, "test")
    assert spec.needs == ({"target": "esp32", "count": 1},)


def test_a_profile_may_share_the_rig_only_when_it_is_scoped():
    """`concurrent: true` lets a run start beside others on boards nobody
    holds. A whole-bank profile cannot: it takes every board."""
    doc = minimal(needs=[{"target": "esp32-c3", "count": 1, "tags": ["psram", "psram"]}])
    doc["suite"].update(exclusive=False, concurrent=True, resources=["mqtt", "gateway"])
    spec = profiles.parse_profile(doc, "test")
    assert spec.concurrent is True and spec.resources == ("gateway", "mqtt")
    assert spec.needs == ({"target": "esp32-c3", "count": 1, "tags": ["psram"]},)

    shipped = profiles.load_profiles(REPO)
    assert not any(item.concurrent for item in shipped.values()), (
        "no shipped profile shares the rig until it has been run beside another on the rig"
    )

    whole = minimal()
    whole["suite"]["concurrent"] = True
    with pytest.raises(profiles.ProfileError, match="cannot be concurrent"):
        profiles.parse_profile(whole, "test")
    for bad, message in (
        ({"concurrent": "yes"}, "suite.concurrent must be true or false"),
        ({"resources": ["the moon"]}, "suite.resources must be a list of gateway, mqtt"),
    ):
        broken = minimal(needs=[{"target": "esp32", "count": 1}])
        broken["suite"].update(exclusive=False, **bad)
        with pytest.raises(profiles.ProfileError, match=message):
            profiles.parse_profile(broken, "test")
    untagged = minimal(needs=[{"target": "esp32", "tags": "psram"}])
    untagged["suite"]["exclusive"] = False
    with pytest.raises(profiles.ProfileError, match="tags must be a list"):
        profiles.parse_profile(untagged, "test")


def test_the_profile_default_ref_is_parsed():
    doc = minimal()
    doc["source"]["default_ref"] = "Feat/next-release"
    assert profiles.parse_profile(doc, "test").default_ref == "Feat/next-release"


# ---- the test stage belongs to the profile ----


def test_a_profile_may_run_its_suite_with_its_own_command():
    doc = minimal(test={"command": ["{python}", "run_hil.py", "--board-map", "{board_map}", "--junit", "{results}"]})
    p = profiles.parse_profile(doc, "x")
    assert p.has_test_command
    assert "{results}" in p.test_command


def test_the_default_runner_is_pytest_and_needs_no_test_section():
    """Every profile that existed before this ran pytest over suite.path, and
    a pytest suite still wants that: the HAL's plugin writes the richer
    records the report prefers."""
    p = profiles.parse_profile(minimal(), "x")
    assert not p.has_test_command


def test_a_test_command_cannot_use_what_the_stage_does_not_supply():
    # {preflight} is the preflight stage's output file; the test stage has none.
    with pytest.raises(profiles.ProfileError, match="not available to the test stage"):
        profiles.parse_profile(minimal(test={"command": ["{python}", "x", "{preflight}"]}), "x")


def test_a_test_section_must_be_a_mapping():
    with pytest.raises(profiles.ProfileError, match="test must be a mapping"):
        profiles.parse_profile(minimal(test=["not", "a", "mapping"]), "x")


# ---- the capability catalog belongs to the profile ----


def test_report_capabilities_must_stay_inside_the_workspace():
    with pytest.raises(profiles.ProfileError, match="report.capabilities"):
        profiles.parse_profile(minimal(report={"capabilities": "../elsewhere.json"}), "x")


def test_a_profile_without_a_catalog_claims_nothing():
    assert profiles.parse_profile(minimal(), "x").capabilities is None
    assert profiles.load_profiles(REPO)["alteriom-firmware"].capabilities is None


def test_painlessmesh_declares_its_catalog_and_every_mark_is_in_it():
    """The drift guard.

    A capability a test marks that the catalog does not list used to produce
    no row and no warning: node.reset_recovery was marked in
    test_power_recovery.py and absent from the catalog, so painlessMesh's own
    report never mentioned it. Now the catalog is a file beside the suite, this
    holds the two together.
    """
    from alteriom_hil.report import load_catalog

    p = profiles.load_profiles(REPO)["painlessmesh"]
    assert p.capabilities == "suites/painlessmesh/capabilities.json"
    catalog = load_catalog(REPO / p.capabilities)
    assert len(catalog) >= 30

    marked = set()
    needle = "pytest.mark.capability("
    for test_file in sorted((REPO / p.suite_path).glob("test_*.py")):
        text = test_file.read_text(encoding="utf-8")
        at = 0
        while True:
            at = text.find(needle, at)
            if at < 0:
                break
            close = text.find(")", at)
            inside = text[at + len(needle):close]
            marked.update(part.strip().strip('"').strip("'") for part in inside.split(",") if part.strip())
            at = close
    assert marked, "no capability marks found; the search is wrong"
    assert marked <= set(catalog), sorted(marked - set(catalog))


def test_a_profile_says_where_its_hil_agent_is_or_has_none():
    assert profiles.parse_profile(minimal(), "t").agent_source_path is None
    named = profiles.parse_profile(minimal(agent={"source_path": "suites/mine/firmware/"}), "t")
    assert named.agent_source_path == "suites/mine/firmware"

    shipped = profiles.load_profiles(REPO)
    assert shipped["painlessmesh"].agent_source_path == "suites/painlessmesh/firmware"
    assert shipped["canary"].agent_source_path is None, "the health check's firmware carries no agent"


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside", "suites/../../outside", "", ".", "suites\\firmware", 7, None])
def test_an_agent_path_stays_inside_the_repository(bad):
    """It is joined to a checkout and handed to `git archive`."""
    with pytest.raises(profiles.ProfileError, match="agent"):
        profiles.parse_profile(minimal(agent={"source_path": bad}), "t")


def test_an_agent_section_must_be_a_mapping_that_names_a_path():
    with pytest.raises(profiles.ProfileError, match="agent must be a mapping"):
        profiles.parse_profile(minimal(agent="suites/mine/firmware"), "t")
    with pytest.raises(profiles.ProfileError, match="source_path"):
        profiles.parse_profile(minimal(agent={}), "t")
