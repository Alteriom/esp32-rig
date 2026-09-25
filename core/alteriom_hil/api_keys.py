"""Who may call the farm's API, and as what.

The farm had one bearer token. CI used it, the dashboard stored it, an
operator pasted it into curl; every holder could pause the queue, delete a
bundle or register a board, and nothing recorded who did. That was defensible
for one person and stops being so the first time a second person holds it
(docs/product-gaps.md, "One token, no identity, no record").

So a caller now presents a **named key with a role**:

- ``admin`` -- everything, as the one token could.
- ``node`` -- a worker's key (docs/portal-plan.md): report in, take work,
  report on the work it took, fetch the bundle to flash. Nothing else --
  not even reading the dashboard's routes.
- ``user`` -- read everything; start runs and health checks, hand the farm a
  bundle, rediscover the hardware; cancel the runs they started. Not: pause
  the queue, reorder it, cancel somebody else's run, delete or pin bundles,
  register or unregister boards.

The farm's own token (``/etc/alteriom-hil/api-token``) stays, as the admin key
named ``farm``: the deploy, the painlessMesh pipeline and every dispatched run
use it on the Pi, and none of them needs to change. Other keys live beside it
in ``api-keys.yaml``, which stores each key's SHA-256 and never the key: a
copy of the file lets nobody in. ``alteriom-hil-admin keys create`` prints a
key once; revoking one removes its line.

The service re-reads the file when it changes, so a key created or revoked
takes effect on the next request, without a restart.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import re
import secrets
import sys
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROLES = ("admin", "user", "node")
# A person who signed in is one of these. Signing in now makes a `user` of
# their own workspace, so nothing new is a `guest` -- the role stays because
# accounts made before that are still stored as one until their next sign-in
# settles it, and because a farm may yet want a way to hold somebody at the
# door. A guest reaches GUEST_ROUTES and nothing else.
ACCOUNT_ROLES = ("admin", "user", "guest")
GUEST_ROUTES: tuple[tuple[str, re.Pattern], ...] = (
    ("GET", re.compile(r"/api/v1/whoami")),
    # Where you are signed in, and signing one of those out. Both concern the
    # caller's own account and nothing of the farm's, so they are open to
    # anyone signed in: a guest waiting to be let in can still see a session
    # on a phone they no longer hold, and end it. The handlers scope every
    # read and every delete to the asking session's own account.
    ("GET", re.compile(r"/api/v1/sessions")),
    ("POST", re.compile(r"/api/v1/sessions/revoke")),
)
FARM_KEY_NAME = "farm"
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}\Z")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
# A recognisable prefix, so a key pasted somewhere it should not be is
# something a secret scanner can find.
KEY_PREFIX = "afk_"
MIN_FARM_TOKEN = 32


@dataclass(frozen=True)
class Identity:
    name: str
    role: str
    # Where this caller came from, because the two are not the same thing at
    # the same role. A `user` KEY is an operator credential somebody was
    # handed on purpose -- CI, the MCP server, a rig host -- and reads the
    # whole farm. A `user` ACCOUNT is whoever signed in a minute ago, and
    # reads their own workspace. Same role, different blast radius, so the
    # views ask which this is rather than guessing from the name.
    kind: str = "key"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_account(self) -> bool:
        """A person who signed in, rather than a key that was issued."""
        return self.kind == "account"

    @property
    def sees_whole_farm(self) -> bool:
        """Whether every rig is this caller's to read. An admin, and any key:
        a key is only ever created by an admin, for a job that needs it."""
        return self.is_admin or not self.is_account


def keys_path_for(token_file: str | Path) -> Path:
    """Where named keys live: beside the farm's own token."""
    return Path(token_file).with_name("api-keys.yaml")


def digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_key() -> str:
    return KEY_PREFIX + secrets.token_hex(32)


def parse_keys(document: object, source: str = "api-keys.yaml") -> list[dict]:
    """The entries of a keys document, checked. Raises ValueError."""
    entries = (document or {}).get("keys") if isinstance(document, dict) else None
    if document not in (None, {}) and not isinstance(document, dict):
        raise ValueError(f"{source}: must be a mapping with a `keys` list")
    entries = entries or []
    if not isinstance(entries, list):
        raise ValueError(f"{source}: `keys` must be a list")
    names: set[str] = set()
    digests: set[str] = set()
    checked = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{source}: every key must be a mapping")
        name, role, sha = entry.get("name"), entry.get("role"), entry.get("sha256")
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            raise ValueError(f"{source}: key name {name!r} must be 1-32 lowercase letters, digits, dots, underscores or hyphens")
        if name == FARM_KEY_NAME:
            raise ValueError(f"{source}: `{FARM_KEY_NAME}` is the farm's own token and cannot be a named key")
        if role not in ROLES:
            raise ValueError(f"{source}: key {name}: role must be one of {', '.join(ROLES)}")
        if not isinstance(sha, str) or not DIGEST_PATTERN.fullmatch(sha):
            raise ValueError(f"{source}: key {name}: sha256 must be 64 lowercase hex digits")
        if name in names:
            raise ValueError(f"{source}: key name {name} appears twice")
        if sha in digests:
            raise ValueError(f"{source}: key {name} has the same key as another")
        names.add(name)
        digests.add(sha)
        checked.append({key: value for key, value in entry.items() if value is not None})
    return checked


def load_keys(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return parse_keys(yaml.safe_load(path.read_text(encoding="utf-8")), str(path))


def keys_document(entries: list[dict]) -> str:
    return yaml.safe_dump({"keys": parse_keys({"keys": entries})}, sort_keys=False)


def write_keys(path: str | Path, entries: list[dict]) -> None:
    """The keys file, replaced whole: never half-written, readable by its owner only."""
    import os

    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(keys_document(entries), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


# Which lock files this process's threads already hold, and how deep: the
# flock is per open file description, so a thread that opened a second one
# for a path it already holds would block on itself. A rig redeeming its
# token takes this lock and then calls KeyStore.create, which takes it
# again for the same path; nested in the one thread, the inner take is a
# no-op re-entry. Other threads and other processes still serialise on the
# flock, which only the outermost take of a thread holds.
_HELD_LOCKS: dict = {}
_HELD_LOCKS_GUARD = threading.Lock()


# "Reload on the next refresh, whatever the file's stamp." Distinct from
# None, which is the stamp of an absent file: after a read failure forgot the
# stamp as None, deleting the broken file (an absent file is a valid empty
# store) left stamp == self._stamp and the refresh skipped, so the error was
# never cleared and naming stayed blocked. A sentinel no real stamp equals
# forces the reload that clears it.
_STAMP_RELOAD = object()


class KeyReadError(RuntimeError):
    """The keys file could not be read, so its names are unknown. Raised
    rather than answered with an empty set: a namespace decision made on
    "no keys" when the truth is "cannot tell" would hand a live key's name
    to somebody else."""


@contextlib.contextmanager
def namespace_lock(path: str | Path | None):
    """One critical section for everything that names a principal.

    A key's name and an account's handle are one namespace, kept in two
    stores -- the keys file and the farm's database -- and a check made
    before a write to either is only a snapshot. A sign-up for `alice`
    overlapping a rig redeeming its token as `alice`, or an operator making
    a key `alice`, could each see the name free and both keep it. This is a
    lock on a file beside the keys file, held from the check to the write,
    by the service and by both CLIs -- other processes included, which is
    why it is a file and not a threading.Lock. Where the platform has no
    flock (Windows, where nothing is deployed) it is a no-op.

    Reentrant within a thread: a caller that already holds it for a path
    (a rig join takes it, then KeyStore.create takes it again) re-enters
    without re-flocking, which on a second file descriptor would deadlock.
    """
    if path is None:
        yield
        return
    try:
        import fcntl
    except ImportError:
        yield
        return
    lock_path = Path(str(path) + ".lock")
    key = (os.path.abspath(str(lock_path)), threading.get_ident())
    with _HELD_LOCKS_GUARD:
        depth = _HELD_LOCKS.get(key, 0)
        _HELD_LOCKS[key] = depth + 1
    try:
        if depth:
            # This thread already holds the flock for this path.
            yield
        else:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "a+", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        with _HELD_LOCKS_GUARD:
            if _HELD_LOCKS.get(key, 0) <= 1:
                _HELD_LOCKS.pop(key, None)
            else:
                _HELD_LOCKS[key] -= 1


def account_handles(state: str | Path | None) -> set[str]:
    """Every account's handle in a farm's store, or none: a rig has no
    accounts, a portal's store may not exist yet, and a store from before
    there were accounts has no table. Read-only."""
    import sqlite3

    if not state:
        return set()
    db_path = Path(state) / "farm.sqlite3"
    try:
        db_path.stat()
    except FileNotFoundError:
        return set()
    except OSError as failed:
        # is_file() answered False to every error and not only to "not
        # there": a parent that cannot be searched, a mount that is gone,
        # were "no store yet" -- and a key was named on that. Only an
        # absent store is an empty one.
        raise RuntimeError(f"could not look at the farm's store at {db_path} ({failed}); "
                           "no key is named until it can be read") from failed
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
            return {row[0] for row in db.execute("SELECT handle FROM accounts")}
    except sqlite3.OperationalError as failed:
        if "no such table" in str(failed):
            return set()
        # Locked, corrupt, unreadable: an empty answer would let a key take
        # a handle the moment the store is back. No key gets named on a
        # guess about who exists.
        raise RuntimeError(f"could not read the farm's accounts from {db_path} ({failed}); "
                           "no key is named until they can be read") from failed
    except sqlite3.DatabaseError as failed:
        raise RuntimeError(f"{db_path} is not a readable store ({failed}); "
                           "no key is named until it is") from failed


def add_key(entries: list[dict], name: str, role: str, note: str | None = None,
            reserved: set[str] | frozenset[str] | None = None) -> tuple[list[dict], str]:
    """The entries with a new key, and the key -- which is shown once and
    stored nowhere. `reserved` is every name that is already somebody's --
    an account's handle -- because a key and an account with one name are
    one principal, and the key would own the account's rigs."""
    if reserved and name in reserved:
        raise ValueError(f"{name} is an account's handle: a key with that name would be that person")
    key = new_key()
    entry = {
        "name": name,
        "role": role,
        "sha256": digest(key),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    if note:
        entry["note"] = note
    updated = parse_keys({"keys": [*entries, entry]})
    return updated, key


class KeyStore:
    """Identifies a presented key. Thread-safe; re-reads the keys file when
    its modification time changes."""

    def __init__(self, farm_token: str, keys_file: str | Path | None = None,
                 reserved=None):
        # `reserved` answers "which names are somebody's already" -- the
        # accounts' handles, from the farm's store -- for every key made
        # through this store: an operator's, a rig's at enrolment. Passed in
        # rather than looked up here because the keys file knows nothing of
        # the store, and a rig has no store at all.
        self.reserved = reserved
        if len(farm_token) < MIN_FARM_TOKEN:
            raise ValueError(f"the farm's API token must contain at least {MIN_FARM_TOKEN} characters")
        self._farm = hashlib.sha256(farm_token.encode("utf-8")).digest()
        self.keys_file = Path(keys_file) if keys_file else None
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._keys: list[tuple[bytes, Identity]] = []
        self.error: str | None = None

    def _refresh(self) -> None:
        if self.keys_file is None:
            return
        try:
            stat = self.keys_file.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            stamp = None
        except OSError as exc:
            self.error = f"cannot read {self.keys_file}: {exc}"
            return
        if stamp == self._stamp:
            return
        try:
            entries = load_keys(self.keys_file) if stamp else []
        except (OSError, ValueError, yaml.YAMLError) as exc:
            # A broken file must not lock everyone out -- the farm's own token
            # keeps working -- nor quietly keep a key its admin just revoked:
            # the named keys stop until the file is fixed, and the reason is
            # kept for the status page. The stamp is not kept: a file that
            # becomes readable again without changing (permissions restored)
            # would otherwise stay "broken" until somebody touched it.
            self._keys = []
            self._stamp = _STAMP_RELOAD
            self.error = str(exc)
            return
        self._keys = [
            (bytes.fromhex(entry["sha256"]), Identity(entry["name"], entry["role"]))
            for entry in entries
        ]
        self._stamp = stamp
        self.error = None

    def entries(self) -> list[dict]:
        """Who holds a key, by name and role -- never a key or its digest."""
        with self._lock:
            self._refresh()
        if self.keys_file is None:
            return []
        try:
            listed = load_keys(self.keys_file)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            # The second read failing is the same fact as the first would
            # have been, and it is found here when the first was skipped:
            # a file whose permissions went while its mtime and size stayed
            # is one _refresh() saw no reason to reopen. An empty answer
            # here let an account take the name of a key still cached and
            # active. The error is set, the cache dropped, and the stamp
            # forgotten so the next look reads the file again.
            with self._lock:
                self._keys = []
                self._stamp = _STAMP_RELOAD
                self.error = f"cannot read {self.keys_file}: {exc}"
            return []
        return [
            {key: entry.get(key) for key in ("name", "role", "created_at", "note") if entry.get(key) is not None}
            for entry in listed
        ]

    def key_names(self) -> set[str]:
        """Every key's name -- or a raised KeyReadError, never a silent empty
        set. The refresh, the read and the verdict are one critical section
        under the store's lock, so a caller deciding a namespace cannot see
        an empty snapshot here and, a moment later, a cleared `error` that a
        concurrent request produced in between: it gets the names or the
        failure, not an ambiguous pair to check separately."""
        if self.keys_file is None:
            return set()
        with self._lock:
            self._refresh()
            listed: list[dict] = []
            if self.error is None:
                try:
                    listed = load_keys(self.keys_file)
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    self._keys = []
                    self._stamp = _STAMP_RELOAD
                    self.error = f"cannot read {self.keys_file}: {exc}"
            if self.error is not None:
                raise KeyReadError(self.error)
            return {entry.get("name") for entry in listed if entry.get("name")}

    def create(self, name: str, role: str, note: str | None = None) -> str:
        """A new named key, written to the keys file; the key is returned once
        and stored nowhere. Raises ValueError for a name that is taken."""
        if self.keys_file is None:
            raise ValueError("this farm has no keys file to add a key to")
        if name == FARM_KEY_NAME:
            raise ValueError(f"`{FARM_KEY_NAME}` is the farm's own token")
        # The reservation and the write are one critical section, shared
        # with account creation and both CLIs (namespace_lock).
        with namespace_lock(self.keys_file), self._lock:
            taken = set(self.reserved() or ()) if callable(self.reserved) else set(self.reserved or ())
            entries = load_keys(self.keys_file)
            if any(entry.get("name") == name for entry in entries):
                raise ValueError(f"a key named {name} exists")
            entries, key = add_key(entries, name, role, note, reserved=taken)
            write_keys(self.keys_file, entries)
            self._stamp = _STAMP_RELOAD
        return key

    def revoke(self, name: str) -> bool:
        """Remove a named key; False when there is none by that name."""
        if self.keys_file is None or name == FARM_KEY_NAME:
            return False
        with self._lock:
            entries = load_keys(self.keys_file)
            remaining = [entry for entry in entries if entry.get("name") != name]
            if len(remaining) == len(entries):
                return False
            write_keys(self.keys_file, remaining)
            self._stamp = _STAMP_RELOAD
        return True

    def identify(self, presented: str | None) -> Identity | None:
        if not presented:
            return None
        supplied = hashlib.sha256(presented.encode("utf-8")).digest()
        with self._lock:
            self._refresh()
            keys = list(self._keys)
        # Every digest is compared, so how long this takes does not say which
        # key, if any, came close.
        found = Identity(FARM_KEY_NAME, "admin") if hmac.compare_digest(supplied, self._farm) else None
        for stored, identity in keys:
            if hmac.compare_digest(supplied, stored) and found is None:
                found = identity
        return found


# ---- what each role may do ----------------------------------------------------

# Requests a `user` may make beyond reading. Everything not listed needs an
# admin: a route added later is admin-only until somebody decides otherwise,
# which is the safe way round.
USER_WRITES: tuple[tuple[str, re.Pattern], ...] = (
    ("POST", re.compile(r"/api/v1/suites")),
    ("POST", re.compile(r"/api/v1/health")),
    # Signing one of your own browsers out. In GUEST_ROUTES as well, which is
    # what lets somebody not yet named do it; this is what lets a `user` do
    # it, since a POST nobody has decided about is an admin's.
    ("POST", re.compile(r"/api/v1/sessions/revoke")),
    ("POST", re.compile(r"/api/v1/inventory/refresh")),
    ("POST", re.compile(r"/api/v1/artifacts")),
    # Only their own runs; the handler checks whose it is.
    ("POST", re.compile(r"/api/v1/jobs/[0-9a-f]{32}/cancel")),
    # Where one rig's events go. A rig's own subscriptions are set from its
    # page by whoever uses that rig; the farm's, under /api/v1/webhooks, stay
    # an admin's. The path says which, so this decision can be made from it.
    ("POST", re.compile(r"/api/v1/rigs/[a-z0-9][a-z0-9._-]{0,31}/webhooks")),
    ("POST", re.compile(r"/api/v1/rigs/[a-z0-9][a-z0-9._-]{0,31}/webhooks/[0-9a-f]{1,16}")),
    ("POST", re.compile(r"/api/v1/rigs/[a-z0-9][a-z0-9._-]{0,31}/webhooks/[0-9a-f]{1,16}/test")),
    ("DELETE", re.compile(r"/api/v1/rigs/[a-z0-9][a-z0-9._-]{0,31}/webhooks/[0-9a-f]{1,16}")),
    # Who may see a rig is its owner's to say (the service checks ownership;
    # `shared` it keeps for an admin).
    ("POST", re.compile(r"/api/v1/rigs/[a-z0-9][a-z0-9._-]{0,31}/visibility")),
)

# Reads that only an admin may make.
ADMIN_READS: tuple[re.Pattern, ...] = (
    re.compile(r"/api/v1/audit"),
    re.compile(r"/api/v1/keys"),
)

# What a signed-in person may reach, as opposed to a key. Every one of these
# scopes itself to the caller's workspace -- the rigs they own and the rigs
# somebody chose to show -- and nothing is on this list until it does.
# See `account_route`.
RIG_NAME = r"[a-z0-9][a-z0-9._-]{0,31}"
ACCOUNT_ROUTES: tuple[tuple[str, re.Pattern], ...] = (
    # Themselves.
    ("GET", re.compile(r"/api/v1/whoami")),
    ("GET", re.compile(r"/api/v1/sessions")),
    ("POST", re.compile(r"/api/v1/sessions/revoke")),
    # Their workspace: the rigs they own or may see, and the runs on those.
    # Each handler filters by FarmManager.visible_rigs.
    ("GET", re.compile(rf"/api/v1/rigs")),
    ("GET", re.compile(rf"/api/v1/rigs/{RIG_NAME}")),
    # The rig view: the detail in the shape a rig serves about itself, and
    # cut for a lent rig the same way (rig_detail does the cutting).
    ("GET", re.compile(rf"/api/v1/rigs/{RIG_NAME}/view")),
    ("GET", re.compile(r"/api/v1/status")),
    ("GET", re.compile(r"/api/v1/jobs")),
    ("GET", re.compile(r"/api/v1/jobs/[0-9a-f]{32}")),
    # What a run they can open produced. The evidence follows the run: the
    # handler makes the same visible-rig check the run itself makes, so this
    # reaches no further than the line above already does.
    ("GET", re.compile(r"/api/v1/jobs/[0-9a-f]{32}/artifacts/[a-z0-9][a-z0-9_.:-]{0,80}")),
    # The boards on the rigs they can see, and nothing on anybody else's.
    ("GET", re.compile(r"/api/v1/inventory")),
    # What has been done to a rig they own, with the arguments and who
    # asked. The handler refuses a rig they do not administer, and the rig
    # page does not offer the tab for one; without this an owner's own
    # Activity tab answered 403 (runner/web/app.js, RIG_TABS).
    ("GET", re.compile(rf"/api/v1/workers/{RIG_NAME}/commands")),
    # And one board's history, the drill-down that list opens: the handler
    # refuses a board on no rig of theirs, and drops the run ids from
    # verdicts they may not follow. Without it a person clicking a board on
    # their own rig gets a 403 (runner/web/app.js, showBoard).
    ("GET", re.compile(rf"/api/v1/inventory/{RIG_NAME}/history")),
    # The artifact store, scoped: a bundle belongs to a project, and a project
    # is shown to the accounts whose workspace it is in and, when its
    # repository is public and shown, to everybody -- the library, the list,
    # one bundle, its archive and its files all ask `visible_profiles` first
    # and answer 404 for the rest. It was closed while a bundle entry had no
    # owner to filter on; it has one now (docs/public-release-plan.md, the
    # library).
    ("GET", re.compile(r"/api/v1/artifacts")),
    ("GET", re.compile(r"/api/v1/artifacts/library")),
    ("GET", re.compile(r"/api/v1/artifacts/[0-9a-f]{32}")),
    ("GET", re.compile(r"/api/v1/artifacts/[0-9a-f]{32}/bundle")),
    ("GET", re.compile(r"/api/v1/artifacts/[0-9a-f]{32}/files/[^\x00-\x1f\x7f]+")),
    # Their own rigs' visibility.
    ("POST", re.compile(rf"/api/v1/rigs/{RIG_NAME}/visibility")),
    # Starting a run: a project they may see, on a rig of theirs or one
    # shared with them, named in the request. The handler asks the portal
    # (may_submit) before anything is queued; it was farm-wide while the
    # allocator did not know whose rig a run was for -- now the run says.
    ("POST", re.compile(r"/api/v1/suites")),
    # Stopping their own run. Safe to admit because the handler already asks
    # whose run it is -- a non-admin may cancel only what they submitted --
    # and necessary because a run an account can see and not stop is worse
    # than one it cannot see: the dashboard offers the button on exactly the
    # runs `run_scope` returns. What stays closed is the farm-wide half of a
    # user key's writes: rediscovery commands every rig at once, and a run
    # is allocated boards from the whole farm, so those wait until the
    # allocator knows whose workspace asked.
    ("POST", re.compile(r"/api/v1/jobs/[0-9a-f]{32}/cancel")),
    # Where their own rig's events go. `_require_rig_admin` checks the owner
    # on every one of these, and the GET refuses a rig the caller does not
    # administer, so an account reaches its own rigs' subscriptions and no
    # others. The farm's own, under /api/v1/webhooks, are not here: they are
    # the fleet's, and stay an admin's.
    ("GET", re.compile(rf"/api/v1/rigs/{RIG_NAME}/webhooks")),
    ("POST", re.compile(rf"/api/v1/rigs/{RIG_NAME}/webhooks")),
    ("POST", re.compile(rf"/api/v1/rigs/{RIG_NAME}/webhooks/[0-9a-f]{{1,16}}")),
    ("POST", re.compile(rf"/api/v1/rigs/{RIG_NAME}/webhooks/[0-9a-f]{{1,16}}/test")),
    ("DELETE", re.compile(rf"/api/v1/rigs/{RIG_NAME}/webhooks/[0-9a-f]{{1,16}}")),
    # Their own rigs, whole: adding one, and the life of one they own --
    # its name and description while it is still joining, a new join
    # command, and deleting it. People bring their own rigs; that is the
    # premise, and for a long time only an admin could act on it, because
    # every host this farm had was the farm's own. `create_rig` gives a new
    # rig to whoever added it, and the handler asks `may_manage_rig` before
    # the other three, so an account reaches exactly its own and is told
    # "no such rig" about everybody else's (the same answer the reads give).
    ("POST", re.compile(r"/api/v1/rigs")),
    ("PATCH", re.compile(rf"/api/v1/rigs/{RIG_NAME}")),
    ("POST", re.compile(rf"/api/v1/rigs/{RIG_NAME}/join")),
    ("DELETE", re.compile(rf"/api/v1/rigs/{RIG_NAME}")),
)

# The USER_WRITES an account may NOT make, and why each one waits.
#
# These are the farm-wide half of a `user` key's powers, and admitting them
# for an account would be worse than leaving an account read-only -- it would
# let whoever signed in a minute ago run code on somebody else's hardware:
#
#   /api/v1/inventory/refresh  request_rediscovery() commands *every* online
#                              hardware rig, not one; there is no per-rig
#                              spelling of it to give an account.
#   /api/v1/suites             a run is allocated boards from the farm, and
#                              the allocator does not know whose workspace
#                              asked. A run submitted by an account could be
#                              placed on a rig they cannot even see.
#   /api/v1/health             the same, by way of a canary run.
#
# What they need is an allocator that takes the caller's rigs the way the
# reads now do (docs/device-platform-plan.md). Until then an account owns its
# rigs and watches its runs, and the farm starts them.

# The calls that carry no key. Two because the caller has none yet: the
# script a new rig runs, and the call that trades its one-time token for its
# node key (docs/farm-service.md, "Adding a rig"). One because it is for
# anyone: the world page's view of the public rigs, which carries nothing
# that is anybody's (docs/farm-service.md, "Public rigs"). The handler
# answers them before it looks for a key.
PUBLIC_ROUTES: tuple[tuple[str, re.Pattern], ...] = (
    ("GET", re.compile(r"/api/v1/join\.sh")),
    ("POST", re.compile(r"/api/v1/enroll")),
    ("GET", re.compile(r"/api/v1/world")),
)


def public_route(method: str, path: str) -> bool:
    return any(verb == method and pattern.fullmatch(path) for verb, pattern in PUBLIC_ROUTES)


# What a worker calls, and all it may call. The handler also checks that a
# worker speaks only for itself (its key's name is the worker's name) and
# reports only on jobs leased to it.
NODE_ROUTES: tuple[tuple[str, re.Pattern], ...] = (
    ("POST", re.compile(r"/api/v1/workers/[a-z0-9][a-z0-9._-]{0,31}/(hello|heartbeat|lease)")),
    # That it has begun a command, and what came of it.
    ("POST", re.compile(r"/api/v1/workers/[a-z0-9][a-z0-9._-]{0,31}/commands/[0-9a-f]{32}(/start)?")),
    # The history a farm brings when it joins (its own: the handler checks).
    ("POST", re.compile(
        r"/api/v1/workers/[a-z0-9][a-z0-9._-]{0,31}/history/"
        r"(known|artifacts/[0-9a-f]{32}|jobs/[0-9a-f]{32}(/evidence|/log|/link)?)"
    )),
    ("POST", re.compile(r"/api/v1/jobs/[0-9a-f]{32}/(stages|log|evidence|result)")),
    ("GET", re.compile(r"/api/v1/artifacts/[0-9a-f]{32}/bundle")),
    # The release of the farm the portal wants it to run: its bundle, and
    # the packages published beside it (docs/public-release-plan.md, 13).
    ("GET", re.compile(r"/api/v1/releases/[0-9a-f]{40}/bundle")),
    ("GET", re.compile(r"/api/v1/releases/[0-9a-f]{40}/files/[A-Za-z0-9][A-Za-z0-9._-]{0,120}")),
    # Which one that is, for a rig joining before it has said hello.
    ("GET", re.compile(r"/api/v1/releases/current")),
)


def _node_route(method: str, path: str) -> bool:
    return any(verb == method and pattern.fullmatch(path) for verb, pattern in NODE_ROUTES)


def required_role(method: str, path: str) -> str:
    """The role a request needs: `user`, `admin`, or `node` for a worker's
    calls -- which no other role makes, so a person's key cannot pose as a
    worker and report a run it never ran."""
    if method != "GET" and _node_route(method, path):
        return "node"
    if method in ("GET", "HEAD"):
        return "admin" if any(pattern.fullmatch(path) for pattern in ADMIN_READS) else "user"
    if any(verb == method and pattern.fullmatch(path) for verb, pattern in USER_WRITES):
        return "user"
    return "admin"


def account_route(method: str, path: str) -> bool:
    """Whether a signed-in person may reach this at all.

    Default-deny, and deliberately not the same question as `required_role`.
    A `user` KEY is an operator credential an admin issued for a job that
    needs it, and reads the whole farm. A `user` ACCOUNT is whoever signed in
    a minute ago, and reads their own workspace -- so the farm may only
    answer them on routes that have been taught to scope themselves.

    Adding a route does not add it here. That is the point: an unscoped read
    answers an account with everybody's farm, and the failure is silent --
    the data simply arrives. So a new endpoint is closed to accounts until
    somebody scopes it and says so on this list, the same way a new write is
    an admin's until USER_WRITES says otherwise.
    """
    return any(verb == method and pattern.fullmatch(path) for verb, pattern in ACCOUNT_ROUTES)


def half_route(extra, method: str, path: str):
    """The route a half declared for this, or None.

    `extra` is what `BaseManager.api_routes()` gave the handler: a half's
    own routes, each carrying the audience it is for. They are checked here
    rather than added to `ACCOUNT_ROUTES` because a half is a distribution
    of its own and this file is not one of them -- but the declaration is
    just as deliberate, and it sits beside the method that answers.
    """
    for route in extra or ():
        if route.method == method and route.pattern.fullmatch(path):
            return route
    return None


def allowed(identity: Identity, method: str, path: str, extra=()) -> bool:
    if identity.role == "node":
        return _node_route(method, path)
    if identity.role == "guest":
        return any(verb == method and pattern.fullmatch(path) for verb, pattern in GUEST_ROUTES)
    needed = required_role(method, path)
    if needed == "node":
        return False
    if identity.is_admin:
        return True
    declared = half_route(extra, method, path)
    # A person, not a key: their own workspace, and only where the farm knows
    # how to show them just that.
    if identity.is_account:
        return account_route(method, path) or (declared is not None and declared.audience == "account")
    if declared is not None:
        return declared.audience in ("account", "user")
    return needed == "user"


# ---- a keys file where there is no host configuration -------------------------
# On a farm host, `alteriom-hil-admin keys` manages keys beside the token the
# host configuration names. A portal in a container has no such configuration:
#   python -m alteriom_hil.api_keys create --name rig-01 --role node
# with the file from --file or ALTERIOM_HIL_API_KEYS_FILE.


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="python -m alteriom_hil.api_keys", description="Named API keys in a keys file.")
    parser.add_argument("--file", default=os.environ.get("ALTERIOM_HIL_API_KEYS_FILE"), help="the keys file")
    parser.add_argument("--state", default=os.environ.get("ALTERIOM_HIL_STATE"),
                        help="the farm's state directory, whose accounts' handles no key may take")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="make a key and print it once")
    create.add_argument("--name", required=True)
    create.add_argument("--role", required=True, choices=ROLES)
    create.add_argument("--note")
    commands.add_parser("list", help="the keys by name and role (never the keys)")
    revoke = commands.add_parser("revoke", help="remove a key")
    revoke.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    if not args.file:
        parser.error("name the keys file with --file or ALTERIOM_HIL_API_KEYS_FILE")
    path = Path(args.file)
    if args.command == "list":
        for entry in load_keys(path):
            print(f"{entry['name']:32} {entry['role']:6} {entry.get('created_at', '')}")
        return 0
    # Read, decide and write under the one lock every naming shares.
    with namespace_lock(path):
        entries = load_keys(path)
        if args.command == "create":
            if any(entry["name"] == args.name for entry in entries):
                parser.error(f"a key named {args.name} exists; revoke it first")
            try:
                entries, key = add_key(entries, args.name, args.role, args.note, reserved=account_handles(args.state))
            except (ValueError, RuntimeError) as refused:
                parser.error(str(refused))
        else:
            remaining = [entry for entry in entries if entry["name"] != args.name]
            if len(remaining) == len(entries):
                parser.error(f"no key named {args.name}")
            entries, key = remaining, None
        write_keys(path, entries)
    if key:
        print(key)
        print(f"{args.name} ({args.role}): shown once, stored nowhere", file=sys.stderr)
    else:
        print(f"revoked {args.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
