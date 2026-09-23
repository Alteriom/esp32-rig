#!/usr/bin/env bash
# The last of the rig's scripts to arrive here. A node in the field installs
# a release by running `$WORK/runner/update-runner.sh`, the path written into
# the node-update.sh it already has, so what is left there is a hand-over to
# this file; that hand-over goes one release after every node has taken a
# node-update.sh that names this path instead
# (docs/public-release-plan.md, step 12g).
# Update the farm host (the Raspberry Pi behind the self-hosted runner) to a
# revision of this repository and redeploy everything that runs from it.
#
#   ./update-runner.sh                  # fast-forward the clone to origin/main
#   ./update-runner.sh --ref v1.2.0     # a branch, tag, or 40-char commit SHA
#   ./update-runner.sh --from-ci        # inside a self-hosted Actions job: never
#                                       # restart the runner unit that is
#                                       # executing this very job
#   ./update-runner.sh --unattended     # nobody to type a sudo password: fail
#                                       # instead of prompting (a node installing
#                                       # a release, rig/node-update.sh)
#   ./update-runner.sh --skip-verify    # no verify-rig / health pass afterwards
#   HIL_DEPLOY_SOURCE=/path/to/checkout ./update-runner.sh --from-ci --ref <sha>
#                                       # deploy from a local git checkout (the
#                                       # Actions job's own) instead of fetching
#                                       # from GitHub: the host needs no credentials
#   HIL_DEPLOY_SOURCE=/path/to/release.bundle ./update-runner.sh --unattended --ref <sha>
#                                       # the same from a git bundle: how a node
#                                       # installs the release its portal names
#
# What "deployed" means on this host, and why a plain `git pull` is not it:
#   1. the clone at paths.repo (/etc/alteriom-hil/config.yaml) — the farm
#      service runs the suites and builds from here;
#   2. the HAL package installed editable into the HIL virtualenv;
#   3. the service snapshots under /usr/local/lib/alteriom-hil
#      (farm_service.py and the rig's dashboard bundle) and the
#      systemd units — only install-health-service.sh refreshes those.
# This script does all three, under the rig lock so a running hardware job
# is never interrupted, then proves the host is still ready.
#
# Run as the runner user (sudo-capable, non-interactive sudo when --from-ci).
# Idempotent; safe to re-run.

set -euo pipefail

REF="main"
FROM_CI=0
UNATTENDED=0
VERIFY=1
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    --ref=*) REF="${1#--ref=}"; shift ;;
    --from-ci) FROM_CI=1; UNATTENDED=1; shift ;;
    --unattended) UNATTENDED=1; shift ;;
    --skip-verify) VERIFY=0; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
say() { printf '\n==> %s\n' "$1"; }
die() { printf 'update-runner: %s\n' "$1" >&2; exit 1; }
# shellcheck source=deploy-lib.sh
. "$HERE/deploy-lib.sh"
RUNTIME_ENV=/etc/alteriom-hil/runtime.env
REPO="${ALTERIOM_HIL_REPO:-$(env_value ALTERIOM_HIL_REPO "$RUNTIME_ENV")}"
REPO="${REPO:-$(cd "$HERE/.." && pwd)}"
VENV="${ALTERIOM_HIL_VENV:-$(env_value ALTERIOM_HIL_VENV "$RUNTIME_ENV")}"
VENV="${VENV:-$HOME/.local/share/alteriom-hil/venv}"
LOCK=/run/lock/alteriom-hil.lock
LOCK_WAIT="${HIL_UPDATE_LOCK_WAIT:-1800}"   # seconds to wait for a running job

ORIGIN_URL="${HIL_REPO_URL:-https://github.com/Alteriom/alteriom-esp32-farm}"
SOURCE="${HIL_DEPLOY_SOURCE:-}"
printf 'repo %s\nvenv %s\n' "$REPO" "$VENV"
[ -n "$SOURCE" ] && printf 'source %s\n' "$SOURCE"
[ -x "$VENV/bin/python" ] || die "HIL virtualenv missing at $VENV; run setup-runner.sh first"
if [ -n "$SOURCE" ] && [ -f "$SOURCE" ]; then
  git bundle list-heads "$SOURCE" >/dev/null 2>&1 || die "HIL_DEPLOY_SOURCE=$SOURCE is not a git bundle"
elif [ -n "$SOURCE" ] && ! git -C "$SOURCE" rev-parse --git-dir >/dev/null 2>&1; then
  die "HIL_DEPLOY_SOURCE=$SOURCE is not a git checkout"
fi
# Prove we can finish before changing anything on the host.
if ! sudo -n true 2>/dev/null; then
  if [ "$UNATTENDED" = 1 ]; then
    die "sudo needs a password for $(id -un), so nothing can be deployed from a job. Run ONCE on the host, \
then re-run this workflow:  echo '$(id -un) ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/alteriom-hil \
&& sudo chmod 0440 /etc/sudoers.d/alteriom-hil   (install-health-service.sh does this on fresh hosts; \
see docs/farm-service.md, Updating the farm host)"
  fi
  say "sudo will prompt for your password"
  sudo -v
fi
sync_clone "$REPO" "$SOURCE" "$REF" "$ORIGIN_URL"
after="$(git -C "$REPO" rev-parse HEAD)"
if [ ! -d "$REPO/rig" ] || [ ! -f "$REPO/rig/pyproject.toml" ]; then
  die "$REPO at $after does not look like alteriom-esp32-farm"
fi

say "waiting for the rig lock (a running hardware job finishes first, up to ${LOCK_WAIT}s)"
[ -e "$LOCK" ] || { sudo touch "$LOCK"; sudo chown "$(id -un)" "$LOCK"; }
exec 9<"$LOCK"
flock -w "$LOCK_WAIT" 9 || die "rig still busy after ${LOCK_WAIT}s; retry later"

say "installing the HAL into $VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -e "$REPO/core[dev]"
"$VENV/bin/python" -m pip install --quiet -e "$REPO/rig[hardware,dev]"
"$VENV/bin/python" -m pip install --quiet -e "$REPO/portal[dev]"

say "refreshing service snapshots, systemd units, and restarting the farm service"
# From inside an Actions job the runner unit must not be restarted: it is the
# process running this script. Its environment file only changes on
# `config apply`, and that path restarts it explicitly when an operator runs
# it; a pending change is picked up on the runner's next restart.
HIL_SKIP_RUNNER_RESTART="$FROM_CI" "$REPO/rig/install-health-service.sh"

flock -u 9

if [ "$VERIFY" = 1 ]; then
  say "verifying the host"
  # The farm service was just restarted; give it a moment before probing.
  for _ in $(seq 30); do
    curl --fail --silent http://127.0.0.1:8090/healthz >/dev/null 2>&1 && break
    sleep 1
  done
  curl --fail --silent --show-error http://127.0.0.1:8090/healthz >/dev/null \
    && echo "  PASS  farm service answers on 127.0.0.1:8090"
  ( cd "$REPO/rig" && ./verify-rig.sh --quick )
  # Full report in the log; "degraded" (optional capabilities missing) is
  # acceptable for a deploy, "unhealthy" is not -- except for the host's own
  # power supply, which is a standing condition of the host and says nothing
  # about whether this release installed. It is still reported and notified;
  # it does not turn a release that installed into one that failed.
  "$VENV/bin/alteriom-hil-health" --output /var/lib/alteriom-hil/status.json --fail-unhealthy --except-supply
fi

say "deployed $(git -C "$REPO" rev-parse --short HEAD) ($(git -C "$REPO" log -1 --format=%s))"
if [ "$FROM_CI" = 1 ]; then
  echo "   runner unit not restarted (this job runs on it); it restarts on the next config apply or reboot"
fi
