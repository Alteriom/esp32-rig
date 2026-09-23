#!/usr/bin/env bash
# Shared deploy helpers for the farm host (update-runner.sh) and the
# simulation host (update-sim-host.sh). Source this file; never execute it.
#
#   env_value KEY FILE
#       print the value of KEY from a KEY=value file (last one wins); empty
#       when the file is unreadable or the key absent.
#
#   sync_clone DIR SOURCE REF ORIGIN_URL
#       bring the git clone at DIR to REF and print "before -> after".
#       With SOURCE set to a local git checkout (an Actions job workspace,
#       typically) its HEAD is fetched over the filesystem, so the host
#       needs no GitHub credentials of its own; REF, when it is a 40-char
#       SHA, must then match what SOURCE is at. Without SOURCE, REF (a
#       branch, tag, or SHA) is fetched from origin. A directory at DIR that
#       is not a git clone is moved aside, never deleted; a missing DIR is
#       cloned in place.
#
# Callers may define say() and die(); defaults are provided.

declare -F say >/dev/null || say() { printf '\n==> %s\n' "$1"; }
declare -F die >/dev/null || die() { printf 'deploy: %s\n' "$1" >&2; exit 1; }

env_value() {
  [ -r "$2" ] && sed -n "s/^$1=//p" "$2" | tail -n 1
  return 0
}

sync_clone() {
  local dir="$1" source="$2" ref="$3" origin_url="$4" aside before after
  if [ -e "$dir" ] && ! git -C "$dir" rev-parse --git-dir >/dev/null 2>&1; then
    # A copy of the tree without git metadata (this is how the first farm
    # host was populated). Keep it — never delete an operator's directory —
    # but get it out of the way so a real clone can live at DIR.
    aside="$dir.pre-deploy-$(date +%Y%m%d-%H%M%S)"
    say "$dir is not a git clone; moving it aside to $aside"
    mv -- "$dir" "$aside"
    echo "   anything you kept in there is still at $aside"
  fi
  if [ ! -e "$dir" ]; then
    if [ -n "$source" ]; then
      say "no clone at $dir yet; cloning from $source"
      git clone --quiet "$source" "$dir"
      git -C "$dir" remote set-url origin "$origin_url"
    else
      say "no clone at $dir yet; cloning $origin_url"
      git clone --quiet "$origin_url" "$dir"
    fi
  fi

  before="$(git -C "$dir" rev-parse HEAD)"
  # An operator who copied a fix into the clone by hand must not make every
  # later deploy fail with "local changes would be overwritten" — that is
  # what stopped two deploys from main in a row. Keep their diff as a patch
  # beside the clone and deploy over it; untracked files are left alone.
  if ! git -C "$dir" diff --quiet HEAD -- 2>/dev/null; then
    local patch="$dir.local-changes-$(date +%Y%m%d-%H%M%S).patch"
    say "$dir has uncommitted changes; saving them to $patch and deploying over them"
    git -C "$dir" diff HEAD -- > "$patch"
    git -C "$dir" reset --quiet --hard HEAD
  fi
  if [ -n "$source" ]; then
    say "fetching $source HEAD into $dir"
    git -C "$dir" fetch --quiet "$source" HEAD
    git -C "$dir" checkout --quiet --detach FETCH_HEAD
    if [[ "$ref" =~ ^[0-9a-fA-F]{40}$ ]] && [ "$(git -C "$dir" rev-parse HEAD)" != "${ref,,}" ]; then
      die "$source is at $(git -C "$dir" rev-parse HEAD), not the requested $ref"
    fi
  else
    say "fetching $ref from origin into $dir"
    git -C "$dir" fetch --prune origin \
      || die "cannot fetch from origin: this host has no GitHub credentials for the private repository; \
add an SSH deploy key or token for the runner user, or deploy through the GitHub workflow"
    if [[ "$ref" =~ ^[0-9a-fA-F]{40}$ ]]; then
      git -C "$dir" checkout --quiet --detach "$ref"
    elif git -C "$dir" show-ref --verify --quiet "refs/remotes/origin/$ref"; then
      git -C "$dir" checkout --quiet "$ref" 2>/dev/null || git -C "$dir" checkout --quiet -b "$ref" "origin/$ref"
      git -C "$dir" pull --ff-only --quiet origin "$ref" \
        || die "local branch $ref has diverged from origin/$ref; resolve by hand (git -C $dir status)"
    elif git -C "$dir" show-ref --verify --quiet "refs/tags/$ref"; then
      git -C "$dir" checkout --quiet --detach "$ref"
    else
      die "unknown ref $ref (not a branch, tag, or commit SHA on origin)"
    fi
  fi
  after="$(git -C "$dir" rev-parse HEAD)"
  printf '   %s -> %s\n' "${before:0:12}" "${after:0:12}"
}
