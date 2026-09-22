"""JUnit XML as a source of run records.

The farm's report is built from :class:`alteriom_hil.run_record.RunRecord`
lines, which the HAL's pytest plugin writes as each test finishes. That is the
richer source -- it carries capability marks, failure classes and the reasons a
test can only run on hardware -- and a consumer whose suite is pytest gets it
without doing anything.

A consumer whose suite is not pytest gets nothing from the plugin, and until
this module existed its run reported ``Validation gate: INCOMPLETE`` with zero
tests, however many had passed. What every test runner *can* produce is JUnit
XML, so that is the contract the test stage asks for -- write it at
``{results}`` -- and this is where it becomes records the report understands.

Only the fields JUnit carries are filled. There is no capability evidence and
no failure class in the XML, so a report built from it has a test table and a
verdict, and no coverage table; the fallback is honest about what it knows.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

from .run_record import RunRecord


def records_from_junit(
    path: str | os.PathLike,
    *,
    suite: str,
    mode: str,
    boards: int,
    firmware_sha: str | None = None,
) -> list[RunRecord]:
    """One record per ``<testcase>``, in document order.

    A ``<testcase>`` with a ``<failure>`` is ``failed``, with an ``<error>`` is
    ``error``, with a ``<skipped>`` is ``skipped``, and otherwise ``passed`` --
    the four verdicts the report already aggregates. The test id is
    ``classname::name`` when a classname is present, which for pytest output
    reads as ``package.module::test``; other runners' ids are kept as they are.
    """
    p = Path(path)
    if not p.is_file():
        return []
    try:
        root = ET.parse(p).getroot()
    except ET.ParseError:
        # A run interrupted mid-write leaves a truncated document. That is a
        # run with no evidence, which the report already reports as
        # INCOMPLETE; it is not a reason to fail the report stage.
        return []

    records: list[RunRecord] = []
    for case in root.iter("testcase"):
        name = case.get("name") or ""
        classname = case.get("classname") or ""
        test = f"{classname}::{name}" if classname else name
        verdict = "passed"
        message = None
        for tag, outcome in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            node = case.find(tag)
            if node is not None:
                verdict = outcome
                message = (node.get("message") or node.text or "").strip() or None
                break
        try:
            duration = float(case.get("time") or 0.0)
        except ValueError:
            duration = 0.0
        records.append(
            RunRecord.now(
                suite=suite,
                test=test,
                verdict=verdict,
                duration_s=duration,
                mode=mode,
                boards=boards,
                firmware_sha=firmware_sha,
                message=message,
            )
        )
    return records
