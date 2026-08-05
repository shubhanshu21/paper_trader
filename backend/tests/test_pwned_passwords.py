"""
tests/test_pwned_passwords.py — utils/pwned_passwords.py::check_password_pwned,
the k-anonymity HaveIBeenPwned range-API client, and its wiring into
routes_auth.py::register (rejects a password found in a known breach,
but only when the check actually confirms a hit — a network failure
must never block registration). No real network access — requests.get
is mocked throughout.
"""
import hashlib
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import api.routes_auth as routes_auth
import utils.pwned_passwords as pwned
from db.models import User
from utils.pwned_passwords import check_password_pwned


def _fake_request() -> Request:
    """Minimal Starlette Request satisfying slowapi's @_limiter.limit() decorator on register()."""
    return Request(scope={
        "type": "http", "method": "POST", "path": "/api/auth/register",
        "client": ("testclient", 12345), "headers": [], "query_string": b"",
    })


class _FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code != 200:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class TestCheckPasswordPwned:
    def test_only_the_hash_prefix_is_sent_never_the_password(self, monkeypatch):
        captured = {}

        def fake_get(url, timeout):
            captured["url"] = url
            return _FakeResponse("")

        monkeypatch.setattr(pwned.requests, "get", fake_get)
        check_password_pwned("hunter2")

        sha1 = hashlib.sha1(b"hunter2").hexdigest().upper()
        assert sha1[:5] in captured["url"]
        assert sha1[5:] not in captured["url"]
        assert "hunter2" not in captured["url"]

    def test_matching_suffix_returns_the_breach_count(self, monkeypatch):
        sha1 = hashlib.sha1(b"password123").hexdigest().upper()
        suffix = sha1[5:]
        monkeypatch.setattr(pwned.requests, "get", lambda url, timeout: _FakeResponse(f"{suffix}:12345\nDEADBEEF00:2"))
        assert check_password_pwned("password123") == 12345

    def test_no_matching_suffix_returns_none(self, monkeypatch):
        monkeypatch.setattr(pwned.requests, "get", lambda url, timeout: _FakeResponse("AAAA111:5\nBBBB222:9"))
        assert check_password_pwned("some-genuinely-unique-passphrase") is None

    def test_network_failure_returns_none_not_raises(self, monkeypatch):
        import requests

        def raise_conn_error(url, timeout):
            raise requests.RequestException("timeout")
        monkeypatch.setattr(pwned.requests, "get", raise_conn_error)
        assert check_password_pwned("anything") is None

    def test_non_200_response_returns_none_not_raises(self, monkeypatch):
        monkeypatch.setattr(pwned.requests, "get", lambda url, timeout: _FakeResponse("", status_code=503))
        assert check_password_pwned("anything") is None


class TestRegisterRejectsBreachedPasswords:
    @pytest.fixture()
    def db(self, db_session_factory, monkeypatch):
        @contextmanager
        def fake_get_session():
            session = db_session_factory()
            try:
                yield session
                session.commit()
            finally:
                session.close()
        monkeypatch.setattr(routes_auth, "get_session", fake_get_session)
        return db_session_factory()

    def test_registration_blocked_when_password_is_breached(self, db, monkeypatch):
        monkeypatch.setattr(routes_auth, "check_password_pwned", lambda pw: 999999)
        from api.routes_auth import RegisterRequest, register
        body = RegisterRequest(username="newuser", email="newuser@example.com", password="correcthorsebatterystaple")

        with pytest.raises(HTTPException) as exc_info:
            register(request=_fake_request(), body=body, response=None)
        assert exc_info.value.status_code == 422
        assert "breach" in exc_info.value.detail.lower()

        assert db.query(User).filter(User.username == "newuser").first() is None

    def test_registration_allowed_when_check_cannot_confirm(self, db, monkeypatch):
        """None (network/API failure) must never block registration."""
        monkeypatch.setattr(routes_auth, "check_password_pwned", lambda pw: None)
        from api.routes_auth import RegisterRequest, register
        body = RegisterRequest(username="newuser2", email="newuser2@example.com", password="a-decent-passphrase-99")

        result = register(request=_fake_request(), body=body, response=None)
        assert result["username"] == "newuser2"

    def test_registration_allowed_when_password_not_breached(self, db, monkeypatch):
        monkeypatch.setattr(routes_auth, "check_password_pwned", lambda pw: None)
        from api.routes_auth import RegisterRequest, register
        body = RegisterRequest(username="newuser3", email="newuser3@example.com", password="another-decent-one-77")

        result = register(request=_fake_request(), body=body, response=None)
        assert result["username"] == "newuser3"
