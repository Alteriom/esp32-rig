#!/usr/bin/env bash
# Provision the farm host's MQTT broker: what a run's gateway board publishes
# to and the suite reads from. Mosquitto, listening on the rig's private AP
# address and loopback only -- the AP is already the rig's own network, and
# the broker holds nothing but the current run's traffic -- with no
# persistence, so one run never reads another's retained messages.
#
# Companion to setup-gateway-network.sh: run that first, since the AP address
# this binds to must exist. Re-runnable. When the two hosts route to each
# other, the simulation host publishes to the same broker; nothing here needs
# to change for that beyond the firewall rule it would then need.

set -euo pipefail

PORT="${HIL_MQTT_PORT:-1883}"
AP_ADDRESS="${HIL_GATEWAY_ADDRESS:-10.42.0.1/24}"
AP_INTERFACE="${HIL_GATEWAY_INTERFACE:-wlan0}"
BIND="${AP_ADDRESS%/*}"
CONF=/etc/mosquitto/conf.d/alteriom-hil.conf

if ! ip -4 -brief addr show "$AP_INTERFACE" 2>/dev/null | grep -q "$BIND/"; then
  echo "$AP_INTERFACE does not carry $BIND; run setup-gateway-network.sh first." >&2
  exit 2
fi

if ! command -v mosquitto >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq mosquitto mosquitto-clients
fi

# The stock mosquitto.conf includes conf.d/*.conf; per_listener_settings has
# to precede every listener, and the stock file declares none.
sudo tee "$CONF" >/dev/null <<EOF
# Alteriom HIL broker (rig/setup-mqtt-broker.sh). Regenerated on re-run.
per_listener_settings true
persistence false
listener $PORT 127.0.0.1
allow_anonymous true
listener $PORT $BIND
allow_anonymous true
EOF

# The AP address comes up with NetworkManager, which can be after mosquitto
# at boot; a bind to an address that is not there yet fails, so the unit
# waits for the network and retries rather than staying down.
sudo install -d -m 0755 /etc/systemd/system/mosquitto.service.d
sudo tee /etc/systemd/system/mosquitto.service.d/alteriom-hil.conf >/dev/null <<EOF
[Unit]
After=network-online.target NetworkManager-wait-online.service
Wants=network-online.target

[Service]
Restart=on-failure
RestartSec=5
EOF

if sudo ufw status 2>/dev/null | grep -q '^Status: active'; then
  sudo ufw allow in on "$AP_INTERFACE" to "$BIND" port "$PORT" proto tcp \
    comment 'HIL MQTT broker' >/dev/null
  sudo ufw reload >/dev/null
fi

sudo systemctl daemon-reload
sudo systemctl enable --now mosquitto.service
sudo systemctl restart mosquitto.service

# A round trip on loopback, then on the AP address: the broker is up and
# listening where the boards will look for it.
for address in 127.0.0.1 "$BIND"; do
  probe="alteriom-hil/probe/$$"
  got="$(timeout 5 mosquitto_sub -h "$address" -p "$PORT" -t "$probe" -C 1 -W 3 2>/dev/null &
         sleep 0.5; mosquitto_pub -h "$address" -p "$PORT" -t "$probe" -m ok; wait)"
  if [ "$got" != "ok" ]; then
    echo "broker did not answer on $address:$PORT" >&2
    exit 1
  fi
done
echo "MQTT broker up on 127.0.0.1:$PORT and $BIND:$PORT (mqtt://$BIND:$PORT for a run)"
echo "Enable it for runs with: sudo alteriom-hil-admin config set mqtt.enabled true"
