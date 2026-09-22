#!/usr/bin/env bash
# Conservative, idempotent baseline for the dedicated Raspberry Pi HIL host.
# Run only after key-based SSH login has been verified.

set -euo pipefail

LAN_CIDR="${HIL_LAN_CIDR:-192.168.1.0/24}"
SSH_USER="${HIL_SSH_USER:-${SUDO_USER:-$USER}}"
AUTHORIZED_KEYS="$(getent passwd "$SSH_USER" | cut -d: -f6)/.ssh/authorized_keys"

# Hardening turns password logins off. With no key installed, this host is
# unreachable the moment the current session ends -- and a rig is usually the
# machine nobody has a screen and keyboard for. So this refuses, and says what
# to do rather than only what is wrong: installing the key is one command from
# the workstation that will administer the rig, and it is a step the operator
# has to take anyway, not a setting to relax here.
if [ ! -s "$AUTHORIZED_KEYS" ]; then
  HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
  HOST="${HOST:-$(hostname)}"
  cat >&2 <<EOF
Refusing to disable password SSH: $AUTHORIZED_KEYS is missing or empty.

Nothing could log in afterwards. Install your key first, from the workstation
you will administer this rig from -- not from this host:

    ssh-keygen -t ed25519                     # only if you have no key yet
    ssh-copy-id $SSH_USER@$HOST
    ssh -o PasswordAuthentication=no $SSH_USER@$HOST true

Windows has no ssh-copy-id; from PowerShell, send the key over the one login
the password still buys you:

    type \$env:USERPROFILE\.ssh\id_ed25519.pub | ssh $SSH_USER@$HOST \\
      "mkdir -p ~/.ssh && chmod 700 ~/.ssh && tr -d '\\r' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"

The last command must succeed: it proves the key works while the password
still would. Then run this script again, and keep this session open until a
second key-only login succeeds.
EOF
  exit 1
fi

sudo apt-get update -qq
sudo apt-get install -y -qq ufw unattended-upgrades apt-listchanges

sudo install -d -m 0755 /etc/ssh/sshd_config.d
# OpenSSH uses the first value it encounters, so this sorts before image and
# cloud-init drop-ins that may otherwise retain password authentication.
sudo tee /etc/ssh/sshd_config.d/00-esp32-hil-hardening.conf >/dev/null <<EOF
PermitRootLogin no
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
X11Forwarding no
AllowUsers $SSH_USER
MaxAuthTries 3
ClientAliveInterval 300
ClientAliveCountMax 2
EOF
sudo sshd -t

sudo ufw --force reset >/dev/null
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from "$LAN_CIDR" to any port 22 proto tcp comment 'HIL SSH from LAN'
sudo ufw --force enable

sudo systemctl set-default multi-user.target
sudo systemctl disable --now lightdm.service wayvnc-control.service \
  cups.service cups.socket cups.path avahi-daemon.service avahi-daemon.socket \
  bluetooth.service rpcbind.service rpcbind.socket 2>/dev/null || true
sudo systemctl enable --now ssh.service apt-daily-upgrade.timer
sudo systemctl reload ssh.service

echo "Pi hardening applied. Keep this session open until a new key-only SSH login succeeds."
