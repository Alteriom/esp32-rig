#!/usr/bin/env python3
"""Decide what a Rig Health Check run (the `canary` profile) means for a deploy, and say so.

A canary run has two kinds of red and they call for different things
(docs/canary.md):

  * A check that failed on **every** board is the farm -- its access point,
    its broker, its uplink, the Pi. The service has already paused the queue
    for it, because everything queued behind the check would otherwise flash
    boards whose rig cannot join a network and report that as the firmware's
    failure. The deploy fails: a release whose hardware cannot answer is not
    a release, and the operator has the reason in the queue and here.

  * A check that failed on **one** board is that board. It is marked on its
    page in the dashboard and in `board-health.json`, the rig keeps working with the
    rest, and the deploy stands -- a farm of six boards is not held hostage
    to one dead ESP, and blocking every release until somebody swaps it would
    only teach people to skip the check.

Anything else that went wrong -- the run never started, the canary would not
build, the bundle was refused, the suite timed out -- is the deploy's problem
too: the farm cannot vouch for its hardware, which is all this step is for.

    python rig/canary_verdict.py canary-run.json

Exits 0 when the deploy may stand, 1 when it must not. Writes a Markdown
summary to stdout, for `$GITHUB_STEP_SUMMARY`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def readable(check: str) -> str:
    """A check's name as a person reads it, not as pytest names it."""
    return check.removeprefix("test_").replace("_", " ")


def verdict(job: dict) -> tuple[bool, list[str]]:
    """`(the deploy may stand, the lines to say why)`."""
    status = job.get("status") or "unknown"
    result = job.get("result") or {}
    health = result.get("health") or {}
    boards = health.get("boards") or {}
    farm_wide = health.get("farm_wide") or []
    version = result.get("version")
    lines = [f"## Rig Health Check {version}" if version else "## Rig Health Check", ""]
    run = job.get("id") or ""
    if run:
        lines.append(f"Run `{run[:8]}` — {status}")
        lines.append("")

    if not boards:
        # No per-board verdicts at all: the run did not get as far as
        # checking anything, whatever its status says.
        lines += [
            "The Rig Health Check produced no per-board verdicts, so the farm cannot "
            "vouch for its hardware for this release.",
            "",
            f"Pipeline status: **{status}**. "
            f"{result.get('summary') or result.get('detail') or 'See the run log and evidence.'}",
        ]
        return status == "passed", lines

    passed = sorted(board for board, outcome in boards.items() if outcome == "passed")
    failed = sorted(board for board, outcome in boards.items() if outcome != "passed")
    lines.append(f"| Board | Verdict |")
    lines.append("|---|---|")
    for board in sorted(boards):
        lines.append(f"| `{board}` | {'healthy' if boards[board] == 'passed' else '**unhealthy**'} |")
    lines.append("")

    if farm_wide:
        checks = ", ".join(readable(check) for check in farm_wide)
        lines += [
            f"**{checks} failed on every board.** That is the farm, not the "
            f"boards: its access point, its broker, its uplink or the Pi. The "
            f"queue is paused until an operator resumes it, and this deploy "
            f"fails — a release whose hardware cannot answer is not a release.",
            "",
            "Fix the rig and redeploy, or redeploy the last good ref "
            "(`workflow_dispatch` takes one).",
        ]
        return False, lines

    if failed:
        lines += [
            f"{len(failed)} of {len(boards)} boards failed their own checks: "
            + ", ".join(f"`{board}`" for board in failed)
            + ".",
            "",
            "Every check that failed did so on some boards and not others, so "
            "this is those boards rather than the farm. They are marked on their "
            "pages under Rigs; the rig keeps working with the rest and this "
            "deploy stands.",
        ]
        return True, lines

    lines.append(f"Every check passed on all {len(passed)} boards.")
    return True, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="the run JSON the CI client wrote")
    args = parser.parse_args(argv)
    try:
        job = json.loads(args.run.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # The client writes this file whatever the run did, so its absence
        # means the step before never ran -- which is not a pass.
        print("## Rig Health Check\n")
        print(f"No run to judge: {exc}")
        return 1
    may_stand, lines = verdict(job if isinstance(job, dict) else {})
    print("\n".join(lines))
    return 0 if may_stand else 1


if __name__ == "__main__":
    sys.exit(main())
