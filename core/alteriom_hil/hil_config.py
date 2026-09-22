#!/usr/bin/env python3
"""Load, validate, and materialize the HIL service configuration."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

import yaml

# A farm host keeps it in /etc/alteriom-hil. A portal in a container has its
# own, mounted where the deployment says (ALTERIOM_HIL_CONFIG): the settings a
# portal decides with -- quarantine, retention -- are read from it.
CONFIG_PATH = Path(os.environ.get("ALTERIOM_HIL_CONFIG") or "/etc/alteriom-hil/config.yaml")
RUNTIME_ENV_PATH = Path("/etc/alteriom-hil/runtime.env")
ALLOWED_KEYS = {
    "schema": None,
    "mode": None,
    "runner": {"unit": None},
    "paths": {"venv": None, "board_map": None, "inventory": None, "repo": None, "state": None},
    "health": {
        "interval_minutes": None,
        "minimum_boards": None,
        "disk_warn_percent": None,
        "disk_critical_percent": None,
    },
    "gateway": {
        "enabled": None,
        "ssid": None,
        "password_file": None,
        "endpoint": None,
        "channel": None,
    },
    "mqtt": {"enabled": None, "url": None},
    "inventory": {"auto_register": None},
    "service": {
        "enabled": None,
        "bind": None,
        "port": None,
        "token_file": None,
        "public_host": None,
    },
    "notify": {"id": None, "enabled": None, "channel": None, "webhook_url_file": None,
               "format": None, "events": None, "token_file": None, "chat_id": None},
    "queue": {"concurrency": None},
    "quarantine": {"enabled": None, "after_failures": None},
    "farm": {"mode": None, "portal_url": None, "worker_name": None, "node_key_file": None},
    "backup": {"enabled": None, "directory": None, "keep": None, "target": None},
    "retention": {
        "enabled": None,
        "run_evidence_days": None,
        "log_days": None,
        "keep_newest_runs": None,
        "workspace_days": None,
    },
    "providers": {"callmebot": {"url_file": None, "send": None, "max_per_day": None}},
}
# What the farm tells a person about, and in which shape a webhook takes it
# (alteriom_hil.notify).
NOTIFY_EVENTS = ("queue_paused", "board_red", "host_unhealthy")
NOTIFY_FORMATS = ("slack", "discord", "json")
# The ways a farm can carry a message. Declared in alteriom_hil.notify, so a
# channel added there is configurable here without a second list.
NOTIFY_CHANNELS = ("webhook", "telegram", "callmebot")
# A Telegram chat id: a user, or a group, which is negative and longer.
CHAT_ID_PATTERN = re.compile(r"-?\d{1,20}")
NOTIFY_FORMATS
DEFAULT_BACKUP = {"enabled": True, "directory": "/var/lib/alteriom-hil/backups", "keep": 14}
# Off unless a host asks for it: deleting evidence is a decision, and a host
# upgraded into this must not start doing it on its own.
DEFAULT_RETENTION = {
    "enabled": False,
    "run_evidence_days": 90,
    "log_days": 90,
    "keep_newest_runs": 100,
    "workspace_days": 2,
}
# Off unless a host asks for it: a quarantine leaves a board out of the
# release gate's runs, and a host upgraded into this should not start doing
# that by itself.
DEFAULT_QUARANTINE = {"enabled": False, "after_failures": 2}
# How the host takes part in the farm (docs/portal-plan.md). `attached` keeps
# the standalone service -- and every CI job that submits to it -- and runs a
# node agent beside it, so a portal sees and uses the rig before anything is
# moved onto it.
FARM_MODES = ("standalone", "attached", "portal", "node")
DEFAULT_GATEWAY = {
    "enabled": False,
    "ssid": "Alteriom-HIL",
    "password_file": "/etc/alteriom-hil/gateway-wifi-password",
    "endpoint": "http://10.42.0.1:8088",
    # The 2.4 GHz channel this rig's AP runs on, and so the channel a board
    # promoted to bridge takes its mesh to. 1 is painlessMesh's own default,
    # where a mesh that has no bridge yet roots: equal, nothing splits
    # (setup-gateway-network.sh). A rig configured before this setting
    # existed has no channel and is read as 1.
    "channel": 1,
}
GATEWAY_DEFAULT_CHANNEL = 1
# The broker a run's gateway board publishes to and a suite reads from --
# on the farm host, reachable from the AP and loopback (setup-mqtt-broker.sh).
# Host configuration rather than a profile setting for the same reason the
# Wi-Fi is: a profile cannot invent a broker the host does not have.
DEFAULT_MQTT = {"enabled": False, "url": "mqtt://10.42.0.1:1883"}
# A board plugged into a rig is a board the rig should have. Naming each one
# by hand taught nobody anything, and on a new rig it was the step between
# "the hub is connected" and a farm that still reports no boards -- so a
# discovery registers what it finds, as `<family>-<last four of the MAC>`,
# the name the dashboard suggested to operators anyway. Off for a rig whose
# bank is deliberate: an instrument, a board on loan, a bench experiment.
DEFAULT_INVENTORY = {"auto_register": True}
# Real third-party services a rig validates with its owner's own credential
# (alteriom_hil.providers, docs/providers.md). Absent, the rig has none and the
# rows that need one skip. The secret stays in url_file; `send` says when a
# run may spend a real message, and max_per_day caps how many it may.
# Kept equal to alteriom_hil.providers (a test holds them together) so this
# module stays loadable without the HAL.
PROVIDER_SEND_POLICIES = ("never", "release", "always")
DEFAULT_CALLMEBOT = {
    "url_file": "/etc/alteriom-hil/providers/callmebot-url",
    "send": "release",
    "max_per_day": 5,
}
DEFAULT_STATE_DIR = "/var/lib/alteriom-hil"


class ConfigError(ValueError):
    pass


def load_config(path: Path = CONFIG_PATH) -> dict:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("configuration root must be a mapping")
    # Per key, not per section: a rig configured before a gateway setting
    # existed keeps its own values and gains the default for the new one --
    # otherwise `config set gateway.channel` on that rig would be setting a
    # key with no type to follow, and would write the string "1". A gateway
    # that is not a mapping at all (`gateway: enabled`) is left exactly as
    # written, so validate_config says so instead of this raising.
    gateway = payload.get("gateway")
    payload["gateway"] = (
        {**DEFAULT_GATEWAY, **gateway} if isinstance(gateway, dict)
        else dict(DEFAULT_GATEWAY) if gateway is None
        else gateway
    )
    payload.setdefault("mqtt", dict(DEFAULT_MQTT))
    payload["inventory"] = {**DEFAULT_INVENTORY, **(payload.get("inventory") or {})} \
        if isinstance(payload.get("inventory"), (dict, type(None))) else payload["inventory"]
    payload.setdefault("backup", dict(DEFAULT_BACKUP))
    payload.setdefault("retention", dict(DEFAULT_RETENTION))
    payload.setdefault("quarantine", dict(DEFAULT_QUARANTINE))
    return payload


def _whole(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _unknown_keys(payload: dict, allowed: dict, prefix: str = "") -> list[str]:
    errors = []
    for key, value in payload.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if key not in allowed:
            errors.append(f"unknown setting: {dotted}")
        elif isinstance(allowed[key], dict) and isinstance(value, dict):
            errors.extend(_unknown_keys(value, allowed[key], dotted))
    return errors


def validate_config(payload: dict) -> list[str]:
    errors = _unknown_keys(payload, ALLOWED_KEYS)
    if isinstance(payload.get("notify"), list):
        for index, channel in enumerate(payload["notify"], start=1):
            if isinstance(channel, dict):
                errors.extend(_unknown_keys(channel, ALLOWED_KEYS["notify"], f"notify[{index}]"))
    if payload.get("schema") != 2:
        errors.append("schema must be 2")
    if payload.get("mode") != "hardware":
        errors.append("mode must be hardware")

    runner = payload.get("runner")
    farm_mode = (payload.get("farm") or {}).get("mode") if isinstance(payload.get("farm"), dict) else None
    if not isinstance(runner, dict):
        errors.append("runner must be a mapping")
    elif runner.get("unit") is None and farm_mode == "node":
        # A node has no GitHub runner: its portal hands it work and releases.
        pass
    elif not re.fullmatch(r"actions\.runner\.[A-Za-z0-9_.-]+\.service", str(runner.get("unit", ""))):
        errors.append("runner.unit must be an actions.runner.*.service unit (or null on a node)")

    paths = payload.get("paths")
    if not isinstance(paths, dict):
        errors.append("paths must be a mapping")
    else:
        for key in ("venv", "board_map"):
            value = paths.get(key)
            if not isinstance(value, str) or not Path(value).is_absolute():
                errors.append(f"paths.{key} must be an absolute path")
        for key in ("inventory", "repo", "state"):
            value = paths.get(key)
            if value is not None and (not isinstance(value, str) or not Path(value).is_absolute()):
                errors.append(f"paths.{key} must be an absolute path when set")

    health = payload.get("health")
    if not isinstance(health, dict):
        errors.append("health must be a mapping")
    else:
        interval = health.get("interval_minutes")
        minimum_boards = health.get("minimum_boards")
        warn = health.get("disk_warn_percent")
        critical = health.get("disk_critical_percent")
        if not isinstance(interval, int) or isinstance(interval, bool) or not 1 <= interval <= 1440:
            errors.append("health.interval_minutes must be an integer from 1 to 1440")
        if (
            not isinstance(minimum_boards, int)
            or isinstance(minimum_boards, bool)
            or not 1 <= minimum_boards <= 100
        ):
            errors.append("health.minimum_boards must be an integer from 1 to 100")
        if not isinstance(warn, int) or isinstance(warn, bool) or not 1 <= warn <= 99:
            errors.append("health.disk_warn_percent must be an integer from 1 to 99")
        if not isinstance(critical, int) or isinstance(critical, bool) or not 2 <= critical <= 100:
            errors.append("health.disk_critical_percent must be an integer from 2 to 100")
        if isinstance(warn, int) and isinstance(critical, int) and warn >= critical:
            errors.append("health.disk_warn_percent must be below disk_critical_percent")

    gateway = payload.get("gateway")
    if gateway is not None:
        if not isinstance(gateway, dict):
            errors.append("gateway must be a mapping")
        else:
            if not isinstance(gateway.get("enabled"), bool):
                errors.append("gateway.enabled must be a boolean")
            if not isinstance(gateway.get("ssid"), str) or not gateway.get("ssid"):
                errors.append("gateway.ssid must be a non-empty string")
            password_file = gateway.get("password_file")
            if not isinstance(password_file, str) or not Path(password_file).is_absolute():
                errors.append("gateway.password_file must be an absolute path")
            channel = gateway.get("channel")
            if channel is not None and not _whole(channel, 1, 13):
                errors.append("gateway.channel must be a whole number from 1 to 13")
            endpoint = gateway.get("endpoint")
            if not isinstance(endpoint, str) or not endpoint.startswith("http://"):
                errors.append("gateway.endpoint must be an http:// URL")
    inventory = payload.get("inventory")
    if inventory is not None:
        if not isinstance(inventory, dict):
            errors.append("inventory must be a mapping")
        elif not isinstance(inventory.get("auto_register"), bool):
            errors.append("inventory.auto_register must be a boolean")

    mqtt = payload.get("mqtt")
    if mqtt is not None:
        if not isinstance(mqtt, dict):
            errors.append("mqtt must be a mapping")
        else:
            if not isinstance(mqtt.get("enabled"), bool):
                errors.append("mqtt.enabled must be a boolean")
            url = mqtt.get("url")
            if not isinstance(url, str) or not re.fullmatch(r"mqtt://[A-Za-z0-9.-]+:[0-9]{1,5}", url):
                errors.append("mqtt.url must be mqtt://<host>:<port>")
    service = payload.get("service")
    if service is not None:
        if not isinstance(service, dict):
            errors.append("service must be a mapping")
        else:
            if not isinstance(service.get("enabled"), bool):
                errors.append("service.enabled must be a boolean")
            bind = service.get("bind")
            if not isinstance(bind, str) or not bind:
                errors.append("service.bind must be a non-empty address")
            port = service.get("port")
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                errors.append("service.port must be an integer from 1 to 65535")
            token_file = service.get("token_file")
            if not isinstance(token_file, str) or not Path(token_file).is_absolute():
                errors.append("service.token_file must be an absolute path")
            public_host = service.get("public_host")
            if public_host is not None and (
                not isinstance(public_host, str)
                or not re.fullmatch(
                    r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
                    r"[A-Za-z]{2,63}",
                    public_host,
                )
            ):
                errors.append("service.public_host must be a DNS hostname when set")
    notify = payload.get("notify")
    if notify is not None and not isinstance(notify, (dict, list)):
        errors.append("notify must be a mapping, or a list of them for several channels")
    elif notify is not None:
        channels = notify_channels(payload)
        seen: set[str] = set()
        for channel in channels:
            if not isinstance(channel, dict):
                errors.append("every notify channel must be a mapping")
                continue
            where = "notify" if len(channels) == 1 else f"notify[{channel['id']}]"
            if channel["id"] in seen:
                errors.append(f"two notify channels share the id {channel['id']}")
            seen.add(channel["id"])
            validate_notify_channel(payload, channel, errors, where)
    backup = payload.get("backup")
    if backup is not None:
        if not isinstance(backup, dict):
            errors.append("backup must be a mapping")
        else:
            if not isinstance(backup.get("enabled"), bool):
                errors.append("backup.enabled must be a boolean")
            directory = backup.get("directory")
            if not isinstance(directory, str) or not Path(directory).is_absolute():
                errors.append("backup.directory must be an absolute path")
            if not _whole(backup.get("keep"), 1, 365):
                errors.append("backup.keep must be an integer from 1 to 365")
            target = backup.get("target")
            if target is not None and (
                not isinstance(target, str)
                or not re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:/[A-Za-z0-9._/-]*", target)
            ):
                errors.append("backup.target must be user@host:/absolute/path when set")
    farm = payload.get("farm")
    if farm is not None:
        if not isinstance(farm, dict):
            errors.append("farm must be a mapping")
        else:
            mode = farm.get("mode")
            if mode not in FARM_MODES:
                errors.append(f"farm.mode must be one of {', '.join(FARM_MODES)}")
            if mode in ("attached", "node"):
                url = farm.get("portal_url")
                if not isinstance(url, str) or not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?", url):
                    errors.append("farm.portal_url must be the portal's https:// URL")
                name = farm.get("worker_name")
                if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", name):
                    errors.append("farm.worker_name must be 1-32 lowercase letters, digits, dots, underscores or hyphens")
                key_file = farm.get("node_key_file")
                if not isinstance(key_file, str) or not Path(key_file).is_absolute():
                    errors.append("farm.node_key_file must be an absolute path")
    quarantine = payload.get("quarantine")
    if quarantine is not None:
        if not isinstance(quarantine, dict):
            errors.append("quarantine must be a mapping")
        else:
            if not isinstance(quarantine.get("enabled"), bool):
                errors.append("quarantine.enabled must be a boolean")
            if not _whole(quarantine.get("after_failures"), 1, 10):
                errors.append("quarantine.after_failures must be an integer from 1 to 10")
    queue = payload.get("queue")
    if queue is not None:
        if not isinstance(queue, dict):
            errors.append("queue must be a mapping")
        elif not _whole(queue.get("concurrency"), 1, 16):
            errors.append("queue.concurrency must be an integer from 1 to 16")
    retention = payload.get("retention")
    if retention is not None:
        if not isinstance(retention, dict):
            errors.append("retention must be a mapping")
        else:
            if not isinstance(retention.get("enabled"), bool):
                errors.append("retention.enabled must be a boolean")
            for key, low, high in (
                ("run_evidence_days", 1, 3650),
                ("log_days", 1, 3650),
                ("keep_newest_runs", 0, 100000),
                ("workspace_days", 1, 365),
            ):
                if not _whole(retention.get(key), low, high):
                    errors.append(f"retention.{key} must be an integer from {low} to {high}")
    providers = payload.get("providers")
    if providers is not None:
        if not isinstance(providers, dict):
            errors.append("providers must be a mapping")
        else:
            callmebot = providers.get("callmebot")
            if callmebot is not None and not isinstance(callmebot, dict):
                errors.append("providers.callmebot must be a mapping")
            elif callmebot is not None:
                url_file = callmebot.get("url_file", DEFAULT_CALLMEBOT["url_file"])
                if not isinstance(url_file, str) or not Path(url_file).is_absolute():
                    errors.append("providers.callmebot.url_file must be an absolute path")
                if callmebot.get("send", DEFAULT_CALLMEBOT["send"]) not in PROVIDER_SEND_POLICIES:
                    errors.append(
                        f"providers.callmebot.send must be one of {', '.join(PROVIDER_SEND_POLICIES)}"
                    )
                if not _whole(callmebot.get("max_per_day", DEFAULT_CALLMEBOT["max_per_day"]), 1, 50):
                    errors.append("providers.callmebot.max_per_day must be an integer from 1 to 50")
    return errors


def notify_channels(payload: dict) -> list[dict]:
    """Every channel this host notifies through, as a list.

    A rig held one: `notify:` was a mapping. It can hold several now -- two
    Telegram chats, a webhook, its own CallMeBot link -- so `notify:` is a
    list. A file written before that is read as a list of one rather than
    rewritten, because a rig's configuration is edited by hand as often as by
    us, and a shape only this version understands is a rig that cannot be
    rolled back.

    Each channel carries an `id`, so a page and a command can name one of
    several. A file that has none is numbered as it is read.
    """
    notify = payload.get("notify")
    if notify is None:
        return []
    channels = [notify] if isinstance(notify, dict) else list(notify)
    numbered = []
    for index, channel in enumerate(channels, start=1):
        if not isinstance(channel, dict):
            numbered.append(channel)   # left for validation to refuse
            continue
        numbered.append({**channel, "id": str(channel.get("id") or index)})
    return numbered


def with_notify_channels(payload: dict, channels: list[dict]) -> dict:
    """The configuration carrying these channels, in the shape that suits how
    many there are: one stays a mapping, so a rig that has always had one
    reads the same as it always did."""
    payload = dict(payload)
    if not channels:
        payload.pop("notify", None)
        return payload
    payload["notify"] = channels[0] if len(channels) == 1 else list(channels)
    return payload


def validate_notify_channel(payload: dict, notify: dict, errors: list[str], where: str = "notify") -> None:
    """One channel, checked. `where` names it in the message: with several,
    "notify must be" says nothing about which one is wrong."""
    if not isinstance(notify.get("enabled"), bool):
        errors.append(f"{where}.enabled must be a boolean")
    channel = notify.get("channel", "webhook")
    if channel not in NOTIFY_CHANNELS:
        errors.append(f"{where}.channel must be one of {', '.join(NOTIFY_CHANNELS)}")
    elif channel == "callmebot":
        # No credential of its own: the rig's CallMeBot link is the one it
        # already validates with, and a second copy of a credential is a
        # second thing to rotate.
        if not (payload.get("providers") or {}).get("callmebot", {}).get("url_file"):
            errors.append(f"{where}.channel callmebot needs the rig's CallMeBot link: "
                          "sudo alteriom-hil-admin providers set callmebot")
    elif channel == "telegram":
        token_file = notify.get("token_file")
        if not isinstance(token_file, str) or not token_file.startswith("/"):
            errors.append(f"{where}.token_file must be an absolute path")
        chat_id = notify.get("chat_id")
        if not isinstance(chat_id, str) or not CHAT_ID_PATTERN.fullmatch(chat_id.strip()):
            errors.append(f"{where}.chat_id must be a Telegram chat id, like 12345678 or -1001234567890")
    if channel == "webhook":
        url_file = notify.get("webhook_url_file")
        if not isinstance(url_file, str) or not Path(url_file).is_absolute():
            errors.append(f"{where}.webhook_url_file must be an absolute path")
    if notify.get("format", "slack") not in NOTIFY_FORMATS:
        errors.append(f"{where}.format must be one of {', '.join(NOTIFY_FORMATS)}")
    events = notify.get("events", list(NOTIFY_EVENTS))
    if not isinstance(events, list) or not events or not set(events) <= set(NOTIFY_EVENTS):
        errors.append(f"{where}.events must be a non-empty list of {', '.join(NOTIFY_EVENTS)}")


def callmebot_settings(payload: dict) -> dict | None:
    """The host's CallMeBot settings with their defaults, or None when the
    configuration has no providers.callmebot section."""
    providers = payload.get("providers")
    callmebot = providers.get("callmebot") if isinstance(providers, dict) else None
    if not isinstance(callmebot, dict):
        return None
    return {**DEFAULT_CALLMEBOT, **callmebot}


def callmebot_budget_file(payload: dict) -> str:
    """Where the rig counts today's real messages: beside the job database,
    which the service can write and a backup does not copy."""
    state = (payload.get("paths") or {}).get("state") or DEFAULT_STATE_DIR
    return f"{state.rstrip('/')}/callmebot-budget.json"


def require_valid(payload: dict) -> dict:
    errors = validate_config(payload)
    if errors:
        raise ConfigError("; ".join(errors))
    return payload


def runtime_env(payload: dict) -> str:
    require_valid(payload)
    venv = payload["paths"]["venv"]
    values = {
        "ALTERIOM_HIL_MODE": payload["mode"],
        "ALTERIOM_HIL_VENV": venv,
        "ALTERIOM_HIL_BOARD_MAP": payload["paths"]["board_map"],
        "HIL_RUNNER_UNIT": payload["runner"].get("unit"),
        "ALTERIOM_HIL_MINIMUM_BOARDS": payload["health"]["minimum_boards"],
        "HIL_DISK_WARN_PERCENT": payload["health"]["disk_warn_percent"],
        "HIL_DISK_CRITICAL_PERCENT": payload["health"]["disk_critical_percent"],
        "PATH": f"{venv}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    for key, env_key in (
        ("inventory", "ALTERIOM_HIL_INVENTORY"),
        ("repo", "ALTERIOM_HIL_REPO"),
        ("state", "ALTERIOM_HIL_STATE"),
    ):
        if payload["paths"].get(key):
            values[env_key] = payload["paths"][key]
    gateway = payload.get("gateway")
    if gateway and gateway["enabled"]:
        values.update(
            {
                "ALTERIOM_HIL_GATEWAY_ENABLED": "1",
                "ALTERIOM_HIL_WIFI_SSID": gateway["ssid"],
                "ALTERIOM_HIL_WIFI_PASSWORD_FILE": gateway["password_file"],
                "ALTERIOM_HIL_GATEWAY_ENDPOINT": gateway["endpoint"],
                "ALTERIOM_HIL_GATEWAY_CHANNEL": str(
                    gateway.get("channel") or GATEWAY_DEFAULT_CHANNEL
                ),
            }
        )
    inventory = payload.get("inventory") or {}
    values["ALTERIOM_HIL_AUTO_REGISTER"] = (
        "1" if inventory.get("auto_register", DEFAULT_INVENTORY["auto_register"]) else "0"
    )
    mqtt = payload.get("mqtt")
    if mqtt and mqtt["enabled"]:
        # What a suite connects to for the queue side of a run; absent when
        # the host has no broker, so a suite skips its MQTT scenario rather
        # than failing to connect.
        values["ALTERIOM_HIL_MQTT_URL"] = mqtt["url"]
    service = payload.get("service")
    if service and service["enabled"]:
        values.update(
            {
                "ALTERIOM_HIL_SERVICE_ENABLED": "1",
                "ALTERIOM_HIL_API_BIND": service["bind"],
                "ALTERIOM_HIL_API_PORT": service["port"],
                "ALTERIOM_HIL_API_TOKEN_FILE": service["token_file"],
            }
        )
        if service.get("public_host"):
            values["ALTERIOM_HIL_PUBLIC_HOST"] = service["public_host"]
    farm = payload.get("farm")
    if farm and farm.get("mode") in ("attached", "node"):
        # Read by the farm service at start: a node connects to its portal.
        # Attached, the main service stays standalone and the installer starts
        # alteriom-hil-node beside it (runner/install-health-service.sh).
        if farm["mode"] == "node":
            values["ALTERIOM_HIL_FARM_MODE"] = "node"
        else:
            values["ALTERIOM_HIL_FARM_ATTACHED"] = "1"
        values["ALTERIOM_HIL_PORTAL_URL"] = farm["portal_url"]
        values["ALTERIOM_HIL_WORKER_NAME"] = farm["worker_name"]
        values["ALTERIOM_HIL_NODE_KEY_FILE"] = farm["node_key_file"]
    elif farm and farm.get("mode") == "portal":
        values["ALTERIOM_HIL_FARM_MODE"] = "portal"
    if values.get("HIL_RUNNER_UNIT") is None:
        values.pop("HIL_RUNNER_UNIT")
    queue = payload.get("queue")
    if queue:
        # How many runs the farm service lets be in progress at once
        # (alteriom_hil.allocation). Read at service start.
        values["ALTERIOM_HIL_MAX_RUNS"] = queue["concurrency"]
    backup = payload.get("backup")
    if backup and backup["enabled"]:
        # For the health check, which says when the last backup is too old.
        values["ALTERIOM_HIL_BACKUP_DIR"] = backup["directory"]
    # Every channel this host notifies through, as one variable the services
    # read (alteriom_hil.notify.Notifier.all_from_env). Paths, never secrets:
    # what a suite and the health check are given is where to read a
    # credential, as it always was.
    channels = []
    for channel in notify_channels(payload):
        if not isinstance(channel, dict) or not channel.get("enabled"):
            continue
        kind = channel.get("channel", "webhook")
        described = {"id": channel["id"], "channel": kind,
                     "events": list(channel.get("events") or NOTIFY_EVENTS)}
        if kind == "callmebot":
            described["url_file"] = (payload.get("providers") or {}).get("callmebot", {}).get("url_file", "")
        elif kind == "telegram":
            described["token_file"] = channel["token_file"]
            described["chat_id"] = str(channel["chat_id"])
        else:
            described["webhook_file"] = channel["webhook_url_file"]
            described["format"] = channel.get("format", "slack")
        channels.append(described)
    if channels:
        values["ALTERIOM_HIL_NOTIFY_CHANNELS"] = json.dumps(channels, separators=(",", ":"))
        # The first one under the names a single channel has always had, so
        # anything reading those -- an older service during an upgrade, a
        # script somebody wrote -- keeps working while this rolls out.
        first = channels[0]
        values["ALTERIOM_HIL_NOTIFY_CHANNEL"] = first["channel"]
        values["ALTERIOM_HIL_NOTIFY_EVENTS"] = ",".join(first["events"])
        if first["channel"] == "callmebot":
            values["ALTERIOM_HIL_NOTIFY_URL_FILE"] = first["url_file"]
        elif first["channel"] == "telegram":
            values["ALTERIOM_HIL_NOTIFY_TOKEN_FILE"] = first["token_file"]
            values["ALTERIOM_HIL_NOTIFY_CHAT_ID"] = first["chat_id"]
        else:
            values["ALTERIOM_HIL_NOTIFY_WEBHOOK_FILE"] = first["webhook_file"]
            values["ALTERIOM_HIL_NOTIFY_FORMAT"] = first["format"]
    callmebot = callmebot_settings(payload)
    if callmebot is not None:
        # The suite, the health check and the farm's redactor read these. The
        # link itself stays in its file: only the path is exported, as for
        # every other secret.
        values.update(
            {
                "ALTERIOM_HIL_CALLMEBOT_URL_FILE": callmebot["url_file"],
                "ALTERIOM_HIL_CALLMEBOT_SEND": callmebot["send"],
                "ALTERIOM_HIL_CALLMEBOT_MAX_PER_DAY": callmebot["max_per_day"],
                "ALTERIOM_HIL_CALLMEBOT_BUDGET_FILE": callmebot_budget_file(payload),
            }
        )
    # What this rig offers a suite, by name, so a consumer reads one variable
    # rather than testing for each of the others (alteriom_hil.connectors). A
    # consumer implements against it when it wants to; the farm neither knows
    # nor cares which of them a given project uses.
    from alteriom_hil import connectors

    said = connectors.names({key: str(value) for key, value in values.items()})
    if said:
        values["ALTERIOM_HIL_CONNECTORS"] = said
    return "".join(f"{key}={value}\n" for key, value in values.items())


def runtime_env_keys() -> frozenset[str]:
    """Every name runtime_env() can write, whatever a host enables.

    What a dispatch may not override in a run's environment (farm_shared):
    these are the rig's to say. Derived by rendering a configuration with
    every section on -- in each farm mode, since the modes write different
    names -- so a setting added to runtime_env() is covered without a second
    list to keep in step.
    """
    import copy

    base = {
        "schema": 2,
        "mode": "hardware",
        "runner": {"unit": "actions.runner.keys.service"},
        "paths": {"venv": "/v", "board_map": "/b", "inventory": "/i", "repo": "/r", "state": "/s"},
        "health": {"interval_minutes": 5, "minimum_boards": 2, "disk_warn_percent": 85, "disk_critical_percent": 95},
        "gateway": {**DEFAULT_GATEWAY, "enabled": True},
        "mqtt": {**DEFAULT_MQTT, "enabled": True},
        "service": {"enabled": True, "bind": "127.0.0.1", "port": 8090, "token_file": "/t",
                    "public_host": "rig.example.com"},
        "notify": {"enabled": True, "webhook_url_file": "/n"},
        "queue": {"concurrency": 1},
        "backup": {**DEFAULT_BACKUP, "enabled": True},
        "providers": {"callmebot": dict(DEFAULT_CALLMEBOT)},
    }
    portal = {"portal_url": "https://portal.example.com", "worker_name": "keys", "node_key_file": "/k"}
    keys: set[str] = set()
    for mode in ("attached", "node", "portal"):
        payload = copy.deepcopy(base)
        payload["farm"] = {"mode": mode, **(portal if mode != "portal" else {})}
        keys.update(line.split("=", 1)[0] for line in runtime_env(payload).splitlines())
    return frozenset(keys)


def write_atomic(path: Path, text: str, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = path.stat() if path.exists() else path.parent.stat()
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", delete=False, encoding="utf-8"
    ) as stream:
        stream.write(text)
        temporary = Path(stream.name)
    os.chmod(temporary, mode)
    if os.geteuid() == 0:
        os.chown(temporary, owner.st_uid if path.exists() else 0, owner.st_gid)
    temporary.replace(path)


def dump_config(payload: dict) -> str:
    return yaml.safe_dump(payload, sort_keys=False)


# The settings a portal may change on a node from its page (a `configure`
# command, runner/node-control.sh): how many runs at once, what the node keeps,
# and when its host health check complains. Nothing that names a path, a
# network or a secret, and nothing that decides where the node connects.
REMOTE_SETTINGS = {
    "queue.concurrency": int,
    "retention.enabled": bool,
    "retention.run_evidence_days": int,
    "retention.log_days": int,
    "retention.keep_newest_runs": int,
    "retention.workspace_days": int,
    "health.interval_minutes": int,
    "health.minimum_boards": int,
    "health.disk_warn_percent": int,
    "health.disk_critical_percent": int,
    # When the rig's CallMeBot row may spend a real message, and how many a
    # day (docs/providers.md). Policy only: the link itself never travels as a
    # setting -- it is sealed to the rig's key (a `provider_set` command).
    # A tuple is the values an enumerated setting may take.
    "providers.callmebot.send": PROVIDER_SEND_POLICIES,
    "providers.callmebot.max_per_day": int,
}
# Bounds a remote integer setting is held to on the portal already, where the
# node's whole-file validation would refuse it anyway.
REMOTE_RANGES = {
    "providers.callmebot.max_per_day": (1, 50),
}
# What a section a remote setting needs starts as, when the file has none.
REMOTE_SECTION_DEFAULTS = {
    "retention": DEFAULT_RETENTION,
    "providers.callmebot": DEFAULT_CALLMEBOT,
}


def checked_remote_settings(settings: object) -> dict:
    """The settings a portal asked for, each a known remote setting of its type."""
    if not isinstance(settings, dict) or not settings:
        raise ConfigError("settings must be a non-empty mapping of setting to value")
    checked = {}
    for key, value in settings.items():
        kind = REMOTE_SETTINGS.get(key)
        if kind is None:
            raise ConfigError(f"{key} cannot be changed remotely; one of: {', '.join(REMOTE_SETTINGS)}")
        if isinstance(kind, frozenset):
            # A list drawn from a known set, in the set's own order so the
            # file reads the same however the page sent it.
            # Every entry checked as text before anything is hashed: a caller
            # can send `[{}]` or a nested list, and an unhashable entry would
            # otherwise be a TypeError -- a 500 where the endpoint means to
            # answer 400 with what was wrong.
            if (not isinstance(value, list) or not value
                    or not all(isinstance(item, str) for item in value)
                    or not set(value) <= kind):
                raise ConfigError(f"{key} must be a non-empty list of {', '.join(sorted(kind))}")
            value = list(dict.fromkeys(value))
        elif isinstance(kind, tuple):
            if not isinstance(value, str) or value not in kind:
                raise ConfigError(f"{key} must be one of {', '.join(kind)}")
        elif kind is bool and not isinstance(value, bool):
            raise ConfigError(f"{key} must be true or false")
        elif kind is int and (not isinstance(value, int) or isinstance(value, bool)):
            raise ConfigError(f"{key} must be an integer")
        if key in REMOTE_RANGES and not REMOTE_RANGES[key][0] <= value <= REMOTE_RANGES[key][1]:
            low, high = REMOTE_RANGES[key]
            raise ConfigError(f"{key} must be an integer from {low} to {high}")
        checked[key] = value
    return checked


def set_values(payload: dict, settings: dict) -> dict:
    """Remote settings into a host configuration, sections made as needed,
    and the whole validated before anything is kept."""
    for key, value in checked_remote_settings(settings).items():
        *sections, leaf = key.split(".")
        target = payload
        for depth, section in enumerate(sections):
            if not isinstance(target.get(section), dict):
                # A section the file never had starts from what the farm
                # assumes without it, so one setting does not leave the rest
                # invalid.
                dotted = ".".join(sections[:depth + 1])
                target[section] = dict(REMOTE_SECTION_DEFAULTS.get(dotted, {}))
            target = target[section]
        target[leaf] = value
    require_valid(payload)
    return payload


DEFAULT_NODE_KEY_FILE = "/etc/alteriom-hil/node-key"


def join_portal(payload: dict, portal_url: str, worker_name: str, node_key_file: str = DEFAULT_NODE_KEY_FILE) -> dict:
    """A host configuration made a node of a portal (runner/join-rig.sh): the
    farm section says so, and the host has no runner of its own. Validated
    whole before anything is kept."""
    farm = payload.get("farm") if isinstance(payload.get("farm"), dict) else {}
    payload["farm"] = {**farm, "mode": "node", "portal_url": portal_url.rstrip("/"),
                       "worker_name": worker_name, "node_key_file": node_key_file}
    if isinstance(payload.get("runner"), dict):
        payload["runner"]["unit"] = None
    require_valid(payload)
    return payload


def set_value(payload: dict, dotted_key: str, raw_value: str) -> dict:
    parts = dotted_key.split(".")
    allowed = ALLOWED_KEYS
    target = payload
    for part in parts[:-1]:
        if part not in allowed or not isinstance(allowed[part], dict):
            raise ConfigError(f"unknown setting: {dotted_key}")
        allowed = allowed[part]
        if not isinstance(target.get(part), dict):
            raise ConfigError(f"{part} is not a mapping")
        target = target[part]
    leaf = parts[-1]
    if leaf not in allowed or isinstance(allowed[leaf], dict):
        raise ConfigError(f"unknown setting: {dotted_key}")
    current = target.get(leaf)
    if isinstance(current, bool):
        normalized = raw_value.strip().lower()
        if normalized not in ("true", "false", "1", "0", "yes", "no"):
            raise ConfigError(f"{dotted_key} requires a boolean")
        value = normalized in ("true", "1", "yes")
    elif isinstance(current, int):
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ConfigError(f"{dotted_key} requires an integer") from exc
    else:
        value = raw_value
    target[leaf] = value
    require_valid(payload)
    return payload
