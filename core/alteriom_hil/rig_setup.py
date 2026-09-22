"""What a rig is set up to do, read from what it already reports.

A rig is a machine somewhere else, and most of what it can do is decided on
it: an access point on its own radio, a broker beside it, a provider's link in
a root-owned file. The dashboard could show a run, a board and a release, and
nothing about any of that -- so "why did the uplink checks skip" or "is
CallMeBot set up here" were answered by logging in.

This is the answer as a list. Every capability a rig can have is declared
once, with what it enables, how to tell whether it is on, and where it is
turned on -- the portal for the few the portal owns, the rig's own CLI for the
rest. Nothing here reads hardware or a secret: it is the configuration and the
health snapshot the rig already sends, turned into one page's worth of truth.

    capabilities(config, health, inventory, worker) -> list of rows

Each row is ``{key, title, label, state, summary, details, enables, where,
command}``:

* ``label`` is what the row's chip says where there is room for three words:
  the rig's page heading, the rigs list, later the public one. It carries the
  one detail a reader wants at a glance -- how many boards, which channel the
  access point is on, which channels are told -- so that the chips are the
  rig's capabilities and not ten words for "yes".
* ``state`` is ``on`` (set up and working), ``off`` (not set up, and that is a
  choice), ``broken`` (set up and not working) or ``unknown`` (the rig has not
  said). A row is never ``on`` because it was configured -- something has to
  have answered.
* ``where`` is ``portal``, ``rig`` or ``deploy``: who can change it.
* ``command`` is what to run when it is the rig's own, filled in ready to
  paste.
"""

from __future__ import annotations

ON, OFF, BROKEN, UNKNOWN = "on", "off", "broken", "unknown"


def _health_checks(health: dict | None) -> dict:
    checks = (health or {}).get("checks") or []
    return {check.get("name"): check for check in checks if isinstance(check, dict)}


def _verdict(check: dict | None) -> str | None:
    """A health row's status as a capability state, or None when absent."""
    status = (check or {}).get("status")
    if status == "ok":
        return ON
    if status in ("unhealthy", "degraded"):
        return BROKEN
    return None


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _boards(config: dict, health: dict | None, inventory: dict | None) -> dict:
    auto = (config.get("inventory") or {}).get("auto_register")
    boards = (inventory or {}).get("boards") or []
    families = sorted({board.get("target") for board in boards if board.get("target")})
    unregistered = len((inventory or {}).get("unregistered") or [])
    missing = len((inventory or {}).get("missing") or [])
    details = []
    if families:
        details.append(f"families: {', '.join(families)}")
    if auto:
        details.append("a board plugged in registers itself")
    if unregistered:
        details.append(f"{unregistered} connected device(s) not registered")
    if missing:
        details.append(f"{missing} registered board(s) not found")
    if boards:
        state, summary = ON, f"{len(boards)} board(s) registered and connected"
    elif unregistered:
        state = BROKEN if auto else OFF
        summary = (
            f"{unregistered} device(s) on the ports that should have registered themselves"
            if auto else f"{unregistered} device(s) on the ports, none registered"
        )
    else:
        state, summary = OFF, "no board registered on this rig"
    return {
        "key": "boards",
        "title": "Boards",
        "label": _count(len(boards), "board") if boards else "no boards",
        "state": state,
        "summary": summary,
        "details": details,
        "enables": "every suite: a rig with no boards is given no runs",
        "where": "rig",
        "command": "sudo alteriom-hil-admin boards add --id <id> --port <port> --target <family>",
    }


def _power(config: dict, health: dict | None, inventory: dict | None) -> dict:
    switched = [
        board for board in ((inventory or {}).get("boards") or [])
        if board.get("power_hub") and board.get("power_port") is not None
    ]
    check = _health_checks(health).get("usb_power")
    state = _verdict(check)
    if state == ON and not switched:
        state, summary = OFF, "uhubctl works; no board has power coordinates"
    elif state == ON:
        summary = f"{len(switched)} board(s) can be power-cut independently"
    else:
        summary = (check or {}).get("message") or "not reported"
        state = state or UNKNOWN
    return {
        "key": "power.switching",
        "title": "Per-board power",
        "label": "Power switching",
        "state": state,
        "summary": summary,
        "details": [],
        "enables": "power-cut recovery, and the tests that pull a board's power",
        "where": "rig",
        "command": "sudo alteriom-hil-admin boards add … --power-hub <hub> --power-port <port>",
    }


def _access_point(config: dict, health: dict | None, inventory: dict | None) -> dict:
    gateway = config.get("gateway") or {}
    checks = _health_checks(health)
    details = []
    if gateway.get("ssid"):
        details.append(f"SSID {gateway['ssid']}")
    if gateway.get("channel"):
        details.append(f"channel {gateway['channel']} (the mesh's channel)")
    if gateway.get("endpoint"):
        details.append(f"probe {gateway['endpoint']}")
    if gateway.get("enabled") is None:
        state, summary = UNKNOWN, "the rig has not said"
    elif not gateway.get("enabled"):
        state, summary = OFF, "no access point: the radio and uplink checks skip"
    else:
        faults = [
            checks.get(name) for name in
            ("gateway_service", "gateway_credentials", "gateway_endpoint", "gateway_channel")
            if _verdict(checks.get(name)) == BROKEN
        ]
        if faults:
            state = BROKEN
            summary = faults[0].get("message") or "the access point is not answering"
        else:
            state, summary = ON, "the rig's own network is up"
    return {
        "key": "network.ap",
        "title": "Rig access point",
        "label": f"Wi-Fi AP · ch {gateway['channel']}" if gateway.get("channel") else "Wi-Fi AP",
        "state": state,
        "summary": summary,
        "details": details,
        "enables": "radio scan and join, the gateway and uplink rows, every Internet test",
        "where": "rig",
        "command": "sudo ./rig/setup-gateway-network.sh && "
                   "sudo alteriom-hil-admin config set gateway.enabled true",
    }


def _broker(config: dict, health: dict | None, inventory: dict | None) -> dict:
    mqtt = config.get("mqtt") or {}
    if mqtt.get("enabled") is None:
        state, summary = UNKNOWN, "the rig has not said"
    elif mqtt.get("enabled"):
        state, summary = ON, f"broker at {mqtt.get('url') or 'its configured address'}"
    else:
        state, summary = OFF, "no broker: the queue checks skip"
    return {
        "key": "network.broker",
        "title": "Rig broker",
        "label": "MQTT broker",
        "state": state,
        "summary": summary,
        "details": [],
        "enables": "the queue row: a board publishing to the rig's own broker",
        "where": "rig",
        "command": "sudo ./rig/setup-mqtt-broker.sh && "
                   "sudo alteriom-hil-admin config set mqtt.enabled true",
    }


def _callmebot(config: dict, health: dict | None, inventory: dict | None) -> dict:
    provider = config.get("callmebot") or {}
    check = _health_checks(health).get("provider_callmebot")
    details = []
    send = provider.get("send")
    if send:
        details.append({"never": "never sends a real message",
                        "release": "sends only during a release run",
                        "always": "sends on every run"}.get(send, f"send: {send}"))
    if provider.get("max_per_day") is not None:
        used = provider.get("used_today")
        details.append(f"{used if used is not None else '?'} of {provider['max_per_day']} messages used today")
    if not provider.get("url_file"):
        state, summary = OFF, "no link stored: the CallMeBot rows skip"
    elif _verdict(check) == BROKEN:
        state, summary = BROKEN, (check or {}).get("message") or "the stored link is not usable"
    elif send == "never":
        state, summary = ON, "a link is stored; no real message is ever sent"
    else:
        state, summary = ON, "a link is stored and may be spent"
    return {
        "key": "provider.callmebot",
        "title": "CallMeBot (WhatsApp)",
        "label": "CallMeBot",
        "state": state,
        "summary": summary,
        "details": details,
        "enables": "the real-service rows: a board's message reaching WhatsApp through the mesh",
        # Sealed to this rig from the portal, which is the one secret path a
        # portal has: the link is encrypted to the rig's key in the browser.
        "where": "portal",
        "command": "sudo alteriom-hil-admin providers set",
    }


CHANNEL_NAMES = {"telegram": "Telegram", "webhook": "webhook", "callmebot": "CallMeBot"}

NOT_ARRIVED = "the last message did not arrive"


def _delivery(value) -> dict | None:
    """The last delivery a channel reports, in either shape it arrives in.

    The portal's `configuration()` summarises it as one line -- `board_red at
    <when>, delivered` or `..., failed: <why>` -- and the node forwards that
    unchanged; a test, or the outcome itself, is `{at, ok, error}`. Read as a
    mapping only, this raised on every rig that had ever sent a message, and
    once the rows were computed for the rigs list that took the list down.
    """
    if isinstance(value, dict):
        if not value.get("at"):
            return None
        ok = bool(value.get("ok"))
        error = None if ok else str(value.get("error") or NOT_ARRIVED)
        return {"ok": ok, "error": error, "text": f"{value['at']}: " + ("delivered" if ok else error)}
    if isinstance(value, str) and value:
        _, failed, why = value.partition(", failed: ")
        ok = not failed
        why = why.strip()
        return {"ok": ok, "error": None if ok else (why if why and why != "None" else NOT_ARRIVED), "text": value}
    return None


def _notifications(config: dict, health: dict | None, inventory: dict | None) -> dict:
    # Every channel the rig is told through (`notify_channels`), or the one
    # channel an older rig reports as `notify`: a rig with Telegram and a
    # webhook was described by its first channel alone, and a page that says
    # "telegram" over a webhook whose last message failed is wrong twice.
    channels = config.get("notify_channels")
    if channels is None:
        channels = [config["notify"]] if config.get("notify") else []
    channels = [channel for channel in channels if isinstance(channel, dict)]
    known = [channel for channel in channels if channel.get("enabled") is not None]
    enabled = [channel for channel in channels if channel.get("enabled")]
    details = []
    for channel in enabled:
        kind = channel.get("channel") or "webhook"
        if kind == "telegram" and channel.get("chat_id"):
            details.append(f"Telegram chat {channel['chat_id']}")
        elif kind == "webhook" and channel.get("format"):
            details.append(f"{channel['format']} webhook")
        elif kind == "callmebot":
            details.append("CallMeBot (WhatsApp)")
        events = channel.get("events")
        if events:
            details.append("sends: " + (", ".join(events) if isinstance(events, list) else str(events)))
        last = _delivery(channel.get("last_delivery"))
        if last:
            details.append(f"last message {last['text']}")
    failed = [last for channel in enabled
              if (last := _delivery(channel.get("last_delivery"))) and not last["ok"]]
    # "Telegram + 2 webhooks": the chip names the channels, which is the
    # answer to "where does this rig say it broke".
    kinds: dict[str, int] = {}
    for channel in enabled:
        kind = channel.get("channel") or "webhook"
        kinds[kind] = kinds.get(kind, 0) + 1
    names = [CHANNEL_NAMES.get(kind, kind) if number == 1 else _count(number, CHANNEL_NAMES.get(kind, kind))
             for kind, number in kinds.items()]
    if not known:
        state, summary = UNKNOWN, "the rig has not said"
    elif not enabled:
        state, summary = OFF, "nobody is told when this rig has a problem"
    elif failed:
        state, summary = BROKEN, failed[0]["error"]
    else:
        state, summary = ON, f"{', '.join(names)}: the farm says here when something breaks"
    return {
        "key": "notifications",
        "title": "Notifications",
        "label": " + ".join(names) if names else "Notifications",
        "state": state,
        "summary": summary,
        "details": details,
        "enables": "hearing that the queue paused, a board went red or the host is unhealthy",
        "where": "rig",
        "command": "sudo alteriom-hil-admin notify set --channel telegram --chat-id <chat>",
    }


def _instruments(config: dict, health: dict | None, inventory: dict | None) -> dict:
    instruments = (inventory or {}).get("instruments") or []
    wires = sum(len(item.get("wiring") or []) for item in instruments if isinstance(item, dict))
    if instruments:
        state = ON
        summary = f"{len(instruments)} instrument(s), {wires} wire(s) recorded"
    else:
        state, summary = OFF, "no instrument wired: the wiring row skips"
    return {
        "key": "instruments",
        "title": "I/O instruments",
        "label": _count(len(instruments), "instrument") if instruments else "Instruments",
        "state": state,
        "summary": summary,
        "details": [],
        "enables": "the wiring row: every jumper from an instrument to a board, both ways",
        "where": "rig",
        "command": "sudo alteriom-hil-admin instruments add --id io-01 --port <port>",
    }


def _updates(config: dict, health: dict | None, inventory: dict | None, worker: dict | None) -> dict:
    farm = config.get("farm") or {}
    mode = farm.get("mode") or (worker or {}).get("kind")
    update = (worker or {}).get("update") or {}
    details = []
    if (worker or {}).get("version"):
        details.append(f"running {worker['version']}")
    if update.get("state"):
        details.append(f"update: {update['state']}" + (f" -- {update['detail']}" if update.get("detail") else ""))
    if mode == "node":
        if update.get("state") == "failed":
            state, summary = BROKEN, update.get("detail") or "the last release did not install"
        else:
            state, summary = ON, "takes its releases from the portal"
    elif mode in ("standalone", "portal", "attached"):
        state, summary = OFF, f"deployed with its host ({mode}), not by a portal"
    else:
        state, summary = UNKNOWN, "the rig has not said how it is deployed"
    return {
        "key": "updates",
        "title": "Releases",
        "label": "Releases",
        "state": state,
        "summary": summary,
        "details": details,
        "enables": "a rig that follows the farm's releases without anyone logging in",
        "where": "deploy",
        "command": None,
    }


def _backup(config: dict, health: dict | None, inventory: dict | None) -> dict:
    backup = config.get("backup") or {}
    check = _health_checks(health).get("backup")
    if backup.get("enabled") is None:
        state, summary = UNKNOWN, "the rig has not said"
    elif not backup.get("enabled"):
        state, summary = OFF, "nothing is backed up"
    elif _verdict(check) == BROKEN:
        state, summary = BROKEN, (check or {}).get("message") or "no backup has been made"
    else:
        state, summary = ON, f"keeping {backup.get('keep', '?')} in {backup.get('directory') or 'its directory'}"
    return {
        "key": "backup",
        "title": "Backups",
        "label": "Backups",
        "state": state,
        "summary": summary,
        "details": [],
        "enables": "a rig that can be rebuilt: its registry, keys and history",
        "where": "portal",
        "command": "sudo alteriom-hil-admin backup create",
    }


def _quarantine(config: dict, health: dict | None, inventory: dict | None) -> dict:
    quarantine = config.get("quarantine") or {}
    if quarantine.get("enabled") is None:
        state, summary = UNKNOWN, "the rig has not said"
    elif quarantine.get("enabled"):
        state = ON
        summary = f"a board red {quarantine.get('after_failures', '?')} health checks in a row is held out"
    else:
        state, summary = OFF, "a failing board keeps taking runs"
    return {
        "key": "quarantine",
        "title": "Automatic quarantine",
        "label": "Quarantine",
        "state": state,
        "summary": summary,
        "details": [],
        "enables": "keeping a board that keeps failing its own checks out of other people's runs",
        "where": "portal",
        "command": "sudo alteriom-hil-admin config set quarantine.enabled true",
    }


def capabilities(
    config: dict | None,
    health: dict | None = None,
    inventory: dict | None = None,
    worker: dict | None = None,
) -> list[dict]:
    """Every capability a rig can have, in the order an operator meets them.

    A rig that has reported nothing is not described as switched off: with no
    configuration every row is `unknown`, because "off" is a statement about a
    rig and this would be a statement about the portal.
    """
    config = config or {}
    rows = [
        _boards(config, health, inventory),
        _power(config, health, inventory),
        _access_point(config, health, inventory),
        _broker(config, health, inventory),
        _callmebot(config, health, inventory),
        _instruments(config, health, inventory),
        _notifications(config, health, inventory),
        _quarantine(config, health, inventory),
        _backup(config, health, inventory),
        _updates(config, health, inventory, worker),
    ]
    if not config:
        for row in rows:
            row["state"] = UNKNOWN
            row["summary"] = "the rig has not reported its configuration"
            row["details"] = []
    return rows


def headline(rows: list[dict]) -> list[dict]:
    """The rows worth a chip where there is no room for a table: what is on,
    and what is set up and not working. A rig that has said nothing gets no
    chips, because "not reported" ten times over is not a heading. Only the
    fields a chip needs, so a list of rigs is not ten commands per rig."""
    return [{"key": row["key"], "title": row["title"], "label": row["label"],
             "state": row["state"], "summary": row["summary"]}
            for row in rows if row["state"] in (ON, BROKEN)]


# What a rig can do is public when its owner says the rig is; how it is
# kept -- backups, quarantine, who is told, how it is deployed -- is its
# owner's business, and stays off the world page.
PUBLIC_KEYS = ("boards", "power.switching", "network.ap", "network.broker", "provider.callmebot", "instruments")


def public_headline(rows: list[dict]) -> list[dict]:
    """The chips for the world page: the public capabilities that are on or
    broken, as key, label and state only. No summary, because a summary can
    carry a broker's address or an SSID and a stranger is owed neither."""
    return [{"key": row["key"], "label": row["label"], "state": row["state"]}
            for row in headline(rows) if row["key"] in PUBLIC_KEYS]


def summarise(rows: list[dict]) -> dict:
    """The counts a page puts at the top: what is on, off, broken, unsaid."""
    counts = {ON: 0, OFF: 0, BROKEN: 0, UNKNOWN: 0}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return {
        "on": counts[ON], "off": counts[OFF],
        "broken": counts[BROKEN], "unknown": counts[UNKNOWN],
        # What a reader should look at first: something set up and not working.
        "attention": [row["key"] for row in rows if row["state"] == BROKEN],
    }
