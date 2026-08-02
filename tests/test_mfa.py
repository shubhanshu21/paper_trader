"""
tests/test_mfa.py — TOTP two-factor auth: utils/mfa.py's pure helpers,
and routes_auth.py's /mfa/* endpoints + the login() branch that issues
an mfa_pending token instead of real session cookies once mfa_enabled.

Real automate_test MySQL schema (see tests/conftest.py) — no network,
no real TOTP apps (pyotp computes codes directly from the secret).
"""
from contextlib import contextmanager

import pyotp
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import automate.api.routes_auth as routes_auth
from automate.api.auth import get_password_hash, create_mfa_pending_token
from automate.db.models import User
from automate.utils.mfa import (
    consume_backup_code, generate_backup_codes, generate_totp_secret,
    hash_backup_codes, verify_totp_code,
)
from automate.api.routes_auth import (
    LoginRequest, MfaConfirmRequest, MfaDisableRequest, MfaVerifyLoginRequest,
    login, mfa_confirm, mfa_disable, mfa_setup, mfa_verify_login,
)


def _fake_request() -> Request:
    """Minimal Starlette Request satisfying slowapi's @_limiter.limit() decorator on login()/mfa_verify_login()."""
    return Request(scope={
        "type": "http", "method": "POST", "path": "/api/auth/login",
        "client": ("testclient", 12345), "headers": [], "query_string": b"",
    })


class _FakeResponse:
    def __init__(self):
        self.cookies_set = []
        self.cookies_deleted = []

    def set_cookie(self, key, **kwargs):
        self.cookies_set.append(key)

    def delete_cookie(self, key, **kwargs):
        self.cookies_deleted.append(key)


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
    monkeypatch.setattr(routes_auth, "get_session", fake_get_session)
    return db_session_factory()


def _make_user(db, **overrides):
    defaults = dict(
        username="alice", email="alice@example.com",
        hashed_password=get_password_hash("correct horse battery staple"),
        role="viewer", is_active=1, token_version=0, mfa_enabled=0,
    )
    defaults.update(overrides)
    u = User(**defaults)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


class TestMfaUtils:
    def test_generated_secret_verifies_its_own_current_code(self):
        secret = generate_totp_secret()
        code = pyotp.TOTP(secret).now()
        assert verify_totp_code(secret, code) is True

    def test_wrong_code_is_rejected(self):
        secret = generate_totp_secret()
        assert verify_totp_code(secret, "000000") is False

    def test_blank_code_is_rejected(self):
        secret = generate_totp_secret()
        assert verify_totp_code(secret, "") is False

    def test_backup_codes_are_hashed_not_plaintext(self):
        codes = generate_backup_codes()
        stored = hash_backup_codes(codes)
        assert all(c not in stored for c in codes)

    def test_consume_backup_code_matches_and_removes_it(self):
        codes = generate_backup_codes()
        stored = hash_backup_codes(codes)
        ok, remaining = consume_backup_code(stored, codes[0])
        assert ok is True
        ok2, _ = consume_backup_code(remaining, codes[0])
        assert ok2 is False  # single-use — already consumed

    def test_consume_backup_code_rejects_unknown_code(self):
        codes = generate_backup_codes()
        stored = hash_backup_codes(codes)
        ok, _ = consume_backup_code(stored, "not-a-real-code")
        assert ok is False

    def test_consume_backup_code_handles_no_codes_stored(self):
        assert consume_backup_code(None, "anything") == (False, None)


class TestLoginBranchesOnMfa:
    def test_login_without_mfa_sets_cookies_directly(self, db):
        _make_user(db, mfa_enabled=0)
        resp = _FakeResponse()
        result = login(request=_fake_request(), body=LoginRequest(username="alice", password="correct horse battery staple"), response=resp)
        assert "mfa_required" not in result
        assert "__Host-session" in resp.cookies_set

    def test_login_with_mfa_returns_pending_token_not_cookies(self, db):
        secret = generate_totp_secret()
        _make_user(db, mfa_enabled=1, mfa_secret=secret)
        resp = _FakeResponse()
        result = login(request=_fake_request(), body=LoginRequest(username="alice", password="correct horse battery staple"), response=resp)
        assert result["mfa_required"] is True
        assert "mfa_token" in result
        assert resp.cookies_set == []


class TestMfaVerifyLogin:
    def test_correct_totp_code_completes_login(self, db):
        secret = generate_totp_secret()
        user = _make_user(db, mfa_enabled=1, mfa_secret=secret)
        mfa_token = create_mfa_pending_token(user.id)
        resp = _FakeResponse()

        result = mfa_verify_login(
            request=_fake_request(),
            body=MfaVerifyLoginRequest(mfa_token=mfa_token, code=pyotp.TOTP(secret).now()),
            response=resp,
        )
        assert result["user"]["username"] == "alice"
        assert "__Host-session" in resp.cookies_set

    def test_wrong_code_is_rejected(self, db):
        secret = generate_totp_secret()
        user = _make_user(db, mfa_enabled=1, mfa_secret=secret)
        mfa_token = create_mfa_pending_token(user.id)

        with pytest.raises(HTTPException) as exc_info:
            mfa_verify_login(request=_fake_request(), body=MfaVerifyLoginRequest(mfa_token=mfa_token, code="000000"), response=_FakeResponse())
        assert exc_info.value.status_code == 401

    def test_a_valid_backup_code_also_completes_login_and_is_consumed(self, db):
        secret = generate_totp_secret()
        codes = generate_backup_codes()
        user = _make_user(db, mfa_enabled=1, mfa_secret=secret, mfa_backup_codes_json=hash_backup_codes(codes))
        mfa_token = create_mfa_pending_token(user.id)

        result = mfa_verify_login(request=_fake_request(), body=MfaVerifyLoginRequest(mfa_token=mfa_token, code=codes[0]), response=_FakeResponse())
        assert result["user"]["username"] == "alice"

        # Same code cannot be reused — a second attempt with the SAME backup code fails now.
        mfa_token2 = create_mfa_pending_token(user.id)
        with pytest.raises(HTTPException):
            mfa_verify_login(request=_fake_request(), body=MfaVerifyLoginRequest(mfa_token=mfa_token2, code=codes[0]), response=_FakeResponse())

    def test_a_real_access_token_cannot_be_used_as_an_mfa_pending_token(self, db):
        """create_mfa_pending_token's output only — decode_mfa_pending_token must reject anything else, including a real session JWT."""
        from automate.api.auth import create_access_token
        secret = generate_totp_secret()
        user = _make_user(db, mfa_enabled=1, mfa_secret=secret)
        real_token = create_access_token(user.id, user.username, user.role, user.token_version)

        with pytest.raises(HTTPException) as exc_info:
            mfa_verify_login(request=_fake_request(), body=MfaVerifyLoginRequest(mfa_token=real_token, code=pyotp.TOTP(secret).now()), response=_FakeResponse())
        assert exc_info.value.status_code == 401


class TestMfaSetupConfirmDisable:
    def test_setup_returns_a_secret_uri_and_qr_code_without_persisting(self, db):
        user = _make_user(db, mfa_enabled=0)
        result = mfa_setup({"sub": str(user.id)})
        assert "secret" in result and "otpauth_uri" in result
        assert result["qr_code_data_uri"].startswith("data:image/png;base64,")

        db.refresh(user)
        assert user.mfa_secret is None  # not persisted until confirm()

    def test_setup_refuses_when_already_enabled(self, db):
        user = _make_user(db, mfa_enabled=1, mfa_secret=generate_totp_secret())
        with pytest.raises(HTTPException) as exc_info:
            mfa_setup({"sub": str(user.id)})
        assert exc_info.value.status_code == 409

    def test_confirm_with_correct_code_enables_mfa_and_returns_backup_codes(self, db):
        user = _make_user(db, mfa_enabled=0)
        secret = generate_totp_secret()

        result = mfa_confirm(MfaConfirmRequest(secret=secret, code=pyotp.TOTP(secret).now()), {"sub": str(user.id)})

        assert len(result["backup_codes"]) == 8
        db.refresh(user)
        assert user.mfa_enabled == 1
        assert user.mfa_secret == secret

    def test_confirm_with_wrong_code_does_not_enable_mfa(self, db):
        user = _make_user(db, mfa_enabled=0)
        secret = generate_totp_secret()

        with pytest.raises(HTTPException) as exc_info:
            mfa_confirm(MfaConfirmRequest(secret=secret, code="000000"), {"sub": str(user.id)})
        assert exc_info.value.status_code == 400

        db.refresh(user)
        assert user.mfa_enabled == 0

    def test_disable_requires_correct_password_and_code(self, db):
        secret = generate_totp_secret()
        user = _make_user(db, mfa_enabled=1, mfa_secret=secret)

        result = mfa_disable(
            MfaDisableRequest(password="correct horse battery staple", code=pyotp.TOTP(secret).now()),
            {"sub": str(user.id)},
        )
        assert result["message"] == "MFA disabled."
        db.refresh(user)
        assert user.mfa_enabled == 0
        assert user.mfa_secret is None

    def test_disable_rejects_wrong_password(self, db):
        secret = generate_totp_secret()
        user = _make_user(db, mfa_enabled=1, mfa_secret=secret)

        with pytest.raises(HTTPException) as exc_info:
            mfa_disable(MfaDisableRequest(password="wrong password", code=pyotp.TOTP(secret).now()), {"sub": str(user.id)})
        assert exc_info.value.status_code == 401

        db.refresh(user)
        assert user.mfa_enabled == 1  # unchanged

    def test_disable_rejects_wrong_code(self, db):
        secret = generate_totp_secret()
        user = _make_user(db, mfa_enabled=1, mfa_secret=secret)

        with pytest.raises(HTTPException) as exc_info:
            mfa_disable(MfaDisableRequest(password="correct horse battery staple", code="000000"), {"sub": str(user.id)})
        assert exc_info.value.status_code == 401
