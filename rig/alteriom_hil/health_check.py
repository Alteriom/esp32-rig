#!/usr/bin/env python3
"""Machine-readable health check for the dedicated HIL runner service."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SEVERITY = {"ok": 0, "degraded": 1, "unhealthy": 2}
NGINX_SITE_PATH = Path("/etc/nginx/sites-enabled/alteriom-hil")


def command(*args: str, search_path: str | None = None) -> tuple[int, str]:
    executable = shutil.which(args[0], path=search_path) if search_path else args[0]
    command_args = (executable or args[0], *args[1:])
    try:
        result = subprocess.run(command_args, capture_output=True, text=True, check=False)
    except OSError as exc:
        return 127, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


def read_number(path: str, scale: float = 1.0) -> float | None:
    """One number out of a /proc or /sys file, or None where there is none."""
    try:
        return float(Path(path).read_text(encoding="utf-8").split()[0]) * scale
    except (OSError, ValueError, IndexError):
        return None


def host_metrics(
    loadavg: str = "/proc/loadavg",
    meminfo: str = "/proc/meminfo",
    uptime: str = "/proc/uptime",
    thermal: str = "/sys/class/thermal/thermal_zone0/temp",
    cpus: int | None = None,
) -> dict:
    """What the host is doing, as numbers: load per CPU, memory in use, the
    SoC's temperature, how long it has been up.

    A rig is a computer in a cupboard. The farm knew whether its disk was full
    and whether the Pi had throttled, and nothing else about it -- so "the runs
    got slow" and "it started swapping three days ago" were not questions the
    dashboard could answer. These are read from the kernel; nothing is
    installed for them and nothing is sampled between checks.
    """
    data: dict = {}
    cores = cpus or os.cpu_count() or 1
    try:
        one, five, fifteen = (float(value) for value in
                              Path(loadavg).read_text(encoding="utf-8").split()[:3])
        data.update(load_1=one, load_5=five, load_15=fifteen,
                    load_per_cpu=round(one / cores, 2), cpus=cores)
    except (OSError, ValueError):
        pass
    try:
        fields = {}
        for line in Path(meminfo).read_text(encoding="utf-8").splitlines():
            name, _, rest = line.partition(":")
            if name in ("MemTotal", "MemAvailable"):
                fields[name] = float(rest.split()[0]) * 1024
        if fields.get("MemTotal"):
            used = fields["MemTotal"] - fields.get("MemAvailable", 0)
            data.update(memory_total=int(fields["MemTotal"]), memory_used=int(used),
                        memory_used_percent=round(100 * used / fields["MemTotal"]))
    except (OSError, ValueError, IndexError):
        pass
    celsius = read_number(thermal, 0.001)
    if celsius is not None:
        data["temperature_c"] = round(celsius, 1)
    seconds = read_number(uptime)
    if seconds is not None:
        data["uptime_seconds"] = int(seconds)

    if not data:
        return {"status": "degraded", "message": "this host reports no metrics"}

    said = []
    if "load_per_cpu" in data:
        said.append(f"load {data['load_1']:.2f} on {data['cpus']} CPU(s)")
    if "memory_used_percent" in data:
        said.append(f"memory {data['memory_used_percent']}% used of "
                    f"{data['memory_total'] / 1_000_000_000:.1f} GB")
    if "temperature_c" in data:
        said.append(f"{data['temperature_c']:.1f} °C")
    if "uptime_seconds" in data:
        days, rest = divmod(data["uptime_seconds"], 86400)
        said.append(f"up {days}d {rest // 3600}h" if days else f"up {rest // 3600}h")

    # Thresholds a rig is judged by, not a desktop: a Pi throttles from 80 °C
    # and stops being a reliable timing reference well before that; memory
    # this full is the state where a flash or a serial capture starts failing
    # in ways that look like the boards' fault; sustained load above four per
    # CPU is a host that cannot keep the serial reads up.
    status = "ok"
    if data.get("temperature_c", 0) >= 80 or data.get("memory_used_percent", 0) >= 95:
        status = "unhealthy"
    elif (data.get("temperature_c", 0) >= 70 or data.get("memory_used_percent", 0) >= 85
          or data.get("load_per_cpu", 0) >= 4):
        status = "degraded"
    return {"status": status, "message": ", ".join(said), **data}


def rig_lock(path: str | None = None) -> dict:
    """Can this host take the lock every run holds?

    It lives on a tmpfs, so it is gone after each boot and made again by a
    tmpfiles rule. A rig where that rule is missing, or where the lock ended up
    somewhere the service's `ProtectSystem=strict` cannot write, fails every
    run with an errno nobody reads until they open the journal -- which is how
    rig02 spent a morning (2026-09-16).
    """
    lock = Path(path or "/run/lock/alteriom-hil.lock")
    try:
        with open(lock, "a"):
            pass
    except OSError as exc:
        return {
            "status": "unhealthy",
            "message": f"{lock}: {exc.strerror or exc}; every run needs this lock",
            "path": str(lock),
        }
    return {"status": "ok", "message": f"{lock} is writable", "path": str(lock)}


def ap_channel(output: str) -> int | None:
    """The channel of the first interface `iw dev` reports as an AP.

    Read rather than asked of NetworkManager: what matters is the channel the
    radio is actually on, which is what a board promoted to bridge will follow
    the AP to.
    """
    channel = None
    is_ap = False
    for line in output.splitlines() + ["Interface end"]:
        stripped = line.strip()
        if stripped.startswith("Interface "):
            if is_ap and channel is not None:
                return channel
            channel, is_ap = None, False
            continue
        if stripped.startswith("type "):
            is_ap = stripped.split()[1] == "AP"
        elif stripped.startswith("channel "):
            try:
                channel = int(stripped.split()[1])
            except (IndexError, ValueError):
                channel = None
    return None


# What a deploy may not conclude a failed install from. A rig's power supply
# is the host's standing condition, not evidence about the release that was
# just put on it: the install either worked or it did not, and saying "could
# not install the current release" because a rail sagged is both untrue and
# the kind of red that hides a real one (esp32-hil, 2026-09-21 and -22). The
# fault is still reported, still notified on, and still shown on the rig's
# page; it just does not fail the install.
HOST_SUPPLY_CHECKS = frozenset({"pi_power"})


def add(checks: list[dict], name: str, status: str, message: str, **data):
    checks.append({"name": name, "status": status, "message": message, **data})


# How many times the throttling flag is read, and how long apart. The flag
# is a comparator against a threshold, so a supply sitting near it reads set
# in one sample and clear in the next: esp32-hil measured 4.63-4.79 V against
# a 4.8 V threshold on 2026-09-22 and flipped between readings seconds apart.
# Several samples catch the flag at all; they do not say whether the rail is
# down or dipping, because a dip outlasts them (below).
THROTTLE_SAMPLES = 3
THROTTLE_SAMPLE_GAP = 0.4

# How long a power fault must have been seen for before it is the rig's
# supply rather than a dip in it. Measured, not guessed: esp32-hil's kernel
# log on 2026-09-22 had 326 under-voltage dips in 12 hours, none shorter than
# 2 s, a median of 4 s, a tenth over a minute and one of 34 minutes -- so
# three samples 0.4 s apart all land inside one dip and call the same supply
# "down" at one check and "dipping" at the next. What separates the two is a
# fault still there at the next check: the health unit runs every five
# minutes, so this is one interval, short enough that two checks in a row
# decide it, long enough that a deploy's check seconds after the timer's is
# not the second one. The record is carried in status.json (`faulting_since`),
# which the timer and the deploy's check both write.
THROTTLE_PERSISTS_AFTER = 4 * 60.0


def read_throttling(search_path: str | None = None) -> list:
    """The throttling flag, several times a moment apart."""
    readings = []
    for index in range(THROTTLE_SAMPLES):
        if index:
            time.sleep(THROTTLE_SAMPLE_GAP)
        readings.append(command("vcgencmd", "get_throttled", search_path=search_path))
        if readings[-1][0] != 0:
            # No vcgencmd, or it refused: sampling again says nothing new, and
            # every other host running this check would pay the gap for it.
            break
    return readings


def power_fault_since(previous: dict | None) -> str | None:
    """When the previous snapshot's power fault began, or None if it had none.

    The check that wrote the snapshot was this one: a fault it saw is the same
    rail, and `faulting_since` is the start of the spell it was in. A snapshot
    from before that field carried its own timestamp as the start. A check
    that could not read the flag at all (`current_flags` absent) saw nothing
    about the rail and starts no spell.
    """
    for check in (previous or {}).get("checks") or []:
        if check.get("name") != "pi_power":
            continue
        if check.get("status") in ("degraded", "unhealthy") and (
            check.get("current_flags") or check.get("readings_faulted")
        ):
            return check.get("faulting_since") or previous.get("timestamp")
        return None
    return None


def classify_throttling(
    returncode: int,
    output: str,
    *also: tuple,
    since: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Separate current Pi power/thermal faults from latched history bits.

    `also` is further readings of the same flag, taken a moment apart
    (`read_throttling`); a fault in any of them is a fault now. `since` is
    when the previous check first saw the fault (`power_fault_since`), if it
    saw one. A fault seen now and first seen THROTTLE_PERSISTS_AFTER or
    longer ago is a supply that is not carrying the rig: unhealthy, and
    notified on. A fault seen now and not before is a supply that dips --
    real, worth fixing, and said so in those words -- but not a rig that
    cannot run, and a rig that is demonstrably running should not be called
    broken at one check and fine at the next for the same standing condition.
    A clear reading ends the spell.
    """
    now = now or datetime.now(timezone.utc)
    match = re.fullmatch(r"throttled=0x([0-9a-fA-F]+)", output)
    if returncode != 0 or not match:
        # A rig that cannot read its own throttling still runs tests. On a
        # host whose user is not in the `video` group, /dev/vcio answers
        # "Can't open device file" and the whole rig was reported UNHEALTHY --
        # which reads as failing hardware, and failed the release install a
        # new rig was in the middle of (rig02, 2026-09-16). Say what to do,
        # and leave the verdict to the checks that are about the hardware.
        denied = "vcio" in output or "permission" in output.lower()
        return {
            "status": "degraded" if denied else "unhealthy",
            "message": (
                f"{output}; add the rig user to the video group "
                f"(sudo usermod -aG video $USER) and log in again"
                if denied else output or "vcgencmd failed"
            ),
        }
    mask = int(match.group(1), 16)
    current = mask & 0xF
    historical = (mask >> 16) & 0xF
    # Every reading that could be parsed, this one included.
    faults = [current]
    for other_code, other_output in also:
        other = re.fullmatch(r"throttled=0x([0-9a-fA-F]+)", other_output)
        if other_code == 0 and other:
            value = int(other.group(1), 16)
            faults.append(value & 0xF)
            historical |= (value >> 16) & 0xF
    if any(faults):
        dipped = sum(1 for fault in faults if fault)
        try:
            began = datetime.fromisoformat(since) if since else now
        except ValueError:
            began = now
        if began.tzinfo is None:
            began = began.replace(tzinfo=timezone.utc)
        persisted = max(0.0, (now - began).total_seconds())
        sustained = persisted >= THROTTLE_PERSISTS_AFTER
        return {
            "status": "unhealthy" if sustained else "degraded",
            "message": (
                f"Pi power fault persisting for {int(persisted // 60)} min: "
                f"under-voltage/throttling flags set in {dipped} of {len(faults)} "
                f"readings ({output}); the supply is not carrying the rig"
                if sustained
                else f"Pi power dips under load: flags set in {dipped} of "
                     f"{len(faults)} readings ({output}); the supply is marginal"
            ),
            "current_flags": current or next((fault for fault in faults if fault), 0),
            "historical_flags": historical,
            "readings": len(faults),
            "readings_faulted": dipped,
            "faulting_since": began.isoformat(),
            "persisted_seconds": round(persisted),
        }
    message = "no current Pi throttling or power fault"
    if historical:
        message += f"; latched history retained ({output})"
    return {
        "status": "ok",
        "message": message,
        "current_flags": current,
        "historical_flags": historical,
    }


def route_problem(host: str, port: int, timeout: float) -> str | None:
    """Why this host cannot open TCP to a provider, or None. A seam for tests;
    the check itself is alteriom_hil.providers.route_problem, which sends
    nothing."""
    from alteriom_hil.providers import route_problem as check

    return check(host, port, timeout=timeout)


def provider_checks(checks: list[dict], url_file: str, send: str) -> None:
    """The rig's CallMeBot link and its route (docs/providers.md).

    A link that cannot be used is unhealthy: the rig was configured to
    validate the real service and cannot. A route that is down is degraded:
    the release row skips as a rig condition, and the service may simply be
    having a bad minute. Neither message carries the link.
    """
    try:
        from alteriom_hil import providers
    except ImportError as exc:
        add(checks, "provider_callmebot", "unhealthy", f"cannot load alteriom_hil.providers: {exc}")
        return
    try:
        link = providers.check_link_file(url_file)
    except providers.ProviderError as exc:
        add(checks, "provider_callmebot", "unhealthy", str(exc), send=send)
    else:
        add(checks, "provider_callmebot", "ok", f"{providers.redacted(link)} (send: {send})", send=send)
    problem = route_problem(providers.CALLMEBOT_HOST, providers.CALLMEBOT_PORT, 3.0)
    add(
        checks,
        "provider_callmebot_route",
        "degraded" if problem else "ok",
        problem or f"{providers.CALLMEBOT_HOST}:{providers.CALLMEBOT_PORT} accepts TCP connections",
    )


def collect_health(env: dict[str, str] | None = None, previous: dict | None = None) -> dict:
    """Every check, and the worst of them. `previous` is the last snapshot
    this host wrote, for the checks whose reading depends on what the last
    one saw (the power supply)."""
    values = dict(os.environ if env is None else env)
    checks: list[dict] = []

    mode = values.get("ALTERIOM_HIL_MODE")
    add(
        checks,
        "mode",
        "ok" if mode == "hardware" else "unhealthy",
        f"ALTERIOM_HIL_MODE={mode or 'unset'}",
    )

    venv_value = values.get("ALTERIOM_HIL_VENV")
    python = Path(venv_value) / "bin" / "python" if venv_value else None
    add(
        checks,
        "python",
        "ok" if python and python.is_file() else "unhealthy",
        str(python) if python and python.is_file() else "ALTERIOM_HIL_VENV is unset or invalid",
    )

    search_path = values.get("PATH")
    # What the rig itself runs. Not PlatformIO: the farm builds no firmware,
    # and setup-runner.sh no longer installs it -- requiring it here marked a
    # correctly provisioned host unhealthy. esptool is named `esptool.py` up
    # to v4 and `esptool` from v5.
    missing = [
        name for name in ("uhubctl", "git") if not shutil.which(name, path=search_path)
    ]
    if not any(shutil.which(name, path=search_path) for name in ("esptool", "esptool.py")):
        missing.append("esptool")
    add(
        checks,
        "tools",
        "ok" if not missing else "unhealthy",
        "required tools available" if not missing else f"missing: {', '.join(missing)}",
    )

    runner_unit = values.get("HIL_RUNNER_UNIT", "")
    if runner_unit:
        code, output = command("systemctl", "is-active", runner_unit)
        add(
            checks,
            "runner_service",
            "ok" if code == 0 and output == "active" else "unhealthy",
            f"{runner_unit}: {output or 'unknown'}",
        )
    elif values.get("ALTERIOM_HIL_FARM_MODE") == "node":
        add(checks, "runner_service", "ok", "no runner: a node takes its work and its releases from its portal")
    else:
        add(checks, "runner_service", "unhealthy", "HIL_RUNNER_UNIT is unset")

    if values.get("ALTERIOM_HIL_GATEWAY_ENABLED") == "1":
        gateway_unit = values.get(
            "ALTERIOM_HIL_GATEWAY_PROBE_UNIT",
            "alteriom-hil-gateway-probe.service",
        )
        code, output = command("systemctl", "is-active", gateway_unit)
        add(
            checks,
            "gateway_service",
            "ok" if code == 0 and output == "active" else "unhealthy",
            f"{gateway_unit}: {output or 'unknown'}",
        )

        password_value = values.get("ALTERIOM_HIL_WIFI_PASSWORD_FILE")
        password_file = Path(password_value) if password_value else None
        secret_ok = False
        if password_file and password_file.is_file():
            try:
                secret_ok = 8 <= len(password_file.read_text(encoding="utf-8").strip()) <= 63
            except OSError:
                pass
        add(
            checks,
            "gateway_credentials",
            "ok" if secret_ok else "unhealthy",
            "gateway credential file is readable and valid"
            if secret_ok
            else "gateway credential file is missing, unreadable, or invalid",
        )

        endpoint = values.get("ALTERIOM_HIL_GATEWAY_ENDPOINT", "").rstrip("/")
        try:
            with urllib.request.urlopen(f"{endpoint}/health", timeout=3) as response:
                payload = json.load(response)
                probe_ok = response.status == 200 and (
                    payload.get("status") == "ok" or payload.get("ok") is True
                )
            probe_message = f"{endpoint}/health returned HTTP 200"
            # A probe from before the features were named is still healthy;
            # the rows that need a feature skip on it.
            features = payload.get("features") if isinstance(payload, dict) else None
            if isinstance(features, list):
                probe_message += f" (features: {', '.join(map(str, features)) or 'none'})"
        except (OSError, ValueError, urllib.error.URLError) as exc:
            probe_ok = False
            probe_message = f"gateway probe unavailable: {exc}"
        add(
            checks,
            "gateway_endpoint",
            "ok" if probe_ok else "unhealthy",
            probe_message,
        )

        # The AP and the mesh must share a channel. A board promoted to bridge
        # associates here and takes its mesh AP to this channel, while boards
        # that are not bridges stay where the mesh was rooted; on two channels
        # a run's mesh splits in half, and a transfer that spans the split
        # dies. Read from the radio, because what NetworkManager was asked for
        # and what the interface ended up on can differ.
        wanted = values.get("ALTERIOM_HIL_GATEWAY_CHANNEL")
        if wanted:
            code, output = command("iw", "dev")
            live = ap_channel(output) if code == 0 else None
            if live is None:
                add(
                    checks,
                    "gateway_channel",
                    "degraded",
                    "cannot read the AP's channel"
                    + (f": {output.splitlines()[0]}" if code != 0 and output else
                       "; no interface is in AP mode"),
                )
            else:
                add(
                    checks,
                    "gateway_channel",
                    "ok" if str(live) == str(wanted) else "unhealthy",
                    f"the gateway AP is on channel {live}"
                    + ("" if str(live) == str(wanted) else
                       f", not the mesh channel {wanted}: a bridge would split the mesh"),
                    channel=live,
                )

    if values.get("ALTERIOM_HIL_SERVICE_ENABLED") == "1":
        service_unit = values.get(
            "ALTERIOM_HIL_SERVICE_UNIT", "alteriom-hil-farm.service"
        )
        code, output = command("systemctl", "is-active", service_unit)
        add(
            checks,
            "farm_service",
            "ok" if code == 0 and output == "active" else "unhealthy",
            f"{service_unit}: {output or 'unknown'}",
        )

        bind = values.get("ALTERIOM_HIL_API_BIND", "127.0.0.1")
        port = values.get("ALTERIOM_HIL_API_PORT", "8090")
        health_host = "127.0.0.1" if bind in ("0.0.0.0", "::") else bind
        endpoint = f"http://{health_host}:{port}/healthz"
        try:
            with urllib.request.urlopen(endpoint, timeout=3) as response:
                service_ok = response.status == 200
            service_message = f"{endpoint} returned HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            service_ok = False
            service_message = f"farm API unavailable: {exc}"
        add(
            checks,
            "farm_endpoint",
            "ok" if service_ok else "unhealthy",
            service_message,
        )

        public_host = values.get("ALTERIOM_HIL_PUBLIC_HOST")
        if public_host:
            code, output = command("systemctl", "is-active", "nginx.service")
            try:
                site_text = NGINX_SITE_PATH.read_text(encoding="utf-8")
            except OSError:
                site_text = ""
            tls_ready = "listen 443 ssl;" in site_text
            lan_ready = (
                "allow " in site_text
                and "proxy_pass http://127.0.0.1:8090" in site_text
            )
            proxy_active = code == 0 and output == "active"
            proxy_status = (
                "ok"
                if proxy_active and (tls_ready or lan_ready)
                else "degraded" if proxy_active else "unhealthy"
            )
            proxy_message = (
                f"nginx active; TLS certificate installed for {public_host}"
                if proxy_active and tls_ready
                else (
                    f"nginx LAN proxy active; public TLS deferred for {public_host}"
                    if proxy_active and lan_ready
                    else (
                        f"nginx bootstrap active; TLS certificate pending for {public_host}"
                        if proxy_active
                        else f"nginx.service: {output or 'unknown'}"
                    )
                )
            )
            add(
                checks,
                "reverse_proxy",
                proxy_status,
                proxy_message,
                public_host=public_host,
                certificate_ready=tls_ready,
                ingress_mode="tls" if tls_ready else "lan" if lan_ready else "bootstrap",
            )

    usage = shutil.disk_usage("/")
    used_pct = round((usage.used / usage.total) * 100, 1)
    try:
        disk_warn = int(values.get("HIL_DISK_WARN_PERCENT", "85"))
        disk_critical = int(values.get("HIL_DISK_CRITICAL_PERCENT", "95"))
    except ValueError:
        disk_warn, disk_critical = 85, 95
        add(checks, "disk_config", "unhealthy", "disk thresholds must be integers")
    disk_status = (
        "ok" if used_pct < disk_warn else "degraded" if used_pct < disk_critical else "unhealthy"
    )
    add(checks, "disk", disk_status, f"root filesystem {used_pct}% used", used_pct=used_pct)

    if shutil.which("vcgencmd", path=search_path):
        first, *also = read_throttling(search_path)
        throttle = classify_throttling(*first, *also, since=power_fault_since(previous))
        add(checks, "pi_power", **throttle)

    add(checks, "rig_lock", **rig_lock(values.get("ALTERIOM_HIL_RIG_LOCK")))

    add(checks, "host", **host_metrics())

    code, hub_output = (
        command("uhubctl", search_path=search_path)
        if shutil.which("uhubctl", path=search_path)
        else (1, "missing")
    )
    if code == 0 and "Current status for hub" in hub_output:
        add(
            checks,
            "usb_power",
            "ok",
            "uhubctl access available; independent switching is reported per board",
        )
    elif "permission" in hub_output.lower():
        add(checks, "usb_power", "unhealthy", hub_output.splitlines()[0])
    else:
        add(
            checks,
            "usb_power",
            "ok",
            "independent USB power control unavailable (optional recovery capability)",
            capability_available=False,
        )

    board_map_value = values.get("ALTERIOM_HIL_BOARD_MAP")
    try:
        minimum_boards = int(values.get("ALTERIOM_HIL_MINIMUM_BOARDS", "2"))
    except ValueError:
        minimum_boards = 2
        add(checks, "board_config", "unhealthy", "minimum board count must be an integer")
    board_map = Path(board_map_value) if board_map_value else None
    if not board_map or not board_map.is_file():
        message = f"waiting for {board_map}" if board_map else "ALTERIOM_HIL_BOARD_MAP is unset"
        add(checks, "board_map", "degraded", message, boards=0)
    else:
        try:
            import yaml

            raw = yaml.safe_load(board_map.read_text(encoding="utf-8")) or {}
            boards = raw.get("boards") or []
            unavailable = []
            missing_power = []
            for board in boards:
                port = Path(str(board.get("port", "")))
                try:
                    is_char = stat.S_ISCHR(port.stat().st_mode)
                except OSError:
                    is_char = False
                if not is_char or not os.access(port, os.R_OK | os.W_OK):
                    unavailable.append(str(board.get("id", port)))
                if board.get("power_hub") in (None, "") or board.get("power_port") is None:
                    missing_power.append(str(board.get("id", port)))
            enough_boards = len(boards) >= minimum_boards
            status = "ok" if enough_boards and not unavailable else "degraded"
            if unavailable:
                message = f"waiting for board devices: {', '.join(unavailable)}"
            elif not enough_boards:
                message = f"{len(boards)} board(s) available; {minimum_boards} required"
            elif missing_power:
                message = (
                    f"{len(boards)} board(s) available; independent power unavailable for: "
                    + ", ".join(missing_power)
                )
            else:
                message = f"{len(boards)} board(s) available"
            add(
                checks,
                "board_map",
                status,
                message,
                boards=len(boards),
                minimum_boards=minimum_boards,
                missing_power=missing_power,
            )
        except Exception as exc:
            add(checks, "board_map", "unhealthy", f"invalid {board_map}: {exc}")

    url_file = values.get("ALTERIOM_HIL_CALLMEBOT_URL_FILE")
    if url_file:
        provider_checks(checks, url_file, values.get("ALTERIOM_HIL_CALLMEBOT_SEND") or "release")

    backup_dir = values.get("ALTERIOM_HIL_BACKUP_DIR")
    if backup_dir:
        # A backup that quietly stopped is the one missing when it is needed.
        try:
            from alteriom_hil.backup import last_backup, staleness

            problem = staleness(Path(backup_dir))
            last = last_backup(Path(backup_dir))
            add(
                checks,
                "backup",
                "degraded" if problem else "ok",
                problem or f"last backup {last['created_at']}",
            )
        except Exception as exc:
            add(checks, "backup", "degraded", f"cannot read the backup record: {exc}")

    overall = max((item["status"] for item in checks), key=SEVERITY.get)
    return {
        "schema": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "status": overall,
        "checks": checks,
    }


def write_atomic(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", delete=False, encoding="utf-8"
    ) as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def render(payload: dict) -> str:
    lines = [f"HIL service: {payload['status'].upper()} ({payload['timestamp']})"]
    for item in payload["checks"]:
        lines.append(f"  {item['status'].upper():9} {item['name']}: {item['message']}")
    return "\n".join(lines)


def notify_transition(previous: dict | None, payload: dict, notifier) -> dict | None:
    """Tell a person when the host went unhealthy, and when it came back.

    Transitions only: a health check runs every few minutes, and a message
    each time the host is still broken is one people learn to ignore.

    `notifier` may be one channel or several: a rig can be told to say things
    in more than one place. What comes back is what the first said, which is
    what the health snapshot has always recorded.
    """
    channels = ([] if notifier is None
                else list(notifier) if isinstance(notifier, (list, tuple)) else [notifier])
    if not channels:
        return None
    notifier = channels[0]
    from alteriom_hil.notify import Notification

    before = (previous or {}).get("status")
    now = payload.get("status")
    failing = [item for item in payload.get("checks") or [] if item.get("status") == "unhealthy"]
    link = notifier.link("#overview")
    if now == "unhealthy" and before != "unhealthy":
        return _tell(channels, Notification(
            "host_unhealthy", f"The farm host {payload.get('hostname')} is unhealthy",
            "; ".join(f"{item['name']}: {item['message']}" for item in failing),
            link=link,
        ))
    if before == "unhealthy" and now != "unhealthy":
        return _tell(channels, Notification(
            "host_unhealthy", f"The farm host {payload.get('hostname')} has recovered",
            f"Health is {now} again.", link=link, tone="good",
        ))
    return None


def _tell(channels: list, note) -> dict | None:
    """Every channel, and what the first of them said: the health snapshot
    records one outcome, and a second channel failing is that channel's own
    business rather than this host's health."""
    outcomes = [channel.send(note) for channel in channels]
    return outcomes[0] if outcomes else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--fail-unhealthy", action="store_true")
    parser.add_argument(
        "--except-supply",
        action="store_true",
        help="with --fail-unhealthy: do not fail for a host power-supply check "
             "(HOST_SUPPLY_CHECKS), which says nothing about the release just installed",
    )
    args = parser.parse_args(argv)
    previous = None
    if args.output:
        try:
            previous = json.loads(args.output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
        if not isinstance(previous, dict):
            previous = None
    payload = collect_health(previous=previous)
    if args.output:
        try:
            from alteriom_hil.notify import Notifier

            delivered = notify_transition(previous, payload, Notifier.all_from_env(os.environ))
        except ImportError:
            delivered = None
        if delivered is not None:
            payload["notification"] = delivered
        write_atomic(args.output, payload)
    print(json.dumps(payload, sort_keys=True) if args.json else render(payload))
    blocking = [
        check for check in payload["checks"]
        if check["status"] == "unhealthy"
        and not (args.except_supply and check["name"] in HOST_SUPPLY_CHECKS)
    ]
    return int(
        (args.strict and payload["status"] != "ok")
        or (args.fail_unhealthy and bool(blocking))
    )


if __name__ == "__main__":
    raise SystemExit(main())
