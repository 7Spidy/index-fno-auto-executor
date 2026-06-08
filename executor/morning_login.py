"""
morning_login.py — runs once per trading day via morning-login.yml.

Steps:
  1. Headless Kite login: password + TOTP → request_token
  2. Exchange request_token → access_token via kiteconnect
  3. Fetch NFO instruments, build symbol → token/lot_size caches
  4. Write all four keys to Redis with 26-hour TTL
"""
from __future__ import annotations

import json
import logging
import os

import pyotp
import redis as redis_lib
import requests
from kiteconnect import KiteConnect

log = logging.getLogger(__name__)

TTL_SECONDS = 26 * 3600  # survives overnight; replaced next morning

_LOGIN_URL   = "https://kite.zerodha.com/api/login"
_TWOFA_URL   = "https://kite.zerodha.com/api/twofa"
_CONNECT_URL = "https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"


def _get_request_token(api_key: str, user_id: str, password: str, totp_secret: str) -> str:
    s = requests.Session()

    # Step 1: password login
    r1 = s.post(_LOGIN_URL, data={"user_id": user_id, "password": password})
    r1.raise_for_status()
    body1 = r1.json()
    if body1.get("status") != "success":
        raise RuntimeError(f"Kite password login failed: {body1.get('message')}")
    request_id = body1["data"]["request_id"]
    log.info("password login ok, request_id=%s", request_id)

    # Step 2: TOTP 2FA — authenticates the session (may return 200 or 302)
    twofa_value = pyotp.TOTP(totp_secret).now()
    r2 = s.post(_TWOFA_URL, data={
        "user_id":     user_id,
        "request_id":  request_id,
        "twofa_value": twofa_value,
        "twofa_type":  "totp",
    }, allow_redirects=False)
    log.info("twofa ok (HTTP %d)", r2.status_code)

    # Step 3: hit connect/login with the authenticated session → Kite redirects
    # to the app's redirect_url with ?request_token=xxx
    r3 = s.get(_CONNECT_URL.format(api_key=api_key), allow_redirects=False)
    location = r3.headers.get("Location", "")

    if "request_token=" not in location:
        raise RuntimeError(
            f"request_token not found in redirect (HTTP {r3.status_code}): {location!r}"
        )

    request_token = location.split("request_token=")[1].split("&")[0]
    log.info("got request_token")
    return request_token


def _build_caches(kite: KiteConnect) -> tuple[dict, dict, int]:
    """Returns (instruments_cache, option_tokens, nifty_spot_token)."""
    nifty_spot_token = 256265  # well-known fallback
    for inst in kite.instruments("NSE"):
        if inst["tradingsymbol"] == "NIFTY 50":
            nifty_spot_token = inst["instrument_token"]
            break
    log.info("nifty_spot_token=%d", nifty_spot_token)

    instruments_cache: dict = {}
    option_tokens: dict = {}
    for inst in kite.instruments("NFO"):
        if inst.get("name") != "NIFTY":
            continue
        if inst.get("instrument_type") not in ("CE", "PE"):
            continue
        sym = inst["tradingsymbol"]
        expiry = inst.get("expiry")
        instruments_cache[sym] = {
            "token":       inst["instrument_token"],
            "lot_size":    inst["lot_size"],
            "strike":      inst["strike"],
            "expiry":      expiry.isoformat() if expiry else None,
            "option_type": inst["instrument_type"],
        }
        option_tokens[sym] = inst["instrument_token"]

    log.info("cached %d NIFTY NFO options", len(instruments_cache))
    return instruments_cache, option_tokens, nifty_spot_token


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    log.info("=== morning login start ===")

    api_key     = os.environ["KITE_API_KEY"]
    api_secret  = os.environ["KITE_API_SECRET"]
    user_id     = os.environ["KITE_USER_ID"]
    password    = os.environ["KITE_PASSWORD"]
    totp_secret = os.environ["KITE_TOTP_SECRET"]
    redis_url   = os.environ["REDIS_URL"]

    # 1. Headless Kite login → request_token
    request_token = _get_request_token(api_key, user_id, password, totp_secret)

    # 2. Exchange → access_token
    kite = KiteConnect(api_key=api_key)
    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session_data["access_token"]
    kite.set_access_token(access_token)
    log.info("access_token obtained")

    # 3. Build instrument caches
    instruments_cache, option_tokens, nifty_spot_token = _build_caches(kite)

    # 4. Write to Redis with 26-hour TTL
    r = redis_lib.from_url(redis_url, decode_responses=False)
    r.ping()
    r.setex("kite:access_token",     TTL_SECONDS, access_token)
    r.setex("kite:instruments",      TTL_SECONDS, json.dumps(instruments_cache))
    r.setex("kite:option_tokens",    TTL_SECONDS, json.dumps(option_tokens))
    r.setex("kite:nifty_spot_token", TTL_SECONDS, str(nifty_spot_token))
    log.info("wrote 4 keys to Redis (TTL=%dh)", TTL_SECONDS // 3600)
    log.info("=== morning login complete ===")


if __name__ == "__main__":
    main()
