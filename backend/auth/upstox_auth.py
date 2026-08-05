"""
auth/upstox_auth.py — Upstox OAuth2 Authentication Flow Handler.

Upstox uses the standard OAuth 2.0 Authorization Code flow. A fresh
access_token must be generated each trading day. This module provides:

  1. A helper to build the authorization URL.
  2. A helper to exchange the authorization code for an access_token.
  3. A CLI mode to run the flow interactively on a server.

Typical daily workflow on a headless Ubuntu server:
  $ python3 -m auth.upstox_auth
  → Prints a login URL
  → You open it in a browser, log in, and copy the `code` from the redirect URL
  → Paste the code back into the terminal
  → The script prints (and optionally writes) the new access_token

Security notes:
  - The client_secret is read from environment variables; never hardcoded.
  - The access_token is persisted via UpstoxConfig.ACCESS_TOKEN (DB-backed,
    see config._UpstoxConfigMeta / auth.token_store) — not written to .env,
    and not logged in plaintext.
  - For automated (unattended) daily refresh, see auth.upstox_auto_login
    instead of this interactive CLI flow.
"""

import logging
import sys
import urllib.parse

import requests

from config import UpstoxConfig
from utils.logger import get_logger

log = get_logger(__name__)

# Upstox OAuth2 endpoints (v2)
_AUTH_BASE_URL = "https://api.upstox.com/v2/login/authorization/dialog"
_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"


class UpstoxAuthClient:
    """
    Handles the Upstox OAuth2 Authorization Code flow.

    This class encapsulates the two-step process:
      Step 1 → Build the authorization URL and have the user log in.
      Step 2 → Exchange the returned code for an access_token.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        redirect_uri: str,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.redirect_uri = redirect_uri

    # ------------------------------------------------------------------
    # Step 1: Build the login URL
    # ------------------------------------------------------------------

    def get_authorization_url(self) -> str:
        """
        Construct the Upstox authorization URL.

        The user opens this URL in a browser, completes the Upstox login
        (including TOTP/OTP), and is redirected to `redirect_uri` with a
        `code` query parameter appended.

        Returns:
            The full authorization URL string.
        """
        params = {
            "response_type": "code",
            "client_id": self.api_key,
            "redirect_uri": self.redirect_uri,
        }
        url = f"{_AUTH_BASE_URL}?{urllib.parse.urlencode(params)}"
        log.info("Authorization URL generated (client_id=...%s)", self.api_key[-6:])
        return url

    # ------------------------------------------------------------------
    # Step 2: Exchange authorization code → access_token
    # ------------------------------------------------------------------

    def exchange_code_for_token(self, authorization_code: str) -> str:
        """
        Exchange the short-lived authorization code for an access_token.

        Upstox returns a JSON body containing `access_token`, `token_type`,
        `expires_in`, and metadata. Only the `access_token` is returned here;
        the caller is responsible for storing it securely.

        Args:
            authorization_code: The `code` value from the redirect URL
                                 query parameters.

        Returns:
            The access_token string.

        Raises:
            RuntimeError: If the token exchange fails (bad code, wrong
                          credentials, or network error).
        """
        # The Upstox token endpoint requires a URL-encoded POST body.
        payload = {
            "code": authorization_code,
            "client_id": self.api_key,
            "client_secret": self.api_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }

        headers = {
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            log.info("Requesting access_token from Upstox token endpoint...")
            response = requests.post(
                _TOKEN_URL,
                data=payload,
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
        except requests.exceptions.Timeout as exc:
            raise RuntimeError("Token exchange timed out. Check network connectivity.") from exc
        except requests.exceptions.HTTPError as exc:
            # Log the response body for debugging, but never log the payload
            # (it contains client_secret).
            raise RuntimeError(
                f"Token exchange failed (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            ) from exc

        data = response.json()

        if "access_token" not in data:
            raise RuntimeError(
                f"Token exchange returned no access_token. Response: {data}"
            )

        log.info("Access token obtained successfully.")
        # Security: we do NOT log the token value itself.
        return data["access_token"]


# ---------------------------------------------------------------------------
# Helper: parse `code` from a pasted redirect URL
# ---------------------------------------------------------------------------

def extract_code_from_redirect_url(url: str) -> str:
    """
    Parse the `code` query parameter from the redirect URL.

    After logging in, Upstox redirects the browser to:
      https://127.0.0.1/?code=AUTH_CODE_HERE&...
    The user copies this full URL and pastes it; this function extracts
    just the `code` value.

    Args:
        url: The full redirect URL pasted by the user.

    Returns:
        The authorization code string.

    Raises:
        ValueError: If `code` parameter is not found in the URL.
    """
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    codes = params.get("code")
    if not codes:
        raise ValueError(
            "No 'code' parameter found in URL. "
            "Make sure you copied the full redirect URL."
        )
    return codes[0]




# ---------------------------------------------------------------------------
# CLI entry point — run this interactively on the server each morning
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging as _logging

    _logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    print("\n" + "=" * 60)
    print("  Upstox Daily Token Generator")
    print("=" * 60)

    auth = UpstoxAuthClient(
        api_key=UpstoxConfig.API_KEY,
        api_secret=UpstoxConfig.API_SECRET,
        redirect_uri=UpstoxConfig.REDIRECT_URI,
    )

    # Step 1 — print the login URL
    login_url = auth.get_authorization_url()
    print("\nStep 1: Open the following URL in your browser and log in:\n")
    print(f"  {login_url}\n")

    # Step 2 — accept the pasted redirect URL or raw code
    print("Step 2: After logging in, paste the FULL redirect URL (or just the 'code') below:")
    user_input = input("  > ").strip()

    if user_input.startswith("http"):
        try:
            code = extract_code_from_redirect_url(user_input)
        except ValueError as e:
            print(f"\n[ERROR] {e}")
            sys.exit(1)
    else:
        code = user_input  # User pasted the raw code

    # Step 3 — exchange code for token
    try:
        token = auth.exchange_code_for_token(code)
    except RuntimeError as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)

    print("\n[SUCCESS] New access_token obtained.")

    # Step 4 — optionally save (DB-backed, see UpstoxConfig.ACCESS_TOKEN / config._UpstoxConfigMeta)
    save = input("\nSave token now? [y/N]: ").strip().lower()
    if save in ("y", "yes"):
        from config import UpstoxConfig
        UpstoxConfig.ACCESS_TOKEN = token
        print("[SAVED] Token stored. Run your strategy now.")
    else:
        print(f"\nToken (not saved):\n{token}\n")
