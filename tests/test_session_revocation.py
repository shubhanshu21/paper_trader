"""
tests/test_session_revocation.py — api/auth.py's session revocation:
every issued JWT embeds the user's token_version ('tv' claim), checked
against the live panel_users.token_version column on every authenticated
request (get_current_user_optional/_token_version_is_current). Bumping
it (bump_token_version, wired to POST /api/auth/logout-all) invalidates
every outstanding session immediately, instead of only at the token's
natural 'exp' — closing the real gap where a stolen token (or a
deactivated account) stayed valid until expiry with no way to kill it
early.

Real automate_test MySQL schema (see tests/conftest.py) — no network.
"""
import sys
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

import automate.api.auth as auth
import automate.api.routes_auth as routes_auth
from automate.db.models import User

# automate/db/__init__.py does `from .engine import engine`, which shadows
# the `automate.db.engine` package ATTRIBUTE with the Engine instance
# itself — `import automate.db.engine as db_engine` would bind to the
# Engine, not the module. Go through sys.modules instead.
db_engine = sys.modules["automate.db.engine"]
from automate.api.auth import (
    bump_token_version,
    create_access_token,
    get_current_user_optional,
)


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

    # auth.py::_token_version_is_current does `from automate.db.engine
    # import get_session` INSIDE the function body — a local import
    # re-resolves automate.db.engine.get_session fresh on every call, so
    # it must be patched at the source module, not on automate.api.auth
    # (which has no such module-level name to patch). routes_auth.py
    # imports it at module level instead, so that one IS patchable directly.
    monkeypatch.setattr(db_engine, "get_session", fake_get_session)
    monkeypatch.setattr(routes_auth, "get_session", fake_get_session)
    return db_session_factory()


def _make_user(db, **overrides):
    defaults = {
        "username": "alice", "email": "alice@example.com", "hashed_password": "x",
        "role": "viewer", "is_active": 1, "token_version": 0,
    }
    defaults.update(overrides)
    u = User(**defaults)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


class TestTokenVersionCheck:
    def test_fresh_token_is_accepted(self, db):
        user = _make_user(db)
        token = create_access_token(user.id, user.username, user.role, user.token_version)
        assert get_current_user_optional(token) is not None

    def test_token_missing_tv_claim_is_treated_as_version_zero(self, db):
        """A token issued before this feature existed has no 'tv' claim — must keep working, not be force-invalidated."""
        user = _make_user(db, token_version=0)
        old_style_token = auth.jwt.encode(
            {"sub": str(user.id), "username": user.username, "role": user.role,
             "iat": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
             "exp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc) + __import__("datetime").timedelta(hours=1)},
            auth.PanelAuthConfig.jwt_secret(), algorithm=auth.ALGORITHM,
        )
        assert get_current_user_optional(old_style_token) is not None

    def test_bumped_version_invalidates_the_old_token(self, db):
        user = _make_user(db)
        old_token = create_access_token(user.id, user.username, user.role, user.token_version)
        assert get_current_user_optional(old_token) is not None

        bump_token_version(db, user)
        db.commit()

        assert get_current_user_optional(old_token) is None

    def test_a_new_token_issued_after_the_bump_works(self, db):
        user = _make_user(db)
        bump_token_version(db, user)
        db.commit()
        db.refresh(user)

        new_token = create_access_token(user.id, user.username, user.role, user.token_version)
        assert get_current_user_optional(new_token) is not None

    def test_deactivated_user_is_rejected_even_with_a_valid_token_version(self, db):
        user = _make_user(db)
        token = create_access_token(user.id, user.username, user.role, user.token_version)
        user.is_active = 0
        db.commit()

        assert get_current_user_optional(token) is None

    def test_deleted_user_is_rejected(self, db):
        user = _make_user(db)
        token = create_access_token(user.id, user.username, user.role, user.token_version)
        db.delete(user)
        db.commit()

        assert get_current_user_optional(token) is None


class TestLogoutAllEndpoint:
    def test_revokes_the_callers_own_session(self, db):
        user = _make_user(db)
        token = create_access_token(user.id, user.username, user.role, user.token_version)
        payload = get_current_user_optional(token)
        assert payload is not None

        class _FakeResponse:
            def delete_cookie(self, **kwargs):
                pass

        result = routes_auth.logout_all(_FakeResponse(), payload)
        assert result["message"] == "Logged out of all sessions."

        assert get_current_user_optional(token) is None

    def test_rejects_a_payload_for_a_since_deleted_user(self, db):
        user = _make_user(db)
        payload = {"sub": str(user.id), "username": user.username, "role": user.role}
        db.delete(user)
        db.commit()

        class _FakeResponse:
            def delete_cookie(self, **kwargs):
                pass

        with pytest.raises(HTTPException) as exc_info:
            routes_auth.logout_all(_FakeResponse(), payload)
        assert exc_info.value.status_code == 401
