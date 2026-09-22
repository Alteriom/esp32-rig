"""Aggregate HIL run records into a per-release-cycle markdown report.

Usage::

    python -m alteriom_hil.report <records-dir-or-file> [--out FILE]
                                  [--since ISO8601] [--title TEXT]

The report answers the go/no-go question from incubation: **is HIL
catching bugs compile-only CI would have missed?** The headline number is
the *bug-catch delta* — failures on tests marked
``@pytest.mark.hil_only(reason=...)`` and classified as ``real_bug``.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Sequence

from .run_record import HIL_ONLY_REASONS, RunRecord, load_records


def load_catalog(path: str | os.PathLike | None) -> dict[str, str]:
    """A consumer's capability catalog: capability key -> requirement text.

    The catalog used to be a dict in this module -- painlessMesh's thirty
    capabilities -- and every report rendered every one of them. The farm's
    second consumer then received a report listing all thirty as coverage
    gaps, in bold, for a product that had never claimed any. What a suite
    proves is the suite's statement to make, so the profile points at a
    file (report.capabilities) and this reads it.

    Keys beginning with an underscore are commentary and are dropped, so the
    file can explain itself.
    """
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: capability catalog must be a JSON object")
    return {
        str(key): str(value)
        for key, value in data.items()
        if not str(key).startswith("_")
    }


def parse_not_covered(values: Sequence[str] | None) -> list[dict]:
    """``["esp32-s3=0/1"]`` -> the families a run did not exercise.

    The farm knows this before the suite starts — it is what its
    allocation did not meet (``alteriom_hil.allocation.coverage``) — and
    the report is where a person reads it. Without it a run that covered four
    families of five renders exactly like one that covered all five, and its
    pass is read as the whole gate.
    """
    out = []
    for value in values or []:
        family, _, counts = str(value).partition("=")
        got, _, wanted = counts.partition("/")
        if not family or not wanted:
            raise ValueError(f"--not-covered expects FAMILY=GOT/WANTED, got {value!r}")
        out.append({"target": family, "got": int(got), "wanted": int(wanted)})
    return out


def _filter(records: Iterable[RunRecord], since: str | None) -> list[RunRecord]:
    out = list(records)
    if since:
        out = [r for r in out if r.ts >= since]
    return out


def summarize(records: Sequence[RunRecord], catalog: dict[str, str] | None = None) -> dict:
    """Compute headline aggregates. Kept pure so tests can drive it."""
    verdict_counts: Counter[str] = Counter(r.verdict for r in records)
    class_counts: Counter[str] = Counter(
        r.failure_class or "unclassified"
        for r in records
        if r.verdict in ("failed", "error")
    )
    by_reason: Counter[str] = Counter(
        r.hil_only_reason or "not_hil_only"
        for r in records
        if r.verdict in ("failed", "error")
    )
    delta = sum(
        1
        for r in records
        if r.verdict in ("failed", "error")
        and r.hil_only_reason in HIL_ONLY_REASONS
        and (r.failure_class or "real_bug") == "real_bug"
    )
    by_sha: dict[str, Counter[str]] = defaultdict(Counter)
    for r in records:
        by_sha[r.firmware_sha or "unknown"][r.verdict] += 1
    inventory = {
        (item["id"], item["target"], item["chip"])
        for record in records
        for item in record.extra.get("inventory", [])
    }
    agent_shas = sorted(
        {
            str(record.extra["hil_agent_sha"])
            for record in records
            if record.extra.get("hil_agent_sha")
        }
    )
    capability_runs: dict[str, list[RunRecord]] = defaultdict(list)
    for record in records:
        for capability in record.extra.get("capabilities", []):
            capability_runs[capability].append(record)
    capabilities = {}
    # One row per catalog entry, plus any capability a test marked that the
    # catalog does not list. The second kind used to vanish: a mark with no
    # catalog entry produced no row and no warning, and node.reset_recovery
    # sat unreported in painlessMesh's own report for that reason.
    table: dict[str, str] = dict(catalog or {})
    for capability in sorted(capability_runs):
        table.setdefault(capability, "(not in the profile's catalog)")
    for capability, description in table.items():
        runs = capability_runs.get(capability, [])
        verdicts = Counter(run.verdict for run in runs)
        non_infra_failures = any(
            run.verdict in ("failed", "error") and run.failure_class != "infra"
            for run in runs
        )
        infra_failures = any(
            run.verdict in ("failed", "error") and run.failure_class == "infra"
            for run in runs
        )
        if non_infra_failures:
            status = "failed"
        elif verdicts.get("passed"):
            status = "validated"
        elif infra_failures:
            status = "blocked"
        elif verdicts.get("skipped"):
            status = "skipped"
        else:
            status = "not_validated"
        capabilities[capability] = {
            "description": description,
            "status": status,
            "tests": sorted({run.test for run in runs}),
            "verdicts": dict(verdicts),
        }
    has_failures = bool(verdict_counts.get("failed") or verdict_counts.get("error"))
    has_non_infra_failure = any(
        r.verdict in ("failed", "error") and r.failure_class != "infra"
        for r in records
    )
    return {
        "validation_gate": (
            "incomplete"
            if not records
            else "failed"
            if has_non_infra_failure
            else "blocked"
            if has_failures
            else "passed"
        ),
        "total_runs": len(records),
        "verdicts": dict(verdict_counts),
        "failure_classes": dict(class_counts),
        "failures_by_reason": dict(by_reason),
        "bug_catch_delta": delta,
        "by_sha": {k: dict(v) for k, v in by_sha.items()},
        "inventory": [
            {"id": board_id, "target": target, "chip": chip}
            for board_id, target, chip in sorted(inventory)
        ],
        "hil_agent_shas": agent_shas,
        "capabilities": capabilities,
    }


def load_images(path: str | os.PathLike | None) -> dict[str, dict]:
    """The per-family image facts from a build manifest, or nothing.

    A consumer choosing a part -- a cheaper 4 MB module for one product
    tier -- needs to know how much of the app slot each family's image
    takes, per commit, before the choice is made: alteriom-firmware's core-3
    C5 image turned out to be 100.9% of a 4 MB slot and its C6's 96.7%, one
    feature from not fitting. The manifest carries the size of every image
    the run flashed; a consumer's build script may add ``app`` (``size``,
    ``slot``) for the number that answers the question. Absent manifest,
    absent section.
    """
    if not path:
        return {}
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    targets = manifest.get("targets") if isinstance(manifest, dict) else None
    return dict(targets) if isinstance(targets, dict) else {}


def _image_rows(images: dict[str, dict]) -> list[str]:
    rows = []
    for family, entry in sorted(images.items()):
        files = entry.get("files") or {}
        app = entry.get("app") or {}
        app_size = app.get("size") or (files.get("firmware.bin") or {}).get("size")
        slot = app.get("slot")
        if app_size and slot:
            use = f"{app_size / slot * 100:.1f}%"
            if app_size / slot > 0.95:
                use = f"**{use}**"
        else:
            use = "—"
        rows.append(
            f"| `{family}` | `{entry.get('environment', '—')}` | {entry.get('board', '—')} "
            f"| {_kib(app_size)} | {_kib(slot)} | {use} |"
        )
    return rows


def _kib(size) -> str:
    return f"{int(size) / 1024:,.0f} KiB" if size else "—"


# The measures a gateway row carries when every uplink test passed, in the
# order the tests take them; the first one missing names where the run
# stopped.
MATRIX_MEASURES = ("time_to_uplink_s", "reconverge_s", "data_from", "free_heap_min")


def load_matrix(path: str | os.PathLike | None) -> list[dict]:
    """The gateway-matrix rows a run wrote, or nothing.

    One JSON line per gateway the suite provisioned (mqtt/gateway-matrix.jsonl,
    written by the consumer's uplink tests): the family that played the
    gateway and what it managed while serving the mesh and the uplink at
    once. The question a part decision needs answered per family.
    """
    if not path:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("family"):
            rows.append(row)
    return rows


def _matrix_verdict(row: dict) -> str:
    missing = [m for m in MATRIX_MEASURES if m not in row]
    if not missing:
        # The suite records whether the mesh was still whole a window after
        # re-converging. A row with every measure but that flag false is a
        # gateway that re-formed its mesh and then lost a sensor -- it read
        # as "holds" before the flag existed (esp32 hand run, 2026-09-10).
        if row.get("mesh_held") is False:
            return "did not: mesh did not hold its window"
        return "**holds**"
    stopped = {
        "time_to_uplink_s": "no uplink",
        "reconverge_s": "mesh did not re-form",
        "data_from": "no sensor data on the queue",
        "free_heap_min": "no heap sample",
    }[missing[0]]
    return f"did not: {stopped}"


def _matrix_rows(rows: list[dict]) -> list[str]:
    out = []
    for row in rows:
        data_from = row.get("data_from")
        data = f"{len(data_from)}/{row.get('sensors', '—')}" if isinstance(data_from, list) else "—"
        # The gateway's own account of the mesh: sensors its topology message
        # named once its status had counted them all. Rows from before the
        # suite recorded it have none.
        in_topology = row.get("in_topology")
        topology = f"{len(in_topology)}/{row.get('sensors', '—')}" if isinstance(in_topology, list) else "—"
        heap = row.get("free_heap_min")
        out.append(
            f"| `{row['family']}` | `{row.get('board_id', '—')}` | {_matrix_verdict(row)} "
            f"| {row.get('time_to_uplink_s', '—')} | {row.get('reconverge_s', '—')} "
            f"| {data} | {topology} | {_kib(heap) if heap else '—'} |"
        )
    return out


def render_markdown(
    records: Sequence[RunRecord],
    title: str,
    catalog: dict[str, str] | None = None,
    images: dict[str, dict] | None = None,
    matrix: list[dict] | None = None,
    not_covered: Sequence[dict] | None = None,
) -> str:
    s = summarize(records, catalog)
    total = s["total_runs"]
    passed = s["verdicts"].get("passed", 0)
    failed = s["verdicts"].get("failed", 0) + s["verdicts"].get("error", 0)
    skipped = s["verdicts"].get("skipped", 0)
    pass_rate = (passed / total * 100) if total else 0.0

    buf = io.StringIO()
    w = buf.write
    w(f"# {title}\n\n")
    w("_Auto-generated by `alteriom_hil.report`._\n\n")
    w("## Headline\n\n")
    w(f"- **Validation gate: {s['validation_gate'].upper()}**.\n")
    w(f"- **Bug-catch delta: {s['bug_catch_delta']}** real-bug failures on\n")
    w("  tests compile-only CI could not have caught.\n")
    w(f"- Total test runs: {total} — {passed} passed, {failed} failed,\n")
    w(f"  {skipped} skipped ({pass_rate:.1f}% pass rate).\n")
    if not_covered:
        families = ", ".join(
            f"`{entry['target']}` ({entry['got']} of {entry['wanted']} boards)"
            for entry in not_covered
        )
        w(f"- **Coverage: PARTIAL** — this run did not exercise {families}.\n")
        w("  The gate above is the verdict on what ran, not on the whole profile.\n")
    w("\n")

    if not_covered:
        w("## Families not covered\n\n")
        w("The profile asks for these boards and this run did not get them: off\n")
        w("the rig, held out, or in use by another run. Nothing in this report\n")
        w("was validated on them, whatever its verdicts say.\n\n")
        w("| Family | Boards wanted | Boards used |\n")
        w("|---|---:|---:|\n")
        for entry in not_covered:
            w(f"| `{entry['target']}` | {entry['wanted']} | {entry['got']} |\n")
        w("\n")

    modes = sorted({record.mode for record in records if record.mode})
    boards = sorted({record.boards for record in records})
    shas = sorted({record.firmware_sha for record in records if record.firmware_sha})
    w("## Provenance\n\n")
    w(f"- Firmware commit(s): {', '.join(f'`{sha}`' for sha in shas) or 'unknown'}\n")
    # The HIL agent is painlessMesh's test firmware; a consumer running its
    # product firmware has none, and an 'unknown' here would read as a gap.
    if s["hil_agent_shas"]:
        w("- HIL agent source SHA(s): " + ", ".join(f"`{sha}`" for sha in s["hil_agent_shas"]) + chr(10))
    w(f"- Execution mode(s): {', '.join(modes) or 'unknown'}\n")
    w(f"- Board count(s): {', '.join(str(n) for n in boards)}\n")
    if s["inventory"]:
        physical = ", ".join(
            f"`{item['id']}` ({item['target']})" for item in s["inventory"]
        )
        w(f"- Physical inventory: {physical}\n")
    w("\n")

    if images:
        w("## Firmware images\n\n")
        w("| Family | Environment | Board | App image | App slot | Use |\n")
        w("|---|---|---|---:|---:|---:|\n")
        for row in _image_rows(images):
            w(row + "\n")
        w("\nUse above 95% is bold: the image is one feature from not fitting its slot.\n\n")

    if matrix:
        w("## Gateway matrix\n\n")
        w("Can this family serve the mesh and the uplink on one radio at once? One row per gateway this run provisioned.\n\n")
        w("| Family | Board | Verdict | To uplink (s) | Mesh re-formed (s) | Sensors on queue | In topology | Free heap min |\n")
        w("|---|---|---|---:|---:|---:|---:|---:|\n")
        for row in _matrix_rows(matrix):
            w(row + "\n")
        w("\n")

    if s["capabilities"]:
        w("## Capability coverage\n\n")
        w("| Capability | Status | Evidence | Requirement |\n")
        w("|---|---|---:|---|\n")
        for capability, data in s["capabilities"].items():
            status = data["status"].upper().replace("_", " ")
            evidence = ", ".join(
                f"{count} {verdict}" for verdict, count in sorted(data["verdicts"].items())
            ) or "no tests"
            w(f"| `{capability}` | **{status}** | {evidence} | {data['description']} |\n")
        w("\nA `NOT VALIDATED` row is an explicit coverage gap, not a pass.\n\n")

    w("## Test evidence\n\n")
    w("| Test | Capabilities | Verdict | Duration | Mode |\n")
    w("|---|---|---|---:|---|\n")
    for record in records:
        capabilities = ", ".join(
            f"`{item}`" for item in record.extra.get("capabilities", [])
        ) or "—"
        w(
            f"| `{record.test}` | {capabilities} | {record.verdict.upper()} | "
            f"{record.duration_s:.3f}s | {record.mode} |\n"
        )
    w("\n")

    failures = [record for record in records if record.verdict in ("failed", "error")]
    w("## Failure details\n\n")
    if failures:
        for record in failures:
            message = html.escape(record.message or "No failure message recorded.")
            w(f"<details><summary><code>{record.test}</code> — {record.verdict.upper()}</summary>\n\n")
            w(f"<pre>{message}</pre>\n\n</details>\n\n")
    else:
        w("_No failures recorded._\n\n")

    w("## Failure classes\n\n")
    if s["failure_classes"]:
        w("| Class | Count |\n|---|---|\n")
        for cls, n in sorted(
            s["failure_classes"].items(), key=lambda x: -x[1]
        ):
            w(f"| {cls} | {n} |\n")
        w("\nOnly `real_bug` failures on `hil_only`-marked tests count "
          "toward the delta.\n\n")
    else:
        w("_No failures recorded._\n\n")

    w("## Failures by HIL-only reason\n\n")
    if s["failures_by_reason"]:
        w("| Reason | Count |\n|---|---|\n")
        for reason, n in sorted(
            s["failures_by_reason"].items(), key=lambda x: -x[1]
        ):
            marker = "*" if reason in HIL_ONLY_REASONS else ""
            w(f"| {reason}{marker} | {n} |\n")
        w("\n`*` = compile-only CI could not have caught this.\n\n")
    else:
        w("_No failures recorded._\n\n")

    w("## Per firmware SHA\n\n")
    if s["by_sha"]:
        w("| SHA | passed | failed | error | skipped |\n|---|---|---|---|---|\n")
        for sha, cts in sorted(s["by_sha"].items()):
            w(
                f"| `{sha[:12] if sha != 'unknown' else sha}` | "
                f"{cts.get('passed', 0)} | {cts.get('failed', 0)} | "
                f"{cts.get('error', 0)} | {cts.get('skipped', 0)} |\n"
            )
    else:
        w("_No records._\n")

    return buf.getvalue()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alteriom_hil.report",
        description="Summarize HIL run records into a markdown report.",
    )
    p.add_argument("source", help="JSONL file or directory of JSONL files")
    p.add_argument("--out", help="Write markdown here (default: stdout)")
    p.add_argument("--since", help="ISO8601 lower bound on record ts")
    p.add_argument(
        "--title", default="HIL bug-catch delta report", help="Report title"
    )
    p.add_argument("--json-out", help="Write the machine-readable summary here")
    p.add_argument(
        "--capabilities",
        help="JSON file mapping capability keys to requirement text (the profile's report.capabilities)",
    )
    # The fallback source. A suite that is not pytest leaves no plugin records
    # behind, and these say how to read its JUnit instead.
    p.add_argument("--junit", help="JUnit XML to build records from when source holds none")
    p.add_argument("--suite", default="", help="suite name for records built from --junit")
    p.add_argument("--mode", default="hardware", help="execution mode for records built from --junit")
    p.add_argument("--boards", type=int, default=0, help="board count for records built from --junit")
    p.add_argument("--firmware-sha", help="firmware revision for records built from --junit")
    p.add_argument("--manifest", help="build manifest of the images the run flashed; adds the firmware-images table")
    p.add_argument("--gateway-matrix", help="mqtt/gateway-matrix.jsonl the run's uplink tests wrote; adds the gateway matrix")
    p.add_argument(
        "--not-covered",
        action="append",
        metavar="FAMILY=GOT/WANTED",
        help="a family the run's allocation did not meet, e.g. esp32-s3=0/1; repeatable",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    records = _filter(load_records(args.source), args.since)
    if not records and args.junit:
        from .junit import records_from_junit

        records = records_from_junit(
            args.junit,
            suite=args.suite,
            mode=args.mode,
            boards=args.boards,
            firmware_sha=args.firmware_sha,
        )
    catalog = load_catalog(args.capabilities)
    images = load_images(args.manifest)
    matrix = load_matrix(args.gateway_matrix)
    not_covered = parse_not_covered(args.not_covered)
    md = render_markdown(records, args.title, catalog, images, matrix, not_covered)
    if args.out:
        from pathlib import Path

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
    else:
        print(md, end="")
    if args.json_out:
        from pathlib import Path

        json_out = Path(args.json_out)
        json_out.parent.mkdir(parents=True, exist_ok=True)
        summary = summarize(records, catalog)
        if images:
            summary["firmware_images"] = images
        if matrix:
            summary["gateway_matrix"] = matrix
        if not_covered:
            # In the machine-readable summary too: whatever reads a report
            # without rendering it -- the dashboard, a release gate -- has to
            # be able to see that this run was not the whole profile.
            summary["not_covered"] = not_covered
        json_out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
