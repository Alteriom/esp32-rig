"""The farm's job store: every run, board verdict, worker, account and
setting a farm keeps, in one SQLite file.

Both halves of the farm hold it -- a rig records the runs it does, a portal
the runs its rigs do and the accounts that watch them -- which is what makes
it core (docs/public-release-plan.md). It knows keys and webhooks, which are
core too, and nothing of a board, a port or a relay.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import webhooks as farm_webhooks


# How many wrong guesses a code survives. Six digits is a million; with
# five tries and ten minutes, a guesser's odds are one in two hundred
# thousand per code -- and the code is bound to the browser that asked for
# it besides, so a guess from anywhere else is not even a try.
SIGNIN_CODE_ATTEMPTS = 5


# What a portal asks a worker to do from its page, and the arguments each
# takes; the worker carries it out and reports (alteriom_hil.farm_node). Restart,
# logs and configure need the host's sudo and go through the node's control
# unit (rig/node-control.sh).
# The furthest into a list a page may ask to start. SQLite binds a 64-bit
# integer and raises OverflowError above it -- which is a 500 where the
# endpoint means to answer what was wrong -- and anything remotely near this is
# past the end of every table the farm keeps.
MAX_PAGE_OFFSET = 2 ** 31 - 1


# Attempts kept per subscription: enough to answer "is it arriving",
# not an archive of every event the farm ever sent.
WEBHOOK_DELIVERIES_KEPT = 200


# What a sealed link reads as in everything the portal returns, and what its
# stored copy becomes once the rig has it.
SEALED_PLACEHOLDER = "(sealed)"


# The console keeps the last few thousand lines: enough to read back through a
# deploy or a bring-up, not a second history of the farm.
EVENT_KEEP = 5000


EVENT_PRUNE_EVERY = 250


def max_runs(env) -> int:
    """How many runs may be in progress at once: the host's queue.concurrency.

    One unless the host says otherwise, which is how the farm always behaved:
    every run has the rig to itself.
    """
    try:
        value = int(env.get("ALTERIOM_HIL_MAX_RUNS", "1"))
    except (TypeError, ValueError):
        return 1
    return min(max(value, 1), 16)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed(start: str | None, end: str | None) -> float | None:
    """Seconds between two stored timestamps, or None if either is missing.

    A running job has no end: the dashboard counts up from ``started_at``
    itself rather than being told a duration that is stale the moment it
    is sent.
    """
    if not start or not end:
        return None
    try:
        began = datetime.fromisoformat(start)
        ended = datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return None
    return round((ended - began).total_seconds(), 1)


# One number per part of the schema. Every statement is idempotent (IF NOT
# EXISTS, a column added when absent), so a version says what a file has
# been brought up to, not what to do: bump it when a part changes shape.
SCHEMA_VERSIONS = {"core": 1, "rig": 1, "portal": 1}


class JobCancelled(Exception):
    """A running job was cancelled — by an operator, or superseded."""

    def __init__(self, summary: str, detail: str | None = None):
        super().__init__(summary)
        self.summary = summary
        self.detail = detail


class JobStore:
    # How coarsely a session's and its account's last_seen_at is kept. A
    # browser polls the dashboard every few seconds while a run is live
    # (web/app.js), and each authenticated request looks the session up; without
    # this, every one of those would take a write lock on both rows. connect()
    # opens a fresh connection with no WAL, so those writes serialize against
    # every reader. A minute-coarse last_seen_at is all anything reads it for.
    SEEN_WRITE_SECONDS = 60

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._initialize()

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def _initialize(self):
        """Every table, in three parts: what every farm keeps, what a rig
        keeps about its boards, and what a portal keeps about its rigs and
        accounts. All three are made on every farm today -- a file from
        before the split opens unchanged, and nothing is ever dropped -- and
        `schema_meta` says which parts this file has been given, so the day
        a rig's store is made without the portal's tables is a decision
        recorded in the file rather than a table quietly missing.
        """
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS schema_meta (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                applied_at TEXT NOT NULL
            )""")
            for component, apply in (("core", self._initialize_core), ("rig", self._initialize_rig),
                                     ("portal", self._initialize_portal)):
                apply(db)
                db.execute(
                    "INSERT INTO schema_meta (component, version, applied_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(component) DO UPDATE SET version=excluded.version, applied_at=excluded.applied_at",
                    (component, SCHEMA_VERSIONS[component], utcnow()),
                )

    def _initialize_core(self, db) -> None:
        """What every farm keeps: runs, artifacts and evidence, events, the audit, webhooks, settings."""
        db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
            request_json TEXT NOT NULL, result_json TEXT, log_path TEXT,
            progress_json TEXT NOT NULL DEFAULT '[]'
        )""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        if "progress_json" not in columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN progress_json TEXT NOT NULL DEFAULT '[]'"
            )
        # Queue order is priority first, then age. Every job is created
        # at priority 0; an operator promoting one raises it above the
        # rest, and the worker takes the highest, oldest queued job next.
        if "priority" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        db.execute("CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at DESC)")
        # The world page counts what finished this week, per rig, on
        # every poll: an index on when a run finished keeps that a
        # week's worth of rows rather than all of history.
        db.execute("CREATE INDEX IF NOT EXISTS jobs_finished ON jobs(finished_at)")
        # What the farm decided about a bundle, beside the bundle itself:
        # kept on purpose (pinned), or removed and when. The removal row
        # outlives the directory, so a run whose images were pruned says
        # so instead of listing a manifest that no longer opens.
        db.execute("""CREATE TABLE IF NOT EXISTS artifact_records (
            id TEXT PRIMARY KEY, pinned_at TEXT, pin_note TEXT,
            removed_at TEXT, removed_bytes INTEGER, removed_reason TEXT
        )""")
        # What retention removed of a run -- its evidence, its log -- and
        # when, so the run's page says so instead of offering files that
        # are gone. The job row itself is never deleted: it is the history.
        db.execute("""CREATE TABLE IF NOT EXISTS evidence_records (
            job_id TEXT NOT NULL, kind TEXT NOT NULL, removed_at TEXT NOT NULL,
            removed_bytes INTEGER, PRIMARY KEY (job_id, kind)
        )""")
        # Who changed what through the API: one row per request that could
        # change something, with the key's name and role and how the farm
        # answered -- a refusal included, since "who tried" is part of
        # the answer to "who deleted that bundle".
        # What the farm and its rigs did, in order, as a person watching
        # would want it: one line per thing that happened, with the rig it
        # happened on and the run or command it belongs to. The dashboard
        # tails it, so pressing a button has something to show while the
        # rig works -- a rig is a remote machine, and "nothing on screen"
        # is indistinguishable from "nothing happened".
        # The farm's own settings, as opposed to a rig's: on a portal
        # the configuration file is read-only (a ConfigMap), so what an
        # admin sets from the page lives here, on the portal's own volume.
        # Where the farm sends events to somebody else's software: a URL,
        # the secret its deliveries are signed with, and what it asked for.
        # Scope is "farm" -- the whole fleet, an admin's -- or the name of
        # one rig, which is that rig's owner's.
        db.execute("""CREATE TABLE IF NOT EXISTS webhook_subs (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, name TEXT NOT NULL,
            url TEXT NOT NULL, secret TEXT NOT NULL, events_json TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, timeout_ms INTEGER NOT NULL,
            max_retries INTEGER NOT NULL, backoff_ms INTEGER NOT NULL,
            created_at TEXT NOT NULL, created_by TEXT, updated_at TEXT, updated_by TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS webhook_subs_scope ON webhook_subs(scope)")
        # Every attempt, so "it is not arriving" is answerable from the
        # page rather than from a log nobody kept.
        db.execute("""CREATE TABLE IF NOT EXISTS webhook_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id TEXT NOT NULL,
            delivery_id TEXT NOT NULL, event TEXT NOT NULL, action TEXT,
            rig TEXT, summary TEXT, attempt INTEGER NOT NULL, state TEXT NOT NULL,
            status INTEGER, error TEXT, latency_ms INTEGER, at TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS webhook_deliveries_sub "
                   "ON webhook_deliveries(sub_id, id DESC)")
        db.execute("""CREATE TABLE IF NOT EXISTS farm_settings (
            name TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL,
            updated_by TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
            worker TEXT, source TEXT NOT NULL, kind TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info', text TEXT NOT NULL,
            job_id TEXT, command_id TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS events_worker ON events(worker, id)")
        db.execute("""CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
            key_name TEXT NOT NULL, role TEXT NOT NULL, method TEXT NOT NULL,
            path TEXT NOT NULL, status INTEGER NOT NULL, address TEXT
        )""")
        # Behind a portal: which worker a job was leased to, and what it
        # was granted, so a portal restart does not lose track of work a
        # node is still doing.
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        if "worker" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN worker TEXT")
        if "grant_json" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN grant_json TEXT")

    def _initialize_rig(self, db) -> None:
        """What a rig keeps about its boards: holds and health-check verdicts."""
        # A board held out of the pool: reserved by an operator for bench
        # work, or quarantined because the canary keeps failing checks of
        # its own. One row per board; releasing it deletes the row.
        db.execute("""CREATE TABLE IF NOT EXISTS board_holds (
            board_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT,
            since TEXT NOT NULL, by TEXT, job_id TEXT
        )""")
        # Every canary verdict per board, not only the last: what a
        # quarantine is decided from, and what a board's page shows.
        db.execute("""CREATE TABLE IF NOT EXISTS board_verdicts (
            job_id TEXT NOT NULL, board_id TEXT NOT NULL, checked_at TEXT NOT NULL,
            verdict TEXT NOT NULL, outcome TEXT NOT NULL, failed_json TEXT NOT NULL,
            PRIMARY KEY (job_id, board_id)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS board_verdicts_board ON board_verdicts(board_id, checked_at DESC)")

    def _initialize_portal(self, db) -> None:
        """What a portal keeps: accounts and sessions, workers and their commands, enrolments, rigs' details, releases' side."""
        # People who signed in. An account is made by GitHub or by an
        # emailed link and is the same account either way: a handle (the
        # shape of a key's name, so a rig it owns is owned as a key's
        # rig is), the email it verified, the GitHub it came from, and a
        # role -- `user`, or `admin` for the farm's own people.
        db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY, handle TEXT NOT NULL UNIQUE,
            email TEXT UNIQUE, email_verified_at TEXT,
            github_id INTEGER UNIQUE, github_login TEXT, display_name TEXT,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL, last_seen_at TEXT
        )""")
        # A browser's session: the digest of the cookie, never the cookie.
        db.execute("""CREATE TABLE IF NOT EXISTS github_logins (
            -- A GitHub login is a name its owner can change and another
            -- can then take; a role granted by login must not follow the
            -- name to a stranger. The first stable id to sign in under a
            -- login owns it while it stands.
            login TEXT PRIMARY KEY, github_id INTEGER NOT NULL, first_seen_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            digest TEXT PRIMARY KEY, account_id TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_seen_at TEXT,
            address TEXT
        )""")
        # A sign-in link, sent to an address and good once for a while;
        # and the state a GitHub round trip carries, good once, briefly.
        # A code, not a link: six digits typed into the page that asked
        # for them, good for minutes, bound to the browser that asked.
        # The link table it replaces is dropped: a one-time token from
        # before this build is worth nothing after it.
        db.execute("DROP TABLE IF EXISTS signin_links")
        db.execute("""CREATE TABLE IF NOT EXISTS signin_codes (
            digest TEXT PRIMARY KEY, email TEXT NOT NULL, next TEXT, browser TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, address TEXT,
            attempts INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY, next TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            -- The digest of a nonce the starting browser holds in a
            -- cookie: a state is only good in the browser that began.
            browser TEXT
        )""")
        if "browser" not in {row[1] for row in db.execute("PRAGMA table_info(oauth_states)")}:
            db.execute("ALTER TABLE oauth_states ADD COLUMN browser TEXT")
        # The workers a portal knows: what each offers and last reported.
        db.execute("""CREATE TABLE IF NOT EXISTS workers (
            name TEXT PRIMARY KEY, kind TEXT NOT NULL, version TEXT, max_runs INTEGER NOT NULL,
            profiles_json TEXT NOT NULL, inventory_json TEXT NOT NULL DEFAULT '{}',
            health_json TEXT, address TEXT, hello_at TEXT NOT NULL, seen_at TEXT NOT NULL
        )""")
        # Which commit each worker runs, and where it is with the release
        # the portal wants it on.
        columns = {row[1] for row in db.execute("PRAGMA table_info(workers)")}
        if "commit_sha" not in columns:
            db.execute("ALTER TABLE workers ADD COLUMN commit_sha TEXT")
        if "update_json" not in columns:
            db.execute("ALTER TABLE workers ADD COLUMN update_json TEXT")
        # The node's own host configuration, as its /api/v1/config says it,
        # so the portal's page shows what each node is set to.
        if "config_json" not in columns:
            db.execute("ALTER TABLE workers ADD COLUMN config_json TEXT")
            db.execute("ALTER TABLE workers ADD COLUMN config_at TEXT")
        # Drained by an operator: online, and given no new run.
        if "drained_json" not in columns:
            db.execute("ALTER TABLE workers ADD COLUMN drained_json TEXT")
        # What the portal asked each worker to do, and what came of it.
        db.execute("""CREATE TABLE IF NOT EXISTS worker_commands (
            id TEXT PRIMARY KEY, worker TEXT NOT NULL, kind TEXT NOT NULL,
            args_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL,
            result_json TEXT, detail TEXT, requested_by TEXT,
            created_at TEXT NOT NULL, sent_at TEXT, started_at TEXT, finished_at TEXT
        )""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(worker_commands)")}
        if "started_at" not in columns:
            db.execute("ALTER TABLE worker_commands ADD COLUMN started_at TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS worker_commands_worker ON worker_commands(worker, created_at DESC)")
        # Rigs an admin asked to add: a one-time token, by its digest only.
        db.execute("""CREATE TABLE IF NOT EXISTS enrollments (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE,
            note TEXT, created_by TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            status TEXT NOT NULL, used_at TEXT, used_from TEXT, hostname TEXT, cancelled_by TEXT
        )""")
        # A rig deleted before it joined leaves nothing; the first ones were
        # kept as "cancelled", which only cluttered the list.
        db.execute("DELETE FROM enrollments WHERE status='cancelled'")
        # What an operator says about a rig -- what it is, where it is --
        # by name, from when it is added until it is deleted.
        db.execute("""CREATE TABLE IF NOT EXISTS rig_details (
            name TEXT PRIMARY KEY, description TEXT, location TEXT, updated_by TEXT, updated_at TEXT,
            -- Whose rig this is: the name of a key. A person holding a
            -- `user` key administers their own rigs and nothing else; the
            -- farm has one admin. Null while a rig is the farm's own.
            owner TEXT,
            -- Who may see it: `private` (its owner and the farm), `public`
            -- (anyone, on the world page), `shared` (anyone may run on it).
            -- Null is private, which is what every rig starts as.
            visibility TEXT
        )""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(rig_details)")}
        if "owner" not in columns:
            db.execute("ALTER TABLE rig_details ADD COLUMN owner TEXT")
        if "visibility" not in columns:
            db.execute("ALTER TABLE rig_details ADD COLUMN visibility TEXT")
        # A rig added before it had details kept its note on its join: that
        # note is its description. Newest first, so the latest note wins;
        # a rig with details already keeps them.
        db.execute(
            "INSERT OR IGNORE INTO rig_details (name, description, updated_by, updated_at) "
            "SELECT name, note, created_by, created_at FROM enrollments "
            "WHERE note IS NOT NULL AND TRIM(note) != '' ORDER BY created_at DESC"
        )

    def set_lease(self, job_id: str, worker: str, grant: dict) -> None:
        with self.connect() as db:
            db.execute("UPDATE jobs SET worker=?, grant_json=? WHERE id=?", (worker, json.dumps(grant), job_id))

    def requeue(self, job_id: str) -> bool:
        """A running job back to queued: its worker never started it."""
        with self.connect() as db:
            changed = db.execute(
                "UPDATE jobs SET status='queued', started_at=NULL, worker=NULL, grant_json=NULL "
                "WHERE id=? AND status='running'",
                (job_id,),
            ).rowcount
        return changed == 1

    def running_on(self, worker: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE status='running' AND worker=?", (worker,)).fetchall()
        return [self._decode(row) for row in rows]

    def upsert_worker(self, name: str, kind: str, version: str | None, max_runs: int,
                      profiles: list[str], address: str | None, commit: str | None = None) -> None:
        now = utcnow()
        with self.connect() as db:
            db.execute(
                "INSERT INTO workers (name, kind, version, max_runs, profiles_json, address, hello_at, seen_at, commit_sha) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, "
                "version=excluded.version, max_runs=excluded.max_runs, profiles_json=excluded.profiles_json, "
                "address=excluded.address, hello_at=excluded.hello_at, seen_at=excluded.seen_at, "
                "commit_sha=excluded.commit_sha",
                (name, kind, version, max_runs, json.dumps(sorted(profiles)), address, now, now, commit),
            )

    def touch_worker(self, name: str, inventory: dict | None, health: dict | None, address: str | None,
                     update: dict | None = None, config: dict | None = None) -> bool:
        fields, values = ["seen_at=?", "address=?"], [utcnow(), address]
        if config is not None:
            fields += ["config_json=?", "config_at=?"]
            values += [json.dumps(config), utcnow()]
        if inventory is not None:
            fields.append("inventory_json=?")
            values.append(json.dumps(inventory))
        if health is not None:
            fields.append("health_json=?")
            values.append(json.dumps(health))
        if update is not None:
            fields.append("update_json=?")
            values.append(json.dumps(update))
        values.append(name)
        with self.connect() as db:
            return db.execute(f"UPDATE workers SET {', '.join(fields)} WHERE name=?", values).rowcount == 1

    def workers(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM workers ORDER BY name").fetchall()
        found = []
        for row in rows:
            item = dict(row)
            item["profiles"] = json.loads(item.pop("profiles_json") or "[]")
            item["inventory"] = json.loads(item.pop("inventory_json") or "{}")
            raw = item.pop("health_json")
            item["health"] = json.loads(raw) if raw else None
            item["commit"] = item.pop("commit_sha", None)
            raw = item.pop("update_json", None)
            item["update"] = json.loads(raw) if raw else None
            raw = item.pop("config_json", None)
            item["config"] = json.loads(raw) if raw else None
            raw = item.pop("drained_json", None)
            item["drained"] = json.loads(raw) if raw else None
            found.append(item)
        return found

    def set_drained(self, name: str, drained: dict | None) -> bool:
        with self.connect() as db:
            return db.execute(
                "UPDATE workers SET drained_json=? WHERE name=?",
                (json.dumps(drained) if drained else None, name),
            ).rowcount == 1

    def delete_worker(self, name: str) -> bool:
        with self.connect() as db:
            db.execute("DELETE FROM worker_commands WHERE worker=?", (name,))
            return db.execute("DELETE FROM workers WHERE name=?", (name,)).rowcount == 1

    @staticmethod
    def _decode_command(row: sqlite3.Row, sealed: bool = False) -> dict:
        """A command as the API returns it. A sealed link is never returned --
        ciphertext or not, it is the rig's to read -- except, with
        ``sealed``, in the heartbeat answer that delivers it."""
        item = dict(row)
        item["args"] = json.loads(item.pop("args_json") or "{}")
        raw = item.pop("result_json")
        item["result"] = json.loads(raw) if raw else None
        if not sealed and "sealed" in item["args"]:
            item["args"] = {**item["args"], "sealed": SEALED_PLACEHOLDER}
        return item

    @staticmethod
    def _forget_sealed(db: sqlite3.Connection, where: str, params: tuple) -> None:
        """Blank the stored ciphertext of the provider_set commands ``where``
        names: once the rig has it, or it will never be delivered, the portal
        keeps no copy."""
        rows = db.execute(
            f"SELECT id, args_json FROM worker_commands "
            f"WHERE kind IN ('provider_set', 'notify_set') AND {where}",
                          params).fetchall()
        for row in rows:
            args = json.loads(row["args_json"] or "{}")
            if args.get("sealed") not in (None, SEALED_PLACEHOLDER):
                args["sealed"] = SEALED_PLACEHOLDER
                db.execute("UPDATE worker_commands SET args_json=? WHERE id=?", (json.dumps(args), row["id"]))

    def add_command(self, worker: str, kind: str, args: dict, by: str | None) -> dict:
        command_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO worker_commands (id, worker, kind, args_json, status, requested_by, created_at) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                (command_id, worker, kind, json.dumps(args), by, utcnow()),
            )
        return self.command(command_id)

    # ---- where the farm sends events -------------------------------------------

    def webhook_subs(self, scope: str | None = None) -> list[dict]:
        """Subscriptions, oldest first. The secret is in the row: nothing that
        renders one to a caller passes it on."""
        with self.connect() as db:
            if scope is None:
                rows = db.execute("SELECT * FROM webhook_subs ORDER BY created_at").fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM webhook_subs WHERE scope=? ORDER BY created_at", (scope,)
                ).fetchall()
        return [self._decode_sub(row) for row in rows]

    def webhook_sub(self, sub_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM webhook_subs WHERE id=?", (sub_id,)).fetchone()
        return self._decode_sub(row) if row else None

    @staticmethod
    def _decode_sub(row) -> dict:
        sub = {key: row[key] for key in row.keys()}
        sub["active"] = bool(sub["active"])
        try:
            sub["events"] = json.loads(sub.pop("events_json"))
        except (TypeError, ValueError):
            sub.pop("events_json", None)
            sub["events"] = [farm_webhooks.WILDCARD]
        return sub

    def add_webhook_sub(self, sub: dict) -> dict:
        with self.connect() as db:
            db.execute(
                "INSERT INTO webhook_subs (id, scope, name, url, secret, events_json, active, "
                "timeout_ms, max_retries, backoff_ms, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sub["id"], sub["scope"], sub["name"], sub["url"], sub["secret"],
                 json.dumps(sub["events"]), int(bool(sub.get("active", True))),
                 sub["timeout_ms"], sub["max_retries"], sub["backoff_ms"],
                 utcnow(), sub.get("created_by")),
            )
        return self.webhook_sub(sub["id"])

    def update_webhook_sub(self, sub_id: str, fields: dict, by: str | None) -> dict | None:
        columns, values = [], []
        for key in ("name", "url", "secret", "active", "timeout_ms", "max_retries", "backoff_ms"):
            if key in fields:
                columns.append(f"{key}=?")
                values.append(int(fields[key]) if key == "active" else fields[key])
        if "events" in fields:
            columns.append("events_json=?")
            values.append(json.dumps(fields["events"]))
        if not columns:
            return self.webhook_sub(sub_id)
        columns.extend(("updated_at=?", "updated_by=?"))
        values.extend((utcnow(), by, sub_id))
        with self.connect() as db:
            db.execute(f"UPDATE webhook_subs SET {', '.join(columns)} WHERE id=?", values)
        return self.webhook_sub(sub_id)

    def remove_webhook_sub(self, sub_id: str) -> bool:
        with self.connect() as db:
            gone = db.execute("DELETE FROM webhook_subs WHERE id=?", (sub_id,)).rowcount
            db.execute("DELETE FROM webhook_deliveries WHERE sub_id=?", (sub_id,))
        return bool(gone)

    def record_webhook_delivery(self, sub_id: str, event, outcome: dict) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO webhook_deliveries (sub_id, delivery_id, event, action, rig, summary, "
                "attempt, state, status, error, latency_ms, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sub_id, outcome.get("delivery_id", ""), event.event, event.action, event.rig,
                 event.summary[:200], outcome.get("attempt", 1), outcome.get("state", "failed"),
                 outcome.get("status"), (outcome.get("error") or None), outcome.get("latency_ms"),
                 utcnow()),
            )
            # A delivery log is a debugging aid, not an archive: the newest few
            # hundred per subscription answer "is it arriving", and the rest
            # would grow without end on a farm that never looks.
            db.execute(
                "DELETE FROM webhook_deliveries WHERE sub_id=? AND id NOT IN "
                "(SELECT id FROM webhook_deliveries WHERE sub_id=? ORDER BY id DESC LIMIT ?)",
                (sub_id, sub_id, WEBHOOK_DELIVERIES_KEPT),
            )

    def webhook_deliveries(self, sub_id: str, limit: int = 25, offset: int = 0) -> dict:
        limit = max(1, min(int(limit), 200))
        offset = max(0, min(int(offset), MAX_PAGE_OFFSET))
        with self.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM webhook_deliveries WHERE sub_id=?", (sub_id,)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM webhook_deliveries WHERE sub_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (sub_id, limit, offset),
            ).fetchall()
        return {"deliveries": [{key: row[key] for key in row.keys()} for row in rows],
                "total": total, "limit": limit, "offset": offset}

    def farm_setting(self, name: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM farm_settings WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        return {**json.loads(row["value_json"]), "updated_at": row["updated_at"],
                "updated_by": row["updated_by"]}

    def set_farm_setting(self, name: str, value: dict | None, by: str | None) -> None:
        with self.connect() as db:
            if value is None:
                db.execute("DELETE FROM farm_settings WHERE name=?", (name,))
                return
            db.execute(
                "INSERT INTO farm_settings (name, value_json, updated_at, updated_by) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (name, json.dumps(value), utcnow(), by),
            )

    def record_event(self, source: str, kind: str, text: str, *, worker: str | None = None,
                     level: str = "info", job_id: str | None = None,
                     command_id: str | None = None) -> int:
        """Append one console line. Never raises into its caller: a line
        nobody could write is not a reason to fail the thing it describes."""
        try:
            with self.connect() as db:
                cursor = db.execute(
                    "INSERT INTO events (at, worker, source, kind, level, text, job_id, command_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (utcnow(), worker, source, kind, level, text[:2000], job_id, command_id),
                )
                written = int(cursor.lastrowid or 0)
                # A console is a tail, not a record: the run, the command and
                # the audit log are where things are kept. Pruned in blocks so
                # the delete is not on every line.
                if written % EVENT_PRUNE_EVERY == 0:
                    db.execute("DELETE FROM events WHERE id <= ?", (written - EVENT_KEEP,))
                return written
        except sqlite3.Error:
            return 0

    def events_since(self, after: int = 0, worker: str | None = None, limit: int = 200) -> dict:
        """The lines after a cursor, oldest first, and the newest id there is.

        The cursor is the id the client last saw, so a client that was away
        gets what it missed rather than a snapshot, and one that has fallen
        further behind than `limit` is told (`missed`) rather than quietly
        shown a gap.
        """
        limit = max(1, min(int(limit), 500))
        with self.connect() as db:
            newest = int(db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
            oldest = int(db.execute("SELECT COALESCE(MIN(id), 0) FROM events").fetchone()[0])
            if worker:
                rows = db.execute(
                    "SELECT * FROM events WHERE id > ? AND (worker = ? OR worker IS NULL) "
                    "ORDER BY id LIMIT ?", (after, worker, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after, limit)
                ).fetchall()
        events = [dict(row) for row in rows]
        return {
            "events": events,
            "cursor": events[-1]["id"] if events else max(after, 0),
            "newest": newest,
            # Lines that were pruned before this client asked for them.
            "missed": max(0, oldest - after - 1) if after and oldest > after + 1 else 0,
        }

    def start_command(self, command_id: str) -> bool:
        """The worker has begun this command. `sent` says the portal handed it
        over; this says the rig acted on it, which is the difference between
        "asked" and "happening" while something takes a minute."""
        with self.connect() as db:
            changed = db.execute(
                "UPDATE worker_commands SET status='running', started_at=? WHERE id=? AND status='sent'",
                (utcnow(), command_id),
            ).rowcount
        return bool(changed)

    def command(self, command_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM worker_commands WHERE id=?", (command_id,)).fetchone()
        return self._decode_command(row) if row else None

    def take_commands(self, worker: str) -> list[dict]:
        """The commands queued for a worker, marked given."""
        now = utcnow()
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM worker_commands WHERE worker=? AND status='queued' ORDER BY created_at", (worker,)
            ).fetchall()
            for row in rows:
                db.execute("UPDATE worker_commands SET status='sent', sent_at=? WHERE id=?", (now, row["id"]))
                self._forget_sealed(db, "id=?", (row["id"],))
        # The rows as read before they were blanked: this answer delivers them.
        return [self._decode_command(row, sealed=True) for row in rows]

    def finish_command(self, command_id: str, status: str, result: dict | None, detail: str | None) -> bool:
        with self.connect() as db:
            finished = db.execute(
                "UPDATE worker_commands SET status=?, result_json=?, detail=?, finished_at=? "
                "WHERE id=? AND status IN ('queued', 'sent', 'running')",
                (status, json.dumps(result) if result is not None else None, detail, utcnow(), command_id),
            ).rowcount == 1
            self._forget_sealed(db, "id=?", (command_id,))
            return finished

    # ---- rigs being added ------------------------------------------------------------------
    def add_enrollment(self, name: str, token_sha256: str, note: str | None, by: str | None, expires_at: str) -> dict:
        enrollment_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO enrollments (id, name, token_sha256, note, created_by, created_at, expires_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting')",
                (enrollment_id, name, token_sha256, note, by, utcnow(), expires_at),
            )
        return self.enrollment(enrollment_id)

    def enrollment(self, enrollment_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM enrollments WHERE id=?", (enrollment_id,)).fetchone()
        return self._decode_enrollment(row) if row else None

    def enrollment_by_token(self, token_sha256: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM enrollments WHERE token_sha256=?", (token_sha256,)).fetchone()
        return self._decode_enrollment(row) if row else None

    def claim_enrollment(self, enrollment_id: str, now: str, address: str | None, hostname: str | None) -> bool:
        """Mark a waiting, unexpired enrollment used -- once: of two callers
        with the same token, one gets it."""
        with self.connect() as db:
            return db.execute(
                "UPDATE enrollments SET status='used', used_at=?, used_from=?, hostname=? "
                "WHERE id=? AND status='waiting' AND expires_at > ?",
                (now, address, hostname, enrollment_id, now),
            ).rowcount == 1

    def unclaim_enrollment(self, enrollment_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE enrollments SET status='waiting', used_at=NULL, used_from=NULL, hostname=NULL WHERE id=?",
                       (enrollment_id,))

    def renew_enrollment(self, enrollment_id: str, token_sha256: str, expires_at: str) -> None:
        """A new token for a rig not yet joined: the old one stops working."""
        with self.connect() as db:
            db.execute(
                "UPDATE enrollments SET token_sha256=?, expires_at=?, status='waiting', used_at=NULL, "
                "used_from=NULL, hostname=NULL WHERE id=?",
                (token_sha256, expires_at, enrollment_id),
            )

    def rename_enrollments(self, name: str, new_name: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE enrollments SET name=? WHERE name=?", (new_name, name))
            db.execute("UPDATE rig_details SET name=? WHERE name=?", (new_name, name))

    def delete_enrollments(self, name: str) -> int:
        with self.connect() as db:
            return db.execute("DELETE FROM enrollments WHERE name=?", (name,)).rowcount

    def recent_enrollments(self, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM enrollments ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._decode_enrollment(row) for row in rows]

    def pending_enrollment_names(self) -> set[str]:
        """Every rig still being added, by name, all of them: a join that
        is waiting, its token expired or not, since an expired one is
        renewed under the same name by a new join command. The listings
        look at the newest so many; a namespace has no such limit.
        (Cancelled joins are deleted at startup; a used one holds a key,
        or was left behind and is not a rig being added.)"""
        with self.connect() as db:
            rows = db.execute("SELECT DISTINCT name FROM enrollments WHERE status='waiting'").fetchall()
        return {row[0] for row in rows}

    def rig_details(self) -> dict[str, dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM rig_details").fetchall()
        return {row["name"]: dict(row) for row in rows}

    def set_rig_owner(self, name: str, owner: str | None, by: str | None) -> None:
        """Whose rig this is. `None` gives it back to the farm: an unowned rig
        is an admin's, which is what a rig is until somebody is named."""
        with self.connect() as db:
            db.execute(
                "INSERT INTO rig_details (name, owner, updated_by, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (name, owner, by, utcnow()),
            )

    def set_rig_visibility(self, name: str, visibility: str, by: str | None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO rig_details (name, visibility, updated_by, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET visibility=excluded.visibility, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (name, visibility, by, utcnow()),
            )

    def set_rig_details(self, name: str, description: str | None, location: str | None, by: str | None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO rig_details (name, description, location, updated_by, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET description=excluded.description, location=excluded.location, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (name, description, location, by, utcnow()),
            )

    def delete_rig_details(self, name: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM rig_details WHERE name=?", (name,))

    # ---- accounts: people who signed in ----------------------------------------
    ACCOUNT_LOOKUPS = ("id", "handle", "email", "github_id")

    def account(self, by: str, value: object) -> dict | None:
        if by not in self.ACCOUNT_LOOKUPS:
            raise ValueError(f"accounts are looked up by {', '.join(self.ACCOUNT_LOOKUPS)}")
        with self.connect() as db:
            row = db.execute(f"SELECT * FROM accounts WHERE {by}=?", (value,)).fetchone()
        return dict(row) if row else None

    def create_account(self, handle: str, *, email: str | None = None, email_verified: bool = False,
                       github_id: int | None = None, github_login: str | None = None,
                       display_name: str | None = None, role: str = "user") -> dict:
        now = utcnow()
        account_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO accounts (id, handle, email, email_verified_at, github_id, github_login, "
                "display_name, role, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, handle, email, now if email and email_verified else None,
                 github_id, github_login, display_name, role, now, now),
            )
        return self.account("id", account_id)

    def update_account(self, account_id: str, **fields) -> None:
        allowed = {"email", "email_verified_at", "github_id", "github_login", "display_name", "role", "last_seen_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"an account has no {', '.join(sorted(unknown))}")
        if not fields:
            return
        with self.connect() as db:
            db.execute(f"UPDATE accounts SET {', '.join(f'{key}=?' for key in fields)} WHERE id=?",
                       (*fields.values(), account_id))

    def claim_github_login(self, login: str, github_id: int, now: str) -> None:
        """Record that this id has signed in under this login, the first
        time it does: later sign-ins under the same login by another id do
        not change who owns it."""
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO github_logins (login, github_id, first_seen_at) VALUES (?, ?, ?)",
                       (login, github_id, now))

    def github_login_owner(self, login: str) -> int | None:
        """The stable id that owns this login, or None if none has claimed it."""
        with self.connect() as db:
            row = db.execute("SELECT github_id FROM github_logins WHERE login=?", (login,)).fetchone()
        return row[0] if row else None

    def merge_accounts(self, keep_id: str, drop_id: str, now: str) -> None:
        """Two rows, one person: the sessions, the rigs, and the running
        runs of the dropped account become the survivor's, and the survivor
        takes the verified address the dropped row proved. One transaction,
        so a reader sees one account or the other, never a half-merge."""
        with self.connect() as db:
            keep = db.execute("SELECT * FROM accounts WHERE id=?", (keep_id,)).fetchone()
            drop = db.execute("SELECT * FROM accounts WHERE id=?", (drop_id,)).fetchone()
            if keep is None or drop is None or keep_id == drop_id:
                return
            keep, drop = dict(keep), dict(drop)
            db.execute("UPDATE sessions SET account_id=? WHERE account_id=?", (keep_id, drop_id))
            # Who owns a rig and who may cancel a run go by the handle: the
            # dropped handle's rigs and its queued or running runs move to
            # the survivor's, so nothing it held is stranded or orphaned.
            db.execute("UPDATE rig_details SET owner=? WHERE owner=?", (keep["handle"], drop["handle"]))
            # Every run, not only the ones still going. `submitted_by` is
            # what says a finished run is yours -- its detail, its log, its
            # artifacts -- so leaving the dropped handle on the history would
            # hide this person's own past work from them, and leave that
            # handle referenced by rows nobody answers for.
            for row in db.execute(
                    "SELECT id, request_json FROM jobs "
                    "WHERE json_extract(request_json,'$.submitted_by') = ?",
                    (drop["handle"],)).fetchall():
                request = json.loads(row["request_json"])
                request["submitted_by"] = keep["handle"]
                db.execute("UPDATE jobs SET request_json=? WHERE id=?", (json.dumps(request), row["id"]))
            email = keep["email"] or drop["email"]
            verified = keep["email_verified_at"] or drop["email_verified_at"]
            # The dropped row goes first: its address is unique across
            # accounts, so the survivor can take it only once it is gone.
            db.execute("DELETE FROM accounts WHERE id=?", (drop_id,))
            if keep["email"] is None and email is not None:
                db.execute("UPDATE accounts SET email=?, email_verified_at=? WHERE id=?", (email, verified, keep_id))

    def handle_taken(self, handle: str) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM accounts WHERE handle=?", (handle,)).fetchone() is not None

    def create_session(self, token_digest: str, account_id: str, expires_at: str, address: str | None) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO sessions (digest, account_id, created_at, expires_at, last_seen_at, address) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (token_digest, account_id, utcnow(), expires_at, utcnow(), address))

    def session_account(self, token_digest: str, now: str) -> dict | None:
        """The account behind a live session, and that it was seen now.

        last_seen_at is advanced at most once every ``SEEN_WRITE_SECONDS``: a
        request that finds it already fresh writes nothing, so a burst of polls
        does not take a write lock on the session and account rows every time.
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT a.* FROM sessions s JOIN accounts a ON a.id = s.account_id "
                "WHERE s.digest=? AND s.expires_at > ?", (token_digest, now)).fetchone()
            if row is None:
                return None
            elapsed = _elapsed(row["last_seen_at"], now)
            if row["last_seen_at"] is None or elapsed is None or elapsed >= self.SEEN_WRITE_SECONDS:
                db.execute("UPDATE sessions SET last_seen_at=? WHERE digest=?", (now, token_digest))
                db.execute("UPDATE accounts SET last_seen_at=? WHERE id=?", (now, row["id"]))
        return dict(row)

    def delete_session(self, token_digest: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE digest=?", (token_digest,))

    def account_sessions(self, account_id: str, now: str) -> list[dict]:
        """Every live session of one account, most recently seen first.

        Expired rows are left to prune_signins rather than shown: a session
        that has run out is not somewhere the account is still signed in, and
        listing it would invite somebody to revoke something already gone.
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT digest, created_at, expires_at, last_seen_at, address FROM sessions "
                "WHERE account_id=? AND expires_at > ? "
                "ORDER BY COALESCE(last_seen_at, created_at) DESC", (account_id, now)).fetchall()
        return [dict(row) for row in rows]

    def delete_account_session(self, account_id: str, digest: str) -> int:
        """Revoke one session of this account. Scoped to the account, so a
        digest belonging to somebody else matches nothing rather than
        signing a stranger out."""
        with self.connect() as db:
            return db.execute("DELETE FROM sessions WHERE account_id=? AND digest=?",
                              (account_id, digest)).rowcount

    def delete_other_sessions(self, account_id: str, keep: str) -> int:
        """Sign out everywhere else, keeping the browser asking."""
        with self.connect() as db:
            return db.execute("DELETE FROM sessions WHERE account_id=? AND digest<>?",
                              (account_id, keep)).rowcount

    def create_signin_code(self, code_digest: str, email: str, browser_digest: str, expires_at: str,
                           address: str | None, next_path: str | None) -> None:
        """One code for this address, replacing any still live: asking again
        is asking for a new code, and the old one stops working -- so a code
        an attacker asked for cannot outlive the one the person then asked
        for themselves."""
        with self.connect() as db:
            db.execute("DELETE FROM signin_codes WHERE email=?", (email,))
            db.execute("INSERT INTO signin_codes (digest, email, next, browser, created_at, expires_at, address) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (code_digest, email, next_path, browser_digest, utcnow(), expires_at, address))

    def consume_signin_code(self, email: str, code_digest: str, browser_digest: str, now: str,
                            attempts: int = SIGNIN_CODE_ATTEMPTS) -> dict | None:
        """The code's row if it is live, was asked for by this browser and
        is the code that was sent -- and gone from now on if it was.

        A wrong guess counts against the code, and the code goes after
        `attempts` of them: a guesser is not given a million tries at ten
        minutes each. A right code from the wrong browser is a wrong guess,
        and is answered no differently, so nothing here tells a caller
        which half they got right.
        """
        with self.connect() as db:
            row = db.execute("SELECT * FROM signin_codes WHERE email=? AND expires_at > ?",
                             (email, now)).fetchone()
            if row is None:
                return None
            right = hmac.compare_digest(row["digest"], code_digest) \
                and hmac.compare_digest(row["browser"], browser_digest)
            if right:
                # The delete is the test. Two requests presenting the right
                # code at once both read it live -- the SELECT takes no lock
                # -- and both used to be let in; only the one whose delete
                # changed the row has the code, as consume_oauth_state has
                # always done. Seen as "2 == 1" on the Python 3.9 lane.
                spent = db.execute("DELETE FROM signin_codes WHERE digest=? AND email=?",
                                   (row["digest"], email)).rowcount
                return dict(row) if spent == 1 else None
            # A wrong guess counts in the row, not in this reader's copy of
            # it, so concurrent wrong guesses each count and the cap holds.
            db.execute("UPDATE signin_codes SET attempts=attempts+1 WHERE digest=?", (row["digest"],))
            db.execute("DELETE FROM signin_codes WHERE digest=? AND attempts>=?", (row["digest"], attempts))
            return None

    def create_oauth_state(self, state: str, browser_digest: str, expires_at: str, next_path: str | None) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO oauth_states (state, browser, next, created_at, expires_at) "
                       "VALUES (?, ?, ?, ?, ?)", (state, browser_digest, next_path, utcnow(), expires_at))

    def consume_oauth_state(self, state: str, browser_digest: str, now: str) -> dict | None:
        """The state's row if it is live, was started by this browser, and
        was not used before -- and gone from now on, whichever it was: a
        state is spent by being presented, right or wrong."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM oauth_states WHERE state=?", (state,)).fetchone()
            spent = db.execute("DELETE FROM oauth_states WHERE state=?", (state,)).rowcount
        if row is None or spent != 1 or row["expires_at"] <= now:
            return None
        if not row["browser"] or not hmac.compare_digest(row["browser"], browser_digest):
            return None
        return dict(row)

    def prune_signins(self, now: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            db.execute("DELETE FROM signin_codes WHERE expires_at <= ?", (now,))
            db.execute("DELETE FROM oauth_states WHERE expires_at <= ?", (now,))

    @staticmethod
    def _decode_enrollment(row) -> dict:
        # Never the token's digest: nothing reads it back but the lookup.
        return {key: row[key] for key in row.keys() if key != "token_sha256"}

    def expire_commands(self, older_than: str) -> int:
        with self.connect() as db:
            expired = db.execute(
                "UPDATE worker_commands SET status='expired', finished_at=?, "
                "detail='the worker never reported on it' "
                "WHERE status IN ('queued', 'sent', 'running') AND created_at < ?",
                (utcnow(), older_than),
            ).rowcount
            self._forget_sealed(db, "status NOT IN ('queued', 'sent', 'running')", ())
            return expired

    def recent_commands(self, worker: str, limit: int = 25) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM worker_commands WHERE worker=? ORDER BY created_at DESC LIMIT ?",
                (worker, max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._decode_command(row) for row in rows]

    def commands_page(self, worker: str, limit: int = 25, offset: int = 0) -> dict:
        """A page of everything ever asked of one rig, newest first.

        A rig's page shows the last few; the question "what has been done to
        this rig, and by whom" is a different one, asked of the whole history
        and answered a page at a time.
        """
        limit = max(1, min(int(limit), 200))
        offset = max(0, min(int(offset), MAX_PAGE_OFFSET))
        with self.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM worker_commands WHERE worker=?", (worker,)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM worker_commands WHERE worker=? ORDER BY created_at DESC "
                "LIMIT ? OFFSET ?",
                (worker, limit, offset),
            ).fetchall()
        return {"commands": [self._decode_command(row) for row in rows],
                "total": total, "limit": limit, "offset": offset}

    def holds(self) -> dict[str, dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM board_holds").fetchall()
        return {row["board_id"]: {key: row[key] for key in row.keys() if key != "board_id"} for row in rows}

    def set_hold(self, board_id: str, state: str, reason: str | None, by: str | None,
                 job_id: str | None = None) -> dict:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO board_holds (board_id, state, reason, since, by, job_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (board_id, state, reason, utcnow(), by, job_id),
            )
        return self.holds()[board_id]

    def release_hold(self, board_id: str) -> dict | None:
        held = self.holds().get(board_id)
        if held is not None:
            with self.connect() as db:
                db.execute("DELETE FROM board_holds WHERE board_id=?", (board_id,))
        return held

    def record_board_verdict(self, job_id: str, board_id: str, checked_at: str, verdict: str,
                             outcome: str, failed: list[str]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO board_verdicts (job_id, board_id, checked_at, verdict, outcome, failed_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, board_id, checked_at, verdict, outcome, json.dumps(failed)),
            )

    def board_verdicts(self, board_id: str, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM board_verdicts WHERE board_id=? ORDER BY checked_at DESC, rowid DESC LIMIT ?",
                (board_id, limit),
            ).fetchall()
        return [
            {"job_id": row["job_id"], "checked_at": row["checked_at"], "verdict": row["verdict"],
             "outcome": row["outcome"], "failed": json.loads(row["failed_json"])}
            for row in rows
        ]

    def record_evidence_removal(self, job_id: str, kind: str, removed_bytes: int | None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO evidence_records (job_id, kind, removed_at, removed_bytes) VALUES (?, ?, ?, ?)",
                (job_id, kind, utcnow(), removed_bytes),
            )

    def delete_job(self, job_id: str) -> bool:
        """Forget a run: its record, its evidence notes, its board verdicts.
        The caller has removed what was on disk. Only a finished run is
        deletable; a queued or running one is cancelled first."""
        with self.connect() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return False
            if row["status"] in ("queued", "running"):
                raise ValueError(f"run {job_id[:8]} is {row['status']}; cancel it before deleting it")
            db.execute("DELETE FROM evidence_records WHERE job_id=?", (job_id,))
            db.execute("DELETE FROM board_verdicts WHERE job_id=?", (job_id,))
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return True

    def evidence_removals(self, job_id: str) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM evidence_records WHERE job_id=?", (job_id,)).fetchall()
        return {row["kind"]: {"removed_at": row["removed_at"], "bytes": row["removed_bytes"]} for row in rows}

    def record_audit(self, key_name: str, role: str, method: str, path: str, status: int,
                     address: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit (at, key_name, role, method, path, status, address) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (utcnow(), key_name, role, method, path, int(status), address),
            )

    # What the audit shows: rows a worker's routine calls left before they
    # stopped being recorded are passed over, not deleted.
    AUDIT_SHOWN = (
        "NOT (role = 'node' AND (path LIKE '%/hello' OR path LIKE '%/heartbeat' OR path LIKE '%/lease' "
        "OR path LIKE '%/stages' OR path LIKE '%/log' OR path LIKE '%/history/%'))"
    )

    def audit_page(self, limit: int = 50, offset: int = 0) -> dict:
        limit = max(1, min(int(limit), 500))
        offset = max(0, min(int(offset), MAX_PAGE_OFFSET))
        with self.connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM audit WHERE {self.AUDIT_SHOWN}").fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM audit WHERE {self.AUDIT_SHOWN} ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return {"entries": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def create(
        self, kind: str, request: dict, log_path: Path, progress: list[dict] | None = None,
        job_id: str | None = None,
    ) -> dict:
        job = {
            # A node keeps the id its portal gave the job.
            "id": job_id or uuid.uuid4().hex,
            "kind": kind,
            "status": "queued",
            "created_at": utcnow(),
            "request": request,
            "log_path": str(log_path),
            "progress": progress or [],
        }
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs(id,kind,status,created_at,request_json,log_path,progress_json) VALUES(?,?,?,?,?,?,?)",
                (
                    job["id"], kind, job["status"], job["created_at"],
                    json.dumps(request), str(log_path), json.dumps(job["progress"]),
                ),
            )
        return job

    def import_job(self, job: dict, worker: str, log_path: Path) -> bool:
        """A finished job another farm ran, as it was; False when this store has it."""
        with self.connect() as db:
            return db.execute(
                "INSERT OR IGNORE INTO jobs(id,kind,status,created_at,started_at,finished_at,request_json,"
                "result_json,log_path,progress_json,priority,worker) VALUES(?,?,?,?,?,?,?,?,?,?,0,?)",
                (
                    job["id"], job["kind"], job["status"], job["created_at"], job.get("started_at"),
                    job.get("finished_at"), json.dumps(job["request"]),
                    json.dumps(job["result"]) if job.get("result") is not None else None,
                    str(log_path), json.dumps(job.get("progress") or []), worker,
                ),
            ).rowcount == 1

    def by_prefix(self, prefix: str) -> dict | None:
        """The one job whose id starts with this, or None if it names none
        or more than one. Short ids appear in the sentences the farm writes
        about itself, and reading one back has to be unambiguous."""
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE id LIKE ? LIMIT 2", (prefix + "%",)).fetchall()
        return self._decode(rows[0]) if len(rows) == 1 else None

    def submitters(self) -> set[str]:
        """Every name that has ever submitted a run on this farm.

        Names, not principals, which is exactly the trouble: `submitted_by`
        is what makes a finished run somebody's, so a name appearing here
        must never be handed to a different person (see `_free_handle`).
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT DISTINCT json_extract(request_json,'$.submitted_by') FROM jobs"
            ).fetchall()
        return {row[0] for row in rows if row[0]}

    def known_jobs(self, ids: list[str]) -> set[str]:
        found: set[str] = set()
        ids = list(ids)
        with self.connect() as db:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" * len(chunk))
                found |= {row[0] for row in db.execute(f"SELECT id FROM jobs WHERE id IN ({marks})", chunk)}
        return found

    def update(self, job_id: str, status: str, result: dict | None = None):
        fields = ["status=?"]
        values: list[object] = [status]
        if status == "running":
            fields.append("started_at=?")
            values.append(utcnow())
        if status in ("passed", "failed", "cancelled"):
            fields.extend(("finished_at=?", "result_json=?"))
            values.extend((utcnow(), json.dumps(result or {})))
        values.append(job_id)
        with self.connect() as db:
            db.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", values)

    def update_progress(self, job_id: str, progress: list[dict]):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET progress_json=? WHERE id=?",
                (json.dumps(progress), job_id),
            )

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json"))
        raw_result = item.pop("result_json")
        item["result"] = json.loads(raw_result) if raw_result else None
        item["progress"] = json.loads(item.pop("progress_json") or "[]")
        raw_grant = item.pop("grant_json", None)
        item["grant"] = json.loads(raw_grant) if raw_grant else None
        # How long it waited and how long it ran. A physical suite takes tens
        # of minutes, so "is this normal or wedged?" is the first question an
        # operator asks, and the timestamps alone do not answer it.
        item["queued_seconds"] = _elapsed(item.get("created_at"), item.get("started_at"))
        item["duration_seconds"] = _elapsed(item.get("started_at"), item.get("finished_at"))
        return item

    @staticmethod
    def _mine(workers: set[str], submitted_by: str | None) -> tuple[str, list]:
        """Which runs a caller may see, as SQL and its parameters.

        Two things, not one. The runs on the rigs whose runs are theirs, and
        their own runs -- wherever those are, which is the whole point. A run
        has no worker at all until allocation starts it, and then it has
        whatever rig the farm chose, which for an account that owns none is
        always somebody else's. Tying "mine" to a rig either way loses a
        caller their own run: while it waits, or the moment it starts. `0`
        when neither applies, so the filter selects nothing, not everything.
        """
        terms, params = [], []
        if workers:
            terms.append(f"worker IN ({','.join('?' * len(workers))})")
            params.extend(sorted(workers))
        if submitted_by:
            terms.append("json_extract(request_json,'$.submitted_by') = ?")
            params.append(submitted_by)
        return (f"({' OR '.join(terms)})" if terms else "0"), params

    @staticmethod
    def mine(job: dict, workers: set[str] | None, submitted_by: str | None) -> bool:
        """`_mine`, for the callers that have the row in hand rather than a
        query to filter. The same rule in both places or neither is true."""
        if workers is None:
            return True
        if job.get("worker") in workers:
            return True
        return bool(submitted_by) \
            and (job.get("request") or {}).get("submitted_by") == submitted_by

    def get(self, job_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._decode(row) if row else None

    def recent(self, limit: int = 50, workers: set[str] | None = None,
               submitted_by: str | None = None) -> list[dict]:
        """The newest runs, within `workers` when given.

        Selected inside the filter, not filtered after selecting: taking the
        farm's newest ten and then keeping this account's leaves an account
        with nothing on its dashboard the moment ten newer runs happen
        elsewhere, while /api/v1/jobs still lists theirs.
        """
        return self.page(limit=limit, workers=workers, submitted_by=submitted_by)["jobs"]

    def many(self, ids) -> dict[str, dict]:
        """The jobs with these ids, by id. In chunks: SQLite caps the number
        of parameters one statement may bind, and the artifact store asks
        about every bundle and reuse link on disk at once."""
        found: dict[str, dict] = {}
        wanted = sorted(set(ids))
        with self.connect() as db:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                marks = ",".join("?" * len(chunk))
                for row in db.execute(f"SELECT * FROM jobs WHERE id IN ({marks})", chunk).fetchall():
                    job = self._decode(row)
                    found[job["id"]] = job
        return found

    def artifact_records(self) -> dict[str, dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM artifact_records").fetchall()
        return {row["id"]: dict(row) for row in rows}

    def artifact_record(self, bundle_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM artifact_records WHERE id=?", (bundle_id,)).fetchone()
        return dict(row) if row else None

    def pin_artifact(self, bundle_id: str, note: str | None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO artifact_records(id, pinned_at, pin_note) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET pinned_at=excluded.pinned_at, pin_note=excluded.pin_note",
                (bundle_id, utcnow(), note),
            )

    def unpin_artifact(self, bundle_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE artifact_records SET pinned_at=NULL, pin_note=NULL WHERE id=?", (bundle_id,))

    def record_artifact_removed(self, bundle_id: str, removed_bytes: int, reason: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO artifact_records(id, removed_at, removed_bytes, removed_reason) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET removed_at=excluded.removed_at, "
                "removed_bytes=excluded.removed_bytes, removed_reason=excluded.removed_reason, "
                "pinned_at=NULL, pin_note=NULL",
                (bundle_id, utcnow(), int(removed_bytes), reason),
            )

    def page(
        self,
        limit: int = 25,
        offset: int = 0,
        status: str | None = None,
        kind: str | None = None,
        search: str | None = None,
        worker: str | None = None,
        workers: set[str] | None = None,
        submitted_by: str | None = None,
    ) -> dict:
        """One page of history, filtered, with the total the filter matches.

        Filtering and paging happen in SQL rather than in the dashboard: the
        history is the farm's permanent record and only grows, so a client
        that fetched everything to filter it would get slower every week and
        would silently stop showing older runs once it hit the cap.

        ``search`` matches the id prefix and the free text of the request and
        result — a ref, a summary, a failed stage — which is what an operator
        has in hand when they come looking for a run.
        """
        limit = max(1, min(limit, 200))
        offset = max(0, min(int(offset), MAX_PAGE_OFFSET))
        where, params = [], []
        if status in ("queued", "running", "passed", "failed", "cancelled"):
            where.append("status = ?")
            params.append(status)
        if kind in ("inventory", "build", "suite"):
            where.append("kind = ?")
            params.append(kind)
        if worker:
            # The runs one rig ran, for its page.
            where.append("worker = ?")
            params.append(worker)
        if workers is not None:
            # Every run this caller may see. In SQL with the rest, so the
            # total and the paging are the caller's too -- filtering a page
            # after the fact would report somebody else's runs in the count
            # and leave short pages behind.
            clause, scoped = self._mine(workers, submitted_by)
            where.append(clause)
            params.extend(scoped)
        if search:
            term = f"%{search.strip().lower()}%"
            where.append(
                "(lower(id) LIKE ? OR lower(request_json) LIKE ? OR lower(coalesce(result_json,'')) LIKE ?)"
            )
            params.extend([term, term, term])
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM jobs{clause}", params).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM jobs{clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {
            "jobs": [self._decode(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def since(self, cutoff: str) -> list[dict]:
        """Every job that touches the time after `cutoff`, oldest first, decoded.

        Created since then, or still going then: a run that started the
        evening before a window opened and finished inside it held the rig
        inside it, and selecting by creation alone left that time out. What
        the statistics are computed from; the window is capped by the caller.
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE created_at >= ? OR finished_at >= ? "
                "OR (started_at IS NOT NULL AND finished_at IS NULL) ORDER BY created_at",
                (cutoff, cutoff),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def finished_counts(self, workers: set[str], cutoff: str) -> dict[str, dict]:
        """Runs that finished since `cutoff`, per worker: how many, and how
        many passed. One aggregate over the finished-at index, and only the
        rows that are somebody's to count -- the world page asks this on
        every poll from anyone."""
        if not workers:
            return {}
        marks = ", ".join("?" for _ in workers)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT worker, status, COUNT(*) FROM jobs WHERE finished_at >= ? "
                f"AND status IN ('passed', 'failed') AND worker IN ({marks}) GROUP BY worker, status",
                (cutoff, *sorted(workers)),
            ).fetchall()
        counts: dict[str, dict] = {}
        for worker, status, number in rows:
            tally = counts.setdefault(worker, {"runs": 0, "passed": 0})
            tally["runs"] += number
            if status == "passed":
                tally["passed"] += number
        return counts

    def history_summary(self) -> dict:
        """How much history the file holds: what the storage detail says
        about the job database, which has no children to list."""
        with self.connect() as db:
            count, oldest, newest = db.execute(
                "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM jobs"
            ).fetchone()
            records = db.execute("SELECT COUNT(*) FROM artifact_records").fetchone()[0]
        return {"jobs": count, "oldest": oldest, "newest": newest, "artifact_records": records}

    def counts_by_status(self, workers: set[str] | None = None,
                         submitted_by: str | None = None) -> dict:
        """How many runs sit behind each filter, so the chips can say.

        Counted over the same runs the page is drawn from: a chip reading 40
        failures above a list of two would be somebody else's forty.
        """
        clause, params = "", []
        if workers is not None:
            sql, params = self._mine(workers, submitted_by)
            clause = f" WHERE {sql}"
        with self.connect() as db:
            rows = db.execute(f"SELECT status, COUNT(*) FROM jobs{clause} GROUP BY status", params).fetchall()
        return {row[0]: row[1] for row in rows}

    def active(self) -> list[dict]:
        """Running and queued jobs in the order the worker will take them:
        the running one first, then by priority, then age."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running') "
                "ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, priority DESC, created_at"
            ).fetchall()
        return [self._decode(row) for row in rows]

    def claim(self, job_id: str) -> bool:
        """Mark a queued job running; False when it is no longer queued.

        Conditional in the one statement, so a cancellation that lands
        between the dispatcher reading the queue and starting the job wins.
        """
        with self.connect() as db:
            changed = db.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
                (utcnow(), job_id),
            ).rowcount
        return changed == 1

    def next_queued(self) -> dict | None:
        """The job the worker should run next, or None."""
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY priority DESC, created_at LIMIT 1"
            ).fetchone()
        return self._decode(row) if row else None

    def promote(self, job_id: str) -> dict:
        """Move a queued job to the front of the queue.

        One above the current highest, so promoting two jobs in turn leaves
        the second one first — the operator's latest word wins.
        """
        with self.connect() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] != "queued":
                raise ValueError(f"job {job_id} is {row['status']}; only a queued job can be promoted")
            top = db.execute("SELECT COALESCE(MAX(priority), 0) FROM jobs WHERE status = 'queued'").fetchone()[0]
            db.execute("UPDATE jobs SET priority=? WHERE id=?", (int(top) + 1, job_id))
        return self.get(job_id)

    def recover_incomplete(self, keep_remote: bool = False) -> list[dict]:
        """Settle what the previous service process left behind.

        A job that was *running* lost its process with the service; its
        evidence, if any, is on disk and the record says so. A job that was
        only *queued* lost nothing — it had not started — and is handed back
        to be queued again, in the order it was submitted. Failing it used
        to be how a deploy on merge cost a sweep its third run.
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at"
            ).fetchall()
        requeue = []
        for row in rows:
            job = self._decode(row)
            if job["status"] == "queued":
                requeue.append(job)
                continue
            if keep_remote and job.get("worker"):
                # A portal restarted; the node running this did not. Its
                # heartbeat says whether it still is.
                continue
            for stage in job["progress"]:
                if stage["status"] == "running":
                    stage.update(
                        status="failed",
                        summary="Farm service restarted during this stage",
                        finished_at=utcnow(),
                    )
                elif stage["status"] == "pending":
                    stage["status"] = "skipped"
            self.update_progress(job["id"], job["progress"])
            self.update(
                job["id"],
                "failed",
                {
                    "summary": "Pipeline interrupted by a farm service restart",
                    "detail": "The previous service process ended before the job completed.",
                },
            )
        return requeue

    def normalize_legacy_failures(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE status='failed'").fetchall()
            for row in rows:
                job = self._decode(row)
                result = job.get("result") or {}
                if "error" not in result or "summary" in result:
                    continue
                summary = {
                    "inventory": "Hardware discovery failed",
                    "build": "Artifact build failed",
                    "suite": "Validation pipeline failed",
                }.get(job["kind"], "Farm job failed")
                normalized = {
                    **{key: value for key, value in result.items() if key != "error"},
                    "summary": summary,
                    "detail": "This legacy run predates structured stage tracking; see the technical log.",
                    "technical_error": result["error"],
                }
                db.execute(
                    "UPDATE jobs SET result_json=? WHERE id=?",
                    (json.dumps(normalized), job["id"]),
                )
