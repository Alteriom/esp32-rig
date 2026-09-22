"""An MCP server for the farm: what an assistant reads the farm through.

It speaks the Model Context Protocol over stdio -- one JSON-RPC message per
line -- and turns each tool call into a read of the farm's API with a named
key (alteriom_hil.farm_client). An assistant can then answer "what is the farm
doing", "why did that run fail", "which bundle did it flash", "is this board
healthy" from the farm itself rather than from a person pasting pages into a
chat.

**Read-only, deliberately.** Every tool reads; none starts a run, cancels one
or touches a board. Tools that change the farm come after this has been used,
and will need a key whose role allows them -- the farm refuses them to a
`user` key whatever a client asks. A `user` key is all this needs.

Written against the protocol directly rather than an SDK: the HAL supports
Python 3.9, the protocol is small, and a dependency the farm host does not
otherwise need is not worth the dozen methods it would save.

    ALTERIOM_FARM_URL=https://hil.example.com \\
    ALTERIOM_FARM_KEY_FILE=~/.config/alteriom/farm-key \\
    alteriom-farm-mcp
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Callable

from .farm_client import FarmClient, FarmError

SERVER_NAME = "alteriom-farm"
SERVER_VERSION = "0.1.0"
# Newest first; a client asking for one of these gets it, any other the newest.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
# What one tool result may put in front of a model.
TEXT_LIMIT = 60_000
JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
INSTRUCTIONS = (
    "Tools for the Alteriom ESP32 hardware-in-the-loop farm: boards on a rig that "
    "flash firmware bundles built by each project's CI and run test suites against "
    "them. Every tool reads; none changes the farm. Start with farm_status; a run's "
    "id comes from farm_runs, and farm_run explains what it did and where it failed."
)


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict
    call: Callable[[FarmClient, dict], object]


class ToolError(ValueError):
    """A tool call the farm could not answer, said to the model as a result."""


def _integer(arguments: dict, name: str, default: int, low: int, high: int) -> int:
    value = arguments.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ToolError(f"{name} must be an integer from {low} to {high}")
    return value


def _text(arguments: dict, name: str, pattern: str | None = None, required: bool = False) -> str | None:
    value = arguments.get(name)
    if value in (None, ""):
        if required:
            raise ToolError(f"{name} is required")
        return None
    if not isinstance(value, str) or len(value) > 200 or (pattern and not re.fullmatch(pattern, value)):
        raise ToolError(f"{name} is not a valid value")
    return value


def _job_id(arguments: dict, name: str = "run_id") -> str:
    value = _text(arguments, name, required=True)
    if not JOB_ID.fullmatch(value):
        raise ToolError(f"{name} must be the 32-character id farm_runs gives")
    return value


# ---- what a model is shown ------------------------------------------------------
# The farm's answers, cut to what a question about them needs: a run page is
# tens of kilobytes of report and log, and a model reading it all to find the
# failed stage is a model with less room for the question.


def _run_brief(job: dict) -> dict:
    request = job.get("request") or {}
    result = job.get("result") or {}
    return {
        "id": job.get("id"),
        "kind": job.get("kind"),
        "status": job.get("status"),
        "project": request.get("project") or request.get("profile"),
        "branch": request.get("branch"),
        "revision": request.get("resolved_sha") or result.get("revision") or request.get("ref"),
        "started_by": request.get("actor") or request.get("submitted_by"),
        "created_at": job.get("created_at"),
        "duration_seconds": job.get("duration_seconds"),
        "summary": result.get("summary"),
        "failed_stage": result.get("failed_stage"),
    }


def _status(client: FarmClient, arguments: dict) -> dict:
    data = client.status()
    inventory = data.get("inventory") or {}
    health = data.get("health") or {}
    return {
        "you": data.get("you"),
        "health": health.get("status"),
        "health_problems": [
            f"{item.get('name')}: {item.get('message')}"
            for item in health.get("checks") or [] if item.get("status") != "ok"
        ],
        "queue": data.get("queue"),
        "boards_connected": len(inventory.get("boards") or []),
        "boards_missing": inventory.get("missing") or [],
        "unregistered_devices": len(inventory.get("unregistered") or []),
        "recent_runs": [_run_brief(job) for job in (data.get("jobs") or [])[:10]],
        "version": (data.get("version") or {}).get("version"),
    }


def _devices(client: FarmClient, arguments: dict) -> dict:
    data = client.inventory()
    boards = []
    for board in data.get("boards") or []:
        health = board.get("health") or {}
        boards.append({
            "id": board.get("id"), "target": board.get("target"), "state": board.get("state"),
            "mac": board.get("mac"), "port": board.get("port"),
            "held_by": (board.get("held_by") or {}).get("job_id"),
            # Out of the pool: reserved for bench work, or quarantined by the canary.
            "hold": {key: (board.get("hold") or {}).get(key) for key in ("state", "reason", "since", "by")}
            if board.get("hold") else None,
            "canary": health.get("verdict"), "canary_failed": health.get("failed"),
            "checked_at": health.get("checked_at"),
        })
    return {
        "boards": boards,
        "missing": data.get("missing") or [],
        "unregistered": [
            {"target": item.get("target"), "mac": item.get("mac"), "port": item.get("port")}
            for item in data.get("unregistered") or []
        ],
        "instruments": [
            {"id": item.get("id"), "kind": item.get("kind"), "port": item.get("port"),
             "wires": len(item.get("wiring") or [])}
            for item in data.get("instruments") or []
        ],
        "probe_errors": data.get("probe_errors") or [],
    }


def _runs(client: FarmClient, arguments: dict) -> dict:
    page = client.jobs(
        limit=_integer(arguments, "limit", 20, 1, 50),
        offset=_integer(arguments, "offset", 0, 0, 100_000),
        status=_text(arguments, "status", r"queued|running|passed|failed|cancelled|interrupted"),
        kind=_text(arguments, "kind", r"suite|inventory|build"),
        search=_text(arguments, "search"),
    )
    return {
        "total": page.get("total"),
        "counts": page.get("counts"),
        "runs": [_run_brief(job) for job in page.get("jobs") or []],
    }


def _run(client: FarmClient, arguments: dict) -> dict:
    job = client.job(_job_id(arguments))
    report = job.get("report") or {}
    capabilities = report.get("capabilities") or {}
    return {
        **_run_brief(job),
        "request": {key: value for key, value in (job.get("request") or {}).items()
                    if key in ("profile", "ref", "targets", "tests", "keyword", "artifact", "boards")},
        "detail": (job.get("result") or {}).get("detail"),
        "stages": [
            {"name": stage.get("name"), "status": stage.get("status"), "summary": stage.get("summary")}
            for stage in job.get("progress") or []
        ],
        "capabilities": {name: (item or {}).get("status") for name, item in capabilities.items()},
        "failed_tests": sorted({
            test for item in capabilities.values() if (item or {}).get("status") not in ("validated", "skipped")
            for test in (item or {}).get("tests") or []
        })[:100],
        "bundle": (job.get("bundle") or {}).get("id"),
        "artifacts": sorted(name for name, item in (job.get("artifacts") or {}).items() if (item or {}).get("available")),
    }


def _run_log(client: FarmClient, arguments: dict) -> dict:
    lines = _integer(arguments, "lines", 200, 1, 1000)
    text = client.job_log(_job_id(arguments))
    tail = text.splitlines()[-lines:]
    return {"run_id": arguments["run_id"], "lines": len(tail), "log": "\n".join(tail)}


def _bundles(client: FarmClient, arguments: dict) -> dict:
    page = client.bundles(
        limit=_integer(arguments, "limit", 20, 1, 50),
        offset=_integer(arguments, "offset", 0, 0, 100_000),
        profile=_text(arguments, "profile", r"[a-z0-9][a-z0-9-]{0,63}"),
        search=_text(arguments, "search"),
    )
    return {
        "matched": page.get("matched"),
        "total": page.get("count"),
        "bundles": [
            {
                "id": entry.get("id"), "profile": entry.get("profile"), "branch": entry.get("branch"),
                "revision": entry.get("revision"), "started_by": entry.get("actor"),
                "families": [family.get("family") for family in entry.get("families") or []],
                "bytes": entry.get("bytes"), "created_at": entry.get("created_at"),
                "source": (entry.get("source") or {}).get("kind"), "pinned": entry.get("pinned"),
                "flashed_by_runs": len(entry.get("reused_by") or []),
            }
            for entry in page.get("bundles") or []
        ],
    }


def _bundle(client: FarmClient, arguments: dict) -> dict:
    entry = client.bundle(_job_id(arguments, "bundle_id"))
    files = entry.get("files") or []
    return {
        **{key: value for key, value in entry.items() if key not in ("files", "reused_by")},
        "files": files[:50],
        "files_not_shown": max(0, len(files) - 50),
        "runs": [item.get("id") for item in (entry.get("reused_by") or [])][:50],
    }


def _capacity(client: FarmClient, arguments: dict) -> dict:
    return client.capacity()


def _statistics(client: FarmClient, arguments: dict) -> dict:
    return client.statistics(days=_integer(arguments, "days", 7, 1, 90))


def _whoami(client: FarmClient, arguments: dict) -> dict:
    return client.whoami()


_RUN_ID = {"type": "string", "description": "A run's 32-character id, from farm_runs"}
TOOLS = (
    Tool("farm_whoami", "Who this key is", "The name and role of the API key this server reads the farm with.",
         {"type": "object", "properties": {}, "additionalProperties": False}, _whoami),
    Tool("farm_status", "Farm status",
         "The farm now: health and what is wrong with it, the queue (paused, why, running, waiting), "
         "boards connected and missing, and the ten most recent runs.",
         {"type": "object", "properties": {}, "additionalProperties": False}, _status),
    Tool("farm_devices", "Boards and instruments",
         "Every board on the rig -- family, state, which run holds it, whether it is reserved or "
         "quarantined and why, its last Rig Health Check verdict and what failed -- plus missing and "
         "unregistered devices and the instruments wired to boards.",
         {"type": "object", "properties": {}, "additionalProperties": False}, _devices),
    Tool("farm_capacity", "What the farm could take now",
         "Boards per family: connected, free and in use, with the tags the free ones carry; how many "
         "runs may be in progress at once, how many are, and how many wait. Whether a run asking "
         "for, say, one esp32-c3 would start now.",
         {"type": "object", "properties": {}, "additionalProperties": False}, _capacity),
    Tool("farm_runs", "Runs", "Runs newest first, with project, branch, revision, status and summary.",
         {"type": "object", "additionalProperties": False, "properties": {
             "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
             "offset": {"type": "integer", "minimum": 0, "default": 0},
             "status": {"type": "string", "enum": ["queued", "running", "passed", "failed", "cancelled", "interrupted"]},
             "kind": {"type": "string", "enum": ["suite", "inventory", "build"]},
             "search": {"type": "string", "description": "id, project, branch, revision, summary or failed stage"},
         }}, _runs),
    Tool("farm_run", "One run",
         "What a run did: its stages and where it stopped, the failure detail, each capability's "
         "verdict, the tests that failed, the bundle it flashed and the evidence it left.",
         {"type": "object", "additionalProperties": False, "required": ["run_id"],
          "properties": {"run_id": _RUN_ID}}, _run),
    Tool("farm_run_log", "A run's log", "The last lines of a run's own log, where a stage's error is.",
         {"type": "object", "additionalProperties": False, "required": ["run_id"], "properties": {
             "run_id": _RUN_ID,
             "lines": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
         }}, _run_log),
    Tool("farm_bundles", "Firmware bundles",
         "The firmware bundles the farm holds, newest first: profile, branch, revision, families, "
         "where each came from and how many runs flashed it.",
         {"type": "object", "additionalProperties": False, "properties": {
             "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
             "offset": {"type": "integer", "minimum": 0, "default": 0},
             "profile": {"type": "string"},
             "search": {"type": "string"},
         }}, _bundles),
    Tool("farm_bundle", "One bundle", "One bundle: its manifest, images, files and the runs that used it.",
         {"type": "object", "additionalProperties": False, "required": ["bundle_id"],
          "properties": {"bundle_id": {"type": "string", "description": "A bundle's 32-character id"}}}, _bundle),
    Tool("farm_statistics", "Farm statistics",
         "What the farm has done over the last days: runs and pass rate, run time and queue wait, "
         "rig busy time, failures by stage, per project, where firmware came from.",
         {"type": "object", "additionalProperties": False, "properties": {
             "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7},
         }}, _statistics),
)


class Server:
    def __init__(self, client_factory: Callable[[], FarmClient] = FarmClient.from_env):
        self._client_factory = client_factory
        self._client: FarmClient | None = None
        self.tools = {tool.name: tool for tool in TOOLS}

    def _client_or_error(self) -> FarmClient:
        if self._client is None:
            try:
                self._client = self._client_factory()
            except (OSError, ValueError) as exc:
                raise ToolError(f"the server is not configured: {exc}") from None
        return self._client

    # ---- JSON-RPC ------------------------------------------------------------

    @staticmethod
    def _error(message_id, code: int, text: str) -> dict:
        return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}

    def handle(self, message) -> dict | None:
        """The reply to one message, or None for a notification or a response."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        if method is None:
            return None  # a response to something this server never asks
        is_request = "id" in message
        message_id = message.get("id")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return self._error(message_id, -32602, "params must be an object") if is_request else None
        if method == "initialize":
            wanted = params.get("protocolVersion")
            version = wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [
                {
                    "name": tool.name, "title": tool.title, "description": tool.description,
                    "inputSchema": tool.input_schema,
                    "annotations": {"title": tool.title, "readOnlyHint": True, "openWorldHint": False},
                }
                for tool in TOOLS
            ]}
        elif method == "tools/call":
            tool = self.tools.get(params.get("name"))
            if tool is None:
                return self._error(message_id, -32602, f"unknown tool: {params.get('name')}")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return self._error(message_id, -32602, "arguments must be an object")
            result = self._call(tool, arguments)
        elif method.startswith("notifications/"):
            return None
        else:
            return self._error(message_id, -32601, f"method not found: {method}") if is_request else None
        return {"jsonrpc": "2.0", "id": message_id, "result": result} if is_request else None

    def _call(self, tool: Tool, arguments: dict) -> dict:
        unknown = set(arguments) - set(tool.input_schema.get("properties") or {})
        try:
            if unknown:
                raise ToolError(f"unknown arguments: {', '.join(sorted(unknown))}")
            data = tool.call(self._client_or_error(), arguments)
        except (ToolError, FarmError) as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        text = json.dumps(data, indent=1, sort_keys=True, default=str)
        if len(text) > TEXT_LIMIT:
            text = text[:TEXT_LIMIT] + f"\n... cut at {TEXT_LIMIT} characters; narrow the request"
        result = {"content": [{"type": "text", "text": text}]}
        if isinstance(data, dict):
            result["structuredContent"] = data
        return result

    def serve(self, stdin=None, stdout=None) -> None:
        """One message per line in, one per line out, until stdin closes."""
        stdin = stdin if stdin is not None else sys.stdin
        stdout = stdout if stdout is not None else sys.stdout
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                reply = self._error(None, -32700, "parse error")
            else:
                if isinstance(message, list):
                    reply = self._error(None, -32600, "batches are not supported")
                else:
                    try:
                        reply = self.handle(message)
                    except Exception as exc:  # a bug here must not end the session
                        sys.stderr.write(f"{SERVER_NAME}: {exc!r}\n")
                        reply = self._error(message.get("id") if isinstance(message, dict) else None, -32603, "internal error")
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()


def main() -> int:
    Server().serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
