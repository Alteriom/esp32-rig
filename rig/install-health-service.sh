#!/usr/bin/env bash
# Install scheduled HIL health/status reporting after the Actions runner exists.

set -euo pipefail

RUN_USER="${HIL_RUN_USER:-${SUDO_USER:-$USER}}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
HIL_HOME="${ALTERIOM_HIL_HOME:-$RUN_HOME/.local/share/alteriom-hil}"
VENV="$HIL_HOME/venv"
RUNNER_HOME="${HIL_ACTIONS_RUNNER_DIR:-$RUN_HOME/actions-runner}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# `systemctl list-unit-files` exits 1 when nothing matches, and under
# pipefail that failed the assignment and ended the installer without a word:
# the first release a node installed after its runner was removed stopped
# here (2026-09-14). No runner is an answer, not an error.
RUNNER_UNIT="${HIL_RUNNER_UNIT:-$(systemctl list-unit-files 'actions.runner.*.service' --no-legend 2>/dev/null | awk 'NR == 1 {print $1}' || true)}"
# Named by the configuration and since removed (a host that became a node):
# there is none to restart or configure.
if [ -n "$RUNNER_UNIT" ] && ! systemctl list-unit-files "$RUNNER_UNIT" --no-legend 2>/dev/null | grep -q .; then
  RUNNER_UNIT=""
fi

# A node (farm.mode: node) has no GitHub runner: its portal hands it work and
# releases. A standalone or attached host is deployed through its runner.
if [ -z "$RUNNER_UNIT" ]; then
  echo "No GitHub Actions runner service on this host; installing without one (a node)."
fi
if [ ! -x "$VENV/bin/python" ]; then
  echo "HIL virtual environment missing at $VENV; run setup-runner.sh first." >&2
  exit 1
fi

# The Deploy farm host workflow re-runs this installer from a job on the
# runner service, where nobody can type a password: the runner user needs
# non-interactive sudo. This is a dedicated appliance (harden-pi.sh), and the
# user already owns the checkout the installer runs from, so a narrower rule
# would not be narrower in practice.
SUDOERS=/etc/sudoers.d/alteriom-hil
if ! sudo -n true 2>/dev/null || [ ! -f "$SUDOERS" ]; then
  printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$RUN_USER" | sudo tee "$SUDOERS" >/dev/null
  sudo chmod 0440 "$SUDOERS"
  sudo visudo -cf "$SUDOERS" >/dev/null || { sudo rm -f "$SUDOERS"; echo "sudoers rule rejected by visudo; removed" >&2; exit 1; }
fi

sudo install -d -m 0755 /usr/local/lib/alteriom-hil
# Nothing of the service is copied here any more: the launcher, the agent,
# the health check and the admin CLI are the installed package, run by name
# out of the venv (docs/public-release-plan.md, steps 12d and 12e). What is
# still written here is what a unit outside the venv runs: the gateway probe,
# node-update.sh, node-control.sh, the schema and the dashboard bundle.
# What an older deploy copied here and this one does not own: the two halves
# of the manager and the host's configuration module, all three installed
# into the venv now. Nothing imports them from here any more, and a stale
# copy beside the service is the kind of thing somebody reads and believes on
# the day it matters.
sudo rm -f \
  /usr/local/lib/alteriom-hil/portal_manager.py \
  /usr/local/lib/alteriom-hil/rig_manager.py \
  /usr/local/lib/alteriom-hil/hil_config.py \
  /usr/local/lib/alteriom-hil/health_check.py \
  /usr/local/lib/alteriom-hil/admin_cli.py \
  /usr/local/lib/alteriom-hil/farm_node.py
# The gateway probe is painlessMesh's -- the HTTP target its gateway suite
# talks to -- and lives beside that suite; installed under the name its unit
# runs (setup-gateway-network.sh), refreshed by every deploy like the rest.
sudo install -m 0755 "$HERE/../suites/painlessmesh/gateway_probe_server.py" /usr/local/lib/alteriom-hil/gateway_probe_server.py
# The installer of a release a node staged (alteriom-hil-update.service).
sudo install -m 0755 "$HERE/node-update.sh" /usr/local/lib/alteriom-hil/node-update.sh
# What the portal asks a node for that needs sudo: restart, logs, settings.
sudo install -m 0755 "$HERE/node-control.sh" /usr/local/lib/alteriom-hil/node-control.sh
sudo install -m 0644 "$HERE/hil-config.schema.json" /usr/local/lib/alteriom-hil/hil-config.schema.json
# The rig's dashboard, which is the rig's own bundle now (rig/web). The
# portal's site pages are not installed here: a rig serves its dashboard at
# `/` and the site belongs to the portal (docs/public-release-plan.md, 12d).
RIG_WEB="$HERE/web"
sudo install -d -m 0755 /usr/local/lib/alteriom-hil/web
# `install` does not recurse, and the web root has a brand/ subdirectory
# (marks, icons, artwork, served under /brand and /world/brand); a plain
# `install web/*` says "omitting directory 'web/brand'" and fails the update.
# Copy the files at each level, skipping directories, so the whole tree lands.
for web_asset in "$RIG_WEB"/*; do
  if [ -f "$web_asset" ]; then
    sudo install -m 0644 "$web_asset" /usr/local/lib/alteriom-hil/web/
  fi
done
if [ -d "$RIG_WEB/brand" ]; then
  sudo install -d -m 0755 /usr/local/lib/alteriom-hil/web/brand
  for brand_asset in "$RIG_WEB"/brand/*; do
    if [ -f "$brand_asset" ]; then
      sudo install -m 0644 "$brand_asset" /usr/local/lib/alteriom-hil/web/brand/
    fi
  done
fi
# What an older deploy left: the portal's site pages and its shell, which a
# rig never serves now. A stale copy would still be found at `/`.
sudo rm -f \
  /usr/local/lib/alteriom-hil/web/site-*.html \
  /usr/local/lib/alteriom-hil/web/site.css \
  /usr/local/lib/alteriom-hil/web/site.js \
  /usr/local/lib/alteriom-hil/web/portal-shell.js
# What is actually running, stamped at install time rather than read from the
# clone at runtime: the service executes the copy under /usr/local/lib, and a
# clone that has moved on since the last install would otherwise report a
# version this host is not running. The dashboard shows this.
VERSION_FILE=/usr/local/lib/alteriom-hil/version.json
REPO_DIR="$(cd "$HERE/.." && pwd)"
# MAJOR.MINOR is declared in VERSION and moved by hand; PATCH is the commit
# count on the deployed branch, so it rises by itself on every build and two
# builds are ordered by comparing three integers. The commit is kept as
# provenance, not as the version: an operator cannot tell whether 2d39e5e is
# newer than a173e4e, and that is the whole job of a version number.
VERSION_BASE="$(tr -d '[:space:]' < "$REPO_DIR/VERSION" 2>/dev/null || echo 0.0)"
VERSION_BUILD="$(git -C "$REPO_DIR" rev-list --count HEAD 2>/dev/null || echo 0)"
VERSION_NUMBER="$VERSION_BASE.$VERSION_BUILD"
VERSION_COMMIT="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
VERSION_SHORT="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
# A modified working tree is not the version it claims to be; say so.
git -C "$REPO_DIR" diff --quiet HEAD 2>/dev/null || VERSION_NUMBER="$VERSION_NUMBER+modified"
VERSION_DATE="$(git -C "$REPO_DIR" log -1 --format=%cI 2>/dev/null || echo unknown)"
VERSION_SUBJECT="$(git -C "$REPO_DIR" log -1 --format=%s 2>/dev/null | tr -d '"' | cut -c1-120 || echo unknown)"
sudo tee "$VERSION_FILE" >/dev/null <<EOF
{
  "version": "$VERSION_NUMBER",
  "base": "$VERSION_BASE",
  "build": $VERSION_BUILD,
  "commit": "$VERSION_COMMIT",
  "short": "$VERSION_SHORT",
  "committed_at": "$VERSION_DATE",
  "subject": "$VERSION_SUBJECT",
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
sudo chmod 0644 "$VERSION_FILE"
sudo install -d -m 2750 -o root -g "$RUN_USER" /etc/alteriom-hil
sudo install -d -m 0750 -o "$RUN_USER" -g "$RUN_USER" /var/lib/alteriom-hil
# /run/lock is a tmpfs: a file made here is gone at the next reboot. The
# tmpfiles rule is what brings it back on every boot; the three commands under
# it are for the boot this installer is running on.
sudo tee /etc/tmpfiles.d/alteriom-hil.conf >/dev/null <<EOF
f /run/lock/alteriom-hil.lock 0660 $RUN_USER $RUN_USER -
EOF
sudo systemd-tmpfiles --create /etc/tmpfiles.d/alteriom-hil.conf >/dev/null 2>&1 || true
sudo touch /run/lock/alteriom-hil.lock
sudo chown "$RUN_USER":"$RUN_USER" /run/lock/alteriom-hil.lock
sudo chmod 0660 /run/lock/alteriom-hil.lock

CONFIG=/etc/alteriom-hil/config.yaml
RUNTIME_ENV=/etc/alteriom-hil/runtime.env
if [ ! -f "$CONFIG" ]; then
  INVENTORY=/var/lib/alteriom-hil/inventory.yaml
  BOARD_MAP=/var/lib/alteriom-hil/board-map.active.yaml
  if [ -f /etc/alteriom-hil/hil.env ]; then
    LEGACY_MAP="$(sed -n 's/^ALTERIOM_HIL_BOARD_MAP=//p' /etc/alteriom-hil/hil.env | tail -1)"
    if [ -n "$LEGACY_MAP" ] && [ -f "$LEGACY_MAP" ]; then
      sudo install -o "$RUN_USER" -g "$RUN_USER" -m 0640 "$LEGACY_MAP" "$INVENTORY"
    fi
  fi
  sudo tee "$CONFIG" >/dev/null <<EOF
schema: 2
mode: hardware
runner:
  unit: $RUNNER_UNIT
paths:
  venv: $VENV
  inventory: $INVENTORY
  board_map: $BOARD_MAP
  repo: $(cd "$HERE/.." && pwd)
  state: /var/lib/alteriom-hil
health:
  interval_minutes: 5
  minimum_boards: 2
  disk_warn_percent: 85
  disk_critical_percent: 95
service:
  enabled: true
  bind: 127.0.0.1
  port: 8090
  token_file: /etc/alteriom-hil/api-token
EOF
fi
if sudo grep -q '^schema: 1$' "$CONFIG"; then
  sudo sed -i 's/^schema: 1$/schema: 2/' "$CONFIG"
  sudo sed -i '/^  interval_minutes:/a\  minimum_boards: 2' "$CONFIG"
fi
# The original deployment stored the mutable registry below ProtectHome. Move
# that exact managed default into the service state directory, keeping the old
# copy as a rollback aid. Custom operator paths are left untouched.
LEGACY_INVENTORY="$RUN_HOME/inventory.yaml"
MANAGED_INVENTORY=/var/lib/alteriom-hil/inventory.yaml
CONFIGURED_INVENTORY="$(sudo awk '/^[[:space:]]+inventory:/ {print $2; exit}' "$CONFIG")"
if [ "$CONFIGURED_INVENTORY" = "$LEGACY_INVENTORY" ]; then
  if [ -f "$LEGACY_INVENTORY" ] && [ ! -f "$MANAGED_INVENTORY" ]; then
    sudo install -o "$RUN_USER" -g "$RUN_USER" -m 0640 "$LEGACY_INVENTORY" "$MANAGED_INVENTORY"
  fi
  sudo sed -i "s|^\([[:space:]]*inventory:[[:space:]]*\)$LEGACY_INVENTORY\([[:space:]]*\)$|\1$MANAGED_INVENTORY\2|" "$CONFIG"
fi
sudo chown root:"$RUN_USER" "$CONFIG"
sudo chmod 0640 "$CONFIG"
TOKEN_FILE=/etc/alteriom-hil/api-token
if [ ! -s "$TOKEN_FILE" ]; then
  umask 027
  openssl rand -hex 32 | sudo tee "$TOKEN_FILE" >/dev/null
fi
sudo chown root:"$RUN_USER" "$TOKEN_FILE"
sudo chmod 0640 "$TOKEN_FILE"
if [ -d "$RUNNER_HOME" ]; then
  sudo -u "$RUN_USER" ln -sfn "$RUNTIME_ENV" "$RUNNER_HOME/.env"
fi
sudo tee /etc/systemd/system/alteriom-hil-health.service >/dev/null <<EOF
[Unit]
Description=Alteriom HIL health snapshot
After=network-online.target $RUNNER_UNIT

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_USER
EnvironmentFile=$RUNTIME_ENV
ExecStart=$VENV/bin/alteriom-hil-health --output /var/lib/alteriom-hil/status.json --fail-unhealthy
EOF

sudo tee /etc/systemd/system/alteriom-hil-health.timer >/dev/null <<'EOF'
[Unit]
Description=Refresh Alteriom HIL health every five minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true
Unit=alteriom-hil-health.service

[Install]
WantedBy=timers.target
EOF

# Nightly backup (alteriom_hil.backup): the job database, the registries, the
# configuration and the pinned bundles, never a secret. As the runner user,
# which owns the state and can read the configuration, and whose ssh key
# reaches backup.target when one is set.
sudo tee /etc/systemd/system/alteriom-hil-backup.service >/dev/null <<EOF
[Unit]
Description=Alteriom HIL nightly backup
After=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_USER
EnvironmentFile=$RUNTIME_ENV
ExecStart=$VENV/bin/alteriom-hil-admin backup create
EOF

sudo tee /etc/systemd/system/alteriom-hil-backup.timer >/dev/null <<'EOF'
[Unit]
Description=Back the Alteriom HIL farm up every night

[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=15min
Persistent=true
Unit=alteriom-hil-backup.service

[Install]
WantedBy=timers.target
EOF

sudo tee /etc/systemd/system/alteriom-hil-farm.service >/dev/null <<EOF
[Unit]
Description=Alteriom ESP32 farm API and dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$(cd "$HERE/.." && pwd)
EnvironmentFile=$RUNTIME_ENV
ExecStart=$VENV/bin/alteriom-hil-service --web-root /usr/local/lib/alteriom-hil/web
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/alteriom-hil /run/lock $(cd "$HERE/.." && pwd) $HIL_HOME

[Install]
WantedBy=multi-user.target
EOF

# The node agent of an attached host (farm.mode: attached): the farm service
# in node mode beside the standalone one, taking runs from the portal. Its own
# state directory and loopback port, so its job store and local page never mix
# with the main service's; the same rig and board locks, so a run from either
# waits for the other's. Arguments rather than Environment=, which the
# runtime file would override.
sudo tee /etc/systemd/system/alteriom-hil-node.service >/dev/null <<EOF
[Unit]
Description=Alteriom ESP32 farm node agent (attached to a portal)
After=network-online.target alteriom-hil-farm.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$(cd "$HERE/.." && pwd)
EnvironmentFile=$RUNTIME_ENV
ExecStart=$VENV/bin/alteriom-hil-service --mode node --state /var/lib/alteriom-hil/node --port 8091 --web-root /usr/local/lib/alteriom-hil/web
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/alteriom-hil /run/lock $(cd "$HERE/.." && pwd) $HIL_HOME

[Install]
WantedBy=multi-user.target
EOF

# A node installs the release its portal names (alteriom_hil.farm_node stages
# it, rig/node-update.sh installs it): the agent leaves request.json in the
# update directory, and this path unit starts the installer for it. A unit of
# its own, not the farm service's: the install restarts the farm service, and
# needs the sudo the farm service's sandbox (NoNewPrivileges) rules out.
sudo install -d -m 0750 -o "$RUN_USER" -g "$RUN_USER" /var/lib/alteriom-hil/update
sudo tee /etc/systemd/system/alteriom-hil-update.service >/dev/null <<EOF
[Unit]
Description=Alteriom HIL rig: install a staged release of the rig software
After=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_USER
EnvironmentFile=$RUNTIME_ENV
ExecStart=/usr/local/lib/alteriom-hil/node-update.sh
TimeoutStartSec=3600
EOF
sudo tee /etc/systemd/system/alteriom-hil-update.path >/dev/null <<'EOF'
[Unit]
Description=Install a rig release when one is staged

[Path]
PathExists=/var/lib/alteriom-hil/update/request.json
Unit=alteriom-hil-update.service

[Install]
WantedBy=paths.target
EOF

# The portal's other requests that need sudo (rig/node-control.sh): the
# agent leaves control.json, and this unit carries it out. Its own unit for
# the same reasons as the update: it restarts the farm service and needs sudo.
sudo tee /etc/systemd/system/alteriom-hil-control.service >/dev/null <<EOF
[Unit]
Description=Alteriom ESP32 farm node: restart, logs or settings the portal asked for
After=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_USER
EnvironmentFile=$RUNTIME_ENV
ExecStart=/usr/local/lib/alteriom-hil/node-control.sh
TimeoutStartSec=300
EOF
sudo tee /etc/systemd/system/alteriom-hil-control.path >/dev/null <<'EOF'
[Unit]
Description=Carry out a request the node agent staged for its host

[Path]
PathExists=/var/lib/alteriom-hil/update/control.json
Unit=alteriom-hil-control.service

[Install]
WantedBy=paths.target
EOF

if [ -n "$RUNNER_UNIT" ]; then
sudo install -d -m 0755 "/etc/systemd/system/$RUNNER_UNIT.d"
sudo tee "/etc/systemd/system/$RUNNER_UNIT.d/zz-hil-environment.conf" >/dev/null <<EOF
[Service]
EnvironmentFile=$RUNTIME_ENV
EOF
fi
LEGACY_OVERRIDE="/etc/systemd/system/$RUNNER_UNIT.d/override.conf"
if [ -n "$RUNNER_UNIT" ] && [ -f "$LEGACY_OVERRIDE" ]; then
  LEGACY_CONTENT="$(sudo grep -Ev '^[[:space:]]*$' "$LEGACY_OVERRIDE")"
  MANAGED_CONTENT="$(printf '[Service]\nEnvironmentFile=%s' "$HIL_HOME/runner.env")"
  MIGRATED_CONTENT="$(printf '[Service]\nEnvironmentFile=%s' "$RUNTIME_ENV")"
  if [ "$LEGACY_CONTENT" = "$MANAGED_CONTENT" ] || [ "$LEGACY_CONTENT" = "$MIGRATED_CONTENT" ]; then
    sudo rm -f -- "$LEGACY_OVERRIDE"
  else
    sudo sed -i "s|EnvironmentFile=$HIL_HOME/runner.env|EnvironmentFile=$RUNTIME_ENV|" "$LEGACY_OVERRIDE"
  fi
fi

sudo tee /usr/local/bin/alteriom-hil-admin >/dev/null <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec $VENV/bin/alteriom-hil-admin "\$@"
EOF
sudo chmod 0755 /usr/local/bin/alteriom-hil-admin
sudo tee /usr/local/bin/alteriom-hil-status >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
exec alteriom-hil-admin status --live "$@"
EOF
sudo chmod 0755 /usr/local/bin/alteriom-hil-status

sudo systemctl daemon-reload
# A host joining a portal (rig/join-rig.sh) becomes its node before the
# configuration is applied, so it starts as one.
if [ -n "${HIL_JOIN_PORTAL_URL:-}" ] && [ -n "${HIL_JOIN_WORKER_NAME:-}" ]; then
  sudo /usr/local/bin/alteriom-hil-admin config join --portal "$HIL_JOIN_PORTAL_URL" --name "$HIL_JOIN_WORKER_NAME"
fi
sudo /usr/local/bin/alteriom-hil-admin config apply --no-restart
# The key a CallMeBot link is sealed to on the portal's page for this rig
# (docs/providers.md): only on a rig a portal manages -- a node, or attached --
# and made once. An existing key is never replaced here; an owner rotates it
# with `sudo alteriom-hil-admin providers seal-key --rotate`. Made before the
# farm service restarts below, so its hello reports the public key.
if sudo grep -qx -e 'ALTERIOM_HIL_FARM_ATTACHED=1' -e 'ALTERIOM_HIL_FARM_MODE=node' "$RUNTIME_ENV"; then
  SEAL_KEY=/etc/alteriom-hil/provider-seal.key
  SEAL_PUB=/etc/alteriom-hil/provider-seal.pub
  if ! sudo test -f "$SEAL_KEY"; then
    # Private from the moment it exists: umask 077 before openssl writes it.
    sudo sh -c "umask 077 && openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out '$SEAL_KEY.new' && mv -f '$SEAL_KEY.new' '$SEAL_KEY'"
    sudo rm -f "$SEAL_PUB"
  fi
  sudo chown root:root "$SEAL_KEY"
  sudo chmod 0600 "$SEAL_KEY"
  if ! sudo test -s "$SEAL_PUB"; then
    sudo openssl pkey -in "$SEAL_KEY" -pubout | sudo tee "$SEAL_PUB" >/dev/null
  fi
  sudo chown root:root "$SEAL_PUB"
  sudo chmod 0644 "$SEAL_PUB"
  echo "Provider seal key fingerprint: $(sudo /usr/local/bin/alteriom-hil-admin providers seal-key --fingerprint)"
fi
sudo systemctl enable --now alteriom-hil-health.timer
sudo systemctl enable --now alteriom-hil-backup.timer
sudo systemctl enable alteriom-hil-farm.service
sudo systemctl restart alteriom-hil-farm.service
# Attached to a portal (config apply writes this line from farm.mode), the
# node agent runs beside the service; otherwise it does not run at all.
if sudo grep -qx 'ALTERIOM_HIL_FARM_ATTACHED=1' "$RUNTIME_ENV"; then
  sudo systemctl enable alteriom-hil-node.service
  sudo systemctl restart alteriom-hil-node.service
else
  sudo systemctl disable --now alteriom-hil-node.service 2>/dev/null || true
fi
# Every rig installs the releases its owner asks for -- from its page, or
# on its own when automatic installs are on -- through the update unit; the
# service stages a release and this unit, which may restart the service and
# use sudo, installs it. The control unit carries out a portal's requests
# and is a node's alone.
sudo systemctl enable --now alteriom-hil-update.path
if sudo grep -qx 'ALTERIOM_HIL_FARM_MODE=node' "$RUNTIME_ENV"; then
  sudo systemctl enable --now alteriom-hil-control.path
else
  sudo systemctl disable --now alteriom-hil-control.path 2>/dev/null || true
fi
# The gateway probe runs from the same snapshot directory, but until now only
# setup-gateway-network.sh — a one-time bring-up script — ever wrote it. A probe
# route added to the repo therefore never reached the rig, and the suite row that
# needs it failed for a reason that is not the library under test. Only a host
# that has the gateway test network has the unit.
if [ -f /etc/systemd/system/alteriom-hil-gateway-probe.service ]; then
  sudo systemctl restart alteriom-hil-gateway-probe.service
fi
# The launcher an older deploy copied here, removed last: the units that ran
# it have been rewritten and reloaded above, so nothing points at it any
# more. Removing it earlier would leave an installer that died in the middle
# with a unit naming a file that is already gone.
sudo rm -f /usr/local/lib/alteriom-hil/farm_service.py
# update-runner.sh --from-ci runs inside a job executed by this very unit;
# restarting it would kill that job. The runner picks up environment
# changes on its next restart (config apply, reboot, or a manual restart).
if [ -z "$RUNNER_UNIT" ]; then
  :
elif [ "${HIL_SKIP_RUNNER_RESTART:-0}" = "1" ]; then
  echo "Runner unit $RUNNER_UNIT left running (HIL_SKIP_RUNNER_RESTART=1)."
else
  sudo systemctl restart "$RUNNER_UNIT"
fi
# The farm service was just restarted and takes a moment to listen; the
# snapshot's farm_endpoint check would otherwise report "connection refused"
# on every deploy and call the rig unhealthy for it. Any HTTP answer will do
# — 401 without a token still proves the API is up.
for _ in $(seq 1 30); do
  curl -s -o /dev/null http://127.0.0.1:8090/api/v1/status && break
  sleep 1
done
# The snapshot service exits non-zero when the rig is unhealthy; that is a
# report, not an installation failure (admin_cli's config apply treats it
# the same way). update-runner.sh prints the full report afterwards.
if ! sudo systemctl start alteriom-hil-health.service; then
  echo "Health snapshot reports the rig unhealthy; see: alteriom-hil-admin status" >&2
fi
echo "HIL administration installed. Run: alteriom-hil-admin --help"
echo "Dashboard: ssh -L 8090:127.0.0.1:8090 $RUN_USER@<runner>, then open http://127.0.0.1:8090"
echo "Dashboard token: sudo cat $TOKEN_FILE"
