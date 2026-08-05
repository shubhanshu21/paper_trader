"""
auth/upstox_auto_login.py — Headless daily Upstox login, so the trading
daemon doesn't need a human to run auth.upstox_auth every morning.

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
Leave them blank to keep using the manual `python3 -m auth.upstox_auth`
flow (~30s/day) instead — that path is unaffected either way.

Usage:
    python3 -m auth.upstox_auto_login       # force a refresh right now
    ensure_fresh_upstox_token()             # importable — used by run_daemon.py,
                                             # no-ops if not configured, checks
                                             # validity before re-logging-in
"""
import base64
import json
import logging
import os
import sys
import urllib.parse
from pathlib import Path

import pyotp
import upstox_client
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from upstox_client.rest import ApiException

from auth.upstox_auth import UpstoxAuthClient
from config import UpstoxConfig
from utils.logger import get_logger
from utils.notify import notify
from utils.telegram_alert import send_telegram_alert

log = get_logger(__name__)

_LOGIN_URL = "https://api.upstox.com/v2/login/authorization/dialog"


def token_expiry_epoch(token: str) -> float | None:
    """
    Read the `exp` claim (unix seconds) straight out of the access token's
    JWT payload, without verifying the signature — this only ever reads a
    token this app already trusts (issued by Upstox and read back from our
    own DB), never one from an untrusted source, so there's nothing to
    verify against; we just need the expiry it already carries.

    Upstox access tokens all expire at a fixed 3:30 AM IST (22:00 UTC), not
    N hours after issuance — confirmed by decoding a real token's `iat`
    (14:11:52 UTC) vs `exp` (22:00:00 UTC) on 2026-08-04, a ~7h48m gap that
    doesn't match any "hours since login" pattern. Reading `exp` directly
    means api/token_refresh_scheduler.py can schedule a proactive re-login
    a few minutes ahead of the real deadline instead of hardcoding "3:30 AM
    IST" and hoping Upstox never changes it.

    Returns None if `token` isn't empty but isn't a parseable JWT (e.g. an
    unexpected token format) — callers should fall back to their own
    periodic-check interval in that case, not treat it as "never expires".
    """
    if not token:
        return None
    try:
        payload_segment = token.split(".")[1]
        padded = payload_segment + "=" * (-len(payload_segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        return float(claims["exp"])
    except Exception:
        return None


# Always-liquid, always-listed instrument used purely as a validation
# probe — NOT a real quote request. Works regardless of market hours (LTP
# reflects the last trade, open or closed).
_VALIDATION_INSTRUMENT = "NSE_INDEX|Nifty 50"


def _token_is_valid(token: str) -> bool:
    """
    Real check against Upstox — hits the same market-quote v3 LTP endpoint
    the rest of the app actually depends on (broker/upstox_broker.py
    get_ltp/get_ltp_batch), not UserApi.get_profile().

    This used to check get_profile() instead. That's a real API call, but
    a DIFFERENT endpoint from the one this app actually lives or dies by —
    and in practice a token was observed passing get_profile() cleanly,
    then getting rejected by the market-quote endpoint with the same
    "invalid token" error a few minutes later (Upstox/Cloudflare appear to
    scope or revoke sessions per-endpoint-family rather than uniformly).
    Validating against get_profile() gave a false "still valid" reading in
    exactly that window, so ensure_fresh_upstox_token() skipped refreshing
    while every real LTP call was already 401ing. Checking the actual
    dependency directly closes that gap.
    """
    if not token:
        return False
    try:
        configuration = upstox_client.Configuration()
        configuration.access_token = token
        response = upstox_client.MarketQuoteV3Api(
            upstox_client.ApiClient(configuration)
        ).get_ltp(instrument_key=_VALIDATION_INSTRUMENT)
        return bool(response.data)
    except ApiException as exc:
        if exc.status == 401:
            log.info("Existing Upstox token is invalid/expired.")
        else:
            log.warning("Upstox token validation error (treating as invalid): HTTP %s — %s", exc.status, exc.reason)
        return False
    except Exception as exc:
        log.warning("Upstox token validation failed unexpectedly (treating as invalid): %s", exc)
        return False


def token_status() -> dict:
    """
    Public, read-only status snapshot for the current Upstox access token
    — used by api/routes_upstox_token.py's GET /status (a header badge the
    user can glance at/poll). Real, not guessed: `valid` is the same live
    LTP-endpoint probe (_token_is_valid) token_refresh_scheduler.py itself
    relies on before deciding whether to re-login, not a locally-decoded
    guess from the JWT's own `exp` claim alone (see _token_is_valid's
    docstring for why that alone isn't trustworthy).

    Returns {"configured": bool, "valid": bool, "expiry_epoch": float | None}
    — `configured` is whether auto-login (Selenium re-login on demand) is
    even possible right now (UPSTOX_USERNAME/PIN/TOTP_SECRET all set);
    `valid` is still meaningful when False (a manually-pasted token can be
    valid without auto-login being configured at all).
    """
    from config import UpstoxConfig

    token = UpstoxConfig.ACCESS_TOKEN
    return {
        "configured": UpstoxConfig.auto_login_configured(),
        "valid": _token_is_valid(token),
        "expiry_epoch": token_expiry_epoch(token) if token else None,
    }


# This host is Oracle Cloud arm64 (aarch64), which Google's official Chrome
# .deb does not target at all (amd64-only) — attempting that install fails
# apt dependency resolution outright. Since this box is Oracle Cloud, not
# WSL2, the mount-namespace restriction that used to break `snap install
# chromium` doesn't apply here, so we use the snap-packaged Chromium
# instead (Ubuntu 24.04 ships Chromium as snap-only anyway, no .deb).
# chromedriver comes bundled inside the same snap revision, so it's pinned
# to it directly rather than relying on Selenium Manager to resolve one —
# Selenium Manager doesn't know how to match a driver to a snap install.
_CHROMIUM_BINARY = "/snap/bin/chromium"
_CHROMIUM_DRIVER = "/snap/chromium/current/usr/lib/chromium-browser/chromedriver"

# Snap's AppArmor confinement only allows Chromium to write under its own
# ~/snap/chromium/ data dir — a --user-data-dir anywhere else in $HOME (or
# /tmp) fails with "Failed to create SingletonLock: Permission denied" and
# the session never starts.
_CHROMIUM_PROFILE_DIR = Path.home() / "snap" / "chromium" / "current" / "selenium-profile"


def _kill_stray_chrome_for_profile() -> None:
    """
    Chrome refuses to start a second instance against a --user-data-dir
    that's already locked (SingletonLock) — it exits immediately with
    "session not created: Chrome instance exited", before Selenium ever
    gets a session to work with.

    Observed in practice, twice: a prior login attempt's Chrome/chromedriver
    survived an automate-api restart — this service's KillMode=process (see
    deploy/automate-api.service) only ever signals the tracked uvicorn PID
    on restart/stop, deliberately never anything detached from it (that's
    what protects the separately-spawned trading daemon from dying on an
    API redeploy) — but it has the same effect on a login attempt that was
    still genuinely mid-flight (waiting on the OAuth redirect) at the exact
    moment of restart: its Chrome process keeps running, reparented to
    init, forever holding the lock, since the Python code that was going to
    call driver.quit() on it no longer exists to do so. An earlier version
    of this function only removed the lock if the PID it named was already
    dead — which correctly left that scenario's orphan alone (it WAS still
    alive) and therefore never recovered from it; every subsequent attempt,
    including across further restarts, kept failing the exact same way.

    So: unconditionally kill anything already touching this profile
    directory before starting a new attempt, rather than trying to guess
    whether it's legitimate. This is safe specifically because a NEW
    attempt is us calling this function ourselves, right before creating
    our own driver — nothing we've launched can be running under this
    profile yet, and ensure_fresh_upstox_token()'s single-worker executor
    plus _MIN_REFRESH_GAP_SEC already guarantee only one attempt is ever
    in flight per process, so anything found here is by definition a stray
    from a previous, no-longer-relevant attempt (this process's or an
    earlier process's), not a session we still need.
    """
    import signal

    profile = str(_CHROMIUM_PROFILE_DIR)
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            cmdline = (pid_dir / "cmdline").read_bytes().decode(errors="replace")
        except OSError:
            continue  # process exited between listdir() and read — fine
        if profile in cmdline and "chrom" in cmdline.lower():
            pid = int(pid_dir.name)
            try:
                os.kill(pid, signal.SIGKILL)
                log.warning("Killed stray Chrome/chromedriver process (pid=%d) from a previous login attempt.", pid)
            except OSError:
                pass
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (_CHROMIUM_PROFILE_DIR / name).unlink(missing_ok=True)


def _setup_driver() -> webdriver.Chrome:
    from selenium.webdriver.chrome.service import Service

    _CHROMIUM_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _kill_stray_chrome_for_profile()

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument(f"--user-data-dir={_CHROMIUM_PROFILE_DIR}")
    options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
    if Path(_CHROMIUM_BINARY).exists():
        options.binary_location = _CHROMIUM_BINARY
    service = Service(_CHROMIUM_DRIVER) if Path(_CHROMIUM_DRIVER).exists() else None
    return webdriver.Chrome(options=options, service=service)


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

    Upstox shows one of two screens depending on whether it recognizes
    this browser: the full mobile-number -> OTP/TOTP -> PIN flow for a
    fresh session, or a "Welcome back, enter PIN" shortcut straight to
    `pinCode` once this browser already has a remembered session. This
    project reuses one persistent Chrome profile directory across every
    login attempt (see _CHROMIUM_PROFILE_DIR, for the same reason a real
    browser would — a fresh profile every time would make Upstox treat
    every login as a brand-new device, likely triggering *more* OTP
    friction, not less) — after enough successful logins in that same
    profile, Upstox started skipping the mobile/OTP steps entirely.
    Observed directly (2026-08-04): a run against this exact profile
    landed on "Hi Shubhanshu / Welcome back / Enter 6-digit PIN" with only
    a `pinCode` input on the page, no `mobileNum` — the original version
    of this function unconditionally waited for `mobileNum` first, so it
    just timed out every time once the shortcut started appearing. Wait
    for whichever field shows up first and branch accordingly, instead of
    assuming only the full flow.
    """
    driver = _setup_driver()
    try:
        params = {
            "response_type": "code",
            "client_id": UpstoxConfig.API_KEY,
            "redirect_uri": UpstoxConfig.REDIRECT_URI,
        }
        driver.get(f"{_LOGIN_URL}?{urllib.parse.urlencode(params)}")

        first_field = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#mobileNum, #pinCode"))
        )

        if first_field.get_attribute("id") == "mobileNum":
            first_field.clear()
            first_field.send_keys(UpstoxConfig.USERNAME)
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
        else:
            log.info("Upstox auto-login: browser already recognized — skipping straight to PIN.")
            pin_field = first_field

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
        from api.deps import reset_brokers_cache as _reset_api_deps_brokers
        _reset_api_deps_brokers()
    except Exception:
        pass
    try:
        from api.custom_strategy_scheduler import (
            reset_brokers_cache as _reset_scheduler_brokers,
        )
        _reset_scheduler_brokers()
    except Exception:
        pass


def ensure_fresh_upstox_token(force: bool = False) -> str | None:
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
            f"Headless login failed: {exc}. Falling back — run `python3 -m auth.upstox_auth` manually "
            f"before market open or entries will fail.",
        )
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    result = ensure_fresh_upstox_token(force=True)
    sys.exit(0 if result else 1)
