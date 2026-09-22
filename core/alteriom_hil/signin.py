"""Signing in: a person becomes an account, by GitHub or by their email.

The farm has known two kinds of caller: a key, for a program, and a person
behind Authelia, for the dashboard. Opening the farm to everyone needs a
third: an account the portal owns, made in one of two ways and the same
account either way (docs/farm-service.md, "Accounts").

* **GitHub.** The developer whose firmware CI already lives there signs in
  with one click. The code GitHub hands back is exchanged for who they are:
  their id, their login, and their primary verified email if they have one.
* **Email.** Everyone else types an address and is sent a link that signs
  them in once, within the half hour. Following it is also verification.

No password: it would be a third thing to store, rotate and reset for no
user we can name.

This module is the part that talks to GitHub and to NorthRelay, kept apart
from the service so that it is small, read in one sitting, and tested with
a fake `fetch`. Nothing here touches the database: the service owns the
accounts, the sessions and the links; this owns the conversations.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import urllib.parse
import urllib.request
from dataclasses import dataclass

USER_AGENT = "Alteriom-ESP32-Farm/1.0"
GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"
# What an account is called: the same shape as a key's name, so that a rig
# owned by a handle is owned the way a rig owned by a key's name is.
HANDLE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}\Z")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+\Z")


class SignInError(Exception):
    """The other side did not say what it had to; the person sees "try again"."""


def new_token() -> str:
    """A session or a link: 256 random bits, URL-safe. Stored only as its digest."""
    return secrets.token_urlsafe(32)


def new_code() -> str:
    """Six digits, from the system's randomness, zero-padded: what a person
    types back from a mail. Short on purpose -- it is bound to the browser
    that asked and dies after a handful of wrong guesses, so its strength is
    in those, not in its length (farm_service.SIGNIN_CODE_ATTEMPTS)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def code_digest(email: str, code: str) -> str:
    """A code is only meaningful with the address it was sent to, so the two
    are digested together: the same six digits mailed to two people are two
    different secrets, and a table of digests says nothing on its own."""
    return digest(f"{email.strip().lower()}:{code}")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_email(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 254 and bool(EMAIL.fullmatch(value.strip()))


def handle_from(*candidates: object) -> str:
    """A handle from the first usable candidate: a GitHub login, an email's
    local part. Lower-cased, anything else made a dash, cut to 32. The
    service makes it unique; this makes it a handle."""
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        made = re.sub(r"[^a-z0-9._-]+", "-", candidate.strip().lower()).strip("-._")[:32]
        if made and HANDLE.fullmatch(made):
            return made
    return "someone"


def http_json(url: str, *, method: str = "GET", body: dict | None = None,
              headers: dict | None = None, timeout: float = 15.0) -> object:
    """One JSON conversation with a host we chose (GitHub, NorthRelay)."""
    data = None
    sent = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        sent["Content-Type"] = "application/json"
    sent.update(headers or {})
    request = urllib.request.Request(url, data=data, method=method, headers=sent)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 -- fixed https hosts
        raw = response.read()
    return json.loads(raw) if raw else {}


@dataclass(frozen=True)
class GitHubApp:
    """The OAuth app the farm is registered as with GitHub."""

    client_id: str
    client_secret: str

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        """Where to send the browser. `state` comes back with the code and
        is what stops a code meant for somebody else from being used."""
        return GITHUB_AUTHORIZE + "?" + urllib.parse.urlencode({
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": "read:user user:email",
            "allow_signup": "true",
        })

    def user(self, code: str, redirect_uri: str, fetch=http_json) -> dict:
        """Who GitHub says this code is: `{id, login, email, name}`, the
        email their primary verified one or None. The access token is used
        for the two reads and then forgotten: the farm keeps no way back
        into anybody's GitHub."""
        answer = fetch(GITHUB_TOKEN, method="POST", body={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "code": code, "redirect_uri": redirect_uri,
        })
        token = answer.get("access_token") if isinstance(answer, dict) else None
        if not token:
            why = (answer.get("error_description") or answer.get("error")) if isinstance(answer, dict) else None
            raise SignInError(f"GitHub did not exchange the code: {why or 'no token in its answer'}")
        auth = {"Authorization": f"Bearer {token}"}
        person = fetch(f"{GITHUB_API}/user", headers=auth)
        if not isinstance(person, dict) or not isinstance(person.get("id"), int) or not person.get("login"):
            raise SignInError("GitHub did not say who this is")
        email = None
        try:
            emails = fetch(f"{GITHUB_API}/user/emails", headers=auth)
        except Exception:  # a login without the email scope still signs in
            emails = []
        for item in emails if isinstance(emails, list) else []:
            if isinstance(item, dict) and item.get("verified") and item.get("primary") and valid_email(item.get("email")):
                email = item["email"].strip().lower()
                break
        return {"id": person["id"], "login": str(person["login"]), "email": email,
                "name": person.get("name") if isinstance(person.get("name"), str) else None}


@dataclass(frozen=True)
class Mailer:
    """NorthRelay, the platform's mail: one account, one verified sender, a
    template by its id, flat string variables. The farm never assembles
    HTML; the brand and the words are NorthRelay's (docs/farm-service.md,
    "Accounts")."""

    api_key: str
    sender: str
    base_url: str = "https://app.northrelay.ca"
    sender_name: str = "Alteriom ESP32 Farm"

    def send_template(self, template_id: str, to: str, variables: dict, *,
                      tags: dict | None = None, fetch=http_json) -> dict:
        body = {
            "from": {"email": self.sender, "name": self.sender_name},
            "to": [{"email": to}],
            "content": {"templateId": template_id},
            # NorthRelay takes strings only, and refuses a template whose
            # variables are not all given: a missing one is our bug, said
            # at send time rather than as a blank in somebody's mail.
            "variables": {str(key): str(value) for key, value in variables.items()},
            "tags": {str(key): str(value) for key, value in (tags or {}).items()},
        }
        answer = fetch(f"{self.base_url}/api/v1/emails/send", method="POST", body=body,
                       headers={"Authorization": f"Bearer {self.api_key}"})
        if not isinstance(answer, dict) or not answer.get("success", True):
            error = (answer.get("error") or {}) if isinstance(answer, dict) else {}
            raise SignInError(f"the mail was not accepted: {error.get('message') or answer}")
        return answer
