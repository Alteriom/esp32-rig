#!/usr/bin/env bash
# Join this host to a farm portal as a rig.
#
# The portal's Rigs page (Add rig) gives the whole command, with a token that
# works once, for an hour:
#
#   curl -fsSL https://<your-portal>/api/v1/join.sh | bash -s -- \
#     --portal https://<your-portal> --token afj_...
#
# Run it as the user the rig runs as -- sudo-capable, not root -- on Raspberry
# Pi OS or Debian. It:
#   1. installs what a rig needs from apt;
#   2. trades the token for the rig's node key, kept in /etc/alteriom-hil/node-key
#      (root and this user's group only);
#   3. downloads the release the portal's rigs run -- a git bundle, checked
#      against the digest the portal gives -- and clones it;
#   4. sets the host up as every rig is (setup-runner.sh) and installs it as a
#      node of the portal (update-runner.sh, farm.mode: node).
# The rig then says hello, and shows on the portal's Rigs page; its boards are
# plugged in, discovered and registered from there.
#
# Stopped halfway, it is re-run the same way: the key it was given is kept, so
# the spent token is not needed again. To join as another rig, remove
# /etc/alteriom-hil/node-key and /etc/alteriom-hil/join.env first.
#
#   --repo DIR   where the farm's clone lives (default ~/esp32-farm-src)

set -euo pipefail

ETC=/etc/alteriom-hil
KEY_FILE="$ETC/node-key"
JOIN_ENV="$ETC/join.env"
ORIGIN_URL=https://github.com/Alteriom/alteriom-esp32-farm

say() { printf '\n==> %s\n' "$1"; }
die() { printf '\njoin-rig: %s\n' "$1" >&2; exit 1; }

# One field of a JSON file (a.b for nested), or nothing.
json_field() {
  python3 - "$1" "$2" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for part in sys.argv[2].split("."):
    value = value.get(part) if isinstance(value, dict) else None
print("" if value is None else value)
PY
}

# POST /api/v1/enroll: the token for the rig's name and node key, the answer
# written to $3. The token goes on curl's stdin, never on a command line
# another user could read in ps.
redeem() {
  local portal="$1" token="$2" out="$3" status
  status="$(printf '{"token": "%s", "hostname": "%s"}' "$token" "$(hostname)" \
    | curl -sS --retry 3 --retry-connrefused -o "$out" -w '%{http_code}' -X POST \
        -H 'Content-Type: application/json' --data-binary @- "$portal/api/v1/enroll")" \
    || die "cannot reach $portal"
  if [ "$status" != 200 ]; then
    die "the portal refused to add this rig ($status): $(json_field "$out" error 2>/dev/null || true)"
  fi
}

# An authenticated GET with the key read from $2: given to curl on stdin as
# its configuration, so it is never an argument either. The body goes to $4.
fetch_as_node() {
  local portal="$1" key_file="$2" route="$3" out="$4" key
  key="$(cat "$key_file")"
  printf 'header = "Authorization: Bearer %s"\n' "$key" \
    | curl -sS --retry 3 --retry-connrefused --config - -o "$out" -w '%{http_code}' "$portal$route"
}

# The portal's current release into $3 (a git bundle), checked against its
# digest; prints the commit.
fetch_release() {
  local portal="$1" key_file="$2" dir="$3" status commit sha256
  status="$(fetch_as_node "$portal" "$key_file" /api/v1/releases/current "$dir/release.json")" \
    || die "cannot reach $portal"
  [ "$status" = 200 ] || die "the portal did not name a release ($status): $(json_field "$dir/release.json" error 2>/dev/null || true)"
  commit="$(json_field "$dir/release.json" commit)"
  sha256="$(json_field "$dir/release.json" sha256)"
  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] && [[ "$sha256" =~ ^[0-9a-f]{64}$ ]] || die "the portal's release answer is not one"
  status="$(fetch_as_node "$portal" "$key_file" "/api/v1/releases/$commit/bundle" "$dir/$commit.bundle")" \
    || die "cannot download the release from $portal"
  [ "$status" = 200 ] || die "the portal would not give the release ($status)"
  printf '%s  %s\n' "$sha256" "$dir/$commit.bundle" | sha256sum --check --status \
    || die "the release downloaded does not match the digest the portal gave"
  printf '%s\n' "$commit"
}

main() {
  local portal="" token="" repo="$HOME/esp32-farm-src"
  while [ $# -gt 0 ]; do
    case "$1" in
      --portal) portal="${2:-}"; shift 2 ;;
      --token) token="${2:-}"; shift 2 ;;
      --repo) repo="${2:-}"; shift 2 ;;
      -h|--help) echo "usage: join-rig.sh --portal https://<portal> --token afj_... [--repo DIR]  (see rig/join-rig.sh)"; exit 0 ;;
      *) die "unknown argument: $1" ;;
    esac
  done

  [ "$(id -u)" != 0 ] || die "run this as the user the rig runs as (it uses sudo), not as root"
  command -v apt-get >/dev/null || die "a rig is a Debian host (Raspberry Pi OS, Debian, Ubuntu)"
  portal="${portal%/}"
  local user group name=""
  user="$(id -un)"
  group="$(id -gn)"

  say "sudo, for installing (it may ask for $user's password)"
  sudo -v

  say "installing what a rig needs"
  sudo apt-get update -qq < /dev/null
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv git uhubctl openssl curl ca-certificates iw < /dev/null
  sudo install -d -m 2750 -o root -g "$group" "$ETC"

  if sudo test -s "$KEY_FILE" && sudo test -s "$JOIN_ENV"; then
    # A join that stopped halfway: the same rig, the same key.
    portal="$(sudo sed -n 's/^PORTAL=//p' "$JOIN_ENV" | tail -n 1)"
    name="$(sudo sed -n 's/^NAME=//p' "$JOIN_ENV" | tail -n 1)"
    say "carrying on joining $portal as $name (the key from the first run is kept)"
  else
    [[ "$portal" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] || die "--portal must be the portal's https:// URL"
    [[ "$token" =~ ^afj_[0-9a-f]{48}$ ]] || die "--token must be the join token the portal's Add rig gave"
    sudo test ! -e "$KEY_FILE" || die "$KEY_FILE exists: this host already has a node key; remove it to join again"
  fi
  [[ "$portal" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] || die "the portal URL ($portal) is not an https:// URL"

  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT

  if [ -z "$name" ]; then
    say "joining $portal"
    redeem "$portal" "$token" "$WORK/enroll.json"
    name="$(json_field "$WORK/enroll.json" name)"
    [[ "$name" =~ ^[a-z0-9][a-z0-9._-]{0,31}$ ]] || die "the portal's answer names no rig"
    json_field "$WORK/enroll.json" key | sudo install -m 0640 -o root -g "$group" /dev/stdin "$KEY_FILE"
    rm -f "$WORK/enroll.json"
    printf 'PORTAL=%s\nNAME=%s\n' "$portal" "$name" | sudo install -m 0640 -o root -g "$group" /dev/stdin "$JOIN_ENV"
    echo "   this host is $name; its node key is in $KEY_FILE"
  fi

  say "downloading the release $portal's rigs run"
  local commit bundle
  commit="$(fetch_release "$portal" "$KEY_FILE" "$WORK")"
  bundle="$WORK/$commit.bundle"
  echo "   ${commit:0:12}"

  if [ ! -d "$repo/.git" ]; then
    [ ! -e "$repo" ] || die "$repo exists and is not a git clone; move it aside or pass --repo"
    git -c advice.detachedHead=false clone --quiet "$bundle" "$repo"
    git -C "$repo" remote set-url origin "$ORIGIN_URL"
  fi

  say "setting the host up (python environment, esptool, serial access, udev rules)"
  HIL_JOINING=1 "$repo/rig/setup-runner.sh" < /dev/null

  say "installing $name as a node of $portal"
  ALTERIOM_HIL_REPO="$repo" HIL_DEPLOY_SOURCE="$bundle" \
    HIL_JOIN_PORTAL_URL="$portal" HIL_JOIN_WORKER_NAME="$name" \
    "$repo/runner/update-runner.sh" --skip-verify --ref "$commit" < /dev/null

  sudo rm -f "$JOIN_ENV"
  cat <<EOF

$name has joined $portal.

It says hello within a minute, and shows on the portal's Rigs page: its
release, its health, and every command -- rediscover, update, logs, restart,
settings -- from there. Next:
  1. Plug the boards in (a powered USB hub), then Rediscover on $name's page
     and register each board it finds.
  2. For suites that need the rig's own test network (painlessMesh gateway,
     MQTT): rig/setup-gateway-network.sh and rig/setup-mqtt-broker.sh
     in $repo (docs/bringup-checklist.md).
  3. Lock the host down: rig/harden-pi.sh.
EOF
}

# Sourced by the tests for its functions; run, it joins.
if [ "${JOIN_RIG_LIB:-0}" != 1 ]; then
  main "$@"
fi
