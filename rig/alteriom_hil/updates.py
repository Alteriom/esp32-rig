"""Updates on the owner's terms.

A rig checks for a newer release of its software and says so; it installs
one when its owner asks -- from the rig's page, or from a farm's page for
the rig -- or on its own when the owner turned automatic installs on.
Never because a farm said so: a rig is its owner's.

A standalone rig asks GitHub for the newest release of the rig software; a
node is told by its portal. Either way the release is staged under the
state directory's update/ and handed to alteriom-hil-update.path, the unit
that installs as a node's releases always were (rig/node-update.sh) -- so
the service never needs root of its own, and what it stages is checked
against the release's own document before anything is installed.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from alteriom_hil import release as release_document
from alteriom_hil.signin import USER_AGENT, http_json

SOURCE = "https://github.com/Alteriom/esp32-rig"
LATEST = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
API_VERSION = "2022-11-28"
VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
REPO_URL = re.compile(r"^https://github\.com/(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$")
DOWNLOAD_LIMIT = 512 * 1024 * 1024
SETTINGS_FILE = "update-settings.json"
# What status.json may say. The first two are the rig's own (a release being
# fetched); the rest are the update unit's, as a node's always were.
STATES = ("available", "downloading", "staged", "installing", "installed", "failed")


class UpdateError(ValueError):
    """Said in words a page can show; never a traceback."""


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def version_tuple(text: object) -> tuple | None:
    match = VERSION.match(str(text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def newer(installed: object, available: object) -> bool:
    """Whether `available` is a release this host does not run. A host that
    cannot say what it runs is offered the newest, and told why."""
    have, offered = version_tuple(installed), version_tuple(available)
    if offered is None:
        return False
    return have is None or offered > have


def latest_release(source: str = SOURCE, token: str | None = None, ask=None) -> dict:
    """GitHub's newest release of the rig software: version, when, where,
    and each asset by name. Asked without a token when the rig has none --
    the repository is public -- and with the rig's when it has one, which
    is kinder to GitHub's rate limit."""
    match = REPO_URL.match(source)
    if not match:
        raise UpdateError(f"the update source is not a github.com repository: {source}")
    url = LATEST.format(owner=match.group("owner"), repo=match.group("repo"))
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        answer = (ask or http_json)(url, headers=headers, timeout=20)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise UpdateError(f"{source} has no release yet") from None
        if error.code == 403:
            raise UpdateError("GitHub's rate limit is spent for now; a GitHub token on this rig lifts it") from None
        raise UpdateError(f"GitHub answered {error.code} when asked for the newest release") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UpdateError(f"GitHub could not be reached: {error.__class__.__name__}: {error}"[:200]) from None
    if not isinstance(answer, dict) or not answer.get("tag_name"):
        raise UpdateError("GitHub answered with no release")
    tag = str(answer["tag_name"])
    version = tag[1:] if tag.startswith("v") else tag
    if version_tuple(version) is None:
        raise UpdateError(f"the newest release is tagged {tag}, which is not a version")
    assets = {str(asset.get("name")): str(asset.get("browser_download_url"))
              for asset in answer.get("assets") or [] if isinstance(asset, dict) and asset.get("name")}
    return {"version": version, "tag": tag, "published_at": answer.get("published_at"),
            "html_url": answer.get("html_url"), "name": answer.get("name") or tag, "assets": assets,
            "source": source}


def download(url: str, limit: int = DOWNLOAD_LIMIT) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=600) as response:  # noqa: S310 -- github.com, over https
            body = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        raise UpdateError(f"GitHub answered {error.code} for {url.rsplit('/', 1)[-1]}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UpdateError(f"the download failed: {error.__class__.__name__}: {error}"[:200]) from None
    if len(body) > limit:
        raise UpdateError(f"{url.rsplit('/', 1)[-1]} is larger than a release file can be")
    return body


def status(update_dir: Path) -> dict | None:
    """What was last written about an update -- by this rig while fetching,
    by the update unit while installing -- or None."""
    try:
        record = json.loads((Path(update_dir) / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or record.get("state") not in STATES:
        return None
    return {"state": record["state"], "version": record.get("version"), "commit": record.get("commit"),
            "detail": (str(record.get("detail") or "")[-4000:] or None), "at": str(record.get("at") or "")[:40] or None}


def write_status(update_dir: Path, state: str, version: object, commit: object, detail: str | None) -> dict:
    update_dir = Path(update_dir)
    update_dir.mkdir(parents=True, exist_ok=True)
    record = {"state": state, "version": version, "commit": commit, "detail": detail, "at": utcnow()}
    staging = update_dir / "status.json.tmp"
    staging.write_text(json.dumps(record), encoding="utf-8")
    staging.replace(update_dir / "status.json")
    return record


def settings(state_dir: Path) -> dict:
    """The owner's choices about updates; today one: install them on their own."""
    try:
        record = json.loads((Path(state_dir) / SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        record = {}
    return {"auto": bool(record.get("auto", False)) if isinstance(record, dict) else False}


def set_auto(state_dir: Path, auto: bool) -> dict:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    record = {**settings(state_dir), "auto": bool(auto), "changed_at": utcnow()}
    staging = state_dir / (SETTINGS_FILE + ".tmp")
    staging.write_text(json.dumps(record), encoding="utf-8")
    staging.replace(state_dir / SETTINGS_FILE)
    return settings(state_dir)


def stage(update_dir: Path, available: dict, fetch=download) -> dict:
    """Fetch the release into update/release-<version>/ -- its document
    first, then every file it names -- check each file against the
    document, and only then leave the request the update unit installs
    from. A file that is not what the document says stops everything
    before the request exists."""
    assets = available.get("assets") or {}
    if "release.json" not in assets:
        raise UpdateError(f"release {available.get('version')} carries no release.json; it is not a rig release")
    update_dir = Path(update_dir)
    update_dir.mkdir(parents=True, exist_ok=True)
    write_status(update_dir, "downloading", available.get("version"), None, "reading the release's document")
    try:
        manifest = release_document.parse_manifest(fetch(assets["release.json"]))
    except release_document.ReleaseError as error:
        raise UpdateError(f"the release's document is not one this rig can install: {error}") from None
    version = str(manifest.get("version") or available.get("version"))
    target = update_dir / f"release-{version}"
    target.mkdir(parents=True, exist_ok=True)
    (target / "release.json").write_bytes(json.dumps(manifest, indent=2).encode("utf-8"))
    for entry in release_document.entries(manifest):
        name = entry["name"]
        if name not in assets:
            raise UpdateError(f"the release names {name}, which GitHub does not carry for it")
        write_status(update_dir, "downloading", version, manifest.get("commit"), f"downloading {name}")
        (target / name).write_bytes(fetch(assets[name]))
    try:
        release_document.check(manifest, lambda name: (target / name).read_bytes())
    except release_document.ReleaseError as error:
        raise UpdateError(f"refusing to install: {error}") from None
    request = {"version": version, "commit": manifest.get("commit"), "release_dir": str(target),
               "requested_at": utcnow()}
    write_status(update_dir, "staged", version, manifest.get("commit"), "waiting for alteriom-hil-update to install it")
    staging = update_dir / "request.json.tmp"
    staging.write_text(json.dumps(request), encoding="utf-8")
    # Last: the path unit starts the install the moment this exists.
    staging.replace(update_dir / "request.json")
    return request
