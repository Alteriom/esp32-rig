#!/usr/bin/env bash
# Build a release of the rig: the two wheels, the dashboard bundle, their
# checksums, and a release.json that names them all.
#
# A release is packages (docs/public-release-plan.md, step 13). Until now it
# was a git bundle of this repository, which a node fetched and installed
# editable -- the only way to install a private repository on a Pi that cannot
# clone it. The public repository has no such constraint, and a portal that
# names a release as a version and a set of files can hand the same release
# to a node, to PyPI and to anyone.
#
# The version is the farm's own: MAJOR.MINOR from VERSION, PATCH the commit
# count, which is what install-health-service.sh stamps into version.json and
# what the dashboard shows. The two project files say MAJOR.MINOR on the
# trunk; this stamps the full number into them for the build and puts them
# back, so a checkout is never left saying it is a release it is not.
#
#   runner/ci/build-release.sh [--out DIR] [--version-only]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="$REPO/dist"
VERSION_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --version-only) VERSION_ONLY=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "build-release: unknown argument $1" >&2; exit 2 ;;
  esac
done

BASE="$(tr -d '[:space:]' < "$REPO/VERSION")"
BUILD="$(git -C "$REPO" rev-list --count HEAD)"
COMMIT="$(git -C "$REPO" rev-parse HEAD)"
VERSION="$BASE.$BUILD"
if [ "$VERSION_ONLY" = 1 ]; then
  echo "$VERSION"
  exit 0
fi

PYTHON="${PYTHON:-python3}"
"$PYTHON" -c "import build" 2>/dev/null || { echo "build-release: python -m build is not installed (pip install build)" >&2; exit 1; }

# The project files carry the full number only for the duration of the build.
# Put back from a copy, not from git: a checkout with uncommitted edits to
# them must come out of this exactly as it went in.
SAVED="$(mktemp -d)"
restore() {
  for project in core rig; do
    [ -f "$SAVED/$project.toml" ] && cp "$SAVED/$project.toml" "$REPO/$project/pyproject.toml"
  done
  rm -rf "$SAVED"
}
trap restore EXIT
for project in core rig; do
  cp "$REPO/$project/pyproject.toml" "$SAVED/$project.toml"
  sed -i.bak "s/^version = \"[^\"]*\"/version = \"$VERSION\"/" "$REPO/$project/pyproject.toml"
  rm -f "$REPO/$project/pyproject.toml.bak"
done

rm -rf "$OUT"
mkdir -p "$OUT"
for project in core rig; do
  "$PYTHON" -m build --wheel --outdir "$OUT" "$REPO/$project" >/dev/null
done

# The dashboard is the rig's bundle; a portal pins a version of it, which is
# how the farm follows the rig's UI without a second implementation.
# Written through a redirection rather than as tar's own argument: a path
# with a colon in it (a Windows drive) is a remote host to tar.
(cd "$REPO/rig" && tar -czf - web) > "$OUT/alteriom-hil-dashboard-$VERSION.tar.gz"

CONTRACT="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["properties"]["contract"]["const"])' "$REPO/rig/rig-view.schema.json" 2>/dev/null || echo 1)"

(cd "$OUT" && sha256sum ./*.whl ./*.tar.gz > SHA256SUMS)

"$PYTHON" - "$OUT" "$VERSION" "$COMMIT" "$CONTRACT" <<'PY'
import hashlib, json, pathlib, sys
out, version, commit, contract = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])

def entry(path):
    return {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}

wheels = sorted(out.glob("*.whl"))
dashboard = next(out.glob("alteriom-hil-dashboard-*.tar.gz"))
release = {
    "schema": 1,
    "version": version,
    "commit": commit,
    "packages": [entry(w) for w in wheels],
    "dashboard": {**entry(dashboard), "contract": contract},
}
(out / "release.json").write_text(json.dumps(release, indent=2) + "\n", encoding="utf-8")
print(f"release {version} ({commit[:9]}): {', '.join(w.name for w in wheels)}, {dashboard.name}")
PY
