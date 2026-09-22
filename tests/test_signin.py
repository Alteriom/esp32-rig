"""Signing in: the conversations with GitHub and with NorthRelay, with a fake
on the other end. What is asserted is what the farm sends and what it makes
of the answer -- the state that stops a stolen code, the verified email
that joins a GitHub person to the one who signed in by mail, the flat
string variables NorthRelay insists on -- not that the network works.
"""

import pytest

from alteriom_hil import signin


def test_github_is_asked_with_a_state_and_answers_with_who_this_is():
    app = signin.GitHubApp("id-123", "secret-xyz")
    url = app.authorize_url("st4te", "https://farm.example/auth/github/callback")
    assert url.startswith(signin.GITHUB_AUTHORIZE + "?")
    assert "client_id=id-123" in url and "state=st4te" in url
    assert "redirect_uri=https%3A%2F%2Ffarm.example%2Fauth%2Fgithub%2Fcallback" in url
    assert "scope=read%3Auser+user%3Aemail" in url
    assert "secret" not in url, "the secret never goes through the browser"

    calls = []

    def fake(url, *, method="GET", body=None, headers=None, timeout=15.0):
        calls.append((method, url, body, headers))
        if url == signin.GITHUB_TOKEN:
            assert body == {"client_id": "id-123", "client_secret": "secret-xyz", "code": "c0de",
                            "redirect_uri": "https://farm.example/auth/github/callback"}
            return {"access_token": "gho_abc", "token_type": "bearer"}
        assert headers == {"Authorization": "Bearer gho_abc"}
        if url == signin.GITHUB_API + "/user":
            return {"id": 4242, "login": "Ada-Lovelace", "name": "Ada"}
        if url == signin.GITHUB_API + "/user/emails":
            return [{"email": "old@example.org", "verified": True, "primary": False},
                    {"email": "Ada@Example.org", "verified": True, "primary": True},
                    {"email": "unverified@example.org", "verified": False, "primary": False}]
        raise AssertionError(url)

    person = app.user("c0de", "https://farm.example/auth/github/callback", fetch=fake)
    assert person == {"id": 4242, "login": "Ada-Lovelace", "email": "ada@example.org", "name": "Ada"}
    assert [call[0] for call in calls] == ["POST", "GET", "GET"]


def test_a_code_github_will_not_exchange_is_an_error_not_a_person():
    app = signin.GitHubApp("id", "secret")

    def refused(url, **kwargs):
        return {"error": "bad_verification_code", "error_description": "The code passed is incorrect or expired."}

    with pytest.raises(signin.SignInError, match="incorrect or expired"):
        app.user("stale", "https://farm.example/cb", fetch=refused)

    def no_emails(url, *, method="GET", body=None, headers=None, timeout=15.0):
        if url == signin.GITHUB_TOKEN:
            return {"access_token": "t"}
        if url.endswith("/user"):
            return {"id": 7, "login": "grace"}
        raise OSError("403 from GitHub")

    # A person without the email scope still signs in, with no email.
    assert app.user("ok", "https://farm.example/cb", fetch=no_emails) == {"id": 7, "login": "grace", "email": None, "name": None}


def test_a_handle_is_the_shape_of_a_keys_name():
    assert signin.handle_from("Ada-Lovelace") == "ada-lovelace"
    assert signin.handle_from("ada.lovelace+farm") == "ada.lovelace-farm"
    assert signin.handle_from("--weird!!") == "weird"
    assert signin.handle_from("", None, "x" * 50) == "x" * 32
    assert signin.handle_from(None, "") == "someone"
    for made in ("ada-lovelace", "weird", "x" * 32):
        assert signin.HANDLE.fullmatch(made)
    assert signin.valid_email("Ada@Example.org") and not signin.valid_email("ada") and not signin.valid_email(None)


def test_the_mail_is_a_template_by_id_with_flat_string_variables():
    """NorthRelay's contract: a template is addressed by id, its variables
    are strings, and the sender is an address the account has verified.
    The farm never assembles HTML."""
    sent = []

    def fake(url, *, method="GET", body=None, headers=None, timeout=15.0):
        sent.append((method, url, body, headers))
        return {"success": True, "data": {"messageId": "m1", "status": "Queued"}}

    mailer = signin.Mailer("nr_live_k", "farm@example.org", base_url="https://mail.example", sender_name="The Farm")
    answer = mailer.send_template("cuid123", "Ada@Example.org", {"link": "https://farm.example/auth/email/t",
                                                                  "expires_minutes": 30},
                                  tags={"purpose": "signin"}, fetch=fake)
    assert answer["data"]["messageId"] == "m1"
    method, url, body, headers = sent[0]
    assert (method, url) == ("POST", "https://mail.example/api/v1/emails/send")
    assert headers == {"Authorization": "Bearer nr_live_k"}
    assert body["from"] == {"email": "farm@example.org", "name": "The Farm"}
    assert body["to"] == [{"email": "Ada@Example.org"}]
    assert body["content"] == {"templateId": "cuid123"}
    assert body["variables"] == {"link": "https://farm.example/auth/email/t", "expires_minutes": "30"}
    assert body["tags"] == {"purpose": "signin"}

    def refused(url, **kwargs):
        return {"success": False, "error": {"code": "FORBIDDEN_SENDER", "message": "farm@example.org is not a verified sender"}}

    with pytest.raises(signin.SignInError, match="not a verified sender"):
        mailer.send_template("cuid123", "ada@example.org", {}, fetch=refused)


def test_tokens_are_random_and_stored_only_as_digests():
    one, two = signin.new_token(), signin.new_token()
    assert one != two and len(one) >= 40
    assert signin.digest(one) != one and len(signin.digest(one)) == 64
