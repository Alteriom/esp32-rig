"""A project from a few facts, and a repository's own declaration of one.

Shared by a rig -- its operator adds a project from the dashboard -- and a
portal -- a person adds one to their workspace -- so the two write the same
document: the suite lives in the project's repository and is checked out per
run; its firmware is a bundle the project's own CI built and handed over; the
generic flasher flashes it by the manifest. Validated as any profile is.
"""
from __future__ import annotations

import base64
import re

import yaml

from alteriom_hil.farm_shared import TARGETS
from alteriom_hil.profiles import NAME_PATTERN, parse_profile

# The manifest key that holds the commit a bundle was built from, unless the
# project names another: what the documented build scripts write.
DEFAULT_REVISION_KEY = "git_sha"
# Where a repository describes itself as a project, at its root.
DECLARATION = ".alteriom-hil.yaml"
REPO_URL = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")


def parse_repo(url: str) -> tuple[str, str]:
    """`owner, repo` from a github.com repository URL, or a ValueError saying
    what a project's repository has to be."""
    match = REPO_URL.fullmatch(str(url or "").strip())
    if not match:
        raise ValueError("a project's repository is a github.com URL, as https://github.com/<owner>/<repository>")
    return match.group("owner"), match.group("repo")


def normalise_repo(url: str) -> str:
    owner, repo = parse_repo(url)
    return f"https://github.com/{owner}/{repo}"


def repo_slug(url: str) -> str:
    """A project name from a repository's name: lowercase letters, digits and
    dashes, as a profile is named."""
    _owner, repo = parse_repo(url)
    slug = re.sub(r"[^a-z0-9-]+", "-", repo.lower()).strip("-")[:64]
    return slug or "project"


def decode_contents(answer) -> str | None:
    """The text of a file as GitHub's contents API answers it, or None."""
    if not isinstance(answer, dict) or answer.get("type", "file") != "file":
        return None
    content = answer.get("content")
    if not isinstance(content, str):
        return None
    if answer.get("encoding") == "base64":
        try:
            return base64.b64decode(content.encode("ascii"), validate=False).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return content


def declared_fields(text: str) -> dict:
    """What a repository's `.alteriom-hil.yaml` says about the project, as
    the fields a form takes -- and which workspaces it says it belongs to
    (`workspace: <id>` or `workspaces: [...]`), which is how a portal knows
    the person adding it controls the repository."""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {"invalid": True, "workspaces": []}
    if not isinstance(doc, dict):
        return {"invalid": True, "workspaces": []}
    found: dict = {}
    if doc.get("label"):
        found["label"] = str(doc["label"])
    source = doc.get("source") or {}
    if isinstance(source, dict) and source.get("default_ref"):
        found["default_ref"] = str(source["default_ref"])
    suite = doc.get("suite") or {}
    if isinstance(suite, dict):
        if suite.get("path"):
            found["suite_path"] = str(suite["path"])
        for key in ("min_boards", "timeout_seconds"):
            if isinstance(suite.get(key), int):
                found[key] = suite[key]
        if isinstance(suite.get("exclusive"), bool):
            found["exclusive"] = suite["exclusive"]
    supply = doc.get("supply") or {}
    if isinstance(supply, dict):
        if supply.get("workflow"):
            found["supply_workflow"] = str(supply["workflow"])
        if supply.get("artifact"):
            found["supply_artifact"] = str(supply["artifact"])
    build = doc.get("build") or {}
    if isinstance(build, dict) and build.get("revision_key"):
        found["revision_key"] = str(build["revision_key"])
    needs = doc.get("needs")
    if isinstance(needs, list):
        found["families"] = [str(need.get("target")) for need in needs if isinstance(need, dict) and need.get("target")]
    named = []
    if doc.get("workspace"):
        named.append(str(doc["workspace"]))
    if isinstance(doc.get("workspaces"), list):
        named.extend(str(item) for item in doc["workspaces"] if item)
    found["workspaces"] = named
    return found


def project_document(fields: dict, *, normalise=normalise_repo) -> dict:
    """A profile document from the few things a person knows about their
    project. Everything else is the generic shape. `normalise` says what a
    repository URL has to be (a rig asks its GitHub client, which knows the
    same and says it the same way). A document that would stop the service
    starting is refused here (ProfileError is a ValueError)."""
    text = lambda key, default="": str(fields.get(key) if fields.get(key) is not None else default).strip()
    name = text("name").lower()
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("a project's name is lowercase letters, digits and dashes, up to 64")
    repo = normalise(text("repo"))
    label = text("label", name)[:80] or name
    default_ref = text("default_ref", "main")[:120]
    if not default_ref or any(ch.isspace() for ch in default_ref):
        raise ValueError("default_ref is a branch, tag or commit, without spaces")
    suite_path = text("suite_path", "tests").strip("/")
    revision_key = text("revision_key", DEFAULT_REVISION_KEY)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", revision_key):
        raise ValueError("revision_key is the manifest key that holds the commit: letters, digits and underscores")
    supply_repo = normalise(text("supply_repo", repo))
    supply_workflow = text("supply_workflow", ".github/workflows/hil.yml")
    supply_artifact = text("supply_artifact", "hil-artifacts")
    try:
        min_boards = int(fields.get("min_boards") or 1)
        timeout = int(fields.get("timeout_seconds") or 1800)
    except (TypeError, ValueError):
        raise ValueError("min_boards and timeout_seconds are whole numbers") from None
    families = fields.get("families") or []
    if isinstance(families, str):
        families = [part.strip() for part in families.split(",") if part.strip()]
    if not isinstance(families, list):
        raise ValueError("families is a list of chip families")
    unknown = sorted(set(families) - set(TARGETS))
    if unknown:
        raise ValueError(f"not a chip family this rig knows: {', '.join(unknown)} (one of {', '.join(sorted(TARGETS))})")
    # Families named: the run takes one board of each and leaves the rest
    # free. None named: it takes the whole bench, as the shipped suites do.
    exclusive = fields.get("exclusive", not families)
    if not isinstance(exclusive, bool):
        raise ValueError("exclusive is true or false")
    doc = {
        "schema": 1,
        "name": name,
        "label": label,
        "source": {"location": "consumer", "repo": repo, "default_ref": default_ref},
        "build": {"revision_key": revision_key},
        "flash": {"command": ["{python}", "-m", "alteriom_hil.flash_artifacts",
                              "--artifacts", "{artifact_dir}", "--board-map", "{board_map}",
                              "--revision-key", revision_key]},
        "supply": {"repo": supply_repo, "workflow": supply_workflow, "artifact": supply_artifact},
        "suite": {"path": suite_path, "min_boards": min_boards, "exclusive": exclusive,
                  "timeout_seconds": timeout},
        "report": {"title": f"HIL {label} {{revision}}"},
    }
    if families:
        doc["needs"] = [{"target": family, "count": 1} for family in dict.fromkeys(families)]
    parse_profile(doc, f"project {name}")   # ProfileError is a ValueError: a 400 with the reason
    return doc
