#!/usr/bin/env python3
"""Administrative CLI for the Alteriom ESP32 HIL service."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

from alteriom_hil import health_check
from alteriom_hil import hil_config
from alteriom_hil import release as release_document
from alteriom_hil import farm_shared
from alteriom_hil import providers
from alteriom_hil.api_keys import (FARM_KEY_NAME, ROLES, account_handles, add_key, keys_document,
                                   keys_path_for, load_keys, namespace_lock)
from alteriom_hil.board import Board, BoardMap, TARGET_CHIPS
from alteriom_hil.devices import load_families
from alteriom_hil.instrument import (
    KINDS,
    Instrument,
    Wire,
    instruments_document,
    instruments_path_for,
    load_instruments,
    validate_instruments,
    wired_to,
)
from alteriom_hil.inventory import (
    discover,
    normalize_mac,
    publish_inventory,
    validate_registry,
    write_registry,
)

STATUS_PATH = Path("/var/lib/alteriom-hil/status.json")
TIMER_DROPIN = Path("/etc/systemd/system/alteriom-hil-health.timer.d/config.conf")
# The same lock the service takes, from the same place, so a test or a host
# that keeps its locks elsewhere moves both.
RIG_LOCK = farm_shared.RIG_LOCK_PATH


@contextmanager
def rig_lock():
    """Serialize manual probes with API and CI hardware jobs.

    The lock lives on a tmpfs, so it is gone after every reboot and the
    installer's copy only covers the boot it ran on: a rig that had been
    restarted answered `boards discover` with "No such file or directory"
    (a fresh install, 2026-09-16). It is a lock, not a record -- an
    absent one means nothing is holding the rig -- so it is created here when
    it is missing rather than reported as a fault. `/run/lock` is world
    writable with the sticky bit, which is what lets the rig's own user make
    it; a directory that refuses falls back to the read-only open, whose
    error names the file as before.
    """
    import fcntl

    # Read-only is sufficient for flock and also works under hardened sudo
    # policies that deny root writes to a user-owned runtime lock.
    try:
        descriptor = os.open(RIG_LOCK, os.O_RDONLY | os.O_CREAT, 0o660)
    except OSError:
        with RIG_LOCK.open("r") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, text=True, check=check)


def require_root() -> None:
    if os.geteuid() != 0:
        raise hil_config.ConfigError("this operation requires root; rerun with sudo")


def config_env(payload: dict) -> dict[str, str]:
    values = dict(os.environ)
    for line in hil_config.runtime_env(payload).splitlines():
        key, value = line.split("=", 1)
        values[key] = value
    return values


def apply(payload: dict, restart: bool = True) -> None:
    require_root()
    hil_config.require_valid(payload)
    hil_config.write_atomic(hil_config.RUNTIME_ENV_PATH, hil_config.runtime_env(payload))
    interval = payload["health"]["interval_minutes"]
    timer = f"[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec={interval}min\n"
    hil_config.write_atomic(TIMER_DROPIN, timer, 0o644)
    run("systemctl", "daemon-reload")
    run("systemctl", "restart", "alteriom-hil-health.timer")
    if restart and payload["runner"].get("unit"):
        run("systemctl", "restart", payload["runner"]["unit"])
    run("systemctl", "start", "alteriom-hil-health.service", check=False)


def command_status(args: argparse.Namespace) -> int:
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    if args.live:
        report = health_check.collect_health(config_env(payload))
    else:
        try:
            report = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise hil_config.ConfigError(f"cannot read saved status: {exc}; use --live") from exc
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else health_check.render(report))
    return int(args.strict and report["status"] != "ok")


def command_config_show(args: argparse.Namespace) -> int:
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    text = json.dumps(payload, indent=2) if args.json else hil_config.dump_config(payload)
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def command_config_validate(args: argparse.Namespace) -> int:
    payload = hil_config.load_config(args.config)
    errors = hil_config.validate_config(payload)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(f"valid: {args.config} (schema {payload['schema']})")
    return 0


def command_config_set(args: argparse.Namespace) -> int:
    require_root()
    payload = hil_config.set_value(hil_config.load_config(args.config), args.key, args.value)
    hil_config.write_atomic(args.config, hil_config.dump_config(payload))
    apply(payload, restart=not args.no_restart)
    print(f"updated {args.key}; configuration applied")
    return 0


def command_config_set_many(args: argparse.Namespace) -> int:
    """Settings a portal asked a node for (rig/node-control.sh): read from a
    file, only the remote settings, validated together, applied at once."""
    require_root()
    try:
        settings = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise hil_config.ConfigError(f"cannot read {args.file}: {exc}") from exc
    payload = hil_config.set_values(hil_config.load_config(args.config), settings)
    hil_config.write_atomic(args.config, hil_config.dump_config(payload))
    apply(payload, restart=False)
    print(json.dumps({key: settings[key] for key in sorted(settings)}))
    return 0


def command_config_join(args: argparse.Namespace) -> int:
    """This host a node of a portal: what join-rig.sh has the installer do
    before it applies the configuration."""
    require_root()
    payload = hil_config.join_portal(hil_config.load_config(args.config), args.portal, args.name, args.key_file)
    hil_config.write_atomic(args.config, hil_config.dump_config(payload))
    print(f"farm.mode node: {args.name} of {payload['farm']['portal_url']}")
    return 0


def command_config_apply(args: argparse.Namespace) -> int:
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    apply(payload, restart=not args.no_restart)
    print("configuration applied")
    return 0


def command_health_refresh(args: argparse.Namespace) -> int:
    require_root()
    run("systemctl", "reset-failed", "alteriom-hil-health.service", check=False)
    result = run("systemctl", "start", "alteriom-hil-health.service", check=False)
    status_args = argparse.Namespace(config=args.config, live=False, json=args.json, strict=False)
    command_status(status_args)
    return result.returncode


def command_service(args: argparse.Namespace) -> int:
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    unit = payload["runner"].get("unit")
    if not unit:
        print("this host has no Actions runner (farm.mode: node); its portal hands it work", file=sys.stderr)
        return 1
    if args.action in ("restart", "start", "stop"):
        require_root()
    return run("systemctl", args.action, unit, "--no-pager", check=False).returncode


# ---- API keys ------------------------------------------------------------------
# Named keys with a role, beside the farm's own token (alteriom_hil.api_keys).
# The file holds each key's SHA-256; a key is printed once, when it is made.

DEFAULT_TOKEN_FILE = "/etc/alteriom-hil/api-token"


def token_file_path(payload: dict) -> Path:
    return Path((payload.get("service") or {}).get("token_file") or DEFAULT_TOKEN_FILE)


def load_api_keys(payload: dict) -> tuple[Path, list[dict]]:
    path = keys_path_for(token_file_path(payload))
    try:
        return path, load_keys(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise hil_config.ConfigError(f"cannot read {path}: {exc}") from exc


def write_api_keys(payload: dict, path: Path, entries: list[dict]) -> None:
    try:
        text = keys_document(entries)
    except ValueError as exc:
        raise hil_config.ConfigError(str(exc)) from exc
    hil_config.write_atomic(path, text, 0o640)
    # Readable by the service exactly as the token file is: root-owned, group
    # the service's. A new file in /etc/alteriom-hil would otherwise take the
    # directory's group, and the service would read no keys at all.
    token = token_file_path(payload)
    if os.geteuid() == 0 and token.exists():
        os.chown(path, 0, token.stat().st_gid)


def command_keys_list(args: argparse.Namespace) -> int:
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    path, entries = load_api_keys(payload)
    listed = [{"name": FARM_KEY_NAME, "role": "admin", "note": f"the farm's own token, {token_file_path(payload)}"}]
    listed += [{k: v for k, v in entry.items() if k != "sha256"} for entry in entries]
    if args.json:
        print(json.dumps(listed, indent=2))
        return 0
    print(f"{'NAME':20} {'ROLE':6} {'CREATED':26} NOTE")
    for entry in listed:
        print(f"{entry['name']:20} {entry['role']:6} {entry.get('created_at', '-'):26} {entry.get('note', '')}")
    return 0


def command_keys_create(args: argparse.Namespace) -> int:
    require_root()
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    # A key's name and an account's handle are one namespace: the key would
    # own the account's rigs and cancel its runs (docs/farm-service.md,
    # "Accounts"). Read, decide and write under the lock the service and
    # the container CLI hold for the same thing.
    with namespace_lock(keys_path_for(token_file_path(payload))):
        path, entries = load_api_keys(payload)
        try:
            reserved = account_handles(payload["paths"].get("state") or "/var/lib/alteriom-hil")
            updated, key = add_key(entries, args.name, args.role, args.note, reserved=reserved)
        except (ValueError, RuntimeError) as exc:
            raise hil_config.ConfigError(str(exc)) from exc
        write_api_keys(payload, path, updated)
    print(f"created {args.role} key {args.name} in {path}")
    print("the key, shown once -- the farm keeps only its hash:")
    print(key)
    return 0


def command_keys_revoke(args: argparse.Namespace) -> int:
    require_root()
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    if args.name == FARM_KEY_NAME:
        raise hil_config.ConfigError(
            f"`{FARM_KEY_NAME}` is the farm's own token; rotate {token_file_path(payload)} instead"
        )
    path, entries = load_api_keys(payload)
    remaining = [entry for entry in entries if entry.get("name") != args.name]
    if len(remaining) == len(entries):
        raise hil_config.ConfigError(f"no key named {args.name}")
    write_api_keys(payload, path, remaining)
    print(f"revoked {args.name}; the farm refuses it from its next request")
    return 0


# ---- notifications and backups ---------------------------------------------------


def command_notify_set(args: argparse.Namespace) -> int:
    """Point notifications at a channel, taking its credential from stdin.

    The same rule the providers follow: a token typed on a command line is a
    token in the shell history and in the process list, so it is read from a
    prompt and written to a root-owned file nothing else can read.
    """
    require_root()
    payload = hil_config.load_config(args.config)
    channels = hil_config.notify_channels(payload)
    # Adding one, or replacing the one named: a rig can be told to say things
    # in several places, so setting a channel no longer silently drops the
    # channel that was already there.
    if getattr(args, "id", None):
        notify = dict(_notify_channel(payload, args.id))
        channels = [item for item in channels if item["id"] != notify["id"]]
    else:
        taken = {item["id"] for item in channels}
        following = 1
        while str(following) in taken:
            following += 1
        notify = {"id": str(following)}
    notify["channel"] = args.channel
    notify["enabled"] = True
    if args.channel == "callmebot":
        # Nothing to store: the rig already keeps this link for the runs it
        # validates with, and a second copy is a second thing to rotate.
        settings, configured = _callmebot(payload)
        if not configured or not settings.get("url_file"):
            raise hil_config.ConfigError(
                "no CallMeBot link on this rig: sudo alteriom-hil-admin providers set callmebot")
        notify.pop("token_file", None)
        notify.pop("chat_id", None)
    elif args.channel == "telegram":
        notify.setdefault("token_file", "/etc/alteriom-hil/providers/telegram-token")
        if not args.chat_id:
            raise hil_config.ConfigError("--chat-id is required for Telegram")
        notify["chat_id"] = str(args.chat_id)
        secret = read_secret_line("Telegram bot token: ")
        write_secret_file(payload, Path(notify["token_file"]), secret)
    else:
        notify.setdefault("webhook_url_file", "/etc/alteriom-hil/notify-webhook")
        if args.format:
            notify["format"] = args.format
        secret = read_secret_line("Webhook URL: ")
        write_secret_file(payload, Path(notify["webhook_url_file"]), secret)
    payload = hil_config.with_notify_channels(payload, channels + [notify])
    # Written down before it is applied: `apply()` renders runtime.env from
    # this payload, and without saving it the very next command -- the
    # `notify test` printed below -- reloads the file and finds the old
    # channel, or none.
    hil_config.require_valid(payload)
    hil_config.write_atomic(args.config, hil_config.dump_config(payload))
    apply(payload, restart=False)
    # The farm service builds its notifier once, at startup, from the
    # environment: until it restarts, the events it sends still go to the old
    # channel. Restarting it is safe here only when it is idle, so this says
    # what it did and what is left.
    restarted = restart_sending_service()
    print(f"channel {notify['id']} notifies through {args.channel}; "
          f"send one with: alteriom-hil-admin notify test --id {notify['id']}")
    if not restarted:
        print("the farm service still has the old channel: restart it when it is idle "
              "(sudo systemctl restart alteriom-hil-farm, or alteriom-hil-node)")
    return 0


def restart_sending_service() -> bool:
    """Restart whichever service sends notifications, if nothing is running.

    A run holds the rig lock for its whole length, and restarting the service
    under one ends it. Taking that lock without waiting is the question "is
    anything running" asked the way the farm itself asks it; a rig that is
    busy keeps the old channel until someone restarts it, and is told so.
    """
    import fcntl

    units = [unit for unit in ("alteriom-hil-farm.service", "alteriom-hil-node.service")
             if subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False).returncode == 0]
    if not units:
        return False
    try:
        with open(RIG_LOCK, "a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError:
        return False
    for unit in units:
        run("systemctl", "restart", unit, check=False)
    return True



def command_notify_show(args: argparse.Namespace) -> int:
    """Every channel this host notifies through, and what each last did. Never
    a credential: its path, so an operator knows which file to rotate."""
    from alteriom_hil.notify import CHANNELS

    payload = hil_config.require_valid(hil_config.load_config(args.config))
    channels = hil_config.notify_channels(payload)
    if not channels:
        print("no channel is set; add one with: alteriom-hil-admin notify set --channel telegram …")
        return 0
    for index, notify in enumerate(channels):
        if index:
            print()
        channel = notify.get("channel", "webhook")
        spec = CHANNELS.get(channel)
        print(f"[{notify['id']}] {channel}" + (f" -- {spec.title}" if spec else ""))
        print(f"  enabled: {bool(notify.get('enabled'))}")
        print(f"  events:  {', '.join(notify.get('events') or hil_config.NOTIFY_EVENTS)}")
        if channel == "telegram":
            print(f"  chat:    {notify.get('chat_id') or '(unset)'}")
            print(f"  token:   {notify.get('token_file') or '(unset)'}")
        elif channel == "callmebot":
            print("  link:    the rig's own, from providers.callmebot")
        else:
            print(f"  format:  {notify.get('format', 'slack')}")
            print(f"  webhook: {notify.get('webhook_url_file') or '(unset)'}")
        if spec and spec.how:
            print(f"  how:     {spec.how}")
    return 0


def _notify_channel(payload: dict, wanted: str | None) -> dict:
    """The channel a command names, or the only one there is."""
    channels = hil_config.notify_channels(payload)
    if not channels:
        raise hil_config.ConfigError("no channel is set on this host")
    if wanted:
        for channel in channels:
            if channel["id"] == str(wanted):
                return channel
        raise hil_config.ConfigError(
            f"no channel {wanted}; this host has {', '.join(item['id'] for item in channels)}")
    if len(channels) > 1:
        raise hil_config.ConfigError(
            "this host has several channels: name one with --id "
            f"({', '.join(item['id'] for item in channels)})")
    return channels[0]


def command_notify_remove(args: argparse.Namespace) -> int:
    """Stop sending down one channel. Its credential file is left where it is:
    removing a channel is not the moment to delete a secret somebody may be
    about to put back."""
    require_root()
    payload = hil_config.load_config(args.config)
    going = _notify_channel(payload, args.id)
    kept = [item for item in hil_config.notify_channels(payload) if item["id"] != going["id"]]
    payload = hil_config.with_notify_channels(payload, kept)
    hil_config.require_valid(payload)
    hil_config.write_atomic(args.config, hil_config.dump_config(payload))
    apply(payload, restart=False)
    restarted = restart_sending_service()
    where = going.get("channel", "webhook")
    print(f"channel {going['id']} ({where}) no longer sends"
          + ("" if kept else "; nothing is told when this rig has a problem"))
    if not restarted:
        print("the farm service still has the old channels: restart it when it is idle "
              "(sudo systemctl restart alteriom-hil-farm, or alteriom-hil-node)")
    return 0


def command_notify_tune(args: argparse.Namespace) -> int:
    """Turn one channel off or on, and choose what it says.

    Neither is a credential, so neither asks for one: a rig with two channels
    can silence the noisy one without being handed a bot token again.
    """
    require_root()
    payload = hil_config.load_config(args.config)
    channel = dict(_notify_channel(payload, args.id))
    if args.on:
        channel["enabled"] = True
    if args.off:
        channel["enabled"] = False
    if args.events:
        wanted = [item.strip() for item in args.events.split(",") if item.strip()]
        if not wanted or not set(wanted) <= set(hil_config.NOTIFY_EVENTS):
            raise hil_config.ConfigError(
                f"--events must be a comma-separated list of {', '.join(hil_config.NOTIFY_EVENTS)}")
        channel["events"] = wanted
    kept = [item if item["id"] != channel["id"] else channel
            for item in hil_config.notify_channels(payload)]
    payload = hil_config.with_notify_channels(payload, kept)
    hil_config.require_valid(payload)
    hil_config.write_atomic(args.config, hil_config.dump_config(payload))
    apply(payload, restart=False)
    restarted = restart_sending_service()
    print(f"channel {channel['id']} is {'on' if channel.get('enabled') else 'off'}; "
          f"sends {', '.join(channel.get('events') or hil_config.NOTIFY_EVENTS)}")
    if not restarted:
        print("the farm service still has the old channels: restart it when it is idle "
              "(sudo systemctl restart alteriom-hil-farm, or alteriom-hil-node)")
    return 0


def command_notify_test(args: argparse.Namespace) -> int:
    """Send one message down the configured path, so the first real one is not
    the first time anybody finds out the webhook is wrong."""
    from alteriom_hil.notify import Notification, Notifier

    payload = hil_config.require_valid(hil_config.load_config(args.config))
    notify = _notify_channel(payload, getattr(args, "id", None))
    channel = notify.get("channel", "webhook")
    if channel == "telegram":
        secret_file = notify.get("token_file")
    elif channel == "callmebot":
        # It sends through the link this rig already validates with: there is
        # no second credential, which is the whole point of the channel. Read
        # from the same place `runtime_env` exports it from.
        secret_file = (hil_config.callmebot_settings(payload) or {}).get("url_file")
    else:
        secret_file = notify.get("webhook_url_file")
    if not secret_file:
        raise hil_config.ConfigError(
            f"this rig has no credential stored for its {channel} channel"
            if notify else "no notify: section in the configuration")
    notifier = Notifier(
        secret_file,
        fmt=notify.get("format", "slack"),
        events=notify.get("events") or hil_config.NOTIFY_EVENTS,
        public_host=(payload.get("service") or {}).get("public_host"),
        channel=channel,
        chat_id=notify.get("chat_id"),
    )
    outcome = notifier.send(Notification(
        "test", "A test from the farm",
        "Notifications reach this channel. Nothing is wrong.",
        link=notifier.link("#overview"), tone="good",
    ))
    state = "" if notify.get("enabled") else " (notify.enabled is false: nothing else will be sent until it is on)"
    if outcome["ok"]:
        print(f"delivered, {notifier.where()} answered {outcome['status']}{state}")
        return 0
    print(f"not delivered: {outcome['error']}{state}", file=sys.stderr)
    return 1


def _backup_settings(payload: dict) -> dict:
    return {**hil_config.DEFAULT_BACKUP, **(payload.get("backup") or {})}


# ---- upgrade ---------------------------------------------------------------
#
# A release is packages (docs/public-release-plan.md, step 13):
# runner/ci/build-release.sh makes two wheels, a dashboard bundle and a
# release.json naming each with its digest. A rig installs one from a
# directory, or from the portal it belongs to.
#
# This is not how the farm's own nodes update -- they take a git bundle and
# alteriom-hil-update installs it -- and it is not meant to be yet. It is
# what a rig with no portal does, and what a rig with one will do when 13d
# turns the node's update path round.


def _believed(body: bytes, commit: str | None = None) -> dict:
    """The manifest, or a reason rather than a traceback. A person running a
    command gets told what is wrong with the release, not where it was
    noticed."""
    try:
        return release_document.parse_manifest(body, commit)
    except release_document.ReleaseError as exc:
        raise SystemExit(f"refusing to install: {exc}") from None


def _release_from_directory(source: Path) -> tuple[dict, dict]:
    """A release built into a directory: the manifest, and its files."""
    manifest_path = source / "release.json"
    if not manifest_path.is_file():
        raise SystemExit(f"{source} holds no release.json; build one with runner/ci/build-release.sh --out {source}")
    manifest = _believed(manifest_path.read_bytes())
    files = {}
    for entry in release_document.entries(manifest):
        path = source / entry["name"]
        if not path.is_file():
            raise SystemExit(f"release.json names {entry['name']}, which is not in {source}")
        files[entry["name"]] = path.read_bytes()
    return manifest, files


def _release_from_portal(base_url: str, token: str, commit: str | None) -> tuple[dict, dict]:
    """The release a portal says its rigs should run, and its files."""
    def get(path: str) -> bytes:
        request = Request(base_url.rstrip("/") + path, headers={"Authorization": f"Bearer {token}"})
        with urlopen(request, timeout=300) as response:
            return response.read()

    if commit is None:
        try:
            current = json.loads(get("/api/v1/releases/current") or b"{}")
        except HTTPError as exc:
            raise SystemExit(f"the portal has no current release ({exc.code})") from None
        commit = current.get("commit")
        if not commit:
            raise SystemExit("the portal named no current release")
    try:
        manifest = _believed(get(f"/api/v1/releases/{commit}/files/release.json"), commit)
    except HTTPError as exc:
        if exc.code == 404:
            raise SystemExit(
                f"the portal holds no packages for release {commit[:12]}: it was published as a bundle only, "
                f"or by a portal older than the packages (docs/public-release-plan.md, step 13)"
            ) from None
        raise SystemExit(f"the portal answered {exc.code} for release {commit[:12]}") from None
    files = {entry["name"]: get(f"/api/v1/releases/{commit}/files/{entry['name']}")
             for entry in release_document.entries(manifest)}
    return manifest, files


def command_upgrade(args: argparse.Namespace) -> int:
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    venv = Path(payload["paths"]["venv"])
    python = venv / "bin" / "python"
    if not python.exists():  # a Windows checkout, or a venv that was moved
        python = venv / "Scripts" / "python.exe"
    if args.source is not None:
        manifest, files = _release_from_directory(args.source)
        where = str(args.source)
    else:
        base_url = args.portal or ((payload.get("farm") or {}).get("portal_url") or "")
        if not base_url:
            raise SystemExit("no --from directory and no portal: set farm.portal_url, or pass --portal")
        key_file = (payload.get("farm") or {}).get("node_key_file")
        if args.token_file:
            token = Path(args.token_file).read_text(encoding="utf-8").strip()
        elif key_file and Path(key_file).is_file():
            token = Path(key_file).read_text(encoding="utf-8").strip()
        else:
            raise SystemExit("no key to ask the portal with: set farm.node_key_file, or pass --token-file")
        manifest, files = _release_from_portal(base_url, token, args.commit)
        where = base_url

    # Nothing is installed until every file is what the release says it is.
    try:
        release_document.check(manifest, files.__getitem__)
    except release_document.ReleaseError as exc:
        raise SystemExit(f"refusing to install: {exc}")

    running = _installed_version()
    print(f"release {manifest.get('version')} ({str(manifest.get('commit'))[:12]}) from {where}")
    print(f"  this host runs {running}")
    for entry in release_document.entries(manifest):
        print(f"  {entry['name']}: {entry['bytes']} bytes, sha256 {entry['sha256'][:12]}")
    if args.dry_run:
        print("--dry-run: nothing installed")
        return 0

    with tempfile.TemporaryDirectory(prefix="alteriom-hil-release-") as scratch:
        staged = Path(scratch)
        for name, body in files.items():
            (staged / name).write_bytes(body)
        wheels = [str(staged / name) for name in release_document.wheels(manifest)]
        print(f"installing into {venv}")
        done = subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--upgrade", *wheels],
                              capture_output=True, text=True)
        if done.returncode != 0:
            print(done.stdout[-4000:] or "", file=sys.stderr)
            print(done.stderr[-4000:] or "", file=sys.stderr)
            raise SystemExit(f"pip refused the release's wheels (exit {done.returncode}); nothing else was changed")
        if args.web_root:
            dashboard = staged / manifest["dashboard"]["name"]
            args.web_root.mkdir(parents=True, exist_ok=True)
            with tarfile.open(dashboard) as archive:
                _extract_dashboard(archive, args.web_root)
            print(f"dashboard bundle unpacked into {args.web_root}")

    print(f"installed {manifest.get('version')}. The service runs the old code until it restarts:")
    print("  sudo systemctl restart alteriom-hil-farm.service")
    # The release carries the health check firmware and this command does not
    # yet install it. Say so: a rig owner who reads the file list above and
    # nothing else would reasonably think their boards had been flashed
    # (docs/public-release-plan.md, step 13d).
    carried = release_document.firmware(manifest)
    if carried:
        print(f"the health check firmware {carried['version']} came with it, for "
              f"{', '.join(carried['families'])}, and is not installed yet.")
    return 0


def _extract_dashboard(archive: "tarfile.TarFile", web_root: Path) -> None:
    """The bundle's `web/` directory, into the web root, and nothing else.

    A tar may name any path it likes, including one outside where it is being
    unpacked; a release is ours and still gets no say in where it lands.
    """
    for member in archive.getmembers():
        if not member.isfile():
            continue
        parts = Path(member.name).parts
        if not parts or parts[0] != "web" or ".." in parts or Path(member.name).is_absolute():
            raise SystemExit(f"the dashboard bundle names {member.name!r}, which is not inside web/")
        target = web_root.joinpath(*parts[1:])
        target.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        if source is not None:
            target.write_bytes(source.read())


def _installed_version() -> str:
    try:
        stamped = json.loads(Path(
            os.environ.get("ALTERIOM_HIL_VERSION_FILE", "/usr/local/lib/alteriom-hil/version.json")
        ).read_text(encoding="utf-8"))
        return str(stamped.get("version") or "an unknown version")
    except (OSError, json.JSONDecodeError, AttributeError):
        return "an unknown version"


def command_backup_create(args: argparse.Namespace) -> int:
    from alteriom_hil.backup import create_backup

    payload = hil_config.require_valid(hil_config.load_config(args.config))
    settings = _backup_settings(payload)
    if not settings["enabled"] and not args.force:
        print("backup.enabled is false; nothing written (--force to make one anyway)")
        return 0
    outcome = create_backup(
        state_dir(payload), Path(args.config).parent, Path(settings["directory"]),
        keep=settings["keep"], target=settings.get("target"),
    )
    print(f"wrote {outcome['archive']} ({outcome['bytes']} bytes, {outcome['files']} files, "
          f"{outcome['pinned_bundles']} pinned bundle(s)); removed {len(outcome['removed'])} old")
    pushed = outcome["pushed"]
    if pushed is None:
        print("not copied anywhere: set backup.target to keep a copy off this host")
        return 0
    if not pushed["ok"]:
        print(f"NOT copied to {pushed['target']}: {pushed['error']}", file=sys.stderr)
        return 1
    print(f"copied to {pushed['target']}")
    return 0


def command_backup_list(args: argparse.Namespace) -> int:
    from alteriom_hil.backup import backups, last_backup

    payload = hil_config.require_valid(hil_config.load_config(args.config))
    directory = Path(_backup_settings(payload)["directory"])
    for path in backups(directory):
        print(f"{path.name}  {path.stat().st_size} bytes")
    last = last_backup(directory)
    if last:
        print(f"last: {last['created_at']}, pushed: {last.get('pushed')}")
    return 0


def command_backup_restore(args: argparse.Namespace) -> int:
    """Show what a restore writes; with --apply, write it -- as root, with the
    farm service stopped, since it replaces the database under it."""
    from alteriom_hil.backup import restore_backup

    payload = hil_config.require_valid(hil_config.load_config(args.config))
    if args.apply:
        require_root()
        if run("systemctl", "is-active", "--quiet", "alteriom-hil-farm.service", check=False).returncode == 0:
            raise hil_config.ConfigError(
                "stop the farm service first: sudo systemctl stop alteriom-hil-farm.service"
            )
    try:
        result = restore_backup(Path(args.archive), state_dir(payload), Path(args.config).parent, apply=args.apply)
    except (OSError, ValueError) as exc:
        raise hil_config.ConfigError(f"cannot restore {args.archive}: {exc}") from exc
    print(f"backup of {result['host']} made {result['created_at']}")
    for step in result["plan"]:
        print(f"  {step['action']:14} {step['to']} ({step['bytes']} bytes)")
    if not args.apply:
        print("nothing written; rerun with --apply to restore")
    else:
        print("restored; the previous database was kept beside the new one as farm.sqlite3.before-restore-*")
    print("not in any backup, provision again if missing: " + ", ".join(result["secrets_to_provision"]))
    return 0


# ---- providers --------------------------------------------------------------------
# Real services a rig validates with its owner's own credential
# (alteriom_hil.providers, docs/providers.md). The credential is read from
# stdin and written to its file; it is never an argument, never printed, and
# never part of an error.

PROVIDERS = ("callmebot",)


def _provider_error(exc: providers.ProviderError) -> hil_config.ConfigError:
    return hil_config.ConfigError(str(exc))


def secret_group(payload: dict) -> int | None:
    """The group a secret file gets: the service's, as the API token has it
    (and the runtime environment, on a node without a token)."""
    for candidate in (token_file_path(payload), hil_config.RUNTIME_ENV_PATH):
        try:
            return candidate.stat().st_gid
        except OSError:
            continue
    return None


def read_secret_line(prompt: str) -> str:
    """One line from the operator: hidden at a terminal, else the first line
    of whatever was piped in. Never from argv, where every process on the host
    and the shell history would see it."""
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass(prompt)
    return sys.stdin.readline()


def write_secret_file(payload: dict, path: Path, text: str) -> None:
    """root:<service group> 0640, its directory 0750, replaced atomically."""
    group = secret_group(payload)
    as_root = os.geteuid() == 0
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o750)
        os.chmod(path.parent, 0o750)
        if as_root and group is not None:
            os.chown(path.parent, 0, group)
    hil_config.write_atomic(path, text, 0o640)
    if as_root and group is not None:
        os.chown(path, 0, group)


def _callmebot(payload: dict) -> tuple[dict, bool]:
    configured = hil_config.callmebot_settings(payload)
    return (configured or dict(hil_config.DEFAULT_CALLMEBOT)), configured is not None


def command_providers_set(args: argparse.Namespace) -> int:
    require_root()
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    settings, configured = _callmebot(payload)
    try:
        link = providers.parse_callmebot_link(read_secret_line("CallMeBot link (input hidden): "))
    except providers.ProviderError as exc:
        raise _provider_error(exc) from None
    path = Path(settings["url_file"])
    write_secret_file(payload, path, providers.link_url(link) + "\n")
    print(f"stored {providers.redacted(link)} in {path}")
    if not configured:
        # The rig exports the file's path only when the configuration names
        # the provider; with none, the link would sit unused.
        payload["providers"] = {**(payload.get("providers") or {}), "callmebot": dict(settings)}
        hil_config.require_valid(payload)
        hil_config.write_atomic(args.config, hil_config.dump_config(payload))
        apply(payload, restart=False)
        print(f"added providers.callmebot (send: {settings['send']}, max_per_day: {settings['max_per_day']}) "
              "and applied the configuration")
        print("restart the farm service when it is idle so runs see it: "
              "sudo systemctl restart alteriom-hil-farm (or alteriom-hil-node)")
    return 0


def command_providers_show(args: argparse.Namespace) -> int:
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    settings, configured = _callmebot(payload)
    report = {"provider": "callmebot", "configured": configured}
    if configured:
        try:
            report["link"] = providers.redacted(providers.load_callmebot_link(settings["url_file"]))
        except providers.ProviderError as exc:
            report["link"] = None
            report["problem"] = str(exc)
        budget = hil_config.callmebot_budget_file(payload)
        report.update(url_file=settings["url_file"], send=settings["send"],
                      max_per_day=settings["max_per_day"], used_today=providers.budget_used(budget))
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    if not configured:
        print("callmebot: not configured; store a link with: sudo alteriom-hil-admin providers set callmebot")
        return 0
    print(f"callmebot: {report['link'] or 'no usable link'}")
    if report.get("problem"):
        print(f"  problem:     {report['problem']}")
    print(f"  file:        {report['url_file']}")
    print(f"  send:        {report['send']}")
    print(f"  budget:      {report['used_today']} of {report['max_per_day']} used today (UTC)")
    return 0


def command_providers_remove(args: argparse.Namespace) -> int:
    require_root()
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    settings, _ = _callmebot(payload)
    path = Path(settings["url_file"])
    try:
        path.unlink()
    except FileNotFoundError:
        print(f"nothing stored at {path}")
        return 0
    print(f"removed {path}; the rig sends no CallMeBot message until a link is stored again")
    return 0


def _seal_paths() -> tuple[Path, Path]:
    return Path(providers.DEFAULT_SEAL_KEY), Path(providers.DEFAULT_SEAL_PUBLIC_KEY)


def _openssl(*arguments: str) -> bytes:
    try:
        completed = subprocess.run(("openssl", *arguments), capture_output=True, check=False)
    except FileNotFoundError:
        raise hil_config.ConfigError("openssl is not installed; install it (apt-get install openssl)") from None
    if completed.returncode != 0:
        reason = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise hil_config.ConfigError(f"openssl {arguments[0]} failed: {reason[-1] if reason else completed.returncode}")
    return completed.stdout


def _public_pem_of(private_key: Path) -> str:
    """The public half of the private key, as the PEM the .pub file holds."""
    return _openssl("pkey", "-in", str(private_key), "-pubout").decode("ascii")


def _write_public_key(path: Path, pem: str) -> None:
    hil_config.write_atomic(path, pem, 0o644)
    if os.geteuid() == 0:
        os.chown(path, 0, 0)


def _der_or_none(pem: str | None) -> bytes | None:
    try:
        return providers.pem_to_der(pem) if pem is not None else None
    except providers.ProviderError:
        return None


def generate_seal_key(private_key: Path, public_key: Path) -> None:
    """A new RSA-3072 key pair: the private key root:root 0600, created that
    way rather than tightened after, and its public key 0644."""
    private_key.parent.mkdir(parents=True, exist_ok=True)
    staged = private_key.with_name(f".{private_key.name}.new")
    staged.unlink(missing_ok=True)
    previous = os.umask(0o077)
    try:
        _openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", f"rsa_keygen_bits:{providers.SEAL_KEY_BITS}",
                 "-out", str(staged))
        os.chmod(staged, 0o600)
        if os.geteuid() == 0:
            os.chown(staged, 0, 0)
        pem = _public_pem_of(staged)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    finally:
        os.umask(previous)
    staged.replace(private_key)
    _write_public_key(public_key, pem)


def command_providers_seal_key(args: argparse.Namespace) -> int:
    """The key a link is sealed to from the portal's page: made when missing
    (or with --rotate), and its fingerprint, to compare with the one the
    portal's page shows before trusting it with a link."""
    private_key, public_key = _seal_paths()
    as_root = os.geteuid() == 0
    if args.fingerprint:
        # The key that decrypts, when this user can read it; else the public
        # key the node reports.
        if as_root and private_key.is_file():
            der = providers.pem_to_der(_public_pem_of(private_key))
        else:
            try:
                der = providers.pem_to_der(public_key.read_text(encoding="ascii"))
            except OSError:
                raise hil_config.ConfigError(
                    f"{public_key} does not exist; run: sudo alteriom-hil-admin providers seal-key") from None
            except providers.ProviderError as exc:
                raise _provider_error(exc) from None
        print(providers.fingerprint(der))
        return 0
    if args.rotate or not private_key.exists():
        require_root()
        generate_seal_key(private_key, public_key)
        print(f"{'rotated' if args.rotate else 'created'} {private_key} (root, 0600) and {public_key}")
        if args.rotate:
            print("a link sealed for the old key and not yet delivered is refused; the stored link is unchanged")
    elif as_root:
        # A missing or stale public key is remade from the key that decrypts.
        pem = _public_pem_of(private_key)
        try:
            current = public_key.read_text(encoding="ascii")
        except OSError:
            current = None
        if providers.pem_to_der(pem) != _der_or_none(current):
            _write_public_key(public_key, pem)
            print(f"rewrote {public_key} from {private_key}")
    try:
        der = providers.pem_to_der(public_key.read_text(encoding="ascii"))
        bits = providers.spki_rsa_bits(der)
    except OSError:
        raise hil_config.ConfigError(
            f"{public_key} does not exist; run: sudo alteriom-hil-admin providers seal-key") from None
    except providers.ProviderError as exc:
        raise _provider_error(exc) from None
    if bits != providers.SEAL_KEY_BITS:
        raise hil_config.ConfigError(
            f"{public_key} is an RSA-{bits} key and the portal seals only to RSA-{providers.SEAL_KEY_BITS}; "
            "run: sudo alteriom-hil-admin providers seal-key --rotate")
    print(f"fingerprint (sha256): {providers.grouped_fingerprint(providers.fingerprint(der))}")
    print("the portal's page for this rig must show the same before you seal a link there")
    return 0


def command_providers_check(args: argparse.Namespace) -> int:
    """Is the stored link usable, and can this host reach the service? Sends
    nothing: the route check opens a TCP connection and closes it."""
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    settings, configured = _callmebot(payload)
    failed = False
    if not configured:
        print("FAIL  configuration: no providers.callmebot section; run: sudo alteriom-hil-admin providers set callmebot")
        failed = True
    try:
        link = providers.check_link_file(settings["url_file"])
    except providers.ProviderError as exc:
        print(f"FAIL  link: {exc}")
        failed = True
    else:
        print(f"ok    link: {providers.redacted(link)} ({settings['url_file']})")
    problem = providers.route_problem(timeout=args.timeout)
    if problem:
        print(f"FAIL  route: {problem}")
        failed = True
    else:
        print(f"ok    route: {providers.CALLMEBOT_HOST}:{providers.CALLMEBOT_PORT} accepts connections")
    if configured:
        print(f"      send: {settings['send']}, max_per_day: {settings['max_per_day']}")
    return int(failed)


# What one test message may read of CallMeBot's reply, and how long it waits.
TEST_REPLY_LIMIT = 64 * 1024
TEST_TIMEOUT_SECONDS = 20.0
# The first words of every line `providers test` ends with; rig/node-control.sh
# relays only a line that starts with one of them.
TEST_RESULT_PREFIXES = ("test message queued", "CallMeBot refused:", "CallMeBot did not queue it:",
                        "could not reach ", "no reply from ", "error: ")
_REFUSALS = {"rate_limited": "rate limited", "account_paused": "the account is paused",
             "invalid_api_key": "the stored API key is invalid; set a new link",
             "not_delivered": "not delivered"}


def _own_budget_file(budget: Path) -> None:
    """A budget file root just created belongs to whoever owns the state
    directory -- the service, whose runs take from the same budget."""
    if os.geteuid() != 0:
        return
    try:
        owner = budget.parent.stat()
        if budget.stat().st_uid != owner.st_uid:
            os.chown(budget, owner.st_uid, owner.st_gid)
    except OSError:
        pass


def command_providers_test(args: argparse.Namespace) -> int:
    """Send ONE WhatsApp message to the stored number, from this host, and say
    in one line what CallMeBot made of it. Taken from today's budget first;
    the link, the key and the number are never printed."""
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    settings, configured = _callmebot(payload)
    if not configured:
        raise hil_config.ConfigError("no providers.callmebot section; run: sudo alteriom-hil-admin providers set callmebot")
    try:
        link = providers.load_callmebot_link(settings["url_file"])
    except providers.ProviderError as exc:
        raise _provider_error(exc) from None
    redactor = providers.Redactor.for_callmebot(link)
    budget = Path(hil_config.callmebot_budget_file(payload))
    allowance = int(settings["max_per_day"])
    try:
        allowed = providers.consume_budget(budget, allowance)
    except OSError as exc:
        raise hil_config.ConfigError(redactor.scrub(
            f"cannot record today's CallMeBot budget in {budget}: {exc.strerror or exc}")) from None
    if not allowed:
        raise hil_config.ConfigError(
            f"today's CallMeBot budget on this rig is used ({allowance} of {allowance}); nothing was sent")
    _own_budget_file(budget)
    rig = (payload.get("farm") or {}).get("worker_name") or providers.worker_name()
    text = providers.callmebot_test_message(
        rig=rig, policy=settings["send"], used=providers.budget_used(budget), max_per_day=allowance,
        redactor=redactor)
    host = providers.CALLMEBOT_HOST
    try:
        with urlopen(Request(providers.message_url(link, text), method="GET"),
                     timeout=args.timeout) as response:
            status = int(response.status)
            body = response.read(TEST_REPLY_LIMIT)
    except HTTPError as exc:
        status = int(exc.code)
        try:
            body = exc.read(TEST_REPLY_LIMIT)
        except OSError:
            body = b""
    except TimeoutError as exc:
        print(redactor.scrub(f"no reply from {host} within {args.timeout:g}s ({exc}); the message may still arrive"))
        return 1
    except (URLError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        if isinstance(reason, TimeoutError):
            print(redactor.scrub(f"no reply from {host} within {args.timeout:g}s; the message may still arrive"))
        else:
            print(redactor.scrub(f"could not reach {host}: {reason}"))
        return 1
    reply = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
    verdict = providers.judge_callmebot_reply(status, reply)
    if verdict == "queued":
        print(f"test message queued by CallMeBot (HTTP {status})")
        return 0
    if verdict in _REFUSALS:
        print(f"CallMeBot refused: {_REFUSALS[verdict]} (HTTP {status})")
    else:
        print(f"CallMeBot did not queue it: an unrecognized reply (HTTP {status})")
    return 1


def board_map_path(args: argparse.Namespace) -> Path:
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    return Path(payload["paths"]["board_map"])


def auto_register(payload: dict) -> bool:
    """Whether a board this discovery finds registers itself.

    The rig's setting, read here as the service reads it: discovery from the
    command line and discovery from the portal are the same discovery, and a
    rig where one registers boards and the other does not is a rig nobody can
    reason about (a rig in bring-up, 2026-09-16: four boards found, none registered).
    """
    return bool((payload.get("inventory") or {}).get("auto_register", True))


def board_registry_path(args: argparse.Namespace) -> Path:
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    return Path(payload["paths"].get("inventory") or payload["paths"]["board_map"])


def load_boards(path: Path, allow_missing: bool = False) -> list[dict]:
    try:
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("boards") or []
    except FileNotFoundError:
        if allow_missing:
            return []
        raise hil_config.ConfigError(f"board map does not exist: {path}")
    except (OSError, yaml.YAMLError) as exc:
        raise hil_config.ConfigError(f"cannot read board map {path}: {exc}") from exc


def validate_boards(path: Path, boards: list[dict]) -> None:
    if not boards:
        raise hil_config.ConfigError(f"board map {path} has no boards")
    try:
        BoardMap([Board(**entry) for entry in boards])
    except (TypeError, ValueError) as exc:
        raise hil_config.ConfigError(f"invalid board map: {exc}") from exc


def validate_inventory(path: Path, boards: list[dict]) -> None:
    if not boards:
        raise hil_config.ConfigError(f"inventory registry {path} has no boards")
    try:
        validate_registry([Board(**entry) for entry in boards])
    except (TypeError, ValueError) as exc:
        raise hil_config.ConfigError(f"invalid inventory registry: {exc}") from exc


def command_boards_list(args: argparse.Namespace) -> int:
    path = board_map_path(args)
    boards = load_boards(path)
    validate_boards(path, boards)
    if args.json:
        print(json.dumps(boards, indent=2))
    else:
        print(f"{'ID':20} {'TARGET':12} {'PORT':28} POWER")
        for board in boards:
            power = f"{board.get('power_hub', '-')}/{board.get('power_port', '-')}"
            print(f"{board.get('id', '-'):20} {board.get('target', '-'):12} {board.get('port', '-'):28} {power}")
    return 0


def command_boards_validate(args: argparse.Namespace) -> int:
    path = board_map_path(args)
    boards = load_boards(path)
    validate_boards(path, boards)
    print(f"valid: {path} ({len(boards)} board(s))")
    return 0


def state_dir(payload: dict) -> Path:
    return Path(payload["paths"].get("state") or "/var/lib/alteriom-hil")


def publish_after_change(payload: dict) -> None:
    """Re-probe and republish the fleet after the registry changed, so the
    active board map, health, the API, and the dashboard reflect the new
    registration now instead of at the next service discovery. Best effort:
    the registry write already happened, and a probe problem is reported,
    not fatal."""
    registry = payload["paths"].get("inventory")
    board_map = payload["paths"]["board_map"]
    if not registry or Path(registry) == Path(board_map):
        return  # legacy single-file mode: the board map is the registry
    try:
        with rig_lock():
            snapshot = publish_inventory(registry, board_map, state_dir(payload),
                                        auto_register=auto_register(payload))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"warning: fleet not republished ({exc}); run: alteriom-hil-admin boards discover", file=sys.stderr)
        return
    print(
        f"fleet republished: {len(snapshot['boards'])} connected, "
        f"{len(snapshot['missing'])} missing, {len(snapshot['unregistered'])} unregistered; "
        f"instruments: {len(snapshot.get('instruments') or [])} connected, "
        f"{len(snapshot.get('missing_instruments') or [])} missing"
    )
    for error in snapshot["probe_errors"]:
        print(f"  probe error {error['port']}: {error['error']}", file=sys.stderr)


def command_boards_discover(args: argparse.Namespace) -> int:
    payload = hil_config.require_valid(hil_config.load_config(args.config))
    registry = payload["paths"].get("inventory")
    if not registry:
        raise hil_config.ConfigError("paths.inventory is required for discovery")
    try:
        with rig_lock():
            report = publish_inventory(registry, payload["paths"]["board_map"], state_dir(payload),
                                       auto_register=auto_register(payload))
    except (OSError, ValueError, RuntimeError) as exc:
        raise hil_config.ConfigError(f"inventory discovery failed: {exc}") from exc
    errors = report["probe_errors"]
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"connected: {len(report['boards'])}; missing: {len(report['missing'])}; "
            f"unregistered: {len(report['unregistered'])}"
        )
        for board in report["boards"]:
            print(f"  {board['id']:20} {board['target']:10} {board['mac']} {board['port']}")
        for board_id in report.get("registered") or []:
            # Registered by this discovery, so say the one thing an operator
            # may still need to correct: an instrument looks like a board.
            print(f"  registered {board_id} -- it was plugged in and nobody had")
            print(f"      an instrument? sudo alteriom-hil-admin instruments add --id <id> "
                  f"--mac <its mac> --port <its port> --kind esp32-io --replace-board")
        for board_id in report["missing"]:
            print(f"  {board_id:20} MISSING    registered, not found on any port")
        for item in report.get("instruments") or []:
            print(f"  {item['id']:20} {item['kind']:10} {item['mac']} {item['port']}  instrument, {len(item.get('wiring') or [])} wire(s)")
        for instrument_id in report.get("missing_instruments") or []:
            print(f"  {instrument_id:20} MISSING    registered instrument, not found on any port")
        for device in report["unregistered"]:
            print(f"  UNREGISTERED         {device['target']:10} {device['mac']} {device['port']}")
            print(
                "      register with: sudo alteriom-hil-admin boards add --id <id> "
                f"--mac {device['mac']} --port {device['port']} --target {device['target']}"
            )
            kinds = [name for name, kind in KINDS.items() if kind.target == device["target"]]
            if kinds:
                print(
                    "      or, if it is test equipment: sudo alteriom-hil-admin instruments add --id <id> "
                    f"--mac {device['mac']} --port {device['port']} --kind {kinds[0]}"
                )
        for hint in transport_hints(report["boards"], report["unregistered"]):
            print(f"  NOTE                 {hint}")
        for error in errors:
            print(f"  PROBE ERROR          {error['port']}: {error['error']}")
    return int(bool(errors))


# From the family descriptors: which consoles are the chip's own USB.
NATIVE_USB_TARGETS = {name for name, family in load_families().items() if family.native_usb}


def transport_hints(boards, unregistered) -> list[str]:
    """Native-USB families put the agent console on their USB-Serial/JTAG port.

    A C3/C5/C6/S3 reached through a UART bridge still answers esptool, so
    discovery succeeds, but the flashed agent would then talk to the other
    connector and every suite would time out on that board. Items are the
    dicts of a published snapshot.
    """
    hints = []
    for item in list(boards) + list(unregistered):
        if item.get("target") in NATIVE_USB_TARGETS and item.get("transport") == "uart-bridge":
            label = item.get("id") or item.get("mac")
            hints.append(
                f"{label} ({item['target']}) is connected through a UART bridge on {item['port']}; "
                "move the cable to the board's native USB connector"
            )
    return hints


def refresh_health() -> None:
    run("systemctl", "reset-failed", "alteriom-hil-health.service", check=False)
    run("systemctl", "start", "alteriom-hil-health.service", check=False)


def command_boards_add(args: argparse.Namespace) -> int:
    require_root()
    path = board_registry_path(args)
    boards = load_boards(path, allow_missing=True)
    if (args.power_hub is None) != (args.power_port is None):
        raise hil_config.ConfigError("--power-hub and --power-port must be supplied together")
    entry = {
        "id": args.id,
        "port": args.port,
        "chip": TARGET_CHIPS[args.target],
        "target": args.target,
        "mac": getattr(args, "mac", None),
        "baud": args.baud,
        "flash_baud": args.flash_baud,
        "tags": args.tag or ["mesh"],
    }
    if args.power_hub is not None:
        entry.update(power_hub=args.power_hub, power_port=args.power_port)
    boards.append(entry)
    validate_inventory(path, boards)
    hil_config.write_atomic(path, yaml.safe_dump({"boards": boards}, sort_keys=False))
    print(f"added {args.id} to {path}")
    publish_after_change(hil_config.require_valid(hil_config.load_config(args.config)))
    refresh_health()
    return 0


def command_boards_remove(args: argparse.Namespace) -> int:
    require_root()
    path = board_registry_path(args)
    boards = load_boards(path)
    remaining = [board for board in boards if board.get("id") != args.id]
    if len(remaining) == len(boards):
        raise hil_config.ConfigError(f"unknown board: {args.id}")
    _, instruments, _ = load_instrument_registry(args)
    wired = wired_to(instruments, args.id)
    if wired:
        raise hil_config.ConfigError(f"{args.id} is wired to {', '.join(wired)}; unwire it first")
    hil_config.write_atomic(path, yaml.safe_dump({"boards": remaining}, sort_keys=False))
    print(f"removed {args.id} from {path}")
    publish_after_change(hil_config.require_valid(hil_config.load_config(args.config)))
    refresh_health()
    return 0


# ---- instruments ---------------------------------------------------------------
# Test equipment the farm owns, registered by MAC like a board and kept apart
# from the boards: see alteriom_hil.instrument and docs/io-instrument.md.


def load_instrument_registry(args: argparse.Namespace) -> tuple[Path, list[Instrument], list[Board]]:
    boards_path = board_registry_path(args)
    try:
        boards = [Board(**entry) for entry in load_boards(boards_path, allow_missing=True)]
    except (TypeError, ValueError) as exc:
        raise hil_config.ConfigError(f"invalid inventory registry: {exc}") from exc
    path = instruments_path_for(boards_path)
    try:
        return path, load_instruments(path, boards), boards
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise hil_config.ConfigError(f"invalid instrument registry {path}: {exc}") from exc


def save_instruments(path: Path, instruments: list[Instrument], boards: list[Board]) -> None:
    try:
        validate_instruments(instruments, boards)
    except ValueError as exc:
        raise hil_config.ConfigError(str(exc)) from exc
    hil_config.write_atomic(path, instruments_document(instruments))


def find_instrument(instruments: list[Instrument], instrument_id: str) -> Instrument:
    found = next((item for item in instruments if item.id == instrument_id), None)
    if found is None:
        raise hil_config.ConfigError(f"unknown instrument: {instrument_id}")
    return found


def command_instruments_list(args: argparse.Namespace) -> int:
    path, instruments, _ = load_instrument_registry(args)
    if args.json:
        print(json.dumps([item.as_dict() for item in instruments], indent=2))
        return 0
    if not instruments:
        print(f"no instruments registered in {path}")
        return 0
    print(f"{'ID':20} {'KIND':10} {'MAC':18} PORT")
    for item in instruments:
        print(f"{item.id:20} {item.kind:10} {item.mac:18} {item.port}")
        for wire in item.wiring:
            note = f"  ({wire.note})" if wire.note else ""
            print(f"    channel {wire.channel:>2} -> {wire.board} GPIO{wire.pin}{note}")
    return 0


def command_instruments_add(args: argparse.Namespace) -> int:
    require_root()
    path, instruments, boards = load_instrument_registry(args)
    # An instrument is an ESP like any board, and on a rig that registers its
    # own boards it will already be one: discovery cannot tell them apart, and
    # only an operator can. A MAC that is a registered board is still refused
    # -- flashing a suite onto an instrument and meshing it with the rig is
    # what that refusal prevents -- but `--replace-board` is the operator
    # saying which it is, and takes it out of the board pool.
    normalized = normalize_mac(args.mac)
    was_a_board = next((board for board in boards if board.mac and normalize_mac(board.mac) == normalized), None)
    if was_a_board is not None and getattr(args, "replace_board", False):
        boards = [board for board in boards if board.id != was_a_board.id]
        write_registry(board_registry_path(args), boards)
    else:
        was_a_board = None
    try:
        instruments.append(Instrument(id=args.id, kind=args.kind, mac=args.mac, port=args.port))
    except ValueError as exc:
        raise hil_config.ConfigError(str(exc)) from exc
    save_instruments(path, instruments, boards)
    print(f"added instrument {args.id} to {path}")
    if was_a_board is not None:
        print(f"  {was_a_board.id} was registered as a board; it is an instrument now, not a test node")
    publish_after_change(hil_config.require_valid(hil_config.load_config(args.config)))
    return 0


def command_instruments_remove(args: argparse.Namespace) -> int:
    require_root()
    path, instruments, boards = load_instrument_registry(args)
    find_instrument(instruments, args.id)
    save_instruments(path, [item for item in instruments if item.id != args.id], boards)
    print(f"removed instrument {args.id} from {path}")
    publish_after_change(hil_config.require_valid(hil_config.load_config(args.config)))
    return 0


def command_instruments_wire(args: argparse.Namespace) -> int:
    require_root()
    path, instruments, boards = load_instrument_registry(args)
    item = find_instrument(instruments, args.id)
    existing = next((wire for wire in item.wiring if wire.channel == args.channel), None)
    if existing is not None:
        raise hil_config.ConfigError(
            f"{args.id} channel {args.channel} is already wired to {existing.board} "
            f"GPIO{existing.pin}; unwire it first"
        )
    item.wiring.append(Wire(channel=args.channel, board=args.board, pin=args.pin, note=args.note))
    save_instruments(path, instruments, boards)
    print(f"wired {args.id} channel {args.channel} to {args.board} GPIO{args.pin}")
    publish_after_change(hil_config.require_valid(hil_config.load_config(args.config)))
    return 0


def command_instruments_unwire(args: argparse.Namespace) -> int:
    require_root()
    path, instruments, boards = load_instrument_registry(args)
    item = find_instrument(instruments, args.id)
    remaining = [wire for wire in item.wiring if wire.channel != args.channel]
    if len(remaining) == len(item.wiring):
        raise hil_config.ConfigError(f"{args.id} channel {args.channel} is not wired")
    item.wiring = remaining
    save_instruments(path, instruments, boards)
    print(f"unwired {args.id} channel {args.channel}")
    publish_after_change(hil_config.require_valid(hil_config.load_config(args.config)))
    return 0


def command_instruments_probe(args: argparse.Namespace) -> int:
    """Ask a connected instrument what it is, under the rig lock.

    The bring-up check: the MAC says which device it is, and only the answer
    says it runs the instrument firmware -- a board still carrying a suite's
    agent answers `info` too. Every channel is released afterwards.
    """
    import serial

    from alteriom_hil.instrument import InstrumentClient
    from alteriom_hil.protocol import ProtocolError, TimeoutWaitingFor
    from alteriom_hil.serial_capture import SerialCapture

    payload = hil_config.require_valid(hil_config.load_config(args.config))
    _, instruments, _ = load_instrument_registry(args)
    item = find_instrument(instruments, args.id)
    # The port discovery last saw it on, which the registry only hints at.
    try:
        snapshot = json.loads((state_dir(payload) / "inventory.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        snapshot = {}
    seen = next((entry for entry in snapshot.get("instruments") or [] if entry.get("id") == args.id), None)
    port = (seen or {}).get("port") or item.port
    if not port:
        raise hil_config.ConfigError(f"{args.id} has no known port; run: alteriom-hil-admin boards discover")

    def opener():
        stream = serial.Serial(port, 115200, timeout=0.1, rtscts=False, dsrdtr=False)
        stream.rts = False
        stream.dtr = False
        return stream

    with rig_lock():
        capture = SerialCapture(opener).start()
        try:
            client = InstrumentClient(item.id, capture, kind=item.kind)
            info = client.info(timeout=args.timeout)
            client.release()
        except (ProtocolError, TimeoutWaitingFor) as exc:
            raise hil_config.ConfigError(f"{args.id} on {port}: {exc}") from exc
        finally:
            capture.stop()
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    if info.get("mac") and str(info["mac"]).lower() != item.mac:
        print(f"warning: {port} answered with MAC {info['mac']}, registered as {item.mac}", file=sys.stderr)
    print(
        f"{args.id} on {port}: {info.get('kind')} protocol {info.get('protocol')}, "
        f"firmware {info.get('fw') or 'unknown'}, {len(info.get('channels') or [])} channels, all released"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="alteriom-hil-admin", description=__doc__)
    root.add_argument("--version", action="version", version="alteriom-hil-admin 1.0")
    root.add_argument("--config", type=Path, default=hil_config.CONFIG_PATH)
    commands = root.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="show saved or live health")
    status.add_argument("--live", action="store_true")
    status.add_argument("--json", action="store_true")
    status.add_argument("--strict", action="store_true")
    status.set_defaults(func=command_status)

    config = commands.add_parser("config", help="inspect or change configuration")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    show = config_commands.add_parser("show")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=command_config_show)
    validate = config_commands.add_parser("validate")
    validate.set_defaults(func=command_config_validate)
    set_command = config_commands.add_parser("set")
    set_command.add_argument("key")
    set_command.add_argument("value")
    set_command.add_argument("--no-restart", action="store_true")
    set_command.set_defaults(func=command_config_set)
    set_many = config_commands.add_parser("set-many", help="set remote settings from a JSON file")
    set_many.add_argument("--file", required=True)
    set_many.set_defaults(func=command_config_set_many)
    join = config_commands.add_parser("join", help="make this host a node of a portal (join-rig.sh)")
    join.add_argument("--portal", required=True, help="the portal's https:// URL")
    join.add_argument("--name", required=True, help="the rig's worker name, which its node key is named for")
    join.add_argument("--key-file", default=hil_config.DEFAULT_NODE_KEY_FILE)
    join.set_defaults(func=command_config_join)
    apply_command = config_commands.add_parser("apply")
    apply_command.add_argument("--no-restart", action="store_true")
    apply_command.set_defaults(func=command_config_apply)

    health = commands.add_parser("health", help="manage health snapshots")
    health_commands = health.add_subparsers(dest="health_command", required=True)
    refresh = health_commands.add_parser("refresh")
    refresh.add_argument("--json", action="store_true")
    refresh.set_defaults(func=command_health_refresh)

    service = commands.add_parser("service", help="control the Actions runner service")
    service.add_argument("action", choices=("status", "start", "stop", "restart"))
    service.set_defaults(func=command_service)

    keys = commands.add_parser("keys", help="named API keys and their roles")
    key_commands = keys.add_subparsers(dest="keys_command", required=True)
    key_list = key_commands.add_parser("list")
    key_list.add_argument("--json", action="store_true")
    key_list.set_defaults(func=command_keys_list)
    key_create = key_commands.add_parser("create", help="make a key; it is printed once")
    key_create.add_argument("--name", required=True, help="who holds it, e.g. a person or a CI")
    key_create.add_argument("--role", required=True, choices=ROLES)
    key_create.add_argument("--note")
    key_create.set_defaults(func=command_keys_create)
    key_revoke = key_commands.add_parser("revoke")
    key_revoke.add_argument("name")
    key_revoke.set_defaults(func=command_keys_revoke)
    notify = commands.add_parser("notify", help="where the farm says it broke")
    notify_commands = notify.add_subparsers(dest="notify_command", required=True)
    notify_set = notify_commands.add_parser("set", help="point notifications at a channel")
    notify_set.add_argument("--channel", choices=("webhook", "telegram", "callmebot"), required=True)
    notify_set.add_argument("--chat-id", help="Telegram: the chat to send to")
    notify_set.add_argument("--format", choices=("slack", "discord", "json"),
                            help="webhook: how the message is shaped")
    notify_set.add_argument("--id", help="replace this channel instead of adding one")
    notify_set.set_defaults(func=command_notify_set)
    notify_show = notify_commands.add_parser("show", help="every channel this host notifies through")
    notify_show.set_defaults(func=command_notify_show)
    notify_test = notify_commands.add_parser("test", help="send a test message down one channel")
    notify_test.add_argument("--id", help="which channel, when this host has several")
    notify_test.set_defaults(func=command_notify_test)
    notify_tune = notify_commands.add_parser("tune", help="turn one channel off or on, or choose what it says")
    notify_tune.add_argument("--id", help="which channel, when this host has several")
    notify_tune.add_argument("--on", action="store_true", help="send down it again")
    notify_tune.add_argument("--off", action="store_true", help="stop sending down it, keeping its credential")
    notify_tune.add_argument("--events", help=f"comma-separated: {', '.join(hil_config.NOTIFY_EVENTS)}")
    notify_tune.set_defaults(func=command_notify_tune)
    notify_remove = notify_commands.add_parser("remove", help="stop sending down one channel")
    notify_remove.add_argument("--id", help="which channel, when this host has several")
    notify_remove.set_defaults(func=command_notify_remove)

    upgrade = commands.add_parser(
        "upgrade", help="install a release's packages (docs/public-release-plan.md, step 13)")
    source = upgrade.add_mutually_exclusive_group()
    source.add_argument("--from", dest="source", type=Path, default=None,
                        help="a directory holding release.json and its files (runner/ci/build-release.sh --out)")
    source.add_argument("--portal", default=None, help="the portal to take the release from (default: farm.portal_url)")
    upgrade.add_argument("--commit", default=None, help="a particular release (default: the portal's current one)")
    upgrade.add_argument("--token-file", default=None, help="the key to ask the portal with (default: farm.node_key_file)")
    upgrade.add_argument("--web-root", type=Path, default=None,
                         help="unpack the release's dashboard bundle here as well")
    upgrade.add_argument("--dry-run", action="store_true", help="say what would be installed, and stop")
    upgrade.set_defaults(func=command_upgrade)

    backup = commands.add_parser("backup", help="back the farm up, and restore it")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    backup_create = backup_commands.add_parser("create", help="write a backup now (what the nightly timer runs)")
    backup_create.add_argument("--force", action="store_true", help="even when backup.enabled is false")
    backup_create.set_defaults(func=command_backup_create)
    backup_list = backup_commands.add_parser("list")
    backup_list.set_defaults(func=command_backup_list)
    backup_restore = backup_commands.add_parser("restore", help="show, or with --apply write, a restore")
    backup_restore.add_argument("archive")
    backup_restore.add_argument("--apply", action="store_true")
    backup_restore.set_defaults(func=command_backup_restore)

    provider = commands.add_parser("providers", help="real services the rig validates with its owner's credential")
    provider_commands = provider.add_subparsers(dest="providers_command", required=True)
    provider_set = provider_commands.add_parser(
        "set", help="store a provider's link, read from stdin (hidden at a terminal), never from an argument"
    )
    provider_set.add_argument("provider", choices=PROVIDERS)
    provider_set.set_defaults(func=command_providers_set)
    provider_show = provider_commands.add_parser("show", help="the stored link redacted, the send policy and today's budget")
    provider_show.add_argument("--json", action="store_true")
    provider_show.set_defaults(func=command_providers_show)
    provider_remove = provider_commands.add_parser("remove", help="delete a provider's stored link")
    provider_remove.add_argument("provider", choices=PROVIDERS)
    provider_remove.set_defaults(func=command_providers_remove)
    provider_check = provider_commands.add_parser(
        "check", help="validate the stored link and the route to the service, without sending anything"
    )
    provider_check.add_argument("provider", choices=PROVIDERS)
    provider_check.add_argument("--timeout", type=float, default=3.0)
    provider_check.set_defaults(func=command_providers_check)
    provider_test = provider_commands.add_parser(
        "test", help="send ONE real test message from this host, taken from today's budget, and print the verdict"
    )
    provider_test.add_argument("provider", choices=PROVIDERS)
    provider_test.add_argument("--timeout", type=float, default=TEST_TIMEOUT_SECONDS)
    provider_test.set_defaults(func=command_providers_test)
    provider_seal = provider_commands.add_parser(
        "seal-key", help="make the key a link is sealed to from the portal, if missing, and print its fingerprint"
    )
    provider_seal.add_argument("--rotate", action="store_true", help="replace the key with a new one")
    provider_seal.add_argument("--fingerprint", action="store_true", help="print only the fingerprint, as hex")
    provider_seal.set_defaults(func=command_providers_seal_key)

    boards = commands.add_parser("boards", help="inspect configured boards")
    board_commands = boards.add_subparsers(dest="boards_command", required=True)
    board_list = board_commands.add_parser("list")
    board_list.add_argument("--json", action="store_true")
    board_list.set_defaults(func=command_boards_list)
    board_validate = board_commands.add_parser("validate")
    board_validate.set_defaults(func=command_boards_validate)
    board_discover = board_commands.add_parser("discover", help="probe ESPs and generate the active board map")
    board_discover.add_argument("--json", action="store_true")
    board_discover.set_defaults(func=command_boards_discover)
    board_add = board_commands.add_parser("add")
    board_add.add_argument("--id", required=True)
    board_add.add_argument("--port", required=True)
    board_add.add_argument("--target", required=True, choices=tuple(TARGET_CHIPS))
    board_add.add_argument("--mac", help="stable ESP eFuse MAC reported by boards discover")
    board_add.add_argument("--power-hub")
    board_add.add_argument("--power-port", type=int)
    board_add.add_argument("--baud", type=int, default=115200)
    board_add.add_argument("--flash-baud", type=int, default=460800)
    board_add.add_argument("--tag", action="append", default=[])
    board_add.set_defaults(func=command_boards_add)
    board_remove = board_commands.add_parser("remove")
    board_remove.add_argument("id")
    board_remove.set_defaults(func=command_boards_remove)

    instruments = commands.add_parser("instruments", help="test equipment wired to boards")
    instrument_commands = instruments.add_subparsers(dest="instruments_command", required=True)
    instrument_list = instrument_commands.add_parser("list", help="registered instruments and their wiring")
    instrument_list.add_argument("--json", action="store_true")
    instrument_list.set_defaults(func=command_instruments_list)
    instrument_add = instrument_commands.add_parser("add", help="register an instrument by its MAC")
    instrument_add.add_argument("--id", required=True)
    instrument_add.add_argument("--mac", required=True, help="the eFuse MAC boards discover reports")
    instrument_add.add_argument("--port", required=True)
    instrument_add.add_argument("--kind", default="esp32-io", choices=tuple(KINDS))
    instrument_add.add_argument(
        "--replace-board", action="store_true",
        help="this MAC is registered as a board (a rig that registers its own): take it out of the pool",
    )
    instrument_add.set_defaults(func=command_instruments_add)
    instrument_remove = instrument_commands.add_parser("remove")
    instrument_remove.add_argument("id")
    instrument_remove.set_defaults(func=command_instruments_remove)
    instrument_wire = instrument_commands.add_parser("wire", help="record a jumper from a channel to a board pin")
    instrument_wire.add_argument("id")
    instrument_wire.add_argument("--channel", type=int, required=True, help="the instrument's GPIO")
    instrument_wire.add_argument("--board", required=True, help="a registered board id")
    instrument_wire.add_argument("--pin", type=int, required=True, help="the board's GPIO")
    instrument_wire.add_argument("--note")
    instrument_wire.set_defaults(func=command_instruments_wire)
    instrument_unwire = instrument_commands.add_parser("unwire")
    instrument_unwire.add_argument("id")
    instrument_unwire.add_argument("--channel", type=int, required=True)
    instrument_unwire.set_defaults(func=command_instruments_unwire)
    instrument_probe = instrument_commands.add_parser("probe", help="ask a connected instrument what it is")
    instrument_probe.add_argument("id")
    instrument_probe.add_argument("--timeout", type=float, default=10.0)
    instrument_probe.add_argument("--json", action="store_true")
    instrument_probe.set_defaults(func=command_instruments_probe)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except hil_config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
