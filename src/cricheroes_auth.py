from __future__ import annotations

"""
cricheroes_auth.py — Login and session management for cricheroes.in.

CricHeroes requires an authenticated session to call the pagination API
(/player/get-player-match/).  This module handles:

  1. Selenium-based login (phone + OTP *or* email + password).
  2. Persisting the session cookies to data/session.json.
  3. Restoring a saved session into a requests.Session for API calls.
  4. Detecting expired sessions and prompting for re-login.

Usage:
  # One-time login (run once; cookies persist across runs):
  from cricheroes_auth import login_interactive, load_session
  cookies = login_interactive()   # opens visible browser

  # Subsequent runs:
  session = load_session()        # loads from data/session.json
  if session is None:
      session = login_interactive()
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CH_BASE_URL, CH_API_URL, CH_API_KEY, CH_API_UDID, CH_API_DEVICE, DATA_DIR, SCORECARD_WAIT

log = logging.getLogger(__name__)

SESSION_FILE   = DATA_DIR / "session.json"
SESSION_MAX_AGE_HOURS = 72   # re-login if cookie is older than 3 days

# ── Session persistence ───────────────────────────────────────────────────────

def save_session(cookies: dict) -> None:
    """Persist cookies dict to data/session.json."""
    payload = {
        "saved_at": datetime.utcnow().isoformat(),
        "cookies":  cookies,
    }
    SESSION_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info(f"[auth] Session saved → {SESSION_FILE}")


def load_session() -> "requests.Session | None":
    """
    Load session from data/session.json.
    Returns a configured requests.Session if session is fresh, else None.
    """
    if not SESSION_FILE.exists():
        return None

    try:
        payload  = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        saved_at = datetime.fromisoformat(payload["saved_at"])
        age_h    = (datetime.utcnow() - saved_at).total_seconds() / 3600
        if age_h > SESSION_MAX_AGE_HOURS:
            log.info(f"[auth] Session is {age_h:.0f}h old — needs refresh")
            return None
        cookies = payload["cookies"]
        log.info(f"[auth] Session loaded ({age_h:.1f}h old)")
        return _build_requests_session(cookies)
    except Exception as e:
        log.warning(f"[auth] Could not load session: {e}")
        return None


def _build_requests_session(cookies: dict) -> requests.Session:
    # Handle both old flat {name: value} and new nested format from login_interactive:
    # {"cookies": {...}, "localStorage": {...}, "landed_url": "..."}
    if "cookies" in cookies and isinstance(cookies.get("cookies"), dict):
        http_cookies  = cookies["cookies"]
        local_storage = cookies.get("localStorage", {})
    else:
        http_cookies  = cookies
        local_storage = {}

    # Extract auth token — check localStorage first, then the Authorization cookie.
    jwt_token = None
    for key in ("token", "authToken", "auth_token", "accessToken", "access_token",
                "userToken", "user_token", "jwtToken", "jwt"):
        val = local_storage.get(key)
        if val and isinstance(val, str) and len(val) > 20:
            jwt_token = val
            log.info(f"[auth] JWT found in localStorage[{key!r}] (len={len(val)})")
            break
    if not jwt_token:
        # CricHeroes sets an "Authorization" cookie containing the session token.
        auth_cookie = http_cookies.get("Authorization")
        if auth_cookie and len(auth_cookie) > 10:
            jwt_token = auth_cookie
            log.info(f"[auth] Auth token found in Authorization cookie (len={len(auth_cookie)})")

    s = requests.Session()
    s.verify = False
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
        ),
        "Accept":           "application/json, text/plain, */*",
        "Accept-Language":  "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer":          f"{CH_BASE_URL}/",
    }
    if jwt_token:
        headers["Authorization"] = f"Bearer {jwt_token}"
    s.headers.update(headers)
    for name, value in http_cookies.items():
        s.cookies.set(name, value, domain="cricheroes.in")
    return s


# ── Validate session ──────────────────────────────────────────────────────────

def validate_session(session: requests.Session, player_id: str = "8102785") -> bool:
    """
    Probe the pagination API with the session.
    Returns True if the API returns JSON (auth working), False if 404/HTML.
    """
    ts  = int(time.time() * 1000)
    url = f"{CH_BASE_URL}/player/get-player-match/{player_id}?pagesize=12&pageno=2&datetime={ts}"
    try:
        r = session.get(url, timeout=15)
        content_type = r.headers.get("content-type", "")
        if r.status_code == 200 and "json" in content_type:
            data = r.json()
            if isinstance(data, dict) and "data" in data:
                log.info("[auth] Session validated — pagination API works")
                return True
        log.info(f"[auth] Session invalid: status={r.status_code}, ct={content_type}")
        return False
    except Exception as e:
        log.warning(f"[auth] Session validation error: {e}")
        return False


# ── Selenium-based login ──────────────────────────────────────────────────────

def login_interactive(
    email_or_phone: str | None = None,
    password: str | None = None,
    headless: bool = False,
) -> requests.Session:
    """
    Open a browser on the CricHeroes login page.

    If email_or_phone + password are provided, tries to auto-fill the form.
    Otherwise (or if auto-fill fails / OTP is required), waits for the user
    to complete login manually, then extracts cookies.

    headless=False  → visible browser window (default; required for OTP).
    headless=True   → headless (only works if auto-fill succeeds with no OTP).

    Returns a requests.Session configured with the fresh session cookies.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    login_url  = f"{CH_BASE_URL}/login"
    log.info(f"[auth] Opening login page: {login_url}")

    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1200,800")
    options.add_argument("--log-level=3")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )

    try:
        driver = webdriver.Chrome(options=options)
    except Exception:
        import shutil
        chromedriver_path = shutil.which("chromedriver")
        if chromedriver_path:
            driver = webdriver.Chrome(service=Service(chromedriver_path), options=options)
        else:
            raise

    cookies = {}
    try:
        driver.get(login_url)
        time.sleep(SCORECARD_WAIT)

        # ── Try auto-fill if credentials supplied ─────────────────────────────
        if email_or_phone and password:
            _try_autofill(driver, email_or_phone, password)

        # ── Wait for the user to be logged in ─────────────────────────────────
        # We detect login by watching for the session cookie or a nav element
        # that only appears for logged-in users.
        print()
        print("=" * 60)
        print("  CRICHEROES LOGIN")
        print("=" * 60)
        if headless:
            print("  Running headless — auto-fill attempted.")
            print("  If login fails, re-run without --headless.")
        else:
            print("  A browser window has opened at cricheroes.in/login.")
            print("  Please log in. The window will close automatically")
            print("  once your session is detected (up to 120s).")
        print("=" * 60)
        print()

        deadline = time.time() + 180
        logged_in = False
        while time.time() < deadline:
            current_url = driver.current_url

            # Detect login by: URL is on cricheroes.in or cricheroes.com (post-login),
            # but NOT on otpskip.cricheroes.com (that is just the start of the OTP flow).
            is_cricheroes = (
                "cricheroes.in" in current_url
                or "cricheroes.com" in current_url
            )
            login_done = (
                is_cricheroes
                and "otpskip" not in current_url
                and "/login" not in current_url
            )

            if login_done:
                # If we landed on cricheroes.com, navigate to cricheroes.in so we
                # capture cookies and localStorage from the correct domain.
                if "cricheroes.in" not in current_url:
                    log.info(
                        f"[auth] Logged in at {current_url} — "
                        f"navigating to cricheroes.in to capture session..."
                    )
                    driver.get(f"{CH_BASE_URL}/")
                    time.sleep(5)

                # Grab every cookie the browser has for cricheroes.in
                all_cookies = {c["name"]: c["value"] for c in driver.get_cookies()}

                # Also read localStorage — CricHeroes stores JWT there
                local_storage = {}
                try:
                    ls_data = driver.execute_script(
                        "return JSON.stringify(window.localStorage);"
                    )
                    if ls_data:
                        local_storage = json.loads(ls_data)
                except Exception:
                    pass

                cookies = {
                    "cookies":       all_cookies,
                    "localStorage":  local_storage,
                    "landed_url":    current_url,
                }
                logged_in = True
                log.info(
                    f"[auth] Login detected at {current_url}. "
                    f"Cookies: {list(all_cookies.keys())}. "
                    f"localStorage keys: {list(local_storage.keys())[:10]}"
                )
                break
            time.sleep(2)

        if not logged_in:
            # Last-ditch: grab whatever is there even if still on login page
            all_cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
            if all_cookies:
                local_storage = {}
                try:
                    ls_data = driver.execute_script(
                        "return JSON.stringify(window.localStorage);"
                    )
                    if ls_data:
                        local_storage = json.loads(ls_data)
                except Exception:
                    pass
                cookies = {
                    "cookies":       all_cookies,
                    "localStorage":  local_storage,
                    "landed_url":    driver.current_url,
                }
                logged_in = True
                log.info(f"[auth] Last-ditch capture: cookies={list(all_cookies.keys())}")
            else:
                raise TimeoutError("Login not detected within 180 seconds.")

    finally:
        save_session(cookies)
        log.info("[auth] Session saved — browser left open for HAR capture.")
        print()
        print("=" * 60)
        print("  SESSION SAVED. Browser left open.")
        print("  In Chrome DevTools (Network tab), navigate to the")
        print("  player profile page and capture the HAR file.")
        print("  Press ENTER here when done to close the browser.")
        print("=" * 60)
        try:
            input()
        except Exception:
            pass
        driver.quit()

    session = _build_requests_session(cookies)
    log.info("[auth] Login complete — session saved")
    return session


def _try_autofill(driver, credential: str, password: str) -> None:
    """Try to auto-fill the CricHeroes login form. Fails silently."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    try:
        # CricHeroes login: phone/email field then password
        selectors = [
            "input[type='tel']",
            "input[type='phone']",
            "input[name='phone']",
            "input[name='email']",
            "input[type='email']",
            "input[placeholder*='phone' i]",
            "input[placeholder*='mobile' i]",
            "input[placeholder*='email' i]",
        ]
        field = None
        for sel in selectors:
            try:
                field = driver.find_element(By.CSS_SELECTOR, sel)
                break
            except Exception:
                continue

        if not field:
            log.debug("[auth] No phone/email field found — skipping auto-fill")
            return

        field.clear()
        field.send_keys(credential)
        time.sleep(0.5)

        # Look for password field or "Send OTP" / "Continue" button
        pwd_selectors = [
            "input[type='password']",
            "input[name='password']",
        ]
        pwd_field = None
        for sel in pwd_selectors:
            try:
                pwd_field = driver.find_element(By.CSS_SELECTOR, sel)
                break
            except Exception:
                continue

        if pwd_field:
            pwd_field.clear()
            pwd_field.send_keys(password)
            time.sleep(0.3)
            pwd_field.send_keys(Keys.RETURN)
            log.info("[auth] Auto-filled email + password, submitted form")
        else:
            # No password field — may be OTP flow; click Continue
            btn_selectors = [
                "button[type='submit']",
                "button:contains('Continue')",
                "button:contains('Send OTP')",
                "button:contains('Login')",
            ]
            for sel in btn_selectors:
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, sel)
                    btn.click()
                    log.info(f"[auth] Clicked submit button: {sel}")
                    break
                except Exception:
                    continue

        time.sleep(2)
    except Exception as e:
        log.debug(f"[auth] Auto-fill failed (will wait for manual login): {e}")


# ── Public helper: get a working session ─────────────────────────────────────

def get_session(
    email_or_phone: str | None = None,
    password: str | None = None,
    force_relogin: bool = False,
) -> "requests.Session | None":
    """
    Return a valid requests.Session for CricHeroes API calls.

    1. Try to load saved session from data/session.json.
    2. Validate it against the pagination API.
    3. If invalid/missing/expired, return None (caller should prompt login).

    Does NOT auto-login — call login_interactive() explicitly.
    """
    if force_relogin:
        SESSION_FILE.unlink(missing_ok=True)
        return None

    session = load_session()
    if session is None:
        return None

    if not validate_session(session):
        log.info("[auth] Saved session is expired or invalid")
        SESSION_FILE.unlink(missing_ok=True)
        return None

    return session


# ── Direct API session (no login required) ────────────────────────────────────

def get_api_session() -> requests.Session:
    """
    Return a requests.Session configured for the CricHeroes REST API.
    No login required — uses the public api-key + udid headers from the browser.
    """
    s = requests.Session()
    s.verify = False
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
        ),
        "api-key":      CH_API_KEY,
        "udid":         CH_API_UDID,
        "device-type":  CH_API_DEVICE,
        "Accept":       "application/json, text/plain, */*",
        "Origin":       "https://cricheroes.com",
        "Referer":      "https://cricheroes.com/",
    })
    return s


# ── Pagination helper ─────────────────────────────────────────────────────────

def fetch_match_page(
    session: requests.Session,
    player_id: str,
    page_num: int,
    pagesize: int = 12,
) -> dict | None:
    """
    Fetch one page of a player's match history via api.cricheroes.in.

    Returns the parsed JSON response dict, or None if the request fails.
    Response structure: { status, page: { next }, data: [...matches...] }
    """
    ts  = int(time.time() * 1000)
    url = (
        f"{CH_API_URL}/player/get-player-match/{player_id}"
        f"?pagesize={pagesize}&pageno={page_num}&datetime={ts}"
    )
    try:
        r = session.get(url, timeout=20)
        content_type = r.headers.get("content-type", "")
        if r.status_code != 200 or "json" not in content_type:
            log.debug(
                f"[auth] page {page_num} for player {player_id}: "
                f"status={r.status_code}, ct={content_type}"
            )
            return None
        return r.json()
    except Exception as e:
        log.warning(f"[auth] fetch_match_page error (player={player_id} page={page_num}): {e}")
        return None
