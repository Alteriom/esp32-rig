#!/usr/bin/env bash
# Provision the Pi's unused wlan0 as a deterministic HIL gateway test AP.

set -euo pipefail

SSID="${HIL_GATEWAY_SSID:-Alteriom-HIL}"
PASSWORD_FILE="${HIL_GATEWAY_PASSWORD_FILE:-/etc/alteriom-hil/gateway-wifi-password}"
CONNECTION="${HIL_GATEWAY_CONNECTION:-alteriom-hil-ap}"
ADDRESS="${HIL_GATEWAY_ADDRESS:-10.42.0.1/24}"
SUBNET="${HIL_GATEWAY_SUBNET:-10.42.0.0/24}"
PORT="${HIL_GATEWAY_PORT:-8088}"
# The channel the mesh under test runs on. A board promoted to bridge
# associates with this AP and takes its mesh AP to the AP's channel; boards
# that are not bridges stay on the channel the mesh was rooted on, which is
# painlessMesh's default, 1. With the two different, one run's bridge splits
# the mesh in half and the halves only find each other through channel
# re-detection -- long enough for an OTA transfer or a gateway relay to die
# mid-run. Keep this equal to the mesh channel; override only for a rig whose
# band is congested, and then move the mesh with it.
CHANNEL="${HIL_GATEWAY_CHANNEL:-1}"
AP_INTERFACE="${HIL_GATEWAY_INTERFACE:-wlan0}"
UPLINK_INTERFACE="${HIL_GATEWAY_UPLINK_INTERFACE:-eth0}"
RUN_USER="${HIL_RUN_USER:-${SUDO_USER:-$USER}}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$PASSWORD_FILE" ]; then
  echo "Missing $PASSWORD_FILE; create a root-owned 0600 WPA2 password first." >&2
  exit 2
fi
# NetworkManager runs the AP. Raspberry Pi OS has it; Ubuntu Server images use
# netplan with systemd-networkd and ship no nmcli, so this script would fail
# half-way with "command not found" (a rig in bring-up, 2026-09-16). Say so up front, with
# what to install.
if ! command -v nmcli >/dev/null 2>&1; then
  cat >&2 <<'EOF'
This host has no nmcli: the rig's access point is a NetworkManager connection.

On Ubuntu Server:

    sudo apt-get install -y network-manager
    sudo systemctl enable --now NetworkManager

Leave the wired interface to netplan and let NetworkManager have the Wi-Fi
one, then run this script again.
EOF
  exit 2
fi
PASSWORD="$(tr -d '\r\n' < "$PASSWORD_FILE")"
if [ "${#PASSWORD}" -lt 8 ]; then
  echo "Gateway Wi-Fi password must contain at least 8 characters." >&2
  exit 2
fi

sudo nmcli radio wifi on
if ! sudo nmcli -t -f NAME connection show | grep -Fxq "$CONNECTION"; then
  sudo nmcli connection add type wifi ifname "$AP_INTERFACE" con-name "$CONNECTION" ssid "$SSID"
fi
sudo nmcli connection modify "$CONNECTION" \
  connection.autoconnect yes \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  802-11-wireless.channel "$CHANNEL" \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "$PASSWORD" \
  wifi-sec.proto rsn \
  wifi-sec.pairwise ccmp \
  wifi-sec.group ccmp \
  wifi-sec.pmf disable \
  ipv4.method shared \
  ipv4.addresses "$ADDRESS" \
  ipv6.method disabled
sudo nmcli connection up "$CONNECTION"

# UFW's stock after-input rules discard DHCP requests before user rules are
# evaluated.  Install the narrowly scoped request rule in ufw-before-input,
# then expose DNS/the deterministic probe and permit AP-to-uplink forwarding.
UFW_BEFORE_RULE="-A ufw-before-input -i $AP_INTERFACE -p udp --sport 68 --dport 67 -j ACCEPT"
if sudo test -f /etc/ufw/before.rules && \
   ! sudo grep -Fqx -- "$UFW_BEFORE_RULE" /etc/ufw/before.rules; then
  UFW_TEMP="$(mktemp)"
  sudo awk -v rule="$UFW_BEFORE_RULE" '
    !inserted && $0 == "COMMIT" { print rule; inserted = 1 }
    { print }
    END { if (!inserted) exit 1 }
  ' /etc/ufw/before.rules > "$UFW_TEMP"
  sudo install -m 0644 "$UFW_TEMP" /etc/ufw/before.rules
  rm -f -- "$UFW_TEMP"
fi
if sudo ufw status 2>/dev/null | grep -q '^Status: active'; then
  sudo ufw allow in on "$AP_INTERFACE" to "${ADDRESS%/*}" port 53 proto udp \
    comment 'HIL gateway DNS' >/dev/null
  sudo ufw allow in on "$AP_INTERFACE" to "${ADDRESS%/*}" port 53 proto tcp \
    comment 'HIL gateway DNS TCP' >/dev/null
  sudo ufw allow in on "$AP_INTERFACE" to "${ADDRESS%/*}" port "$PORT" proto tcp \
    comment 'HIL gateway probe' >/dev/null
  sudo ufw route allow in on "$AP_INTERFACE" out on "$UPLINK_INTERFACE" \
    from "$SUBNET" comment 'HIL gateway Internet' >/dev/null
  sudo ufw reload >/dev/null
fi

sudo install -d -m 0755 /usr/local/lib/alteriom-hil
sudo install -m 0755 "$HERE/../suites/painlessmesh/gateway_probe_server.py" /usr/local/lib/alteriom-hil/gateway_probe_server.py
sudo install -d -m 0750 -o "$RUN_USER" -g "$RUN_USER" /var/lib/alteriom-hil
sudo tee /etc/systemd/system/alteriom-hil-gateway-probe.service >/dev/null <<EOF
[Unit]
Description=Alteriom HIL deterministic gateway HTTP probe
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
ExecStart=/usr/bin/python3 /usr/local/lib/alteriom-hil/gateway_probe_server.py --port $PORT
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now alteriom-hil-gateway-probe.service
sudo systemctl restart alteriom-hil-gateway-probe.service
for _ in 1 2 3 4 5; do
  if curl --fail --silent "http://127.0.0.1:$PORT/health" >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent --show-error "http://127.0.0.1:$PORT/health" >/dev/null
echo "Gateway test network ready: $SSID at $ADDRESS, probe port $PORT"
