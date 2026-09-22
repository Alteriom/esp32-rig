"""A client for the farm's API: what the MCP server, and any script, reads the
farm through.

The first piece of the SDK the device platform plan describes. It is thin on
purpose -- one method per route, the farm's own JSON back, an error that says
what the farm said -- because the API is the contract and a client that
re-shaped it would be a second contract to keep.

A key is a named API key (``alteriom-hil-admin keys create``); a ``user`` key
reads everything this exposes. Nothing here changes the farm.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

USER_AGENT = "alteriom-farm-client"


class FarmError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        self.status = status
        super().__init__(message)


class FarmClient:
    def __init__(self, base_url: str, key: str, timeout: float = 30.0):
        parsed = urlparse(base_url)
        loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            # A key sent over plain http to another host is a key given away.
            raise ValueError("the farm URL must be https (http only to loopback)")
        if not key:
            raise ValueError("an API key is required")
        self.base_url = base_url.rstrip("/")
        self._key = key
        self.timeout = timeout

    @classmethod
    def from_env(cls, env=None) -> "FarmClient":
        """ALTERIOM_FARM_URL, and ALTERIOM_FARM_KEY or ALTERIOM_FARM_KEY_FILE."""
        env = os.environ if env is None else env
        url = env.get("ALTERIOM_FARM_URL")
        if not url:
            raise ValueError("set ALTERIOM_FARM_URL to the farm, e.g. https://hil.example.com")
        key = env.get("ALTERIOM_FARM_KEY") or ""
        key_file = env.get("ALTERIOM_FARM_KEY_FILE")
        if not key and key_file:
            key = Path(key_file).expanduser().read_text(encoding="utf-8").strip()
        if not key:
            raise ValueError("set ALTERIOM_FARM_KEY_FILE (or ALTERIOM_FARM_KEY) to a farm API key")
        return cls(url, key)

    def _get(self, path: str, params: dict | None = None, raw: bool = False):
        query = {key: value for key, value in (params or {}).items() if value not in (None, "")}
        url = self.base_url + path + (f"?{urlencode(query)}" if query else "")
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._key}", "User-Agent": USER_AGENT, "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error") or detail
            except (json.JSONDecodeError, AttributeError):
                pass
            raise FarmError(exc.code, f"the farm answered {exc.code}: {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise FarmError(None, f"cannot reach the farm at {self.base_url}: {getattr(exc, 'reason', exc)}") from None
        if raw:
            return body.decode("utf-8", "replace")
        return json.loads(body or b"null")

    # ---- one method per route ------------------------------------------------

    def whoami(self) -> dict:
        return self._get("/api/v1/whoami")

    def status(self) -> dict:
        return self._get("/api/v1/status")

    def inventory(self) -> dict:
        return self._get("/api/v1/inventory")

    def capacity(self) -> dict:
        return self._get("/api/v1/capacity")

    def jobs(self, limit: int = 25, offset: int = 0, status: str | None = None,
             kind: str | None = None, search: str | None = None) -> dict:
        return self._get("/api/v1/jobs", {"limit": limit, "offset": offset, "status": status, "kind": kind, "q": search})

    def job(self, job_id: str) -> dict:
        return self._get(f"/api/v1/jobs/{quote(job_id, safe='')}")

    def job_log(self, job_id: str) -> str:
        return self._get(f"/api/v1/jobs/{quote(job_id, safe='')}/artifacts/log", raw=True)

    def bundles(self, limit: int = 25, offset: int = 0, profile: str | None = None, search: str | None = None) -> dict:
        return self._get("/api/v1/artifacts", {"limit": limit, "offset": offset, "profile": profile, "q": search})

    def bundle(self, bundle_id: str) -> dict:
        return self._get(f"/api/v1/artifacts/{quote(bundle_id, safe='')}")

    def statistics(self, days: int = 7, tz_offset_minutes: int = 0) -> dict:
        return self._get("/api/v1/stats", {"days": days, "tz_offset_minutes": tz_offset_minutes})
