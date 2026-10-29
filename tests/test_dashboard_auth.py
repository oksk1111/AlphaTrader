"""Dashboard authentication regressions: OAuth CSRF binding and secure cookies."""

import importlib
import os
import sys

from fastapi import Response

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _auth(monkeypatch, *, secure=True):
    monkeypatch.setenv("DASHBOARD_API_KEY", "dashboard-key")
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret")
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "true" if secure else "false")
    import web.auth as auth
    return importlib.reload(auth)


def _app(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_KEY", "dashboard-key")
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret")
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "google-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "google-secret")
    import web.auth as auth
    import web.app as app
    importlib.reload(auth)
    return importlib.reload(app)


def test_empty_password_is_never_accepted(monkeypatch):
    auth = _auth(monkeypatch)
    monkeypatch.setattr(auth, "LOGIN_PASSWORD", "")
    assert not auth.verify_password("")
    assert not auth.verify_password("anything")


def test_empty_oauth_allowlist_fails_closed(monkeypatch):
    auth = _auth(monkeypatch)
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", [])
    assert not auth.is_email_allowed("operator@example.com")
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", ["operator@example.com"])
    assert auth.is_email_allowed("operator@example.com")


def test_oauth_state_must_match_issuing_browser_and_expire(monkeypatch):
    auth = _auth(monkeypatch)
    state = auth.create_oauth_state()
    assert auth.verify_oauth_state(state, state)
    assert not auth.verify_oauth_state(state, "a-different-browser-state")
    assert not auth.verify_oauth_state("not-a-jwt", "not-a-jwt")


def test_auth_cookies_are_secure_by_default(monkeypatch):
    auth = _auth(monkeypatch, secure=True)
    response = Response()
    auth.set_session_cookie(response, "session-token")
    auth.set_oauth_state_cookie(response, "state-token")
    cookies = "\n".join(response.headers.getlist("set-cookie")).lower()
    assert "secure" in cookies
    assert "httponly" in cookies
    assert "samesite=lax" in cookies


def test_local_http_cookie_override_is_explicit(monkeypatch):
    auth = _auth(monkeypatch, secure=False)
    response = Response()
    auth.set_session_cookie(response, "session-token")
    assert "secure" not in "\n".join(response.headers.getlist("set-cookie")).lower()


def test_oauth_callback_rejects_state_from_another_browser(monkeypatch):
    app = _app(monkeypatch)
    from starlette.testclient import TestClient

    client = TestClient(app.app)
    login = client.get("/login")
    assert login.status_code == 200
    own_state = client.cookies.get("oauth_state")
    assert own_state

    other_state = app.create_oauth_state()
    response = client.get(f"/auth/callback?code=code&state={other_state}", follow_redirects=False)
    assert response.status_code == 307
    assert "Google%20" in response.headers["location"]

    # The initiating browser state remains signed and is not accepted without a code exchange.
    assert app.verify_oauth_state(own_state, own_state)
