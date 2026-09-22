"""The farm's email templates: assembled from their words and the shared
shell, checked against what the farm sends, and put into NorthRelay by
name -- with a fake on the other end, so what is asserted is what would be
sent and what would be refused, not that the network works."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("northrelay_templates", REPO / "runner" / "northrelay_templates.py")
tool = importlib.util.module_from_spec(spec)
sys.modules["northrelay_templates"] = tool
spec.loader.exec_module(tool)


def test_every_template_is_built_in_the_shell_and_names_only_what_the_farm_sends():
    """NorthRelay refuses a message whose template names a variable that was
    not sent -- at send time, in somebody's sign-in. The check runs here
    instead, and the other way too: a variable the farm sends and the
    template ignores is a template that says less than it could."""
    built = {item["name"]: item for item in tool.build()}
    assert "farm-signin-code" in built and "farm-signin-link" not in built
    signin = built["farm-signin-code"]
    assert signin["variables"] == ["code", "expires_minutes", "farm"]
    assert signin["category"] == "TRANSACTIONAL"
    assert "{{theme.logo_url}}" in signin["html"] and "{{theme.primary_color}}" in signin["html"]
    assert ">{{code}}<" in signin["html"] and "{{expires_minutes}} minutes" in signin["html"]
    assert "{{code}}" in signin["subject"], "the code is in the subject: a notification is enough to read it"
    assert "href=\"{{" not in signin["html"].replace('href="{{farm}}"', ""), "nothing to follow: the code is typed"
    assert "Your code is {{code}}" in signin["html"], "the preheader is in the shell"
    assert "<!-- preheader" not in signin["html"], "and not left as a comment"
    assert "unsubscribe" not in signin["html"].lower() or "nothing to unsubscribe from" in signin["html"]
    # The text part says the same thing, with the code in it.
    assert "{{code}}" in signin["text"] and "<" not in signin["text"]
    for item in built.values():
        assert item["category"] in tool.CATEGORIES, item["name"]
        assert item["html"].startswith("<!doctype html>"), item["name"]


def test_a_template_that_would_fail_at_send_time_fails_here(tmp_path):
    shell = (tool.EMAIL_DIR / "_shell.html").read_text(encoding="utf-8")
    (tmp_path / "_shell.html").write_text(shell, encoding="utf-8")
    (tmp_path / "bad.html").write_text("<p>Hello {{first_name}}, see {{link}}</p>", encoding="utf-8")
    (tmp_path / "templates.json").write_text(json.dumps({"templates": [
        {"name": "farm-bad", "file": "bad.html", "subject": "x", "category": "TRANSACTIONAL", "variables": ["link"]},
    ]}), encoding="utf-8")
    with pytest.raises(tool.TemplateError, match=r"names \['first_name'\], which the farm does not send"):
        tool.build(tmp_path)
    (tmp_path / "templates.json").write_text(json.dumps({"templates": [
        {"name": "farm-bad", "file": "bad.html", "subject": "x", "category": "MARKETING",
         "variables": ["link", "first_name"]},
    ]}), encoding="utf-8")
    with pytest.raises(tool.TemplateError, match="unsubscribe footer"):
        tool.build(tmp_path)


def test_sync_creates_what_is_missing_and_updates_what_exists_by_name():
    calls = []

    def fake(url, *, method="GET", body=None, headers=None, timeout=15.0):
        calls.append((method, url, body))
        assert headers == {"Authorization": "Bearer nr_live_k"}
        if method == "GET":
            # Two pages: the farm's template sits on the second, where a
            # one-page lookup would not find it and would create a twin.
            if url.endswith("page=1"):
                return {"success": True, "data": {"templates": [
                    {"id": "cuid-other", "name": "Welcome Series - Day 1"}], "page": 1, "has_more": True}}
            return {"success": True, "data": {"templates": [
                {"id": "cuid-signin", "name": "farm-signin-code"}], "page": 2, "has_more": False}}
        if method == "POST":
            return {"success": True, "data": {"id": f"cuid-new-{body['name']}"}}
        return {"success": True, "data": {"id": url.rsplit('/', 1)[1]}}

    built = [item for item in tool.build() if item["name"] in ("farm-signin-code", "farm-welcome")]
    ids = tool.sync(built, "nr_live_k", "https://mail.example", theme_id="theme-1", fetch=fake)
    assert ids == {"farm-signin-code": "cuid-signin", "farm-welcome": "cuid-new-farm-welcome"}
    methods = [(method, url.replace("https://mail.example", "")) for method, url, _ in calls]
    assert methods == [("GET", "/api/v1/templates?limit=100&page=1"),
                       ("GET", "/api/v1/templates?limit=100&page=2"),
                       ("PATCH", "/api/v1/templates/cuid-signin"),
                       ("POST", "/api/v1/templates")]
    patched = calls[2][2]
    assert patched["themeId"] == "theme-1" and patched["category"] == "TRANSACTIONAL" and "name" not in patched
    posted = calls[3][2]
    assert posted["name"] == "farm-welcome" and posted["subject"] == "Welcome to the farm" and posted["text"]
    # A dry run reads and never writes.
    calls.clear()
    tool.sync(built, "nr_live_k", "https://mail.example", dry_run=True, fetch=fake)
    assert [method for method, _, _ in calls] == ["GET", "GET"]
