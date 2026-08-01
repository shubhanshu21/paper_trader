"""
auth/upstox_auto_login.py — Headless daily Upstox login, so the trading
daemon doesn't need a human to run automate.auth.upstox_auth every morning.

Adapted from a working implementation the user already built and
validated (github.com/shubhanshu21/study, paper_trading/daily_trading_bot_upstox.py)
— same Selenium-driven login flow, wired into THIS project's existing
token machinery (auth.upstox_auth.UpstoxAuthClient) instead of duplicating
the OAuth exchange. The refreshed token is persisted via
UpstoxConfig.ACCESS_TOKEN's DB-backed setter (see config._UpstoxConfigMeta),
not written to .env — both automate-api and this daemon read the same
MySQL row, so a refresh here is immediately visible to the API process too.

SECURITY WARNING — read before enabling:
Unlike UPSTOX_ACCESS_TOKEN (daily-expiring, revocable, API-scoped), the
UPSTOX_TOTP_SECRET this needs is the actual seed your 2FA app uses to
generate codes. Anyone who gets it (plus UPSTOX_PIN) has permanent, full
login access to the real account — 2FA no longer protects it once both
are on disk. This also drives Upstox's real login page via Selenium/XPath
selectors (not just the official OAuth token-exchange call), so it WILL
break silently if Upstox changes their login page's markup, and this
kind of UI automation may fall outside their normal API ToS.

Auto-login is entirely opt-in: it only runs if UPSTOX_USERNAME/PIN/
TOTP_SECRET are ALL set in .env (see UpstoxConfig.auto_login_configured).
Leave them blank to keep using the manual `python3 -m automate.auth.upstox_auth`
flow (~30s/day) instead — that path is unaffected either way.

Usage:
    python3 -m automate.auth.upstox_auto_login       # force a refresh right now
    ensure_fresh_upstox_token()             # importable — used by run_daemon.py,
                                             # no-ops if not configured, checks
                                             # validity before re-logging-in
"""
import logging
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

import pyotp
import upstox_client
from upstox_client.rest import ApiException
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from automate.config import UpstoxConfig
from automate.utils.logger import get_logger
from automate.utils.telegram_alert import send_telegram_alert
from automate.utils.notify import notify
from automate.auth.upstox_auth import UpstoxAuthClient

log = get_logger(__name__)

_LOGIN_URL = "https://api.upstox.com/v2/login/authorization/dialog"


def _token_is_valid(token: str) -> bool:
    """Real check against Upstox (get_profile), not just non-empty."""
    if not token:
        return False
    try:
        configuration = upstox_client.Configuration()
        configuration.access_token = token
        profile = upstox_client.UserApi(upstox_client.ApiClient(configuration)).get_profile(api_version="2.0")
        return profile.status == "success"
    except ApiException as exc:
        if exc.status == 401:
            log.info("Existing Upstox token is invalid/expired.")
        else:
            log.warning("Upstox token validation error (treating as invalid): HTTP %s — %s", exc.status, exc.reason)
        return False
    except Exception as exc:
        log.warning("Upstox token validation failed unexpectedly (treating as invalid): %s", exc)
        return False


# Installed via the official Google Chrome .deb (not snap — snap's strict
# confinement fails under WSL2's mount-namespace restrictions, verified
# during setup: `snap install chromium` errors out with "cannot preserve
# mount namespace"). chromedriver itself is auto-resolved by Selenium
# Manager (built into selenium>=4.6) to match whatever Chrome version is
# actually installed — no separate driver binary/version to keep in sync.
_CHROME_BINARY = "/usr/bin/google-chrome"


def _setup_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
    if Path(_CHROME_BINARY).exists():
        options.binary_location = _CHROME_BINARY
    return webdriver.Chrome(options=options)


def _js_click(driver, element) -> None:
    """
    Real click() on these buttons intermittently hits Upstox's own
    "OTP sent" toast overlay (verified: ElementClickInterceptedException,
    the toast sits on top of continueBtn/pinContinueBtn for a few seconds
    after submission) — a JS click bypasses that instead of guessing a
    fixed sleep long enough to always outlast the toast.
    """
    driver.execute_script("arguments[0].click();", element)


def _auto_login_get_code() -> str:
    """
    Drive Upstox's real login page headlessly; return the OAuth 'code'.
    Element IDs below (mobileNum/getOtp/otpNum/continueBtn/pinCode/
    pinContinueBtn) were captured directly off Upstox's live login page
    (screenshots + page source) — not guessed from generic tag/type
    selectors, which is what broke on the first attempt (their OTP/TOTP
    field has no `maxlength` HTML attribute; their buttons are `id`-only,
    not text-matchable reliably).
    """
    driver = _setup_driver()
    try:
        params = {
            "response_type": "code",
            "client_id": UpstoxConfig.API_KEY,
            "redirect_uri": UpstoxConfig.REDIRECT_URI,
        }
        driver.get(f"{_LOGIN_URL}?{urllib.parse.urlencode(params)}")

        mobile_field = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "mobileNum"))
        )
        mobile_field.clear()
        mobile_field.send_keys(UpstoxConfig.USERNAME)
        driver.find_element(By.ID, "getOtp").click()
        log.info("Upstox auto-login: mobile number submitted.")

        totp_code = pyotp.TOTP(UpstoxConfig.TOTP_SECRET).now()
        otp_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "otpNum"))
        )
        otp_field.clear()
        otp_field.send_keys(totp_code)
        continue_btn = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "continueBtn"))
        )
        WebDriverWait(driver, 10).until(lambda d: continue_btn.get_attribute("disabled") is None)
        _js_click(driver, continue_btn)
        log.info("Upstox auto-login: TOTP submitted.")

        pin_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "pinCode"))
        )
        pin_field.clear()
        pin_field.send_keys(UpstoxConfig.PIN)
        pin_continue_btn = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "pinContinueBtn"))
        )
        WebDriverWait(driver, 10).until(lambda d: pin_continue_btn.get_attribute("disabled") is None)
        _js_click(driver, pin_continue_btn)
        log.info("Upstox auto-login: PIN submitted, waiting for redirect ...")

        WebDriverWait(driver, 30).until(lambda d: "code=" in d.current_url)
        code = urllib.parse.parse_qs(urllib.parse.urlparse(driver.current_url).query)["code"][0]
        log.info("Upstox auto-login: authorization code received.")
        return code
    finally:
        driver.quit()


def _invalidate_broker_caches() -> None:
    """
    Drop every already-constructed UpstoxBroker singleton in this process
    after a successful token refresh above.

    UpstoxConfig.ACCESS_TOKEN is DB-backed and always reads fresh (see
    config._UpstoxConfigMeta), but UpstoxBroker itself bakes the token
    into the Upstox SDK's Configuration object once at construction
    (broker/upstox_broker.py __init__) and never re-reads it — so writing
    a new token to the DB here has NO effect on any broker object that
    already exists in memory. api/deps.py and api/custom_strategy_scheduler.py
    each keep their own long-lived {'paper','live'} broker pair cached at
    module scope for exactly that reason (avoid rebuilding on every call),
    which means without this, a successful auto-login would keep every
    live request 401ing against the OLD token until the process is
    manually restarted — indistinguishable from the refresh never having
    happened at all. Only relevant to the long-running API process; a
    no-op (caught below) for one-shot CLI invocations that never imported
    those modules.
    """
    try:
        from automate.api.deps import reset_brokers_cache as _reset_api_deps_brokers
        _reset_api_deps_brokers()
    except Exception:
        pass
    try:
        from automate.api.custom_strategy_scheduler import reset_brokers_cache as _reset_scheduler_brokers
        _reset_scheduler_brokers()
    except Exception:
        pass


def ensure_fresh_upstox_token(force: bool = False) -> Optional[str]:
    """
    Ensure UpstoxConfig.ACCESS_TOKEN is valid, refreshing it headlessly if
    needed. No-ops (returns None immediately, no Selenium/network call) if
    auto-login isn't configured — see UpstoxConfig.auto_login_configured.
    Callers should treat that as "fall back to expecting the manual daily
    refresh," not as a failure.

    Returns the (possibly unchanged) valid token, or None if a refresh was
    needed but failed (already alerted via Telegram in that case).
    """
    if not UpstoxConfig.auto_login_configured():
        return None

    if not force and _token_is_valid(UpstoxConfig.ACCESS_TOKEN):
        log.info("Existing Upstox token still valid — no refresh needed.")
        return UpstoxConfig.ACCESS_TOKEN

    log.info("Refreshing Upstox token via headless auto-login ...")
    try:
        code = _auto_login_get_code()
        auth = UpstoxAuthClient(
            api_key=UpstoxConfig.API_KEY,
            api_secret=UpstoxConfig.API_SECRET,
            redirect_uri=UpstoxConfig.REDIRECT_URI,
        )
        token = auth.exchange_code_for_token(code)
        UpstoxConfig.ACCESS_TOKEN = token  # persists to DB — see config._UpstoxConfigMeta
        _invalidate_broker_caches()
        log.info("Upstox token refreshed automatically.")
        send_telegram_alert("🔑 Upstox token refreshed automatically — no action needed.")
        return token
    except Exception as exc:
        log.critical("Automatic Upstox login failed: %s", exc, exc_info=True)
        notify(
            "upstox_login",
            f"Headless login failed: {exc}. Falling back — run `python3 -m automate.auth.upstox_auth` manually "
            f"before market open or entries will fail.",
        )
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    result = ensure_fresh_upstox_token(force=True)
    sys.exit(0 if result else 1)
