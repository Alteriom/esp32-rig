#!/usr/bin/env bash
# Install the release a node staged: run by alteriom-hil-update.service when
# alteriom-hil-update.path sees <update dir>/request.json.
#
# The node agent (alteriom_hil.farm_node) writes that request once it is idle and
# has downloaded the portal's current release -- a git bundle of this
# repository -- and checked it against the digest the portal gave. This
# installs it the way every deploy has: update-runner.sh, the one from the
# release itself, fetching from the bundle instead of from GitHub, under the
# rig lock, then verifying the host. What happened is left in status.json,
# which the agent reports to the portal; the whole output in update.log.
#
# Runs as the runner user (non-interactive sudo, like update-runner.sh from a
# job). The update service is not the farm service, so restarting the farm
# service -- the node -- does not stop this.

set -euo pipefail

main() {
  local dir="${ALTERIOM_HIL_UPDATE_DIR:-/var/lib/alteriom-hil/update}"
  local request="$dir/request.json" taken="$dir/request.taken.json"
  local status_file="$dir/status.json" log="$dir/update.log"
  # PathExists= starts this while the request exists: take it first, so a
  # failure below cannot start it again in a loop.
  [ -f "$request" ] || exit 0
  mv -f "$request" "$taken"

  local commit sha256 bundle
  commit="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("commit", ""))' "$taken")"
  sha256="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("sha256", ""))' "$taken")"
  bundle="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("bundle", ""))' "$taken")"

  status() {
    python3 - "$status_file" "$1" "$commit" "$2" <<'PY'
import json, sys
from datetime import datetime, timezone
path, state, commit, detail = sys.argv[1:5]
record = {"state": state, "commit": commit or None, "detail": detail[-4000:] or None,
          "at": datetime.now(timezone.utc).isoformat()}
with open(path + ".tmp", "w", encoding="utf-8") as handle:
    json.dump(record, handle)
import os
os.replace(path + ".tmp", path)
PY
  }
  fail() {
    status failed "$1"
    echo "node-update: $1" >&2
    exit 1
  }

  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail "the request names no commit"
  # Only a bundle the agent staged, where it stages them.
  [ "$bundle" = "$dir/$commit.bundle" ] || fail "the request names a bundle outside $dir"
  [ -f "$bundle" ] || fail "the staged bundle $bundle is gone"
  printf '%s  %s\n' "$sha256" "$bundle" | sha256sum --check --status \
    || fail "the staged bundle does not match the digest the portal gave"

  local runtime_env=/etc/alteriom-hil/runtime.env repo
  repo="${ALTERIOM_HIL_REPO:-$(sed -n 's/^ALTERIOM_HIL_REPO=//p' "$runtime_env" 2>/dev/null | tail -n 1)}"
  [ -n "$repo" ] && [ -d "$repo/.git" ] || fail "no clone at ALTERIOM_HIL_REPO=${repo:-(unset)}"
  git -C "$repo" bundle verify "$bundle" > /dev/null 2>&1 || fail "git does not accept $bundle as a bundle"

  status installing "installing ${commit:0:12}"
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
  # The release's own update script: what it installs is what it knows how
  # to install.
  git -C "$repo" fetch --quiet "$bundle" HEAD
  [ "$(git -C "$repo" rev-parse FETCH_HEAD)" = "$commit" ] || fail "the bundle's HEAD is not $commit"
  # The release's own scripts. `runner` alone was enough when every installer
  # lived there; the rig's are under `rig/` now. The whole tree comes out
  # rather than a list of directories, so no further move can leave this
  # copy -- which a node in the field keeps until it installs the release
  # that replaces it -- naming a path the release no longer has
  # (docs/public-release-plan.md, step 12f).
  git -C "$repo" archive FETCH_HEAD | tar -x -C "$WORK"

  if HIL_DEPLOY_SOURCE="$bundle" "$WORK/runner/update-runner.sh" --unattended --ref "$commit" > "$log" 2>&1; then
    status installed "$(tail -n 1 "$log")"
    rm -f "$taken"
    # The one just installed is in the clone now; older staged ones are litter.
    find "$dir" -maxdepth 1 -name '*.bundle' -delete
  else
    fail "update-runner.sh failed: $(tail -n 40 "$log")"
  fi
}

main "$@"
