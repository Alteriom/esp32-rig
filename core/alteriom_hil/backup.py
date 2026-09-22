"""Backing the farm up, and putting it back.

Everything that is the farm and cannot be rebuilt lives on the Pi's SD card:
the job database (every run, its result, the input to the release reports),
the board registry that gives boards their identity, their last health
verdicts and chip readings, the configuration, the named API keys and the
pinned bundles. Re-provisioning a Pi is quick and documented; the history is
not recoverable at all. Until this, nothing copied any of it.

A backup is one ``.tar.gz``: a consistent copy of the database (SQLite's own
backup, safe while the service writes), the files beside it, and a
``backup.json`` naming each member with its SHA-256 and where it restores to.
**Never a secret**: the API token, the gateway Wi-Fi password, the consumer
token and the webhook URL are named in the manifest as excluded, because a
backup copied to another host must not be a second place a credential lives.
They are re-created by provisioning; the manifest says which.

The newest ``keep`` stay in the backup directory, and each is pushed with
rsync to ``target`` when one is set -- the only copy that survives the card.
The last outcome is kept in ``last.json`` for the health check and the
dashboard: a backup that silently stopped is the one that is missing when it
is needed.

A restore verifies every checksum before it writes anything, sets the current
database aside rather than overwriting it, and never replaces a bundle that is
already there.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
PREFIX = "alteriom-hil-"
# Files beside the database that are the farm's identity and memory.
STATE_FILES = ("inventory.yaml", "instruments.yaml", "board-health.json", "chip-details.json")
ETC_FILES = ("config.yaml", "api-keys.yaml")
# Named so a restore says what provisioning must put back.
SECRETS = ("api-token", "gateway-wifi-password", "consumer-token", "notify-webhook", "node-key",
           "providers/callmebot-url", "providers/telegram-token")
LAST = "last.json"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _database_copy(path: Path) -> bytes:
    """A consistent copy of a live SQLite database, as bytes."""
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "farm.sqlite3"
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(copy)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        return copy.read_bytes()


def _pinned_bundles(state: Path, database: Path) -> list[str]:
    try:
        db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            rows = db.execute(
                "SELECT id FROM artifact_records WHERE pinned_at IS NOT NULL AND removed_at IS NULL"
            ).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return []
    return sorted(
        row[0] for row in rows
        if (state / "artifacts" / row[0]).is_dir() and not (state / "artifacts" / row[0]).is_symlink()
    )


def create_backup(state: Path, etc: Path, directory: Path, keep: int = 14, target: str | None = None,
                  push=None) -> dict:
    """Write a backup, prune old ones, push it; return and record the outcome."""
    state, etc, directory = Path(state), Path(etc), Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    started = _now()
    members: dict[str, bytes] = {}
    restore_to: dict[str, str] = {}
    database = state / "farm.sqlite3"
    if database.is_file():
        members["state/farm.sqlite3"] = _database_copy(database)
        restore_to["state/farm.sqlite3"] = "state"
    for name in STATE_FILES:
        if (state / name).is_file():
            members[f"state/{name}"] = (state / name).read_bytes()
            restore_to[f"state/{name}"] = "state"
    for name in ETC_FILES:
        if (etc / name).is_file():
            members[f"etc/{name}"] = (etc / name).read_bytes()
            restore_to[f"etc/{name}"] = "etc"
    bundles = _pinned_bundles(state, database) if database.is_file() else []
    for bundle_id in bundles:
        root = state / "artifacts" / bundle_id
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                key = f"state/artifacts/{bundle_id}/{path.relative_to(root).as_posix()}"
                members[key] = path.read_bytes()
                restore_to[key] = "bundle"
    manifest = {
        "schema": SCHEMA,
        "created_at": started.isoformat(),
        "host": socket.gethostname(),
        "files": {
            name: {"sha256": _sha256(data), "bytes": len(data), "restore": restore_to[name]}
            for name, data in sorted(members.items())
        },
        "pinned_bundles": bundles,
        "excluded_secrets": [str(etc / name) for name in SECRETS],
    }
    archive = directory / f"{PREFIX}{started.strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
    partial = archive.with_suffix(".partial")
    with tarfile.open(partial, "w:gz") as tar:
        for name, data in [("backup.json", json.dumps(manifest, indent=2).encode()), *sorted(members.items())]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(started.timestamp())
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    os.chmod(partial, 0o600)
    partial.replace(archive)

    removed = prune(directory, keep)
    outcome = {
        "created_at": started.isoformat(),
        "archive": str(archive),
        "bytes": archive.stat().st_size,
        "files": len(members),
        "pinned_bundles": len(bundles),
        "removed": removed,
        "pushed": None,
    }
    if target:
        outcome["pushed"] = (push or push_backup)(archive, target)
    record(directory, outcome)
    return outcome


def backups(directory: Path) -> list[Path]:
    return sorted(Path(directory).glob(f"{PREFIX}*.tar.gz"))


def prune(directory: Path, keep: int) -> list[str]:
    found = backups(directory)
    doomed = found[: max(0, len(found) - max(1, keep))]
    for path in doomed:
        path.unlink(missing_ok=True)
    return [path.name for path in doomed]


def push_backup(archive: Path, target: str) -> dict:
    """rsync one archive to `target` over ssh, never prompting."""
    command = [
        "rsync", "--archive", "--partial", "--timeout=120",
        "-e", "ssh -o BatchMode=yes -o ConnectTimeout=20",
        str(archive), target.rstrip("/") + "/",
    ]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"target": target, "ok": False, "error": str(exc)}
    error = (done.stderr or done.stdout).strip().splitlines()[-1:] if done.returncode else []
    return {"target": target, "ok": done.returncode == 0, "error": error[0] if error else None}


def record(directory: Path, outcome: dict) -> None:
    path = Path(directory) / LAST
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def last_backup(directory: Path) -> dict | None:
    try:
        return json.loads((Path(directory) / LAST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def staleness(directory: Path, max_age_hours: float = 36) -> str | None:
    """Why the backups need attention, or None when they do not."""
    last = last_backup(directory)
    if last is None:
        return "no backup has been made"
    try:
        made = datetime.fromisoformat(last["created_at"])
    except (KeyError, ValueError):
        return "the last backup record is unreadable"
    age = (_now() - made).total_seconds() / 3600
    if age > max_age_hours:
        return f"the last backup is {age:.0f} h old"
    pushed = last.get("pushed")
    if pushed and not pushed.get("ok"):
        return f"the last backup was not copied to {pushed.get('target')}: {pushed.get('error')}"
    return None


# ---- restore -------------------------------------------------------------------


def read_backup(archive: Path) -> tuple[dict, dict[str, bytes]]:
    """The manifest and members of a backup, every checksum verified."""
    with tarfile.open(archive, "r:gz") as tar:
        members: dict[str, bytes] = {}
        for info in tar.getmembers():
            if not info.isfile():
                raise ValueError(f"{info.name}: a backup holds regular files only")
            name = info.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"{name}: a member may not leave the restore directories")
            members[name] = tar.extractfile(info).read()
    manifest = json.loads(members.pop("backup.json", b"null") or b"null")
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ValueError(f"{archive}: not a schema {SCHEMA} farm backup")
    listed = manifest.get("files") or {}
    if set(listed) != set(members):
        raise ValueError(f"{archive}: members do not match the manifest")
    for name, data in members.items():
        if _sha256(data) != listed[name]["sha256"]:
            raise ValueError(f"{name}: checksum mismatch; the backup is damaged")
    return manifest, members


def restore_backup(archive: Path, state: Path, etc: Path, apply: bool = False) -> dict:
    """What a restore writes; with `apply`, write it.

    The current database is renamed aside, never overwritten, and a bundle
    already on disk is left as it is.
    """
    manifest, members = read_backup(Path(archive))
    state, etc = Path(state), Path(etc)
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    plan = []
    for name, data in sorted(members.items()):
        where = manifest["files"][name]["restore"]
        relative = Path(name).relative_to("etc" if where == "etc" else "state")
        destination = (etc if where == "etc" else state) / relative
        action = "write"
        if where == "bundle" and destination.exists():
            action = "keep existing"
        elif destination.exists():
            action = "replace"
        plan.append({"member": name, "to": str(destination), "action": action, "bytes": len(data)})
    if apply:
        database = state / "farm.sqlite3"
        if database.exists():
            shutil.move(database, state / f"farm.sqlite3.before-restore-{stamp}")
        for step in plan:
            if step["action"] == "keep existing":
                continue
            destination = Path(step["to"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.restore")
            temporary.write_bytes(members[step["member"]])
            temporary.replace(destination)
    return {
        "archive": str(archive),
        "created_at": manifest.get("created_at"),
        "host": manifest.get("host"),
        "applied": apply,
        "plan": plan,
        "secrets_to_provision": manifest.get("excluded_secrets") or [],
    }
