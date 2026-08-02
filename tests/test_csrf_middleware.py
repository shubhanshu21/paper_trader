"""
tests/test_csrf_middleware.py — api/main.py's CSRF double-submit
enforcement (_csrf_exempt gating, csrf_protection_middleware calling
api/auth.py::validate_csrf). validate_csrf itself already had no direct
tests either; this covers the real gap found this session: it was
defined but never actually wired into the request path, so no endpoint
was CSRF-protected despite the frontend already sending the header.
"""
import automate.api.main as main_module
from automate.api.auth import validate_csrf
from fastapi import HTTPException, Request
import pytest


def _fake_request(headers: dict) -> Request:
    header_list = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request(scope={
        "type": "http", "method": "POST", "path": "/api/wallet/reset",
        "headers": header_list, "query_string": b"", "client": ("test", 1),
    })


class TestCsrfExempt:
    def test_safe_methods_are_always_exempt(self):
        assert main_module._csrf_exempt("GET", "/api/wallet/reset") is True
        assert main_module._csrf_exempt("HEAD", "/api/wallet/reset") is True
        assert main_module._csrf_exempt("OPTIONS", "/api/wallet/reset") is True

    def test_non_api_paths_are_exempt(self):
        assert main_module._csrf_exempt("POST", "/some/frontend/route") is True

    def test_login_register_logout_and_mfa_verify_are_exempt(self):
        for path in main_module.CSRF_EXEMPT_PATHS:
            assert main_module._csrf_exempt("POST", path) is True

    def test_ordinary_mutating_api_routes_are_protected(self):
        assert main_module._csrf_exempt("POST", "/api/wallet/reset") is False
        assert main_module._csrf_exempt("PATCH", "/api/custom-strategies/1/status") is False
        assert main_module._csrf_exempt("DELETE", "/api/positions/1") is False

    def test_mfa_setup_confirm_disable_and_logout_all_are_protected(self):
        """These are real account-security actions reachable via an ambient session cookie — must NOT be in the exempt list."""
        for path in ("/api/auth/logout-all", "/api/auth/mfa/setup", "/api/auth/mfa/confirm", "/api/auth/mfa/disable"):
            assert main_module._csrf_exempt("POST", path) is False


class TestValidateCsrf:
    def test_matching_cookie_and_header_passes(self):
        validate_csrf(_fake_request({"X-CSRF-Token": "abc123"}), "abc123")

    def test_mismatched_cookie_and_header_is_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_csrf(_fake_request({"X-CSRF-Token": "wrong"}), "abc123")
        assert exc_info.value.status_code == 403

    def test_missing_header_is_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_csrf(_fake_request({}), "abc123")
        assert exc_info.value.status_code == 403

    def test_missing_cookie_is_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_csrf(_fake_request({"X-CSRF-Token": "abc123"}), None)
        assert exc_info.value.status_code == 403

    def test_bearer_token_auth_bypasses_csrf(self):
        """A Bearer-authenticated request has no ambient cookie for a forged cross-site request to exploit — CSRF doesn't apply."""
        validate_csrf(_fake_request({"Authorization": "Bearer sometoken"}), None)
