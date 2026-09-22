#!/usr/bin/env bash
# Preflight for an ESP32-farm rig. Run this on the runner host AFTER
# setup-runner.sh and after plugging boards in — it checks every
# precondition the HIL workflow depends on and tells you exactly which one
# is broken, instead of you finding out 40 minutes into a CI job.
#
#   ./verify-rig.sh                     # full check (probes each board)
#   ./verify-rig.sh --quick             # skip the esptool board probe
#   ALTERIOM_HIL_BOARD_MAP=~/board-map.yaml ./verify-rig.sh
#
# Exit 0 = rig is ready. Exit 1 = at least one FAIL. Warnings never fail.
#
# Safe to re-run: read-only except for the serial probe, which only reads
# each board's chip ID.

set -uo pipefail

QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

# Runner services often start without USER set; derive it rather than
# tripping `set -u`.
USER="${USER:-$(id -un)}"

HERE="$(cd "$(dirname "$0")" && pwd)"
# Both halves of alteriom_hil, as a PYTHONPATH: the core and the rig are
# two distributions filling one package, and this script runs from a
# checkout where neither need be installed.
HAL_DIR="$HERE/../core:$HERE/../rig"
BOARD_MAP="${ALTERIOM_HIL_BOARD_MAP:-$HOME/board-map.yaml}"
REGISTRY="${ALTERIOM_HIL_INVENTORY:-/var/lib/alteriom-hil/inventory.yaml}"
# A rig nobody has registered a board on yet is a rig in bring-up, not a
# broken one: its boards, its udev names and its board map are steps still
# ahead of it, and failing them fails the release install that a new rig is
# in the middle of (a rig in bring-up, 2026-09-16). They are reported either way; what
# changes is whether they are a fault.
# Asked of the loader the farm itself uses. A line-oriented guess read
# `boards: [{id: esp32-01, ...}]` -- a registry an operator may well write --
# as no registry at all, and then excused a missing board map on a rig that
# has hardware registered.
NEW_RIG=$(PYTHONPATH="${PYTHONPATH:-}:$HAL_DIR" python3 - "$REGISTRY" <<'PY' 2>/dev/null || echo 1
import sys
try:
    from alteriom_hil.inventory import load_registry
    print(0 if load_registry(sys.argv[1]) else 1)
except Exception:
    # A registry that cannot be read is not a new rig: section 5 says so.
    print(0)
PY
)
UDEV_RULES="/etc/udev/rules.d/99-esp32-farm.rules"
UHUB_RULES="/etc/udev/rules.d/52-uhubctl.rules"

FAILS=0
WARNS=0

if [ -t 1 ]; then
  R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'
else
  R=""; G=""; Y=""; B=""; N=""
fi

pass() { printf '  %sPASS%s  %s\n' "$G" "$N" "$1"; }
fail() { printf '  %sFAIL%s  %s\n' "$R" "$N" "$1"; FAILS=$((FAILS + 1)); }
warn() { printf '  %sWARN%s  %s\n' "$Y" "$N" "$1"; WARNS=$((WARNS + 1)); }
hint() { printf '        %s\n' "$1"; }
section() { printf '\n%s%s%s\n' "$B" "$1" "$N"; }

# ---------------------------------------------------------------- 1. host
section "1. Host tooling"

for bin in python3 git; do
  if command -v "$bin" >/dev/null 2>&1; then
    pass "$bin present"
  else
    fail "$bin missing"
    hint "run ./setup-runner.sh"
  fi
done

if command -v uhubctl >/dev/null 2>&1; then
  pass "uhubctl present ($(uhubctl --version 2>&1 | head -n1))"
  if [ -r "$UHUB_RULES" ]; then
    pass "$UHUB_RULES installed"
  else
    warn "$UHUB_RULES missing — hub control may require sudo"
    hint "run ./setup-runner.sh"
  fi
else
  warn "uhubctl missing — power-cycle recovery and power tests will skip"
  hint "sudo apt-get install uhubctl"
fi

if python3 -c "import esptool" >/dev/null 2>&1 || command -v esptool.py >/dev/null 2>&1; then
  pass "esptool importable"
else
  fail "esptool missing — flashing will fail"
  hint "python3 -m pip install --user esptool"
fi

# ------------------------------------------------------------ 2. HAL pkg
section "2. HIL package"

if PYTHONPATH="${PYTHONPATH:-}:$HAL_DIR" python3 -c "import alteriom_hil" >/dev/null 2>&1; then
  pass "alteriom_hil importable"
else
  fail "alteriom_hil not importable"
  hint "python3 -m pip install --user -e $HERE/../core[dev] && python3 -m pip install --user -e $HERE/../rig[hardware,dev]"
fi

if python3 -c "import serial" >/dev/null 2>&1; then
  pass "pyserial present"
else
  fail "pyserial missing — serial capture will fail"
  hint "python3 -m pip install --user 'pyserial>=3.5'"
fi

# --------------------------------------------------------- 3. permissions
section "3. Permissions"

if id -nG "$USER" 2>/dev/null | tr ' ' '\n' | grep -qx dialout; then
  pass "$USER is in the dialout group (active in this session)"
elif getent group dialout 2>/dev/null | grep -q "\b$USER\b"; then
  warn "$USER is in dialout but the session predates it — log out/in"
  hint "the GitHub runner service must also be restarted to pick it up"
else
  fail "$USER is not in the dialout group — serial ports will be denied"
  hint "sudo usermod -aG dialout $USER && log out/in"
fi

# --------------------------------------------------------------- 4. udev
section "4. udev rules"

if [ -f "$UDEV_RULES" ]; then
  pass "$UDEV_RULES installed"
  if grep -q '^SUBSYSTEM=="tty", KERNELS=="1-1\.[0-9]"' "$UDEV_RULES" &&
     ! ls /dev/esp32-farm-* >/dev/null 2>&1; then
    warn "rules still carry the example KERNELS paths and no symlinks exist"
    hint "udevadm info -a -n /dev/ttyUSB0 | grep KERNELS  # then edit to match"
  fi
else
  fail "$UDEV_RULES not installed — device paths will not be stable"
  hint "sudo cp $HERE/udev/99-esp32-farm.rules /etc/udev/rules.d/ &&
        sudo udevadm control --reload && sudo udevadm trigger"
fi

symlinks=$(ls -1 /dev/esp32-farm-* 2>/dev/null | wc -l)
if [ "$symlinks" -gt 0 ]; then
  pass "$symlinks /dev/esp32-farm-* symlink(s) present"
elif [ "$NEW_RIG" = 1 ]; then
  warn "no /dev/esp32-farm-* symlinks yet — no board is registered on this rig"
  hint "plug the boards in, then: alteriom-hil-admin boards discover"
else
  # Not a fault. A board is registered by its MAC, and every discovery
  # rewrites the active map with the port that MAC is on now -- so a tty that
  # moved is corrected by the next discovery rather than breaking a run. The
  # symlinks are still worth having (a name that never moves reads better in
  # a log), which is why this is said at all. Section 6 fails a board whose
  # port is not there.
  warn "no /dev/esp32-farm-* symlinks — boards are named by their MAC instead"
  hint "for stable names: set KERNELS in $UDEV_RULES from 'udevadm info -a -n /dev/ttyUSB0'"
fi

# ---------------------------------------------------------- 5. board map
section "5. Board map"

no_boards_yet() {
  # A rig in bring-up: say what is missing, and let the release install it is
  # in the middle of finish. Only the board steps are excused -- a host that
  # failed a check of its own is still a rig that is not ready.
  warn "$1"
  hint "plug the boards in; they register themselves (alteriom-hil-admin boards discover)"
  if [ "$FAILS" -gt 0 ]; then
    printf '\n%sSummary%s: %d fail, %d warn — rig NOT ready\n' "$B" "$N" "$FAILS" "$WARNS"
    exit 1
  fi
  printf '\n%sSummary%s: %d fail, %d warn — rig is set up, with no boards registered yet\n' \
    "$B" "$N" "$FAILS" "$WARNS"
  exit 0
}

if [ ! -f "$BOARD_MAP" ] && [ "$NEW_RIG" = 1 ]; then
  no_boards_yet "no board map yet at $BOARD_MAP — no board is registered on this rig"
fi
if [ ! -f "$BOARD_MAP" ]; then
  fail "board map not found at $BOARD_MAP"
  hint "cp $HERE/board-map.example.yaml $BOARD_MAP && edit it"
  hint "or set ALTERIOM_HIL_BOARD_MAP to its path"
  printf '\n%sSummary%s: %d fail, %d warn — rig NOT ready\n' "$B" "$N" "$FAILS" "$WARNS"
  exit 1
fi

BOARDS=$(PYTHONPATH="${PYTHONPATH:-}:$HAL_DIR" python3 - "$BOARD_MAP" <<'PY' 2>&1
import sys
try:
    from alteriom_hil.board import BoardMap
    for b in BoardMap.load(sys.argv[1]):
        print(f"{b.id}\t{b.port}\t{b.power_hub or ''}\t{'' if b.power_port is None else b.power_port}")
except ValueError as exc:
    # `boards: []` is what a rig publishes before anything is registered: not
    # a map that cannot be read, and reading it as one failed the release
    # install of a rig in bring-up (2026-09-16). Only that document is
    # excused -- an empty file, `{}`, `boards: null` or a mapping with no
    # `boards` key are all a map somebody got wrong, and are still failures.
    import yaml

    empty = False
    if "has no boards" in str(exc):
        try:
            document = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
            empty = isinstance(document, dict) and document.get("boards") == []
        except Exception:
            empty = False
    print("!EMPTY" if empty else f"!ERROR\t{exc}")
    sys.exit(0 if empty else 1)
except Exception as exc:
    print(f"!ERROR\t{exc}")
    sys.exit(1)
PY
)

if printf '%s' "$BOARDS" | grep -q '^!EMPTY'; then
  if [ "$NEW_RIG" = 1 ]; then
    no_boards_yet "the board map at $BOARD_MAP is empty — no board is registered on this rig"
  fi
  fail "the board map at $BOARD_MAP is empty, but boards are registered"
  hint "alteriom-hil-admin boards discover  # republishes the map from the registry"
  printf '\n%sSummary%s: %d fail, %d warn — rig NOT ready\n' "$B" "$N" "$FAILS" "$WARNS"
  exit 1
fi
if [ $? -ne 0 ] || printf '%s' "$BOARDS" | grep -q '^!ERROR'; then
  fail "board map $BOARD_MAP does not parse"
  hint "$(printf '%s' "$BOARDS" | sed 's/^!ERROR\t//')"
  printf '\n%sSummary%s: %d fail, %d warn — rig NOT ready\n' "$B" "$N" "$FAILS" "$WARNS"
  exit 1
fi

n_boards=$(printf '%s\n' "$BOARDS" | grep -c .)
pass "board map parses — $n_boards board(s)"
minimum_boards="${ALTERIOM_HIL_MINIMUM_BOARDS:-2}"
if [ "$n_boards" -lt "$minimum_boards" ]; then
  fail "board map has $n_boards board(s); painlessMesh requires $minimum_boards"
  hint "add another independently controllable mesh node before dispatching HIL"
fi

if [ "$symlinks" -gt 0 ] && [ "$n_boards" -ne "$symlinks" ]; then
  warn "board map lists $n_boards board(s) but $symlinks symlink(s) exist"
  hint "a board may be unplugged, or the map may be out of date"
fi

# ------------------------------------------------------------- 6. devices
section "6. Board devices"

seen_real=""
while IFS=$'\t' read -r id port hub pport; do
  [ -z "${id:-}" ] && continue

  if [ ! -e "$port" ]; then
    fail "$id: $port does not exist"
    hint "board unplugged, or its udev KERNELS path is wrong"
    continue
  fi
  if [ ! -c "$port" ]; then
    fail "$id: $port is not a character device"
    continue
  fi

  # Two symlinks pointing at the same tty is a classic udev misconfig: the
  # suite silently tests one board twice and mesh tests hang.
  real=$(readlink -f "$port")
  if printf '%s' "$seen_real" | tr ' ' '\n' | grep -qx "$real"; then
    fail "$id: $port resolves to $real, already used by another board"
    hint "duplicate KERNELS in $UDEV_RULES — give each port a distinct path"
    continue
  fi
  seen_real="$seen_real $real"

  if [ -r "$port" ] && [ -w "$port" ]; then
    pass "$id: $port -> $real (rw)"
  else
    fail "$id: $port not readable/writable by $USER"
    hint "dialout group, or MODE= in $UDEV_RULES"
  fi
done <<< "$BOARDS"

# --------------------------------------------------------------- 7. power
section "7. Power control"

powered=$(printf '%s\n' "$BOARDS" | awk -F'\t' '$3 != "" && $4 != ""' | grep -c .)
while IFS=$'\t' read -r id port hub pport; do
  [ -z "${id:-}" ] && continue
  if [ -z "${hub:-}" ] || [ -z "${pport:-}" ]; then
    warn "$id: no independent power coordinates"
    hint "connect this board to its own switchable hub port, then set power_hub/power_port"
  fi
done <<< "$BOARDS"
if [ "$powered" -eq 0 ]; then
  warn "no board has power_hub/power_port — optional power-cycle recovery unavailable"
elif ! command -v uhubctl >/dev/null 2>&1; then
  warn "$powered board(s) map power coordinates but uhubctl is missing"
else
  hubs=$(uhubctl 2>/dev/null)
  if [ -z "$hubs" ]; then
    warn "uhubctl found no switchable hub (needs sudo, or hub is not compatible)"
    hint "sudo uhubctl   # if this lists nothing, the hub can't switch power"
  else
    while IFS=$'\t' read -r id port hub pport; do
      [ -z "${hub:-}" ] || [ -z "${pport:-}" ] && continue
      if printf '%s' "$hubs" | grep -q "hub $hub"; then
        pass "$id: hub $hub port $pport switchable"
      else
        warn "$id: hub $hub not listed by uhubctl"
        hint "run 'sudo uhubctl' and correct power_hub in $BOARD_MAP"
      fi
    done <<< "$BOARDS"
  fi
fi

# --------------------------------------------------------------- 8. probe
section "8. Board probe"

if [ "$QUICK" = "1" ]; then
  warn "skipped (--quick)"
else
  if python3 -c "import esptool" >/dev/null 2>&1; then
    while IFS=$'\t' read -r id port hub pport; do
      [ -z "${id:-}" ] && continue
      [ -c "$port" ] || continue
      out=$(python3 -m esptool --port "$port" --before default_reset chip_id 2>&1)
      if printf '%s' "$out" | grep -qi "chip is\|Chip type"; then
        chip=$(printf '%s' "$out" | grep -i "chip is\|chip type" | head -n1 | sed 's/^ *//')
        pass "$id: responds — ${chip:-ok}"
      else
        fail "$id: no response on $port"
        hint "bad cable (charge-only?), board held in reset, or wrong port"
        hint "last esptool line: $(printf '%s' "$out" | tail -n1)"
      fi
    done <<< "$BOARDS"
  else
    warn "esptool not importable — cannot probe boards"
  fi
fi

# -------------------------------------------------------------- summary
printf '\n%sSummary%s: ' "$B" "$N"
if [ "$FAILS" -eq 0 ]; then
  printf '%s0 fail%s, %d warn — rig is ready.\n' "$G" "$N" "$WARNS"
  printf 'Next: register the runner (docs/bringup-checklist.md step 6), then\n'
  printf 'dispatch the "HIL painlessMesh" workflow.\n'
  exit 0
fi
printf '%s%d fail%s, %d warn — rig NOT ready.\n' "$R" "$FAILS" "$N" "$WARNS"
printf 'Fix the FAIL lines above and re-run. Day-2 issues: docs/runbook.md\n'
exit 1
