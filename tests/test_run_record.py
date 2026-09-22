"""Tests for RunRecord + the aggregate report."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from alteriom_hil.junit import records_from_junit
from alteriom_hil.report import load_catalog, render_markdown, summarize

REPO = Path(__file__).resolve().parents[1]
# painlessMesh's catalog, which every report used to render. The tests that
# assert on gateway.bridge and NOT VALIDATED were written against it and
# now say so.
PAINLESSMESH = load_catalog(REPO / "suites" / "painlessmesh" / "capabilities.json")
from alteriom_hil.run_record import (
    HIL_ONLY_REASONS,
    RunRecord,
    load_records,
    write_record,
)


def _rec(**over) -> RunRecord:
    base = dict(
        suite="painlessmesh",
        test="suites/painlessmesh/tests/test_x.py::test_x",
        verdict="passed",
        duration_s=1.0,
        mode="sim",
        boards=3,
    )
    base.update(over)
    return RunRecord.now(**base)


def test_record_roundtrips_through_jsonl(tmp_path):
    path = tmp_path / "runs.jsonl"
    write_record(path, _rec())
    write_record(
        path,
        _rec(
            verdict="failed",
            failure_class="real_bug",
            hil_only_reason="radio_timing",
            message="AssertionError",
        ),
    )
    loaded = list(load_records(path))
    assert len(loaded) == 2
    assert loaded[0].verdict == "passed"
    assert loaded[1].failure_class == "real_bug"


def test_record_omits_none_and_empty_from_json():
    r = _rec()
    data = json.loads(r.to_json())
    assert "failure_class" not in data
    assert "extra" not in data
    assert data["verdict"] == "passed"


def test_load_records_walks_a_directory(tmp_path):
    (tmp_path / "a.jsonl").write_text(_rec().to_json() + "\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.jsonl").write_text(_rec(verdict="failed").to_json() + "\n")
    verdicts = sorted(r.verdict for r in load_records(tmp_path))
    assert verdicts == ["failed", "passed"]


def test_summarize_computes_bug_catch_delta():
    records = [
        _rec(),  # passed, no delta
        _rec(
            verdict="failed",
            failure_class="real_bug",
            hil_only_reason="radio_timing",
        ),
        _rec(  # flaky doesn't count
            verdict="failed",
            failure_class="flaky_hardware",
            hil_only_reason="radio_timing",
        ),
        _rec(  # not hil_only doesn't count
            verdict="failed",
            failure_class="real_bug",
        ),
        _rec(
            verdict="error",
            failure_class="real_bug",
            hil_only_reason="ota",
        ),
    ]
    s = summarize(records)
    assert s["total_runs"] == 5
    assert s["bug_catch_delta"] == 2
    assert s["verdicts"]["passed"] == 1
    assert s["failure_classes"]["real_bug"] == 3


def test_summarize_defaults_missing_failure_class_to_real_bug():
    # A failed record with hil_only_reason but no failure_class still counts —
    # unclassified failures default to real_bug for the delta computation.
    records = [_rec(verdict="failed", hil_only_reason="power")]
    assert summarize(records)["bug_catch_delta"] == 1


def test_summarize_reports_physical_target_inventory():
    records = [
        _rec(
            mode="hardware",
            extra={
                "inventory": [
                    {"id": "esp32-01", "target": "esp32", "chip": "esp32"},
                    {"id": "c3-01", "target": "esp32-c3", "chip": "esp32c3"},
                ]
            },
        )
    ]
    assert summarize(records)["inventory"] == [
        {"id": "c3-01", "target": "esp32-c3", "chip": "esp32c3"},
        {"id": "esp32-01", "target": "esp32", "chip": "esp32"},
    ]


def test_summarize_and_render_report_hil_agent_provenance():
    records = [_rec(extra={"hil_agent_sha": "a" * 64})]
    assert summarize(records)["hil_agent_shas"] == ["a" * 64]
    assert f"`{'a' * 64}`" in render_markdown(records, "agent provenance")


def test_render_markdown_includes_headline(tmp_path):
    md = render_markdown(
        [
            _rec(
                verdict="failed",
                failure_class="real_bug",
                hil_only_reason="radio_timing",
                firmware_sha="abcdef1234567890",
                message="association failed <safely>",
                extra={"capabilities": ["mesh.formation"]},
            )
        ],
        title="Q3 report",
        catalog=PAINLESSMESH,
    )
    assert "Q3 report" in md
    assert "Bug-catch delta: 1" in md
    assert "radio_timing" in md
    assert "abcdef123456" in md
    assert "mesh.formation" in md
    assert "VALIDATED" in md
    assert "gateway.bridge" in md
    assert "NOT VALIDATED" in md
    assert "Validation gate: FAILED" in md
    assert "association failed &lt;safely&gt;" in md


def test_hil_only_reasons_cover_incubation_categories():
    for expected in ("radio_timing", "ota", "power", "soak"):
        assert expected in HIL_ONLY_REASONS


def test_report_cli_writes_markdown(tmp_path):
    log = tmp_path / "runs.jsonl"
    write_record(
        log,
        _rec(
            verdict="failed",
            failure_class="real_bug",
            hil_only_reason="radio_timing",
        ),
    )
    out = tmp_path / "report.md"
    json_out = tmp_path / "report.json"
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "alteriom_hil.report",
            str(log),
            "--out",
            str(out),
            "--json-out",
            str(json_out),
            "--capabilities",
            str(REPO / "suites" / "painlessmesh" / "capabilities.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    assert "Bug-catch delta: 1" in text
    assert json.loads(json_out.read_text())["capabilities"]["gateway.bridge"][
        "status"
    ] == "not_validated"


def test_missing_metrics_generate_an_incomplete_report(tmp_path):
    records = list(load_records(tmp_path / "missing-metrics"))
    summary = summarize(records)
    assert summary["validation_gate"] == "incomplete"
    assert "Validation gate: INCOMPLETE" in render_markdown(records, "Interrupted run")


def test_infra_only_failure_blocks_without_failing_capability():
    records = [
        _rec(
            verdict="error",
            failure_class="infra",
            extra={"capabilities": ["mesh.formation"]},
        )
    ]
    summary = summarize(records, PAINLESSMESH)
    assert summary["validation_gate"] == "blocked"
    assert summary["capabilities"]["mesh.formation"]["status"] == "blocked"
    assert summary["bug_catch_delta"] == 0


# ---- the catalog is the profile's, and JUnit is a source ----


def test_a_report_with_no_catalog_has_no_coverage_table():
    """The farm's second consumer's first report listed all thirty of
    painlessMesh's capabilities as NOT VALIDATED, in bold, for a product that
    had never claimed one. No catalog means no table -- not someone else's."""
    md = render_markdown([_rec()], "alteriom-firmware")
    assert "Capability coverage" not in md
    assert "NOT VALIDATED" not in md
    assert "HIL agent source SHA" not in md, "painlessMesh's agent is not a gap in someone else's report"
    assert summarize([_rec()])["capabilities"] == {}


def test_a_marked_capability_the_catalog_omits_is_reported_not_dropped():
    records = [_rec(extra={"capabilities": ["node.reset_recovery"]})]
    summary = summarize(records, {"mesh.formation": "Every node joins one mesh"})
    row = summary["capabilities"]["node.reset_recovery"]
    assert row["status"] == "validated"
    assert "not in the profile" in row["description"]
    assert summary["capabilities"]["mesh.formation"]["status"] == "not_validated"


def test_catalog_commentary_keys_are_ignored(tmp_path):
    path = tmp_path / "caps.json"
    path.write_text(json.dumps({"_comment": ["why"], "boot.console": "It boots"}), encoding="utf-8")
    assert load_catalog(path) == {"boot.console": "It boots"}
    assert load_catalog(None) == {}


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="hil" tests="3" failures="1" errors="0" skipped="1">
    <testcase classname="tests.hil.test_boot" name="test_boots" time="2.5"/>
    <testcase classname="tests.hil.test_boot" name="test_console" time="0.4">
      <failure message="expected a prompt">AssertionError: expected a prompt</failure>
    </testcase>
    <testcase classname="tests.hil.test_mesh" name="test_peers" time="0.0">
      <skipped message="one board"/>
    </testcase>
  </testsuite>
</testsuites>
"""


def _manifest(tmp_path, with_app: bool):
    import json

    targets = {
        "esp32-c5": {
            "board": "esp32-c5-devkitc-1", "chip": "esp32c5", "environment": "unified-oled-c5",
            "files": {"firmware.bin": {"size": 1_969_696}}, "size": 2_035_232,
        },
        "esp32": {
            "board": "esp32dev", "chip": "esp32", "environment": "universal-sensor-prod",
            "files": {"firmware.bin": {"size": 1_540_000}}, "size": 1_607_088,
        },
    }
    if with_app:
        targets["esp32-c5"]["app"] = {"size": 1_969_696, "slot": 3_342_336}
        targets["esp32"]["app"] = {"size": 1_540_000, "slot": 1_835_008}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema": 2, "targets": targets}), encoding="utf-8")
    return path


def test_the_report_shows_each_family_image_against_its_slot(tmp_path):
    """A consumer choosing a cheaper 4 MB part needs the number per commit,
    not after the choice: the core-3 C5 image was 100.9% of a 4 MB slot and
    the C6's 96.7%. Use above 95% is bold, so it reads as the warning it is."""
    from alteriom_hil.report import load_images

    md = render_markdown([_rec()], "images", None, load_images(_manifest(tmp_path, with_app=True)))
    assert "## Firmware images" in md
    assert "| `esp32-c5` | `unified-oled-c5` | esp32-c5-devkitc-1 | 1,924 KiB | 3,264 KiB | 58.9% |" in md
    assert "| `esp32` | `universal-sensor-prod` | esp32dev | 1,504 KiB | 1,792 KiB | **83.9%** |" not in md
    assert "| 1,504 KiB | 1,792 KiB | 83.9% |" in md


def test_an_image_near_its_slot_is_flagged(tmp_path):
    import json

    from alteriom_hil.report import load_images

    path = _manifest(tmp_path, with_app=True)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["targets"]["esp32"]["app"] = {"size": 1_773_806, "slot": 1_835_008}
    path.write_text(json.dumps(doc), encoding="utf-8")
    md = render_markdown([_rec()], "images", None, load_images(path))
    assert "**96.7%**" in md


def test_a_manifest_without_app_facts_still_lists_the_images(tmp_path):
    """build scripts that predate the app field: the image is known, the
    slot is not, and the table says so rather than inventing a number."""
    from alteriom_hil.report import load_images

    md = render_markdown([_rec()], "images", None, load_images(_manifest(tmp_path, with_app=False)))
    assert "| `esp32-c5` | `unified-oled-c5` | esp32-c5-devkitc-1 | 1,924 KiB | — | — |" in md


def test_the_gateway_matrix_says_whether_a_family_held_mesh_and_uplink(tmp_path):
    """The question a part decision needs answered per family, from the row
    the consumer's uplink tests wrote: every measure present is "holds";
    the first one missing names where the run stopped."""
    import json

    from alteriom_hil.report import load_matrix

    path = tmp_path / "gateway-matrix.jsonl"
    path.write_text(
        json.dumps({"family": "esp32-s3", "board_id": "esp32-s3-01", "sensors": 5, "time_to_uplink_s": 18.4,
                    "reconverge_s": 61.0, "data_from": ["a", "b", "c", "d", "e"], "free_heap_min": 143360})
        + "\n"
        + json.dumps({"family": "esp32-c3", "board_id": "esp32-c3-02", "sensors": 5, "time_to_uplink_s": 22.1})
        + "\n"
        + json.dumps({"family": "esp32", "board_id": "esp32-03", "sensors": 5, "time_to_uplink_s": 13.0,
                      "reconverge_s": 100.2, "data_from": ["a", "b", "c", "d", "e"], "in_topology": ["a", "c"],
                      "free_heap_min": 157232})
        + "\n"
        + json.dumps({"family": "esp32-c6", "board_id": "esp32-c6-14b4", "sensors": 5, "time_to_uplink_s": 9.7,
                      "reconverge_s": 112.3, "mesh_held": False, "data_from": ["a", "b", "c", "d", "e"],
                      "in_topology": ["a", "b", "c", "d", "e"], "free_heap_min": 148856})
        + "\n"
        + "not json\n",
        encoding="utf-8",
    )
    md = render_markdown([_rec()], "matrix", None, None, load_matrix(path))
    assert "## Gateway matrix" in md
    assert "| `esp32-s3` | `esp32-s3-01` | **holds** | 18.4 | 61.0 | 5/5 | — | 140 KiB |" in md
    assert "| `esp32-c3` | `esp32-c3-02` | did not: mesh did not re-form | 22.1 | — | — | — | — |" in md
    # The gateway's own account of the mesh, once the suite records it: a
    # topology naming two of five sensors is a gateway that reached them all
    # and said so about two.
    assert "| `esp32` | `esp32-03` | **holds** | 13.0 | 100.2 | 5/5 | 2/5 | 154 KiB |" in md
    # Every measure present, but a sensor dropped out during the stability
    # window: the numbers stay, the verdict does not say holds.
    assert "| `esp32-c6` | `esp32-c6-14b4` | did not: mesh did not hold its window | 9.7 | 112.3 | 5/5 | 5/5 | 145 KiB |" in md


def test_no_matrix_means_no_matrix_section(tmp_path):
    from alteriom_hil.report import load_matrix

    assert load_matrix(None) == []
    assert load_matrix(tmp_path / "missing.jsonl") == []
    assert "## Gateway matrix" not in render_markdown([_rec()], "matrix")


def test_no_manifest_means_no_images_section(tmp_path):
    from alteriom_hil.report import load_images

    assert load_images(None) == {}
    assert load_images(tmp_path / "missing.json") == {}
    assert "## Firmware images" not in render_markdown([_rec()], "images")


def test_junit_becomes_records(tmp_path):
    path = tmp_path / "results.xml"
    path.write_text(JUNIT, encoding="utf-8")
    records = records_from_junit(path, suite="my-project", mode="hardware", boards=2, firmware_sha="abc123")
    assert [r.verdict for r in records] == ["passed", "failed", "skipped"]
    assert records[0].test == "tests.hil.test_boot::test_boots"
    assert records[0].duration_s == 2.5
    assert records[1].message == "expected a prompt"
    assert {r.suite for r in records} == {"my-project"}
    assert {r.boards for r in records} == {2}
    assert {r.firmware_sha for r in records} == {"abc123"}


def test_a_missing_or_truncated_junit_is_no_records_not_an_error(tmp_path):
    assert records_from_junit(tmp_path / "absent.xml", suite="x", mode="sim", boards=0) == []
    broken = tmp_path / "broken.xml"
    broken.write_text("<testsuites><testsuite", encoding="utf-8")
    assert records_from_junit(broken, suite="x", mode="sim", boards=0) == []


def test_the_cli_reads_junit_when_there_are_no_records(tmp_path):
    """A suite that is not pytest leaves no plugin records; until this, its
    run reported INCOMPLETE with zero tests, however many had passed."""
    metrics = tmp_path / "metrics"
    metrics.mkdir()
    junit = tmp_path / "results.xml"
    junit.write_text(JUNIT, encoding="utf-8")
    out = tmp_path / "report.md"
    json_out = tmp_path / "report.json"
    r = subprocess.run(
        [sys.executable, "-m", "alteriom_hil.report", str(metrics),
         "--junit", str(junit), "--suite", "my-project", "--boards", "2",
         "--firmware-sha", "abc123", "--title", "HIL my-project abc123",
         "--out", str(out), "--json-out", str(json_out)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    text = out.read_text(encoding="utf-8")
    assert "Validation gate: FAILED" in text
    assert "1 passed, 1 failed" in text
    assert "expected a prompt" in text
    assert "Capability coverage" not in text
    summary = json.loads(json_out.read_text(encoding="utf-8"))
    assert summary["by_sha"] == {"abc123": {"passed": 1, "failed": 1, "skipped": 1}}


def test_plugin_records_win_over_junit(tmp_path):
    """The plugin's records carry marks and failure classes; JUnit does not.
    When both exist the richer source is the report."""
    log = tmp_path / "metrics" / "runs.jsonl"
    write_record(log, _rec())
    junit = tmp_path / "results.xml"
    junit.write_text(JUNIT, encoding="utf-8")
    out = tmp_path / "report.md"
    r = subprocess.run(
        [sys.executable, "-m", "alteriom_hil.report", str(log.parent),
         "--junit", str(junit), "--suite", "x", "--out", str(out)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "Total test runs: 1 " in out.read_text(encoding="utf-8")
