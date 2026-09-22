"""The farm's MCP server: an assistant reads the farm through it, and only reads.

What is held here: it speaks the protocol a client expects -- over real stdio,
one message per line -- every tool answers from the farm's API with a named
key, a wrong argument or a refused key comes back as a result the model can
read rather than a broken session, and nothing it can call changes the farm.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from alteriom_hil import farm_client as farm_client_module
from alteriom_hil.api_keys import KeyStore, add_key, keys_document, keys_path_for
from alteriom_hil.farm_client import FarmClient, FarmError
from alteriom_hil.mcp_server import PROTOCOL_VERSIONS, TOOLS, Server

REPO = Path(__file__).resolve().parents[1]
RUN = "0123456789abcdef0123456789abcdef"
BUNDLE = "fedcba9876543210fedcba9876543210"
KEY = "afk_" + "1" * 64


def _job(job_id=RUN, status="failed"):
    return {
        "id": job_id, "kind": "suite", "status": status, "created_at": "2026-09-13T10:00:00+00:00",
        "duration_seconds": 812.0,
        "request": {"profile": "canary", "project": "Farm canary", "branch": "main", "resolved_sha": "a" * 40,
                    "submitted_by": "sparck", "targets": ["esp32-c6"], "artifact": BUNDLE},
        "result": {"summary": "Validation tests did not complete successfully", "failed_stage": "test",
                   "detail": "pytest exited with status 1"},
        "progress": [{"name": "build", "status": "passed", "summary": "Supplied by x"},
                     {"name": "test", "status": "failed", "summary": "1 failed"}],
    }


class FakeFarm:
    """The farm's read routes, answering canned JSON to one key."""

    def __init__(self):
        seen = self.seen = []
        routes = {
            "/api/v1/whoami": {"name": "sparck", "role": "user"},
            "/api/v1/status": {
                "you": {"name": "sparck", "role": "user"},
                "health": {"status": "degraded", "checks": [{"name": "backup", "status": "degraded", "message": "no backup"},
                                                            {"name": "tools", "status": "ok", "message": "fine"}]},
                "queue": {"paused": False, "queued": [], "running": RUN},
                "inventory": {"boards": [{"id": "esp32-c6-01"}], "missing": ["esp32-02"], "unregistered": []},
                "jobs": [_job()], "version": {"version": "1.0.140"},
            },
            "/api/v1/inventory": {
                "boards": [{"id": "esp32-c6-01", "target": "esp32-c6", "state": "in_use", "mac": "aa:bb:cc:dd:ee:01",
                            "port": "/dev/ttyACM0", "held_by": {"job_id": RUN},
                            "health": {"verdict": "failed", "failed": ["test_the_flash_keeps"], "checked_at": "2026-09-13"}}],
                "missing": [], "unregistered": [{"target": "esp32", "mac": "24:6f:28:00:00:99", "port": "/dev/ttyUSB3"}],
                "instruments": [{"id": "io-01", "kind": "esp32-io", "port": "/dev/ttyUSB4", "wiring": [{}, {}]}],
                "probe_errors": [],
            },
            "/api/v1/capacity": {
                "schema": 1, "farm": "esp32-hil", "paused": False, "concurrency": 2, "running": 1, "queued": 0,
                "families": {"esp32-c6": {"connected": 1, "available": 0, "in_use": 1, "available_tags": {}}},
                "missing": 0,
            },
            "/api/v1/jobs": {"total": 1, "counts": {"failed": 1}, "jobs": [_job()]},
            f"/api/v1/jobs/{RUN}": {
                **_job(),
                "report": {"capabilities": {
                    "esp.flash": {"status": "failed", "tests": ["test_esp_health.py::test_the_flash_keeps[esp32-c6-01]"]},
                    "esp.boot": {"status": "validated", "tests": ["test_esp_health.py::test_boots[esp32-c6-01]"]},
                }},
                "bundle": {"id": BUNDLE},
                "artifacts": {"log": {"available": True}, "junit": {"available": True}, "manifest": {"available": False}},
                "log_tail": "x" * 50_000,
            },
            f"/api/v1/jobs/{RUN}/artifacts/log": "\n".join(f"line {n}" for n in range(1, 501)),
            "/api/v1/artifacts": {"matched": 1, "count": 4, "bundles": [{
                "id": BUNDLE, "profile": "canary", "branch": "main", "revision": "a" * 40, "actor": "sparck",
                "families": [{"family": "esp32-c6"}], "bytes": 1024, "created_at": "2026-09-13",
                "source": {"kind": "supplied"}, "pinned": True, "reused_by": [{"id": RUN}],
            }]},
            f"/api/v1/artifacts/{BUNDLE}": {"id": BUNDLE, "profile": "canary", "files": [{"path": f"f{n}"} for n in range(60)],
                                            "reused_by": [{"id": RUN}]},
            "/api/v1/stats": {"window": {"days": 7}, "totals": {"runs": 12}},
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                seen.append(("GET", parsed.path, parse_qs(parsed.query)))
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    return self._send(401, {"error": "bearer token required"})
                body = routes.get(parsed.path)
                if body is None:
                    return self._send(404, {"error": "no such job"})
                return self._send(200, body)

            def do_POST(self):
                seen.append(("POST", self.path, {}))
                self._send(405, {"error": "no"})

            def _send(self, status, body):
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def farm():
    fake = FakeFarm()
    yield fake
    fake.close()


def _call(server, name, arguments=None):
    reply = server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments or {}}})
    return reply["result"]


def test_it_speaks_the_protocol_a_client_expects():
    server = Server(client_factory=lambda: (_ for _ in ()).throw(ValueError("not used")))
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}}})
    result = reply["result"]
    assert reply["id"] == 1 and result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "alteriom-farm" and "Every tool reads" in result["instructions"]
    # A version it does not know gets its newest.
    newer = server.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}})
    assert newer["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"})["result"] == {}
    assert server.handle({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})["error"]["code"] == -32601
    assert server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "farm_delete_everything"}})["error"]["code"] == -32602
    assert server.handle({"id": 6, "method": "ping"})["error"]["code"] == -32600

    tools = server.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})["result"]["tools"]
    assert [tool["name"] for tool in tools] == [tool.name for tool in TOOLS]
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"] is True, tool["name"]
        assert tool["inputSchema"]["type"] == "object" and tool["description"], tool["name"]


def test_every_tool_answers_from_the_farm(farm):
    server = Server(client_factory=lambda: FarmClient(farm.url, KEY))
    whoami = _call(server, "farm_whoami")
    assert whoami["structuredContent"] == {"name": "sparck", "role": "user"}

    status = _call(server, "farm_status")["structuredContent"]
    assert status["health"] == "degraded" and status["health_problems"] == ["backup: no backup"]
    assert status["boards_missing"] == ["esp32-02"] and status["version"] == "1.0.140"
    assert status["recent_runs"][0]["started_by"] == "sparck" and status["recent_runs"][0]["failed_stage"] == "test"

    devices = _call(server, "farm_devices")["structuredContent"]
    assert devices["boards"][0]["canary_failed"] == ["test_the_flash_keeps"] and devices["boards"][0]["held_by"] == RUN
    assert devices["instruments"] == [{"id": "io-01", "kind": "esp32-io", "port": "/dev/ttyUSB4", "wires": 2}]

    capacity = _call(server, "farm_capacity")["structuredContent"]
    assert capacity["families"]["esp32-c6"] == {"connected": 1, "available": 0, "in_use": 1, "available_tags": {}}
    assert capacity["concurrency"] == 2

    runs = _call(server, "farm_runs", {"limit": 5, "status": "failed", "search": "canary"})
    assert runs["structuredContent"]["runs"][0]["id"] == RUN
    assert ("GET", "/api/v1/jobs", {"limit": ["5"], "offset": ["0"], "status": ["failed"], "q": ["canary"]}) in farm.seen

    run = _call(server, "farm_run", {"run_id": RUN})["structuredContent"]
    assert [stage["name"] for stage in run["stages"]] == ["build", "test"]
    assert run["capabilities"] == {"esp.flash": "failed", "esp.boot": "validated"}
    assert run["failed_tests"] == ["test_esp_health.py::test_the_flash_keeps[esp32-c6-01]"]
    assert run["bundle"] == BUNDLE and run["artifacts"] == ["junit", "log"]
    assert "log_tail" not in run, "a run's page is cut to what a question needs"

    log = _call(server, "farm_run_log", {"run_id": RUN, "lines": 3})["structuredContent"]
    assert log["log"] == "line 498\nline 499\nline 500"

    bundles = _call(server, "farm_bundles", {"profile": "canary"})["structuredContent"]
    assert bundles["bundles"][0]["families"] == ["esp32-c6"] and bundles["bundles"][0]["flashed_by_runs"] == 1
    bundle = _call(server, "farm_bundle", {"bundle_id": BUNDLE})["structuredContent"]
    assert len(bundle["files"]) == 50 and bundle["files_not_shown"] == 10 and bundle["runs"] == [RUN]

    assert _call(server, "farm_statistics", {"days": 30})["structuredContent"]["totals"] == {"runs": 12}
    assert ("GET", "/api/v1/stats", {"days": ["30"], "tz_offset_minutes": ["0"]}) in farm.seen
    # Nothing it did asked the farm to change anything.
    assert all(method == "GET" for method, _path, _query in farm.seen)


def test_what_the_farm_refuses_or_a_wrong_argument_is_a_result_not_a_broken_session(farm):
    server = Server(client_factory=lambda: FarmClient(farm.url, KEY))
    for name, arguments, message in (
        ("farm_run", {"run_id": "not-an-id"}, "run_id must be the 32-character id"),
        ("farm_run", {}, "run_id is required"),
        ("farm_runs", {"limit": 500}, "limit must be an integer from 1 to 50"),
        ("farm_runs", {"status": "exploded"}, "status is not a valid value"),
        ("farm_statistics", {"days": 0}, "days must be an integer from 1 to 90"),
        ("farm_status", {"please": "delete"}, "unknown arguments: please"),
        ("farm_run", {"run_id": "f" * 32}, "the farm answered 404: no such job"),
    ):
        result = _call(server, name, arguments)
        assert result["isError"] is True and message in result["content"][0]["text"], (name, arguments)

    refused = Server(client_factory=lambda: FarmClient(farm.url, "afk_" + "0" * 64))
    result = _call(refused, "farm_status")
    assert result["isError"] and "401" in result["content"][0]["text"]
    unconfigured = Server(client_factory=lambda: FarmClient.from_env({}))
    result = _call(unconfigured, "farm_status")
    assert result["isError"] and "set ALTERIOM_FARM_URL" in result["content"][0]["text"]
    down = Server(client_factory=lambda: FarmClient("http://127.0.0.1:9", KEY))
    assert "cannot reach the farm" in _call(down, "farm_whoami")["content"][0]["text"]


def test_the_client_only_reads_and_never_sends_a_key_in_the_clear():
    with pytest.raises(ValueError, match="must be https"):
        FarmClient("http://hil.example.com", KEY)
    FarmClient("https://hil.example.com", KEY)
    FarmClient("http://127.0.0.1:8090", KEY)
    with pytest.raises(ValueError, match="API key"):
        FarmClient("https://hil.example.com", "")
    source = inspect.getsource(farm_client_module)
    assert "method=" not in source and "data=" not in source, "the client builds no request that changes the farm"


def test_the_key_comes_from_a_file(tmp_path):
    key_file = tmp_path / "farm-key"
    key_file.write_text(KEY + "\n")
    client = FarmClient.from_env({"ALTERIOM_FARM_URL": "https://hil.example.com", "ALTERIOM_FARM_KEY_FILE": str(key_file)})
    assert client._key == KEY
    with pytest.raises(ValueError, match="ALTERIOM_FARM_KEY_FILE"):
        FarmClient.from_env({"ALTERIOM_FARM_URL": "https://hil.example.com"})


def test_it_runs_over_stdio_one_message_per_line(farm, tmp_path):
    key_file = tmp_path / "farm-key"
    key_file.write_text(KEY)
    env = {**os.environ, "ALTERIOM_FARM_URL": farm.url, "ALTERIOM_FARM_KEY_FILE": str(key_file),
           "PYTHONPATH": os.pathsep.join([str(REPO / "rig"), str(REPO / "core"), os.environ.get("PYTHONPATH", "")])}
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "farm_runs", "arguments": {"limit": 1}}},
    ]
    stdin = "\n".join(json.dumps(message) for message in messages) + "\nnot json\n"
    done = subprocess.run([sys.executable, "-m", "alteriom_hil.mcp_server"], input=stdin, capture_output=True,
                          text=True, env=env, timeout=60)
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [reply.get("id") for reply in replies] == [1, 2, 3, None], "a notification gets no reply"
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert len(replies[1]["result"]["tools"]) == len(TOOLS)
    assert json.loads(replies[2]["result"]["content"][0]["text"])["runs"][0]["id"] == RUN
    assert replies[3]["error"]["code"] == -32700
    assert done.returncode == 0 and not done.stderr


def test_a_user_key_reads_the_real_service(tmp_path):
    """Against the farm's own handler, not a fake: the key a user is given is
    enough for every read the tools make."""
    # The launcher, a console script now (docs/public-release-plan.md, 12e).
    from alteriom_hil import launcher as farm_service
    token_file = tmp_path / "api-token"
    token_file.write_text("t" * 64)
    entries, user_key = add_key([], "assistant", "user")
    keys_path_for(token_file).write_text(keys_document(entries))
    manager = farm_service.FarmManager.__new__(farm_service.FarmManager)
    manager.state = tmp_path
    (tmp_path / "logs").mkdir()
    manager.store = farm_service.JobStore(tmp_path / "farm.sqlite3")
    job = manager.store.create("suite", {"profile": "canary", "submitted_by": "assistant"}, tmp_path / "logs" / "x.log")
    manager.store.update(job["id"], "failed", {"summary": "it broke", "failed_stage": "flash"})
    (tmp_path / "logs" / f"{job['id']}.log").write_text("first\nsecond\n")
    server = ThreadingHTTPServer(("127.0.0.1", 0), farm_service.make_handler(manager, KeyStore("t" * 64, keys_path_for(token_file)), tmp_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        tools = Server(client_factory=lambda: FarmClient(f"http://127.0.0.1:{server.server_address[1]}", user_key))
        assert _call(tools, "farm_whoami")["structuredContent"] == {"name": "assistant", "role": "user"}
        runs = _call(tools, "farm_runs")["structuredContent"]["runs"]
        assert runs[0]["id"] == job["id"] and runs[0]["failed_stage"] == "flash" and runs[0]["started_by"] == "assistant"
        assert _call(tools, "farm_run_log", {"run_id": job["id"]})["structuredContent"]["log"] == "first\nsecond"
    finally:
        server.shutdown()
        server.server_close()


def test_the_documentation_names_every_tool():
    doc = (REPO / "docs" / "mcp.md").read_text(encoding="utf-8")
    for tool in TOOLS:
        assert f"`{tool.name}`" in doc, tool.name
    assert "alteriom-farm-mcp" in (REPO / "core" / "pyproject.toml").read_text(encoding="utf-8")
