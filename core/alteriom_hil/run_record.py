"""Per-test HIL run records — the raw data behind the bug-catch delta.

Each pytest test outcome becomes one JSON line in ``ALTERIOM_HIL_RUN_LOG``:

    {"ts": "...", "suite": "painlessmesh", "test": "test_x", "verdict": "failed",
     "duration_s": 3.2, "failure_class": "real_bug", "hil_only_reason":
     "radio_timing", "firmware_sha": "abc123", "mode": "hardware", "boards": 3,
     "workflow_run_id": "...", "message": "AssertionError: ..."}

Records are append-only; the aggregator (:mod:`alteriom_hil.report`) reads a
directory of them across runs and computes the **bug-catch delta**: failures
of tests marked ``hil_only(reason=<category>)`` that compile-only CI could
not have caught.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

# Reasons a test can only be exercised on real hardware. A failure of a test
# tagged with one of these contributes to the bug-catch delta.
HIL_ONLY_REASONS = frozenset(
    {"radio_timing", "ota", "power", "soak", "multi_node", "peripheral"}
)

# Classes we bucket a failed run into so operators can subtract flakiness and
# rig outages from the delta.
FAILURE_CLASSES = frozenset({"real_bug", "flaky_hardware", "infra"})


@dataclass
class RunRecord:
    """One test outcome from one HIL (or sim) run."""

    ts: str
    suite: str
    test: str
    verdict: str  # passed | failed | skipped | error
    duration_s: float
    mode: str  # sim | hardware
    boards: int
    firmware_sha: str | None = None
    workflow_run_id: str | None = None
    failure_class: str | None = None  # only set when verdict != passed
    hil_only_reason: str | None = None
    message: str | None = None
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        d = {k: v for k, v in asdict(self).items() if v not in (None, {}, [])}
        return json.dumps(d, sort_keys=True)

    @classmethod
    def now(cls, **kwargs) -> "RunRecord":
        return cls(ts=datetime.now(timezone.utc).isoformat(), **kwargs)


def write_record(path: str | os.PathLike, record: RunRecord) -> None:
    """Append one record as a single JSON line to ``path``."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(record.to_json())
        f.write("\n")


def load_records(source: str | os.PathLike) -> Iterator[RunRecord]:
    """Yield RunRecords from a JSONL file or a directory of them."""
    p = Path(source)
    if not p.exists():
        return
    files: Iterable[Path]
    if p.is_dir():
        files = sorted(p.rglob("*.jsonl"))
    else:
        files = [p]
    for fp in files:
        with fp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                yield RunRecord(**{**_defaults(), **data})


def _defaults() -> dict:
    return {
        "ts": "",
        "suite": "",
        "test": "",
        "verdict": "",
        "duration_s": 0.0,
        "mode": "",
        "boards": 0,
    }
