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

  local commit sha256 bundle release_dir version
  commit="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("commit", ""))' "$taken")"
  sha256="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("sha256", ""))' "$taken")"
  bundle="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("bundle", ""))' "$taken")"
  release_dir="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("release_dir", ""))' "$taken")"
  version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version", ""))' "$taken")"

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

  local runtime_env=/etc/alteriom-hil/runtime.env repo
  repo="${ALTERIOM_HIL_REPO:-$(sed -n 's/^ALTERIOM_HIL_REPO=//p' "$runtime_env" 2>/dev/null | tail -n 1)}"

  # A wheel release the rig staged itself (alteriom_hil.updates): its
  # document names every file, `alteriom-hil-admin upgrade` checks each
  # against it and installs nothing if one disagrees. The dashboard goes to
  # the web root as root; the checkout moves to the release's tag when it
  # has it, and the installer of that tag stamps the version and restarts
  # the service. A clone without the tag (a farm's own) gets a restart.
  if [ -n "$release_dir" ]; then
    case "$release_dir" in "$dir"/release-*) ;; *) fail "the request names a release outside $dir" ;; esac
    [ -f "$release_dir/release.json" ] || fail "the staged release $release_dir holds no release.json"
    local venv
    venv="${ALTERIOM_HIL_VENV:-$(sed -n 's/^ALTERIOM_HIL_VENV=//p' "$runtime_env" 2>/dev/null | tail -n 1)}"
    [ -x "$venv/bin/alteriom-hil-admin" ] || fail "no alteriom-hil-admin under ALTERIOM_HIL_VENV=${venv:-(unset)}"
    [ -n "$version" ] || version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version", ""))' "$release_dir/release.json")"
    status installing "installing $version"
    local web=${ALTERIOM_HIL_WEB_ROOT:-/usr/local/lib/alteriom-hil/web} dashboard
    dashboard="$(ls "$release_dir"/alteriom-hil-dashboard-*.tar.gz 2>/dev/null | head -n 1)"
    if {
      "$venv/bin/alteriom-hil-admin" upgrade --from "$release_dir" \
      && { [ -z "$dashboard" ] || { sudo -n install -d -m 0755 "$web" && sudo -n tar -xzf "$dashboard" --strip-components=1 -C "$web" && sudo -n chown -R root:root "$web"; }; } \
      && if [ -n "$repo" ] && [ -d "$repo/.git" ] \
            && { git -C "$repo" rev-parse -q --verify "refs/tags/v$version" >/dev/null 2>&1 \
                 || { git -C "$repo" fetch -q --tags origin >/dev/null 2>&1 && git -C "$repo" rev-parse -q --verify "refs/tags/v$version" >/dev/null 2>&1; }; }; then
           git -C "$repo" -c advice.detachedHead=false checkout -q "v$version" && "$repo/rig/install-health-service.sh"
         else
           sudo -n python3 - "$version" "$commit" <<'PY'
import json, sys, time
version, commit = sys.argv[1], sys.argv[2] or None
record = {"version": version, "short": version, "commit": commit,
          "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
with open("/usr/local/lib/alteriom-hil/version.json", "w", encoding="utf-8") as handle:
    json.dump(record, handle)
PY
           sudo -n systemctl restart alteriom-hil-farm.service
         fi
    } > "$log" 2>&1; then
      status installed "installed $version"
      rm -f "$taken"
      find "$dir" -maxdepth 1 -type d -name 'release-*' ! -path "$release_dir" -exec rm -rf {} +
      exit 0
    fi
    fail "the release install failed: $(tail -n 40 "$log")"
  fi

  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail "the request names no commit"
  # Only a bundle the agent staged, where it stages them.
  [ "$bundle" = "$dir/$commit.bundle" ] || fail "the request names a bundle outside $dir"
  [ -f "$bundle" ] || fail "the staged bundle $bundle is gone"
  printf '%s  %s\n' "$sha256" "$bundle" | sha256sum --check --status \
    || fail "the staged bundle does not match the digest the portal gave"

  # The clone, read from runtime.env above.
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

  # The release's own update script. `runner/update-runner.sh` is a hand-over
  # to this one for now, and a node whose node-update.sh predates the move
  # runs that; this runs the real one directly (step 12g).
  update="$WORK/rig/update-runner.sh"
  [ -x "$update" ] || update="$WORK/runner/update-runner.sh"
  if HIL_DEPLOY_SOURCE="$bundle" "$update" --unattended --ref "$commit" > "$log" 2>&1; then
    status installed "$(tail -n 1 "$log")"
    rm -f "$taken"
    # The one just installed is in the clone now; older staged ones are litter.
    find "$dir" -maxdepth 1 -name '*.bundle' -delete
  else
    fail "update-runner.sh failed: $(tail -n 40 "$log")"
  fi
}

main "$@"
