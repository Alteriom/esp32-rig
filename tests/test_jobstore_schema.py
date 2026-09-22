"""The job store's schema is three parts, and a file from before knows it."""

from __future__ import annotations

import sqlite3

from alteriom_hil import jobstore
from alteriom_hil.jobstore import SCHEMA_VERSIONS, JobStore

CORE = {"jobs", "artifact_records", "evidence_records", "webhook_subs", "webhook_deliveries",
        "farm_settings", "events", "audit"}
RIG = {"board_holds", "board_verdicts"}
PORTAL = {"accounts", "github_logins", "sessions", "signin_codes", "oauth_states", "workers",
          "worker_commands", "enrollments", "rig_details"}


def _tables(db) -> set:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} - {"sqlite_sequence"}


def test_every_table_is_one_parts_and_each_part_makes_only_its_own(tmp_path):
    """A table on no side of the split is a decision nobody made -- the same
    rule the boundary test holds modules to."""
    made = {}
    for part, apply in (("core", JobStore._initialize_core), ("rig", JobStore._initialize_rig),
                        ("portal", JobStore._initialize_portal)):
        db = sqlite3.connect(tmp_path / f"{part}.sqlite3")
        # The rig's and the portal's parts add columns to `jobs` and back-fill
        # from `enrollments`, so each is applied over the core's.
        JobStore._initialize_core(None, db)
        before = _tables(db)
        apply(None, db)
        made[part] = _tables(db) - (before if part != "core" else set())
        db.close()
    assert made["core"] == CORE
    assert made["rig"] == RIG
    assert made["portal"] == PORTAL
    store = JobStore(tmp_path / "farm.sqlite3")
    with store.connect() as db:
        assert _tables(db) == CORE | RIG | PORTAL | {"schema_meta"}


def test_a_store_from_before_the_split_opens_as_it_was_plus_what_it_was_given(tmp_path):
    """Nothing is dropped and nothing rewritten: the same file, with a row
    per part saying what it has been brought up to."""
    path = tmp_path / "farm.sqlite3"
    first = JobStore(path)
    job = first.create("suite", {"profile": "painlessmesh", "ref": "main"}, tmp_path / "x.log")
    with first.connect() as db:
        db.execute("INSERT INTO rig_details (name, description, updated_by, updated_at, owner, visibility) "
                   "VALUES ('bench', 'a bench', 'me', '2026-09-22', 'me', 'public')")
        db.execute("DROP TABLE schema_meta")  # as a file from before this looks
        tables_before = _tables(db)
    again = JobStore(path)
    with again.connect() as db:
        assert _tables(db) == tables_before | {"schema_meta"}
        assert {tuple(row) for row in db.execute("SELECT component, version FROM schema_meta")} == set(SCHEMA_VERSIONS.items())
        assert [tuple(row) for row in db.execute("SELECT name, owner, visibility FROM rig_details")] == [("bench", "me", "public")]
    assert again.get(job["id"])["status"] == "queued"
    # And a third open changes nothing but the time it was brought up to.
    JobStore(path)
    with again.connect() as db:
        assert _tables(db) == tables_before | {"schema_meta"}


def test_a_parts_version_is_a_number_that_is_bumped_on_purpose():
    assert set(SCHEMA_VERSIONS) == {"core", "rig", "portal"}
    assert all(isinstance(v, int) and v >= 1 for v in SCHEMA_VERSIONS.values())
    assert jobstore.SCHEMA_VERSIONS is SCHEMA_VERSIONS
