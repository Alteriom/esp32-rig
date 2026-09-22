#!/usr/bin/env python3
"""Validation profiles: what the farm runs, described as data.

The schema lives in the HAL because the service, its tests and the admin
CLI all import it; the profile *documents* live in `profiles/` at the repo
root, where adding a consumer is a reviewed data change.

Until this module existed the farm knew exactly one consumer. painlessMesh's
repository URL, suite path, build/flash/preflight commands and manifest key
were literals scattered through farm_service.py, so a second project could not
be added without editing the service. `docs/farm-allocation.md` names that as
step 5 of the order-of-work ("Project profiles ... making the farm generic").

A profile is a YAML document in `profiles/`. Two things follow from that:

- Adding a consumer is a reviewed data change, not a service change.
- The schema is deliberately the *farm side* of the same contract a consumer
  declares in its own `.alteriom-hil.yaml`. When the farm learns to read that
  file from the consumer's checkout, it merges into this shape rather than
  replacing it -- the document moves, the vocabulary does not.

Commands are lists, never strings, and are run without a shell. Placeholders
are substituted from a fixed, validated set; an unknown placeholder is an error
at load time rather than a confusing failure twenty minutes into a run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

from .allocation import RESOURCES

PROFILE_SCHEMA = 1
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

# What each stage can actually substitute, keyed by the profile field it
# validates. A single global list was not enough: it accepted {revision} in
# build.command, where the revision is not known until the manifest that
# command writes has been read, and {workspace} in report.title, which the
# report render does not supply. Such a profile passed startup validation and
# raised ProfileError mid-run -- potentially after flashing and testing, with
# the rig held. A placeholder a stage cannot render is a mistake in the
# profile, and the only place to say so usefully is at load.
#
# These sets are the contract with farm_service's render calls; the pipeline
# tests assert the two agree, so widening one without the other fails there
# rather than in production.
STAGE_PLACEHOLDERS = {
    "flash": frozenset({
        "python", "artifact_dir", "board_map", "manifest", "workspace",
        "revision", "ref", "targets_csv", "farm_repo", "farm_runner",
    }),
    "preflight": frozenset({
        "python", "board_map", "manifest", "preflight", "log_dir",
        "artifact_dir", "workspace", "revision", "ref", "targets_csv",
        "farm_repo", "farm_runner",
    }),
    # The consumer's own test runner, for a profile that declares one. It is
    # handed everything flash and preflight get plus {results}: the path the
    # JUnit XML must be written to, because that is what the report is built
    # from when the HAL's pytest plugin was not there to write records.
    "test": frozenset({
        "python", "board_map", "results", "log_dir", "manifest", "artifact_dir",
        "workspace", "revision", "ref", "targets_csv", "farm_repo", "farm_runner",
    }),
    "env": frozenset({
        "python", "revision", "ref", "artifact_dir", "board_map", "workspace",
        "targets_csv", "farm_repo", "farm_runner",
    }),
    # The report is rendered from the run's outcome, not its inputs. {version}
    # is the bundle's stamped version, or its short revision when it has none.
    "report": frozenset({"revision", "ref", "version"}),
}

# The union, for documentation and for anything that needs the vocabulary as a
# whole. Validation always uses a stage's own set.
PLACEHOLDERS = frozenset().union(*STAGE_PLACEHOLDERS.values())

# Where a profile's build script and suite live.
#   farm     -- in this repository, under suites/<name>/. Correct for
#               painlessMesh: it is a public library and can hold no credential
#               for this private farm, so the farm owns its suite.
#   consumer -- in the project's own repository, checked out per run. This is
#               the product direction: a team keeps its HIL suite beside the
#               firmware it tests, and the farm runs what it finds.
LOCATIONS = frozenset({"farm", "consumer"})


class ProfileError(ValueError):
    """A profile document is malformed. Raised at load, never mid-run."""


@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    location: str
    repo: str
    default_ref: str
    # The manifest field that says which commit a bundle is for. The farm
    # never builds, so this is the whole of what a profile says about a
    # build: the project's own CI made the bundle, and this names where in
    # its manifest the commit is.
    revision_key: str
    suite_path: str
    min_boards: int
    exclusive: bool
    suite_timeout: int
    needs: tuple[dict, ...] = ()
    # May run at the same time as other runs, on boards it was not given
    # (alteriom_hil.allocation). Only a suite that touches nothing but its own
    # board map may say so; a whole-bank profile never can.
    concurrent: bool = False
    # Rig-wide things a concurrent run uses that another must not use at the
    # same time: allocation.RESOURCES.
    resources: tuple[str, ...] = ()
    flash_command: tuple[str, ...] = ()
    preflight_command: tuple[str, ...] = ()
    preflight_timeout: int = 180
    test_command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    report_title: str = ""
    capabilities: str | None = None
    # Who builds this profile's firmware: the only source of a bundle the
    # farm will flash, apart from one an earlier run already left for the
    # same commit. The farm takes it only from the repository and workflow
    # named here, so a bundle cannot arrive from anywhere else.
    supply_repo: str | None = None
    supply_workflow: str | None = None
    supply_artifact: str | None = None
    # Where the source of this profile's HIL agent is, in the farm's
    # repository: the firmware a suite's boards run so that the suite can talk
    # to them. A bundle built against one agent speaks a protocol a suite
    # deployed with another does not, so the farm digests this directory and
    # holds a bundle that declares `hil_agent_sha` to it. None for a profile
    # whose firmware carries no agent -- a product's own image, the health
    # check -- and such a profile's bundles are held to none.
    agent_source_path: str | None = None

    @property
    def runs_in_consumer_repo(self) -> bool:
        return self.location == "consumer"

    @property
    def accepts_supplied_bundles(self) -> bool:
        """Does this profile name a producer whose bundles the farm takes?"""
        return bool(self.supply_repo and self.supply_workflow)

    @property
    def has_test_command(self) -> bool:
        """Whether the profile runs its suite with a command of its own.

        Without one the farm runs pytest over suite.path, which is what every
        profile did before this existed and what a pytest suite still wants:
        the HAL's plugin then writes the richer per-test records the report
        prefers. A profile declares a command when its suite is something
        else, and takes on writing JUnit to {results} in exchange.
        """
        return bool(self.test_command)

    @property
    def has_preflight(self) -> bool:
        """Whether boards can be asked what they are running before the suite.

        painlessMesh can: its HIL agent answers a JSON info command, so the
        farm can skip a flash when the boards already carry the revision.
        A profile running stock product firmware usually cannot, and must not
        be failed for it -- absence means "always flash", not "unhealthy".
        """
        return bool(self.preflight_command)

    def render(self, template: tuple[str, ...] | str, **values) -> list[str] | str:
        """Substitute placeholders in a command list or a single string.

        Values are inserted verbatim into argv entries. Nothing is passed
        through a shell, so a path with a space is one argument and cannot
        split; the caller is responsible for not inventing values from
        unvalidated request fields.
        """
        if isinstance(template, str):
            return self._render_one(template, values)
        return [self._render_one(part, values) for part in template]

    @staticmethod
    def _render_one(part: str, values: dict) -> str:
        def swap(match: re.Match) -> str:
            key = match.group(1)
            if key not in values:
                raise ProfileError(
                    f"no value supplied for placeholder {{{key}}} while rendering {part!r}"
                )
            return str(values[key])

        return PLACEHOLDER.sub(swap, part)


def _require(doc: dict, key: str, where: str):
    if key not in doc:
        raise ProfileError(f"{where}: missing required key {key!r}")
    return doc[key]


def _check_placeholders(text: str, stage: str, where: str) -> None:
    """Every placeholder in `text` must be one `stage` can substitute."""
    allowed = STAGE_PLACEHOLDERS[stage]
    for found in PLACEHOLDER.findall(text):
        if found in allowed:
            continue
        if found in PLACEHOLDERS:
            raise ProfileError(
                f"{where}: {{{found}}} is not available to the {stage} stage; "
                f"it can use: {sorted(allowed)}"
            )
        raise ProfileError(
            f"{where}: unknown placeholder {{{found}}}; known: {sorted(PLACEHOLDERS)}"
        )


def _command(raw: object, where: str, stage: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if raw is None and allow_empty:
        return ()
    if not isinstance(raw, list) or not raw:
        raise ProfileError(f"{where}: command must be a non-empty list of strings")
    parts = []
    for item in raw:
        if not isinstance(item, str):
            raise ProfileError(f"{where}: command entries must be strings, got {type(item).__name__}")
        _check_placeholders(item, stage, where)
        parts.append(item)
    return tuple(parts)


def agent_source_path(doc: object, source: str) -> str | None:
    """The `agent.source_path` a profile document declares, or None.

    On its own, and lenient about everything else in the document, because a
    portal reads it out of a *release's* profiles to learn which agent that
    release's rigs speak -- and a release may be older or newer than the
    portal reading it, with a profile this code would not otherwise accept.
    """
    agent = doc.get("agent") if isinstance(doc, dict) else None
    if agent is None:
        return None
    if not isinstance(agent, dict):
        raise ProfileError(f"{source}: agent must be a mapping")
    raw = _require(agent, "source_path", f"{source}: agent")
    if not isinstance(raw, str) or not raw.strip():
        raise ProfileError(f"{source}: agent.source_path must be a non-empty string")
    path = PurePosixPath(raw.strip())
    # Inside the repository, by a path that says so: it is joined to a
    # checkout and handed to `git archive`.
    if path.is_absolute() or ".." in path.parts or "\\" in raw or path.parts[:1] in ((), (".",)):
        raise ProfileError(
            f"{source}: agent.source_path must be a relative path inside the repository, got {raw!r}"
        )
    return path.as_posix()


def parse_profile(doc: object, source: str) -> Profile:
    """Validate one profile document. `source` names it in error messages."""
    if not isinstance(doc, dict):
        raise ProfileError(f"{source}: profile must be a mapping")
    if doc.get("schema") != PROFILE_SCHEMA:
        raise ProfileError(f"{source}: unsupported profile schema {doc.get('schema')!r}")

    name = _require(doc, "name", source)
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise ProfileError(f"{source}: name must be lowercase alphanumeric with dashes")

    src = _require(doc, "source", source)
    if not isinstance(src, dict):
        raise ProfileError(f"{source}: source must be a mapping")
    location = _require(src, "location", source)
    if location not in LOCATIONS:
        raise ProfileError(f"{source}: source.location must be one of {sorted(LOCATIONS)}")
    repo = _require(src, "repo", source)
    if not isinstance(repo, str) or not repo.strip():
        raise ProfileError(f"{source}: source.repo must be a non-empty string")

    build = _require(doc, "build", source)
    if not isinstance(build, dict):
        raise ProfileError(f"{source}: build must be a mapping")
    # The farm runs firmware; it does not build it. The build for an ESP is
    # the business of the project that ships it, in its own pipeline, and
    # the farm takes the bundle that pipeline made. A profile that still
    # carries a build command is refused where it is written, naming what
    # to do instead, rather than silently ignored.
    for retired in ("command", "target_option"):
        if retired in build:
            raise ProfileError(
                f"{source}: build.{retired} is not accepted -- the farm does not "
                f"build firmware. Build the bundle in the project's own CI and "
                f"name that workflow under `supply:`; the farm flashes what it "
                f"supplies (docs/adding-a-consumer.md)"
            )
    if not (doc.get("supply") or {}):
        # Without a producer nothing can ever give this profile firmware, and
        # every run of it would be refused. Said where the profile is written.
        raise ProfileError(
            f"{source}: a profile must declare a `supply:` producer -- the "
            f"workflow that builds its firmware -- or nothing can ever give it "
            f"a bundle to flash"
        )
    suite = _require(doc, "suite", source)
    if not isinstance(suite, dict):
        raise ProfileError(f"{source}: suite must be a mapping")

    # What the run needs from the bank. An exclusive profile takes everything
    # and needs no list; a shared one must say, because the board map it gets
    # is built from this and it may touch nothing else.
    needs_raw = doc.get("needs") or []
    if not isinstance(needs_raw, list):
        raise ProfileError(f"{source}: needs must be a list")
    needs = []
    for entry in needs_raw:
        if not isinstance(entry, dict) or "target" not in entry:
            raise ProfileError(f"{source}: each needs entry must be a mapping with a target")
        count = entry.get("count", 1)
        if not isinstance(count, int) or count < 1:
            raise ProfileError(f"{source}: needs count must be a positive integer")
        # Tags narrow a family to the boards that carry every one of them
        # (a board's `tags` in the registry): psram, a wired instrument.
        tags = entry.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
            raise ProfileError(f"{source}: needs tags must be a list of strings")
        need = {"target": str(entry["target"]), "count": count}
        if tags:
            need["tags"] = sorted(set(tags))
        needs.append(need)

    flash = doc.get("flash") or {}
    if not flash:
        raise ProfileError(f"{source}: missing required key 'flash'")
    preflight = doc.get("preflight") or {}
    test = doc.get("test") or {}
    if not isinstance(test, dict):
        raise ProfileError(f"{source}: test must be a mapping")
    env_raw = doc.get("env") or {}
    if not isinstance(env_raw, dict):
        raise ProfileError(f"{source}: env must be a mapping")
    env = {}
    for key, value in env_raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ProfileError(f"{source}: env keys and values must be strings")
        _check_placeholders(value, "env", f"{source}: env {key}")
        env[key] = value

    min_boards = suite.get("min_boards", 1)
    if not isinstance(min_boards, int) or min_boards < 1:
        raise ProfileError(f"{source}: suite.min_boards must be a positive integer")
    timeout = suite.get("timeout_seconds", 1800)
    if not isinstance(timeout, int) or timeout < 1:
        raise ProfileError(f"{source}: suite.timeout_seconds must be a positive integer")
    suite_path = _require(suite, "path", source)
    if not isinstance(suite_path, str) or suite_path.startswith("/") or ".." in suite_path:
        raise ProfileError(f"{source}: suite.path must be a relative path inside the workspace")

    report = doc.get("report") or {}
    title = report.get("title", f"HIL {name} {{revision}}")
    _check_placeholders(str(title), "report", f"{source}: report.title")
    # Where the consumer says what its suite proves. A path into its own
    # checkout, so a farm profile names a file in this repository and a
    # consumer profile a file that arrives with the clone. Optional: a suite
    # that claims no named capabilities gets a report with no coverage table
    # rather than someone else's table of gaps.
    capabilities = report.get("capabilities")
    if capabilities is not None and (
        not isinstance(capabilities, str) or capabilities.startswith("/") or ".." in capabilities
    ):
        raise ProfileError(f"{source}: report.capabilities must be a relative path inside the workspace")

    if not bool(suite.get("exclusive", True)) and not needs:
        raise ProfileError(
            f"{source}: a non-exclusive profile must declare needs; the board map "
            f"it runs against is built from them"
        )
    concurrent = suite.get("concurrent", False)
    if not isinstance(concurrent, bool):
        raise ProfileError(f"{source}: suite.concurrent must be true or false")
    if concurrent and bool(suite.get("exclusive", True)):
        raise ProfileError(
            f"{source}: an exclusive profile takes the whole bank and cannot be "
            f"concurrent; set exclusive: false and declare needs"
        )
    resources = suite.get("resources") or []
    if not isinstance(resources, list) or not set(resources) <= set(RESOURCES):
        raise ProfileError(f"{source}: suite.resources must be a list of {', '.join(RESOURCES)}")

    supply = doc.get("supply") or {}
    if not isinstance(supply, dict):
        raise ProfileError(f"{source}: supply must be a mapping")
    supply_repo = supply_workflow = supply_artifact = None
    if supply:
        # A bundle is accepted on the strength of where it was built, so both
        # halves are required: a repository alone would take any workflow in
        # it, including one a pull request can add.
        supply_repo = str(_require(supply, "repo", f"{source}: supply")).strip()
        supply_workflow = str(_require(supply, "workflow", f"{source}: supply")).strip()
        if not supply_repo:
            raise ProfileError(f"{source}: supply.repo must not be empty")
        if not supply_workflow.startswith(".github/workflows/"):
            raise ProfileError(
                f"{source}: supply.workflow must be a workflow path under "
                f".github/workflows/, got {supply_workflow!r}"
            )
        supply_artifact = str(supply.get("artifact") or "").strip() or None

    return Profile(
        name=name,
        label=doc.get("label") or name,
        location=location,
        repo=repo.strip(),
        default_ref=src.get("default_ref", "main"),
        revision_key=_require(build, "revision_key", source),
        # Required, not optional. An empty flash command reaches
        # subprocess.Popen([]) and raises IndexError -- after the hardware
        # discovery, so a profile that looked valid at startup fails
        # deep into a run holding the rig. A profile with nothing to flash
        # cannot run a suite; say so at load.
        flash_command=_command(_require(flash, "command", f"{source}: flash"), f"{source}: flash", "flash"),
        preflight_command=_command(
            preflight.get("command"), f"{source}: preflight", "preflight", allow_empty=True
        ),
        preflight_timeout=int(preflight.get("timeout_seconds", 180)),
        test_command=_command(
            test.get("command"), f"{source}: test", "test", allow_empty=True
        ),
        suite_path=suite_path,
        min_boards=min_boards,
        exclusive=bool(suite.get("exclusive", True)),
        suite_timeout=timeout,
        needs=tuple(needs),
        concurrent=concurrent,
        resources=tuple(sorted(set(resources))),
        env=env,
        report_title=str(title),
        capabilities=capabilities,
        supply_repo=supply_repo,
        supply_workflow=supply_workflow,
        supply_artifact=supply_artifact,
        agent_source_path=agent_source_path(doc, source),
    )


def load_profiles(repo: Path) -> dict[str, Profile]:
    """Every profile under `<repo>/profiles/`, keyed by name.

    A malformed document raises rather than being skipped: a profile silently
    missing would show up as "unsupported validation profile" on a submitted
    run, which points the operator at the request instead of the typo.
    """
    directory = Path(repo) / "profiles"
    if not directory.is_dir():
        raise ProfileError(f"no profiles directory at {directory}")
    found: dict[str, Profile] = {}
    for path in sorted(directory.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ProfileError(f"{path}: cannot be read: {exc}") from exc
        profile = parse_profile(doc, str(path))
        if profile.name in found:
            raise ProfileError(f"{path}: duplicate profile name {profile.name!r}")
        if profile.name != path.stem:
            raise ProfileError(
                f"{path}: profile name {profile.name!r} does not match its filename"
            )
        found[profile.name] = profile
    if not found:
        raise ProfileError(f"no profiles found in {directory}")
    return found
