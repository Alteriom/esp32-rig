"""A rig's access to GitHub: the token it holds, and what it does with it.

GitHub is where a project lives and where its CI builds the firmware this
rig flashes, so a rig without a token can add no project: it could neither
check out the repository at a run nor take a bundle from the workflow that
built it. The token is one file, `/etc/alteriom-hil/consumer-token`, the
same one `git` is answered with when a run clones a private repository
(RigMixin._clone_credentials), provisioned with `alteriom-hil-admin github
set`. It is read here and never returned: the rig says who the token is
(`login`), never what it is.

What this module does with it: asks GitHub who the token is, whether a
repository can be read with it, and fetches the newest bundle a project's
supply workflow uploaded -- the Actions artifact, by name -- so a rig on a
LAN, which no CI can reach, still gets its firmware from the CI that built
it. Every call is a small, fixed conversation with api.github.com.
"""
from __future__ import annotations

import calendar
import hashlib
import io
import json
import re
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from alteriom_hil.signin import GITHUB_API, USER_AGENT, http_json

API_VERSION = "2022-11-28"
STATUS_TTL = 600            # who the token is, kept this long
STATUS_RETRY = 60           # a failed ask is not repeated sooner
ARTIFACT_LIMIT = 256 * 1024 * 1024   # a bundle zip larger than this is not a bundle
REPO_URL = re.compile(r"https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")

SET_COMMAND = "sudo alteriom-hil-admin github set"


class GitHubError(ValueError):
    """GitHub answered, and the answer is no: said in the words of the
    request that was refused, never with the token."""


def read_token(path: Path) -> str | None:
    """The token, or None when there is none. Unreadable is an error the
    caller says in its own words: a file that exists and cannot be read is
    the most common way a valid token fails."""
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": API_VERSION,
            "Accept": "application/vnd.github+json"}


def _ask(url: str, token: str, timeout: float = 15.0):
    try:
        return http_json(url, headers=_headers(token), timeout=timeout)
    except urllib.error.HTTPError as error:
        raise GitHubError(_refusal(error, url)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise GitHubError(f"GitHub could not be reached: {error.__class__.__name__}: {error}"[:200]) from None


def _ask_full(url: str, token: str, timeout: float = 15.0):
    """One GET answered as (body, headers). The headers are where GitHub
    says what it knows about the token itself -- when it expires, what a
    classic one is scoped to -- so /user is asked this way."""
    request = urllib.request.Request(url, headers={**_headers(token), "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 -- api.github.com
            raw = response.read()
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
    except urllib.error.HTTPError as error:
        raise GitHubError(_refusal(error, url)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise GitHubError(f"GitHub could not be reached: {error.__class__.__name__}: {error}"[:200]) from None
    return (json.loads(raw) if raw else {}), headers


# What a token is, by the prefix GitHub gives each kind. A fine-grained token
# is the one a rig wants: it expires, and it reaches the repositories it was
# given and no other -- which is also why a project can be refused with it.
TOKEN_KINDS = (("github_pat_", "fine-grained"), ("ghp_", "classic"), ("gho_", "oauth"),
               ("ghu_", "app-user"), ("ghs_", "app-installation"))
EXPIRY = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")


def token_kind(token: str) -> str:
    for prefix, kind in TOKEN_KINDS:
        if str(token or "").startswith(prefix):
            return kind
    return "unknown"


def token_expiry(headers: dict) -> tuple:
    """(when, days left) from the expiration header GitHub sends with every
    answer to a token that expires; (None, None) for one that does not."""
    match = EXPIRY.search(str(headers.get("github-authentication-token-expiration") or ""))
    if not match:
        return None, None
    when = f"{match.group(1)}T{match.group(2)}Z"
    try:
        stamp = calendar.timegm(time.strptime(when, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None, None
    return when, int((stamp - time.time()) // 86400)


def _refusal(error: urllib.error.HTTPError, url: str) -> str:
    what = url.replace(GITHUB_API, "")
    if error.code == 401:
        return "GitHub refused the token (401): it is wrong, expired or revoked"
    if error.code == 403:
        return f"GitHub refused {what} (403): the token has no access to it, or the rate limit is spent"
    if error.code == 404:
        return f"GitHub has no {what} for this token (404): a private repository the token cannot read looks like this too"
    return f"GitHub answered {error.code} for {what}"


def parse_repo(url: str) -> tuple[str, str]:
    """`owner, repo` from a github.com repository URL, or a GitHubError
    saying what a project's repository has to be."""
    match = REPO_URL.fullmatch(str(url or "").strip())
    if not match:
        raise GitHubError("a project's repository is a github.com URL, as https://github.com/<owner>/<repository>")
    return match.group("owner"), match.group("repo")


def normalise_repo(url: str) -> str:
    owner, repo = parse_repo(url)
    return f"https://github.com/{owner}/{repo}"


def whoami(token: str) -> dict:
    """Who the token is -- the login GitHub answers /user with -- and what
    it is: its kind, when it expires, and a classic token's scopes, all read
    from that one answer. Never the token."""
    answer, headers = _ask_full(f"{GITHUB_API}/user", token)
    if not isinstance(answer, dict) or not answer.get("login"):
        raise GitHubError("GitHub answered /user with no login")
    expires_at, expires_in_days = token_expiry(headers)
    scopes = [part.strip() for part in str(headers.get("x-oauth-scopes") or "").split(",") if part.strip()]
    return {"login": str(answer["login"]), "type": str(answer.get("type") or ""), "kind": token_kind(token),
            "expires_at": expires_at, "expires_in_days": expires_in_days, "scopes": scopes}


REACH_PAGE = 100


def reachable_repositories(token: str) -> dict:
    """The repositories the token can see, as GitHub lists them for it. A
    fine-grained token restricted to selected repositories lists exactly
    those -- what a person needs to see when adding a project is refused. A
    classic token lists everything its user can, so one page is asked for
    and `more` says there are others."""
    answer = _ask(f"{GITHUB_API}/user/repos?per_page={REACH_PAGE}&sort=full_name"
                  f"&affiliation=owner,collaborator,organization_member", token)
    rows = answer if isinstance(answer, list) else []
    repositories = [{"name": str(row["full_name"]), "private": bool(row.get("private"))}
                    for row in rows if isinstance(row, dict) and row.get("full_name")]
    return {"repositories": repositories, "more": len(rows) >= REACH_PAGE}


def repository_access(token: str, repo_url: str) -> dict:
    """What the token may do with one repository, asked the way the rig uses
    it: see it at all (metadata), read its code (Contents, for the checkout),
    list its Actions artifacts (Actions, for the bundles). Each is a small
    request GitHub answers or refuses; the refusals are kept in words."""
    owner, repo = parse_repo(repo_url)
    base = f"{GITHUB_API}/repos/{owner}/{repo}"
    result = {"repo": f"https://github.com/{owner}/{repo}", "metadata": False, "contents": False,
              "actions": False, "private": None, "error": None, "refused": {}, "description": None}
    try:
        seen = _ask(base, token)
    except GitHubError as error:
        result["error"] = str(error)
        return result
    result["metadata"] = True
    result["private"] = bool(seen.get("private")) if isinstance(seen, dict) else None
    # What the repository says it is, for the library's card; nothing else of it.
    result["description"] = (str(seen.get("description") or "").strip()[:200] or None) if isinstance(seen, dict) else None
    for key, url in (("contents", f"{base}/contents/"), ("actions", f"{base}/actions/artifacts?per_page=1")):
        try:
            _ask(url, token)
            result[key] = True
        except GitHubError as error:
            result["refused"][key] = str(error)
    return result


def repository(token: str, url: str) -> dict:
    """The repository as the token sees it: its default branch, whether it
    is private, and its canonical URL. A repository the token cannot read
    is a GitHubError that says so."""
    owner, repo = parse_repo(url)
    answer = _ask(f"{GITHUB_API}/repos/{owner}/{repo}", token)
    if not isinstance(answer, dict) or not answer.get("full_name"):
        raise GitHubError(f"GitHub answered for {owner}/{repo} with no repository")
    return {
        "url": f"https://github.com/{answer['full_name']}",
        "default_branch": str(answer.get("default_branch") or "main"),
        "private": bool(answer.get("private")),
        "archived": bool(answer.get("archived")),
    }


def contents(token: str, repo_url: str, path: str, ref: str | None = None):
    """A file or a directory listing from the repository, as the contents
    API gives it: a dict for a file (base64 body), a list for a directory,
    None when there is no such path. Anything else is a GitHubError."""
    import base64
    owner, repo = parse_repo(repo_url)
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{urllib.parse.quote(path.strip('/'))}"
    if ref:
        url += f"?ref={urllib.parse.quote(ref)}"
    try:
        answer = http_json(url, headers=_headers(token), timeout=15)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise GitHubError(_refusal(error, url)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise GitHubError(f"GitHub could not be reached: {error.__class__.__name__}: {error}"[:200]) from None
    if isinstance(answer, dict) and answer.get("type") == "file" and answer.get("encoding") == "base64":
        try:
            answer["text"] = base64.b64decode(answer.get("content") or "").decode("utf-8", "replace")
        except (ValueError, TypeError):
            answer["text"] = ""
    return answer


SUITE_CANDIDATES = ("hil/tests", "tests/hil", "test/hil", "hil", "tests", "test")


def look_around(token: str, repo_url: str) -> dict:
    """What the repository tells about itself, for a form to be filled in:
    its default branch; its `.alteriom-hil.yaml` when it has one (then the
    project is described there); else the directory that looks like the
    suite and the workflow that looks like the HIL build. Guesses are
    marked as guesses."""
    seen = repository(token, repo_url)
    ref = seen["default_branch"]
    found: dict = {"repo": seen["url"], "default_ref": ref, "private": seen["private"], "found": [], "guessed": []}
    described = contents(token, seen["url"], ".alteriom-hil.yaml", ref)
    if isinstance(described, dict) and described.get("text"):
        found["declared"] = described["text"]
        found["found"].append(".alteriom-hil.yaml")
        try:
            import yaml
            doc = yaml.safe_load(described["text"]) or {}
        except Exception:  # noqa: BLE001 -- a document the project wrote; say it, do not crash
            doc = {}
        if isinstance(doc, dict):
            suite = doc.get("suite") or {}
            supply = doc.get("supply") or {}
            if isinstance(suite, dict) and suite.get("path"):
                found["suite_path"] = str(suite["path"])
            if doc.get("label"):
                found["label"] = str(doc["label"])
            build = doc.get("build") or {}
            if isinstance(build, dict) and build.get("revision_key"):
                found["revision_key"] = str(build["revision_key"])
            if isinstance(supply, dict):
                if supply.get("workflow"):
                    found["supply_workflow"] = str(supply["workflow"])
                if supply.get("artifact"):
                    found["supply_artifact"] = str(supply["artifact"])
            needs = doc.get("needs")
            if isinstance(needs, list):
                found["families"] = [str(n.get("target")) for n in needs if isinstance(n, dict) and n.get("target")]
    if "suite_path" not in found:
        for candidate in SUITE_CANDIDATES:
            listing = contents(token, seen["url"], candidate, ref)
            if isinstance(listing, list) and any(isinstance(e, dict) and str(e.get("name", "")).startswith("test_") for e in listing):
                found["suite_path"] = candidate
                found["guessed"].append(f"suite_path: {candidate} (has test_*.py)")
                break
    if "supply_workflow" not in found:
        listing = contents(token, seen["url"], ".github/workflows", ref)
        names = [str(e.get("name")) for e in listing if isinstance(e, dict)] if isinstance(listing, list) else []
        found["workflows"] = sorted(names)
        pick = next((n for n in sorted(names) if "hil" in n.lower()), None) or next((n for n in sorted(names) if "firmware" in n.lower() or "build" in n.lower()), None)
        if pick:
            found["supply_workflow"] = f".github/workflows/{pick}"
            found["guessed"].append(f"supply_workflow: {pick}")
    return found


class Status:
    """Who this rig is to GitHub, asked rarely and never guessed.

    One per manager. The answer is kept STATUS_TTL and keyed by a digest of
    the token, so replacing the file is noticed at the next ask; a failure
    is kept STATUS_RETRY so a page polling the rig does not turn into a
    poll of GitHub."""

    def __init__(self, path: Path):
        self.path = path
        self._held: dict | None = None
        self._key: str | None = None
        self._at = 0.0

    def token(self) -> str | None:
        return read_token(self.path)

    def view(self, ask=None) -> dict:
        """{configured, connected, login, path, error, checked_at, how}."""
        ask = ask or whoami   # looked up now, so a test's stand-in for GitHub is the one asked
        try:
            token = self.token()
        except OSError as error:
            return self._answer(configured=True, connected=False, login=None,
                                error=f"{self.path} exists but cannot be read by the service: {error.__class__.__name__}. "
                                      f"Give it the API token's owner, group and mode (0640).")
        if not token:
            self._held = None
            return self._answer(configured=False, connected=False, login=None, error=None,
                                kind=None, expires_at=None, expires_in_days=None, scopes=[])
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        if self._held is not None and self._key == key and now - self._at < (
                STATUS_TTL if self._held["connected"] else STATUS_RETRY):
            return self._held
        try:
            who = ask(token)
            answer = self._answer(configured=True, connected=True, login=who["login"], error=None,
                                  kind=who.get("kind") or token_kind(token), expires_at=who.get("expires_at"),
                                  expires_in_days=who.get("expires_in_days"), scopes=list(who.get("scopes") or []))
        except GitHubError as error:
            answer = self._answer(configured=True, connected=False, login=None, error=str(error),
                                  kind=token_kind(token), expires_at=None, expires_in_days=None, scopes=[])
        self._held, self._key, self._at = answer, key, now
        return answer

    def _answer(self, **fields) -> dict:
        return {**fields, "path": str(self.path), "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "how": SET_COMMAND}

    def forget(self) -> None:
        self._held = None


# ---- the newest bundle a project's CI built -----------------------------------------

def newest_artifact(token: str, repo_url: str, artifact_name: str, workflow_path: str) -> dict:
    """The newest Actions artifact of that name whose run is the project's
    supply workflow, with what accept_bundle needs to know about the run.

    Artifacts are listed newest first; each names its run, and the run names
    its workflow file, its commit, its branch and who started it. Expired
    artifacts are skipped: GitHub keeps them 90 days by default and still
    lists them."""
    owner, repo = parse_repo(repo_url)
    listing = _ask(f"{GITHUB_API}/repos/{owner}/{repo}/actions/artifacts?name={urllib.parse.quote(artifact_name)}&per_page=20", token)
    artifacts = listing.get("artifacts") if isinstance(listing, dict) else None
    if not artifacts:
        raise GitHubError(f"{owner}/{repo} has no Actions artifact named {artifact_name!r}: has the supply workflow run and uploaded one?")
    seen_runs: dict = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("expired"):
            continue
        run = artifact.get("workflow_run") or {}
        run_id = str(run.get("id") or "")
        if not run_id.isdigit():
            continue
        if run_id not in seen_runs:
            seen_runs[run_id] = _ask(f"{GITHUB_API}/repos/{owner}/{repo}/actions/runs/{run_id}", token)
        details = seen_runs[run_id]
        if str(details.get("path") or "") != workflow_path:
            continue
        if str(details.get("conclusion") or "") not in ("success", ""):
            continue
        return {
            "artifact_id": str(artifact.get("id")),
            "artifact_name": str(artifact.get("name") or artifact_name),
            "size": int(artifact.get("size_in_bytes") or 0),
            "download_url": str(artifact.get("archive_download_url") or ""),
            "run_id": run_id,
            "run_url": str(details.get("html_url") or ""),
            "commit": str(details.get("head_sha") or "").lower(),
            "branch": str(details.get("head_branch") or "") or None,
            "actor": str(((details.get("actor") or {}).get("login")) or "") or None,
            "created_at": str(artifact.get("created_at") or ""),
            "repo": f"https://github.com/{owner}/{repo}",
        }
    raise GitHubError(f"{owner}/{repo} has artifacts named {artifact_name!r}, but none from a successful run of {workflow_path}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 -- urllib's hook
        return None


def download_artifact(token: str, url: str, limit: int = ARTIFACT_LIMIT) -> bytes:
    """The artifact zip. GitHub answers the download with a redirect to
    storage; the token goes to GitHub and not to wherever that is."""
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={**_headers(token), "User-Agent": USER_AGENT})
    try:
        try:
            with opener.open(request, timeout=30) as response:
                location = None
                body = response.read(limit + 1)
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308) and error.headers.get("Location"):
                location = error.headers["Location"]
                body = b""
            else:
                raise GitHubError(_refusal(error, url)) from None
        if location:
            plain = urllib.request.Request(location, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(plain, timeout=120) as response:  # noqa: S310 -- where GitHub sent us
                body = response.read(limit + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise GitHubError(f"the artifact could not be downloaded: {error.__class__.__name__}: {error}"[:200]) from None
    if len(body) > limit:
        raise GitHubError(f"the artifact is larger than {limit // (1024 * 1024)} MB, which no bundle is")
    return body


def tarball_from_zip(zipped: bytes, top: str = "bundle", limit: int = ARTIFACT_LIMIT) -> bytes:
    """An Actions artifact is a zip of the directory that was uploaded; the
    rig takes bundles as one tar.gz with a single top directory. Regular
    files only, sizes bounded, paths kept relative."""
    out = io.BytesIO()
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(zipped)) as archive, tarfile.open(fileobj=out, mode="w:gz") as tar:
            names = [info for info in archive.infolist() if not info.is_dir()]
            if not names:
                raise GitHubError("the artifact zip is empty")
            if not any(Path(info.filename).name == "manifest.json" for info in names):
                raise GitHubError("the artifact holds no manifest.json: it is not a bundle a rig can flash")
            for info in names:
                name = info.filename.replace("\\", "/").lstrip("/")
                if not name or ".." in name.split("/"):
                    raise GitHubError(f"the artifact names a path outside itself: {info.filename!r}")
                total += info.file_size
                if total > limit:
                    raise GitHubError("the artifact unpacks to more than a bundle can be")
                data = archive.read(info)
                member = tarfile.TarInfo(f"{top}/{name}")
                member.size = len(data)
                member.mode = 0o644
                member.mtime = int(time.time())
                tar.addfile(member, io.BytesIO(data))
    except zipfile.BadZipFile:
        raise GitHubError("the artifact is not a zip GitHub would have made") from None
    return out.getvalue()


def describe(fetched: dict) -> str:
    return json.dumps({k: fetched.get(k) for k in ("artifact_id", "run_id", "commit", "branch", "created_at")}, sort_keys=True)
