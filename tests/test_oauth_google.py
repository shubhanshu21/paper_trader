"""
tests/test_oauth_google.py — api/routes_oauth.py's "Sign in with Google"
scaffolding: config-gated (every endpoint 503s cleanly when
GoogleOAuthConfig isn't set, rather than crashing or half-working),
state-token CSRF protection on the callback, and account auto-
provisioning / linking-by-email once a real Google identity is
confirmed. No real network access to Google — requests.post/get mocked.
"""
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

import automate.api.routes_oauth as oauth
from automate.api.auth import create_oauth_state_token, get_password_hash
from automate.config import GoogleOAuthConfig
from automate.db.models import User


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(GoogleOAuthConfig, "CLIENT_ID", "fake-client-id")
    monkeypatch.setattr(GoogleOAuthConfig, "CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setattr(GoogleOAuthConfig, "REDIRECT_URI", "https://example.test/api/auth/oauth/google/callback")


@pytest.fixture()
def unconfigured(monkeypatch):
    monkeypatch.setattr(GoogleOAuthConfig, "CLIENT_ID", "")
    monkeypatch.setattr(GoogleOAuthConfig, "CLIENT_SECRET", "")
    monkeypatch.setattr(GoogleOAuthConfig, "REDIRECT_URI", "")


@pytest.fixture()
def db(db_session_factory, monkeypatch):
    @contextmanager
    def fake_get_session():
        session = db_session_factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()
    monkeypatch.setattr(oauth, "get_session", fake_get_session)
    return db_session_factory()


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {}
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code != 200:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class TestOauthStatus:
    def test_reports_unconfigured(self, unconfigured):
        assert oauth.oauth_status() == {"configured": False}

    def test_reports_configured(self, configured):
        assert oauth.oauth_status() == {"configured": True}


class TestOauthLogin:
    def test_503_when_not_configured(self, unconfigured):
        with pytest.raises(HTTPException) as exc_info:
            oauth.oauth_login()
        assert exc_info.value.status_code == 503

    def test_redirects_to_google_with_a_state_param_when_configured(self, configured):
        result = oauth.oauth_login()
        assert isinstance(result, RedirectResponse)
        assert "accounts.google.com" in result.headers["location"]
        assert "state=" in result.headers["location"]
        assert "fake-client-id" in result.headers["location"]


class TestOauthCallback:
    def test_503_when_not_configured(self, unconfigured):
        with pytest.raises(HTTPException) as exc_info:
            oauth.oauth_callback(code="abc", state="xyz", error=None)
        assert exc_info.value.status_code == 503

    def test_redirects_on_google_provided_error(self, configured):
        result = oauth.oauth_callback(code=None, state=None, error="access_denied")
        assert isinstance(result, RedirectResponse)
        assert "oauth_error" in result.headers["location"]

    def test_rejects_missing_code_or_state(self, configured):
        with pytest.raises(HTTPException) as exc_info:
            oauth.oauth_callback(code=None, state="xyz", error=None)
        assert exc_info.value.status_code == 400

    def test_rejects_a_forged_or_expired_state(self, configured):
        with pytest.raises(HTTPException) as exc_info:
            oauth.oauth_callback(code="abc", state="not-a-real-signed-token", error=None)
        assert exc_info.value.status_code == 400

    def test_rejects_a_real_session_token_used_as_state(self, configured):
        """oauth_state tokens are a distinct purpose from access/mfa_pending tokens — one must never pass as another."""
        from automate.api.auth import create_access_token
        bogus_state = create_access_token(1, "someone", "viewer", 0)
        with pytest.raises(HTTPException) as exc_info:
            oauth.oauth_callback(code="abc", state=bogus_state, error=None)
        assert exc_info.value.status_code == 400

    def test_new_verified_google_account_is_auto_provisioned(self, configured, db, monkeypatch):
        state = create_oauth_state_token()
        monkeypatch.setattr(oauth.requests, "post", lambda *a, **k: _FakeResponse({"access_token": "gtok"}))
        monkeypatch.setattr(oauth.requests, "get", lambda *a, **k: _FakeResponse({"email": "newperson@gmail.com", "email_verified": True}))

        result = oauth.oauth_callback(code="abc", state=state, error=None)

        assert isinstance(result, RedirectResponse)
        assert "__Host-session" in result.headers.get("set-cookie", "")
        user = db.query(User).filter(User.email == "newperson@gmail.com").first()
        assert user is not None
        assert user.username  # derived from the email local-part
        assert user.role == "admin"  # first user in this test's fresh schema

    def test_existing_account_with_matching_email_logs_in_without_creating_a_duplicate(self, configured, db, monkeypatch):
        existing = User(
            username="existinguser", email="existing@gmail.com",
            hashed_password=get_password_hash("whatever"), role="viewer", is_active=1,
        )
        db.add(existing)
        db.commit()

        state = create_oauth_state_token()
        monkeypatch.setattr(oauth.requests, "post", lambda *a, **k: _FakeResponse({"access_token": "gtok"}))
        monkeypatch.setattr(oauth.requests, "get", lambda *a, **k: _FakeResponse({"email": "existing@gmail.com", "email_verified": True}))

        oauth.oauth_callback(code="abc", state=state, error=None)

        assert db.query(User).filter(User.email == "existing@gmail.com").count() == 1

    def test_unverified_email_is_rejected(self, configured, db, monkeypatch):
        state = create_oauth_state_token()
        monkeypatch.setattr(oauth.requests, "post", lambda *a, **k: _FakeResponse({"access_token": "gtok"}))
        monkeypatch.setattr(oauth.requests, "get", lambda *a, **k: _FakeResponse({"email": "unverified@gmail.com", "email_verified": False}))

        with pytest.raises(HTTPException) as exc_info:
            oauth.oauth_callback(code="abc", state=state, error=None)
        assert exc_info.value.status_code == 403

    def test_deactivated_existing_account_is_rejected(self, configured, db, monkeypatch):
        existing = User(
            username="banneduser", email="banned@gmail.com",
            hashed_password=get_password_hash("whatever"), role="viewer", is_active=0,
        )
        db.add(existing)
        db.commit()

        state = create_oauth_state_token()
        monkeypatch.setattr(oauth.requests, "post", lambda *a, **k: _FakeResponse({"access_token": "gtok"}))
        monkeypatch.setattr(oauth.requests, "get", lambda *a, **k: _FakeResponse({"email": "banned@gmail.com", "email_verified": True}))

        with pytest.raises(HTTPException) as exc_info:
            oauth.oauth_callback(code="abc", state=state, error=None)
        assert exc_info.value.status_code == 403

    def test_google_network_failure_returns_502_not_a_crash(self, configured, monkeypatch):
        import requests
        state = create_oauth_state_token()

        def raise_conn_error(*a, **k):
            raise requests.RequestException("timeout")
        monkeypatch.setattr(oauth.requests, "post", raise_conn_error)

        with pytest.raises(HTTPException) as exc_info:
            oauth.oauth_callback(code="abc", state=state, error=None)
        assert exc_info.value.status_code == 502
