#!/usr/bin/env bash
# Provision an ESP32-farm runner host (Raspberry Pi 4/5 or any Debian-ish
# mini-PC). Run as a sudo-capable user. Idempotent.
#
# After this script you still need the HUMAN steps in
# docs/bringup-checklist.md (plug boards, tune udev KERNELS, register the
# GitHub runner with a token).

set -euo pipefail

echo "==> apt dependencies"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv git uhubctl openssl

HIL_HOME="${ALTERIOM_HIL_HOME:-$HOME/.local/share/alteriom-hil}"
VENV="$HIL_HOME/venv"

echo "==> isolated Python environment"
mkdir -p "$HIL_HOME"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
# esptool flashes; nothing here compiles. The farm does not build firmware --
# each project builds its own bundle in its CI -- so there is no toolchain to
# install on this host.
"$VENV/bin/python" -m pip install --upgrade esptool

echo "==> serial-port and board-health group access"
# dialout for the boards' serial ports; video for /dev/vcio, which is how
# vcgencmd reads a Pi's throttling and temperature. Without video the health
# check reports the host unhealthy over a permission, which reads like a
# hardware fault (a rig in bring-up, 2026-09-16).
sudo usermod -aG dialout "$USER"
if [ -e /dev/vcio ]; then
  sudo usermod -aG video "$USER"
fi

echo "==> udev rules (edit board KERNELS to match your hub first!)"
if compgen -G "$(dirname "$0")/udev/*.rules" >/dev/null; then
  sudo cp "$(dirname "$0")"/udev/*.rules /etc/udev/rules.d/
  sudo udevadm control --reload
  sudo udevadm trigger --attr-match=subsystem=usb
  sudo udevadm trigger --subsystem-match=tty
fi

echo "==> HAL package"
"$VENV/bin/python" -m pip install -e "$(dirname "$0")/../core[dev]"
"$VENV/bin/python" -m pip install -e "$(dirname "$0")/../rig[hardware,dev]"
# The portal is the farm's own half and is not in the rig's repository: a
# checkout that has it is the farm's; a rig's has core and rig and that is
# the whole of it. Installing it unconditionally failed every fresh install
# from the public repository at this line.
if [ -d "$(dirname "$0")/../portal" ]; then
  "$VENV/bin/python" -m pip install -e "$(dirname "$0")/../portal[dev]"
fi

RUNNER_ENV="$HIL_HOME/runner.env"
touch "$RUNNER_ENV"
set_runner_env() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "$RUNNER_ENV"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$RUNNER_ENV"
  else
    printf '%s=%s\n' "$key" "$value" >> "$RUNNER_ENV"
  fi
}
set_runner_env ALTERIOM_HIL_VENV "$VENV"
set_runner_env PATH "$VENV/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# A rig joining a portal (join-rig.sh) has no GitHub runner to register: the
# script that ran this says what is left.
[ "${HIL_JOINING:-0}" = 1 ] && exit 0

cat <<'EOF'

Done. Remaining HUMAN steps (docs/bringup-checklist.md):
 1. Plug the board bank in; verify /dev/esp32-farm-* symlinks appear.
 2. Copy rig/board-map.example.yaml -> board-map.yaml and adjust.
 3. Log out/in (dialout group takes effect on a new session).
 4. Check the rig before touching GitHub:
      ALTERIOM_HIL_BOARD_MAP=~/board-map.yaml ./verify-rig.sh
    It must print "rig is ready" — it checks tooling, permissions, udev
    symlinks, the board map, power control, and probes every board.
 5. Register the self-hosted GitHub Actions runner (org level), labels:
      self-hosted, esp32-farm
    https://github.com/organizations/Alteriom/settings/actions/runners/new
 6. Copy ~/.local/share/alteriom-hil/runner.env to the Actions runner's
    .env, then add:
      ALTERIOM_HIL_MODE=hardware
      ALTERIOM_HIL_BOARD_MAP=/home/<user>/esp32-farm/board-map.yaml
 7. Start the runner service.
EOF
